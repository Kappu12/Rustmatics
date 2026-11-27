"""Build small library icons from installed assets without retaining converted models."""
from contextlib import closing, ExitStack
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ruster.assets import CONVERTER_VERSION, THUMBNAIL_SIZE, catalog
from ruster.assets.metadata import rustedit_root
from ruster.core.headless import ProfileLock
from ruster.core.project import atomic_write


def directory(cache, install):
    """A changed installation, game data build, or converter gets its own index."""
    parts = [str(install.resolve()), CONVERTER_VERSION, 'library-v1']
    for file in (install/'Bundles/AssetSceneManifest.json', install/'Bundles/shared/assetscenes.bundle'):
        stat = file.stat()
        parts.extend((stat.st_size, stat.st_mtime_ns))
    key = hashlib.sha256(repr(parts).encode()).hexdigest()[:20]
    return cache/'thumbnails'/str(THUMBNAIL_SIZE)/('library-' + key)


def ready_since(folder, cursor, limit):
    """Short read transactions let open editors pick up newly published PNGs."""
    dbfile = folder/'icons.sqlite3'
    if not dbfile.is_file():
        return []
    with closing(sqlite3.connect(dbfile.resolve().as_uri()+'?mode=ro', uri=True, timeout=0)) as db:
        return db.execute("SELECT sequence,uid FROM icons WHERE sequence>? AND state='ready' "
                          "ORDER BY sequence LIMIT ?", (cursor, limit)).fetchall()


def render_one(cache, scratch, folder, install, blender, uid, bundle):
    from ruster.assets.worker import bundle_inventory, run_child
    filename = f'{uid}.json.gz'
    source = cache
    stamp = cache/'prefabs'/f'{uid}.stamp'
    stat = (install/bundle).stat()
    prefix = f'{CONVERTER_VERSION}:{stat.st_size}:{stat.st_mtime_ns}:'
    if (cache/'prefabs'/filename).is_file() and stamp.is_file() and stamp.read_text().startswith(prefix):
        png = folder.parent/f'{uid}.png'
        if png.is_file() and png.stat().st_mtime_ns == (cache/'prefabs'/filename).stat().st_mtime_ns:
            atomic_write(folder/f'{uid}.png', png.read_bytes())
            return 'ready'
    else:
        source = scratch
        inventory = scratch/'bundles.json'
        if not inventory.is_file():
            atomic_write(inventory, json.dumps(bundle_inventory(install)).encode())
        run_child('extract-one', install, scratch, uid=uid, report=scratch/'result.json',
                  use_thumbnail_inventory=True)
    with gzip.open(source/'prefabs'/filename, 'rt', encoding='utf-8') as stream:
        model = json.load(stream)
    if not model.get('meshes') or model.get('marker') == 'VOLUME':
        return 'empty'
    target = folder/f'{uid}.png'
    cmd = [str(blender), '-b', '--factory-startup', '-t', '1', '--disable-autoexec',
           '--python-exit-code', '1', '--python', str(Path(__file__).with_name('thumbnail_worker.py')),
           '--', str(source), str(uid), str(target), str(os.getpid())]
    subprocess.run(cmd, check=True, timeout=120, stdin=subprocess.DEVNULL,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return 'ready' if target.is_file() else 'error'


def build(cache, install, blender, folder, editor):
    """The caller owns the generation lock, including its private extraction directory."""
    scratch = folder/'work'
    # An interrupted run may leave one unfinished conversion and its old inventory.
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(exist_ok=True)
    snapshot = scratch/'catalog.sqlite3'
    try:
        with closing(catalog.read_connection(cache)) as source, closing(sqlite3.connect(snapshot)) as copy:
            source.backup(copy)
            rows = copy.execute('SELECT n.uid,n.path,MIN(a.bundle) FROM names n JOIN assets a '
                                'ON n.path=a.path COLLATE NOCASE WHERE a.bundle IS NOT NULL '
                                'GROUP BY n.uid ORDER BY a.bundle,n.path').fetchall()
        cached_ids = {p.name.split('.')[0] for p in (cache/'prefabs').glob('*.json.gz')}
        rows.sort(key=lambda row: str(row[0]) not in cached_ids)
        artwork = set()
        if editor:
            previews = editor/'PreviewImages'
            artwork = {p.relative_to(previews).as_posix()[:-4].casefold() for p in previews.rglob('*.png')}
        with closing(sqlite3.connect(folder/'icons.sqlite3')) as index:
            index.execute('CREATE TABLE IF NOT EXISTS icons(sequence INTEGER PRIMARY KEY AUTOINCREMENT, '
                          'uid INTEGER UNIQUE NOT NULL, state TEXT NOT NULL, updated INTEGER NOT NULL)')
            previous = {uid: (state, updated) for uid, state, updated in
                        index.execute('SELECT uid,state,updated FROM icons')}
            for uid, path, bundle in rows:
                if path.casefold() in artwork:
                    continue
                state, updated = previous.get(uid, ('', 0))
                target = folder/f'{uid}.png'
                if state == 'empty' or (state == 'ready' and target.is_file()):
                    continue
                if state == 'error' and time.time() - updated < 3600:
                    continue
                try:
                    if target.is_file():
                        state = 'ready'  # Last run stopped between PNG publication and index commit.
                    else:
                        if (scratch/'prefabs').exists():
                            shutil.rmtree(scratch/'prefabs')
                        state = render_one(cache, scratch, folder, install, blender, uid, bundle)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    print(f'Thumbnail {uid}: {error}', flush=True)
                    state = 'error'
                with index:
                    index.execute('INSERT OR REPLACE INTO icons(uid,state,updated) VALUES(?,?,?)',
                                  (uid, state, int(time.time())))
    finally:
        shutil.rmtree(scratch)


def main():
    cache, install, blender, folder, editor, parent = sys.argv[1:]
    from ruster.core.process_limits import MIB, protect_process
    protect_process(3072*MIB, int(parent))
    if os.name != 'nt':
        os.nice(10)
    cache, install, folder = Path(cache), Path(install), Path(folder)
    if folder.resolve() != directory(cache, install).resolve():
        raise ValueError('Thumbnail cache does not match this game installation')
    with ExitStack() as stack:
        try:
            stack.enter_context(ProfileLock(folder))
        except ValueError:
            return 75
        build(cache, install, Path(blender), folder, rustedit_root(editor or None))
    return 0


if __name__ == '__main__':
    sys.exit(main())
