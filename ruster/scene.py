"""Blender scene adapter. Only placement roots are serialized back to Rust."""
import base64
import gzip
import hashlib
import json
import math
import re
from pathlib import Path
import uuid

import bpy
from mathutils import Matrix, Vector, Euler, Quaternion
from .core import wire
from .core.rust_map import RustMap, prefab_id, prefab_transform, update_transform
from .core.project import atomic_write
from .core.asset_names import display_name

BASIS = Matrix(((1, 0, 0), (0, 0, 1), (0, 1, 0)))


def to_blender(values):
    return BASIS @ Vector(values)


def to_unity(values):
    return tuple(BASIS @ Vector(values))


def unity_euler(values):
    rotation = Euler(tuple(math.radians(a) for a in values), 'ZXY').to_matrix()
    return (BASIS @ rotation @ BASIS).to_quaternion()


def blender_euler(rotation):
    matrix = BASIS @ rotation.to_matrix() @ BASIS
    return tuple(math.degrees(a) for a in matrix.to_euler('ZXY'))


def cache_dir():
    from .core.settings import user_root
    return user_root() / 'assets'


def material(name, color):
    mat = bpy.data.materials.get(name)
    if mat is not None and mat.library:mat=None
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = color
    return mat


def new_collection(name, parent=None):
    collection = bpy.data.collections.new(name)
    (parent or bpy.context.scene.collection).children.link(collection)
    return collection


