"""Rustmatics sections inside Blender's existing Preferences editor and navigation."""
import bpy
from bpy.props import EnumProperty,PointerProperty,StringProperty
from bpy.app.handlers import persistent
from bl_ui.space_userpref import CenterAlignMixIn
from . import app,scene,mcp_bridge
from .core import settings,steam
from .ui_style import ICONS as I,action,properties,wrapped

_classes=[]
_polls=[]
_refreshed=[]
_original_navigation=None
_owner=object()
NATIVE_ITEMS=tuple((i.identifier,i.name,i.description,i.icon,i.value) for i in bpy.types.Preferences.bl_rna.properties['active_section'].enum_items
    if i.identifier in {'INTERFACE','THEMES','VIEWPORT','NAVIGATION','INPUT','KEYMAP','SYSTEM','SAVE_LOAD'})
NATIVE_IDS={item[0] for item in NATIVE_ITEMS}
SECTIONS=(*NATIVE_ITEMS,None,('RUSTER_ASSETS','Assets','Local Rust installation and assets','NONE',100),('RUSTER_MCP','MCP','AI editor connection','NONE',101))


def is_rust(context):
    state=getattr(context.window_manager,'ruster_preferences',None)
    return bool(state and state.section.startswith('RUSTER_'))


def changed(state,context):
    if state.section in NATIVE_IDS:context.preferences.active_section=state.section
    app.redraw()


def native_changed():
    # Native operators (including extension settings shortcuts) can select a
    # Blender section without going through our expanded navigation property.
    state=getattr(bpy.context.window_manager,'ruster_preferences',None)
    if state and state.section!=bpy.context.preferences.active_section:
        state.section=bpy.context.preferences.active_section


def subscribe():
    bpy.msgbus.clear_by_owner(_owner)
    bpy.msgbus.subscribe_rna(key=(bpy.types.Preferences,'active_section'),owner=_owner,args=(),notify=native_changed,options={'PERSISTENT'})


@persistent
def after_load(_dummy):
    if hasattr(bpy.context.window_manager,'ruster_preferences'):
        state=bpy.context.window_manager.ruster_preferences
        state.section=bpy.context.preferences.active_section
        root=steam.resolve_install(settings.load().get('rust_install',''))
        state.directory=str(root) if root else ''
        from .assets.metadata import rustedit_root
        root=rustedit_root(settings.load().get('rustedit_install'))
        state.rustedit=str(root) if root else ''
        subscribe()


def navigation(self,context):
    col=self.layout.column();col.scale_x=1.3;col.scale_y=1.3
    col.prop(context.window_manager.ruster_preferences,'section',expand=True)


class RUSTER_PG_preferences(bpy.types.PropertyGroup):
    section:EnumProperty(items=SECTIONS,name='Preferences Section',options={'SKIP_SAVE'},update=changed)
    directory:StringProperty(name='Rust Installation',description='Game folder containing the Rust executable and Bundles folder',subtype='DIR_PATH',options={'SKIP_SAVE'})
    rustedit:StringProperty(name='RustEdit Folder',description='Read local prefab names and available preview images',subtype='DIR_PATH',options={'SKIP_SAVE'})


class RUSTER_OT_catalog_source(bpy.types.Operator):
    bl_idname='ruster.catalog_source'
    bl_label='Apply Catalog Source'
    def execute(self,context):
        from .assets.metadata import rustedit_root
        root=rustedit_root(bpy.path.abspath(context.window_manager.ruster_preferences.rustedit))
        if not root:self.report({'ERROR'},'Choose RustEdit folder containing stringpool.txt');return {'CANCELLED'}
        config=settings.load();config['rustedit_install']=str(root);settings.save(config)
        context.window_manager.ruster_preferences.rustedit=str(root)
        from . import thumbnails
        if not bpy.app.background:thumbnails.refresh()
        try:app.start_job('index');return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}


class RUSTER_OT_generator_install(bpy.types.Operator):
    bl_idname='ruster.generator_install'
    bl_label='Apply Generator Installation'
    detect: bpy.props.BoolProperty(default=False,options={'SKIP_SAVE'})
    def execute(self,context):
        from .core import generation
        try:
            state=context.window_manager.ruster_preferences
            path=generation.detect() if self.detect else generation.executable(bpy.path.abspath(state.dedicated))
            if path is None:raise ValueError('RustDedicated was not found in Steam libraries. Choose its executable manually.')
            state.dedicated=str(path);config=settings.load();config['rust_dedicated']=str(path);settings.save(config)
            self.report({'INFO'},'Generator installation saved');return {'FINISHED'}
        except (OSError,ValueError) as error:self.report({'ERROR'},str(error));return {'CANCELLED'}


class RUSTER_OT_preferences(bpy.types.Operator):
    bl_idname='ruster.preferences'
    bl_label='Preferences'
    bl_description="Open Blender Preferences, including Assets and MCP"
    section:StringProperty(options={'SKIP_SAVE'})
    def invoke(self,context,event):return self.execute(context)
    def execute(self,context):
        state=context.window_manager.ruster_preferences
        if self.section:
            if self.section not in NATIVE_IDS|{'RUSTER_ASSETS','RUSTER_MCP'}:
                self.report({'ERROR'},'Unknown preferences section');return {'CANCELLED'}
            state.section=self.section
        # Blender reuses an existing Preferences window when possible.
        if not bpy.app.background:bpy.ops.screen.userpref_show('INVOKE_DEFAULT')
        return {'FINISHED'}


