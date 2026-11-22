"""Native Blender panels and menus, sharing Blender's icon and theme vocabulary."""
import bpy
from . import scene,browser
from .assets import catalog
from .ui_style import ICONS as I,action,properties,section,wrapped,tool_button,duplicate_button

_classes=[]
_draws=[]
_polls=[]
_refreshed_panels=[]
_preview_splits={}


def is_map(context):
    # World editor source remains dormant while the product edits prefabs.
    return False


class MapPanel:
    bl_space_type='VIEW_3D'
    bl_region_type='UI'
    bl_category='Rustmatics'
    icon='world'
    @classmethod
    def poll(cls,context):return is_map(context)
    def draw_header(self,context):self.layout.label(text='',icon=I[self.icon])


class RUSTER_PT_start(bpy.types.Panel):
    bl_label='Prefab Studio'
    bl_space_type='VIEW_3D'
    bl_region_type='UI'
    bl_category='Rustmatics'

    @classmethod
    def poll(cls,context):return not is_map(context)

    def draw(self,context):
        from . import __version__
        layout=self.layout
        from . import branding
        branding.label(layout,text=f'Rustmatics {__version__}',context=context)
        wrapped(layout,context,context.scene.ruster.title,icon='prefab')
        from .quitting import save_status
        layout.label(text=save_status(),icon=I['save'])
        layout.label(text=f'{len(scene.prefab_roots()):,} prefab placements')
        layout.menu('RUSTER_MT_add',text='Add Rust Prefab · Shift+A',icon=I['prefab'])
        row=layout.row(align=True)
        action(row,'ruster.save',text='Save');action(row,'ruster.prefab_export',text='Export Prefab…')
        action(layout,'ruster.monument_export')
        row=layout.row(align=True)
        action(row,'ruster.prefab_import',text='Import Prefab…');action(row,'ruster.model_export',text='Export Models…')
        action(layout,'ruster.blend_export')
        if not context.scene.ruster.native_ground:
            layout.operator('ruster.add_ground',icon='MESH_PLANE')


class RUSTER_MT_file(bpy.types.Menu):
    bl_label='Rustmatics'
    def draw(self,context):
        layout=self.layout
        for op in ('ruster.welcome','ruster.new','ruster.open'):action(layout,op)
        layout.separator()
        action(layout,'ruster.save')
        action(layout,'ruster.save',text='Save Project As…',icon='save_as').save_as=True
        action(layout,'ruster.import_map')
        action(layout,'ruster.export_map')
        layout.operator('ruster.add_ground',icon='MESH_PLANE')
        layout.separator()
        for op in ('ruster.prefab_import','ruster.prefab_export','ruster.monument_export',
                   'ruster.blend_export','ruster.model_export'):action(layout,op)
        layout.separator()
        action(layout,'ruster.setup')
        layout.separator()
        action(layout,'ruster.quit')


class RUSTER_MT_edit(bpy.types.Menu):
    bl_label='Edit'
    def draw(self,context):
        layout=self.layout
        action(layout,'ed.undo');action(layout,'ed.redo')
        layout.separator()
        duplicate_button(layout,context);action(layout,'ruster.delete')
        layout.separator()
        action(layout,'ruster.group');action(layout,'ruster.ungroup')
        if is_map(context):action(layout,'ruster.surface_snap')
        layout.separator()
        action(layout,'ruster.preferences')


class RUSTER_MT_map(bpy.types.Menu):
    bl_label='Map'
    def draw(self,context):
        layout=self.layout
        action(layout,'ruster.new_map')
        action(layout,'ruster.generate_map')
        layout.operator_menu_enum('ruster.path_add','kind',text='Add Path',icon=I['path'])
        layout.separator()
        action(layout,'ruster.height_import');action(layout,'ruster.height_export')
        action(layout,'ruster.mask_import');action(layout,'ruster.mask_export')
        layout.separator()
        action(layout,'ruster.validate');action(layout,'ruster.export_map')


