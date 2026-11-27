"""Run in a separate Python process: no bpy, no UI thread work, no game injection.

Reads shipped catalog metadata without loading bundles. A requested model reads
only its asset scene and required serialized dependencies through UnityPy.
Unity shaders/behaviours are not executed.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if os.environ.get('RUSTER_PYTHON_MODULES'):
    sys.path.insert(0, os.environ['RUSTER_PYTHON_MODULES'])

from ruster.assets.catalog import connect, find, parse_lookup, parse_manifest
from ruster.assets import CONVERTER_VERSION
from ruster.core.settings import validate_install
from ruster.core.project import atomic_write


def status(cache, **values):
    atomic_write(Path(cache) / 'progress.json', json.dumps(values).encode())


def bundle_files(root):
    for path in sorted((Path(root) / 'Bundles').rglob('*')):
        if not path.is_file():
            continue
        try:
            with path.open('rb') as stream:
                if stream.read(8).startswith((b'UnityFS', b'UnityWeb', b'UnityRaw')):
                    yield path
        except OSError:
            continue


def component_pointers(gameobject):
    for entry in gameobject.m_Component:
        yield entry[1] if isinstance(entry, tuple) else entry.component


def builtin_resource(root, name):
    """Resolve Unity's primitive meshes/textures from this game's Resources only."""
    clean = name.replace('\\', '/').rsplit('/', 1)[-1].lower()
    if clean not in {'unity default resources', 'unity_builtin_extra'}:
        return None
    path = Path(root) / 'RustClient_Data' / 'Resources' / clean
    if not path.is_file():
        return None
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Unity built-in resources exceed the 32 MiB read limit')
    return path


def run_child(action, install, cache, **options):
    command = [sys.executable, str(Path(__file__).resolve()), action,
               '--install', str(install), '--cache', str(cache), '--parent-pid', str(os.getpid())]
    for key, value in options.items():
        option = '--' + key.replace('_', '-')
        command += [option] if value is True else [option, str(value)]
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:
        result = subprocess.run(command, timeout=120, **kwargs)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('Asset operation exceeded its 120 second limit') from error
    if result.returncode:
        raise RuntimeError('Asset worker failed or reached its 768 MiB memory limit; see worker.log')


def index_bundle(install, cache, key):
    from ruster.assets.streaming import streamed_bundles
    root = validate_install(install)
    path = (root / key).resolve()
    if not path.is_relative_to(root.resolve()):raise ValueError('Bundle path escapes the Rust installation')
    stat = path.stat()
    db = connect(cache)
    try:
        with streamed_bundles() as UnityPy:
            env = UnityPy.load(str(path))
            entries, names, roots = [], [], []
            for asset_path, pointer in env.container.items():
                if asset_path.lower().endswith('.prefab'):
                    reader = pointer.deref()
                    if reader and reader.type.name == 'GameObject':
                        entries.append((asset_path.lower(), key, reader.assets_file.name, reader.path_id))
                elif asset_path.lower().endswith('manifest.asset'):
                    names.extend(parse_manifest(pointer.deref().read_typetree()))
            for obj in env.objects:
                if obj.type.name == 'TextAsset':
                    data = obj.read()
                    name = data.m_Name.lower()
                    if 'prefab' in name or 'stringpool' in name:
                        script = data.m_Script
                        text = script.decode('utf8', errors='replace') if isinstance(script, bytes) else script
                        names.extend(parse_lookup(text))
            # Keep scene roots for a second pass after all lookup tables are read.
            for obj in env.objects:
                if obj.type.name != 'GameObject':
                    continue
                go = obj.read()
                for pointer in component_pointers(go):
                    if not pointer.path_id:
                        continue
                    component = pointer.deref()
                    if component.type.name == 'Transform' and not component.read().m_Father.path_id:
                        roots.append((go.m_Name.lower(), key, obj.assets_file.name, obj.path_id))
                        break
            with db:
                db.execute('DELETE FROM assets WHERE bundle=?', (key,))
                db.executemany('INSERT OR REPLACE INTO assets VALUES(?,?,?,?)', entries)
                db.executemany('INSERT OR REPLACE INTO names VALUES(?,?)', names)
                db.execute('DELETE FROM roots WHERE bundle=?', (key,))
                db.executemany('INSERT OR REPLACE INTO roots VALUES(?,?,?,?)', roots)
                db.execute('DELETE FROM serialized_files WHERE bundle=?', (key,))
                db.executemany('INSERT OR REPLACE INTO serialized_files VALUES(?,?)',
                               [(asset.name.rsplit('/',1)[-1].lower(), key) for asset in env.assets])
                db.execute('INSERT OR REPLACE INTO bundles VALUES(?,?,?)', (key, stat.st_size, stat.st_mtime_ns))
                db.execute('DELETE FROM failures WHERE bundle=?', (key,))
    finally:
        db.close()


def deep_index(install, cache):
    root = validate_install(install)
    files = list(bundle_files(root))
    if not files:
        raise ValueError('No Unity AssetBundles found in this Rust installation')
    db = connect(cache)
    known = set()
    try:
        for i, path in enumerate(files):
            key = str(path.relative_to(root))
            known.add(key)
            stat = path.stat()
            old = db.execute('SELECT size, mtime FROM bundles WHERE path=?', (key,)).fetchone()
            if old == (stat.st_size, stat.st_mtime_ns):continue
            status(cache, state='INDEXING', current=i + 1, total=len(files), item=path.name)
            try:
                # One short-lived child per bundle releases UnityPy object graphs
                # and native decompression buffers even if cyclic references remain.
                run_child('index-bundle', root, cache, bundle=key)
            except Exception as error:
                with db:
                    db.execute('INSERT OR REPLACE INTO failures VALUES(?,?)', (key, str(error)))
        # Remove records for bundles removed by a Rust update.
        with db:
            for (key,) in db.execute('SELECT path FROM bundles').fetchall():
                if key not in known:
                    db.execute('DELETE FROM assets WHERE bundle=?', (key,))
                    db.execute('DELETE FROM roots WHERE bundle=?', (key,))
                    db.execute('DELETE FROM serialized_files WHERE bundle=?', (key,))
                    db.execute('DELETE FROM bundles WHERE path=?', (key,))
            by_name = {}
            for uid, asset_path in db.execute('SELECT uid,path FROM names'):
                by_name.setdefault(Path(asset_path).stem.lower(), []).append(asset_path)
            for name, matching in by_name.items():
                if len(matching) != 1:
                    continue
                roots = db.execute('SELECT bundle,file,path_id FROM roots WHERE name=?',(name,)).fetchall()
                if len(roots) == 1:
                    db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?)',(matching[0],*roots[0]))
        count = db.execute('SELECT count(*) FROM assets').fetchone()[0]
        ids = db.execute('SELECT count(*) FROM names').fetchone()[0]
        failures = db.execute('SELECT count(*) FROM failures').fetchone()[0]
        if not ids:
            raise ValueError('No embedded prefab ID lookup was found. This Rust build needs an updated lookup adapter.')
        status(cache, state='READY', assets=count, ids=ids, failures=failures,
               install=str(root), current=len(files), total=len(files))
    finally:
        db.close()


def xyz(value):
    return [float(value.x), float(value.y), float(value.z)]


def quaternion(value):
    return [float(value.w), float(value.x), float(value.y), float(value.z)]


def deref(pointer):
    return pointer.read() if pointer and pointer.path_id else None


def export_prefab(env, root_reader, output, uid, asset_path, cache_signature=''):
    from UnityPy.helpers.MeshHelper import MeshHandler
    from ruster.core.asset_categories import is_volume
    volume = is_volume(asset_path)
    colliders = []
    preview_info = None
    meshes, materials, nodes, warnings = {}, {}, [], []
    excluded_renderers=set()
    sound_effect=False
    particle_effect=False
    seen = set()
    sound_components=[]
    base = Path(output).parent
    texture_dir = base / 'textures'
    texture_dir.mkdir(parents=True, exist_ok=True)

    def key(reader):
        return f'{reader.assets_file.name}:{reader.path_id}'

    def texture(entry, field, texenv, material_name):
        if not texenv or not texenv.m_Texture.path_id:return
        prefix='' if field=='texture' else field+'_'
        entry[prefix+'uv_scale']=[texenv.m_Scale.x,texenv.m_Scale.y]
        entry[prefix+'uv_offset']=[texenv.m_Offset.x,texenv.m_Offset.y]
        try:
            tex_reader=texenv.m_Texture.deref()
            token=hashlib.sha256((key(tex_reader)+':'+cache_signature).encode()).hexdigest()[:24]
            target=texture_dir/f'{token}.png'
            if not target.exists():
                from ruster.assets.textures import preview_image
                with preview_image(tex_reader.read()) as image:image.save(target,compress_level=1)
            entry[field]=str(target.relative_to(base))
        except Exception as error:
            traceback.print_exc()
            warnings.append(f'{material_name}: {field} unavailable: {type(error).__name__}: {error}')

    def material(pointer):
        if not pointer or not pointer.path_id:
            return None
        reader = pointer.deref()
        ident = key(reader)
        if ident in materials:
            return ident
        data = reader.read()
        entry = {'name': data.m_Name, 'color': [0.55, 0.55, 0.55, 1.], 'texture': None}
        materials[ident] = entry
        floats=dict(data.m_SavedProperties.m_Floats)
        entry['roughness']=max(0.,min(1.,1-floats.get('_Smoothness',floats.get('_Glossiness',.2))))
        entry['metallic']=max(0.,min(1.,floats.get('_Metallic',0.)))
        mode=floats.get('_Mode',0)
        tags=dict(getattr(data,'m_StringTagMap',[]) or [])
        keywords=set(getattr(data,'m_ValidKeywords',[]) or [])
        keywords.update(str(getattr(data,'m_ShaderKeywords','')).split())
        queue=getattr(data,'m_CustomRenderQueue',-1)
        entry['alpha_clip']=bool(floats.get('_AlphaClip',0) or floats.get('_AlphaCutoffEnable',0) or mode==1 or
            '_ALPHATEST_ON' in keywords or tags.get('RenderType')=='TransparentCutout' or queue==2450)
        entry['alpha_blend']=bool(mode in (2,3) or floats.get('_Surface',0)==1 or floats.get('_SurfaceType',0)==1 or tags.get('RenderType')=='Transparent')
        entry['alpha_cutoff']=max(0.,min(1.,floats.get('_AlphaCutoff',floats.get('_Cutoff',.5))))
        entry['gloss_map_scale']=max(0.,min(1.,floats.get('_GlossMapScale',1.)))
        colors = dict(data.m_SavedProperties.m_Colors)
        color = colors.get('_BaseColor', colors.get('_Color'))
        if color:
            entry['color'] = [color.r, color.g, color.b, color.a]
        emission=colors.get('_EmissiveColor',colors.get('_EmissionColor'))
        if emission:entry['emission']=[emission.r,emission.g,emission.b,1.]
        textures = dict(data.m_SavedProperties.m_TexEnvs)
        def mapped(*names):
            return next((textures[name] for name in names if name in textures and textures[name].m_Texture.path_id),None)
        texture(entry,'texture',mapped('_BaseColorMap','_BaseMap','_MainTex','_AlbedoMap','_AlbedoTex','_DiffuseMap','_BaseTex'),data.m_Name)
        texture(entry,'metallic_gloss_texture',mapped('_MetallicGlossMap','_MaskMap'),data.m_Name)
        texture(entry,'emission_texture',mapped('_EmissiveColorMap','_EmissionMap'),data.m_Name)
        return ident

    def mesh(pointer):
        reader = pointer.deref()
        ident = key(reader)
        if ident not in meshes:
            from ruster.assets import geometry
            token=hashlib.sha256((ident+':'+cache_signature).encode()).hexdigest()[:24]
            target=base/'meshes'/f'{token}.mesh.gz'
            if target.exists():spec=geometry.info(target)
            else:
                handler = MeshHandler(reader.read())
                handler.process()
                spec=geometry.write(target,handler.m_Vertices or [],handler.m_UV0 or [],handler.get_triangles())
            spec['buffer']=str(target.relative_to(base))
            if not spec['vertices'] or not spec['faces']:return None
            meshes[ident]=spec
        return ident

    def walk(reader, parent=None, depth=0,lod_managed=False,prefab_root=False,placement_root=False):
        nonlocal sound_effect,particle_effect
        if depth > 128 or len(nodes) > 20000:
            raise ValueError('Prefab hierarchy exceeds the supported limit')
        ident = key(reader)
        if ident in seen:
            return
        go = reader.read()
        if parent is not None and not prefab_root and not go.m_IsActive:
            return
        # Prefab instances retain one visible LOD, not all overlapping LOD meshes.
        if parent is not None and not prefab_root and not lod_managed and re.search(r'(?:_|\b)lod[1-9]\d*(?:_|\b)', go.m_Name.lower()):
            return
        seen.add(ident)
        components = {}
        component_keys={}
        node_scripts=[]
        node_colliders=[]
        for pointer in component_pointers(go):
            if not pointer or not pointer.path_id:continue
            component_reader = pointer.deref()
            if component_reader:
                kind=component_reader.type.name
                if kind=='AudioSource':sound_effect=True
                elif kind in ('ParticleSystem','ParticleSystemRenderer'):particle_effect=True
                elif kind=='MonoBehaviour':
                    sound_components.append(component_reader)
                    node_scripts.append(component_reader)
                elif kind in ('Transform','RectTransform','MeshRenderer','MeshFilter','SkinnedMeshRenderer','LODGroup'):
                    components[kind]=component_reader.read()
                    component_keys[kind]=key(component_reader)
                elif volume and kind in ('BoxCollider','SphereCollider','CapsuleCollider','MeshCollider'):
                    data=component_reader.read()
                    if getattr(data,'m_Enabled',True):node_colliders.append((kind,data))
        lods=getattr(components.get('LODGroup'),'m_LODs',None)
        if lods:
            first=set()
            later=set()
            first_nonempty=next((i for i,lod in enumerate(lods) if any(r.renderer.path_id for r in lod.renderers)),0)
            for index,lod in enumerate(lods):
                for renderer in lod.renderers:
                    pointer=renderer.renderer
                    if pointer and pointer.path_id:
                        (first if index==first_nonempty else later).add(key(pointer.deref()))
            excluded_renderers.update(later-first)
        transform = components.get('Transform') or components.get('RectTransform')
        if transform is None:
            return
        index = len(nodes)
        node = {'name': go.m_Name, 'parent': parent,
                'position': xyz(transform.m_LocalPosition),
                'rotation': quaternion(transform.m_LocalRotation),
                'scale': xyz(transform.m_LocalScale), 'mesh': None, 'materials': []}
        if placement_root:node.update(position=[0.,0.,0.],rotation=[1.,0.,0.,0.],scale=[1.,1.,1.])
        nodes.append(node)
        for kind,data in node_colliders:
            try:
                spec={'node':index,'type':kind,'center':xyz(data.m_Center) if hasattr(data,'m_Center') else [0.,0.,0.]}
                if kind=='BoxCollider':spec['size']=xyz(data.m_Size)
                elif kind in ('SphereCollider','CapsuleCollider'):
                    spec['radius']=float(data.m_Radius)
                    if kind=='CapsuleCollider':
                        spec.update(height=float(data.m_Height),direction=int(data.m_Direction))
                elif kind=='MeshCollider':
                    if not data.m_Mesh or not data.m_Mesh.path_id:continue
                    spec['mesh']=mesh(data.m_Mesh)
                    if not spec['mesh']:continue
                colliders.append(spec)
            except Exception as error:
                warnings.append(f'{go.m_Name}: volume collider unavailable: {error}')
        renderer = components.get('MeshRenderer') or components.get('SkinnedMeshRenderer')
        filter_ = components.get('MeshFilter') or components.get('SkinnedMeshRenderer')
        renderer_key=component_keys.get('MeshRenderer',component_keys.get('SkinnedMeshRenderer'))
        # Unity's ShadowsOnly (3) meshes cover the visible model to cast cheaper
        # shadows. Drawing them as surfaces creates white patches and z-fighting.
        # Keep the transform node and walk its children, without decoding the
        # hidden mesh or its materials. Off/On/TwoSided (0/1/2) remain visible.
        visible = renderer and getattr(renderer, 'm_Enabled', True) and getattr(renderer, 'm_CastShadows', 1) != 3
        if visible and filter_:
            for script_reader in node_scripts:
                try:
                    script=script_reader.read().m_Script
                    runtime_flare=bool(script and script.path_id and
                        script.deref().read().m_ClassName=='DistanceFlareLOD')
                except (AttributeError,KeyError,TypeError):runtime_flare=False
                if runtime_flare:
                    # Unity's view-dependent additive flare is a billboard,
                    # not part of the fixture. Blender otherwise draws its
                    # full 12 m quad as an opaque glowing wall. Keep its node
                    # and children, but omit this runtime-only render surface.
                    node['preview_effect']='DISTANCE_FLARE';visible=False
                    break
        if visible and filter_ and renderer_key not in excluded_renderers:
            try:
                if filter_.m_Mesh.path_id:
                    node['mesh'] = mesh(filter_.m_Mesh)
                    node['materials'] = [material(p) for p in renderer.m_Materials]
            except Exception as error:
                warnings.append(f'{go.m_Name}: geometry unavailable: {error}')
        for child in transform.m_Children:
            child_transform = deref(child)
            if child_transform:
                walk(child_transform.m_GameObject.deref(), index, depth + 1,lod_managed or bool(lods))

    walk(root_reader)
    marker=''
    helper_bounds=None
    if not meshes:
        for reader in sound_components:
            data=reader.read()
            if getattr(data,'soundDefinition',None):sound_effect=True
            if hasattr(data,'HeightMap') and hasattr(data,'size'):
                marker='TERRAIN';helper_bounds=xyz(data.size)
            # Tree/entity templates often contain no geometry at all. Their
            # visual is a separate GameObjectRef, resolved by GUID at runtime.
            for field in ('model','prefab','modelPrefab','worldModelPrefab'):
                value=getattr(data,field,None)
                target=None
                if value and getattr(value,'path_id',0):
                    component=value.deref()
                    if component.type.name=='GameObject':target=component
                    else:
                        component_data=component.read()
                        if getattr(component_data,'m_GameObject',None):target=component_data.m_GameObject.deref()
                elif value and getattr(value,'guid','') and hasattr(env,'resolve_prefab'):
                    target=env.resolve_prefab(value.guid)
                    if target is None:warnings.append(f'Visual prefab reference could not be resolved: {value.guid}')
                if target:
                    walk(target,parent=0,depth=1,prefab_root=True)
                    if meshes:break
            if meshes:break
    if not meshes and not warnings and sound_effect:
        marker='SOUND'
    if not meshes and not warnings and particle_effect:marker='EFFECT'
    if not meshes and not volume:
        from ruster.core.spawn_previews import preview
        candidate=preview(asset_path)
        if candidate and hasattr(env,'resolve_asset_path'):
            target=env.resolve_asset_path(candidate['path'])
            if target:
                walk(target,parent=0,depth=1,prefab_root=True,placement_root=True)
                if meshes:preview_info=candidate;marker=''
            if not meshes:warnings.append('Spawner crate preview unavailable in the local game assets')
    if volume:marker='VOLUME'
    if not meshes and not marker and not warnings:marker='HELPER'
    if not meshes and not marker:
        raise ValueError('Prefab geometry could not be read; ' + '; '.join(warnings[:3]))
    result = {'schema': 1, 'uid': uid, 'path': asset_path, 'nodes': nodes,
              'meshes': meshes, 'materials': materials, 'warnings': warnings, 'marker':marker,
              'helper_bounds':helper_bounds, 'colliders':colliders, 'preview':preview_info}
    # Geometry lives in bounded per-mesh buffers; don't allocate another giant
    # JSON string/UTF-8 copy of a whole monument during cache serialization.
    from tempfile import mkstemp
    descriptor,temporary=mkstemp(prefix='.prefab-',dir=base);os.close(descriptor)
    try:
        with gzip.open(temporary,'wt',compresslevel=1,encoding='utf-8') as stream:
            json.dump(result,stream,separators=(',',':'))
        os.replace(temporary,output)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    return warnings


def bundle_inventory(root):
    inventory = []
    for path in bundle_files(root):
        stat = path.stat()
        inventory.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    return inventory


def extract_one(install, cache, uid, inventory=None):
    from ruster.assets.streaming import streamed_bundles
    entry=find(cache,uid)
    selected=set()
    if entry and entry[2] and entry[2].startswith('@scene:'):
        selected.add('buildplayer-'+entry[2][7:].lower())
    with streamed_bundles(lambda name: name.lower() in selected or name.lower().endswith(('.ress','.resource')),
                          skip_containers=True) as UnityPy:
        return extract_batch(UnityPy, install, cache, [uid], inventory)


def extract_batch(UnityPy, install, cache, ids, inventory=None):
    root = validate_install(install)
    cache = Path(cache)
    target = cache / 'prefabs'
    target.mkdir(parents=True, exist_ok=True)
    failures, done = [], 0
    current_bundle, env = None, None
    if inventory is None:
        inventory = bundle_inventory(root)
    with connect(cache) as db:
        source=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_metadata'").fetchone()
        catalog_source=db.execute('SELECT value FROM catalog_metadata').fetchall() if source else []
        catalog_signature=hashlib.sha256(repr((catalog_source,inventory)).encode()).hexdigest()
    from ruster.assets import locations
    cab_bundles=locations.prepare(cache,catalog_signature)
    discovered_cabs={};discovered_roots=[]
    candidates=iter(sorted((root/name for name, _, _ in inventory),key=lambda p:('/shared/' not in p.as_posix(),p.as_posix())))

    class LocalEnvironment(UnityPy.Environment):
        def resolve_prefab(self,guid):
            from ruster.assets.references import guid_path
            from contextlib import closing
            asset_path=guid_path(self,cache,root,guid)
            if not asset_path:return None
            return self.resolve_asset_path(asset_path)

        def resolve_asset_path(self,asset_path):
            from contextlib import closing
            with closing(connect(cache)) as db:
                entry=db.execute('SELECT bundle,file,path_id FROM assets WHERE path=? COLLATE NOCASE',(asset_path,)).fetchone()
            if not entry:return None
            bundle,file_name,path_id=entry
            if not self.get_cab(file_name if not file_name.startswith('@scene:') else 'BuildPlayer-'+file_name[7:]):
                self.load_file(str(root/bundle),is_dependency=True)
            name='BuildPlayer-'+file_name[7:] if file_name.startswith('@scene:') else file_name
            data=self.get_cab(name)
            if not data:return None
            if not file_name.startswith('@scene:'):return data.objects.get(path_id)
            objects=data.objects
            readers=objects.of_type('GameObject') if hasattr(objects,'of_type') else objects.values()
            for candidate in readers:
                if candidate.type.name!='GameObject':continue
                name=candidate.peek_name().lower()
                if name.startswith('assets/') and name.endswith('.prefab'):
                    discovered_roots.append(('BuildPlayer-'+file_name[7:],candidate.path_id,name,bundle))
                if name==asset_path:return candidate
            return None

        def find_file(self, name, is_dependency=True):
            clean = name.replace('\\', '/').rsplit('/',1)[-1].lower()
            existing = self.get_cab(clean)
            if existing:
                return existing
            resource = builtin_resource(root, name)
            if resource:
                self.load_file(str(resource), is_dependency=True)
                return self.get_cab(clean)
            bundle = cab_bundles.get(clean)
            if not bundle:
                # Dependency location needs only UnityFS directory headers, not
                # an object/mesh index. Resolve them once per isolated request.
                from ruster.assets.streaming import streamed_bundles
                with streamed_bundles(lambda _name:False) as directory_unity:
                    for candidate in candidates:
                        if candidate.suffix.lower()!='.bundle':continue
                        directory_env=directory_unity.load(str(candidate))
                        for container in directory_env.files.values():
                            for node in getattr(container,'files',{}):
                                cab=node.rsplit('/',1)[-1].lower()
                                location=str(candidate.relative_to(root))
                                cab_bundles[cab]=location;discovered_cabs[cab]=location
                        if clean in cab_bundles:break
                bundle=cab_bundles.get(clean)
            if bundle:
                self.load_file(str(root/bundle), is_dependency=True)
                existing = self.get_cab(clean)
                if existing:
                    return existing
            return None
    # Group by bundle to avoid reloading it for every prefab in the same batch.
    jobs = []
    for uid in sorted(set(ids)):
        entry = find(cache, uid)
        if entry and entry[1]:
            jobs.append((entry[1], uid, entry))
        else:
            failures.append({'uid': uid, 'error': 'No matching prefab in the local asset catalog'})
    for bundle, uid, (asset_path, _, file_name, path_id) in sorted(jobs):
        status(cache, state='EXTRACTING', current=done, total=len(jobs), item=asset_path)
        try:
            path = root / bundle
            stat = path.stat()
            signature = f'{CONVERTER_VERSION}:{stat.st_size}:{stat.st_mtime_ns}:{catalog_signature}'
            from ruster.core.asset_categories import is_volume
            if is_volume(asset_path):signature+=':volumes-1'
            from ruster.core.spawn_previews import preview
            if preview(asset_path):signature+=':spawner-preview-1'
            # Backward-compatible preview correction; invalidate light caches
            # on request without requiring the running editor to restart.
            if '/light_fixtures/' in asset_path:signature+=':render-effects-1'
            stamp = target / f'{uid}.stamp'
            output = target / f'{uid}.json.gz'
            if not (output.exists() and stamp.exists() and stamp.read_text() == signature):
                if bundle != current_bundle:
                    env = LocalEnvironment(str(path))
                    current_bundle = bundle
                if file_name.startswith('@scene:'):
                    data=env.get_cab('BuildPlayer-'+file_name[7:])
                    reader=None
                    if data:
                        objects=data.objects
                        candidates_=objects.of_type('GameObject') if hasattr(objects,'of_type') else objects.values()
                        for candidate in candidates_:
                            if candidate.type.name!='GameObject':continue
                            name=candidate.read().m_Name.lower()
                            if name.startswith('assets/') and name.endswith('.prefab'):
                                discovered_roots.append(('BuildPlayer-'+file_name[7:],candidate.path_id,name,bundle))
                            if name==asset_path.lower():reader=candidate;break
                else:
                    # Parse only the requested serialized node, even with an old
                    # fully indexed catalog.
                    data=env.get_cab(file_name)
                    reader=data.objects.get(path_id) if data and hasattr(data,'objects') else None
                if reader is None:
                    raise ValueError('Prefab object is no longer in the indexed bundle; rebuild the catalog')
                warnings = export_prefab(env, reader, output, uid, asset_path,signature)
                atomic_write(stamp, signature.encode())
                if warnings:
                    failures.append({'uid': uid, 'error': '; '.join(warnings)})
            done += 1
        except Exception as error:
            failures.append({'uid': uid, 'error': str(error) or type(error).__name__})
    locations.remember(cache,discovered_cabs,discovered_roots)
    return {'failures': failures, 'done': done}


def index(install,cache,rustedit=None):
    from ruster.assets.metadata import refresh
    root=validate_install(install)
    status(cache,state='CATALOG',current=0,total=1,item='Reading shipped prefab metadata')
    result=refresh(root,cache,rustedit)
    status(cache,state='READY',failures=0,current=1,total=1,**result)


def extract(install, cache, ids):
    from tempfile import TemporaryDirectory
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    failures, done = [], 0
    ids = sorted(set(ids))
    for uid in ids:
        entry = find(cache, uid)
        status(cache, state='EXTRACTING', current=done, total=len(ids),
               item=entry[0] if entry else str(uid))
        # The child writes its own report; retain aggregate progress in the parent.
        with TemporaryDirectory(prefix='asset-report-', dir=cache) as directory:
            report = Path(directory) / 'report.json'
            try:
                run_child('extract-one', install, cache, uid=uid, report=report)
                result = json.loads(report.read_text())
                failures.extend(result['failures'])
                done += result['done']
            except Exception as error:
                failures.append({'uid': uid, 'error': str(error)})
    atomic_write(cache / 'asset-report.json', json.dumps(failures, indent=2).encode())
    status(cache, state='READY', extracted=done, failures=len(failures), current=len(ids), total=len(ids))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['index', 'extract', 'index-bundle', 'extract-one'])
    parser.add_argument('--install', required=True)
    parser.add_argument('--cache', required=True)
    parser.add_argument('--ids-file')
    parser.add_argument('--bundle')
    parser.add_argument('--uid', type=int)
    parser.add_argument('--use-thumbnail-inventory', action='store_true')
    parser.add_argument('--report')
    parser.add_argument('--rustedit')
    parser.add_argument('--parent-pid', type=int)
    args = parser.parse_args()
    try:
        from ruster.core.process_limits import protect_process, ASSET_MEMORY, MIB
        child = args.action in ('index-bundle', 'extract-one')
        protect_process(ASSET_MEMORY if child else 1024 * MIB, args.parent_pid)
        # Avoid BLAS thread pools/reservations in metadata and conversion workers.
        os.environ['OPENBLAS_NUM_THREADS'] = '1'
        os.environ['OMP_NUM_THREADS'] = '1'
        if args.action == 'index-bundle':
            index_bundle(args.install, args.cache, args.bundle)
        elif args.action == 'extract-one':
            inventory = None
            if args.use_thumbnail_inventory:
                inventory = [tuple(row) for row in json.loads((Path(args.cache)/'bundles.json').read_text())]
                if any(Path(name).anchor or '..' in Path(name).parts for name, _, _ in inventory):
                    raise ValueError('Invalid path in thumbnail bundle inventory')
            result = extract_one(args.install, args.cache, args.uid, inventory)
            atomic_write(args.report, json.dumps(result).encode())
        elif args.action == 'index':
            index(args.install, args.cache,args.rustedit)
        else:
            ids = json.loads(Path(args.ids_file).read_text())
            extract(args.install, args.cache, ids)
    except Exception as error:
        status(args.cache, state='ERROR', error=str(error))
        traceback.print_exc()
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
