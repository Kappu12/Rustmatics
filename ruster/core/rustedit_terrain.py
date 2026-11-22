"""RustEdit 1 companion images for a custom prefab."""
from contextlib import contextmanager
import math
import os
from io import BytesIO
from pathlib import Path
import struct
import tempfile
import zlib

import numpy as np

from .layer_images import MAX_IMAGE_PIXELS
from .terrain import BIOMES, SPLATS, TOPOLOGIES, Terrain, normalize_weights, resample
from .wire import FormatError

TERRAIN_SUFFIXES = ('.heights', '.splat0', '.splat1', '.biome', '.alpha', '.topology')
EXPORT_EXTENSIONS = ('.prefab', '.asset')
SUFFIXES = (*TERRAIN_SUFFIXES, '.png')
SPLAT_MASK = (1 << len(SPLATS)) - 1
BIOME_MASK = (1 << len(BIOMES)) - 1
TOPOLOGY_MASK = (1 << len(TOPOLOGIES)) - 1
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
MAX_IMAGE_BYTES = MAX_IMAGE_PIXELS * 4 + 1024 * 1024


def _rgba_png(pixels):
    """Encode data channels as RGBA8 PNG without colour space conversion."""
    height, width, _ = pixels.shape
    scanlines = np.zeros((height, width * 4 + 1), dtype='u1')
    # Rust grids start in the south; Unity's PNG starts in the north.
    scanlines[:, 1:] = pixels[::-1].reshape(height, width * 4)

    def chunk(kind, data):
        content = kind + data
        return struct.pack('>I', len(data)) + content + struct.pack('>I', zlib.crc32(content))

    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(scanlines)) + chunk(b'IEND', b'')


def capture(terrain, origin, width):
    """Sample a square around an editor-space X, Y, height pivot."""
    if type(width) is not int or not 2 <= width <= 32768:
        raise FormatError('Choose a terrain width from 2 to 32768 metres')
    if len(origin) != 3 or not all(math.isfinite(n) for n in origin):
        raise FormatError('Terrain export needs a finite prefab pivot')

    def sample_axes(grid):
        resolution = grid.shape[-1]
        count = min(math.isqrt(MAX_IMAGE_PIXELS), max(2, round(width / terrain.size * resolution)))
        offsets = ((np.arange(count) + .5) / count - .5) * width
        return tuple(((centre + offsets) / terrain.size + .5) * (resolution - 1)
                     for centre in origin[:2])

    def crop(grid):
        x, y = (np.rint(axis).astype(int).clip(0, grid.shape[-1] - 1)
                for axis in sample_axes(grid))
        return grid[..., y[:, None], x]

    masks = {}
    raw_height = terrain.grid('terrain')[0]
    x, y = sample_axes(raw_height)
    samples = np.rint(resample(raw_height, x[None, :], y[:, None])
                      - (origin[2] + 500) * (32766 / 1000))
    if samples.min() < -32768 or samples.max() > 32767:
        raise FormatError('Terrain heights are out of range relative to the prefab pivot')
    samples = samples.astype('<i2').view('<u2')
    heights = np.zeros((*samples.shape, 4), dtype='u1')
    heights[..., 0] = samples & 255
    heights[..., 2] = samples >> 8
    heights[..., 3] = 1
    masks['.heights'] = _rgba_png(heights)

    splat = crop(terrain.grid('splat'))
    for index in (0, 1):
        masks[f'.splat{index}'] = _rgba_png(np.moveaxis(splat[index * 4:index * 4 + 4], 0, -1))
    # RustEdit reconstructs Jungle as one minus the other four weights.
    masks['.biome'] = _rgba_png(np.moveaxis(crop(terrain.grid('biome')[:4]), 0, -1))
    alpha = crop(terrain.grid('alpha')[0])
    holes = np.zeros((*alpha.shape, 4), dtype='u1')
    holes[..., 3] = alpha
    masks['.alpha'] = _rgba_png(holes)
    topology = (crop(terrain.grid('topology')[0]) & TOPOLOGY_MASK).astype('<u4')
    masks['.topology'] = _rgba_png(topology.view('u1').reshape(*topology.shape, 4))
    return masks