class RUSTER_MT_assets(bpy.types.Menu):
    bl_label='Assets'
    def draw(self,context):
        layout=self.layout
        action(layout,'ruster.assets')
        action(layout,'ruster.assets',text='Refresh Asset Catalog',icon='refresh').rebuild=True
        action(layout,'ruster.asset_report')
        layout.separator()
        action(layout,'ruster.setup')


class RUSTER_PT_map(MapPanel,bpy.types.Panel):
    bl_label='Map'
    def draw(self,context):
        state=context.scene.ruster;layout=self.layout
        wrapped(layout,context,state.title,icon='world')
        row=layout.row()
        row.label(text=f'{state.map_size:,} m')
        row.label(text=f'{len(scene.prefab_roots()):,} prefabs',icon=I['prefab'])
        layout.label(text='F3: Rustmatics Commands',icon=I['info'])
        layout.separator()
        action(layout,'ruster.save')
        row=layout.row(align=True)
        action(row,'ruster.validate');action(row,'ruster.export_map',text='Export Map…')


class RUSTER_PT_prefabs(MapPanel,bpy.types.Panel):
    bl_label='Prefab Browser'
    icon='prefab'
    @classmethod
    def poll(cls,context):return True
    def draw(self,context):
        layout=self.layout;state=context.scene.ruster
        layout.prop(state,'query',text='',icon=I['search'])
        layout.prop(state,'category',text='')
        row=layout.row(align=True)
        row.prop(state,'favorites_only',text='Favorites',toggle=True,
            icon=I['favorite_on' if state.favorites_only else 'favorite_off'])
        row.prop(state,'recent_only',text='Recent',toggle=True,icon=I['recover'])
        row=layout.row(align=True)
        row.prop(state,'available_only',text='Available Locally',toggle=True,icon=I['import'])
        row.prop(state,'include_helpers',text='Hidden Assets',toggle=True)
        layout.operator('ruster.clear_prefab_filters',text='Reset Filters',icon=I['refresh'])
        from .core import settings,palettes
        favorites=set(palettes.favorite_ids(settings.load()))
        try:
            result=browser.prefab_page(state)
            matches=result['rows']
        except Exception as error:
            wrapped(layout,context,'Could not read the asset catalog: '+str(error),icon='error')
            action(layout,'ruster.asset_report');return
        layout.label(text=f"{result['total']:,} matching prefabs",icon=I['prefab'])
        if not matches:
            if not (scene.cache_dir()/'catalog.sqlite3').is_file():
                wrapped(layout,context,'Load your Rust assets to browse prefabs.',icon='info')
                action(layout,'ruster.assets')
            elif state.recent_only and not palettes.recent_ids(settings.load()):
                wrapped(layout,context,'No recently used prefabs yet. Placed prefabs appear here automatically.',icon='info')
            elif state.favorites_only and not favorites:
                wrapped(layout,context,'No favorites yet. Use the star beside a prefab to save it here.',icon='info')
            elif state.query or state.category!='ALL' or state.favorites_only or state.recent_only or state.available_only:
                wrapped(layout,context,'No matching prefabs. Try different words, enable Hidden Assets or reset the filters.',icon='info')
            else:
                wrapped(layout,context,'The catalog is empty. Refresh your Rust assets.',icon='info')
                action(layout,'ruster.assets',text='Refresh Asset Catalog',icon='refresh').rebuild=True
        for uid,path,available in matches:
            row=layout.row(align=True)
            row.operator('ruster.favorite',text='',icon=I['favorite_on' if uid in favorites else 'favorite_off'],emboss=False).uid=str(uid)
            body=row.row(align=True);body.enabled=bool(available)
            from .core.asset_names import display_name
            body.operator('ruster.place',text=display_name(path),icon=I['prefab']).uid=str(uid)
            if is_map(context):body.operator('ruster.paint_asset',text='',icon=I['prefab_brush']).uid=str(uid)
            row.operator('ruster.prefab_details',text='',icon=I['info']).uid=str(uid)
        if matches:
            row=layout.row(align=True)
            previous=row.row(align=True);previous.enabled=result['page']>0
            previous.operator('ruster.catalog_page',text='',icon=I['previous']).delta=-1
            row.label(text=f"Page {result['page']+1} of {result['pages']}")
            next_=row.row(align=True);next_.enabled=result['page']+1<result['pages']
            next_.operator('ruster.catalog_page',text='',icon=I['next']).delta=1
        wrapped(layout,context,'Click a prefab to place it at the 3D cursor. Move, rotate and scale the whole placement in the viewport.')


