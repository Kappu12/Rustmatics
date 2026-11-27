"""Rust-focused prefab composition and interchange tools."""
import base64
import json
import math
import struct
import time
import sqlite3
from contextlib import closing
from pathlib import Path
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Matrix, Vector

from . import app, scene, placement, thumbnails
from .assets import catalog
from .core import rustedit_terrain, wire
from .core.asset_categories import GROUPS
from .core.project import atomic_write
from .core.rust_map import prefab_id, prefab_transform, update_transform
from .core.rustedit_prefab import RustEditPrefab
from .ui_style import invoke_dialog

_classes, _keymaps = [], []
_menu_cache = {}
MENU_CACHE_LIMIT = len(GROUPS)+1
_menu_signature = None
_menu_builder = None
_category_menus = {}
_model_items = []
# These categories partition the catalog. The special HIDDEN and ALL_ASSETS
# filters overlap them and would duplicate entries in Blender's menu search.
ADD_GROUPS = tuple(group for group in GROUPS if group[0] not in {'HIDDEN','ALL_ASSETS'})

# Scalar settings verified against the installed SerializedIOEntity contract.
IO_SETTINGS = {
    'access_level': (5,'int',0), 'door_effect': (6,'int',0),
    'timer_length': (7,'float',0.), 'frequency': (8,'int',0),
    'unlimited_ammo': (9,'bool',False), 'peacekeeper': (10,'bool',False),
    'weapon': (11,'str',''), 'branch_amount': (12,'int',0),
    'counter_target': (13,'int',0), 'camera_identifier': (14,'str',''),
    'counter_passthrough': (15,'bool',False), 'floors': (16,'int',1),
    'phone_name': (17,'str',''),
}


def io_value(fields, name):
    number, kind, default = IO_SETTINGS[name]
    raw = wire.get(fields, number)
    if raw is None:return default
    if kind == 'str':return raw.decode('utf8')
    if kind == 'float':return struct.unpack('<f',raw)[0]
    if kind == 'bool':return bool(raw)
    return raw - (1 << 64) if raw >= (1 << 63) else raw


def io_source(root):
    if not root or root.get('ruster_kind') != 'PREFAB':return None
    group = next((obj for obj in placement.parent_chain(root) if obj.get('ruster_prefab_source')),None)
    if not group:return None
    text = bpy.data.texts.get(group['ruster_prefab_source'])
    if not text:return None
    source = RustEditPrefab.read(base64.b64decode(text.as_string(),validate=True))
    position = prefab_transform(wire.parse(base64.b64decode(root['ruster_record'])))[0]
    found = []
    for index, field in enumerate(source.fields):
        if field.number != 5 or field.wire != 2:continue
        entries = wire.parse(field.value)
        for entry_index, entry in enumerate(entries):
            if entry.number != 1 or entry.wire != 2:continue
            entity = wire.parse(entry.value)
            value = wire.get(entity,2)
            if value is not None and max(abs(a-b) for a,b in zip(wire.vector(value),position)) < .001:
                found.append((text,source,index,entries,entry_index,entity))
    if len(found)>1:raise ValueError('Multiple IO entities share this position')
    return found[0] if found else None


