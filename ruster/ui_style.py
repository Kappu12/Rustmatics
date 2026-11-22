"""Shared UI vocabulary using only Blender's bundled, theme-aware icon set.

No image previews, external icon fonts, fixed UI colors or theme overrides.
Toolbar .dat handles also come from the pinned Blender runtime.
"""
import textwrap

ICONS={
    'none':'NONE','project':'FILE_BLEND','world':'WORLD','start':'HOME',
    'new':'FILE_NEW','open':'FILE_FOLDER','save':'FILE_TICK','save_as':'FILE_BLEND',
    'import':'IMPORT','export':'EXPORT','settings':'PREFERENCES','quit':'QUIT',
    'undo':'LOOP_BACK','redo':'LOOP_FORWARDS','duplicate':'DUPLICATE','delete':'TRASH',
    'group':'GROUP','ungroup':'UNLINKED','snap':'SNAP_FACE','select_same':'SELECT_EXTEND',
    'replace':'ARROW_LEFTRIGHT','scatter':'PARTICLES','restore':'FILE_REFRESH',
    'prefab':'OUTLINER_OB_GROUP_INSTANCE','terrain':'MESH_GRID','terrain_brush':'BRUSH_DATA',
    'prefab_brush':'PARTICLE_DATA','path':'CURVE_PATH','edit_path':'EDITMODE_HLT',
    'finish_path':'CHECKMARK','validate':'CHECKMARK','locked':'LOCKED','info':'INFO',
    'error':'ERROR','busy':'TIME','cancel':'CANCEL','refresh':'FILE_REFRESH','report':'TEXT',
    'recover':'RECOVER_LAST','search':'VIEWZOOM','favorite_on':'SOLO_ON','favorite_off':'SOLO_OFF',
    'previous':'TRIA_LEFT','next':'TRIA_RIGHT','cursor':'CURSOR','clone':'EYEDROPPER',
    'bridge':'IPO_LINEAR','generate':'WORLD','overlay':'OVERLAY','detail':'MESH_GRID',
    'move':'EMPTY_ARROWS','rotate':'ORIENTATION_GIMBAL','scale':'FULLSCREEN_ENTER',
    'select':'RESTRICT_SELECT_OFF','water':'MOD_OCEAN','splat':'TEXTURE','biome':'WORLD',
    'topology':'GROUP_VERTEX','alpha':'IMAGE_ALPHA','blank':'BLANK1','snapping':'SNAP_ON',
    'perspective':'VIEW_PERSPECTIVE','volume':'MESH_UVSPHERE',
    'array':'MOD_ARRAY','radial':'FORCE_VORTEX','align':'ALIGN_JUSTIFY',
    'play':'PLAY','stop':'CANCEL','plugin':'PLUGIN','console':'CONSOLE','owner':'USER',
}

TOOL_ICONS={
    'terrain':'ops.sculpt.mesh_filter',
    'prefab':'brush.particle.add',
}

