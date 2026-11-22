"""Optional community Blender MCP bridge with typed, placement-only Rust edits."""
import hmac
import json
import math
import sys
from contextlib import nullcontext
from pathlib import Path
import bpy
from bpy.props import BoolProperty,IntProperty,StringProperty,PointerProperty
from mathutils import Matrix,Euler,Vector
from .vendor.blender_mcp_bridge import BlenderMCPServer
from .core import settings,mcp_config,wire
from . import app,scene,editing,painting,placement
from .assets import catalog
from . import mcp_building,mcp_prefab_ops,mcp_blender,mcp_session,ground
from .ui_style import ICONS as I,action,properties,wrapped

_server=None
_loading=False
_classes=[]
HEADLESS_UNAVAILABLE={'get_viewport_image','get_viewport_state','set_viewport_state','focus_view','undo','redo'}


def vector(values,label,nonzero=False):
    if not isinstance(values,(list,tuple)) or len(values)!=3 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in values):
        raise ValueError(label+' must contain three finite numbers')
    if nonzero and any(abs(v)<1e-7 for v in values):raise ValueError('Scale must be nonzero')
    return tuple(values)


def map_object(name):
    obj=bpy.context.scene.objects.get(name)
    if not obj or obj.library or obj.get('ruster_kind') not in {'PREFAB','GROUP'}:
        raise ValueError('Choose an editable Rustmatics prefab placement or group; original models are read-only')
    return obj


def require_map():
    if bpy.context.scene.ruster.mode!='MAP':raise ValueError('Open or create a Rust map first')


def require_editor():
    if bpy.context.scene.ruster.mode not in {'MAP','PREFAB'}:raise ValueError('Open a prefab composition first')


def require_idle():
    if bpy.context.window_manager.is_interface_locked or editing.stroke_active or painting.stroke_active or bpy.context.mode!='OBJECT':
        raise ValueError('Finish the active edit or operation before MCP changes the composition')


def object_info(server,name,include_bounds=True,hierarchy=None,bounds_cache=None):
    obj=map_object(name)
    location,rotation,scale=obj.matrix_world.decompose()
    hierarchy=mcp_building.hierarchy() if hierarchy is None else hierarchy
    children=[o for o in hierarchy.get(obj,()) if o.get('ruster_kind') in {'PREFAB','GROUP'}]
    return {'name':obj.name,'type':obj.type,'kind':obj.get('ruster_kind'),'prefab_id':obj.get('ruster_uid'),
        'position':list(location),'rotation_degrees':[math.degrees(v) for v in rotation.to_euler('XYZ')],
        'world_scale':list(scale),'transform_space':'WORLD','rotation_order':'XYZ',
        'editable_placement':True,'geometry_read_only':True,'visible':obj.visible_get(),
        'selected':obj.select_get(),'parent':obj.parent.name if obj.parent else None,
        'children':[o.name for o in children[:200]],'child_count':len(children),'children_truncated':len(children)>200,
        'world_bounds':mcp_building.object_bounds(obj,hierarchy,bounds_cache) if include_bounds else None,
        'bounds_requested':include_bounds,'visual_loaded':bool(obj.get('ruster_visual'))}


def prefab_search(query='',category='',limit=20,offset=0,available_only=False):
    if not isinstance(query,str) or not isinstance(category,str) or len(query)>256 or len(category)>256 or len(query.split())>32:raise ValueError('Search/category must contain at most 256 characters and 32 search words')
    if type(available_only)!=bool:raise ValueError('Available-only filter must be boolean')
    if type(limit)!=int or type(offset)!=int or not 1<=limit<=100 or offset<0:raise ValueError('Use a limit of 1–100 and a nonnegative offset')
    rows=catalog.search(scene.cache_dir(),query,limit=limit+1,offset=offset,category=category,available_only=available_only)
    return {'prefabs':[{'id':uid,'path':path,'available':bool(available)} for uid,path,available in rows[:limit]],
        'offset':offset,'limit':limit,'has_more':len(rows)>limit,'next_offset':offset+limit if len(rows)>limit else None,
        'catalog_ready':(scene.cache_dir()/'catalog.sqlite3').is_file()}