def read_png(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
        raise FormatError(f'Choose a PNG image of at most 65 MiB for {path.name}')
    return _checked_png(path.read_bytes(), path.name)


def _checked_png(data, name):
    if (len(data) < 33 or len(data) > MAX_IMAGE_BYTES or
            data[:16] != PNG_SIGNATURE + b'\x00\x00\x00\x0dIHDR'):
        raise FormatError(f'{name} is not a supported PNG image')
    width, height = struct.unpack_from('>II', data, 16)
    if not width or not height or width * height > MAX_IMAGE_PIXELS:
        raise FormatError(f'{name} must have at most 16 megapixels')
    return data


def _companion(path, suffix):
    return path.with_name(path.name + suffix)


def read_companions(path):
    """Keep the original PNG bytes so saved projects can re-export them."""
    return {suffix: read_png(image) for suffix in SUFFIXES
            if (image := _companion(Path(path), suffix)).is_file()}


def decode_companions(companions, width):
    """Decode a legacy stamp to relative native terrain and the layers it supplies."""
    try:
        from PIL import Image
    except ImportError as error:
        raise FormatError('Install Rustmatics asset dependencies to convert legacy terrain images') from error
    from . import wire
    from .rust_map import RustMap

    images = {}
    for suffix in TERRAIN_SUFFIXES:
        if suffix in companions:
            with Image.open(BytesIO(_checked_png(companions[suffix], suffix))) as image:
                if image.width != image.height or image.width < 2 or image.n_frames != 1:
                    raise FormatError(f'{suffix} must be a square, single-frame RustEdit terrain image')
                images[suffix] = np.array(image.convert('RGBA'))[::-1]

    world = RustMap(10, b'', [wire.encode(1, 0, width)])
    enabled = set()
    resolution = max((len(samples) for samples in images.values()), default=2)

    def pixels(suffix):
        samples = images.get(suffix)
        if samples is None:
            return np.zeros((resolution, resolution, 4), dtype='u1')
        if len(samples) == resolution:
            return samples
        indices = np.rint(np.linspace(0, len(samples) - 1, resolution)).astype(int)
        return samples[indices[:, None], indices]

    height = np.full((2, 2), 16383, dtype='<i2')
    if '.heights' in images:
        data = images['.heights'].astype('<u2')
        height = (data[..., 0] | (data[..., 2] << 8)).view('<i2').astype('i4') + 16383
        if height.min() < 0 or height.max() > 32766:
            raise FormatError('Legacy terrain heights exceed RustEdit 2’s range relative to the export pivot')
        height = height.astype('<i2')
        enabled.add('Height')
    world.set_layer('terrain', height.tobytes())

    splat = np.concatenate((pixels('.splat0'), pixels('.splat1')), axis=-1).transpose(2, 0, 1)
    if {'.splat0', '.splat1'} & images.keys():
        enabled.add('Splat')
    else:
        splat[4] = 255
    if np.any(splat.sum(axis=0, dtype='u2') != 255):
        splat = normalize_weights(splat)
    world.set_layer('splat', splat.tobytes())

    biome = pixels('.biome').transpose(2, 0, 1)
    if '.biome' in images:
        enabled.add('Biome')
    else:
        biome[1] = 255
    # The four legacy bytes leave their remaining weight to Jungle.
    total = biome.sum(axis=0, dtype='i2')
    biome = np.concatenate((biome, np.maximum(255 - total, 0)[None].astype('u1')))
    if np.any(total > 255):
        biome = normalize_weights(biome)
    world.set_layer('biome', biome.tobytes())
    topology = np.ascontiguousarray(pixels('.topology')).view('<u4') & TOPOLOGY_MASK
    if '.topology' in images:
        enabled.add('Topology')
    world.set_layer('topology', topology.tobytes())
    alpha = np.full((resolution, resolution), 255, dtype='u1')
    if '.alpha' in images:
        alpha = np.where(pixels('.alpha')[..., 3] >= 128, 255, 0).astype('u1')
        enabled.add('Alpha')
    world.set_layer('alpha', alpha.tobytes())
    return Terrain(world), enabled


@contextmanager
def _destination_lock(path):
    lock = path.with_name(f'.{path.name}.ruster-export')
    with lock.open('xb'):
        pass
    try:
        yield
    finally:
        lock.unlink()


def write_bundle(path, body, companions, *, overwrite):
    """Publish a prefab and its images together, restoring old files on failure."""
    unknown = companions.keys() - set(SUFFIXES)
    if unknown:
        raise FormatError('Unknown RustEdit companion suffix')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    outputs = {_companion(path, suffix): _checked_png(data, suffix)
               for suffix, data in companions.items()}
    outputs[path] = body
    bundle = [*(_companion(path, suffix) for suffix in SUFFIXES), path]
    with _destination_lock(path):
        existing = [file for file in bundle if file.exists() or file.is_symlink()]
        if any(not file.is_file() for file in existing):
            raise FormatError('A prefab or companion path is not a file')
        if existing and not overwrite:
            raise FileExistsError('A prefab or companion already exists at this destination')
        with tempfile.TemporaryDirectory(prefix='.ruster-prefab-', dir=path.parent) as directory:
            staging = Path(directory)
            files = {}
            for i, (destination, content) in enumerate(outputs.items()):
                source = staging / f'new-{i}'
                source.write_bytes(content)
                files[destination] = source
            backups, published = {}, []
            try:
                for i, destination in enumerate(existing):
                    backup = staging / f'old-{i}'
                    os.replace(destination, backup)
                    backups[destination] = backup
                for destination, source in files.items():
                    os.replace(source, destination)
                    published.append(destination)
            except OSError:
                for destination in reversed(published):
                    destination.unlink()
                for destination, source in backups.items():
                    os.replace(source, destination)
                raise
    return list(outputs)
