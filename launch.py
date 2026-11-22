#!/usr/bin/env python3
"""Launch a separate Rustmatics application process using a bundled or local Blender."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent


def startup_completed(log_path):
    with Path(log_path).open('r',encoding='utf-8',errors='replace') as stream:
        return any(line.startswith('RUSTER_READY ') for line in stream.read(65536).splitlines())


def check_exit(code, log_path):
    if not code:return 0
    ready=startup_completed(log_path)
    # Windows TerminateProcess (including Stop-Process during an update) uses
    # -1. An editor that already started was stopped, not a failed startup.
    if sys.platform=='win32' and code in (-1,0xFFFFFFFF) and ready:
        with Path(log_path).open('a',encoding='utf-8') as stream:
            stream.write('\nRUSTER_STOPPED: editor stopped after successful startup (exit -1).\n')
        return 0
    action='Rustmatics stopped unexpectedly' if ready else 'Rustmatics could not start'
    raise OSError(f'{action} (exit {code}). Details: {log_path}')


def find_blender(explicit=None):
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file():
            return str(path)
        raise FileNotFoundError(f'Blender executable does not exist: {path}')
    if value := os.environ.get('RUSTER_BLENDER'):
        return find_blender(value)
    candidates = [ROOT/'runtime/blender.exe', ROOT/'runtime/blender',
                  ROOT/'runtime/Blender.app/Contents/MacOS/Blender',
                  Path('/Applications/Blender.app/Contents/MacOS/Blender')]
    for path in candidates:
        if path.is_file():
            return str(path)
    if path := shutil.which('blender'):
        return path
    raise FileNotFoundError('Blender 5.2 LTS was not found. Run tools/package.py or set RUSTER_BLENDER.')


def main():
    parser = argparse.ArgumentParser(description='Rustmatics — Rust prefab studio')
    parser.add_argument('project', nargs='?', help='A .prefab, prefab-only .map import, or .ruster project')
    parser.add_argument('--blender', help='Path to Blender 5.2 LTS')
    parser.add_argument('--background', action='store_true', help='Launch without a window (development)')
    parser.add_argument('--headless', action='store_true', help='Run a persistent MCP host without a desktop window')
    parser.add_argument('--user-dir', help='Settings/cache directory (headless defaults to a separate profile)')
    parser.add_argument('--mcp-port', type=int, help='Headless MCP port (default 9878 on first use)')
    parser.add_argument('--rust-install', help='Rust game directory to index in headless mode')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--validate-map', action='store_true', help='Validate the input project for map export and exit')
    actions.add_argument('--export-map', metavar='OUTPUT.map', help='Write a native Rust map and exit')
    actions.add_argument('--export-prefab', metavar='OUTPUT.{asset,prefab}',
                         help='Write a RustEdit 2 monument or legacy prefab bundle and exit')
    actions.add_argument('--export-report', metavar='OUTPUT.docx', help='Write a Word project report and exit')
    parser.add_argument('--require-ground', action='store_true', help='Require native ground for validation or export')
    parser.add_argument('--terrain-width', type=int, help='Prefab export terrain width in metres; zero fits the placements')
    parser.add_argument('--terrain-fade', type=int, help='Prefab export terrain edge fade in metres (default: 5)')
    parser.add_argument('--overwrite', action='store_true', help='Allow the one-shot export to replace the output file')
    args = parser.parse_args()
    if (args.mcp_port is not None or args.rust_install) and not args.headless:
        parser.error('--mcp-port and --rust-install require --headless')
    if args.mcp_port is not None and not 1024 <= args.mcp_port <= 65535:
        parser.error('--mcp-port must be between 1024 and 65535')
    project = Path(args.project).expanduser().resolve() if args.project else None
    if project and (project.suffix.lower() not in ('.map', '.prefab', '.ruster') or not project.is_file()):
        parser.error('Choose an existing .map, .prefab or .ruster file')
    exports = {'export_map': ('.map',), 'export_prefab': ('.asset', '.prefab'), 'export_report': ('.docx',)}
    action = next((name for name in exports if getattr(args, name) is not None), None)
    output = getattr(args, action) if action else None
    batch = None
    if args.validate_map or output is not None:
        if args.headless or args.background or args.user_dir is not None:
            parser.error('One-shot commands use a temporary profile and run without --headless, --background or --user-dir')
        if project is None or project.suffix.lower() == '.map':
            parser.error('One-shot commands need a .ruster or .prefab input; opening a .map only imports placements')
        batch = {'action': 'validate_map', 'require_ground': args.require_ground}
        if action:
            destination = Path(output).expanduser().resolve()
            extensions = exports[action]
            if destination.suffix.lower() not in extensions:
                parser.error('The destination must end with ' + ' or '.join(extensions))
            batch.update(action=action, path=str(destination), overwrite=args.overwrite)
    if args.overwrite and output is None:
        parser.error('--overwrite requires an export')
    if args.require_ground and not (args.validate_map or action in ('export_map', 'export_prefab')):
        parser.error('--require-ground requires --validate-map, --export-map or --export-prefab')
    if args.terrain_width is not None or args.terrain_fade is not None:
        if action != 'export_prefab':
            parser.error('--terrain-width and --terrain-fade require --export-prefab')
        if args.terrain_width is not None and not 0 <= args.terrain_width <= 32768:
            parser.error('--terrain-width must be between 0 and 32768 metres')
        if args.terrain_fade is not None and not 1 <= args.terrain_fade <= 16383:
            parser.error('--terrain-fade must be between 1 and 16383 metres')
        batch.update(terrain_width=args.terrain_width or 0, terrain_fade=args.terrain_fade or 5)
    scratch = None
    try:
        executable = find_blender(args.blender)
        sys.path.insert(0, str(ROOT))
        from ruster.core.headless import configure_user_dir
        if batch:
            scratch = tempfile.TemporaryDirectory(prefix='ruster-batch-')
        user_dir = configure_user_dir(scratch.name if scratch else args.user_dir, headless=args.headless)
        env = os.environ.copy()
        # Prevent an inherited launcher mode from changing this invocation.
        env.pop('RUSTER_HEADLESS', None)
        env.pop('RUSTER_BATCH', None)
        if batch:
            env['RUSTER_BATCH'] = json.dumps(batch)
        if args.headless:
            env['RUSTER_HEADLESS'] = '1'
            if args.mcp_port is not None:env['RUSTER_HEADLESS_PORT'] = str(args.mcp_port)
            if args.rust_install:env['RUSTER_HEADLESS_RUST_INSTALL'] = str(Path(args.rust_install).expanduser().resolve())
        env['RUSTER_LAUNCHER_PID'] = str(os.getpid())
        env['BLENDER_USER_CONFIG'] = str(user_dir / 'blender-config')
        env['BLENDER_USER_SCRIPTS'] = str(user_dir / 'blender-scripts')
        if (ROOT/'ruster/branding/splash.png').is_file():
            env['BLENDER_CUSTOM_SPLASH']=str(ROOT/'ruster/branding/splash.png')
        if (ROOT/'python_modules').exists():
            env['RUSTER_PYTHON_MODULES'] = str(ROOT/'python_modules')
        command = [executable, '--factory-startup', '--disable-autoexec', '--python-exit-code', '1',
                   '--python', str(ROOT/'bootstrap.py')]
        if args.background or args.headless or batch:
            command.insert(1, '--background')
        if project:
            command += ['--', str(project)]
        if args.headless or batch:
            # Keep readiness/errors visible to CMD and supervising agents.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
            process = subprocess.Popen(command, env=env, creationflags=flags)
            def stop_child(_signum=None, _frame=None):
                if process.poll() is None:
                    process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == 'win32' else signal.SIGTERM)
            signals = (signal.SIGTERM, *([signal.SIGBREAK] if hasattr(signal, 'SIGBREAK') else []))
            previous = {kind: signal.signal(kind, stop_child) for kind in signals}
            try:
                # On Windows an infinite native wait defers Python's SIGBREAK
                # handler, so Ctrl+Break never reaches the background engine.
                # Return to the interpreter regularly to forward console events.
                while True:
                    try:
                        code = process.wait(timeout=.2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except KeyboardInterrupt:
                stop_child()
                try:code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(); code = 1
            finally:
                for kind, handler in previous.items():signal.signal(kind, handler)
        else:
            logs=user_dir/'logs';logs.mkdir(parents=True,exist_ok=True)
            descriptor,filename=tempfile.mkstemp(prefix='startup-',suffix='.log',dir=logs)
            os.close(descriptor);log_path=Path(filename)
            # Concurrent launches must never overwrite another editor's diagnostics.
            (logs/'latest-startup.txt').write_text(str(log_path),encoding='utf-8')
            with log_path.open('w', encoding='utf-8') as log:
                code = subprocess.call(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:shutil.copyfile(log_path,logs/'startup.log')
            except OSError:pass  # An older launcher can still have startup.log open.
            return check_exit(code,log_path)
        if code and not batch:
            raise OSError(f'Rustmatics could not start (exit {code}). See the terminal output above.')
        return code
    except (FileNotFoundError, OSError) as error:
        if sys.stderr is not None:
            print(f'Rustmatics: {error}', file=sys.stderr)
        elif sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), 'Rustmatics application error', 0x10)
        return 1
    finally:
        if scratch:
            scratch.cleanup()


if __name__ == '__main__':
    sys.exit(main())
