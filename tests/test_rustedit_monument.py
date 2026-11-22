import json
import struct

import numpy as np
import pytest

from ruster.core import legacy_lz4, rustedit_monument, rustedit_terrain, wire
from ruster.core.ground import flat_ground
from ruster.core.rust_map import update_transform
from ruster.core.terrain import Terrain


def contents(encoded):
    fields = wire.parse(legacy_lz4.decompress(encoded))
    entries = [wire.parse(field.value) for field in fields if field.number == 4]
    return fields, {wire.get(entry, 1).decode(): wire.get(entry, 2) for entry in entries}


def test_monument_embeds_placements_and_crops_all_terrain_layers():
    terrain = Terrain(flat_ground(size=56, height=8, biome='Jungle'))
    terrain.grid('alpha')[0, 254:258, 254:258] = 0
    stamp = rustedit_monument.capture(terrain, (0, 0, 10), 64, 62, 4)
    record = update_transform([wire.encode(1, 2, b'Decor'), wire.encode(2, 0, 123)],
                              (1, 2, 3), (0, 90, 0), (1, 2, 1))
    encoded = rustedit_monument.encode([record], 64, stamp=stamp, prefab_paths={123: 'assets/test.prefab'})
    fields, data = contents(encoded)

    assert [wire.get(fields, i) for i in range(1, 4)] == [64, 513, 512]
    root, = json.loads(data['entities'])
    placement, = root['Children']
    assert placement['ParentUid'] == root['UniqueId']
    assert placement['PrefabID'] == 123
    assert placement['Position'] == dict(x=1, y=2, z=3)
    assert placement['Rotation'] == dict(x=0, y=90, z=0)
    assert placement['Scale'] == dict(x=1, y=2, z=1)
    assert json.loads(data['prefabrecords']) == [{'Id': 123, 'Path': 'assets/test.prefab'}]
    assert struct.unpack('<3f', data['CollectionPivot']) == (0, 0, 0)
    assert all(root['Data']['CollectionSettings']['Save' + layer] for layer in rustedit_monument.TERRAIN_FIELDS)

    resolution = stamp.resolution
    inside = (350, 256)
    hole = (256, 256)
    outside = (256, 486)
    blend = np.frombuffer(data['collectionBlend'], dtype='u1').reshape(resolution, resolution)
    alpha = np.frombuffer(data['collectionAlpha'], dtype='u1').reshape(resolution, resolution)
    biome = np.frombuffer(data['collectionBiome'], dtype='u1').reshape(5, resolution, resolution)
    splat = np.frombuffer(data['collectionSplat'], dtype='u1').reshape(8, resolution, resolution)
    topology = np.frombuffer(data['collectionTopology'], dtype='<u4').reshape(resolution, resolution)
    height = np.frombuffer(data['collectionHeight'], dtype='<i2').reshape(resolution + 1, resolution + 1)
    assert (blend[inside], blend[hole], blend[outside]) == (255, 255, 0)
    assert (alpha[inside], alpha[hole], alpha[outside]) == (255, 0, 0)
    assert (biome[4][inside], biome[4][outside]) == (255, 0)
    assert splat[4][inside] == 255
    assert topology[inside] & (1 << 5)
    assert topology[outside] == 0
    assert (int(height[inside]) - 16383) * 1000 / 32766 == pytest.approx(-2, abs=.04)
    base = wire.parse(wire.get(wire.parse(data['terrainScene']), 1))
    assert wire.get(base, 1) == b'Base Layer'
    assert wire.get(base, 5) == data['collectionHeight']


def test_legacy_companions_convert_in_pivot_space_and_retain_the_circular_fade():
    terrain = Terrain(flat_ground(height=7, biome='Jungle'))
    original = rustedit_terrain.capture(terrain, (0, 0, 10), 32)
    restored, layers = rustedit_terrain.decode_companions(original, 32)
    assert layers == set(rustedit_monument.TERRAIN_FIELDS)
    assert restored.heights()[0, 0] == pytest.approx(-3, abs=.04)
    assert restored.biome_weights()[4, 0, 0] == 255
    stamp = rustedit_monument.capture(restored, (0, 0, 0), 64, 32, 4, circular=True, enabled=layers)
    blend = np.frombuffer(stamp.blend, dtype='u1').reshape(stamp.resolution, stamp.resolution)
    assert blend[256, 256] == 255
    assert 0 < blend[256, 370] < 255
    assert blend[145, 145] == 0


def test_geometry_only_monument_has_an_editable_base_without_a_terrain_stamp():
    _, data = contents(rustedit_monument.encode([[wire.encode(2, 0, 123)]], 64))
    root, = json.loads(data['entities'])
    assert 'terrainScene' in data
    assert not any(key.startswith('collection') for key in data)
    assert not any(root['Data']['CollectionSettings']['Save' + layer] for layer in rustedit_monument.TERRAIN_FIELDS)