class RUSTER_OT_detect_install(bpy.types.Operator):
    bl_idname='ruster.detect_install'
    bl_label='Find Rust Installation'
    bl_description='Check standard Steam paths and secondary Steam libraries'
    def execute(self,context):
        try:
            path=steam.detect_install(require_nonempty=True)
            if path is None:raise ValueError('Rust was not found in your Steam libraries. Choose its installation folder.')
            context.window_manager.ruster_preferences.directory=str(path)
            self.report({'INFO'},'Rust found. Apply the installation to read its prefab catalog.');return {'FINISHED'}
        except (OSError,ValueError,RuntimeError) as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}


class RustPreferences:
    bl_space_type='PREFERENCES'
    bl_region_type='WINDOW'
    bl_options={'HIDE_HEADER'}
    section=''
    @classmethod
    def poll(cls,context):
        state=getattr(context.window_manager,'ruster_preferences',None)
        return bool(state and state.section==cls.section)


class RUSTER_PT_preferences_assets(RustPreferences,CenterAlignMixIn,bpy.types.Panel):
    bl_label='Assets'
    section='RUSTER_ASSETS'
    def draw_centered(self,context,layout):
        width=min(500,max(180,context.region.width/max(context.preferences.system.ui_scale,.5)-180))
        state=context.window_manager.ruster_preferences;project=context.scene.ruster
        layout.label(text='Rust Installation',icon=I['world'])
        fields=properties(layout.column());fields.enabled=not project.busy
        fields.prop(state,'directory')
        row=fields.row(align=True);row.operator_context='EXEC_DEFAULT'
        action(row,'ruster.setup',text='Apply Installation')
        row.operator('ruster.detect_install',icon=I['search'])
        wrapped(layout,context,'Rustmatics checks Steam libraries automatically. Game files stay on your computer and are read-only.',icon='locked',width=width)
        layout.separator()
        layout.label(text='Local Assets',icon=I['import'])
        from .loading import draw_status
        draw_status(layout,context,installation=False)
        layout.separator()
        fields=properties(layout.column());fields.prop(state,'rustedit')
        layout.operator('ruster.catalog_source')
        wrapped(layout,context,'Browsing reads the shipped scene manifest and local RustEdit string pool. Models load only when requested; no full bundle index runs.',width=width)
        layout.separator()
        wrapped(layout,context,'Asset cache: '+str(scene.cache_dir()),width=width)
        wrapped(layout,context,'Rust installation changes are saved when you click Apply Installation.',width=width)


class RUSTER_PT_preferences_mcp(RustPreferences,CenterAlignMixIn,bpy.types.Panel):
    bl_label='MCP'
    section='RUSTER_MCP'
    def draw_centered(self,context,layout):mcp_bridge.draw_settings(layout,context)


def register():
    global _original_navigation
    from .ui import refresh_panels
    for cls in (RUSTER_PG_preferences,RUSTER_OT_catalog_source,RUSTER_OT_preferences,RUSTER_OT_detect_install,RUSTER_PT_preferences_assets,RUSTER_PT_preferences_mcp):
        bpy.utils.register_class(cls);_classes.append(cls)
    bpy.types.WindowManager.ruster_preferences=PointerProperty(type=RUSTER_PG_preferences)
    state=bpy.context.window_manager.ruster_preferences
    state.section=bpy.context.preferences.active_section
    detected_game=steam.resolve_install(settings.load().get('rust_install',''))
    state.directory=str(detected_game) if detected_game else ''
    from .assets.metadata import rustedit_root
    detected_editor=rustedit_root(settings.load().get('rustedit_install'))
    state.rustedit=str(detected_editor) if detected_editor else ''
    _original_navigation=bpy.types.USERPREF_PT_navigation_bar.draw
    bpy.types.USERPREF_PT_navigation_bar.draw=navigation
    # The native editor's active_section enum is defined by Blender's RNA.
    # Mirror its entries and add Rust entries, leaving native sections intact.
    panels=[getattr(bpy.types,name) for name in dir(bpy.types) if name.startswith('USERPREF_PT_')]
    panels=[cls for cls in panels if isinstance(cls,type) and issubclass(cls,bpy.types.Panel) and getattr(cls,'bl_region_type','')=='WINDOW']
    originals=[(cls,cls.__dict__.get('poll'),getattr(cls,'poll',None)) for cls in panels]
    refresh={cls for cls,_own,original in originals if original is None}
    while True:
        ids={getattr(cls,'bl_idname',cls.__name__) or cls.__name__ for cls in refresh}
        children={cls for cls in panels if getattr(cls,'bl_parent_id','') in ids}
        if children<=refresh:break
        refresh.update(children)
    for cls,own,original in originals:
        _polls.append((cls,own))
        def make_poll(original):
            def poll(cls,context):return not is_rust(context) and (original(context) if original else True)
            return poll
        cls.poll=classmethod(make_poll(original))
    _refreshed.extend(cls for cls in panels if cls in refresh);refresh_panels(_refreshed)
    subscribe();bpy.app.handlers.load_post.append(after_load)


def unregister():
    global _original_navigation
    from .ui import refresh_panels
    bpy.msgbus.clear_by_owner(_owner)
    if after_load in bpy.app.handlers.load_post:bpy.app.handlers.load_post.remove(after_load)
    if _original_navigation:
        bpy.types.USERPREF_PT_navigation_bar.draw=_original_navigation;_original_navigation=None
    for cls,own in _polls:
        if own is None:del cls.poll
        else:cls.poll=own
    _polls.clear();refresh_panels(_refreshed);_refreshed.clear()
    if hasattr(bpy.types.WindowManager,'ruster_preferences'):del bpy.types.WindowManager.ruster_preferences
    for cls in reversed(_classes):bpy.utils.unregister_class(cls)
    _classes.clear()
