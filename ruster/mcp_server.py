"""Stdio MCP adapter for the bundled, authenticated community Blender bridge."""
import base64
import threading
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,Field,ConfigDict
from mcp.server.fastmcp import FastMCP,Image
from mcp.types import ToolAnnotations
from .core import settings,mcp_config
from .vendor.blender_mcp_client import BlenderConnection

_connection=None
_lock=threading.Lock()
ROTATION_GUIDANCE=('Verify every placement\'s orientation and intended facing direction using the loaded model and multiple viewport angles. '
    'Do not assume its forward axis or the sign of a rotation. Confirm it faces the correct way and is not reversed, inverted or upside down '
    'before duplicating it or finishing the composition.')
mcp=FastMCP('Rustmatics',instructions='Author custom prefab compositions using original local Rust assets. Read ruster://guide/design and ruster://guide/building, then get_editor_status first. Discover assets with paginated search_prefabs including hidden helpers. Inspect cached model bounds; request missing assets and poll status. Use atomic placement/transform batches, groups, radial arrays, spacing, grid and bounds snapping. Inspect Blender context, control viewport and cursor, and measure compositions. World metres: Z up, XYZ Euler degrees. Original geometry/materials remain linked and read-only: never create custom mesh primitives, including triangles. Existing triangular Rust prefabs and triangular arrangements are allowed. Enable Preferences MCP. Inspect the scene before retrying timed-out mutations. '+ROTATION_GUIDANCE)
READ=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
EDIT=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=False)
DESTRUCTIVE=ToolAnnotations(readOnlyHint=False,destructiveHint=True,openWorldHint=False)


def command(rpc_kind,**params):
    global _connection
    with _lock:
        config=settings.load()
        if not mcp_config.enabled(config):raise ValueError('Enable MCP in Preferences → MCP first')
        token=config.get('mcp_token','')
        if not isinstance(token,str) or len(token)<32:raise ValueError('Start MCP in Preferences → MCP first')
        port=mcp_config.port(config)
        if _connection and (_connection.port!=port or _connection.token!=token):_connection.disconnect();_connection=None
        if _connection is None:_connection=BlenderConnection('127.0.0.1',port,token)
        return _connection.send_command(rpc_kind,params)


@mcp.tool(annotations=READ)
def get_scene_info()->dict:
    """Inspect active prefab composition mode, prefab count and scene objects."""
    return command('get_scene_info')


@mcp.tool(annotations=READ)
def get_object_info(name:str)->dict:
    """Inspect world position, XYZ degree rotation and scale of an object."""
    return command('get_object_info',name=name)


@mcp.tool(annotations=READ)
def list_map_objects(limit:int=100,offset:int=0,query:str='',kind:str='',prefab_id:int|None=None,include_bounds:bool=False)->dict:
    """Legacy alias for list_objects. Page placements/groups; bounds are opt-in for large scenes. Filter names, kind PREFAB/GROUP, or ID; limit 1–200."""
    return command('list_map_objects',limit=limit,offset=offset,query=query,kind=kind,prefab_id=prefab_id,include_bounds=include_bounds)


@mcp.tool(annotations=READ)
def search_prefabs(query:str='',category:str='',limit:int=20,offset:int=0,available_only:bool=False)->dict:
    """Browse/search every indexed Rust prefab by descriptive tokens in any word order (with aliases) and optional category prefix; hidden assets are included. Empty query lists all. Returns IDs, paths, local availability, next_offset. Follow pages until next_offset=null; limit 1–100. Never invent prefab IDs."""
    return command('search_prefabs',query=query,category=category,limit=limit,offset=offset,available_only=available_only)


@mcp.tool(annotations=EDIT)
def place_prefab(uid:int,position:list[float],rotation_degrees:list[float]|None=None,scale:list[float]|None=None)->dict:
    """Place an available local prefab. Three-component world vectors: position/metres with Z height, rotation XYZ degrees, scale factors. Original model stays read-only."""
    return command('place_prefab',uid=uid,position=position,rotation_degrees=rotation_degrees,scale=scale)


@mcp.tool(annotations=EDIT)
def transform_object(name:str,position:list[float]|None=None,rotation_degrees:list[float]|None=None,scale:list[float]|None=None)->dict:
    """Set absolute world placement/group transforms. Rotations are XYZ degrees. Rejects group shear and zero scale; cannot change prefab meshes."""
    return command('transform_object',name=name,position=position,rotation_degrees=rotation_degrees,scale=scale)