class RUSTER_PT_selection(MapPanel,bpy.types.Panel):
    bl_label='Placement'
    icon='prefab'
    @classmethod
    def poll(cls,context):return True
    def draw(self,context):
        layout=self.layout;obj=context.active_object
        if not obj or obj.get('ruster_kind') not in {'PREFAB','GROUP'}:
            wrapped(layout,context,'Select a prefab or group in the viewport or Outliner.',icon='info');return
        wrapped(layout,context,obj.name,icon='group' if obj.get('ruster_kind')=='GROUP' else 'prefab')
        if obj.get('ruster_kind')=='PREFAB':
            layout.label(text='Prefab ID: '+obj.get('ruster_uid','?'))
            if obj.get('ruster_spawner_preview'):
                wrapped(layout,context,'Spawner preview: move and rotate the crate to place its spawner. Rust controls runtime spawn variations.',icon='info')
            if obj.get('ruster_has_io'):
                action(layout,'ruster.io_settings')
            from .protection import model_error
            if obj.get('ruster_marker')=='SOUND':
                wrapped(layout,context,'Sound effect — no visible mesh in Rust.',icon='info')
            elif obj.get('ruster_marker'):
                wrapped(layout,context,obj.get('ruster_model_status','Rust helper — no static model'),icon='info')
            elif not obj.get('ruster_visual'):
                wrapped(layout,context,obj.get('ruster_model_status','Model not loaded'),icon='info')
                if obj.get('ruster_model_error'):
                    wrapped(layout,context,obj['ruster_model_error'],icon='error')
                action(layout,'ruster.assets',text='Load / Retry Model')
            elif model_error(obj):
                wrapped(layout,context,'Restore this prefab model before exporting.',icon='error')
                action(layout,'ruster.restore_models')
            else:layout.label(text='Original model is read-only',icon=I['locked'])
            if obj.get('ruster_visual') and obj.get('ruster_model_error'):
                wrapped(layout,context,obj['ruster_model_error'],icon='error')
                action(layout,'ruster.assets',text='Retry Model / Materials')
        from .construction import symmetry_members
        if obj.get('ruster_symmetry_source') or symmetry_members([obj]):
            message=('Editing or deleting this member affects its symmetry set.'
                     if context.scene.ruster_construction.symmetry else 'Symmetry is off; this member can be edited independently.')
            wrapped(layout,context,message,icon='info')
            layout.operator('ruster.symmetry_original',icon=I['select'])
            layout.operator('ruster.symmetry_select',icon=I['select'])
            layout.operator('ruster.symmetry_release',icon=I['ungroup'])
        fields=properties(layout.column())
        fields.prop(obj,'location')
        fields.prop(obj,'rotation_mode',text='Rotation Mode')
        rotation='rotation_quaternion' if obj.rotation_mode=='QUATERNION' else ('rotation_axis_angle' if obj.rotation_mode=='AXIS_ANGLE' else 'rotation_euler')
        fields.prop(obj,rotation,text='Rotation')
        fields.prop(obj,'scale')
        row=layout.row(align=True)
        action(row,'ruster.duplicate_move',text='Duplicate');action(row,'ruster.delete',text='Delete')
        if is_map(context):action(layout,'ruster.surface_snap')
        body=section(layout,'ruster_placement_more','Placement Tools','settings')
        if body is not None:
            action(body,'ruster.select_same');action(body,'ruster.replace')
            if is_map(context):action(body,'ruster.scatter')
            row=body.row(align=True)
            action(row,'ruster.group',text='Group');action(row,'ruster.ungroup',text='Ungroup')
            action(body,'ruster.prefab_import');action(body,'ruster.prefab_export')
            action(body,'ruster.restore_models')


