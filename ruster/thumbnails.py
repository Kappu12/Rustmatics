"""Local images shared by native Add search, the sidebar, and prefab details."""
import os
import sqlite3
from pathlib import Path
import subprocess
import time

import bpy
import bpy.utils.previews

from . import scene
from .assets import THUMBNAIL_SIZE
from .assets.metadata import rustedit_root
from .core import palettes, settings

_previews = None
_scanner = None
_pending = {}
_checked = set()
_job = None
_folder = Path('thumbnails') / str(THUMBNAIL_SIZE)


def request(uid):
    """Only visible sidebar rows, inspected, or recently used prefabs need renders."""
    key = str(uid)
    if _previews is not None and key not in _checked:
        _pending[key] = None


def converted(uid):
    key = str(uid)
    _checked.discard(key)
    request(key)


def icon_id(uid, asset_path):
    if _previews is None:
        return 0
    image = _previews.get(asset_path.casefold()) or _previews.get(str(uid))
    return image.icon_id if image is not None else 0


def _load(key, path):
    if key in _previews:
        del _previews[key]
    try:
        preview = _previews.load(key, str(path), 'IMAGE')
        # Decode our tiny generated icon on the idle tick. Native search can
        # otherwise keep Blender's loading glyph until the next popup redraw.
        if key.isdecimal() and not preview.icon_size[0]:
            del _previews[key]
    except (OSError, RuntimeError):
        pass


def _index(editor, cache):
    if editor:
        folder = editor / 'PreviewImages'
        for directory, _, files in os.walk(folder):
            for name in files:
                if name.lower().endswith('.prefab.png'):
                    path = Path(directory) / name
                    key = path.relative_to(folder).as_posix()[:-4].casefold()
                    _load(key, path)
                    yield
            yield
    for path in (cache / _folder).glob('*.png'):
        if path.stem.isdecimal() and _current(cache, path.stem, path):
            _load(path.stem, path)
        yield


def _current(cache, uid, png):
    try:
        return png.stat().st_mtime_ns == (cache / 'prefabs' / f'{uid}.json.gz').stat().st_mtime_ns
    except OSError:
        return False


def _stop_worker():
    global _job
    if _job:
        process, _, _, _ = _job
        if process.poll() is None:
            process.kill()
        process.wait()
        _job = None


def refresh():
    global _scanner, _previews
    _stop_worker()
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
    _previews = bpy.utils.previews.new()
    config = settings.load()
    _scanner = _index(rustedit_root(config.get('rustedit_install')), scene.cache_dir())
    _checked.clear()
    _pending.clear()
    for uid in palettes.recent_ids(config) + palettes.favorite_ids(config):
        request(uid)


def _prepare(cache, uid):
    global _job
    from .assets import catalog
    target = cache / _folder / f'{uid}.png'
    if _current(cache, uid, target):
        if uid not in _previews:
            _load(uid, target)
        return
    if uid in _previews:
        del _previews[uid]
    if not (cache / 'prefabs' / f'{uid}.json.gz').is_file():
        return
    row = catalog.find(cache, int(uid))
    if not row or row[0].casefold() in _previews:
        return
    command = [bpy.app.binary_path, '-b', '--factory-startup', '-t', '2',
               '--disable-autoexec', '--python-exit-code', '1', '--python',
               str(Path(__file__).parent / 'assets' / 'thumbnail_worker.py'),
               '--', str(cache), uid, str(target), str(os.getpid())]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with (cache / 'thumbnail-worker.log').open('w') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=flags)
    _job = process, uid, target, time.monotonic()


def tick():
    global _scanner
    from . import app
    cache = scene.cache_dir()
    deadline = time.monotonic() + .004
    if _scanner is not None:
        for _ in _scanner:
            if time.monotonic() >= deadline:
                return .03
        _scanner = None
        app.redraw()
    if _job:
        process, uid, target, started = _job
        if process.poll() is None and time.monotonic() - started < 120:
            return .3
        _stop_worker()
        if _current(cache, uid, target):
            _load(uid, target)
            app.redraw()
    while _pending and _job is None:
        uid = next(iter(_pending))
        del _pending[uid]
        _checked.add(uid)
        try:
            _prepare(cache, uid)
        except sqlite3.Error:
            converted(uid)
            return 1.
        except OSError:
            # A missing/read-only cache must not interrupt the Add menu.
            pass
        if time.monotonic() >= deadline:
            return .05
    return .5


def register():
    if not bpy.app.background:
        refresh()
        bpy.app.timers.register(tick, first_interval=.1, persistent=True)


def unregister():
    global _scanner, _previews
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    _stop_worker()
    _scanner = None
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
    _checked.clear()
    _pending.clear()