class RUSTER_OT_io_settings(bpy.types.Operator):
    bl_idname = 'ruster.io_settings'
    bl_label = 'Rust IO Settings'
    bl_options = {'REGISTER','UNDO'}
    access_level: IntProperty(name='Card Access Level',min=0,max=3)
    door_effect: IntProperty(name='Door Effect',min=-1,max=3)
    timer_length: FloatProperty(name='Timer / Access Duration',min=0,max=86400,unit='TIME')
    frequency: IntProperty(name='Radio Frequency',min=0,max=9999999)
    unlimited_ammo: BoolProperty(name='Unlimited Turret Ammo')
    peacekeeper: BoolProperty(name='Peacekeeper Turret')
    weapon: StringProperty(name='Turret Weapon Shortname',maxlen=256)
    branch_amount: IntProperty(name='Branch Amount',min=0)
    counter_target: IntProperty(name='Counter Target',min=0)
    camera_identifier: StringProperty(name='Camera Identifier',maxlen=256)
    counter_passthrough: BoolProperty(name='Counter Passthrough')
    floors: IntProperty(name='Elevator Floors',min=1,max=100,default=1)
    phone_name: StringProperty(name='Phone Name',maxlen=256)

    def invoke(self,context,event):
        try:
            found = io_source(context.active_object)
            if not found:raise ValueError('Select an imported prefab with existing RustEdit IO data')
            for name in IO_SETTINGS:setattr(self,name,io_value(found[-1],name))
            return invoke_dialog(self,context,event,width=460)
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

    def draw(self,context):
        for name in IO_SETTINGS:self.layout.prop(self,name)

    def execute(self,context):
        try:
            found = io_source(context.active_object)
            if not found:raise ValueError('The selected prefab has no imported IO settings')
            text,source,index,entries,entry_index,entity = found
            for name,(number,kind,_default) in IO_SETTINGS.items():
                value = getattr(self,name)
                if value == io_value(entity,name):continue
                raw = value.encode('utf8') if kind=='str' else (struct.pack('<f',value) if kind=='float' else int(value)&0xffffffffffffffff)
                entity = wire.replace(entity,number,2 if kind=='str' else (5 if kind=='float' else 0),raw)
            entries[entry_index] = wire.encode(1,2,wire.serialize(entity))
            source.fields[index] = wire.encode(5,2,wire.serialize(entries))
            text.clear();text.write(base64.b64encode(source.write()).decode('ascii'))
            return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}


def new_prefab():
    from . import quitting
    from . import preview_jobs
    preview_jobs.cancel()
    app._visual_queue.clear();app._visual_iterator=None;app._visual_uid=None
    quitting.reset()
    scene.clear_scene()
    state = bpy.context.scene.ruster
    state.mode, state.title = 'PREFAB', 'Untitled Prefab'
    state.project_path = state.source_map = ''
    state.terrain_state = None
    state.native_ground = False
    scene.new_collection('Prefabs')
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type=='VIEW_3D':
                shading=area.spaces.active.shading
                shading.type='SOLID';shading.color_type='TEXTURE';shading.light='STUDIO'
    from . import ui
    if not bpy.app.background:bpy.app.timers.register(ui.start_workspace, first_interval=.1)


def prefab_records(roots, origin):
    records = []
    bpy.context.view_layer.update()
    from .map_prefabs import validate_placements
    roots = list(roots)
    errors, _notes = validate_placements(roots)
    if errors:raise ValueError('; '.join(errors[:5]))
    if len(origin) != 3 or not all(math.isfinite(v) for v in origin):
        raise ValueError('Export origin must be a finite XYZ position')
    for root in roots:
        old = wire.parse(base64.b64decode(root['ruster_record'], validate=True))
        position, rotation, scale = prefab_transform(old)
        relative = root.matrix_world.copy();relative.translation -= origin
        baseline = root.get('ruster_prefab_matrix')
        original=(Matrix([baseline[i*4:i*4+4] for i in range(4)]) if baseline else
                  Matrix.LocRotScale(scene.to_blender(position),scene.unity_euler(rotation),scene.to_blender(scale)))
        same_matrix=all(math.isclose(relative[i][j],original[i][j],rel_tol=3e-7,abs_tol=5e-6)
                        for i in range(4) for j in range(4))
        if same_matrix:
            # Original Rust IO tubes can be hundreds of metres long and very
            # thin. Preserve their exact records before float32 decomposition.
            records.append(old);continue
        location, quaternion, actual_scale = root.matrix_world.decompose()
        if not all(math.isfinite(v) for v in (*location, *quaternion, *actual_scale)):
            raise ValueError('Prefab has an invalid transform')
        if scene.matrix_has_shear(root.matrix_world):
            raise ValueError('Group scaling introduces shear; use uniform group scale')
        location -= origin
        unchanged = ((location - scene.to_blender(position)).length < 1e-5
                     and abs(quaternion.dot(scene.unity_euler(rotation))) > 1 - 1e-7
                     and (actual_scale - scene.to_blender(scale)).length < 1e-6)
        records.append(old if unchanged else update_transform(old, scene.to_unity(location),
                       scene.blender_euler(quaternion), scene.to_unity(actual_scale)))
    return records


