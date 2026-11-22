"""Real CLI -> background Blender -> authenticated socket -> stdio MCP round trip.

Run with Python matching the pinned engine: headless_roundtrip.py BLENDER [APP_ROOT].
Uses synthetic models, no display, Rust installation or personal editor settings.
"""
import asyncio
from contextlib import closing
import gzip
import json
import os
from pathlib import Path
import signal
import site
import socket
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
APP = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT
ENGINE = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(APP))
if (APP / 'python_modules').is_dir():
    sys.path.insert(0, str(APP / 'python_modules'))
    site.addsitedir(str(APP / 'python_modules'))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ruster.assets import catalog
from ruster.core import rustedit_terrain
from ruster.core.rust_map import RustMap
from ruster.core.rustedit_prefab import RustEditPrefab
from ruster.vendor.blender_mcp_client import BlenderConnection
from tests.fixtures import sample_world, triangle_asset


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def start(directory, profile, port, project):
    log_path = directory / 'host.log'
    log = log_path.open('w')
    env = dict(os.environ, PYTHONUNBUFFERED='1', RUSTER_PREVIEW_TEST_NO_DISCOVERY='1')
    # Keep a developer's installed Rust manifest from reindexing our fixture.
    env['RUSTER_HEADLESS_RUST_INSTALL'] = str(directory / 'test-install')
    env.pop('DISPLAY', None); env.pop('WAYLAND_DISPLAY', None)
    env.pop('RUSTER_USER_DIR', None)
    command = [sys.executable, str(APP / 'launch.py'), '--headless', '--blender', str(ENGINE),
               '--user-dir', str(profile), '--mcp-port', str(port), str(project)]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
    process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            output = log_path.read_text(errors='replace')
            if 'RUSTER_HEADLESS_READY ' in output:
                return process, log, command, env
            assert process.poll() is None, output
            time.sleep(.1)
        raise AssertionError('Headless startup timed out: ' + log_path.read_text(errors='replace'))
    except BaseException:
        process.kill(); process.wait(); log.close(); raise


def stop(process, log_path):
    if process.poll() is None:
        process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == 'win32' else signal.SIGTERM)
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print(log_path.read_text(errors='replace'), flush=True)
            if sys.platform == 'win32':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
            else:process.kill()
            process.wait(); raise
    assert process.returncode == 0, (process.returncode, log_path.read_text(errors='replace'))


async def exercise(profile, directory):
    params = StdioServerParameters(command=sys.executable,
        args=[str(APP / 'mcp_server.py'), '--headless', '--user-dir', str(profile)],
        env={'PYTHONIOENCODING': 'utf-8', 'PYTHONPATH': os.environ.get('PYTHONPATH', '')})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()

            async def call(rpc_name, **values):
                result = await session.call_tool(rpc_name, values)
                assert not result.isError, (rpc_name, result.content)
                return json.loads(result.content[0].text)

            # The positional .map imports placements before readiness. Its cached
            # models must finish through the background asset/preview pump.
            deadline = time.monotonic() + 40
            while True:
                status = await call('get_editor_status')
                assert status['headless'] and status['prefab_count'] == 2, status
                if not status['busy']:break
                assert time.monotonic() < deadline, status
                await asyncio.sleep(.1)
            assert status['visible_prefabs'] == 2, status
            assert {'get_viewport_image', 'undo'} <= set(status['unavailable_tools'])
            assert 'undo/redo' not in status['capabilities']
            for name in status['unavailable_tools']:
                args = {'names': ['unused']} if name == 'focus_view' else {}
                if name == 'set_viewport_state':args = {'settings': {'distance': 10}}
                result = await session.call_tool(name, args)
                assert result.isError and 'desktop app' in result.content[0].text, (name, result)
            rows = await call('search_prefabs', query='model_123456')
            assert rows['prefabs'][0]['id'] == 123456
            obj = await call('place_prefab', uid=123456, position=[3, 4, 5])
            name = obj['name']
            changed = await call('transform_object', name=name, position=[9, 4, 5])
            assert changed['position'] == [9, 4, 5]
            # Failed edits remain atomic and the session stays usable.
            result = await session.call_tool('transform_object', {'name': name, 'scale': [0, 1, 1]})
            assert result.isError
            assert (await call('get_object_info', name=name))['position'] == [9, 4, 5]
            result = await session.call_tool('new_project', {})
            assert result.isError and 'unsaved changes' in result.content[0].text
            saved = directory / 'Built.ruster'
            await call('save_project', path=str(saved))
            assert saved.is_file()
            assert not (await call('get_project_status'))['unsaved_changes']
            objects = await call('list_objects', kind='PREFAB')
            names = [row['name'] for row in objects['objects']]
            exported = directory / 'Built.prefab'
            await call('export_prefab', names=names, path=str(exported))
            assert len(RustEditPrefab.load(exported).prefabs) == 3
            result = await session.call_tool('export_prefab', {'names': names, 'path': str(exported)})
            assert result.isError and 'exists' in result.content[0].text
            await call('new_project')
            assert (await call('get_editor_status'))['prefab_count'] == 0
            await call('open_project', path=str(saved))
            status = await call('get_editor_status')
            assert status['headless'] and status['prefab_count'] == 3
            assert (await call('get_object_info', name=name))['position'] == [9, 4, 5]
            assert not (await call('validate_map', require_ground=True))['valid']
            terrain = await call('add_ground', size=64, biome='Jungle')
            assert terrain['present'] and terrain['biome_channels_stored'] == 5
            await call('save_project', path=str(directory / 'Ground.ruster'))
            monument = directory / 'MCP.asset'
            result = await call('export_prefab', names=names, path=str(monument))
            assert Path(result['path']) == monument and monument.is_file() and not result['companions']
            native_map = directory / 'MCP.map'
            await call('export_map', path=str(native_map), require_ground=True)
            world = RustMap.load(native_map)
            assert len(world.prefabs) == 3 and {'terrain', 'water', 'splat', 'biome', 'alpha', 'topology'} <= world.layers.keys()
            report = directory / 'MCP.docx'
            result = await call('export_project_report', path=str(report))
            assert result['valid'] and result['native_ground'] and result['prefab_count'] == 3
            with ZipFile(report) as archive:
                assert b'Prefab Inventory' in archive.read('word/document.xml')
            return saved


