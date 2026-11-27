"""Render one cached prefab in a disposable background Blender scene."""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def render(cache, uid, target):
    import bpy
    from mathutils import Vector
    from ruster import scene
    from ruster.assets import THUMBNAIL_SIZE
    from ruster.core import wire
    from ruster.model_geometry import placement_meshes

    source = cache / 'prefabs' / f'{uid}.json.gz'
    modified = source.stat().st_mtime_ns
    scene.cache_dir = lambda: cache
    bpy.ops.wm.read_factory_settings(use_empty=True)
    world = bpy.context.scene
    root = scene.root_object([wire.encode(2, 0, uid)], world.collection)
    if not scene.attach_visual(root, transient=True) or not root.instance_collection:
        return False
    bpy.context.view_layer.update()

    # Frame each transformed mesh in camera space, including offset pivots.
    direction = Vector((1.35, -1.8, 1.05)).normalized()
    rotation = (-direction).to_track_quat('-Z', 'Y')
    view = rotation.inverted().to_matrix().to_4x4()
    low, high = [float('inf')] * 3, [float('-inf')] * 3
    for model, matrix in placement_meshes(root):
        transform = view @ matrix
        for corner in model.bound_box:
            point = transform @ Vector(corner)
            for axis in range(3):
                low[axis] = min(low[axis], point[axis])
                high[axis] = max(high[axis], point[axis])
    extent = max(high[0] - low[0], high[1] - low[1])
    if extent <= 0:
        return False
    center = (Vector(low) + Vector(high)) * .5
    distance = (high[2] - low[2]) * .5 + extent
    camera = bpy.data.cameras.new('Thumbnail')
    camera.type = 'ORTHO'
    camera.ortho_scale = extent * 1.12
    camera.clip_start = max(.0001, extent * .001)
    camera.clip_end = distance * 2 + extent
    world.camera = bpy.data.objects.new('Thumbnail', camera)
    world.collection.objects.link(world.camera)
    world.camera.rotation_euler = rotation.to_euler()
    world.camera.location = rotation @ center + direction * distance

    world.render.engine = 'BLENDER_WORKBENCH'
    world.display.shading.light = 'STUDIO'
    world.display.shading.color_type = 'TEXTURE'
    world.display.shading.show_shadows = True
    world.render.film_transparent = True
    world.display.render_aa = 'FXAA'
    world.render.resolution_x = world.render.resolution_y = THUMBNAIL_SIZE
    world.render.resolution_percentage = 100
    world.render.image_settings.file_format = 'PNG'
    world.render.image_settings.color_mode = 'RGBA'
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(suffix='.png', prefix='.render-', dir=target.parent)
    os.close(fd)
    try:
        world.render.filepath = temporary
        bpy.ops.render.render(write_still=True)
        if modified != source.stat().st_mtime_ns:
            return False
        os.utime(temporary, ns=(modified, modified))
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


if __name__ == '__main__':
    from ruster.core.process_limits import protect_process, MIB
    cache, uid, target, parent = sys.argv[sys.argv.index('--') + 1:]
    protect_process(2048 * MIB, int(parent))
    render(Path(cache), int(uid), Path(target))