def import_prefab(path):
    path = Path(path)
    data = RustEditPrefab.load(path)  # Validate everything before creating objects.
    companions = rustedit_terrain.read_companions(path)
    cursor = bpy.context.scene.cursor.location.copy()
    collection = bpy.data.collections.get('Prefabs') or scene.new_collection('Prefabs')
    io_positions = set()
    linked_extensions = bool(data.populated_extensions)
    for field in data.fields:
        if field.number == 5 and field.wire == 2:
            for entry in wire.parse(field.value):
                if entry.number == 1 and entry.wire == 2:
                    position = wire.get(wire.parse(entry.value),2)
                    if position is not None:io_positions.add(tuple(round(v,3) for v in wire.vector(position)))
    created = []
    root = None
    try:
        for index, fields in enumerate(data.prefabs):
            obj = scene.root_object(fields, collection)
            obj.location += cursor
            obj['ruster_prefab_index'] = index
            obj['ruster_has_io'] = tuple(round(v,3) for v in prefab_transform(fields)[0]) in io_positions
            obj['ruster_linked_extensions'] = linked_extensions
            created.append(obj)
        root = placement.group(created, path.stem)
        # RustEdit's pivot is the file's origin, not the mean placement position.
        matrices = [(obj, obj.matrix_world.copy()) for obj in created]
        root.location = cursor
        bpy.context.view_layer.update()
        for obj, matrix in matrices:obj.matrix_world = matrix
        bpy.context.view_layer.update()
        for obj in created:
            matrix = obj.matrix_world.copy();matrix.translation -= cursor
            obj['ruster_prefab_matrix'] = [matrix[i][j] for i in range(4) for j in range(4)]
        text = bpy.data.texts.new('RustEdit source: ' + path.name)
        text.write(base64.b64encode(data.original).decode('ascii'))
        root['ruster_prefab_source'] = text.name
        if companions:
            images = bpy.data.texts.new('RustEdit companion files: ' + path.name)
            images.write(json.dumps({suffix:base64.b64encode(value).decode('ascii')
                                     for suffix,value in companions.items()}))
            root['ruster_prefab_companions'] = images.name
        app.request_missing_visuals(created)
        return root
    except Exception:
        for obj in created:
            if obj.name in bpy.data.objects:bpy.data.objects.remove(obj, do_unlink=True)
        if root and root.name in bpy.data.objects:bpy.data.objects.remove(root, do_unlink=True)
        raise


def _terrain_width(roots, origin, fade, *, circular=True):
    from . import mcp_building
    children=mcp_building.hierarchy()
    bounds={}
    radius=1.
    for obj in roots:
        box=mcp_building.object_bounds(obj,children,bounds)
        if box:
            x,y=(max(abs(box[edge][axis]-origin[axis]) for edge in ('min','max'))
                 for axis in (0,1))
        else:
            x,y=(obj.matrix_world.translation-origin)[:2]
        reach = math.hypot(x, y) if circular else max(abs(x), abs(y))
        radius = max(radius, reach)
    return math.ceil(2*(radius+fade))