class RUSTER_PT_assets(MapPanel,bpy.types.Panel):
    bl_label='Model Loading'
    bl_options={'DEFAULT_CLOSED'}
    icon='import'
    @classmethod
    def poll(cls,context):return True
    def draw(self,context):
        from .loading import draw_status
        draw_status(self.layout,context)


class RUSTER_PT_asset_status(bpy.types.Panel):
    bl_label='Model Loading'
    bl_space_type='TOPBAR'
    bl_region_type='HEADER'
    bl_ui_units_x=24
    def draw(self,context):
        from .loading import draw_status
        draw_status(self.layout,context)


class RUSTER_PT_properties(bpy.types.Panel):
    bl_label='Rust Map'
    bl_space_type='PROPERTIES'
    bl_region_type='WINDOW'
    bl_context='scene'
    @classmethod
    def poll(cls,context):return is_map(context)
    def draw_header(self,context):self.layout.label(text='',icon=I['world'])
    def draw(self,context):
        layout=self.layout;state=context.scene.ruster
        wrapped(layout,context,state.title,icon='world')
        layout.label(text=f'{state.map_size:,} m')
        layout.separator()
        action(layout,'ruster.validate');action(layout,'ruster.export_map')
        wrapped(layout,context,'Edit terrain, paths and prefab placements in the Rustmatics sidebar.')


class RUSTER_PT_terrain(MapPanel,bpy.types.Panel):
    bl_label='Terrain'
    icon='terrain'
    def draw(self,context):
        layout=self.layout;brush=context.scene.ruster_brush
        fields=properties(layout.column())
        fields.prop(brush,'layer')
        if brush.layer in {'terrain','water'}:
            fields.prop(brush,'tool')
            if brush.tool=='FLATTEN':fields.prop(brush,'height')
            if brush.tool=='TERRACE':fields.prop(brush,'terrace')
            if brush.tool=='NOISE':fields.prop(brush,'seed')
            if brush.tool=='CLONE':action(layout,'ruster.clone_source')
            if brush.tool in {'STAMP','SLOPE'}:
                fields.prop(brush,'height');fields.prop(brush,'rotation')
            if brush.tool=='STAMP':fields.prop(brush,'stamp_path')
            if brush.tool=='SLOPE':fields.prop(brush,'slope')
        elif brush.layer in {'splat','biome','topology'}:fields.prop(brush,brush.layer)
        if brush.layer in {'topology','alpha','splat','biome'}:
            fields.prop(brush,'remove',text={'alpha':'Cut Holes','topology':'Remove Topology'}.get(brush.layer,'Erase Layer'))
        for prop in ('radius','strength','falloff','shape'):fields.prop(brush,prop)
        controls=layout.column();controls.enabled=context.mode=='OBJECT'
        tool_button(controls,context,'ruster.terrain_brush','Paint Terrain in Viewport','terrain_brush').name='ruster.terrain_brush'
        action(controls,'ruster.terrain_apply')
        wrapped(layout,context,'Alt: invert brush. Shift: smooth. Terrain holes affect terrain only.')
        body=section(layout,'ruster_terrain_masks','Height and Slope Limits','settings')
        if body is not None:
            body.prop(brush,'use_mask',text='Enable Limits')
            limits=body.column();limits.enabled=brush.use_mask
            for prop in ('minimum','maximum','min_slope','max_slope'):limits.prop(brush,prop)
        body=section(layout,'ruster_terrain_images','Heightmaps and Generation','terrain')
        if body is not None:
            action(body,'ruster.height_import');action(body,'ruster.height_export')
            action(body,'ruster.terrain_bridge',text='Set Bridge Start',icon='cursor').set_start=True
            action(body,'ruster.terrain_bridge')
            action(body,'ruster.generate_island')
        body=section(layout,'ruster_layer_masks','Layer Image Masks','splat')
        if body is not None:
            controls=body.column();controls.enabled=context.mode=='OBJECT'
            from .layers import preset_from_brush
            for operator in ('ruster.mask_import','ruster.mask_export'):
                op=action(controls,operator)
                preset_from_brush(op,context)
            wrapped(body,context,'Edit grayscale masks outside Rustmatics. Import changes one channel at full resolution.')
        body=section(layout,'ruster_terrain_preview','Viewport Preview','overlay')
        if body is not None:
            body.prop(brush,'display',text='Overlay')
            action(body,'ruster.terrain_overlay')
            body.prop(context.scene.ruster,'preview_resolution')
            action(body,'ruster.terrain_detail')


