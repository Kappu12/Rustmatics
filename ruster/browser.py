"""Native prefab catalog filters and persistent favorite palettes."""
import zlib
import sqlite3
import bpy
from bpy.props import StringProperty,EnumProperty
from . import scene
from .assets import catalog
from .core import settings,palettes

_classes=[]
PAGE_SIZE=12
_category_key=None
_category_history=[]
_category_items=[('ALL','All Categories','',0)]
_page_cache={}


def prefab_page(state):
    """UI redraws reuse a page until its filters or the catalog actually change."""
    path=scene.cache_dir()/'catalog.sqlite3'
    stat=path.stat() if path.exists() else None
    config=settings.load()
    favorites=tuple(palettes.favorite_ids(config)) if state.favorites_only else None
    recent=tuple(palettes.recent_ids(config)) if state.recent_only else None
    key=(str(path),stat.st_mtime_ns if stat else 0,stat.st_size if stat else 0,
         state.query,state.query_page,state.category,favorites,recent,state.available_only,state.include_helpers)
    if key not in _page_cache:
        allowed=favorites
        if recent is not None:allowed=tuple(uid for uid in recent if favorites is None or uid in favorites)
        result=catalog.page(scene.cache_dir(),state.query,page_size=palettes.MAX_RECENT if recent is not None else PAGE_SIZE,
            page_index=0 if recent is not None else state.query_page,category='' if state.category=='ALL' else state.category,
            favorites=allowed,available_only=state.available_only,include_helpers=state.include_helpers)
        if recent is not None:
            ranked=sorted(result['rows'],key=lambda row:recent.index(row[0]))
            pages=(len(ranked)+PAGE_SIZE-1)//PAGE_SIZE
            page=min(state.query_page,max(0,pages-1))
            result={**result,'rows':ranked[page*PAGE_SIZE:(page+1)*PAGE_SIZE],'page':page,'pages':pages}
        _page_cache[key]=result
        while len(_page_cache)>16:del _page_cache[next(iter(_page_cache))]
    return _page_cache[key]


def prefab_description(uid,action):
    try:
        found=catalog.find(scene.cache_dir(),int(uid))
        if found:return f'{action}\n{found[0]}\nRust prefab ID: {uid}'+('' if found[1] else '\nNot found in local Rust bundles')
    except (ValueError,OSError,sqlite3.Error):pass
    return action


def category_items(_state,_context):
    global _category_key,_category_items
    path=scene.cache_dir()/'catalog.sqlite3'
    key=(str(path),path.stat().st_mtime_ns if path.exists() else 0)
    if key!=_category_key:
        try:
            from .core.asset_categories import GROUPS
            items=[('ALL','All Categories','',0)]+[('@'+key,label,'',index+1)
                for index,(key,label,_patterns) in enumerate(GROUPS)]
            if items!=_category_items:
                _category_history.append(_category_items)  # RNA may retain callback string references.
                _category_items=items
            _category_key=key
        except (OSError,ValueError,sqlite3.Error):pass
    return _category_items


class RUSTER_OT_favorite(bpy.types.Operator):
    bl_idname='ruster.favorite'
    bl_label='Toggle Favorite Prefab'
    bl_description='Add or remove this prefab from your local favorite palette'
    uid:StringProperty()
    def execute(self,context):
        try:
            settings.save(palettes.toggle(settings.load(),self.uid))
            context.scene.ruster.query_page=0
            for window in context.window_manager.windows:
                for area in window.screen.areas:area.tag_redraw()
            return {'FINISHED'}
        except (ValueError,OSError) as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}


class RUSTER_OT_clear_filters(bpy.types.Operator):
    bl_idname='ruster.clear_prefab_filters'
    bl_label='Reset Prefab Filters'
    bl_description='Clear search, category, favorites and recents; show all locally available assets'
    def execute(self,context):
        state=context.scene.ruster
        state.query='';state.category='ALL';state.favorites_only=False;state.recent_only=False
        state.available_only=True;state.include_helpers=True;state.query_page=0
        return {'FINISHED'}


