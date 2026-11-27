# Rustmatics

**Everyone is welcome to fork this repository.**

Rustmatics is a prefab editor built on Blender 5.2 LTS for AI agents working with Rust assets. Through MCP, agents can find, place, and arrange prefabs to build custom monuments and other scenes, then export those compositions for use in Rust. Rustmatics loads the original models and textures from your local Rust installation.

You can also build a composition from scratch by hand or fine-tune an agent's work in the desktop editor using Blender's viewport, object tools, and undo history.

For general-purpose Rust map editing, we recommend [RustEdit](https://rustedit.rusthq.com/).

## Rustmatics in action

https://github.com/user-attachments/assets/4dcc43d6-ee81-4d33-8e18-b2d898a37dec

## Start with a packaged build

Open [Releases](https://github.com/Kappu12/Rustmatics/releases), download the ZIP for your platform, and extract it.

- **Windows x64:** open `Rustmatics.exe`.
- **Linux x64:** run `./Rustmatics.sh` from the extracted application folder. If needed, run `chmod +x Rustmatics.sh` first.
- **macOS Apple Silicon:** open `Rustmatics.app` from the macOS ZIP.

Keep the application folder together. The package includes its Blender runtime and Python dependencies, so a separate Blender installation is unnecessary.

### Make your first composition

1. Open **Preferences > Assets**. Let Rustmatics find your Steam installation, or select the Rust game folder containing `Bundles`, then click **Apply Installation**.
2. Create a new prefab composition from the welcome screen.
3. Press **Shift+A** to browse Rust prefabs. The browser supports search, categories, favorites, and recently used assets. Enable **Hidden Assets** when looking for helpers such as volumes or spawners.
4. Add a prefab and arrange it in the viewport. Models and materials load on demand, with progress shown in **Model Loading**.
5. Save a `.ruster` project, then export a RustEdit 2 monument (`.asset`) or a legacy RustEdit bundle (`.prefab`).

If you have RustEdit installed, its folder can also be selected in **Preferences > Assets** as a source for prefab names through its local `stringpool.txt`.

Shift+A and the Prefab Browser use RustEdit’s local `PreviewImages` when
available. While the editor is open, a background worker fills a local cache
of 24 × 24 PNGs for the installed prefab library. It processes one model at a
time, skips helpers with no visible mesh, and resumes on the next launch.
Open the dropdown again to see newly cached icons.

## Build in the viewport

Use Blender's object controls to move, rotate, and scale placements. **G**, **R**, and **S** start transforms; axis constraints and numeric input work as usual. **Shift+D** duplicates a placement, and **F3** opens command search. The Outliner provides another way to select and organize the composition.

For repeated structures, use the layout tools:

- Linear and radial arrays.
- Alignment by model bounds and distribution by spacing.
- Grid snapping and placement on a plane.
- Radial symmetry sets that update together while symmetry is enabled.
- Vertex, edge, and face snapping, plus stepped rotation.

Group placements to move an assembly as a unit. **Composition Checks** helps locate overlapping copies, mirrored transforms, and other export issues.

With **Volumes** and viewport overlays enabled, native volume bounds have faint
fills and persistent outlines: green for safe zones, amber for no-build zones,
red for movement/no-entry restrictions, and cyan for other triggers. Click an
edge or filled face to select; repeat to cycle overlapping volumes. Edges and
smaller volumes have priority over enclosing boxes. Hover
brightens the type colour, and selection adds a stronger outline. The colours
and fills only affect the editor preview; the original Rust prefab is preserved.

Original prefab geometry stays read-only in the composition. Shape changes come from choosing and arranging whole assets. **Export Blender File** creates a separate scene with editable models and packed textures for rendering or further work in Blender.

## Choose an output

| Format | Purpose |
| --- | --- |
| `.ruster` | Save a Rustmatics project and resume editing it later. |
| `.asset` | Export a RustEdit 2 monument with embedded terrain. |
| `.prefab` | Import or export a legacy RustEdit composition with companion terrain masks. |
| `.map` | Import prefab placements; export placements plus native terrain when Rust Ground has been added. |
| `.blend` | Export a standalone Blender scene for rendering and model work. |
| `.docx` | Export a Word report with map validation, ground status and a prefab inventory through the CLI or MCP. |
| Other model formats | Export selected models through the exporters available in the bundled Blender build, including glTF, FBX, and OBJ. |

Rust exports describe original prefab IDs and placement transforms. Model exports produce geometry for use in other applications. For imported RustEdit prefabs with IO data, the editor also exposes supported IO settings on the selected placement.

### Add native Rust ground

Choose **Add Rust Ground** in Prefab Studio or the **Rust Ground** panel. Set the
flat patch width and height, a containing world size, biome and ground texture.
Existing prefabs keep their positions. The Ground panel provides viewport brushes
for height, water, splat, biome, alpha and topology, with undo and project saving.
Native maps include five biome channels with explicit Jungle weights.

Export a **`.map`** to include native walkable ground and its painted layers.
Starting Live Preview uses the current native ground. Restart it after changing
terrain layers.
See [native ground](docs/native-ground.md) for data and verification details.

The RustEdit export commands use highlighted prefabs and groups, or the whole
composition when nothing is highlighted. **Export RustEdit 2 Monument** embeds
height, splat, biome, alpha, and topology in one `.asset`. **Export RustEdit
Prefab** writes those masks beside the legacy `.prefab`. Keep those files
together when moving the prefab. Both formats are available with
`--export-prefab` from a [headless command](docs/headless.md).

Save your working project before testing an export in RustEdit or Rust. Viewport previews show assets and placement; gameplay behavior is determined by the game.

## Local live preview

The **Live Preview** editor below **Placements** on the right provides a private
local server, configurable port, owner Steam ID, console, and an explicit client-module action.
It downloads the server on first use. Client injection requires verified inactive
EAC and is experimental; see [setup, checks and current limits](docs/live-preview.md).

## Connect an MCP client

Rustmatics includes an optional local MCP connection for inspecting the editor and authoring prefab compositions with an AI client.

1. Open **Preferences > MCP** and enable MCP.
2. Click **Copy MCP Client Configuration**.
3. Add the copied configuration to your client's MCP settings, then connect while Rustmatics remains open.

The copied configuration uses the application's Python and `mcp_server.py` entry point. The editor bridge listens on `127.0.0.1`, uses a per-user authentication token, and defaults to port `9877`.

Start an automation session by reading `ruster://guide/building` and calling `get_editor_status`. Tools can search the local catalog, request models, place and transform prefabs, organize groups, inspect the viewport, and export compositions. They operate on whole placements while preserving the original assets.

Project tools expose the current path and unsaved state, recent projects, and
`open_project`/`new_project`. Save current work first; discarding changes requires
`discard_unsaved=True`. An active preview stays running across project switches
in the same Rustmatics window, clearing the old scene and syncing the new project.
`start_live_preview`, `stop_live_preview` and `get_live_preview_status` control the
owned server and report progress, logs, connection details and client errors.
Console and owner-ID tools queue commands on that server. Launch and Inject use
the Live Preview panel's explicit client actions and EAC verification.
After updating Rustmatics, refresh the MCP client's tool list/reconnect the adapter.

### Run an agent without a desktop window

For a saved project, validate, export a native map or generate a Word report in
one command:

```bat
Rustmatics.cmd "C:\maps\Project.ruster" --validate-map --require-ground
Rustmatics.cmd "C:\maps\Project.ruster" --export-map "C:\maps\Project.map" --require-ground
Rustmatics.cmd "C:\maps\Project.ruster" --export-report "C:\maps\Project Report.docx"
```

One-shot commands use a temporary profile and exit when finished. For Linux,
source, machine-readable output and server handoff, see the
[headless guide](docs/headless.md), also available in MCP as
`ruster://guide/headless`.

Start the persistent headless host from CMD in the extracted application folder:

```bat
Rustmatics.cmd --headless --rust-install "C:\Program Files (x86)\Steam\steamapps\common\Rust"
```

On Linux use `./Rustmatics.sh --headless --rust-install "/path/to/Rust"`. From source,
use `python launch.py --headless --blender "/path/to/blender"`. Add an existing
`.ruster`, `.prefab`, or `.map` path as the positional argument to open it at startup.
The Rust path is saved for later runs; Steam detection is also available.

Wait for `RUSTER_HEADLESS_READY` in the terminal. Its JSON includes an MCP client
configuration using the bundled Python and the correct profile. Add that
configuration to your agent. Alternatively, run the stdio adapter with
`python mcp_server.py --headless` using Python with the MCP dependencies installed.
The adapter connects to the running host; it does not launch a second editor.

Headless mode uses a separate `headless` subdirectory of the normal user data
folder and port **9878** by default. `--user-dir` (or `RUSTER_USER_DIR`) selects
another profile; give the same directory to the MCP adapter. `--mcp-port` selects
another listener port. Use a distinct directory and port for each concurrent
session. A profile lock prevents two headless hosts from writing the same cache
and credentials. The desktop keeps its existing settings and default port 9877.

Agents can search the catalog, load models, place/group/transform prefabs,
measure bounds, switch projects, save `.ruster`, and export compositions/models.
`get_editor_status` reports `headless: true` and `unavailable_tools`: viewport
images/navigation and undo/redo need the desktop. Use explicit saved checkpoints
and atomic batches. Asset loading and the owned live-preview server continue
through the host's main-thread pump; desktop Rust-client actions remain in the UI.

Save through MCP before stopping the host with **Ctrl+C**; shutdown does not
automatically save an unfinished composition. No window or display server is
required. The development-only `--background` flag still performs registration
and exits; use `--headless` for a persistent agent session.

## Run from source

Use Python 3.13 and the Blender version pinned in [engine.lock.json](engine.lock.json), currently Blender 5.2.3. Set `RUSTER_BLENDER` to that Blender executable, or make `blender` available on your `PATH`.

Install the asset and MCP dependencies into the application's `python_modules` directory using Blender's bundled Python:

```sh
python tools/setup_assets.py
python launch.py
```

You can also select the executable for a single launch:

```sh
python launch.py --blender "/path/to/blender"
```

Pass an existing project or import file as the positional argument:

```sh
python launch.py "/path/to/composition.ruster"
```

The launcher uses a separate Blender process and a Rustmatics-specific configuration directory. Each desktop launch keeps its own `logs/startup-*.log`; `logs/latest-startup.txt` points to the latest run, and `logs/startup.log` contains the latest completed run. Errors link to that run's own log. Headless output stays in the terminal and can be redirected to a file.

### Check changes

In a Python 3.13 development environment:

```sh
python -m pip install pytest lz4 numpy -r requirements-assets.txt -r requirements-mcp.txt
python -m pytest -q tests
python -m compileall -q ruster tools bootstrap.py launch.py
```

Run the native editor fixtures against the pinned Blender executable:

```sh
python tools/native_checks.py --engine "/path/to/blender" --suite background
python tools/native_checks.py --engine "/path/to/blender" --suite graphical
```

On Linux, `--xvfb` gives the graphical suite a disposable virtual display when Xvfb is installed.

### Create an application package

On the target operating system, use Python 3.13. On a Mac, use native arm64 Python and install the Xcode Command Line Tools (`xcode-select --install`) if they are missing.

```sh
python tools/package.py --archive --output-dir artifacts
```

The packager downloads the pinned Blender archive, verifies its SHA256, installs application dependencies, and runs the package checks. Use `--output-name` to give the folder and ZIP a versioned name. On macOS, add `--macos-app` to build `Rustmatics.app` as the ZIP's top-level item. The app bundle is ad-hoc signed. The Windows Live Preview client module requires Windows.

### Publish a version

Branch pushes and pull requests run the unit and graphical tests. Version tag pushes also build and test the Ubuntu package and upload its ZIP to a draft GitHub release. Windows and macOS packages are built locally.

To release a version, update `ruster/__init__.py` and commit the changes. On Windows, build the package from the commit you plan to tag.

```sh
python tools/package.py --archive --output-dir artifacts --output-name Rustmatics-windows-x64-v1.2.3
git tag v1.2.3
git push origin main v1.2.3
```

Wait for the **Release Rustmatics App** run for that tag to succeed. Then upload the Windows ZIP and publish with the authenticated GitHub CLI.

```sh
python tools/release.py upload --repo Kappu12/Rustmatics --tag v1.2.3 --platform windows-x64 --directory artifacts
python tools/release.py publish --repo Kappu12/Rustmatics --tag v1.2.3
```

Publication verifies the Windows and Linux downloads. A Mac builder can check out the release source, use `--macos-app --output-name Rustmatics-macos-arm64-v1.2.3` with the packaging command above, and attach the tested ZIP to the release. Tags such as `v1.2.3-rc.1` create prereleases.

## Local files

Rustmatics reads the installed game files without changing them. The rename keeps the existing locations for settings, the asset cache, recovery files, and logs:

| Platform | Default user data folder |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Ruster` |
| Linux | `$XDG_DATA_HOME/ruster`, or `~/.local/share/ruster` when unset |
| macOS | `~/Library/Application Support/Ruster` |

Set `RUSTER_USER_DIR` to choose another user data folder. Projects and exports are saved to the locations you select.

## Further reading

- [Project container format](docs/ruster-format.md)
- [RustEdit prefab format and interoperability](docs/rustedit-prefabs.md)
- [Architecture](docs/architecture.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

Rustmatics's application code is licensed under GPL-3.0-or-later. See [LICENSE](LICENSE). Rust game assets remain the property of their respective owners and are supplied by your local installation. Rustmatics is an independent community project.