def _export_monument(path, records, roots, origin, source, companions, *,
                     include_terrain, terrain_width, terrain_fade, overwrite):
    from . import editing
    from .core import rustedit_monument

    if source and source.populated_extensions:
        raise ValueError('RustEdit 2 export cannot translate linked ' + ', '.join(source.populated_extensions))
    state = bpy.context.scene.ruster
    native = state.mode == 'MAP' and state.native_ground
    legacy = bool(set(rustedit_terrain.TERRAIN_SUFFIXES) & companions.keys())
    if include_terrain is None:
        include_terrain = native or legacy
    if type(include_terrain) is not bool:
        raise ValueError('Include terrain must be a boolean')

    size = _terrain_width(roots, origin, 0, circular=False)
    stamp, masks = None, {}
    if include_terrain:
        if type(terrain_width) is not int or terrain_width < 0:
            raise ValueError('Terrain width must be a positive whole number or zero for automatic sizing')
        if type(terrain_fade) is not int or terrain_fade < 1:
            raise ValueError('Terrain edge fade must be a positive whole number')
        if native:
            width = terrain_width or size + 2 * terrain_fade
            size = rustedit_monument.canvas_size(max(size, width))
            stamp = rustedit_monument.capture(editing.terrain(), origin, size, width, terrain_fade)
        elif legacy:
            options = wire.parse(wire.get(source.fields, 1, b''))
            width, fade = wire.get(options, 1, 0), wire.get(options, 2, 0)
            if terrain_width:
                raise ValueError('Imported terrain has a fixed width. Add native ground to choose a new capture area')
            if width < 2:
                raise ValueError('The legacy prefab is missing its terrain width')
            size = rustedit_monument.canvas_size(max(size, width))
            document, layers = rustedit_terrain.decode_companions(companions, width)
            stamp = rustedit_monument.capture(document, (0, 0, 0), size, width, fade,
                                             circular=True, enabled=layers)
            masks = {name: wire.get(options, i, mask) & mask
                     for i, (name, mask) in enumerate(rustedit_monument.LAYER_MASKS.items(), 3)}
        else:
            raise ValueError('Open a project with native ground or a RustEdit prefab with terrain companions')
    size = rustedit_monument.canvas_size(size)

    paths = {}
    cache = scene.cache_dir()
    if (cache / 'catalog.sqlite3').is_file():
        with closing(catalog.read_connection(cache)) as database:
            for uid in set(map(prefab_id, records)):
                row = database.execute('SELECT path FROM names WHERE uid=?', (uid,)).fetchone()
                if row:
                    paths[uid] = row[0]
    atomic_write(path, rustedit_monument.encode(records, size, stamp=stamp, masks=masks, prefab_paths=paths),
                 overwrite=overwrite)
    return [path]


def export_prefab(path, selected=True, *, include_terrain=None, terrain_width=0,
                  terrain_fade=5, preview_png=None, overwrite=True):
    # Properties/file-browser contexts do not always expose selected_objects.
    # Read actual view-layer flags so empty-selection fallback is unambiguous.
    layer=bpy.context.view_layer
    objects=[obj for obj in layer.objects if obj.select_get(view_layer=layer)] if selected else []
    roots=placement.selected_prefabs(objects) if objects else scene.prefab_roots()
    if not roots:raise ValueError('The composition has no prefab placements to export' if not objects else 'Select prefab placements or a prefab group')
    path = Path(path)
    is_monument = path.suffix.lower() == '.asset'
    active = layer.objects.active if selected or not is_monument else None
    groups = {next((parent for parent in placement.parent_chain(obj)
                    if parent.get('ruster_prefab_source')),None) for obj in roots}
    if len(groups)>1:
        raise ValueError('Export imported custom-prefab groups separately to preserve their metadata')
    source_group = next(iter(groups))
    if not source_group and any(obj.get('ruster_linked_extensions') for obj in roots):
        raise ValueError('The imported prefab group is missing; restore it before exporting linked metadata')
    source = None
    companions = {}
    if source_group:
        text = bpy.data.texts.get(source_group.get('ruster_prefab_source', ''))
        if not text:raise ValueError('The imported prefab metadata is missing')
        source = RustEditPrefab.read(base64.b64decode(text.as_string(), validate=True))
        if key := source_group.get('ruster_prefab_companions'):
            images = bpy.data.texts.get(key)
            if not images:raise ValueError('The imported RustEdit companion files are missing')
            companions = {suffix:base64.b64decode(value, validate=True)
                          for suffix,value in json.loads(images.as_string()).items()}
        roots.sort(key=lambda o: int(o.get('ruster_prefab_index', -1)))
        origin = source_group.matrix_world.translation.copy()
    else:
        origin = (active.matrix_world.translation.copy() if active and active.get('ruster_kind') == 'GROUP'
                  else bpy.context.scene.cursor.location.copy())
    records = prefab_records(roots, origin)
    if is_monument:
        if preview_png:
            raise ValueError('External preview images are only supported for .prefab exports')
        return _export_monument(path, records, roots, origin, source, companions,
                                include_terrain=include_terrain, terrain_width=terrain_width,
                                terrain_fade=terrain_fade, overwrite=overwrite)
    if source:source.replace_prefabs(records)
    else:source = RustEditPrefab.create(records)
    state = bpy.context.scene.ruster
    has_ground = state.mode=='MAP' and state.native_ground
    if include_terrain is None:include_terrain=has_ground
    if type(include_terrain) is not bool:raise ValueError('Include map terrain must be a boolean')
    if include_terrain:
        if not has_ground:raise ValueError('Open or create a map with native ground to export terrain masks')
        if type(terrain_width) is not int or terrain_width<0:
            raise ValueError('Terrain width must be a positive whole number or zero for automatic sizing')
        if type(terrain_fade) is not int or terrain_fade<1:
            raise ValueError('Terrain edge fade must be a positive whole number')
        from . import editing
        document=editing.terrain()
        width=terrain_width or _terrain_width(roots,origin,terrain_fade)
        source.set_terrain_options(width,terrain_fade)
        companions.update(rustedit_terrain.capture(document,origin,width))
    if preview_png:
        companions['.png']=rustedit_terrain.read_png(preview_png)
    if path.suffix.lower() != '.prefab':
        path = path.with_suffix('.prefab')
    return rustedit_terrain.write_bundle(path, source.write(), companions, overwrite=overwrite)