class RUSTER_PT_paint(MapPanel,bpy.types.Panel):
    bl_label='Prefab Paint'
    bl_options={'DEFAULT_CLOSED'}
    icon='prefab_brush'
    def draw(self,context):
        layout=self.layout;brush=context.scene.ruster_paint
        fields=properties(layout.column())
        fields.prop(brush,'palette')
        if brush.palette=='SINGLE':fields.prop(brush,'uid')
        fields.prop(brush,'spacing')
        controls=layout.column();controls.enabled=context.mode=='OBJECT'
        tool_button(controls,context,'ruster.prefab_brush','Paint Prefabs in Viewport','prefab_brush').name='ruster.prefab_brush'
        wrapped(layout,context,'Drag to place. Esc cancels the whole stroke. Spacing uses placement origins.')
        body=section(layout,'ruster_paint_random','Randomization','scatter')
        if body is not None:
            for prop in ('random_yaw','min_scale','max_scale','seed'):body.prop(brush,prop)
        body=section(layout,'ruster_paint_surface','Terrain Placement','snap')
        if body is not None:
            for prop in ('align','offset','max_slope','respect_holes'):body.prop(brush,prop)
        layout.label(text='Original models stay read-only',icon=I['locked'])


class RUSTER_PT_paths(MapPanel,bpy.types.Panel):
    bl_label='Paths'
    icon='path'
    def draw(self,context):
        layout=self.layout
        layout.operator_menu_enum('ruster.path_add','kind',text='Add Path',icon=I['path'])
        obj=context.object
        if not obj or obj.get('ruster_kind')!='PATH':
            wrapped(layout,context,'Select a path to edit its nodes and settings.',icon='info');return
        fields=properties(layout.column())
        fields.prop(obj,'name',text='Name')
        editing=obj.mode=='EDIT'
        layout.operator('object.mode_set',text='Finish Editing Nodes' if editing else 'Edit Path Nodes',
            icon=I['finish_path' if editing else 'edit_path']).mode='OBJECT' if editing else 'EDIT'
        wrapped(layout,context,'G: move nodes. E: extend. X: delete.')
        fields.prop(obj.ruster_path,'kind')
        fields.prop(obj.ruster_path,'width')
        controls=layout.column();controls.enabled=not editing
        action(controls,'ruster.path_snap');action(controls,'ruster.path_grade')
        body=section(layout,'ruster_path_shape','Shape and Grading','path')
        if body is not None:
            for prop in ('inner_padding','outer_padding','inner_fade','outer_fade','terrain_offset','mesh_offset','random_scale'):
                body.prop(obj.ruster_path,prop)
        body=section(layout,'ruster_path_flags','Masks and Connections','settings')
        if body is not None:
            for prop in ('splat','topology','spline','start','end'):body.prop(obj.ruster_path,prop)


def patch_draw(cls,replacement):
    original=cls.draw;_draws.append((cls,original))
    def draw(self,context):
        replacement(self,context)
    cls.draw=draw


def menus(self,context):
    for menu in ('RUSTER_MT_file','RUSTER_MT_edit','RUSTER_MT_assets'):self.layout.menu(menu)
    if is_map(context):self.layout.menu('RUSTER_MT_map')


def add_menu(self,context):
    from .studio import RUSTER_MT_add
    RUSTER_MT_add.draw(self,context)