# One action has the same name and icon in sidebars, menus and command popups.
ACTIONS={
    'ruster.welcome':('Start','start'),
    'ruster.new':('New Prefab','new'),
    'ruster.open':('Open Map, Prefab or Project…','open'),
    'ruster.save':('Save Project','save'),
    'ruster.prefab_import':('Import RustEdit Prefab…','import'),
    'ruster.prefab_export':('Export RustEdit Prefab…','export'),
    'ruster.monument_export':('Export RustEdit 2 Monument…','export'),
    'ruster.model_export':('Export Models…','export'),
    'ruster.blend_export':('Export Blender File (.blend)…','export'),
    'ruster.io_settings':('Rust IO Settings…','settings'),
    'ruster.import_map':('Import .map…','import'),
    'ruster.export_map':('Export .map…','export'),
    'ruster.setup':('Rust Installation…','settings'),
    'ruster.preferences':('Preferences…','settings'),
    'ruster.mcp_config':('Copy MCP Client Configuration','duplicate'),
    'ruster.mcp_retry':('Retry MCP Connection','refresh'),
    'ruster.quit':('Quit Rustmatics','quit'),
    'ruster.start_live_preview':('Start Live Preview','play'),
    'ruster.stop_live_preview':('Stop Live Preview','stop'),
    'ruster.launch_preview_rust':('Launch Rust Without EAC','play'),
    'ruster.inject_preview':('Inject','plugin'),
    'ruster.preview_copy_ip':('Copy IP','duplicate'),
    'ruster.preview_command':('Send Command','console'),
    'ruster.preview_owner':('Grant Owner ID','owner'),
    'ruster.preview_logs':('Open Preview Logs','open'),
    'ed.undo':('Undo','undo'),'ed.redo':('Redo','redo'),
    'ruster.duplicate':('Duplicate Prefabs','duplicate'),
    'ruster.duplicate_move':('Duplicate Prefabs','duplicate'),
    'ruster.delete':('Delete Prefabs','delete'),
    'ruster.group':('Group Prefabs','group'),
    'ruster.ungroup':('Ungroup Prefabs','ungroup'),
    'ruster.select_same':('Select Same Prefab','select_same'),
    'ruster.replace':('Replace Prefabs…','replace'),
    'ruster.assembly_import':('Import Prefab Group…','import'),
    'ruster.assembly_export':('Export Prefab Group…','export'),
    'ruster.restore_models':('Restore Original Models','restore'),
    'ruster.assets':('Load Selected Models','import'),
    'ruster.cancel_assets':('Cancel Asset Loading','cancel'),
    'ruster.asset_report':('Asset Report','report'),
    'ruster.composition_check':('Check Composition','validate'),
    'ruster.select_composition_issue':('Select Composition Issues','select'),
    'ruster.layout':('Prefab Layout…','move'),
    'transform.translate':('Move','move'),
    'transform.rotate':('Rotate','rotate'),
    'transform.resize':('Scale','scale'),
    'view3d.view_persportho':('Perspective/Orthographic','perspective'),
    'wm.search_single_menu':('Search All Prefabs…','search'),
}


def action(layout,operator,*,text=None,icon=None,**kwargs):
    label,semantic=ACTIONS[operator]
    return layout.operator(operator,text=label if text is None else text,
        icon=ICONS[semantic if icon is None else icon],**kwargs)


def properties(layout):
    layout.use_property_split=True
    layout.use_property_decorate=False
    return layout


def invoke_dialog(operator,context,event=None,**kwargs):
    """Anchor modal dialogs in their parent window, preserving pointer position."""
    window=context.window
    original=(event.mouse_x,event.mouse_y) if event is not None and hasattr(event,'mouse_x') else None
    if window is None or original is None:
        return context.window_manager.invoke_props_dialog(operator,**kwargs)
    try:
        window.cursor_warp(window.width//2,window.height//2)
        return context.window_manager.invoke_props_dialog(operator,**kwargs)
    finally:window.cursor_warp(*original)


def duplicate_button(layout, context):
    """Interactive movement belongs to a viewport; other editors copy in place."""
    in_view = context.area and context.area.type == 'VIEW_3D'
    props = action(layout, 'ruster.duplicate_move' if in_view else 'ruster.duplicate')
    if not in_view:props.offset = (0, 0, 0)
    return props


def section(layout,identifier,title,icon,*,closed=True):
    header,body=layout.panel(identifier,default_closed=closed)
    header.label(text=title,icon=ICONS[icon])
    if body is not None:properties(body)
    return body


def wrapped(layout,context,text,*,icon=None,width=None):
    """Wrap full-width hints using region width and the user's UI scale."""
    region=getattr(context,'region',None)
    scale=context.preferences.system.ui_scale
    available=width*scale if width is not None else getattr(region,'width',320)
    columns=max(12,min(64,int((available-48*scale)/(7*max(scale,.5)))))
    lines=textwrap.wrap(str(text),width=columns,break_long_words=True,break_on_hyphens=False) or ['']
    for index,line in enumerate(lines):
        layout.label(text=line,icon=ICONS[icon] if index==0 and icon else ICONS['blank' if icon else 'none'])


def tool_button(layout,context,tool,label,icon):
    active=None
    if context.workspace:
        active=context.workspace.tools.from_space_view3d_mode(context.mode,create=False)
    return layout.operator('wm.tool_set_by_id',text=label,icon=ICONS[icon],
        depress=bool(active and active.idname==tool))