@mcp.tool(annotations=EDIT)
def duplicate_object(name:str)->dict:
    """Duplicate a whole placement or group using native Rustmatics operations."""
    return command('duplicate_object',name=name)


@mcp.tool(annotations=DESTRUCTIVE)
def delete_object(name:str)->dict:
    """Delete a whole placement or group. Supports undo; never deletes model parts."""
    return command('delete_object',name=name)


@mcp.tool(annotations=READ)
def validate_map(require_ground:bool=False)->dict:
    """Report map export errors and notes. Set require_ground=True when the map must contain native terrain. This check does not boot Rust."""
    return command('validate_map',require_ground=require_ground)


@mcp.tool(annotations=DESTRUCTIVE)
def save_project(path:str,overwrite:bool=False)->dict:
    """Save to an absolute .ruster destination. Existing files require explicit overwrite=True."""
    return command('save_project',path=path,overwrite=overwrite)


@mcp.tool(annotations=READ)
def get_project_status()->dict:
    """Inspect current project path/title, unsaved changes, placement count and active preview before switching."""
    return command('get_project_status')


@mcp.tool(annotations=READ)
def list_recent_projects(limit:int=20,offset:int=0)->dict:
    """List recently used .ruster project paths and whether they still exist. Does not scan the filesystem. Page 1–100."""
    return command('list_recent_projects',limit=limit,offset=offset)


@mcp.tool(annotations=DESTRUCTIVE)
def new_project(discard_unsaved:bool=False)->dict:
    """Switch to an empty prefab project. Save existing work first; discard_unsaved=True explicitly discards it. An active live preview stays running and clears its scene. Never changes the open project on a refused request."""
    return command('new_project',discard_unsaved=discard_unsaved)


@mcp.tool(annotations=DESTRUCTIVE)
def open_project(path:str,discard_unsaved:bool=False)->dict:
    """Switch to an existing absolute .ruster file through the native project loader. Save current work first or explicitly discard_unsaved=True. An active live preview stays running, clears the previous scene and synchronizes the new project. Embedded scripts stay disabled."""
    return command('open_project',path=path,discard_unsaved=discard_unsaved)


@mcp.tool(annotations=READ)
def get_live_preview_status(log_lines:int=20)->dict:
    """Inspect local preview readiness, installation progress, server/client errors, scene revision, loopback connect command and up to 100 log lines. Does not launch or inject a game client."""
    return command('get_live_preview_status',log_lines=log_lines)


@mcp.tool(annotations=EDIT)
def start_live_preview(port:int|None=None,owner_id:str|None=None,update_server:bool=False)->dict:
    """Start the owned local preview server for this project; downloads SteamCMD/Rust/Carbon on first use. Optional game port and SteamID64; server EAC is disabled and verified. Returns immediately: poll get_live_preview_status. Launch Rust Without EAC and Inject remain explicit Live Preview panel actions."""
    return command('start_live_preview',port=port,owner_id=owner_id,update_server=update_server)


@mcp.tool(annotations=DESTRUCTIVE)
def stop_live_preview()->dict:
    """Request stopping only Rustmatics's owned preview server and bridge. Returns immediately: poll until inactive before starting another session. Does not close Rust or other servers."""
    return command('stop_live_preview')


@mcp.tool(annotations=DESTRUCTIVE)
def send_live_preview_command(console_command:str)->dict:
    """Queue one console command on the running owned local preview server (up to 4096 characters). Check preview logs for execution/results; a queued response is not success from Rust."""
    return command('send_live_preview_command',command=console_command)


@mcp.tool(annotations=DESTRUCTIVE)
def grant_live_preview_owner(steam_id:str)->dict:
    """Queue ownerid for a verified 17-digit SteamID64 on the owned preview server. Reconnect in Rust after it applies."""
    return command('grant_live_preview_owner',steam_id=steam_id)


@mcp.tool(annotations=DESTRUCTIVE)
def export_map(path:str,overwrite:bool=False,require_ground:bool=False)->dict:
    """Write a native .map at an absolute path. Set require_ground=True to require native terrain layers. Works headlessly. Existing files require overwrite=True. See ruster://guide/headless for the one-shot CLI."""
    return command('export_map',path=path,overwrite=overwrite,require_ground=require_ground)