def view_header(self,context):
    layout=self.layout
    layout.menu('VIEW3D_MT_view',text='View')
    layout.menu('VIEW3D_MT_select_edit_curve' if context.mode=='EDIT_CURVE' else 'VIEW3D_MT_select_object',text='Select')
    layout.menu('RUSTER_MT_add',text='Add Prefab')
    layout.menu('RUSTER_MT_object',text='Object')
    if context.mode=='EDIT_CURVE':
        layout.operator('object.mode_set',text='Finish Path',icon=I['finish_path']).mode='OBJECT'
    # Keep Blender's menus / centered transforms / right viewport controls.
    # Active tools already have their native toolbar and tool-settings header.
    layout.separator_spacer()
    layout.prop_with_popover(context.scene.transform_orientation_slots[0], 'type', text='',
                             panel='VIEW3D_PT_transform_orientations')
    layout.prop(context.scene.tool_settings, 'transform_pivot_point', text='', icon_only=True)
    construction=context.scene.ruster_construction
    layout.prop(construction,'object_snap',text='',icon=I['snapping'])
    layout.popover(panel='RUSTER_PT_snapping',text='Snapping')
    layout.separator_spacer()
    space=context.space_data
    view_controls=layout.row(align=True)
    action(view_controls,'view3d.view_persportho',text='',depress=space.region_3d.view_perspective=='PERSP')
    view_controls.prop(space.overlay,'show_overlays',text='',icon=I['overlay'],toggle=True)
    view_controls.prop(context.scene,'ruster_show_volumes',text='',icon=I['volume'],toggle=True)
    layout.prop(space.shading,'type',text='',expand=True)


def context_menu(self,context):
    for op in ('transform.translate','transform.rotate','transform.resize'):action(self.layout,op)
    self.layout.separator()
    duplicate_button(self.layout,context);action(self.layout,'ruster.delete')
    self.layout.separator()
    for op in ('ruster.group','ruster.ungroup'):action(self.layout,op)
    self.layout.menu('RUSTER_MT_layout');self.layout.menu('RUSTER_MT_symmetry')
    self.layout.separator()
    action(self.layout,'ruster.restore_models')


class RUSTER_MT_object(bpy.types.Menu):
    bl_label='Object'
    def draw(self,context):context_menu(self,context)


def readonly_source_menu(self,context):
    self.layout.label(text='Original models are read-only',icon=I['locked'])
    action(self.layout,'ruster.restore_models')


def properties_header(self,context):self.layout.label(text='Live Preview',icon=I['play'])


def outliner_header(self,context):
    layout=self.layout
    layout.label(text='Placements',icon=I['group'])
    layout.separator_spacer()
    layout.prop(context.space_data,'filter_text',text='',icon=I['search'])
    layout.prop(context.space_data,'show_restrict_column_select',text='',icon=I['select'])


def topbar(self,context):
    layout=self.layout
    if context.region.alignment=='RIGHT':
        # Blender only moves reports here when its bottom status bar is hidden.
        if not context.screen.show_statusbar:
            layout.template_reports_banner();layout.template_running_jobs()
        from .loading import cached_model_summary,header_label
        counts=cached_model_summary()
        layout.popover(panel='RUSTER_PT_asset_status',text=header_label(context.scene.ruster,counts),
            icon=I['busy' if context.scene.ruster.busy else 'error' if counts['ISSUE'] else 'import'])
        from .quitting import save_status
        layout.label(text=context.scene.ruster.title+' · '+save_status(),icon=I['project'])
    else:
        from . import branding
        branding.label(layout,text='Prefab Editor',context=context)
        layout.separator()
        bpy.types.TOPBAR_MT_editor_menus.draw_collapsible(context,layout)


def close_map_timeline():
    from . import editing,painting
    if editing.stroke_active or painting.stroke_active:return .5
    for window in bpy.context.window_manager.windows:
        if not ensure_preview_area(window):return .2
    # Joining areas rebuilds region state. Focus the sidebar on a later tick.
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type=='DOPESHEET_EDITOR' and area.ui_type=='TIMELINE':
                with bpy.context.temp_override(window=window,area=area):bpy.ops.screen.area_close()
                return .2
    retry=False
    for window in bpy.context.window_manager.windows:
        for area in list(window.screen.areas):
            if area.type=='VIEW_3D':
                area.spaces.active.show_region_toolbar=True
                area.spaces.active.show_region_ui=True
                region=next((r for r in area.regions if r.type=='UI'),None)
                if region and region.active_panel_category!='Rustmatics':
                    try:region.active_panel_category='Rustmatics';area.tag_redraw()
                    except RuntimeError:pass  # Category list needs its first draw.
                    retry=True
    return .2 if retry else None