def menu_signature():
    from .core import palettes, settings
    path=scene.cache_dir()/'catalog.sqlite3'
    stat=path.stat() if path.is_file() else None
    favorites=frozenset(palettes.favorite_ids(settings.load()))
    return str(path),((stat.st_mtime_ns,stat.st_size,stat.st_ino) if stat else None),favorites


def advance_menu_cache(budget=None):
    """One cache generation, shared by every category; no eviction while searching."""
    global _menu_signature,_menu_builder
    signature=menu_signature()
    if signature!=_menu_signature or (not _menu_cache and _menu_builder is None):
        if _menu_builder is not None:_menu_builder.close()
        _menu_cache.clear();_menu_builder=None;_menu_signature=signature
        if signature[1] is not None:
            from .assets import menu_catalog
            _menu_builder=menu_catalog.build(scene.cache_dir(),signature[2])
    deadline=time.perf_counter()+budget if budget is not None else None
    try:
        while _menu_builder is not None:
            try:next(_menu_builder)
            except StopIteration as complete:
                _menu_cache.update(complete.value);_menu_builder=None
                break
            if deadline is not None and time.perf_counter()>=deadline:break
    except Exception:
        if _menu_builder is not None:_menu_builder.close()
        _menu_builder=None;_menu_signature=None
        raise
    return _menu_builder is None


def warm_prefab_menu():
    # Four-millisecond slices keep name preparation out of the typing path.
    # The main thread owns the iterator; no bpy calls run in background threads.
    try:complete=advance_menu_cache(.004)
    except (OSError,ValueError,sqlite3.Error):return 1.
    return 1. if complete else .02


def prefab_menu_data(context, category=''):
    """Reuse the complete snapshot for menu traversal and native search."""
    advance_menu_cache()
    if not _menu_cache:return {'items':[],'total':0}
    if category in _menu_cache:return _menu_cache[category]
    if category.startswith('@'):raise ValueError('Unknown Rust prefab category')
    # Preserve explicit path-prefix callers without adding duplicate snapshots.
    prefix=category.rstrip('/').casefold()+'/'
    items=[item for item in _menu_cache['']['items'] if item[2].casefold().startswith(prefix)]
    return {'items':items,'total':len(items)}


