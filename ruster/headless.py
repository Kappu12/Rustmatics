"""One-shot commands and persistent hosting in background Blender."""
import json
import os
import signal
import time
from zipfile import BadZipFile

import bpy

from . import app, live_preview, mcp_bridge, studio
from .core import mcp_config, settings
from .core.headless import DEFAULT_PORT, ProfileLock
from .core.steam import resolve_install


def run_batch(project, options):
    """Open a saved input, perform one action, and leave the profile disposable."""
    from . import map_prefabs, scene

    action = options['action']
    try:
        bpy.context.preferences.edit.use_global_undo = False
        studio.new_prefab()
        app.open_path(project)
        require_ground = options['require_ground']
        if action == 'validate_map':
            result = map_prefabs.validation_status(require_ground=require_ground)
        elif action == 'export_map':
            overwrite = options['overwrite']
            path = mcp_bridge.file_target(options['path'], '.map', overwrite)
            map_prefabs.export_map(path, overwrite=overwrite, require_ground=require_ground)
            result = {'path': str(path)}
        elif action == 'export_prefab':
            from .core.rustedit_terrain import EXPORT_EXTENSIONS
            if require_ground and not bpy.context.scene.ruster.native_ground:
                raise ValueError('Prefab export requires native Rust ground')
            overwrite = options['overwrite']
            path = mcp_bridge.file_target(options['path'], EXPORT_EXTENSIONS, overwrite)
            files = studio.export_prefab(path, selected=False, terrain_width=options.get('terrain_width', 0),
                                         terrain_fade=options.get('terrain_fade', 5), overwrite=overwrite)
            result = {'path': str(files[-1]), 'companions': [str(file) for file in files[:-1]]}
        elif action == 'export_report':
            from . import project_report
            overwrite = options['overwrite']
            path = mcp_bridge.file_target(options['path'], '.docx', overwrite)
            result = project_report.export(path, overwrite=overwrite)
        else:
            raise ValueError('Unknown one-shot operation')
        result.update(action=action, prefab_count=len(scene.prefab_roots()),
                      native_ground=bpy.context.scene.ruster.native_ground)
        print('RUSTER_BATCH_RESULT ' + json.dumps(result), flush=True)
        return result.get('valid', True) if action == 'validate_map' else True
    except (BadZipFile, OSError, ValueError) as error:
        print('RUSTER_BATCH_RESULT ' + json.dumps({'action': action, 'error': str(error)}), flush=True)
        return False
    finally:
        import ruster
        ruster.unregister()


def configure():
    config = settings.load()
    port = int(os.environ.get('RUSTER_HEADLESS_PORT', config.get('mcp_port', DEFAULT_PORT)))
    if not 1024 <= port <= 65535:
        raise ValueError('MCP port must be between 1024 and 65535')
    explicit = os.environ.get('RUSTER_HEADLESS_RUST_INSTALL')
    install = settings.validate_install(explicit) if explicit else resolve_install(config.get('rust_install', ''))
    if install:
        config['rust_install'] = str(install)
    config.update(mcp_enabled=True, mcp_port=port)
    settings.save(config)
    mcp_config.token()
    return config


def pump(server, deadlines):
    """Background Blender does not run app timers. Pump the actual work queues."""
    server._drain_command_queue()
    now = time.monotonic()
    if now >= deadlines.get('assets', 0):
        if app._job is not None or app._pending_asset_ids:
            app.poll_job()
        deadlines['assets'] = now + .2
    if app._visual_queue:
        app.load_visuals()
    if now >= deadlines.get('preview', 0):
        deadlines['preview'] = now + (live_preview.tick() or .5)
    bpy.context.view_layer.update()


def run(project=None):
    if not bpy.app.background:
        raise ValueError('Headless hosting requires Blender background mode')
    stopping = False
    def request_stop(_signum, _frame):
        nonlocal stopping
        # Signal handlers must not acquire locks held by the interrupted code.
        stopping = True
    previous = {}
    try:
        with ProfileLock(settings.user_root()):
            try:
                config = configure()
                # Native undo depends on the GUI event loop. Explicit saves and the
                # existing atomic batch operations are available in background mode.
                bpy.context.preferences.edit.use_global_undo = False
                studio.new_prefab()
                if project:
                    app.open_path(project)
                if app._job is None and config.get('rust_install'):
                    from pathlib import Path
                    if (Path(config['rust_install']) / 'Bundles/AssetSceneManifest.json').is_file():
                        app.start_job('index')
                if not config.get('rust_install'):
                    bpy.context.scene.ruster.asset_status = 'No Rust installation found; pass --rust-install to index and extract assets'
                mcp_bridge.start(config['mcp_port'], manual_pump=True)
                mcp_bridge.sync_state()
                for kind in (signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if hasattr(signal, 'SIGBREAK') else [])):
                    previous[kind] = signal.signal(kind, request_stop)
                print('RUSTER_HEADLESS_READY ' + json.dumps({
                    'host': '127.0.0.1', 'port': config['mcp_port'], 'pid': os.getpid(),
                    'user_dir': str(settings.user_root()), 'mcp': mcp_bridge.client_config(),
                }), flush=True)
                deadlines = {}
                while not stopping and mcp_bridge.running():
                    pump(mcp_bridge._server, deadlines)
                    time.sleep(.02)
            except KeyboardInterrupt:
                pass
            finally:
                mcp_bridge.stop()
                # Cancel workers and stop the owned preview before releasing
                # the profile lock, including during SIGTERM/Ctrl+C shutdown.
                import ruster
                ruster.unregister()
    finally:
        for kind, handler in previous.items():
            # Blender installs C signal handlers, represented by None in
            # Python. The process exits after this background script returns.
            signal.signal(kind, handler if handler is not None else signal.SIG_DFL)
