"""Bounded whole-prefab authoring commands; original linked models stay read-only."""
import base64
from contextlib import contextmanager
import math
import os
import tempfile
from pathlib import Path
import struct
import uuid
import bpy
from mathutils import Euler,Matrix,Vector
from . import app,scene,placement,mcp_building
from .core import wire

READ={'get_objects_info','get_prefabs_details','get_selection','get_export_formats','get_io_settings'}
EDIT={'select_objects','duplicate_objects','delete_objects','array_objects','align_objects','snap_objects_to_plane',
      'export_prefab','import_prefab','export_models','import_map_prefabs','set_io_settings'}
MAX_NEW=2000


@contextmanager
def preserved_selection():
    selected=list(bpy.context.selected_objects);active=bpy.context.view_layer.objects.active
    try:yield
    finally:
        placement.select([obj for obj in selected if obj.name in bpy.context.scene.objects])
        bpy.context.view_layer.objects.active=active if active and active.name in bpy.context.scene.objects else None


def list_objects(server,p,bridge):
    limit=mcp_building.bounded_int(p.get('limit',100),1,200,'Limit')
    offset=mcp_building.bounded_int(p.get('offset',0),0,2**31-1,'Offset')
    query=p.get('query','');uid=p.get('prefab_id');kind=p.get('kind','');with_bounds=p.get('include_bounds',False)
    if not isinstance(query,str) or len(query)>256 or kind not in {'','PREFAB','GROUP'}:raise ValueError('Invalid object filter')
    if type(with_bounds)!=bool:raise ValueError('include_bounds must be boolean')
    if uid is not None:mcp_building.bounded_int(uid,0,0xffffffff,'Prefab ID')
    objects=sorted((o for o in bpy.context.scene.objects if o.get('ruster_kind') in {'PREFAB','GROUP'}
        and query.casefold() in o.name.casefold() and (not kind or o.get('ruster_kind')==kind)
        and (uid is None or o.get('ruster_uid')==str(uid))),key=lambda o:o.name)
    hierarchy=mcp_building.hierarchy();cache={}
    return {'total':len(objects),'objects':[bridge.object_info(server,o.name,with_bounds,hierarchy,cache) for o in objects[offset:offset+limit]],
        'next_offset':offset+limit if offset+limit<len(objects) else None}


def trees(roots):
    children=mcp_building.hierarchy();result=[]
    for root in roots:
        objects=[];pending=[root]
        while pending:
            obj=pending.pop()
            if obj.get('ruster_kind') not in {'PREFAB','GROUP'}:raise ValueError('Composition contains a non-prefab child')
            objects.append(obj);pending.extend(children.get(obj,()))
        result.append(objects)
    return result


def prefabs_in(objects):
    return [o for tree in trees(objects) for o in tree if o.get('ruster_kind')=='PREFAB']


def clone_arrays(objects,count,offset,rotation,bridge,anchor=None):
    from .construction import clear_copy_link
    source_trees=trees(objects)
    n=sum(o.get('ruster_kind')=='PREFAB' for tree in source_trees for o in tree)
    if n*count>MAX_NEW or sum(map(len,source_trees))*count>4000:
        raise ValueError('Array/duplicate exceeds 2000 new prefab placements or 4000 hierarchy objects; choose fewer copies')
    bpy.context.view_layer.update()
    originals={obj:obj.matrix_world.copy() for tree in source_trees for obj in tree}
    if any(scene.matrix_has_shear(matrix) or any(not math.isfinite(v) for row in matrix for v in row) for matrix in originals.values()):
        raise ValueError('Source hierarchy contains shear, zero scale or an invalid transform')
    anchor=Vector(anchor) if anchor is not None else sum((originals[o].translation for o in objects),Vector())/len(objects)
    created=[];roots=[];texts=[]
    try:
        for index in range(1,count+1):
            rotate=Euler(tuple(math.radians(v)*index for v in rotation),'XYZ').to_matrix().to_4x4()
            delta=Matrix.Translation(anchor+Vector(offset)*index)@rotate@Matrix.Translation(-anchor)
            for source_tree in source_trees:
                copies={}
                for obj in source_tree:
                    copy=obj.copy();created.append(copy);obj.users_collection[0].objects.link(copy);copies[obj]=copy
                    copy['ruster_instance']=uuid.uuid4().hex
                    clear_copy_link(copy)
                    if obj.get('ruster_prefab_source'):
                        text=bpy.data.texts.get(obj['ruster_prefab_source'])
                        if not text:raise ValueError('Imported prefab source metadata is missing')
                        text=text.copy();texts.append(text);copy['ruster_prefab_source']=text.name
                for obj,copy in copies.items():
                    copy.parent=copies.get(obj.parent,obj.parent);copy.matrix_world=originals[obj]
                bpy.context.view_layer.update()
                root=copies[source_tree[0]];desired=delta@originals[source_tree[0]]
                location,rot,scale=desired.decompose()
                bridge.set_transform(root,list(location),[math.degrees(v) for v in rot.to_euler('XYZ')],list(scale))
                roots.append(root)
        placement.select(roots)
        return roots,n*count
    except Exception:
        for obj in reversed(created):bpy.data.objects.remove(obj,do_unlink=True)
        for text in texts:
            if text.users==0:bpy.data.texts.remove(text)
        bpy.context.view_layer.update();raise