def search_items(operator, context):
    """Compatibility metadata adapter for callers inspecting the Add catalog."""
    data=prefab_menu_data(context,getattr(operator,'category',''))
    if 'enum_items' not in data:
        data['enum_items']=[(uid,name,path+'\nID: '+uid) for uid,name,path,_weight,_icon in data['items']]
    return data['enum_items'] or [('NONE','No local catalog — select a Rust installation','')]


def draw_prefab_menu(menu, context, category=''):
    layout=menu.layout
    if context.mode!='OBJECT':
        layout.label(text='Finish editing before adding prefabs')
        return
    layout.operator_context='EXEC_DEFAULT'
    data=prefab_menu_data(context,category)
    if not data['items']:
        layout.label(text='Select a Rust installation in Assets preferences',icon='INFO')
        return
    for uid,name,source,weight,icon in data['items']:
        preview = thumbnails.icon_id(uid, source)
        layout.operator('ruster.place', text=name, icon='NONE' if preview else icon,
                        icon_value=preview, search_weight=weight).uid = uid


class RUSTER_OT_add_prefab(bpy.types.Operator):
    bl_idname = 'ruster.add_prefab'
    bl_label = 'Add Rust Prefab'
    category: StringProperty(default='',options={'SKIP_SAVE'})

    def invoke(self, context, event):
        menu=_category_menus.get(self.category) if self.category else RUSTER_MT_add
        if menu is None:
            self.report({'ERROR'},'Unknown Rust prefab category')
            return {'CANCELLED'}
        return bpy.ops.wm.call_menu(name=menu.__name__)

    def execute(self, context):
        return self.invoke(context,None)


class RUSTER_MT_add(bpy.types.Menu):
    bl_label = 'Add'
    bl_options = {'SEARCH_ON_KEY_PRESS'}

    def draw(self, context):
        from .ui_style import action
        layout=self.layout
        if context.mode!='OBJECT':
            layout.label(text='Finish editing before adding prefabs')
            return
        action(layout,'wm.search_single_menu').menu_idname='RUSTER_MT_add'
        layout.separator()
        # Native menu search traverses this complete, nonoverlapping partition.
        for key,label,_patterns in ADD_GROUPS:
            layout.menu(_category_menus['@'+key].__name__,text=label)


def category_menu(key, label):
    def draw(self, context):
        draw_prefab_menu(self,context,'@'+key)
    return type('RUSTER_MT_add_'+key,(bpy.types.Menu,),
                {'__module__':__name__,'bl_label':label,
                 'bl_options':{'SEARCH_ON_KEY_PRESS'},'draw':draw})


class RUSTER_OT_prefab_import(bpy.types.Operator, ImportHelper):
    bl_idname = 'ruster.prefab_import'
    bl_label = 'Import RustEdit Prefab'
    bl_options = {'REGISTER', 'UNDO'}
    filter_glob: StringProperty(default='*.prefab', options={'HIDDEN'})
    def execute(self, context):
        try:import_prefab(self.filepath);return {'FINISHED'}
        except Exception as error:self.report({'ERROR'}, str(error));return {'CANCELLED'}