def set_transform(obj,position=None,rotation_degrees=None,scale=None):
    bpy.context.view_layer.update()
    location,rotation,old_scale=obj.matrix_world.decompose()
    if position is not None:location=Vector(vector(position,'Position'))
    if rotation_degrees is not None:rotation=Euler(tuple(math.radians(v) for v in vector(rotation_degrees,'Rotation')),'XYZ').to_quaternion()
    if scale is not None:old_scale=Vector(vector(scale,'Scale',True))
    original=placement.transform_state(obj)
    desired=Matrix.LocRotScale(location,rotation,old_scale)
    if not all(math.isfinite(v) for row in desired for v in row):raise ValueError('Transform exceeds Blender number range')
    obj.matrix_world=desired
    bpy.context.view_layer.update()
    # Nonuniform group scale plus child rotation cannot be exported as Rust TRS.
    affected=[obj]+list(obj.children_recursive)
    try:
        if max(abs(desired[i][j]-obj.matrix_world[i][j]) for i in range(4) for j in range(4))>1e-4:
            raise ValueError('Parent transforms cannot represent this world transform without shear')
        for item in affected:
            if item.get('ruster_kind') not in {'PREFAB','GROUP'}:continue
            if not all(math.isfinite(v) for row in item.matrix_world for v in row):raise ValueError('Transform exceeds Blender number range')
            loc,rot,scl=item.matrix_world.decompose();rebuilt=Matrix.LocRotScale(loc,rot,scl)
            if any(abs(v)<1e-7 for v in scl) or scene.matrix_has_shear(item.matrix_world):
                raise ValueError('This transform introduces shear or zero scale; use uniform group scale')
    except Exception:
        placement.restore_transform(obj,original);bpy.context.view_layer.update();raise


def place_prefab(uid,position,rotation_degrees=None,scale=None):
    if type(uid)!=int or not 0<=uid<=0xFFFFFFFF:raise ValueError('Invalid Rust prefab ID')
    found=catalog.find(scene.cache_dir(),uid)
    if not found or not found[1]:raise ValueError('Prefab is unavailable in the local Rust catalog')
    position=vector(position,'Position')
    if rotation_degrees is not None:vector(rotation_degrees,'Rotation')
    if scale is not None:vector(scale,'Scale',True)
    fields=[wire.encode(1,2,b'Decor'),wire.encode(2,0,uid)]
    fields=wire.replace(fields,3,2,wire.set_vector(None,scene.to_unity(position)))
    fields=wire.replace(fields,5,2,wire.set_vector(None,(1.,1.,1.)))
    obj=scene.root_object(fields,bpy.data.collections.get('Prefabs') or scene.new_collection('Prefabs'))
    scene.name_prefab(obj,found[0])
    try:
        scene.attach_visual(obj);set_transform(obj,position,rotation_degrees,scale)
    except Exception:
        for child in list(obj.children_recursive):bpy.data.objects.remove(child,do_unlink=True)
        bpy.data.objects.remove(obj,do_unlink=True);raise
    placement.select([obj])
    if not obj.get('ruster_visual') and not obj.get('ruster_marker'):app.request_missing_visuals([obj])
    return obj


def file_target(path,extension,overwrite):
    if not isinstance(path,str) or not path:raise ValueError('Provide a destination path')
    target=Path(path).expanduser()
    if not target.is_absolute():raise ValueError('Provide an absolute destination path')
    target=target.resolve()
    extensions = (extension,) if isinstance(extension, str) else extension
    if target.suffix.lower() not in extensions:
        raise ValueError('Choose a ' + ' or '.join(extensions) + ' destination')
    if target.exists() and not target.is_file():raise ValueError('Destination must be a file, not a directory')
    if target.exists() and not overwrite:raise ValueError('Destination exists; choose a new name or explicitly enable overwrite')
    return target