def align(objects,axis,mode,target,bridge):
    bpy.context.view_layer.update()
    if axis not in {'X','Y','Z'} or mode not in {'MIN','CENTER','MAX'}:raise ValueError('Use axis X/Y/Z and mode MIN/CENTER/MAX')
    if any(not o.instance_collection and o.get('ruster_marker')!='SOUND' for o in prefabs_in(objects)):
        raise ValueError('Load original model geometry for every target before bounds alignment')
    index='XYZ'.index(axis);hierarchy=mcp_building.hierarchy();cache={}
    boxes=[mcp_building.object_bounds(o,hierarchy,cache) for o in objects]
    if any(box is None for box in boxes):raise ValueError('Load original model geometry for every target before bounds alignment')
    def coordinate(box):return (box['min'][index]+box['max'][index])/2 if mode=='CENTER' else box['min' if mode=='MIN' else 'max'][index]
    target=coordinate(boxes[0]) if target is None else bridge.vector([target,0,0],'Alignment target')[0]
    before={obj:placement.transform_state(obj) for obj in objects}
    try:
        for obj,box in zip(objects,boxes):
            pos=obj.matrix_world.translation.copy();pos[index]+=target-coordinate(box)
            bridge.set_transform(obj,position=list(pos))
    except Exception:
        for obj,state in before.items():placement.restore_transform(obj,state)
        bpy.context.view_layer.update();raise
    return target


def export_model_files(target,format_,roots,overwrite):
    from . import model_export
    target.parent.mkdir(parents=True,exist_ok=True)
    # Export all companion files into an isolated directory first. This keeps
    # an exporter failure away from existing user files and checks sidecars.
    with tempfile.TemporaryDirectory(prefix='.ruster-model-',dir=target.parent) as directory:
        staging=Path(directory);output=staging/'output';output.mkdir()
        model_export.export_models(output/target.name,format_,roots)
        files=[path for path in output.rglob('*') if path.is_file()]
        if len(files)>10000:raise ValueError('Model export produced too many companion files')
        pairs=[(source,target.parent/source.relative_to(output)) for source in files]
        for _,destination in pairs:
            if not destination.resolve().is_relative_to(target.parent.resolve()):
                raise ValueError('Model companion destination escapes the selected export directory')
            if destination.exists() and not destination.is_file():
                raise ValueError('Model companion destination is a directory; choose a different export name')
        if not overwrite and any(destination.exists() for _,destination in pairs):
            raise ValueError('A model or companion file already exists; choose a new name or explicitly enable overwrite')
        backups=[];installed=[]
        try:
            for source,destination in pairs:
                destination.parent.mkdir(parents=True,exist_ok=True)
                if destination.exists():
                    backup=staging/'backup'/source.relative_to(output);backup.parent.mkdir(parents=True,exist_ok=True)
                    os.replace(destination,backup);backups.append((backup,destination))
                os.replace(source,destination);installed.append(destination)
        except Exception:
            for destination in reversed(installed):destination.unlink(missing_ok=True)
            for backup,destination in reversed(backups):os.replace(backup,destination)
            raise
    return target


