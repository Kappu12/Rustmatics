import sqlite3
from contextlib import closing

from ruster.assets import catalog, thumbnail_library as library


def test_preload_skips_existing_art_and_resumes_with_rotated_source(tmp_path, monkeypatch):
    cache, game, editor = (tmp_path/name for name in ('cache', 'game', 'editor'))
    bundle = game/'Bundles/shared/assetscenes.bundle'
    bundle.parent.mkdir(parents=True)
    bundle.touch()
    (game/'Bundles/AssetSceneManifest.json').touch()
    names = [(uid, f'assets/props/model_{uid}.prefab') for uid in range(1, 6)]
    with closing(catalog.connect(cache)) as db, db:
        db.executemany('INSERT INTO names VALUES(?,?)', names)
        db.executemany('INSERT INTO assets VALUES(?,?,?,?)',
                       [(path, 'Bundles/shared/assetscenes.bundle', 'file', uid) for uid, path in names[:-1]])
    png = editor/'PreviewImages'/(names[0][1]+'.png')
    png.parent.mkdir(parents=True)
    png.touch()
    folder = library.directory(cache, game)
    folder.mkdir(parents=True)
    attempts = []

    def render(cache, scratch, destination, install, blender, uid, bundle):
        attempts.append(uid)
        assert (scratch/'catalog.sqlite3').is_file()
        if uid == 3:
            return 'empty'
        if uid == 4:
            raise RuntimeError('temporary extraction failure')
        (destination/f'{uid}.png').write_bytes(b'fixture')
        return 'ready'

    monkeypatch.setattr(library, 'render_one', render)
    library.build(cache, game, 'blender', folder, editor)
    assert attempts == [2, 3, 4]
    ready = library.ready_since(folder, 0, 8)
    assert [uid for _, uid in ready] == [2]
    assert library.ready_since(folder, ready[0][0], 8) == []
    assert not (folder/'work').exists()
    assert not (cache/'prefabs').exists()

    library.build(cache, game, 'blender', folder, editor)
    assert attempts == [2, 3, 4]
    (folder/'2.png').unlink()
    with sqlite3.connect(folder/'icons.sqlite3') as db:
        db.execute('UPDATE icons SET updated=0 WHERE uid=4')
    library.build(cache, game, 'blender', folder, editor)
    assert attempts == [2, 3, 4, 2, 4]
    assert library.ready_since(folder, ready[0][0], 8)[0][1] == 2
    bundle.write_bytes(b'new release')
    assert library.directory(cache, game) != folder
