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
from .assets import thumbnail_library
from .assets.metadata import rustedit_root
from .core import palettes, settings

_previews = None
_scanner = None
_pending = {}
_checked = set()
_job = None
_folder = Path('thumbnails') / str(THUMBNAIL_SIZE)
_library = None
_library_icons = None
_library_process = None
_library_cursor = 0
_check_install_at = 0.
_next_pass = 0.


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
    if image is None and _library_icons is not None:
        image = _library_icons.get(str(uid))
    return image.icon_id if image is not None else 0


def _close_library():
    global _library, _library_icons, _library_cursor, _library_process, _next_pass, _check_install_at
    if _library_process is not None:
        if _library_process.poll() is None:
            _library_process.terminate()
        try:
            _library_process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _library_process.kill()
            _library_process.wait()
        _library_process = None
    if _library_icons is not None:
        bpy.utils.previews.remove(_library_icons)
        _library_icons = None
    _library = None
    _library_cursor = 0
    _next_pass = _check_install_at = 0.


def _poll_library():
    global _library, _library_icons, _library_cursor, _library_process, _next_pass, _check_install_at
    from . import app
    cache = scene.cache_dir()
    now = time.monotonic()
    if _library_process is not None and (code := _library_process.poll()) is not None:
        _library_process = None
        _next_pass = now + (3600 if code == 0 else 30)
    if now >= _check_install_at:
        config = settings.load()
        folder = None
        if config.get('rust_install') and (cache/'catalog.sqlite3').is_file():
            try:
                install = settings.validate_install(config['rust_install'])
                folder = thumbnail_library.directory(cache, install)
            except (OSError, ValueError):
                pass
        if folder != _library:
            _close_library()
            _library = folder
            if folder:
                _library_icons = bpy.utils.previews.new()
        _check_install_at = now + 5.
        if folder and _library_process is None and now >= _next_pass and app._job is None:
            folder.mkdir(parents=True, exist_ok=True)
            command = [app.worker_python(), str(Path(thumbnail_library.__file__)),
                       str(cache), str(install), bpy.app.binary_path, str(folder),
                       config.get('rustedit_install') or '', str(os.getpid())]
            flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == 'nt' else 0
            with (folder/'worker.log').open('a', encoding='utf-8') as log:
                _library_process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                                    stderr=subprocess.STDOUT, creationflags=flags)
    if _library:
        rows = thumbnail_library.ready_since(_library, _library_cursor, 12)
        for seq, uid in rows:
            _library_cursor = seq
            key = str(uid)
            if key in _library_icons:
                del _library_icons[key]
            image = _library_icons.load(key, str(_library/f'{uid}.png'), 'IMAGE')
            if not image.icon_size[0]:
                del _library_icons[key]
            if time.monotonic() - now >= .004:
                break
        if rows:
            app.redraw()
            return .05
    return 1.


def preload_library():
    try:
        return _poll_library()
    except (sqlite3.Error, OSError, ValueError, RuntimeError):
        # A simultaneous catalog refresh or temporarily missing install can recover.
        return 5.


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
    _close_library()
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
    if _library_icons is not None and uid in _library_icons:
        return
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
        bpy.app.timers.register(preload_library, first_interval=2., persistent=True)


def unregister():
    global _scanner, _previews
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    if bpy.app.timers.is_registered(preload_library):
        bpy.app.timers.unregister(preload_library)
    _close_library()
    _stop_worker()
    _scanner = None
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
    _checked.clear()
    _pending.clear()