def ensure_preview_area(window):
    """Keep preview in a native editor below Placements, including old projects."""
    areas = list(window.screen.areas)
    key=window.as_pointer()
    pending=_preview_splits.get(key)
    if pending:
        candidates=[area for area in areas if area.as_pointer() in pending]
        if len(candidates)==2 and all(area.height>1 for area in candidates):
            min(candidates,key=lambda area:area.y).type='PROPERTIES'
            _preview_splits.pop(key,None)
        elif len(candidates)!=2:_preview_splits.pop(key,None)
        else:return False
    if not any(area.type=='PROPERTIES' for area in window.screen.areas):
        outliners = [area for area in areas if area.type=='OUTLINER']
        if outliners:
            outliner = max(outliners, key=lambda area: (area.x, area.height))
            if outliner.height >= 120:
                previous={area.as_pointer() for area in areas}
                with bpy.context.temp_override(window=window, area=outliner):
                    bpy.ops.screen.area_split(direction='HORIZONTAL', factor=.72)
                created=[area.as_pointer() for area in window.screen.areas if area.as_pointer() not in previous]
                if created:
                    # Area rectangles become valid on the next UI tick. Match
                    # identities first; geometry immediately after split is stale.
                    _preview_splits[key]={outliner.as_pointer(),created[0]}
                    return False
    for area in window.screen.areas:
        if area.type=='PROPERTIES':
            area.spaces.active.context='SCENE'
            area.spaces.active.pin_id=window.scene
            area.spaces.active.use_pin_id=True
            navigation=next((region for region in area.regions if region.type=='NAVIGATION_BAR'),None)
            if navigation and navigation.width>1:
                with bpy.context.temp_override(window=window,area=area):
                    bpy.ops.screen.region_toggle(region_type='NAVIGATION_BAR')
    return True


def start_workspace():
    for window in bpy.context.window_manager.windows:
        with bpy.context.temp_override(window=window):
            ensure_preview_area(window)
            if not is_map(bpy.context):
                window.workspace.name='Prefab Studio'
                window.workspace['ruster_workspace']='PREFAB'
            for area in window.screen.areas:
                with bpy.context.temp_override(area=area):
                    if area.type=='IMAGE_EDITOR' and not is_map(bpy.context):area.type='VIEW_3D'
                    if area.type=='VIEW_3D':
                        area.spaces.active.show_region_ui=True
                        area.spaces.active.show_region_toolbar=True
                        area.spaces.active.show_gizmo=True
                        area.spaces.active.overlay.show_relationship_lines=False
                    area.tag_redraw()
    if not bpy.app.background and not bpy.app.timers.is_registered(close_map_timeline):
        bpy.app.timers.register(close_map_timeline,first_interval=.1)


def splash(self,context):
    from .app import RUSTER_OT_welcome
    RUSTER_OT_welcome.draw(self,context)


class RUSTER_PT_welcome(bpy.types.Panel):
    bl_label = 'Rustmatics'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'HIDE_HEADER'}
    bl_ui_units_x = 30

    def draw(self, context):
        splash(self, context)


def refresh_panels(classes):
    """Refresh RNA callbacks, unregistering children before their parents.

    Blender doesn't install a previously absent poll callback on assignment.
    Panels that had no poll at registration need registration refreshed once.
    Include their children so Blender's parent panel pointers remain valid.
    """
    remaining=list(classes);ordered=[]
    while remaining:
        ids={getattr(cls,'bl_idname',cls.__name__) or cls.__name__ for cls in remaining}
        ready=[cls for cls in remaining if getattr(cls,'bl_parent_id','') not in ids]
        if not ready:raise RuntimeError('Cyclic Blender panel parent IDs')
        for cls in ready:remaining.remove(cls);ordered.append(cls)
    for cls in reversed(ordered):bpy.utils.unregister_class(cls)
    for cls in ordered:bpy.utils.register_class(cls)