def io_settings(name,bridge):
    from . import studio
    found=studio.io_source(bridge.map_object(name))
    if not found:raise ValueError('Choose an imported prefab with existing RustEdit IO data')
    return studio,found


def dispatch(server,kind,p,bridge):
    if kind=='get_objects_info':
        objects=mcp_building.object_names(p['names'],bridge)
        include=p.get('include_bounds',True)
        if type(include)!=bool:raise ValueError('include_bounds must be boolean')
        hierarchy=mcp_building.hierarchy();cache={}
        return {'objects':[bridge.object_info(server,o.name,include,hierarchy,cache) for o in objects]}
    if kind=='get_prefabs_details':
        ids=p['uids']
        if not isinstance(ids,list) or not 1<=len(ids)<=20:raise ValueError('Provide 1–20 prefab IDs')
        return {'prefabs':[mcp_building.details(uid) for uid in ids]}
    if kind=='get_selection':
        objects=[o for o in bpy.context.selected_objects if o.get('ruster_kind') in {'PREFAB','GROUP'}]
        limit=mcp_building.bounded_int(p.get('limit',100),1,200,'Limit');offset=mcp_building.bounded_int(p.get('offset',0),0,2**31-1,'Offset')
        objects.sort(key=lambda o:o.name);hierarchy=mcp_building.hierarchy()
        return {'total':len(objects),'active':bpy.context.object.name if bpy.context.object else None,
            'objects':[bridge.object_info(server,o.name,False,hierarchy) for o in objects[offset:offset+limit]],
            'next_offset':offset+limit if offset+limit<len(objects) else None}
    if kind=='get_export_formats':
        from importlib.util import find_spec
        from . import model_export
        return {'formats':[{'id':key,'extension':model_export.FORMATS[key][1],'label':label} for key,label,_ in model_export.available_formats()],
            'placement_formats':['.prefab','.asset','.rusterprefab','.map'],'report_formats':['.docx'] if find_spec('docx') else [],
            'model_budget':{'unique_vertices':2000000,'mesh_instances':50000}}
    if kind=='get_io_settings':
        studio,found=io_settings(p['name'],bridge)
        return {'name':p['name'],'settings':{name:studio.io_value(found[-1],name) for name in studio.IO_SETTINGS},'wiring_editable':False}
    bridge.require_idle();bridge.require_editor()
    if kind=='set_io_settings':
        studio,found=io_settings(p['name'],bridge);values=p['settings']
        if not isinstance(values,dict) or not values or set(values)-studio.IO_SETTINGS.keys():raise ValueError('Provide known imported IO scalar settings')
        for name,value in values.items():
            _,type_,_=studio.IO_SETTINGS[name];prop=bpy.ops.ruster.io_settings.get_rna_type().properties[name]
            if type_=='str':
                if not isinstance(value,str) or len(value)>256:raise ValueError('IO text must contain at most 256 characters')
            elif type_=='bool':
                if type(value)!=bool:raise ValueError(name+' must be boolean')
            elif type_=='int':
                if type(value)!=int or not prop.hard_min<=value<=prop.hard_max:raise ValueError('Invalid IO '+name)
            elif isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not prop.hard_min<=value<=prop.hard_max:raise ValueError('Invalid IO '+name)
        text,source,index,entries,entry_index,entity=found
        for name,value in values.items():
            number,type_,_=studio.IO_SETTINGS[name]
            raw=value.encode('utf8') if type_=='str' else struct.pack('<f',value) if type_=='float' else int(value)&0xffffffffffffffff
            entity=wire.replace(entity,number,2 if type_=='str' else 5 if type_=='float' else 0,raw)
        entries[entry_index]=wire.encode(1,2,wire.serialize(entity));source.fields[index]=wire.encode(5,2,wire.serialize(entries))
        encoded=base64.b64encode(source.write()).decode('ascii');text.clear();text.write(encoded)
        result={'name':p['name'],'settings':{name:studio.io_value(entity,name) for name in studio.IO_SETTINGS}}
    elif kind=='import_map_prefabs':
        from . import map_prefabs
        target=bridge.file_target(p['path'],'.map',True)
        if not target.is_file() or target.stat().st_size>256*1024*1024:raise ValueError('Choose an existing .map of at most 256 MiB')
        created=map_prefabs.import_map(target,replace=False,max_prefabs=MAX_NEW)
        result={'prefab_count':len(created),'objects':[o.name for o in created[:200]],'objects_truncated':len(created)>200}
    elif kind=='import_prefab':
        from . import studio
        from .core.rustedit_prefab import RustEditPrefab
        target=bridge.file_target(p['path'],'.prefab',True);data=RustEditPrefab.load(target)
        if len(data.prefabs)>MAX_NEW:raise ValueError('MCP imports support at most 2000 prefab placements')
        position=Vector(bridge.vector(p.get('position',[0,0,0]),'Import position'));cursor=bpy.context.scene.cursor.location.copy()
        try:
            bpy.context.scene.cursor.location=position;root=studio.import_prefab(target)
        finally:bpy.context.scene.cursor.location=cursor
        result={'group':bridge.object_info(server,root.name,False),'prefab_count':len(data.prefabs)}
    else:
        objects=mcp_building.object_names(p['names'],bridge);mcp_building.independent(objects)
        if kind in {'export_prefab','export_models'}:
            with preserved_selection():
                placement.select(objects)
                if kind=='export_prefab':
                    from . import studio
                    from .core.rustedit_terrain import EXPORT_EXTENSIONS
                    overwrite=p.get('overwrite') is True
                    target=bridge.file_target(p['path'],EXPORT_EXTENSIONS,overwrite)
                    thumbnail=p.get('preview_png')
                    if thumbnail is not None:thumbnail=bridge.file_target(thumbnail,'.png',True)
                    files=studio.export_prefab(target,include_terrain=p.get('include_terrain'),
                        terrain_width=p.get('terrain_width',0),terrain_fade=p.get('terrain_fade',5),
                        preview_png=thumbnail,overwrite=overwrite)
                    return {'path':str(files[-1]),'companions':[str(file) for file in files[:-1]]}
                else:
                    from . import model_export
                    format_=p.get('format','GLB')
                    if format_ not in model_export.FORMATS:raise ValueError('Choose a format from get_export_formats')
                    target=bridge.file_target(p['path'],model_export.FORMATS[format_][1],p.get('overwrite') is True)
                    target=export_model_files(target,format_,prefabs_in(objects),p.get('overwrite') is True)
            return {'path':str(target)}
        if kind=='select_objects':placement.select(objects);return {'selected':[o.name for o in objects]}
        if kind=='delete_objects':
            from .construction import deletion_roots
            objects=deletion_roots(objects)
            mcp_building.independent(objects)
            names=[o.name for o in objects];all_objects=[o for tree in trees(objects) for o in tree]
            for obj in all_objects:bridge.map_object(obj.name)
            # All targets and hierarchies have been validated before deletion.
            for obj in reversed(all_objects):bpy.data.objects.remove(obj,do_unlink=True)
            result={'deleted':names,'deleted_objects':len(all_objects)}
        elif kind in {'duplicate_objects','array_objects'}:
            offset=bridge.vector(p.get('offset',[2,0,0]),'Copy offset')
            rotation=bridge.vector(p.get('rotation_degrees',[0,0,0]),'Rotation increment')
            count=mcp_building.bounded_int(p.get('count',1),1,MAX_NEW,'Copy count') if kind=='array_objects' else 1
            with preserved_selection():roots,n=clone_arrays(objects,count,offset,rotation,bridge)
            result={'objects':[o.name for o in roots[:200]],'root_count':len(roots),'new_prefab_count':n,'objects_truncated':len(roots)>200}
        elif kind in {'align_objects','snap_objects_to_plane'}:
            from .mcp_blender import editable_roots
            objects=editable_roots(p['names'],bridge)
            target=align(objects,p.get('axis','Z'),p.get('mode','MIN'),p.get('target',0) if kind=='snap_objects_to_plane' else p.get('target'),bridge)
            result={'objects':mcp_building.object_infos(server,objects,bridge,False),'target':target}
        else:raise ValueError('Unsupported prefab operation')
    if bpy.context.preferences.edit.use_global_undo:bpy.ops.ed.undo_push(message='MCP: '+kind.replace('_',' ').title())
    app.redraw();return result
