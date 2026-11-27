"""Application lifecycle, native Blender operators, and background asset jobs."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty, IntProperty, EnumProperty, PointerProperty, BoolProperty, FloatProperty, FloatVectorProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper

from . import scene
from .core import project, settings, wire
from .assets import catalog
from .ui_style import ICONS as I,action,properties,wrapped,invoke_dialog

_classes = []
_keymaps = []
_job = None
_job_log = None
_job_action = None
_visual_queue = []
_visual_iterator = None
_visual_uid = None
_visual_scene = None
VISUALS_PER_TICK = 32
VISUAL_TIME_BUDGET = .008
_session_dirs = []
_pending_project = None
_job_cancelled = False
_pending_asset_ids=set()
_job_ids=set()


def category_items(state,context):
    from .browser import category_items as items
    return items(state,context)


def query_changed(state,context):
    state.query_page=0


class RUSTER_PG_project(bpy.types.PropertyGroup):
    mode: EnumProperty(items=[('BLENDER', 'Legacy project', ''), ('MAP', 'Rust map', ''),
                             ('PREFAB', 'Rust prefab', '')], default='PREFAB')
    title: StringProperty(default='Untitled')
    project_path: StringProperty(subtype='FILE_PATH')
    source_map: StringProperty(subtype='FILE_PATH')
    map_size: IntProperty(default=4000)
    query: StringProperty(name='Search prefabs', options={'SKIP_SAVE'},update=query_changed)
    asset_status: StringProperty(default='Choose your Rust installation', options={'SKIP_SAVE'})
    busy: BoolProperty(default=False, options={'SKIP_SAVE'})
    asset_progress: FloatProperty(default=0,min=0,max=1,subtype='FACTOR',options={'SKIP_SAVE'})
    asset_progress_known: BoolProperty(default=False,options={'SKIP_SAVE'})
    terrain_state: PointerProperty(type=bpy.types.Text)
    native_ground: BoolProperty(default=False)
    ground_size: FloatProperty(default=512,min=16,max=2000)
    category:EnumProperty(name='Category',items=category_items,update=query_changed)
    favorites_only:BoolProperty(name='Favorites Only',default=False,update=query_changed)
    recent_only:BoolProperty(name='Recently Used',default=False,update=query_changed)
    available_only:BoolProperty(name='Available Locally',description='Show prefabs found in your local Rust installation',default=True,update=query_changed)
    include_helpers:BoolProperty(name='Show Hidden Assets',description='Show all catalog entries, including internal effects, IO handles, sounds, and test prefabs',default=False,update=query_changed)
    query_page: IntProperty(default=0,min=0,options={'SKIP_SAVE'})
    preview_resolution:EnumProperty(name='Terrain Detail',items=[('129','Fast','Lowest viewport memory use'),
        ('257','Balanced','Balanced detail and performance'),('513','Detailed','More terrain samples in the viewport'),
        ('1025','Very Detailed','Highest viewport memory use')],default='257')


def session_dir():
    # Keep directories alive for the session, especially .ruster's embedded source.map.
    directory = tempfile.TemporaryDirectory(prefix='ruster-')
    _session_dirs.append(directory)
    return Path(directory.name)


def worker_python():
    if path := os.environ.get('RUSTER_WORKER_PYTHON'):
        return path
    base = Path(bpy.app.binary_path).parent
    candidates = list(base.glob('*/python/bin/python*'))
    if sys.platform == 'win32':
        candidates += list(base.glob('*/python/bin/python.exe'))
    if sys.platform == 'darwin':
        candidates += list(base.glob('../Resources/*/python/bin/python*'))
    candidates = [p for p in candidates if p.is_file() and '-config' not in p.name]
    if not candidates:
        raise ValueError('Bundled Python not found; set RUSTER_WORKER_PYTHON for development')
    return str(sorted(candidates)[0].resolve())


def updated_version():
    """External app files may change while this editor still runs old modules."""
    import re
    from . import __version__
    try:
        match=re.search(r'__version__\s*=\s*[\'"]([^\'"]+)',Path(__file__).with_name('__init__.py').read_text())
    except OSError:return None
    return match[1] if match and match[1]!=__version__ else None


def start_job(action, ids=None):
    global _job, _job_log, _job_action, _job_cancelled, _job_ids
    if _job is not None:
        raise ValueError('An asset operation is already running')
    if version:=updated_version():
        raise ValueError(f'Rustmatics {version} was installed while this editor was open. Save your project and restart Rustmatics before loading models.')
    config = settings.load()
    root = settings.validate_install(config.get('rust_install', ''))
    cache = scene.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    args = [worker_python(), str(Path(__file__).parent / 'assets/worker.py'), action,
            '--install', str(root), '--cache', str(cache), '--parent-pid', str(os.getpid())]
    if config.get('rustedit_install'):args+=['--rustedit',config['rustedit_install']]
    if ids is not None:
        ids_path = session_dir() / 'ids.json'
        ids_path.write_text(json.dumps(ids))
        args += ['--ids-file', str(ids_path)]
    (cache / 'progress.json').write_text(json.dumps({'state': 'STARTING'}))
    job_log = (cache / 'worker.log').open('w')
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:job = subprocess.Popen(args, stdout=job_log, stderr=subprocess.STDOUT, **kwargs)
    except Exception:
        job_log.close();raise
    if _job_log:_job_log.close()
    _job_log,_job=job_log,job
    _job_action = action
    _job_ids=set(ids or [])
    _job_cancelled = False
    bpy.context.scene.ruster.busy = True
    bpy.context.scene.ruster.asset_status='Loading prefab models…' if action=='extract' else 'Refreshing prefab names…'
    bpy.context.scene.ruster.asset_progress = 0
    bpy.context.scene.ruster.asset_progress_known = False
    if not bpy.app.timers.is_registered(poll_job):
        bpy.app.timers.register(poll_job, first_interval=.2,persistent=True)


def request_assets(ids):
    ids=set(ids)
    if not ids:return
    if len(ids|_pending_asset_ids)>100:
        bpy.context.scene.ruster.asset_status='Load at most 100 different prefab models per request'
        return False
    # A completed process still owns its IDs until poll_job consumes the result.
    # Replacing it between timer ticks used to strand the previous placements.
    active=_job_ids if _job is not None and _job_action=='extract' and not _job_cancelled else set()
    _pending_asset_ids.update(ids-active)
    for obj in scene.prefab_roots():
        if int(obj['ruster_uid']) in ids:
            obj['ruster_model_status']='Loading model and materials…'
            if 'ruster_model_error' in obj:del obj['ruster_model_error']
            if 'ruster_model_error_kind' in obj:del obj['ruster_model_error_kind']
    if _job is not None:
        if not bpy.app.timers.is_registered(poll_job):
            bpy.app.timers.register(poll_job,first_interval=.2,persistent=True)
        return True
    pending=sorted(_pending_asset_ids);_pending_asset_ids.clear()
    try:
        start_job('extract',pending)
        return True
    except Exception as error:
        bpy.context.scene.ruster.asset_status=f'Could not start asset loading: {error}. Check Rust Installation.'
        for obj in scene.prefab_roots():
            if int(obj['ruster_uid']) in pending:
                obj['ruster_model_error']=str(error)
                obj['ruster_model_status']='Model loading failed'
        return False


def redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def poll_job():
    global _job, _job_log, _visual_queue, _job_action, _job_ids
    if bpy.context.window_manager.is_interface_locked:return .5
    state = bpy.context.scene.ruster
    progress = {}
    try:
        progress = json.loads((scene.cache_dir() / 'progress.json').read_text())
        phase = progress.get('state', 'STARTING')
        total=progress.get('total',0)
        current=progress.get('current',0)
        state.asset_progress_known=isinstance(total,(int,float)) and total>0 and isinstance(current,(int,float))
        state.asset_progress=max(0,min(1,current/total)) if state.asset_progress_known else 0
        if phase == 'ERROR':
            state.asset_status = progress.get('error', 'Asset operation failed')
        elif phase == 'READY':
            if _job_action=='index':state.asset_status=f"{progress.get('ids',0):,} prefab names ready · models load on demand"
            else:state.asset_status = f"{progress.get('extracted', 0)} prefabs ready · {progress.get('failures', 0)} reported issues"
        elif phase=='EXTRACTING' and progress.get('item'):
            from .core.asset_names import display_name
            state.asset_status='Loading '+display_name(progress['item'])+'…'
        elif phase=='CATALOG':state.asset_status='Refreshing prefab names…'
    except (OSError, ValueError):
        pass
    if _job and _job.poll() is None:
        redraw()
        return .5
    code = _job.returncode if _job else 0
    _job = None
    if _job_log:
        _job_log.close()
        _job_log = None
    state.busy = bool(_visual_queue)
    if _job_cancelled:
        state.asset_status='Asset import cancelled. Completed cache entries are available.'
    elif code and progress.get('state') != 'ERROR':
        state.asset_status = 'Asset import failed — see the asset report / worker.log'
    action, _job_action = _job_action, None
    completed_ids,_job_ids=_job_ids,set()
    if action=='extract':
        try:
            issues=json.loads((scene.cache_dir()/'asset-report.json').read_text())
            errors={int(item['uid']):item['error'] for item in issues}
        except (OSError,ValueError,KeyError,TypeError):errors={}
        for obj in scene.prefab_roots():
            uid=int(obj['ruster_uid'])
            if uid not in completed_ids:continue
            obj['ruster_model_status']='Model loading cancelled' if _job_cancelled else 'Model unavailable'
            if uid in errors:
                obj['ruster_model_error']=errors[uid]
                if obj.get('ruster_model_error_kind') in {'PREVIEW','PREVIEW_UPGRADE'}:del obj['ruster_model_error_kind']
            elif code:obj['ruster_model_error']=state.asset_status
    if action=='extract' and not _job_cancelled:
        from . import thumbnails
        for uid in completed_ids:
            thumbnails.converted(uid)
        waiting=set(_visual_queue)
        _visual_queue.extend(sorted({int(obj['ruster_uid']) for obj in scene.prefab_roots()} & completed_ids-waiting))
        state.busy=bool(_visual_queue)
        if _visual_queue:
            state.asset_status='Linking original models into the scene…'
            state.asset_progress_known=False
        if _visual_queue and not bpy.app.timers.is_registered(load_visuals):
            bpy.app.timers.register(load_visuals,first_interval=.1,persistent=True)
    # Cancel clears the queue. Requests added afterwards belong to a retry,
    # even while the canceled process still awaits this completion tick.
    if _pending_asset_ids:
        pending=sorted(_pending_asset_ids);_pending_asset_ids.clear()
        try:
            start_job('extract',pending);return .5
        except Exception as error:
            state.asset_status=f'Could not start asset loading: {error}'
            for obj in scene.prefab_roots():
                if int(obj['ruster_uid']) in pending:
                    obj['ruster_model_status']='Model loading failed'
                    obj['ruster_model_error']=str(error)
    redraw()
    return None


def load_visuals():
    # Bound repeated placements too: one UID can represent thousands of copies.
    # A single library link is indivisible, but no further copies block that tick.
    global _visual_iterator,_visual_uid,_visual_scene
    if bpy.context.window_manager.is_interface_locked:return .1
    if _visual_queue:
        uid=_visual_queue[0]
        from . import preview_jobs
        try:preparation,error=preview_jobs.prepare(uid)
        except Exception as failure:preparation,error='ERROR',str(failure)
        if preparation=='WAIT':
            state=bpy.context.scene.ruster
            state.busy=True;state.asset_progress_known=False
            state.asset_status='Preparing original model preview in the background…'
            redraw();return .1
        if preparation=='ERROR':
            preview_jobs.cancel()
            _visual_queue.pop(0);_visual_iterator=None;_visual_uid=None
            for obj in scene.prefab_roots():
                if int(obj['ruster_uid'])==uid:
                    obj['ruster_model_status']='Preview preparation failed'
                    obj['ruster_model_error']=error
                    obj['ruster_model_error_kind']='PREVIEW'
            bpy.context.scene.ruster.asset_status=error
            bpy.context.scene.ruster.busy=bool(_visual_queue or _pending_asset_ids or _job)
            redraw();return .05 if _visual_queue else None
        current_scene=bpy.context.scene.as_pointer()
        if _visual_iterator is None or _visual_uid!=uid or _visual_scene!=current_scene:
            _visual_iterator=iter([obj for obj in scene.prefab_roots() if int(obj['ruster_uid'])==uid])
            _visual_uid=uid;_visual_scene=current_scene
        deadline=time.perf_counter()+VISUAL_TIME_BUDGET
        for _index in range(VISUALS_PER_TICK):
            obj=next(_visual_iterator,None)
            if obj is None:
                _visual_queue.pop(0);_visual_iterator=None;_visual_uid=None
                break
            try:
                loaded=scene.attach_visual(obj,force=True,prepared=True)
                if loaded and not obj.get('ruster_marker'):
                    if not obj.get('ruster_spawner_preview'):obj['ruster_model_status']='Model loaded'
                    frame_first_model(obj)
                elif not loaded:
                    active_ids=_job_ids if _job is not None and _job_action=='extract' else set()
                    upgrading=obj.get('ruster_model_error_kind')=='PREVIEW_UPGRADE'
                    waiting=uid in active_ids|_pending_asset_ids
                    if upgrading and not obj.get('ruster_model_error') and not waiting:
                        waiting=bool(request_assets([uid]))
                    if not obj.get('ruster_model_error'):
                        obj['ruster_model_status']=('Loading updated volume or spawner preview…' if upgrading and waiting
                            else 'Model unavailable — retry loading')
                    bpy.context.scene.ruster.asset_status=obj.get('ruster_model_error',obj['ruster_model_status'])
            except ReferenceError:continue  # Deleted while the previous tick yielded.
            except Exception as error:
                obj['ruster_model_status']='Model loading failed'
                obj['ruster_model_error']=str(error)
                bpy.context.scene.ruster.asset_status = f'Could not load {obj.name}: {error}'
            if time.perf_counter()>=deadline:break
    else:
        _visual_iterator=None;_visual_uid=None
    bpy.context.scene.ruster.busy=bool(_visual_queue or _pending_asset_ids or (_job is not None and _job.poll() is None))
    state=bpy.context.scene.ruster
    if _visual_queue and _job is None:
        state.asset_status=f'Linking original models · {len(_visual_queue):,} asset types remaining'
        state.asset_progress_known=False
    elif not state.busy:
        from .loading import model_summary
        counts=model_summary()
        state.asset_status=(f"{counts['READY']:,} placements loaded · {counts['ISSUE']:,} with issues · "
            f"{counts['MISSING']:,} not loaded · {counts['HELPER']:,} helpers")
        state.asset_progress_known=False
    redraw()
    return .05 if _visual_queue else None


def queue_visuals(ids):
    """Share one bounded preview queue across placement and recovery paths."""
    _visual_queue.extend(sorted(set(ids)-set(_visual_queue)))
    if not _visual_queue:return
    bpy.context.scene.ruster.busy=True
    if not bpy.app.timers.is_registered(load_visuals):
        bpy.app.timers.register(load_visuals,first_interval=.1,persistent=True)


def restore_cached_visuals(objects=None):
    """Recover saved placeholders one unique cached model per tick, without extraction."""
    waiting=set(_visual_queue)|_pending_asset_ids|_job_ids
    cache=scene.cache_dir()/'prefabs'
    ids={int(obj['ruster_uid']) for obj in (scene.prefab_roots() if objects is None else objects)
         if not obj.get('ruster_visual') and (not obj.get('ruster_marker') or
             (obj.get('ruster_marker') in {'HELPER','SOUND','TERRAIN'} and not obj.get('ruster_helper_preview_version')))}
    ready=sorted(uid for uid in ids-waiting if (cache/f'{uid}.json.gz').is_file())
    if not ready:return
    queue_visuals(ready)


def request_missing_visuals(objects):
    objects=list(objects)
    ids={int(obj['ruster_uid']) for obj in objects if not obj.get('ruster_visual') and not obj.get('ruster_marker')}
    # Cached models can always recover incrementally, even when uncached unique
    # IDs exceed the conversion batch limit.
    restore_cached_visuals(objects)
    ids.difference_update(_visual_queue)
    if len(ids)>100:
        bpy.context.scene.ruster.asset_status=(f'{len(objects):,} prefab placements imported. '
            'Select placements to load up to 100 different models at a time.')
        return False
    if ids:request_assets(ids)


def frame_first_model(obj):
    if not obj.get('ruster_frame_on_load'):return
    del obj['ruster_frame_on_load']
    if bpy.app.background or not obj.get('ruster_visual') or bpy.context.view_layer.objects.active!=obj or not obj.select_get():return
    if any(operator.bl_idname.startswith('TRANSFORM_OT_')
           for window in bpy.context.window_manager.windows for operator in window.modal_operators):return
    bpy.context.view_layer.update()
    for window in bpy.context.window_manager.windows:
        if window.scene!=bpy.context.scene:continue
        for area in window.screen.areas:
            if area.type!='VIEW_3D':continue
            region=next((region for region in area.regions if region.type=='WINDOW'),None)
            if region:
                with bpy.context.temp_override(window=window,area=area,region=region):
                    bpy.ops.view3d.view_selected(use_all_regions=False)


class RUSTER_OT_setup(bpy.types.Operator):
    bl_idname = 'ruster.setup'
    bl_label = 'Rust Installation'
    bl_description = 'Choose the Rust game installation to load local assets'
    directory: StringProperty(name='Rust installation', subtype='DIR_PATH')

    def invoke(self, context, event):
        state=context.window_manager.ruster_preferences
        from .core.steam import resolve_install
        root=resolve_install(settings.load().get('rust_install',''))
        state.directory=str(root) if root else ''
        return bpy.ops.ruster.preferences('INVOKE_DEFAULT',section='RUSTER_ASSETS')

    def execute(self, context):
        global _pending_project
        try:
            root = settings.validate_install(bpy.path.abspath(self.directory or context.window_manager.ruster_preferences.directory))
            config = settings.load()
            config['rust_install'] = str(root)
            settings.save(config)
            context.window_manager.ruster_preferences.directory=str(root)
            start_job('index')
            if _pending_project:
                pending, _pending_project = _pending_project, None
                open_path(pending)
            self.report({'INFO'},'Rust installation saved; loading the asset catalog')
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

def welcome_projects(config):
    recent=[Path(path) for path in config.get('recent_projects',[])[:5] if Path(path).is_file()]
    recovery=Path(config['last_recovery']) if config.get('last_recovery') else None
    return recent,recovery if recovery and recovery.is_file() else None


def welcome_popup_position(window,ui_scale,config):
    # call_panel anchors its first button, rather than its frame, to the mouse.
    # Account for the welcome header/footer and growth of the recent-project
    # column, using the same button/separator sizes as the native layout below.
    recent,recovery=welcome_projects(config)
    recent_height=(len(recent)+bool(recovery))*(1.15+.35)
    create_height=3*1.15+2*.35
    offset_units=2-max(0,recent_height-create_height)/2
    return window.width//2,round(window.height/2-offset_units*20*ui_scale)


class RUSTER_OT_welcome(bpy.types.Operator):
    bl_idname = 'ruster.welcome'
    bl_label = 'Rustmatics'

    def invoke(self, context, event):
        # Native splash blocks deliberately KEEP_OPEN after operator buttons.
        # A standard panel closes first, then lets New/Open show confirmation.
        window=context.window
        original=(event.mouse_x,event.mouse_y)
        anchor=welcome_popup_position(window,context.preferences.system.ui_scale,settings.load())
        try:
            window.cursor_warp(*anchor)
            return bpy.ops.wm.call_panel('INVOKE_DEFAULT', name='RUSTER_PT_welcome', keep_open=False)
        finally:
            window.cursor_warp(*original)

    def draw(self, context):
        from . import __version__
        layout = self.layout
        layout.operator_context = 'INVOKE_DEFAULT'
        from . import branding
        if self.__class__.__name__ == 'RUSTER_PT_welcome':
            header = layout.split(factor=.31)
            badge = header.column()
            if icon := branding.icon_id(context,welcome=True):
                badge.template_icon(icon_value=icon, scale=8)
            title = header.column()
            title.separator(factor=2)
            title.label(text=f'Version {__version__}')
            title.label(text='Prefab Editor')
            title.label(text='Build with your local Rust assets')
            layout.separator(factor=.7)
        else:
            branding.label(layout,text=f'Rustmatics {__version__}',context=context)
        split = layout.split(factor=.5)
        col = split.column()
        col.label(text='Create or Open')
        buttons = col.column()
        buttons.scale_y = 1.15
        for op in ('ruster.new','ruster.open'):
            action(buttons,op)
            buttons.separator(factor=.35)
        buttons.operator('ruster.import_map',text='Import .map…',icon=I['import'])
        recent_col = split.column()
        recent_col.label(text='Recent Projects')
        recent_buttons = recent_col.column()
        recent_buttons.scale_y = 1.15
        recent,recovery=welcome_projects(settings.load())
        if recovery:
            action(recent_buttons,'ruster.open',text='Recover Autosaved Session',icon='recover').filepath=str(recovery)
            recent_buttons.separator(factor=.35)
        if recent:
            for path in recent:
                action(recent_buttons,'ruster.open',text=path.name,icon='world' if path.suffix.lower()=='.map' else 'project').filepath=str(path)
                recent_buttons.separator(factor=.35)
        else:
            recent_col.label(text='No recent projects')
            wrapped(recent_col,context,'Start an empty prefab composition or import existing placements.')
        layout.separator(factor=1.5)
        footer = layout.split(factor=.5)
        preferences = footer.column()
        preferences.scale_y = 1.15
        action(preferences,'ruster.preferences')
        footer.column().label(text=f'Blender {bpy.app.version_string}')
        layout.separator(factor=1.5)

    def execute(self, context):
        return {'FINISHED'}


class RUSTER_OT_new(bpy.types.Operator):
    bl_idname = 'ruster.new'
    bl_label = 'New Prefab'
    bl_description = 'Start an empty Rust prefab composition'

    def invoke(self, context, event):
        if context.scene.ruster.mode == 'PREFAB' and not context.scene.objects:
            return {'FINISHED'}
        from .quitting import confirm_replacement
        return confirm_replacement('NEW')

    def execute(self, context):
        from .studio import new_prefab
        if context.scene.ruster.mode != 'PREFAB' or context.scene.objects:
            new_prefab()
        return {'FINISHED'}


def open_path(path):
    global _pending_project
    path = Path(path).resolve()
    if path.suffix.lower() == '.prefab':
        from .studio import new_prefab, import_prefab
        from .core.rustedit_prefab import RustEditPrefab
        RustEditPrefab.load(path)  # Validate before replacing the current project.
        new_prefab()
        import_prefab(path)
        bpy.context.scene.ruster.title = path.stem
    elif path.suffix.lower() == '.map':
        from .map_prefabs import import_map
        import_map(path, replace=True)
    elif path.suffix.lower() == '.ruster':
        directory = session_dir()
        manifest = project.unpack(path, directory)
        if manifest['mode'] == 'MAP':
            # A legacy project's source must be valid before replacing the
            # current scene. Never rebuild its terrain or path editing tools.
            from .map_prefabs import read_map
            read_map(directory / 'source.map')
        # No embedded Python, drivers, or file UI is trusted or executed.
        bpy.ops.wm.open_mainfile(filepath=str(directory / 'scene.blend'), load_ui=False, use_scripts=False)
        state = bpy.context.scene.ruster
        state.project_path = str(path)
        native_ground = manifest['mode']=='MAP' and state.native_ground
        state.mode = 'MAP' if native_ground else 'PREFAB'
        state.title = manifest.get('title', path.stem)
        state.source_map = str(directory/'source.map') if native_ground else ''
        terrain_state = state.terrain_state
        if not native_ground:state.terrain_state = None
        state.busy = _job is not None and _job.poll() is None
        if native_ground:
            from .core.ground import validate_ground
            from . import editing
            editing.clear_cache();validate_ground(editing.terrain().export_world());editing.refresh()
        elif manifest['mode'] == 'MAP':
            for obj in list(bpy.context.scene.objects):
                if obj.get('ruster_kind') not in {'PREFAB','GROUP','VISUAL'}:
                    data = obj.data
                    bpy.data.objects.remove(obj, do_unlink=True)
                    if data and data.users == 0:
                        if isinstance(data, bpy.types.Mesh):bpy.data.meshes.remove(data)
                        elif isinstance(data, bpy.types.Curve):bpy.data.curves.remove(data)
            if terrain_state and terrain_state.users <= int(terrain_state.use_fake_user):
                bpy.data.texts.remove(terrain_state)
            for collection in list(bpy.data.collections):
                if not collection.objects and not collection.children and collection.name in {'Terrain','Paths','Build'}:
                    bpy.data.collections.remove(collection)
        from . import ui
        if not bpy.app.background:bpy.app.timers.register(ui.start_workspace, first_interval=.1)
    else:
        raise ValueError('Choose a Rust .map, RustEdit .prefab or Rustmatics .ruster file')
    # File import and post-load source relocation must be an undo baseline.
    # Otherwise the first brush undo can restore the old scene or a stale source.
    if bpy.context.preferences.edit.use_global_undo:
        if bpy.ops.ed.undo_push.poll():bpy.ops.ed.undo_push(message='Open Rustmatics Project')
        elif not bpy.app.background and not bpy.app.timers.is_registered(open_undo_baseline):
            # A freshly loaded .blend has not initialized its undo context until
            # the next UI cycle. Finish opening, then establish the baseline.
            bpy.app.timers.register(open_undo_baseline,first_interval=.1)
    remember_project(path)
    if path.suffix.lower() == '.ruster':
        from . import quitting
        quitting.mark_saved()


def open_undo_baseline():
    if not bpy.context.preferences.edit.use_global_undo:return None
    for window in bpy.context.window_manager.windows:
        area=next((area for area in window.screen.areas if area.type=='VIEW_3D'),None)
        if not area:continue
        region=next((region for region in area.regions if region.type=='WINDOW'),None)
        with bpy.context.temp_override(window=window,area=area,region=region):
            if bpy.ops.ed.undo_push.poll():
                bpy.ops.ed.undo_push(message='Open Rustmatics Project');return None
    return .1


def remember_project(path):
    config=settings.load()
    path=str(Path(path).resolve())
    recent=[path]+[p for p in config.get('recent_projects',[]) if p!=path]
    settings.save({**config,'last_project':path,'recent_projects':recent[:10]})


def save_project(path,update_path=True):
    state=bpy.context.scene.ruster
    old_path,old_title=state.project_path,state.title
    try:
        if update_path:
            state.project_path=str(Path(path).resolve())
            state.title=Path(path).stem
        with tempfile.TemporaryDirectory(prefix='ruster-save-') as temp:
            blend=Path(temp)/'scene.blend'
            from .protection import pack_models
            pack_models()
            bpy.ops.file.pack_all()
            bpy.ops.wm.save_as_mainfile(filepath=str(blend),copy=True,check_existing=False,compress=True)
            project.save(path,blend,state.mode,state.source_map or None,state.title)
        if update_path:
            remember_project(path)
            from . import quitting
            quitting.mark_saved()
    except Exception:
        state.project_path,state.title=old_path,old_title
        raise


def recovery_tick():
    if bpy.app.background:return None
    try:
        if bpy.context.window_manager.is_interface_locked or bpy.context.mode!='OBJECT':return 20.
        from . import editing,painting
        if editing.stroke_active or painting.stroke_active:return 20.
        state=bpy.context.scene.ruster
        path=settings.user_root()/'recovery'/'last-session.ruster'
        save_project(path,update_path=False)
        config=settings.load();config['last_recovery']=str(path);settings.save(config)
    except Exception:
        # Recovery must never interrupt active editing; manual saves still report errors.
        pass
    return 120.


class RUSTER_OT_open(bpy.types.Operator, ImportHelper):
    bl_idname = 'ruster.open'
    bl_label = 'Open Map, Prefab or Project'
    filter_glob: StringProperty(default='*.map;*.prefab;*.ruster', options={'HIDDEN'})

    def invoke(self, context, event):
        from .quitting import confirm_replacement
        return confirm_replacement('OPEN', self.filepath)

    def execute(self, context):
        # Interactive invocation checks unsaved work before entering the picker.
        if not self.filepath:
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        try:
            open_path(self.filepath)
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class RUSTER_OT_save(bpy.types.Operator, ExportHelper):
    bl_idname = 'ruster.save'
    bl_label = 'Save Project'
    filename_ext = '.ruster'
    filter_glob: StringProperty(default='*.ruster', options={'HIDDEN'})
    save_as: BoolProperty(default=False, options={'HIDDEN'})
    quit_after_save: BoolProperty(default=False, options={'HIDDEN','SKIP_SAVE'})
    after_save: EnumProperty(items=[('NONE','Stay in Project',''),('NEW','New Prefab',''),
        ('OPEN','Open Project','')], default='NONE', options={'HIDDEN','SKIP_SAVE'})
    next_filepath: StringProperty(options={'HIDDEN','SKIP_SAVE'})

    def invoke(self, context, event):
        if context.scene.ruster.project_path and not self.save_as:
            self.filepath = context.scene.ruster.project_path
            return self.execute(context)
        self.filepath = context.scene.ruster.title + '.ruster'
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        try:
            if Path(self.filepath).suffix.lower() != '.ruster':
                self.filepath += '.ruster'
            save_project(self.filepath)
            self.report({'INFO'}, f'Saved {Path(self.filepath).name}')
            if self.quit_after_save:
                from . import quitting
                quitting.schedule_quit()
            elif self.after_save != 'NONE':
                from .quitting import schedule_action
                schedule_action(self.after_save, self.next_filepath)
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class RUSTER_OT_import_map(bpy.types.Operator, ImportHelper):
    bl_idname = 'ruster.import_map'
    bl_label = 'Import .map'
    bl_description = 'Append existing prefab placements from a Rust map'
    bl_options = {'REGISTER','UNDO'}
    filter_glob: StringProperty(default='*.map', options={'HIDDEN'})

    def execute(self, context):
        try:
            from .map_prefabs import import_map
            created = import_map(self.filepath)
            self.report({'INFO'}, f'Imported {len(created):,} prefab placements')
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class RUSTER_OT_export(bpy.types.Operator, ExportHelper):
    bl_idname = 'ruster.export_map'
    bl_label = 'Export .map'
    bl_description = 'Export prefab placements and any added native Rust ground layers'
    filename_ext = '.map'
    filter_glob: StringProperty(default='*.map', options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.scene.ruster.mode in {'PREFAB','MAP'}

    def execute(self, context):
        try:
            from .map_prefabs import export_map
            export_map(self.filepath)
            self.report({'INFO'}, 'Exported Rust map data')
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class RUSTER_OT_validate(bpy.types.Operator):
    bl_idname = 'ruster.validate'
    bl_label = 'Validate Map'

    def execute(self, context):
        from . import map_prefabs
        errors, notes = map_prefabs.validate()
        def draw(popup, _context):
            for error in errors:
                wrapped(popup.layout,_context,error,icon='error')
            if not errors:
                wrapped(popup.layout,_context,'Map data is ready to export',icon='validate')
            for note in notes:
                wrapped(popup.layout,_context,note,icon='info')
        context.window_manager.popup_menu(draw, title='Map Validation')
        return {'FINISHED'}


class RUSTER_OT_assets(bpy.types.Operator):
    bl_idname = 'ruster.assets'
    bl_label = 'Load Selected Models'
    bl_description = 'Load selected prefab models, or refresh names when nothing is selected'
    rebuild: BoolProperty(default=False, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return not context.scene.ruster.busy

    def execute(self, context):
        try:
            from .placement import selected_prefabs
            ids={int(o['ruster_uid']) for o in selected_prefabs()}
            if self.rebuild or not ids:start_job('index')
            elif not request_assets(ids):raise ValueError(context.scene.ruster.asset_status)
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class RUSTER_OT_cancel_assets(bpy.types.Operator):
    bl_idname='ruster.cancel_assets'
    bl_label='Cancel Asset Loading'
    def execute(self,context):
        global _job_cancelled,_visual_iterator,_visual_uid
        cancelled=_pending_asset_ids|_job_ids|set(_visual_queue)
        _pending_asset_ids.clear();_visual_queue.clear()
        _visual_iterator=None;_visual_uid=None
        from . import preview_jobs
        preview_jobs.cancel()
        _job_cancelled=True
        for obj in scene.prefab_roots():
            if int(obj['ruster_uid']) in cancelled and not obj.get('ruster_visual'):
                obj['ruster_model_status']='Model loading cancelled'
        if _job is not None and _job.poll() is None:
            _job.terminate()
        else:
            context.scene.ruster.busy=False
        context.scene.ruster.asset_status='Asset loading cancelled'
        return {'FINISHED'}


class RUSTER_OT_catalog_page(bpy.types.Operator):
    bl_idname='ruster.catalog_page'
    bl_label='Change Prefab Page'
    delta:IntProperty(default=1)
    def execute(self,context):
        state=context.scene.ruster
        try:
            from .browser import prefab_page
            result=prefab_page(state)
            state.query_page=max(0,min(result['pages']-1,result['page']+self.delta))
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'},'Could not read the prefab catalog: '+str(error));return {'CANCELLED'}


class RUSTER_FH_project(bpy.types.FileHandler):
    bl_idname='RUSTER_FH_project'
    bl_label='Rustmatics Map, Prefab or Project'
    bl_import_operator='ruster.open'
    bl_file_extensions='.map;.prefab;.ruster'
    @classmethod
    def poll_drop(cls,context):return context.area is not None and context.area.type=='VIEW_3D'


class RUSTER_OT_place(bpy.types.Operator):
    bl_idname = 'ruster.place'
    bl_label = 'Place Prefab'
    bl_options = {'REGISTER', 'UNDO'}
    uid: StringProperty()

    @classmethod
    def description(cls,context,properties):
        from .browser import prefab_description
        return prefab_description(properties.uid,'Place at the 3D cursor')

    def execute(self, context):
        try:
            uid = int(self.uid)
            found=catalog.find(scene.cache_dir(), uid) if 0 <= uid <= 0xFFFFFFFF else None
            if not found:
                raise ValueError('Prefab is not in the local Rust catalog')
            fields = [wire.encode(1, 2, b'Decor'), wire.encode(2, 0, uid)]
            fields = wire.replace(fields, 3, 2, wire.set_vector(None, scene.to_unity(context.scene.cursor.location)))
            fields = wire.replace(fields, 5, 2, wire.set_vector(None, (1.,1.,1.)))
            collection = bpy.data.collections.get('Prefabs') or scene.new_collection('Prefabs')
            first=context.scene.ruster.mode=='PREFAB' and not any(root.get('ruster_visual') for root in scene.prefab_roots())
            obj = scene.root_object(fields, collection)
            if first:obj['ruster_frame_on_load']=True
            scene.name_prefab(obj,found[0])
            scene.attach_visual(obj)
            from .construction import add_symmetry
            try:add_symmetry(obj,context)
            except Exception:
                bpy.data.objects.remove(obj,do_unlink=True)
                raise
            for item in context.selected_objects:
                item.select_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
            if obj.get('ruster_visual'):frame_first_model(obj)
            if not obj.get('ruster_visual') and not obj.get('ruster_marker'):request_missing_visuals([obj])
            from .core import palettes
            from . import thumbnails
            thumbnails.request(uid)
            try:settings.save(palettes.remember(settings.load(),uid))
            except OSError:self.report({'WARNING'},'Prefab placed, but the recent list could not be saved')
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class RUSTER_OT_duplicate(bpy.types.Operator):
    bl_idname = 'ruster.duplicate'
    bl_label = 'Duplicate Prefabs'
    bl_options = {'REGISTER', 'UNDO'}
    offset: FloatVectorProperty(name='Offset', size=3, default=(2, 0, 0), subtype='TRANSLATION')

    @classmethod
    def poll(cls, context):
        from .placement import selected_roots
        return context.mode == 'OBJECT' and bool(selected_roots())

    def execute(self, context):
        from .placement import duplicate_selected
        try:duplicate_selected(self.offset);return {'FINISHED'}
        except Exception as error:self.report({'ERROR'}, str(error));return {'CANCELLED'}


class RUSTER_OT_duplicate_move(bpy.types.Macro):
    bl_idname = 'ruster.duplicate_move'
    bl_label = 'Duplicate Prefabs'
    bl_description = 'Duplicate whole prefabs, then move with native axis constraints and snapping; Esc keeps the copy in place'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D' and RUSTER_OT_duplicate.poll(context))


class RUSTER_OT_delete(bpy.types.Operator):
    bl_idname = 'ruster.delete'
    bl_label = 'Delete Prefabs'
    bl_options = {'REGISTER', 'UNDO'}
    confirm: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        from .placement import selected_roots
        return context.mode == 'OBJECT' and bool(selected_roots())

    def invoke(self, context, event):
        if self.confirm:return invoke_dialog(self,context,event,title='Delete selected prefabs?',confirm_text='Delete')
        return self.execute(context)

    def execute(self, context):
        from .placement import delete_selected
        delete_selected()
        return {'FINISHED'}


class RUSTER_OT_report(bpy.types.Operator):
    bl_idname = 'ruster.asset_report'
    bl_label = 'Asset Report'

    def execute(self, context):
        try:
            path=scene.cache_dir()/'asset-report.json'
            issues=json.loads(path.read_text()) if path.is_file() else []
            if not isinstance(issues,list):raise ValueError('Expected a list of asset issues')
            entries=[]
            reported=set()
            names={int(obj['ruster_uid']):obj.name for obj in scene.prefab_roots()}
            from .core.asset_names import display_name
            for item in issues:
                if not isinstance(item,dict):continue
                uid=item.get('uid')
                name=names.get(uid) or display_name(str(item.get('path',''))) or 'Prefab '+str(uid)
                entries.append((name,str(item.get('error','Unknown asset issue'))))
                reported.add(uid)
            # An imported/offline project's errors need not belong to the last worker batch.
            for obj in scene.prefab_roots():
                uid=int(obj['ruster_uid'])
                if obj.get('ruster_model_error') and uid not in reported:
                    entries.append((obj.name,str(obj['ruster_model_error'])));reported.add(uid)
            block=bpy.data.texts.get('Rustmatics Asset Report') or bpy.data.texts.new('Rustmatics Asset Report')
            block.clear();block.write(context.scene.ruster.asset_status+'\n\n'+
                '\n\n'.join(name+'\n'+error for name,error in entries))
            self.entries=json.dumps(entries[:5])
            self.total=len(entries)
            return {'FINISHED'}
        except (OSError,ValueError,TypeError) as error:
            self.report({'ERROR'},'Could not read the asset report: '+str(error));return {'CANCELLED'}

    entries:StringProperty(options={'HIDDEN','SKIP_SAVE'})
    total:IntProperty(options={'HIDDEN','SKIP_SAVE'})

    def invoke(self,context,event):
        result=self.execute(context)
        if result!={'FINISHED'}:return result
        return invoke_dialog(self,context,event,width=560,confirm_text='Close')

    def draw(self,context):
        wrapped(self.layout,context,context.scene.ruster.asset_status,icon='info',width=500)
        self.layout.label(text=f'{self.total:,} reported issues')
        for name,error in json.loads(self.entries or '[]'):
            box=self.layout.box()
            wrapped(box,context,name,icon='prefab',width=500)
            wrapped(box,context,error[:320]+('…' if len(error)>320 else ''),icon='error',width=500)
        if self.total>5:wrapped(self.layout,context,'Showing the first 5 issues. Copy the full report for all details.',width=500)
        self.layout.operator('ruster.copy_asset_report',icon=I['duplicate'])
        wrapped(self.layout,context,'Logs and source report: '+str(scene.cache_dir()),width=500)


class RUSTER_OT_copy_asset_report(bpy.types.Operator):
    bl_idname='ruster.copy_asset_report'
    bl_label='Copy Full Asset Report'
    def execute(self,context):
        block=bpy.data.texts.get('Rustmatics Asset Report')
        if not block:self.report({'ERROR'},'Open the asset report first');return {'CANCELLED'}
        context.window_manager.clipboard=block.as_string()
        self.report({'INFO'},'Asset report copied');return {'FINISHED'}


class RUSTER_OT_select_model_status(bpy.types.Operator):
    bl_idname='ruster.select_model_status'
    bl_label='Select Models by Status'
    bl_description='Select the placements needing model loading or attention'
    status:EnumProperty(items=[('MISSING','Not Loaded',''),('ISSUE','With Issues','')])
    def execute(self,context):
        from .loading import model_state
        from .placement import select
        pending=_pending_asset_ids|_job_ids|set(_visual_queue)
        objects=[obj for obj in scene.prefab_roots() if model_state(obj,pending)==self.status]
        select(objects)
        self.report({'INFO'},f'{len(objects):,} placements selected')
        return {'FINISHED'}


@persistent
def before_scene_replace(_dummy):
    global _visual_iterator,_visual_uid
    _visual_iterator=None;_visual_uid=None


@persistent
def after_undo(_dummy):
    global _visual_iterator,_visual_uid
    _visual_iterator=None;_visual_uid=None
    if not hasattr(bpy.context.scene,'ruster'):return
    objects=scene.prefab_roots()
    for obj in objects:
        if 'ruster_frame_on_load' in obj:del obj['ruster_frame_on_load']
    restore_cached_visuals(objects)


@persistent
def after_load(_dummy):
    global _visual_iterator,_visual_uid
    _visual_iterator=None;_visual_uid=None
    from . import preview_jobs
    preview_jobs.cancel()
    from . import editing
    editing.clear_cache()
    if hasattr(bpy.context.scene, 'ruster'):
        bpy.context.scene.ruster.busy = bool(_job is not None or _visual_queue or _pending_asset_ids)
        if _job is not None and not bpy.app.timers.is_registered(poll_job):
            bpy.app.timers.register(poll_job,first_interval=.2,persistent=True)
        if _visual_queue and not bpy.app.timers.is_registered(load_visuals):
            bpy.app.timers.register(load_visuals,first_interval=.1,persistent=True)
        from . import mcp_bridge
        mcp_bridge.sync_state()
        restore_cached_visuals()


def startup():
    global _pending_project
    if bpy.app.background:
        return None
    if bpy.context.window is None:
        windows=list(bpy.context.window_manager.windows)
        if not windows:return .2
        with bpy.context.temp_override(window=windows[0]):
            return startup()
    from . import ui
    from .studio import new_prefab
    new_prefab()
    ui.start_workspace()
    config = settings.load()
    from .core.steam import resolve_install
    detected=resolve_install(config.get('rust_install',''))
    if not detected:
        bpy.context.scene.ruster.asset_status='Choose your Rust installation in Preferences > Assets'
    else:
        if config.get('rust_install')!=str(detected):
            config['rust_install']=str(detected);settings.save(config)
        bpy.context.window_manager.ruster_preferences.directory=str(detected)
        if (detected/'Bundles/AssetSceneManifest.json').is_file():
            try:start_job('index')
            except Exception as error:bpy.context.scene.ruster.asset_status=str(error)
        else:bpy.context.scene.ruster.asset_status='Rust found; prefab scene manifest is missing'
    if _pending_project:
        try:
            pending, _pending_project = _pending_project, None
            open_path(pending)
        except Exception as error:
            bpy.context.scene.ruster.asset_status = str(error)
            bpy.ops.ruster.welcome('INVOKE_DEFAULT')
    else:
        bpy.ops.ruster.welcome('INVOKE_DEFAULT')
    return None


def register():
    from . import ui
    if hasattr(bpy.types.Scene, 'ruster'):
        return
    classes = [RUSTER_PG_project, RUSTER_OT_setup, RUSTER_OT_welcome, RUSTER_OT_new,
               RUSTER_OT_open, RUSTER_OT_save, RUSTER_OT_import_map, RUSTER_OT_export,
               RUSTER_OT_assets, RUSTER_OT_place, RUSTER_OT_duplicate, RUSTER_OT_duplicate_move, RUSTER_OT_delete,
               RUSTER_OT_report,RUSTER_OT_copy_asset_report,RUSTER_OT_select_model_status,
               RUSTER_OT_cancel_assets,RUSTER_OT_catalog_page,RUSTER_FH_project]
    for cls in classes:
        bpy.utils.register_class(cls)
        _classes.append(cls)
    duplicate = RUSTER_OT_duplicate_move.define('RUSTER_OT_duplicate')
    duplicate.properties.offset = (0, 0, 0)
    move = RUSTER_OT_duplicate_move.define('TRANSFORM_OT_translate')
    move.properties.use_proportional_edit = False
    bpy.types.Scene.ruster = PointerProperty(type=RUSTER_PG_project)
    from . import volumes
    volumes.register()
    from . import editing
    editing.register()
    from . import placement
    placement.register()
    from . import protection,browser
    protection.register()
    browser.register()
    from . import mcp_bridge
    mcp_bridge.register()
    from . import live_preview
    live_preview.register()
    from . import ground
    ground.register()
    ui.register()
    from . import preferences
    preferences.register()
    from . import studio
    studio.register()
    from . import quitting
    quitting.register()
    bpy.app.handlers.load_post.append(after_load)
    for handlers in (bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre,bpy.app.handlers.load_pre):
        handlers.append(before_scene_replace)
    bpy.app.handlers.undo_post.append(after_undo)
    bpy.app.handlers.redo_post.append(after_undo)
    config = bpy.context.window_manager.keyconfigs.addon
    if config:
        km = config.keymaps.new(name='Window', space_type='EMPTY')
        for key, shift, op in [('S',False,'ruster.save'), ('S',True,'ruster.save'), ('O',False,'ruster.open')]:
            item = km.keymap_items.new(op, key, 'PRESS', ctrl=True, shift=shift)
            if key == 'S':
                item.properties.save_as = shift
            _keymaps.append((km,item))
        item = km.keymap_items.new('ruster.quit', 'Q', 'PRESS', ctrl=True)
        _keymaps.append((km,item))
    bpy.context.preferences.view.show_splash = False
    bpy.context.preferences.filepaths.use_scripts_auto_execute = False
    if not bpy.app.background and not bpy.app.timers.is_registered(recovery_tick):
        bpy.app.timers.register(recovery_tick,first_interval=120.)


def unregister():
    global _job,_job_log,_job_action,_job_cancelled,_visual_iterator,_visual_uid
    from . import ui,preferences,studio
    from . import quitting
    from . import preview_jobs
    preview_jobs.cancel()
    quitting.unregister()
    studio.unregister()
    preferences.unregister()
    if _job is not None:
        if _job.poll() is None:_job.terminate()
        try:_job.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _job.kill();_job.wait(timeout=2)
        _job=None
    if _job_log:
        _job_log.close();_job_log=None
    _job_action=None;_job_cancelled=False
    for callback in (poll_job, load_visuals, startup,recovery_tick,open_undo_baseline):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    if after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(after_load)
    for handlers in (bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre,bpy.app.handlers.load_pre):
        if before_scene_replace in handlers:handlers.remove(before_scene_replace)
    for handlers in (bpy.app.handlers.undo_post,bpy.app.handlers.redo_post):
        if after_undo in handlers:handlers.remove(after_undo)
    for km, item in _keymaps:
        km.keymap_items.remove(item)
    _keymaps.clear()
    _pending_asset_ids.clear()
    _job_ids.clear()
    _visual_queue.clear()
    _visual_iterator=None;_visual_uid=None
    from . import mcp_bridge
    mcp_bridge.unregister()
    ui.unregister()
    from . import ground
    ground.unregister()
    from . import live_preview
    live_preview.unregister()
    from . import protection,browser
    browser.unregister()
    protection.unregister()
    from . import placement
    placement.unregister()
    from . import editing
    editing.unregister()
    from . import volumes
    volumes.unregister()
    if hasattr(bpy.types.Scene, 'ruster'):
        del bpy.types.Scene.ruster
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    _classes.clear()