def map_workspace():
    for window in bpy.context.window_manager.windows:
        if window.workspace.get('ruster_workspace')!='MAPS':
            window.workspace.name='Rust Map';window.workspace['ruster_workspace']='3D'
        for area in window.screen.areas:
            if area.type=='PROPERTIES':area.spaces.active.context='SCENE'
            area.tag_redraw()
    from . import maps
    maps.ensure_workspace()
    if not bpy.app.background and not bpy.app.timers.is_registered(close_map_timeline):
        bpy.app.timers.register(close_map_timeline,first_interval=.1)


def register():
    from .live_preview import RUSTER_PT_live_preview
    classes=[RUSTER_MT_file,RUSTER_MT_edit,RUSTER_MT_assets,RUSTER_MT_object,RUSTER_PT_welcome,
             RUSTER_PT_start,RUSTER_PT_prefabs,RUSTER_PT_selection,RUSTER_PT_live_preview,RUSTER_PT_assets,RUSTER_PT_asset_status]
    for cls in classes:bpy.utils.register_class(cls);_classes.append(cls)
    for cls in (bpy.types.WM_MT_splash,bpy.types.WM_MT_splash_quick_setup):
        _draws.append((cls,cls.draw));cls.draw=splash
    patch_draw(bpy.types.TOPBAR_HT_upper_bar,topbar)
    patch_draw(bpy.types.TOPBAR_MT_editor_menus,menus)
    patch_draw(bpy.types.VIEW3D_HT_header,view_header)
    patch_draw(bpy.types.VIEW3D_MT_add,add_menu)
    patch_draw(bpy.types.VIEW3D_MT_object_context_menu,context_menu)
    for cls in (bpy.types.OUTLINER_MT_context_menu,bpy.types.OUTLINER_MT_object,bpy.types.OUTLINER_MT_collection):patch_draw(cls,context_menu)
    for cls in (bpy.types.OUTLINER_MT_id_data,bpy.types.OUTLINER_MT_liboverride,bpy.types.OUTLINER_MT_edit_datablocks):patch_draw(cls,readonly_source_menu)
    patch_draw(bpy.types.PROPERTIES_HT_header,properties_header)
    patch_draw(bpy.types.OUTLINER_HT_header,outliner_header)
    panels=[]
    for name in dir(bpy.types):
        cls=getattr(bpy.types,name)
        if not isinstance(cls,type) or not issubclass(cls,bpy.types.Panel) or name.startswith('RUSTER_'):continue
        if not (getattr(cls,'bl_space_type','')=='PROPERTIES' or
                (getattr(cls,'bl_space_type','')=='VIEW_3D' and getattr(cls,'bl_region_type','')=='UI')):continue
        panels.append(cls)
    # Snapshot before changing parents, so inherited polls retain their originals.
    originals=[(cls,cls.__dict__.get('poll'),getattr(cls,'poll',None)) for cls in panels]
    refresh={cls for cls,_own,original in originals if original is None}
    while True:
        ids={getattr(cls,'bl_idname',cls.__name__) or cls.__name__ for cls in refresh}
        children={cls for cls in panels if getattr(cls,'bl_parent_id','') in ids}
        if children<=refresh:break
        refresh.update(children)
    for cls,own_poll,original in originals:
        _polls.append((cls,own_poll))
        def make_poll(original):
            def poll(panel,context):return False
            return poll
        cls.poll=classmethod(make_poll(original))
    _refreshed_panels.extend(cls for cls in panels if cls in refresh)
    refresh_panels(_refreshed_panels)


def unregister():
    _preview_splits.clear()
    if bpy.app.timers.is_registered(start_workspace):bpy.app.timers.unregister(start_workspace)
    if bpy.app.timers.is_registered(close_map_timeline):bpy.app.timers.unregister(close_map_timeline)
    for cls,poll in reversed(_polls):
        if poll is None:delattr(cls,'poll')
        else:cls.poll=poll
    _polls.clear()
    refresh_panels(_refreshed_panels)
    _refreshed_panels.clear()
    for cls,draw in reversed(_draws):cls.draw=draw
    _draws.clear()
    for cls in reversed(_classes):bpy.utils.unregister_class(cls)
    _classes.clear()