class RusterMCPServer(BlenderMCPServer):
    def __init__(self,port,token):
        super().__init__(host='127.0.0.1',port=port)
        self.token=token
        self.headless=False

    def execute_command(self,command):
        try:
            if not isinstance(command,dict) or not isinstance(command.get('token'),str) or not hmac.compare_digest(command['token'],self.token):
                raise ValueError('MCP authentication failed; use the Rustmatics MCP client configuration')
            params=command.get('params',{})
            if not isinstance(params,dict):raise ValueError('Command parameters must be an object')
            # File loading replaces Window/Scene RNA. Never keep a temporary
            # context override or selection references alive across that load.
            if command.get('type') in mcp_session.PROJECT_EDIT:
                return {'status':'success','result':self.dispatch(command['type'],params)}
            if self.headless and command.get('type') in HEADLESS_UNAVAILABLE:
                raise ValueError('This tool requires the desktop app; it is unavailable in headless mode')
            windows=list(bpy.context.window_manager.windows)
            if not windows and not self.headless:raise ValueError('Rustmatics has no active window')
            window=next((w for w in windows if any(a.type=='VIEW_3D' for a in w.screen.areas)),windows[0] if windows else None)
            with nullcontext() if self.headless else bpy.context.temp_override(window=window):
                selected=[obj.name for obj in bpy.context.selected_objects]
                active=bpy.context.view_layer.objects.active
                active_name=active.name if active else None
                try:result=self.dispatch(command.get('type'),params)
                except Exception:
                    placement.select([bpy.context.scene.objects[name] for name in selected if name in bpy.context.scene.objects])
                    bpy.context.view_layer.objects.active=bpy.context.scene.objects.get(active_name) if active_name else None
                    raise
            return {'status':'success','result':result}
        except Exception as error:return {'status':'error','message':str(error)}

    def dispatch(self,kind,params):
        if kind=='ping':return {'pong':True,'application':'Rustmatics'}
        if kind in ground.READ|ground.EDIT:return ground.dispatch(self,kind,params,sys.modules[__name__])
        if kind in mcp_session.READ|mcp_session.EDIT:return mcp_session.dispatch(self,kind,params,sys.modules[__name__])
        if kind in mcp_blender.READ|mcp_blender.EDIT:return mcp_blender.dispatch(self,kind,params,sys.modules[__name__])
        if kind in {'duplicate_object','delete_object'}:
            result=mcp_prefab_ops.dispatch(self,'duplicate_objects' if kind=='duplicate_object' else 'delete_objects',{'names':[params['name']]},sys.modules[__name__])
            return {'objects':[object_info(self,name,False) for name in result['objects']]} if kind=='duplicate_object' else {'deleted':params['name']}
        if kind in mcp_prefab_ops.READ|mcp_prefab_ops.EDIT:return mcp_prefab_ops.dispatch(self,kind,params,sys.modules[__name__])
        if kind in mcp_building.READ|mcp_building.EDIT:
            return mcp_building.dispatch(self,kind,params,sys.modules[__name__])
        if kind=='get_scene_info':
            result=self.get_scene_info();state=bpy.context.scene.ruster
            result.update(mode=state.mode,title=state.title,map_size=state.map_size,
                prefab_count=len(scene.prefab_roots()),coordinates='Editor world metres; Z is height, rotations XYZ degrees')
            return result
        if kind=='get_object_info':return object_info(self,**params)
        if kind=='search_prefabs':return prefab_search(**params)
        if kind in {'list_map_objects','list_objects'}:return mcp_prefab_ops.list_objects(self,params,sys.modules[__name__])
        if kind=='validate_map':
            from . import map_prefabs
            return map_prefabs.validation_status(require_ground=params.get('require_ground') is True)
        if kind=='export_project_report':
            from . import project_report
            overwrite=params.get('overwrite') is True
            target=file_target(params.get('path'),'.docx',overwrite)
            return project_report.export(target,overwrite=overwrite)
        # There is deliberately no generic eval/exec or upstream asset downloader.
        if kind not in {'place_prefab','transform_object','duplicate_object','delete_object','save_project','export_map','undo','redo'}:
            raise ValueError('Unsupported Rustmatics MCP command; arbitrary Python and prefab mesh editing are not exposed')
        require_idle()
        if kind in {'save_project','export_map'}:
            target=file_target(params.get('path'),'.ruster' if kind=='save_project' else '.map',params.get('overwrite') is True)
            if kind=='save_project':app.save_project(target)
            else:
                from . import map_prefabs
                map_prefabs.export_map(target,overwrite=params.get('overwrite') is True,
                                       require_ground=params.get('require_ground') is True)
            return {'path':str(target)}
        if kind in {'undo','redo'}:
            result=getattr(bpy.ops.ed,kind)()
            if result!={'FINISHED'}:raise ValueError('Nothing to '+kind)
            return {'finished':True}
        require_editor()
        if kind=='place_prefab':obj=place_prefab(**params);result=object_info(self,obj.name)
        elif kind=='transform_object':
            values=dict(params);obj=map_object(values.pop('name'))
            set_transform(obj,**values);result=object_info(self,obj.name)
        if bpy.context.preferences.edit.use_global_undo:bpy.ops.ed.undo_push(message='MCP: '+kind.replace('_',' ').title())
        app.redraw()
        return result


def stop():
    global _server
    if _server:_server.stop();_server=None


def start(port, *, manual_pump=False):
    global _server
    if _server and _server.running:return
    candidate=RusterMCPServer(port,mcp_config.token());candidate.headless=bool(manual_pump)
    candidate.start(manual_pump=manual_pump)
    if not candidate.running:raise ValueError('Could not start MCP. The selected local port may already be in use.')
    _server=candidate