class RUSTER_OT_prefab_export(bpy.types.Operator, ExportHelper):
    bl_idname = 'ruster.prefab_export'
    bl_label = 'Export RustEdit Prefab'
    bl_description = 'Export prefab placements and the companion terrain masks from a map with native ground'
    filename_ext = '.prefab'
    filter_glob: StringProperty(default='*.prefab', options={'HIDDEN'})
    selected_only: BoolProperty(name='Use Selection When Available', description='Export highlighted prefabs/groups; export everything when nothing is highlighted', default=True)
    include_terrain: BoolProperty(name='Include Map Terrain', description='Save heights, splat, biome, alpha and topology beside the prefab', default=True)
    terrain_width: IntProperty(name='Terrain Width (m)', description='Width of the terrain stamp around the prefab pivot; zero fits the placements', default=0, min=0, max=32768)
    terrain_fade: IntProperty(name='Terrain Edge Fade (m)', description='Blend distance inside the terrain stamp edge', default=5, min=1, max=16383)
    preview_png: StringProperty(name='Preview PNG', description='Optional PNG thumbnail for the RustEdit prefab browser', subtype='FILE_PATH')

    def draw(self, context):
        self.layout.prop(self, 'selected_only')
        if context.scene.ruster.mode=='MAP' and context.scene.ruster.native_ground:
            self.layout.prop(self, 'include_terrain')
            if self.include_terrain:
                self.layout.prop(self, 'terrain_width')
                self.layout.prop(self, 'terrain_fade')
        self.layout.prop(self, 'preview_png')

    def execute(self, context):
        try:
            state=context.scene.ruster
            files=export_prefab(
                self.filepath, self.selected_only,
                include_terrain=self.include_terrain and state.mode=='MAP' and state.native_ground,
                terrain_width=self.terrain_width, terrain_fade=self.terrain_fade,
                preview_png=bpy.path.abspath(self.preview_png) if self.preview_png else None,
                overwrite=Path(self.filepath).with_suffix('.prefab').is_file(),
            )
            if len(files)>1:self.report({'INFO'}, f'Exported prefab and {len(files)-1} companion files')
            return {'FINISHED'}
        except Exception as error:self.report({'ERROR'}, str(error));return {'CANCELLED'}


