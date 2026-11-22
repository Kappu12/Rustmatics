"""Write RustEdit 2 custom monuments using Rust prefab IDs and native terrain."""
from dataclasses import dataclass
import json
import math
import struct

import numpy as np

from . import legacy_lz4, wire
from .rust_map import prefab_id, prefab_transform
from .rustedit_prefab import validate_records
from .rustedit_terrain import BIOME_MASK, SPLAT_MASK, TOPOLOGY_MASK
from .terrain import resample
from .wire import FormatError


TERRAIN_FIELDS = ('Height', 'Splat', 'Biome', 'Topology', 'Alpha')
LAYER_MASKS = {'Splat': SPLAT_MASK, 'Biome': BIOME_MASK, 'Topology': TOPOLOGY_MASK}
CANVAS_SIZES = (64, 128, 256, 512, 1024, 2048)
BASE_HEIGHT = 16383
HEIGHT_UNITS = 32766 / 1000


@dataclass
class TerrainStamp:
    resolution: int
    layers: dict[str, bytes]
    enabled: frozenset[str]
    blend: bytes | None = None


def canvas_size(required_width):
    for size in CANVAS_SIZES:
        if required_width <= size:
            return size
    raise FormatError('RustEdit 2 monuments must fit within a 2,048 metre square around the export pivot')


def _flat_layers(resolution):
    height = np.full((resolution + 1, resolution + 1), BASE_HEIGHT, dtype='<i2')
    splat = np.zeros((8, resolution, resolution), dtype='u1')
    splat[4] = 255
    biome = np.zeros((5, resolution, resolution), dtype='u1')
    biome[1] = 255
    topology = np.zeros((resolution, resolution), dtype='<u4')
    alpha = np.full((resolution, resolution), 255, dtype='u1')
    return dict(zip(TERRAIN_FIELDS, (height, splat, biome, topology, alpha)))


def _ground_coverage(solid):
    """Exclude the void outside a terrain patch, retaining enclosed alpha holes."""
    if solid.all() or not solid.any():
        return solid
    # Flood the boundary-connected void in horizontal runs. This avoids
    # visiting every pixel individually on a large, mostly empty island crop.
    height, width = solid.shape
    void = np.pad(~solid, ((0, 0), (1, 1)))
    spans = [np.flatnonzero(row[1:] != row[:-1]).reshape(-1, 2) for row in void]
    pending = [(z, start, stop) for z, row in enumerate(spans) for start, stop in row
               if z in (0, height - 1) or start == 0 or stop == width]
    coverage = np.ones_like(solid)
    while pending:
        z, start, stop = pending.pop()
        if not coverage[z, start]:
            continue
        coverage[z, start:stop] = False
        for neighbor in (z - 1, z + 1):
            if 0 <= neighbor < height:
                runs = spans[neighbor]
                first = np.searchsorted(runs[:, 1], start, side='right')
                last = np.searchsorted(runs[:, 0], stop, side='left')
                pending.extend((neighbor, left, right) for left, right in runs[first:last]
                               if coverage[neighbor, left])
    return coverage


def capture(terrain, origin, size, width, fade, *, circular=False, enabled=TERRAIN_FIELDS):
    """Sample existing terrain in editor metres relative to the monument pivot."""
    if not 0 < width <= size or not 0 <= fade < width / 2:
        raise FormatError('Terrain fade must be smaller than half the stamp width')
    resolution = 512
    source_res = terrain.grid('alpha').shape[-1]
    while resolution < size / terrain.size * source_res and resolution < 2048:
        resolution *= 2
    cells = (np.arange(resolution) + .5) * size / resolution - size / 2
    reach = (np.hypot(cells[:, None], cells) if circular
             else np.maximum(abs(cells[:, None]), abs(cells)))
    border = width / 2 - reach

    def coordinates(grid, positions):
        x, y = (((coordinate + positions) / terrain.size + .5) * (grid.shape[-1] - 1)
                for coordinate in origin[:2])
        return x, y

    def nearest(grid):
        x, y = coordinates(grid, cells)
        x, y = (np.rint(axis).astype(int).clip(0, grid.shape[-1] - 1) for axis in (x, y))
        return grid[..., y[:, None], x]

    alpha = nearest(terrain.grid('alpha')[0])
    in_world = ((abs(cells + origin[0]) <= terrain.size / 2)[None, :]
                & (abs(cells + origin[1]) <= terrain.size / 2)[:, None])
    coverage = in_world if circular else _ground_coverage((alpha > 0) & in_world) & in_world
    fade_strength = np.clip(border / fade, 0, 1) if fade else (border > 0).astype(float)
    blend = np.rint(255 * fade_strength ** 2 * (3 - 2 * fade_strength)).astype('u1')
    blend[~coverage] = 0
    inside = blend > 0
    layers = _flat_layers(resolution)
    layers['Alpha'][:] = np.where(inside, alpha, 0)
    if 'Height' in enabled:
        axis = np.linspace(-size / 2, size / 2, resolution + 1)
        source = terrain.grid('terrain')[0]
        x, y = coordinates(source, axis)
        for start in range(0, len(axis), 64):
            stop = start + 64
            block = np.rint(resample(source, x[None, :], y[start:stop, None]) - origin[2] * HEIGHT_UNITS)
            if block.min() < 0 or block.max() > 32766:
                raise FormatError('Terrain heights exceed RustEdit 2’s range relative to the export pivot')
            layers['Height'][start:stop] = block.astype('<i2')
    for name, values in (('Splat', terrain.grid('splat')), ('Biome', terrain.biome_weights()),
                         ('Topology', terrain.grid('topology')[0])):
        if name in enabled:
            layers[name][..., inside] = nearest(values)[..., inside]
    return TerrainStamp(resolution, {name: array.tobytes() for name, array in layers.items()},
                        frozenset(enabled), blend.tobytes())