@mcp.tool(annotations=DESTRUCTIVE)
def export_project_report(path:str,overwrite:bool=False)->dict:
    """Write an editable .docx summary with map validation, ground status and prefab inventory to an absolute path. Reports can document invalid projects. Works headlessly without Word."""
    return command('export_project_report',path=path,overwrite=overwrite)


@mcp.tool(annotations=DESTRUCTIVE)
def undo()->dict:
    """Undo the latest native editing action, including manual edits."""
    return command('undo')


@mcp.tool(annotations=DESTRUCTIVE)
def redo()->dict:
    """Redo the latest undone native editing action."""
    return command('redo')


class PrefabPlacement(BaseModel):
    model_config=ConfigDict(extra='forbid')
    uid:int=Field(ge=0,le=4294967295,description='Rust prefab ID returned by search_prefabs')
    position:list[float]=Field(min_length=3,max_length=3,description='World XYZ metres, Z up')
    rotation_degrees:list[float]|None=Field(default=None,min_length=3,max_length=3)
    scale:list[float]|None=Field(default=None,min_length=3,max_length=3)


class ObjectTransform(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str
    position:list[float]|None=Field(default=None,min_length=3,max_length=3)
    rotation_degrees:list[float]|None=Field(default=None,min_length=3,max_length=3)
    scale:list[float]|None=Field(default=None,min_length=3,max_length=3)


@mcp.tool(annotations=READ)
def get_editor_status()->dict:
    """Start here for project mode, catalog readiness, asset loading and available tools. If headless is true, see ruster://guide/headless for automation and exports."""
    return command('get_editor_status')


@mcp.tool(annotations=READ)
def list_prefab_categories(limit:int=100,offset:int=0)->dict:
    """Discover catalog category paths and prefab counts. Page 1–200 categories; pass a path to search_prefabs(category=...)."""
    return command('list_prefab_categories',limit=limit,offset=offset)


@mcp.tool(annotations=READ)
def get_prefab_details(uid:int)->dict:
    """Inspect a catalog prefab's local dimensions/bounds, mesh/material/node/triangle counts and cached availability. Bounds enclose the original model; useful for sizing compositions. Uncached assets return a preload next step."""
    return command('get_prefab_details',uid=uid)


@mcp.tool(annotations=EDIT)
def request_prefab_assets(uids:list[int])->dict:
    """Queue extraction of 1–100 locally available prefab models without placing objects. Poll get_editor_status until idle, then inspect details/place."""
    return command('request_prefab_assets',uids=uids)


@mcp.tool(annotations=EDIT)
def place_prefabs(placements:list[PrefabPlacement],group_name:str|None=None)->dict:
    """Build a model from 1–100 whole Rust prefabs in one undo step. Every position/rotation/scale is absolute WORLD space. Optionally create a named composition group. Entire batch rolls back if any placement fails. Geometry remains original."""
    return command('place_prefabs',placements=[p.model_dump() for p in placements],group_name=group_name)


@mcp.tool(annotations=EDIT)
def transform_objects(transforms:list[ObjectTransform])->dict:
    """Atomically transform 1–200 placements/groups in one undo step. Absolute world transforms. On error restores all transforms. Choose parent group OR descendants, not both."""
    return command('transform_objects',transforms=[p.model_dump(exclude_none=True) for p in transforms])


@mcp.tool(annotations=EDIT)
def group_objects(names:list[str],name:str='AI Composition')->dict:
    """Combine 1–200 independent whole prefabs/groups into a reusable model. Preserves world transforms. Use uniform scale when rotating grouped components."""
    return command('group_objects',names=names,name=name)


@mcp.tool(annotations=EDIT)
def ungroup_object(name:str)->dict:
    """Remove a composition group while keeping its children and world transforms. Undo supported."""
    return command('ungroup_object',name=name)


@mcp.tool(annotations=EDIT)
def rename_object(name:str,new_name:str)->dict:
    """Name a placement/group for subsequent AI operations. Returns actual Blender name (uniqueness suffix may be added)."""
    return command('rename_object',name=name,new_name=new_name)


@mcp.tool(annotations=EDIT)
def focus_view(names:list[str],axis:Literal['CURRENT','TOP','BOTTOM','FRONT','BACK','LEFT','RIGHT','OBLIQUE']='CURRENT',rotation_degrees:list[float]|None=None)->dict:
    """Select and frame placements/groups without transforming models. Native axes or OBLIQUE (60,0,45 degrees); alternatively pass finite XYZ viewport Euler rotation_degrees with axis=CURRENT. Oblique/custom views use perspective."""
    return command('focus_view',names=names,axis=axis,rotation_degrees=rotation_degrees)


@mcp.tool(annotations=READ)
def get_viewport_image(include_overlays:bool=False)->Image:
    """Return a clean PNG of the current 3D viewport. Overlays (grids, relationships and selection outlines) are hidden by default; include_overlays=True includes them. Restores viewport/render settings on success or failure. Requires the desktop app."""
    result=command('get_viewport_image',include_overlays=include_overlays)
    return Image(data=base64.b64decode(result['data']),format='png')


@mcp.tool(annotations=DESTRUCTIVE)
def export_assembly(names:list[str],path:str,overwrite:bool=False,origin:list[float]|None=None)->dict:
    """Export reusable .rusterprefab using original prefab IDs. Positions are relative to explicit world XYZ origin, the pivot of a single selected group, or the mean placement position for other selections. Absolute destination; existing file requires overwrite=True."""
    return command('export_assembly',names=names,path=path,overwrite=overwrite,origin=origin)


@mcp.tool(annotations=EDIT)
def import_assembly(path:str,position:list[float],name:str|None=None)->dict:
    """Load a .rusterprefab composition at a world XYZ origin, preserving that group pivot. Uses only available local original prefabs; 2000-placement limit. Rolls back on failure; supports undo. Does not import arbitrary meshes."""
    return command('import_assembly',path=path,position=position,name=name)


BUILDING_GUIDE="""Rustmatics prefab authoring workflow
1. get_editor_status reports actual version, converter_version, restart_required, visible and unloaded placements. Enable Preferences MCP and open a prefab composition. Wait for extraction/visual loading before requesting more assets.
2. list_prefab_categories discovers taxonomy. search_prefabs includes hidden/IO helpers and supports descriptive tokens in any word order and aliases. Empty query browses all; follow next_offset. Never guess IDs. Local availability does not guarantee successful model extraction.
3. get_prefab_details/get_prefabs_details inspect original cached model dimensions and enclosing bounds. request_prefab_assets queues up to 100 real IDs; poll status then retry details. No full-engine scan is needed.
4. Coordinates are world XYZ metres, Z up. Rotation is XYZ Euler degrees; scale is a finite nonzero factor. Ground is an authoring plane at Z=0. snap_objects_to_plane moves true loaded model bottoms to a plane. align_objects uses MIN/CENTER/MAX bounds on X/Y/Z; omitted target uses the first object's bound.
5. Compose whole prefabs using atomic place_prefabs (up to 100) and transform_objects (up to 200). Groups preserve world transforms. Select parent groups OR descendants per batch. Prefer uniform group scale to avoid shear. Original meshes/materials remain linked and read-only.
6. list_objects/get_selection paginate lightweight metadata. include_bounds opts into model world bounds; get_objects_info batches up to 200 objects, get_prefabs_details up to 20 IDs. Child and mutation names are capped at 200; list_objects locates more without returning huge groups.
7. duplicate_objects makes one translated copy. array_objects makes count NEW copies using per-step world offset and XYZ rotation about the chosen roots' mean origin. Requests roll back on failure and allow at most 2000 new prefab placements/4000 hierarchy objects. delete_objects validates all targets before deletion. Undo/redo also affects manual edits.
8. focus_view frames native axes, OBLIQUE, or explicit XYZ rotation_degrees without transforming models. get_viewport_image defaults to a clean image; include_overlays=True includes guides. Viewport/render settings are restored after capture. Bounds enclose render geometry, not collision shapes; they do not certify server gameplay.
9. export_prefab writes RustEdit 2 .asset or legacy .prefab according to the destination extension. Legacy import_prefab and .prefab re-export preserve supported imported metadata. get_io_settings/set_io_settings expose only verified EXISTING imported IO scalar fields; wiring creation is unsupported. export_assembly uses explicit world origin when supplied, otherwise the single selected group's pivot or the mean placement position. import_assembly supports 2000 placements and preserves its requested position as group pivot. get_export_formats lists available GLB/FBX/OBJ and other model exporters; export_models uses bounded disposable copies and preserves source scene and selection.
10. import_map_prefabs appends ONLY prefab placements from .map, ignoring terrain/paths. export_map writes prefab placements and, when added, native Rust ground layers. validate_map checks compositions and ground, save_project writes .ruster including source.map for ground. Destinations are absolute; existing files require overwrite=True. Source models and selection survive exports.
11. get_blender_context inspects Blender version, mode, cursor and native snapping. get_viewport_state/set_viewport_state control view target, distance, rotation, projection, shading, overlays, X-ray, lens and clipping without moving prefabs. set_cursor sets world location and XYZ orientation (omitted rotation resets to zero). Viewport and cursor settings are UI state, not an undo step.
12. transform_objects_relative applies world-axis scale, then XYZ rotation around a shared pivot, then world translation. Omitted pivot is the mean selected root origin; shear is rejected rather than approximated. radial_array_objects makes count NEW independent copies about an explicit world center/axis using a per-copy angle step; for a ring of 8 including the source use count=7, angle_degrees=45. Existing array limits, rollback and metadata preservation apply.
13. distribute_objects uses the supplied names order; the first root stays fixed. ORIGIN makes signed origin steps; BOUNDS_GAP leaves the requested signed gap between successive loaded world bounds. snap_objects_to_grid rounds root origins to a world grid, half steps away from zero relative to origin. measure_objects reports origin distance and optionally axis-aligned render bounds separation, never certified collision clearance. Turn off live Radial Symmetry before batch layout of linked symmetry members.
14. Read ruster://guide/design (the dedicated docs/design.md resource file) before building. Move, rotate and duplicate whole real prefabs. Scale static decorations with verification; networked doors, IO and deployables ignore scale in Rust. No custom triangle/cube mesh creation, prefab mesh edits, material replacement, arbitrary Python, path tools or procedural map generation are exposed. Existing triangular Rust assets and triangular compositions are valid. After timeout inspect the scene before replaying a mutation.
15. Project switching: get_project_status and list_recent_projects identify the active/recent .ruster files. save_project before open_project/new_project; unsaved work is protected unless discard_unsaved=True is explicitly requested. An active live preview stays running in the same Rustmatics window: its old scene is cleared and the new project is synchronized/recentered. Project replacement is not undoable.
16. Live preview: start_live_preview starts only the owned local EAC-disabled server. Poll get_live_preview_status for setup progress, placement count, readiness, client errors, logs and the F1 connect command. stop_live_preview requests owned-session cleanup. send_live_preview_command and grant_live_preview_owner queue server commands; inspect logs for results. Launch Rust Without EAC and Inject are explicit Live Preview panel actions with the existing EAC checks.
17. add_ground adds a native terrain patch. paint_ground brushes height, water, splat, biome, alpha and topology; get_ground_status inspects it. export_prefab embeds terrain in .asset and writes legacy terrain masks beside .prefab with native ground. terrain_width=0 fits the selection around its prefab pivot. Current .map exports and preview startup include the whole terrain, including water and Jungle. Restart preview after painting ground.
"""
BUILDING_GUIDE+='17. '+ROTATION_GUIDANCE+'\n'
BUILDING_GUIDE+='18. Read ruster://guide/headless for headless authoring, validation, one-shot .map exports, and .docx project reports. get_editor_status lists unavailable_tools. Save projects explicitly before stopping the host.\n'

@mcp.resource('ruster://guide/building',mime_type='text/plain')
def building_guide()->str:
    return BUILDING_GUIDE


@mcp.resource('ruster://guide/design',mime_type='text/markdown')
def design_guide()->str:
    """The project's building constraints and Rust compatibility sources."""
    return (Path(__file__).resolve().parent.parent/'docs'/'design.md').read_text(encoding='utf-8')


@mcp.resource('ruster://guide/headless',mime_type='text/markdown')
def headless_guide()->str:
    """Use the one-shot CLI or a persistent background MCP editing session."""
    return (Path(__file__).resolve().parent.parent/'docs'/'headless.md').read_text(encoding='utf-8')


@mcp.prompt()
def build_rust_composition(description:str)->str:
    """Plan and build a custom Rust model from the local prefab library."""
    return 'Read ruster://guide/design first.\n'+BUILDING_GUIDE+'\nUser composition request: '+description+'\nDiscover suitable real assets, report any missing models, plan dimensions, build and validate. In the desktop editor inspect multiple viewport angles. In a headless session inspect loaded bounds and transforms, then report that visual inspection is pending. Do not create custom mesh geometry. Do not claim server-tested compatibility.'


@mcp.prompt()
def audit_rust_composition()->str:
    """Inspect a prefab composition for concrete export and original-asset issues without changes."""
    return 'Read ruster://guide/design and ruster://guide/building. Inspect status, paginate list_objects, inspect relevant original model details/world bounds and validate_map. Report concrete issues and names. Do not modify or overwrite files.'


@mcp.tool(annotations=READ)
def list_objects(limit:int=100,offset:int=0,query:str='',kind:str='',prefab_id:int|None=None,include_bounds:bool=False)->dict:
    """Page 1–200 placements/groups; bounds are opt-in. Filter names, kind PREFAB/GROUP or ID. Follow next_offset."""
    return command('list_objects',limit=limit,offset=offset,query=query,kind=kind,prefab_id=prefab_id,include_bounds=include_bounds)

@mcp.tool(annotations=READ)
def get_objects_info(names:list[str],include_bounds:bool=True)->dict:
    """Inspect 1–200 named roots in one request, with optional loaded model world bounds. Child names capped at 200."""
    return command('get_objects_info',names=names,include_bounds=include_bounds)

@mcp.tool(annotations=READ)
def get_prefabs_details(uids:list[int])->dict:
    """Read original cached dimensions and metadata for 1–20 real prefab IDs. Does not extract or scan game bundles."""
    return command('get_prefabs_details',uids=uids)

@mcp.tool(annotations=READ)
def get_selection(limit:int=100,offset:int=0)->dict:
    """Read active object and paginate selected prefab placements/groups; limit 1–200."""
    return command('get_selection',limit=limit,offset=offset)

@mcp.tool(annotations=EDIT)
def select_objects(names:list[str])->dict:
    """Select 1–200 independent whole placements/groups without changing transforms."""
    return command('select_objects',names=names)

@mcp.tool(annotations=EDIT)
def duplicate_objects(names:list[str],offset:list[float]|None=None)->dict:
    """Atomically copy roots/groups with world translation (default [2,0,0]). At most 2000 new placements. Preserves source selection."""
    return command('duplicate_objects',names=names,offset=offset if offset is not None else [2,0,0])

@mcp.tool(annotations=DESTRUCTIVE)
def delete_objects(names:list[str])->dict:
    """Validate then delete 1–200 independent roots/groups in one undo step. Rejects group and descendant targets together."""
    return command('delete_objects',names=names)

@mcp.tool(annotations=EDIT)
def array_objects(names:list[str],count:int,offset:list[float],rotation_degrees:list[float]|None=None)->dict:
    """Make count NEW copies. Each step adds world offset and XYZ rotation around roots' mean origin. Atomic; at most 2000 new placements/4000 hierarchy objects. Preserves source selection."""
    return command('array_objects',names=names,count=count,offset=offset,rotation_degrees=rotation_degrees if rotation_degrees is not None else [0,0,0])

@mcp.tool(annotations=EDIT)
def align_objects(names:list[str],axis:str='Z',mode:str='MIN',target:float|None=None)->dict:
    """Align loaded model world MIN/CENTER/MAX bounds on X/Y/Z. target is world coordinate; omitted uses first root's bound. Atomic with undo."""
    return command('align_objects',names=names,axis=axis,mode=mode,target=target)

@mcp.tool(annotations=EDIT)
def snap_objects_to_plane(names:list[str],target:float=0,axis:str='Z')->dict:
    """Place loaded model/group MIN bounds on world plane (default ground Z=0), accounting for rotations, scales and pivots. Atomic with undo."""
    return command('snap_objects_to_plane',names=names,target=target,axis=axis,mode='MIN')

@mcp.tool(annotations=READ)
def get_export_formats()->dict:
    """List available model and report formats, placement interchange formats, and geometry limits."""
    return command('get_export_formats')

@mcp.tool(annotations=DESTRUCTIVE)
def export_prefab(names:list[str],path:str,overwrite:bool=False,include_terrain:bool|None=None,
                  terrain_width:int=0,terrain_fade:int=5,preview_png:str|None=None)->dict:
    """Export a RustEdit 2 .asset with embedded terrain or a legacy .prefab with terrain companion files. Native ground is included by default. Width 0 fits the selection around its pivot. Width and fade are in metres. preview_png is optional for .prefab. overwrite=True replaces the output."""
    return command('export_prefab',names=names,path=path,overwrite=overwrite,
        include_terrain=include_terrain,terrain_width=terrain_width,terrain_fade=terrain_fade,
        preview_png=preview_png)

@mcp.tool(annotations=EDIT)
def import_prefab(path:str,position:list[float]|None=None)->dict:
    """Append native RustEdit .prefab at world position (default origin), preserving supported metadata. At most 2000 placements, validated first. Undo supported."""
    return command('import_prefab',path=path,position=position if position is not None else [0,0,0])

@mcp.tool(annotations=DESTRUCTIVE)
def export_models(names:list[str],path:str,format:str='GLB',overwrite:bool=False)->dict:
    """Export loaded original render geometry in a format from get_export_formats. Uses bounded disposable copies and preserves original scene/selection."""
    return command('export_models',names=names,path=path,format=format,overwrite=overwrite)

@mcp.tool(annotations=EDIT)
def import_map_prefabs(path:str)->dict:
    """Append ONLY prefab placements from absolute .map path; ignore terrain/paths. At most 2000 placements/256MiB. Preserves existing composition. Undo supported."""
    return command('import_map_prefabs',path=path)

class IOSettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    access_level:int|None=Field(default=None,ge=0,le=3)
    door_effect:int|None=Field(default=None,ge=-1,le=3)
    timer_length:float|None=Field(default=None,ge=0,le=86400)
    frequency:int|None=Field(default=None,ge=0,le=9999999)
    unlimited_ammo:bool|None=None
    peacekeeper:bool|None=None
    weapon:str|None=Field(default=None,max_length=256)
    branch_amount:int|None=Field(default=None,ge=0,le=2147483647)
    counter_target:int|None=Field(default=None,ge=0,le=2147483647)
    camera_identifier:str|None=Field(default=None,max_length=256)
    counter_passthrough:bool|None=None
    floors:int|None=Field(default=None,ge=1,le=100)
    phone_name:str|None=Field(default=None,max_length=256)

@mcp.tool(annotations=READ)
def get_io_settings(name:str)->dict:
    """Read verified existing RustEdit IO scalar fields on an imported placement. Wiring creation is unsupported."""
    return command('get_io_settings',name=name)

@mcp.tool(annotations=EDIT)
def set_io_settings(name:str,settings:IOSettings)->dict:
    """Update supplied verified EXISTING imported IO scalar fields; preserves other fields. One undo step; does not fabricate wiring."""
    return command('set_io_settings',name=name,settings=settings.model_dump(exclude_none=True))

class ViewportSettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    target:list[float]|None=Field(default=None,min_length=3,max_length=3)
    rotation_degrees:list[float]|None=Field(default=None,min_length=3,max_length=3)
    distance:float|None=Field(default=None,gt=0,le=1e7)
    projection:Literal['PERSP','ORTHO']|None=None
    shading:Literal['WIREFRAME','SOLID','MATERIAL','RENDERED']|None=None
    color_type:Literal['MATERIAL','OBJECT','RANDOM','SINGLE','TEXTURE']|None=None
    show_overlays:bool|None=None
    show_xray:bool|None=None
    lens:float|None=Field(default=None,gt=0,le=1e7)
    clip_start:float|None=Field(default=None,gt=0,le=1e7)
    clip_end:float|None=Field(default=None,gt=0,le=1e7)


@mcp.tool(annotations=READ)
def get_blender_context()->dict:
    """Inspect Blender version, scene/workspace, mode, selection count, active object, 3D cursor and native snap state. No mesh mutation access."""
    return command('get_blender_context')


@mcp.tool(annotations=READ)
def get_viewport_state()->dict:
    """Read the first 3D viewport's target, distance, XYZ degree orientation, projection, shading, overlays, X-ray, lens and clipping."""
    return command('get_viewport_state')


@mcp.tool(annotations=EDIT)
def set_viewport_state(settings:ViewportSettings)->dict:
    """Set supplied viewport fields without changing models/selection. Target is world XYZ; rotation is XYZ degrees. Validates all fields first. UI state has no undo step. Requires a 3D viewport."""
    return command('set_viewport_state',**settings.model_dump(exclude_none=True))


@mcp.tool(annotations=EDIT)
def set_cursor(position:list[float],rotation_degrees:list[float]|None=None)->dict:
    """Set Blender's 3D cursor world XYZ metres and XYZ degree orientation (default zero). Does not move prefabs. UI state has no undo step."""
    return command('set_cursor',position=position,rotation_degrees=rotation_degrees if rotation_degrees is not None else [0,0,0])


@mcp.tool(annotations=READ)
def measure_objects(first:str,second:str,include_bounds:bool=True)->dict:
    """Measure two distinct placement/group origins in metres. Optional loaded world render bounds give per-axis gaps and AABB distance; overlap reports zero, not collision certification."""
    return command('measure_objects',first=first,second=second,include_bounds=include_bounds)


@mcp.tool(annotations=EDIT)
def transform_objects_relative(names:list[str],offset:list[float]|None=None,rotation_degrees:list[float]|None=None,scale:list[float]|None=None,pivot:list[float]|None=None)->dict:
    """Atomically scale on world axes, then rotate XYZ degrees around a shared world pivot, then translate in world metres. Default pivot: mean root origin. Up to 200 independent roots; rejects shear/zero scale. Disable live symmetry for linked members. Undo supported."""
    return command('transform_objects_relative',names=names,offset=offset if offset is not None else [0,0,0],rotation_degrees=rotation_degrees if rotation_degrees is not None else [0,0,0],scale=scale if scale is not None else [1,1,1],pivot=pivot)


@mcp.tool(annotations=EDIT)
def radial_array_objects(names:list[str],count:int,center:list[float],angle_degrees:float,axis:Literal['X','Y','Z']='Z')->dict:
    """Make count NEW independent copies around an explicit world center and axis, each rotated another angle_degrees. Ring of 8 including source: count=7, angle_degrees=45. Atomic, undoable; 2000-new-placement/4000-hierarchy limit. Preserves source selection and originals."""
    return command('radial_array_objects',names=names,count=count,center=center,angle_degrees=angle_degrees,axis=axis)


@mcp.tool(annotations=EDIT)
def distribute_objects(names:list[str],spacing:float,axis:Literal['X','Y','Z']='X',mode:Literal['ORIGIN','BOUNDS_GAP']='ORIGIN')->dict:
    """Space 2–200 independent roots in supplied name order, keeping the first fixed. Signed metres: ORIGIN steps between origins; BOUNDS_GAP gaps from previous max to next min loaded render bounds. Atomic with undo; disable live symmetry for linked members."""
    return command('distribute_objects',names=names,spacing=spacing,axis=axis,mode=mode)


@mcp.tool(annotations=EDIT)
def snap_objects_to_grid(names:list[str],step:float,origin:list[float]|None=None,axes:str='XYZ')->dict:
    """Atomically round 1–200 whole root origins to a world grid of step metres (positive). Origin defaults to [0,0,0]; axes contains unique X/Y/Z letters. Half steps round away from grid origin. Rotation/scale unchanged. Undo supported."""
    return command('snap_objects_to_grid',names=names,step=step,origin=origin if origin is not None else [0,0,0],axes=axes)


@mcp.tool(annotations=READ)
def get_ground_status()->dict:
    """Inspect optional native Rust terrain, patch/world dimensions, paint channels and preview restart state."""
    return command('get_ground_status')


@mcp.tool(annotations=EDIT)
def add_ground(size:float=512.,height:float=0.,biome:Literal['Arid','Temperate','Tundra','Arctic','Jungle']='Temperate',splat:Literal['Dirt','Snow','Sand','Rock','Grass','Forest','Stones','Gravel']='Grass',world_size:int=1000)->dict:
    """Add one flat native Rust ground patch without moving prefab placements. Width 16–2000m, containing world 1000–2000m. Ground is not a prefab mesh: .map exports include native height/biome/splat/alpha/topology layers; prefab exports omit terrain. Undoable; existing ground is preserved."""
    return command('add_ground',size=size,height=height,biome=biome,splat=splat,world_size=world_size)


@mcp.tool(annotations=EDIT)
def paint_ground(layer:Literal['terrain','water','splat','biome','topology','alpha'],center:list[float],radius:float,channel:str|int=0,strength:float=.5,falloff:float=.6,shape:Literal['CIRCLE','SQUARE']='CIRCLE',tool:Literal['PAINT','RAISE','LOWER','FLATTEN','SMOOTH']='PAINT',height:float=0.,invert:bool=False)->dict:
    """Brush full-resolution native ground at world XY metres; Z is height. Biome/splat/topology channel accepts a name or index. Jungle is the implicit remainder of four native biome channels. Height brushes need a height tool. One undo step. Restart an active preview server after terrain edits."""
    return command('paint_ground',layer=layer,center=center,radius=radius,channel=channel,strength=strength,falloff=falloff,shape=shape,tool=tool,height=height,invert=invert)


def main():
    try:mcp.run(transport='stdio')
    finally:
        if _connection:_connection.disconnect()