class RUSTER_OT_monument_export(bpy.types.Operator, ExportHelper):
    bl_idname = 'ruster.monument_export'
    bl_label = 'Export RustEdit 2 Monument'
    bl_description = 'Export prefab placements and embedded terrain as a RustEdit 2 monument'
    filename_ext = '.asset'
    filter_glob: StringProperty(default='*.asset', options={'HIDDEN'})
    selected_only: BoolProperty(name='Use Selection When Available', default=True)
    include_terrain: BoolProperty(name='Include Terrain', default=True)
    terrain_width: IntProperty(name='Terrain Width (m)', description='Width of the terrain stamp; zero fits the placements', default=0, min=0, max=2048)
    terrain_fade: IntProperty(name='Terrain Edge Fade (m)', default=5, min=1, max=1023)

    def draw(self, context):
        self.layout.prop(self, 'selected_only')
        self.layout.prop(self, 'include_terrain')
        if self.include_terrain and context.scene.ruster.native_ground:
            self.layout.prop(self, 'terrain_width')
            self.layout.prop(self, 'terrain_fade')

    def execute(self, context):
        try:
            path = Path(self.filepath).with_suffix('.asset')
            export_prefab(path, self.selected_only, include_terrain=None if self.include_terrain else False,
                          terrain_width=self.terrain_width, terrain_fade=self.terrain_fade, overwrite=path.is_file())
            self.report({'INFO'}, 'Exported ' + path.name)
            return {'FINISHED'}
        except (OSError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


def model_formats(_operator, _context):
    global _model_items
    from .model_export import available_formats
    if not _model_items:_model_items = available_formats()
    return _model_items


class RUSTER_OT_model_export(bpy.types.Operator, ExportHelper):
    bl_idname = 'ruster.model_export'
    bl_label = 'Export Models'
    filename_ext = ''
    format: EnumProperty(name='File Format', items=model_formats)
    selected_only: BoolProperty(name='Selected Prefabs Only', default=True)
    def execute(self, context):
        try:
            from .model_export import export_models
            roots = placement.selected_prefabs() if self.selected_only else scene.prefab_roots()
            result = export_models(self.filepath, self.format, roots)
            self.report({'INFO'}, 'Exported ' + result.name)
            return {'FINISHED'}
        except Exception as error:self.report({'ERROR'}, str(error));return {'CANCELLED'}


class RUSTER_OT_blend_export(bpy.types.Operator, ExportHelper):
    bl_idname = 'ruster.blend_export'
    bl_label = 'Export Blender File'
    bl_description = 'Export a standalone Blender scene with editable models and packed textures for rendering in Blender'
    filename_ext = '.blend'
    filter_glob: StringProperty(default='*.blend', options={'HIDDEN'})
    selected_only: BoolProperty(name='Selected Prefabs Only', default=False)
    def execute(self, context):
        try:
            from .model_export import export_models
            roots = placement.selected_prefabs() if self.selected_only else scene.prefab_roots()
            result = export_models(self.filepath, 'BLEND', roots)
            self.report({'INFO'}, 'Exported ' + result.name)
            return {'FINISHED'}
        except Exception as error:self.report({'ERROR'}, str(error));return {'CANCELLED'}


class RUSTER_OT_edit_mode(bpy.types.Operator):
    bl_idname = 'ruster.edit_mode'
    bl_label = 'Edit Path Nodes'
    def execute(self, context):
        obj = context.active_object
        if obj and obj.get('ruster_kind') == 'PATH':
            return bpy.ops.object.mode_set(mode='OBJECT' if context.mode == 'EDIT_CURVE' else 'EDIT')
        self.report({'INFO'}, 'Rust prefabs are edited as whole placements')
        return {'CANCELLED'}


def register():
    thumbnails.register()
    _category_menus.update(('@'+key,category_menu(key,label)) for key,label,_patterns in GROUPS)
    classes = [RUSTER_OT_io_settings, RUSTER_OT_add_prefab, RUSTER_MT_add, *_category_menus.values(), RUSTER_OT_prefab_import,
               RUSTER_OT_prefab_export, RUSTER_OT_monument_export, RUSTER_OT_model_export,
               RUSTER_OT_blend_export, RUSTER_OT_edit_mode]
    for cls in classes:bpy.utils.register_class(cls);_classes.append(cls)
    if not bpy.app.background:bpy.app.timers.register(warm_prefab_menu,first_interval=.1,persistent=True)
    config = bpy.context.window_manager.keyconfigs.addon
    if config:
        view_keymap = config.keymaps.new(name='3D View', space_type='VIEW_3D')
        walk = view_keymap.keymap_items.new('view3d.walk', 'F', 'PRESS', shift=True)
        _keymaps.append((view_keymap, walk))
        add = view_keymap.keymap_items.new('wm.call_menu', 'A', 'PRESS', shift=True)
        add.properties.name = 'RUSTER_MT_add'
        _keymaps.append((view_keymap, add))
        keymap = config.keymaps.new(name='Object Mode', space_type='EMPTY')
        item = keymap.keymap_items.new('wm.call_menu', 'A', 'PRESS', shift=True)
        item.properties.name = 'RUSTER_MT_add';_keymaps.append((keymap, item))
        item = keymap.keymap_items.new('ruster.edit_mode', 'TAB', 'PRESS')
        _keymaps.append((keymap, item))
        for shift, alt in ((True, False), (False, True)):
            item = keymap.keymap_items.new('ruster.duplicate_move', 'D', 'PRESS', shift=shift, alt=alt)
            _keymaps.append((keymap,item))
        for key, confirm in (('X', True), ('DEL', False)):
            item = keymap.keymap_items.new('ruster.delete', key, 'PRESS')
            item.properties.confirm = confirm;_keymaps.append((keymap,item))
        keymap = config.keymaps.new(name='Curve',space_type='EMPTY')
        item = keymap.keymap_items.new('wm.call_menu','A','PRESS',shift=True)
        item.properties.name = 'RUSTER_MT_add';_keymaps.append((keymap,item))


def unregister():
    global _menu_builder,_menu_signature
    thumbnails.unregister()
    if bpy.app.timers.is_registered(warm_prefab_menu):bpy.app.timers.unregister(warm_prefab_menu)
    if _menu_builder is not None:_menu_builder.close()
    _menu_builder=None;_menu_signature=None
    _menu_cache.clear()
    for keymap, item in _keymaps:keymap.keymap_items.remove(item)
    _keymaps.clear()
    for cls in reversed(_classes):bpy.utils.unregister_class(cls)
    _classes.clear()
    _category_menus.clear()