def _xyz(values=(0, 0, 0)):
    return dict(zip('xyz', values))


def _entity(uid, parent, index, kind, position, rotation, scale, **extra):
    return dict(UniqueId=uid, ParentUid=parent, SiblingIndex=index, Collapsed=False, NameOverride=None,
                Position=_xyz(position), Rotation=_xyz(rotation), Scale=_xyz(scale),
                Data={}, Children=None, Type=kind, **extra)


def _entities(records, size, enabled, masks):
    collection = len(records) + 1
    children = []
    for index, record in enumerate(records):
        position, rotation, scale = prefab_transform(record)
        children.append(_entity(index + 1, collection, index, 'Prefab', position, rotation, scale,
                                PrefabID=prefab_id(record), Category=wire.get(record, 1, b'Decor').decode('utf-8')))
    root = _entity(collection, None, 0, 'Entity', (0, 0, 0), (0, 0, 0), (1, 1, 1))
    root['Children'] = children
    root['Data'] = {
        'CollectionSettings': {
            'Version': '1.0.0', 'HasPivot': False,
            **{'Save' + name: name in enabled for name in TERRAIN_FIELDS},
            **{name + 'Mask': masks.get(name, default) for name, default in LAYER_MASKS.items()},
        },
        'MapGeneration': {'Enabled': False, 'ResourceFolder': 'monument/medium'},
        'MonumentInfo': {
            'Type': 2, 'Tier': 7,
            'Bounds': {'m_Center': _xyz(), 'm_Extent': _xyz((size / 2, 500, size / 2))},
            'shouldDisplayOnMap': True,
            'displayPhrase': {'token': '', 'english': ''},
        },
    }
    return [root]


def _json_bytes(data):
    return json.dumps(data, separators=(',', ':'), allow_nan=False).encode('utf-8')


def encode(records, size, *, stamp=None, masks=None, prefab_paths=None):
    """Serialize the measured RustEdit 2 monument envelope and one base layer."""
    validate_records(records)
    if size not in CANVAS_SIZES:
        raise FormatError('Invalid RustEdit 2 monument size')
    if stamp is None:
        resolution = 512
        stamp = TerrainStamp(resolution, {name: array.tobytes() for name, array in _flat_layers(resolution).items()},
                             frozenset())
    resolution = stamp.resolution
    paths = prefab_paths or {}
    data = {
        'entities': _json_bytes(_entities(records, size, stamp.enabled, masks or {})),
        'prefabrecords': _json_bytes([{'Id': uid, 'Path': paths[uid]} for uid in dict.fromkeys(map(prefab_id, records))
                                     if uid in paths]),
        'CollectionPivot': struct.pack('<3f', 0, 0, 0),
    }
    for name in TERRAIN_FIELDS:
        if name in stamp.enabled:
            data['collection' + name] = stamp.layers[name]
    if stamp.blend is not None:
        data['collectionBlend'] = stamp.blend
    base = [wire.encode(1, 2, b'Base Layer'), wire.encode(2, 0, 1),
            wire.encode(4, 5, struct.pack('<f', 1))]
    base.extend(wire.encode(number, 2, stamp.layers[name]) for number, name in enumerate(TERRAIN_FIELDS, 5))
    data['terrainScene'] = wire.encode(1, 2, wire.serialize(base)).raw
    data['validator'] = b'validator'
    fields = [wire.encode(1, 0, size), wire.encode(2, 0, resolution + 1), wire.encode(3, 0, resolution)]
    for name, payload in data.items():
        fields.append(wire.encode(4, 2, wire.serialize([wire.encode(1, 2, name.encode()), wire.encode(2, 2, payload)])))
    return legacy_lz4.compress(wire.serialize(fields), compact=True)