class RUSTER_OT_prefab_details(bpy.types.Operator):
    bl_idname='ruster.prefab_details'
    bl_label='Inspect Prefab'
    bl_description='Show the full asset path, ID, availability and cached model status without placing it'
    uid:StringProperty()
    def execute(self,context):
        try:
            uid=int(self.uid)
            found=catalog.find(scene.cache_dir(),uid)
            if not found:raise ValueError('Prefab is no longer in the local catalog')
        except (ValueError,OSError,sqlite3.Error) as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}
        from .core.asset_names import display_name
        from .ui_style import ICONS as I,wrapped
        name=display_name(found[0])
        cached=(scene.cache_dir()/'prefabs'/f'{uid}.json.gz').is_file()
        from . import thumbnails
        thumbnails.request(uid)
        def draw(popup,draw_context):
            layout=popup.layout
            preview = thumbnails.icon_id(uid, found[0])
            if preview:
                layout.template_icon(icon_value=preview, scale=1.5)
            elif found[1]:
                layout.operator('ruster.load_thumbnail',text='Load Thumbnail',icon='IMAGE_DATA').uid=str(uid)
            wrapped(layout,draw_context,name,icon='prefab',width=400)
            layout.label(text=f'Prefab ID: {uid}')
            wrapped(layout,draw_context,found[0],width=400)
            layout.separator()
            wrapped(layout,draw_context,'Available in local Rust bundles' if found[1] else
                'Not found in local Rust bundles. Check your Rust installation or refresh the catalog.',
                icon='info' if found[1] else 'error',width=400)
            layout.label(text='Conversion cache available' if cached else 'Model loads on demand',icon=I['import'])
            from .core.asset_names import effect_path
            if effect_path(found[0]):
                wrapped(layout,draw_context,'Rust effect asset: may contain runtime particles or audio instead of a static building model.',icon='info',width=400)
            row=layout.row(align=True)
            row.operator('ruster.copy_prefab',text='Copy ID',icon=I['duplicate']).uid=str(uid)
            copy=row.operator('ruster.copy_prefab',text='Copy Path',icon=I['duplicate'])
            copy.uid=str(uid);copy.value='PATH'
            row=layout.row();row.enabled=bool(found[1])
            row.operator('ruster.place',text='Place at 3D Cursor',icon=I['prefab']).uid=str(uid)
        context.window_manager.popup_menu(draw,title='Prefab Details')
        return {'FINISHED'}


class RUSTER_OT_load_thumbnail(bpy.types.Operator):
    bl_idname='ruster.load_thumbnail'
    bl_label='Load Prefab Thumbnail'
    bl_description='Load this model to create a browser preview without placing it'
    uid:StringProperty()

    def execute(self,context):
        from . import app, thumbnails
        try:
            uid=int(self.uid)
        except ValueError:
            self.report({'ERROR'},'Invalid prefab ID')
            return {'CANCELLED'}
        if (scene.cache_dir()/'prefabs'/f'{uid}.json.gz').is_file():
            thumbnails.converted(uid)
        elif not app.request_assets([uid]):
            self.report({'ERROR'},context.scene.ruster.asset_status)
            return {'CANCELLED'}
        self.report({'INFO'},'Requested prefab thumbnail')
        return {'FINISHED'}


class RUSTER_OT_paint_asset(bpy.types.Operator):
    bl_idname='ruster.paint_asset'
    bl_label='Paint this Prefab'
    bl_description='Choose this prefab for terrain placement painting'
    uid:StringProperty()
    @classmethod
    def description(cls,context,properties):return prefab_description(properties.uid,'Paint this prefab on terrain')
    def execute(self,context):
        try:
            found=catalog.find(scene.cache_dir(),int(self.uid))
            if not found or not found[1]:raise ValueError('Choose a prefab available in your local Rust bundles')
        except (ValueError,OSError,sqlite3.Error) as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}
        brush=context.scene.ruster_paint
        brush.uid=self.uid;brush.palette='SINGLE'
        bpy.ops.wm.tool_set_by_id(name='ruster.prefab_brush')
        return {'FINISHED'}


class RUSTER_OT_copy_prefab(bpy.types.Operator):
    bl_idname='ruster.copy_prefab'
    bl_label='Copy Prefab ID'
    uid:StringProperty()
    value:EnumProperty(items=[('ID','Prefab ID',''),('PATH','Asset Path','')],default='ID',options={'SKIP_SAVE'})
    @classmethod
    def description(cls,context,properties):return prefab_description(properties.uid,'Copy this asset path' if properties.value=='PATH' else 'Copy this Rust prefab ID')
    def execute(self,context):
        try:
            found=catalog.find(scene.cache_dir(),int(self.uid))
            if not found:raise ValueError('Prefab is no longer in the local catalog')
            context.window_manager.clipboard=found[0] if self.value=='PATH' else self.uid
            self.report({'INFO'},'Asset path copied' if self.value=='PATH' else 'Prefab ID copied');return {'FINISHED'}
        except (ValueError,OSError,sqlite3.Error) as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}


def register():
    for cls in (RUSTER_OT_favorite,RUSTER_OT_copy_prefab,RUSTER_OT_clear_filters,RUSTER_OT_prefab_details,RUSTER_OT_load_thumbnail):
        bpy.utils.register_class(cls);_classes.append(cls)


def unregister():
    global _category_key
    _category_key=None
    _page_cache.clear()
    for cls in reversed(_classes):bpy.utils.unregister_class(cls)
    _classes.clear()