def clear_scene():
    from . import live_preview
    live_preview.document_replaced()
    active = bpy.context.view_layer.objects.active
    if active and active.mode != 'OBJECT':
        # Properties may be pinned to the preview Scene and expose no object
        # context. New/Open still acts on the actual view-layer edit object.
        with bpy.context.temp_override(object=active, active_object=active):
            bpy.ops.object.mode_set(mode='OBJECT')
    for obj in list(bpy.context.scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in list(bpy.data.collections):
        if collection.users == 0 or collection.name in {'Collection', 'Terrain', 'Prefabs', 'Paths', 'Build'}:
            bpy.data.collections.remove(collection)


def create_terrain(world):
    import numpy as np
    n, raw = world.height_grid()
    # The viewport resolution is independent of authoritative full-resolution data.
    detail=int(bpy.context.scene.ruster.preview_resolution)
    indices = np.unique(np.linspace(0, n - 1, min(n, detail), dtype=int))
    grid = np.asarray(raw, dtype=np.float64).reshape(n, n)
    heights = grid[np.ix_(indices, indices)] / 32766.0 * 1000.0 - 500.0
    m = len(indices)
    positions = indices / (n - 1) * world.size - world.size / 2
    xx, yy = np.meshgrid(positions, positions)
    vertices = np.column_stack((xx.ravel(), yy.ravel(), heights.ravel()))
    faces = [(z*m+x, z*m+x+1, (z+1)*m+x+1, (z+1)*m+x) for z in range(m-1) for x in range(m-1)]
    mesh = bpy.data.meshes.new('Rust terrain preview')
    mesh.from_pydata(vertices.tolist(), [], faces)
    mesh.update()
    obj = bpy.data.objects.new('Terrain', mesh)
    (bpy.data.collections.get('Terrain') or new_collection('Terrain')).objects.link(obj)
    obj['ruster_kind'] = 'TERRAIN'
    if bpy.context.scene.ruster.native_ground:obj['ruster_ground_preview']=True
    obj['ruster_resolution'] = n
    obj.hide_select = True
    obj.data.materials.append(material('Terrain', (0.24, 0.33, 0.19, 1)))
    # Give the terrain a useful biome/splat preview using vertex colors.
    layers = world.layers
    splat = layers.get('splat', b'')
    res = math.isqrt(len(splat) // 8)
    if res > 0 and res * res * 8 == len(splat):
        weights = np.frombuffer(splat, dtype=np.uint8).reshape(8, res, res)
        lookup = np.minimum((indices / (n - 1) * (res - 1)).astype(int), res - 1)
        palette = np.array([(0.34,0.24,0.15), (0.85,0.89,0.93), (0.65,0.55,0.32),
                            (0.29,0.3,0.29), (0.22,0.35,0.12), (0.11,0.22,0.08),
                            (0.4,0.39,0.35), (0.37,0.34,0.29)])
        sample = weights[:, lookup[:, None], lookup[None, :]].reshape(8, -1).T / 255
        colors = np.column_stack((sample @ palette, np.ones(m*m)))
        attr = mesh.color_attributes.new(name='RustGround', type='FLOAT_COLOR', domain='POINT')
        attr.data.foreach_set('color', colors.ravel())
        mat = material('Rust ground preview', (0.3,0.4,0.2,1))
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        attr_node = nodes.get('RustGround') or nodes.new('ShaderNodeVertexColor')
        attr_node.name = 'RustGround'
        attr_node.layer_name = 'RustGround'
        mat.node_tree.links.new(attr_node.outputs['Color'], nodes.get('Principled BSDF').inputs['Base Color'])
        mesh.materials.clear()
        mesh.materials.append(mat)
    return obj


def create_paths(world):
    collection = new_collection('Paths')
    for i, fields in enumerate(world.paths):
        name = wire.get(fields, 1, b'Path').decode('utf8', errors='replace')
        nodes = [wire.vector(f.value) for f in fields if f.number == 15 and f.wire == 2]
        if not nodes:
            continue
        curve = bpy.data.curves.new(name, 'CURVE')
        curve.dimensions = '3D'
        spline = curve.splines.new('POLY')
        spline.points.add(len(nodes) - 1)
        for point, values in zip(spline.points, nodes):
            point.co = (*to_blender(values), 1)
        curve.bevel_depth = 0.3
        curve.resolution_u = 1
        obj = bpy.data.objects.new(name, curve)
        collection.objects.link(obj)
        from .editing import configure_path
        configure_path(obj,fields)
        obj.data.materials.append(material('Path overlay', (0.8, 0.5, 0.12, 1)))


def name_prefab(obj, source):
    generated=obj.get('ruster_generated_name')
    if generated and obj.name!=generated:return  # Keep a placement the user renamed.
    if not generated and not re.fullmatch(r'Prefab '+str(obj.get('ruster_uid',''))+r'(?:\.\d+)?',obj.name):
        # Older saved projects do not record generated names. Preserve their
        # existing labels while recovering/reloading their model instances.
        return
    base=display_name(source)
    if generated and re.fullmatch(re.escape(base)+r'(?:\.\d{3,})?',obj.name):return
    # Blender's native name allocator avoids rescanning every prior copy for
    # each repeated placement (quadratic work on large prefab compositions).
    obj.name=base
    obj['ruster_generated_name']=obj.name


def model_placeholder(obj):
    """A wire box for an unresolved placement; never exported as geometry."""
    if obj.get('ruster_visual') or obj.get('ruster_marker'):
        return
    obj.empty_display_type = 'CUBE'
    obj.empty_display_size = .5
    if not obj.get('ruster_model_status'):
        obj['ruster_model_status'] = 'Model not loaded · wireframe placeholder'


def model_attached(obj):
    """Remove the loading box once the original linked model is visible."""
    obj['ruster_visual'] = True
    if obj.get('ruster_model_error_kind') in {'PREVIEW','PREVIEW_UPGRADE'}:
        del obj['ruster_model_error_kind']
        if 'ruster_model_error' in obj:del obj['ruster_model_error']
    obj['ruster_model_status'] = 'Model loaded'
    preview=obj.instance_collection.get('ruster_preview') if obj.instance_collection else None
    if preview:
        obj['ruster_spawner_preview']=preview
        obj['ruster_model_status']='Spawner · crate preview'
    elif 'ruster_spawner_preview' in obj:del obj['ruster_spawner_preview']
    for field in ('ruster_marker', 'ruster_helper_bounds'):
        if field in obj:
            del obj[field]
    obj.empty_display_type = 'PLAIN_AXES'
    obj.empty_display_size = .0001


def root_object(fields, collection):
    uid = prefab_id(fields)
    obj = bpy.data.objects.new(f'Prefab {uid}', None)
    collection.objects.link(obj)
    obj['ruster_kind'] = 'PREFAB'
    obj['ruster_uid'] = str(uid)  # Blender integer ID properties are signed.
    obj['ruster_record'] = base64.b64encode(wire.serialize(fields)).decode()
    obj['ruster_instance'] = uuid.uuid4().hex
    obj['ruster_visual'] = False
    position, rotation, scale = prefab_transform(fields)
    obj.location = to_blender(position)
    obj.rotation_mode = 'XYZ'
    obj.rotation_euler = unity_euler(rotation).to_euler('XYZ')
    obj.scale = to_blender(scale)
    model_placeholder(obj)
    obj.color = (1, .36, .08, 1)
    return obj


def import_map(path):
    world = RustMap.load(path)
    world.height_grid()  # Validate terrain before discarding the current scene.
    clear_scene()
    scene = bpy.context.scene
    scene.ruster.mode = 'MAP'
    scene.ruster.map_size = world.size
    scene.ruster.title = Path(path).stem
    scene.ruster.project_path = ''
    scene.ruster.source_map = str(Path(path).resolve())
    scene.ruster.terrain_state = None
    from . import editing
    editing.clear_cache()
    create_terrain(world)
    collection = new_collection('Prefabs')
    for fields in world.prefabs:
        obj = root_object(fields, collection)
        attach_visual(obj)
    create_paths(world)
    new_collection('Build')
    editing.refresh()
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                space = area.spaces.active
                space.clip_end = max(world.size * 4, 10000)
                space.region_3d.view_distance = world.size * 0.9
                space.region_3d.view_location = (0, 0, 0)
                space.region_3d.view_rotation = Euler((math.radians(55), 0, math.radians(35))).to_quaternion()
                space.show_region_ui = True
                space.overlay.show_floor = False
                space.shading.type = 'MATERIAL'
                space.shading.studiolight_rotate_z = 0.5
    bpy.context.view_layer.update()
    return world


def restore_map_tools():
    from . import editing
    editing.clear_cache()
    # Upgrade preview-only paths in projects saved by the first version.
    legacy=[o for o in bpy.context.scene.objects if o.get('ruster_kind')=='PATH_PREVIEW']
    if legacy:
        world=RustMap.load(bpy.context.scene.ruster.source_map)
        for obj,fields in zip(legacy,world.paths):
            editing.configure_path(obj,fields)
    for root in prefab_roots():
        from .protection import protect_root
        model_placeholder(root)
        protect_root(root)
    editing.refresh()


def model_asset_key(uid,signature=None):
    if signature is None:
        stamp=cache_dir()/'prefabs'/f'{uid}.stamp'
        signature=stamp.read_text() if stamp.exists() else ''
    token=hashlib.sha256(signature.encode()).hexdigest()[:12] if signature else ''
    return str(uid)+(':'+token if token else '')+':view4'


def attach_visual(root,force=False,prepared=False,*,transient=False):
    if root.get('ruster_visual') and not force:
        return True
    # Also upgrade unresolved placements restored from older saved projects.
    model_placeholder(root)
    path = cache_dir() / 'prefabs' / f"{root['ruster_uid']}.json.gz"
    if not path.exists():
        return bool(root.get('ruster_visual'))
    stamp=path.with_name(str(root['ruster_uid'])+'.stamp')
    signature=stamp.read_text() if stamp.exists() else ''
    from .assets import CONVERTER_VERSION
    if signature.startswith('converter-') and not signature.startswith(CONVERTER_VERSION+':'):
        # Reconvert only a requested model after a material format upgrade.
        # Already linked/packed projects keep their offline models above.
        from . import app
        updated=app.updated_version()
        message=(f'Rustmatics {updated} is installed; save your project and restart Rustmatics to load these models.' if updated else
                 'This model cache was made by a different Rustmatics version. Save and restart Rustmatics, then use Assets > Load Selected Models.')
        root['ruster_model_error']=message;root['ruster_model_error_kind']='CACHE_VERSION'
        root['ruster_model_status']='Model cache version mismatch'
        return False
    if root.get('ruster_model_error_kind')=='CACHE_VERSION':
        del root['ruster_model_error_kind']
        if 'ruster_model_error' in root:del root['ruster_model_error']
    # Display-only changes rebuild the small Blender preview from cached data;
    # existing packed originals remain valid for offline projects.
    asset_key=model_asset_key(root['ruster_uid'],signature)
    collection_name = 'RusterAsset:' + asset_key
    match=bpy.data.collections.get(collection_name)
    from .protection import VERSION
    existing=match if match and match.library and match.get('ruster_model_verified') and match.get('ruster_model_version')==VERSION else None
    if existing:
        root.instance_type = 'COLLECTION'
        asset_name=existing.get('ruster_asset_name',root.name)
        from .protection import freeze
        root.instance_collection = freeze(existing,root['ruster_uid'])
        model_attached(root)
        name_prefab(root,asset_name)
        return True
    from .protection import library_path,linked_collection
    model_path=library_path(asset_key)
    if model_path.is_file():
        model=linked_collection(model_path,collection_name)
        if model.get('ruster_asset_key')!=asset_key or not model.get('ruster_model_verified') or model.get('ruster_model_version')!=VERSION:
            raise ValueError('Cached prefab model does not match the original asset; refresh its assets')
        root.instance_type='COLLECTION';root.instance_collection=model
        model_attached(root)
        name_prefab(root,model.get('ruster_asset_name',root.name))
        return True
    if not bpy.app.background and not prepared:
        # Placement, replace, restore and assembly import also reach this path.
        # Only the bounded loader may construct a prepared foreground preview.
        from .app import queue_visuals
        queue_visuals([int(root['ruster_uid'])])
        root['ruster_model_status']='Preparing original model preview…'
        return False
    with gzip.open(path, 'rt') as stream:
        asset = json.load(stream)
    if asset.get('schema') != 1:
        return False
    from .core.asset_categories import is_volume
    from .core.spawn_previews import preview
    if (asset.get('marker') in {'HELPER','SOUND'} and not asset.get('meshes') and
        ((is_volume(asset['path']) and asset.get('marker')!='VOLUME') or preview(asset['path']))):
        # Old caches contain only a generic marker for these assets. Queue a
        # targeted conversion instead of silently retaining the old sphere.
        if 'ruster_marker' in root:del root['ruster_marker']
        root['ruster_model_error_kind']='PREVIEW_UPGRADE'
        root['ruster_model_status']='Loading updated volume or spawner preview…'
        model_placeholder(root)
        return False
    if asset.get('marker')=='VOLUME':
        from . import volumes
        volumes.attach(root,asset,path.parent)
        if 'ruster_model_error' in root:del root['ruster_model_error']
        if 'ruster_model_error_kind' in root:del root['ruster_model_error_kind']
        return True
    if asset.get('marker') and not asset['meshes']:
        marker=asset['marker']
        from .core.asset_names import effect_path
        if marker in {'HELPER','SOUND'} and effect_path(asset['path']):marker='EFFECT'
        suffix={'TERRAIN':' (Terrain Stamp)','HELPER':' (Helper)'}
        name_prefab(root,display_name(asset['path'])+suffix.get(marker,''))
        root.instance_type='NONE';root.instance_collection=None
        root['ruster_visual']=False
        root['ruster_marker']=marker
        root['ruster_helper_preview_version']=1
        root['ruster_model_status']={'SOUND':'Sound effect · this prefab has no visible mesh',
            'EFFECT':'Runtime effect · particles or audio play in Rust; this prefab has no static building mesh',
            'TERRAIN':'Terrain stamp · modifies map terrain; this prefab has no static model',
            'HELPER':'Rust helper · this prefab contains no static model'}[marker]
        root.empty_display_type={'TERRAIN':'CUBE','SOUND':'SINGLE_ARROW','EFFECT':'ARROWS','HELPER':'PLAIN_AXES'}[marker]
        root.empty_display_size=.5 if marker=='TERRAIN' else .25
        root.show_name=True
        if asset.get('helper_bounds'):root['ruster_helper_bounds']=asset['helper_bounds']
        if 'ruster_model_error' in root:del root['ruster_model_error']
        return True
    # Collection instances make the whole prefab clickable as one placement and
    # avoid creating thousands of duplicate hierarchy objects for repeated assets.
    collection = bpy.data.collections.new(collection_name)
    materials = {}
    for ident, spec in asset['materials'].items():
        name = 'Rust ' + asset_key + ' ' + hashlib.sha256(ident.encode()).hexdigest()[:16]
        # Rebuild from source rather than trusting editable legacy preview data.
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = spec['color']
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get('Principled BSDF')
        bsdf.inputs['Base Color'].default_value = spec['color']
        bsdf.inputs['Roughness'].default_value = spec.get('roughness',.8)
        bsdf.inputs['Metallic'].default_value = spec.get('metallic',0.)
        emission=spec.get('emission',[0,0,0,1])
        bsdf.inputs['Emission Color'].default_value=emission
        bsdf.inputs['Emission Strength'].default_value=1 if any(emission[:3]) else 0

        def image_node(field,non_color=False):
            if not spec.get(field):return None
            tex_path = (path.parent / spec[field]).resolve()
            if tex_path.is_relative_to(path.parent.resolve()) and tex_path.exists():
                color_space='Non-Color' if non_color else 'sRGB'
                image=next((image for image in bpy.data.images if not image.library and
                    image.filepath==str(tex_path) and image.colorspace_settings.name==color_space),None)
                if image is None:
                    image=bpy.data.images.load(str(tex_path),check_existing=False)
                    image.colorspace_settings.name=color_space
                image.pack()
                node = mat.node_tree.nodes.new('ShaderNodeTexImage')
                node.image = image;node.label=field
                prefix='' if field=='texture' else field+'_'
                if spec.get(prefix+'uv_scale') or spec.get(prefix+'uv_offset'):
                    uv=mat.node_tree.nodes.new('ShaderNodeTexCoord')
                    mapping=mat.node_tree.nodes.new('ShaderNodeMapping')
                    mapping.inputs['Scale'].default_value=(*spec.get(prefix+'uv_scale',[1,1]),1)
                    mapping.inputs['Location'].default_value=(*spec.get(prefix+'uv_offset',[0,0]),0)
                    mat.node_tree.links.new(uv.outputs['UV'],mapping.inputs['Vector'])
                    mat.node_tree.links.new(mapping.outputs['Vector'],node.inputs['Vector'])
                return node
            return None

        node=image_node('texture')
        if node:
            mat.node_tree.nodes.active=node  # Workbench uses the base texture, never the mask.
            mat.node_tree.links.new(node.outputs['Color'],bsdf.inputs['Base Color'])
        if spec.get('alpha_clip') or spec.get('alpha_blend'):
            mat.surface_render_method='DITHERED'
            bsdf.inputs['Alpha'].default_value=spec['color'][3]
            if node:
                alpha=mat.node_tree.nodes.new('ShaderNodeMath');alpha.operation='MULTIPLY'
                alpha.inputs[1].default_value=spec['color'][3]
                mat.node_tree.links.new(node.outputs['Alpha'],alpha.inputs[0])
                output=alpha.outputs[0]
                if spec.get('alpha_clip'):
                    cutoff=mat.node_tree.nodes.new('ShaderNodeMath');cutoff.operation='GREATER_THAN'
                    cutoff.inputs[1].default_value=spec.get('alpha_cutoff',.5)
                    mat.node_tree.links.new(output,cutoff.inputs[0]);output=cutoff.outputs[0]
                mat.node_tree.links.new(output,bsdf.inputs['Alpha'])
        mask=image_node('metallic_gloss_texture',non_color=True)
        if mask:
            channels=mat.node_tree.nodes.new('ShaderNodeSeparateColor')
            mat.node_tree.links.new(mask.outputs['Color'],channels.inputs['Color'])
            mat.node_tree.links.new(channels.outputs['Red'],bsdf.inputs['Metallic'])
            gloss=mat.node_tree.nodes.new('ShaderNodeMath');gloss.operation='MULTIPLY'
            gloss.inputs[1].default_value=spec.get('gloss_map_scale',1.)
            mat.node_tree.links.new(mask.outputs['Alpha'],gloss.inputs[0])
            rough=mat.node_tree.nodes.new('ShaderNodeMath');rough.operation='SUBTRACT'
            rough.inputs[0].default_value=1.
            mat.node_tree.links.new(gloss.outputs[0],rough.inputs[1])
            mat.node_tree.links.new(rough.outputs[0],bsdf.inputs['Roughness'])
        glow=image_node('emission_texture')
        if glow:
            tint=mat.node_tree.nodes.new('ShaderNodeMixRGB');tint.blend_type='MULTIPLY'
            tint.inputs[0].default_value=1.;tint.inputs[2].default_value=emission
            mat.node_tree.links.new(glow.outputs['Color'],tint.inputs[1])
            mat.node_tree.links.new(tint.outputs[0],bsdf.inputs['Emission Color'])
        if node:mat.node_tree.nodes.active=node
        materials[ident] = mat
    meshes = {}
    for ident, spec in asset['meshes'].items():
        name = 'Rust ' + asset_key + ' ' + hashlib.sha256(ident.encode()).hexdigest()[:16]
        # Rebuild from source rather than trusting editable legacy preview data.
        mesh = bpy.data.meshes.new(name)
        if spec.get('buffer'):
            import numpy as np
            from .assets import geometry
            source=(path.parent/spec['buffer']).resolve()
            if not source.is_relative_to(path.parent.resolve()):raise ValueError('Invalid mesh buffer path')
            points,coords,indices,counts=geometry.read(source)
            # Fill native arrays directly. Python tuples for every vertex/loop
            # used several times more memory than Blender's actual mesh data.
            vertices=np.ascontiguousarray(points[:,[0,2,1]],dtype='f4')
            loops=np.ascontiguousarray(indices[:,::-1].reshape(-1),dtype='i4')
            mesh.vertices.add(len(points));mesh.vertices.foreach_set('co',vertices.reshape(-1))
            mesh.loops.add(len(loops));mesh.loops.foreach_set('vertex_index',loops)
            mesh.polygons.add(len(indices))
            mesh.polygons.foreach_set('loop_start',np.arange(len(indices),dtype='i4')*3)
            mesh.polygons.foreach_set('loop_total',np.full(len(indices),3,dtype='i4'))
            mesh.polygons.foreach_set('material_index',np.repeat(np.arange(len(counts),dtype='i4'),counts))
            mesh.update(calc_edges=True)
            if len(coords):
                layer=mesh.uv_layers.new()
                layer.data.foreach_set('uv',np.ascontiguousarray(coords[loops]).reshape(-1))
            meshes[ident]=mesh
            continue
        faces, slots = [], []
        for slot, triangles in enumerate(spec['submeshes']):
            # Coordinate basis changes handedness, so reverse winding.
            faces.extend(tuple(reversed(t)) for t in triangles)
            slots.extend([slot] * len(triangles))
        mesh.from_pydata([tuple(to_blender(v)) for v in spec['vertices']], [], faces)
        mesh.update()
        for poly, slot in zip(mesh.polygons, slots):
            poly.material_index = slot
        uv = spec.get('uv', [])
        if uv and len(uv) == len(mesh.vertices):
            layer = mesh.uv_layers.new()
            for loop in mesh.loops:
                layer.data[loop.index].uv = uv[loop.vertex_index][:2]
        meshes[ident] = mesh
    objects = []
    for spec in asset['nodes']:
        obj = bpy.data.objects.new(spec['name'], meshes.get(spec['mesh']))
        collection.objects.link(obj)
        obj['ruster_kind'] = 'VISUAL'
        obj['ruster_source_uid'] = root['ruster_uid']
        obj.hide_select = True
        if obj.type=='EMPTY':
            obj.empty_display_type='PLAIN_AXES';obj.empty_display_size=.0001
        obj.parent = None if spec['parent'] is None else objects[spec['parent']]
        # Ignore the source prefab root transform: map placement owns it.
        if spec['parent'] is not None:
            obj.location = to_blender(spec['position'])
            obj.rotation_mode = 'QUATERNION'
            obj.rotation_quaternion = (BASIS @ Quaternion(spec['rotation']).to_matrix() @ BASIS).to_quaternion()
            obj.scale = to_blender(spec['scale'])
        if obj.type == 'MESH':
            # Mesh data are shared; use per-object material slots.
            needed = len(spec['materials'])
            while len(obj.data.materials) < needed:
                obj.data.materials.append(material('Rust fallback '+asset_key, (.5,.5,.5,1)))
            for i, mat_id in enumerate(spec['materials']):
                obj.material_slots[i].link = 'OBJECT'
                obj.material_slots[i].material = materials.get(mat_id)
        objects.append(obj)
    name_prefab(root,asset['path'])
    collection['ruster_asset_name'] = display_name(asset['path'])
    if asset.get('preview'):collection['ruster_preview']=json.dumps(asset['preview'])
    root.instance_type = 'COLLECTION'
    from .protection import freeze
    # A disposable thumbnail scene can render the local collection directly.
    root.instance_collection = collection if transient else freeze(collection,root['ruster_uid'],asset_key,verified=True)
    model_attached(root)
    return True


def prefab_roots():
    return [o for o in bpy.context.scene.objects if o.get('ruster_kind') == 'PREFAB']


def matrix_has_shear(matrix):
    """Check normalized transform axes, independently of their scale/position.

    A Rust placement can have a very long, thin source scale. Comparing its
    decomposed matrix in world units mistakes float32 roundoff for shear.
    Degenerate and nonfinite axes are also not representable Rust transforms.
    """
    axes = []
    for column in range(3):
        values = tuple(float(matrix[row][column]) for row in range(3))
        length = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(length) or length < 1e-7:
            return True
        axes.append(tuple(value / length for value in values))
    return any(abs(sum(a * b for a, b in zip(axes[i], axes[j]))) > 1e-5
               for i, j in ((0, 1), (0, 2), (1, 2)))


def validate():
    bpy.context.view_layer.update()
    errors, notes = [], []
    scene = bpy.context.scene
    if scene.ruster.mode != 'MAP':
        return ['Open a Rust .map to export game data'], []
    source = Path(scene.ruster.source_map)
    if not source.is_file():
        errors.append('Source map is missing; reopen the .ruster project or source map')
    missing = 0
    checked_models={}
    for obj in prefab_roots():
        try:
            from .protection import model_error
            problem=model_error(obj,checked_models)
            if problem:errors.append(f'{obj.name}: {problem}')
            uid = int(obj['ruster_uid'])
            if not 0 <= uid <= 0xFFFFFFFF:
                raise ValueError()
            fields = wire.parse(base64.b64decode(obj['ruster_record'], validate=True))
            if prefab_id(fields) != uid:
                errors.append(f'{obj.name}: prefab ID differs from the source record')
            from .placement import parent_chain
            if any(parent.get('ruster_kind')!='GROUP' for parent in parent_chain(obj)):
                errors.append(f'{obj.name}: parent must be a Rustmatics prefab group')
            if obj.constraints or obj.modifiers or obj.animation_data:
                errors.append(f'{obj.name}: bake constraints/modifiers/animation before exporting')
            loc, rot, scale = obj.matrix_world.decompose()
            if matrix_has_shear(obj.matrix_world):
                errors.append(f'{obj.name}: group transforms introduce shear; use uniform group scale')
            if not all(math.isfinite(v) for v in (*loc, *rot, *scale)):
                errors.append(f'{obj.name}: invalid transform')
            if any(abs(v) < 1e-7 for v in scale):
                errors.append(f'{obj.name}: zero scale')
            missing += not bool(obj.get('ruster_visual'))
        except (KeyError, ValueError, TypeError):
            errors.append(f'{obj.name}: invalid prefab metadata')
    if missing:
        notes.append(f'{missing} prefab placements have no loaded visual; IDs and transforms still export')
    build = [o for o in scene.objects if not o.get('ruster_kind') and o.type in {'MESH','CURVE','FONT'}]
    if build:
        errors.append(f'{len(build)} custom objects cannot be embedded in a Rust .map; save them in .ruster instead')
    unpacked=[o for o in scene.objects if o.get('ruster_kind')=='VISUAL']
    if unpacked:
        errors.append(f'{len(unpacked)} unpacked prefab model parts cannot export; restore the original prefab models')
    from .editing import path_record
    if bpy.context.mode=='EDIT_CURVE':
        errors.append('Finish editing path nodes before exporting the map')
    for obj in scene.objects:
        if obj.get('ruster_kind')=='PATH':
            try:
                path_record(obj)
            except Exception as error:
                errors.append(str(error))
    notes.append('Unknown map fields and unedited layer data are preserved')
    return errors, notes


def export_map(path):
    errors, notes = validate()
    if errors:
        raise ValueError('; '.join(errors[:5]))
    from . import editing
    world = editing.terrain().export_world()
    prefabs = []
    for obj in prefab_roots():
        fields = wire.parse(base64.b64decode(obj['ruster_record']))
        position, rotation, scale = prefab_transform(fields)
        loc, rot, scl = obj.matrix_world.decompose()
        # Keep original record bytes for untouched transforms (including Euler turns).
        same = ((loc - to_blender(position)).length < 1e-5 and
                abs(rot.dot(unity_euler(rotation))) > 1 - 1e-7 and
                (scl - to_blender(scale)).length < 1e-6)
        if not same:
            fields = update_transform(fields, to_unity(loc), blender_euler(rot), to_unity(scl))
        prefabs.append(fields)
    world.replace_prefabs(prefabs)
    world.replace_paths([editing.path_record(obj) for obj in bpy.context.scene.objects if obj.get('ruster_kind')=='PATH'])
    if Path(path).resolve() == Path(bpy.context.scene.ruster.source_map).resolve():
        raise ValueError('Export to a new filename to preserve the source map')
    atomic_write(path, world.write())
    return notes