def check_one_shot(directory):
    source = directory / 'Ground.ruster'
    command = [sys.executable, str(APP / 'launch.py'), '--blender', str(ENGINE)]

    def run(*args, ok=True):
        child = subprocess.run([*command, *map(str, args)], capture_output=True, text=True, timeout=30)
        assert (child.returncode == 0) == ok, (child.stdout, child.stderr)
        assert 'RUSTER_HEADLESS_READY' not in child.stdout
        return next(json.loads(line.removeprefix('RUSTER_BATCH_RESULT ')) for line in child.stdout.splitlines()
                    if line.startswith('RUSTER_BATCH_RESULT '))

    assert run(source, '--validate-map', '--require-ground')['valid']
    invalid = run(directory / 'Built.ruster', '--validate-map', '--require-ground', ok=False)
    assert not invalid['valid'] and 'native Rust ground' in invalid['errors'][0]
    target = directory / 'One shot.map'
    assert run(source, '--export-map', target, '--require-ground')['native_ground']
    original = target.read_bytes()
    assert 'exists' in run(source, '--export-map', target, ok=False)['error']
    assert target.read_bytes() == original
    target.write_bytes(b'previous output')
    assert run(source, '--export-map', target, '--overwrite')['native_ground']
    rust_map = RustMap.load(target)
    mcp_map = RustMap.load(directory / 'MCP.map')
    assert rust_map.prefabs == mcp_map.prefabs and rust_map.layers == mcp_map.layers
    monument = directory / 'One shot.asset'
    result = run(source, '--export-prefab', monument, '--require-ground')
    assert monument.is_file() and result['prefab_count'] == 3 and not result['companions']
    prefab = directory / 'One shot.prefab'
    result = run(source, '--export-prefab', prefab, '--require-ground')
    assert len(RustEditPrefab.load(prefab).prefabs) == 3
    assert {Path(path).suffix for path in result['companions'] if Path(path).is_file()} == set(rustedit_terrain.TERRAIN_SUFFIXES)
    report = directory / 'One shot.docx'
    assert run(source, '--export-report', report)['prefab_count'] == 3
    with ZipFile(report) as archive:
        document = archive.read('word/document.xml')
        assert b'Native Rust ground is present' in document and b'123456' in document


def run():
    with tempfile.TemporaryDirectory(prefix='ruster-headless-test-') as temporary:
        directory = Path(temporary); profile = directory / 'profile'; cache = profile / 'assets'
        install = directory / 'test-install'
        (install / 'Bundles').mkdir(parents=True)
        (install / 'RustClient.exe').touch()
        (cache / 'prefabs').mkdir(parents=True)
        for uid in (123456, 234567):
            asset = triangle_asset(uid); path = asset['path']
            with closing(catalog.connect(cache)) as db:
                db.execute('INSERT INTO names VALUES(?,?)', (uid, path))
                db.execute('INSERT INTO assets VALUES(?,?,?,?)', (path, 'bundle', 'file', uid)); db.commit()
            (cache / 'prefabs' / f'{uid}.json.gz').write_bytes(gzip.compress(json.dumps(asset).encode()))
        source = directory / 'Input.map'
        source.write_bytes(sample_world().write())
        port = free_port()
        process, log, command, env = start(directory, profile, port, source)
        try:
            if sys.platform == 'win32':
                # CMD automation must preserve failures without waiting at PAUSE.
                invalid_cli = subprocess.run(['cmd.exe', '/d', '/c', str(APP / 'Rustmatics.cmd'),
                    '--headless', '--mcp-port', '1'], input='', capture_output=True, text=True, timeout=10)
                assert invalid_cli.returncode == 2, (invalid_cli.stdout, invalid_cli.stderr)
            # A second owner cannot alter credentials or settings of the first.
            before = (profile / 'settings.json').read_bytes()
            other = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            assert other.returncode != 0 and 'already owns this profile' in other.stdout + other.stderr
            assert (profile / 'settings.json').read_bytes() == before
            invalid = BlenderConnection('127.0.0.1', port, 'invalid-token')
            try:
                try:invalid.send_command('ping')
                except Exception as error:assert 'authentication' in str(error).lower(), error
                else:raise AssertionError('Unauthenticated headless command succeeded')
            finally:invalid.disconnect()
            saved = asyncio.run(exercise(profile, directory))
        except BaseException:
            print((directory / 'host.log').read_text(errors='replace'), flush=True); raise
        finally:
            stop(process, directory / 'host.log'); log.close()
        # Normal shutdown releases both profile and socket; a saved .ruster
        # reopens on the same port in a fresh background process.
        process, log, _command, _env = start(directory, profile, port, saved)
        try:
            token = json.loads((profile / 'settings.json').read_text())['mcp_token']
            client = BlenderConnection('127.0.0.1', port, token)
            try:
                status = client.send_command('get_editor_status')
                assert status['headless'] and status['prefab_count'] == 3, status
            finally:client.disconnect()
        finally:stop(process, directory / 'host.log'); log.close()
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', port))
        check_one_shot(directory)
    print('RUSTER_HEADLESS_MCP_OK', flush=True)


if __name__ == '__main__':run()