def running():return bool(_server and _server.running)


def update_enabled(state,context):
    if _loading:return
    config=settings.load();settings.save({**config,'mcp_enabled':bool(state.enabled),'mcp_port':state.port})
    try:
        if state.enabled:start(state.port)
        else:stop()
        state.status='Running on 127.0.0.1:'+str(state.port) if running() else 'Disabled'
    except Exception as error:state.status=str(error)


def update_port(state,context):
    if _loading:return
    if running():
        sync_state();return  # Stop before changing the listener address.
    config=settings.load();settings.save({**config,'mcp_port':state.port})
    if state.enabled:update_enabled(state,context)


class RUSTER_PG_mcp(bpy.types.PropertyGroup):
    enabled:BoolProperty(name='Enable MCP',description='Allow local MCP clients to inspect Rustmatics and author prefab compositions',default=False,options={'SKIP_SAVE'},update=update_enabled)
    port:IntProperty(name='Local Port',default=mcp_config.DEFAULT_PORT,min=1024,max=65535,options={'SKIP_SAVE'},update=update_port)
    status:StringProperty(default='Disabled',options={'SKIP_SAVE'})


def sync_state():
    global _loading
    if not hasattr(bpy.context.window_manager,'ruster_mcp'):return
    _loading=True
    try:
        config=settings.load();state=bpy.context.window_manager.ruster_mcp
        state.enabled=mcp_config.enabled(config);state.port=mcp_config.port(config)
        state.status='Running on 127.0.0.1:'+str(_server.port) if running() else ('Enabled; waiting to start' if state.enabled else 'Disabled')
    finally:_loading=False


def startup():
    sync_state()
    state=bpy.context.window_manager.ruster_mcp
    if state.enabled:update_enabled(state,bpy.context)
    return None


def client_config():
    root=Path(__file__).resolve().parent.parent
    args=['-B'] if sys.platform=='darwin' else []
    entry={'command':app.worker_python(),'args':[*args,str(root/'mcp_server.py')]}
    if 'RUSTER_USER_DIR' in __import__('os').environ:entry['env']={'RUSTER_USER_DIR':str(settings.user_root())}
    return {'mcpServers':{'ruster':entry}}


class RUSTER_OT_mcp_config(bpy.types.Operator):
    bl_idname='ruster.mcp_config'
    bl_label='Copy MCP Client Configuration'
    def execute(self,context):
        try:context.window_manager.clipboard=json.dumps(client_config(),indent=2)
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        self.report({'INFO'},'MCP client configuration copied');return {'FINISHED'}


class RUSTER_OT_mcp_retry(bpy.types.Operator):
    bl_idname='ruster.mcp_retry'
    bl_label='Retry MCP Connection'
    def execute(self,context):update_enabled(context.window_manager.ruster_mcp,context);return {'FINISHED'}


def draw_settings(layout,context):
    state=context.window_manager.ruster_mcp
    width=min(500,max(180,context.region.width/max(context.preferences.system.ui_scale,.5)-180))
    layout.label(text='MCP · AI Editor Connection',icon=I['settings'])
    properties(layout).prop(state,'enabled')
    row=layout.row();row.enabled=not running();row.prop(state,'port')
    wrapped(layout,context,state.status,icon='validate' if running() else ('info' if not state.enabled else 'error'),width=width)
    if state.enabled and not running():action(layout,'ruster.mcp_retry')
    action(layout,'ruster.mcp_config')
    layout.separator()
    wrapped(layout,context,'Connect an MCP client using the copied configuration. Keep Rustmatics open while connected.',width=width)
    wrapped(layout,context,'Uses the community Blender MCP bridge with whole-prefab composition and export tools. Prefab geometry stays read-only.',icon='locked',width=width)
    wrapped(layout,context,'Connection settings are saved automatically on this computer.',width=width)


def register():
    for cls in (RUSTER_PG_mcp,RUSTER_OT_mcp_config,RUSTER_OT_mcp_retry):bpy.utils.register_class(cls);_classes.append(cls)
    bpy.types.WindowManager.ruster_mcp=PointerProperty(type=RUSTER_PG_mcp)
    sync_state()
    if not bpy.app.background:bpy.app.timers.register(startup,first_interval=1.,persistent=True)


def unregister():
    if bpy.app.timers.is_registered(startup):bpy.app.timers.unregister(startup)
    stop()
    del bpy.types.WindowManager.ruster_mcp
    for cls in reversed(_classes):bpy.utils.unregister_class(cls)
    _classes.clear()
