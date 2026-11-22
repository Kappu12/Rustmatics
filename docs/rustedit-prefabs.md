# RustEdit custom prefab interchange

## Export a RustEdit 2 monument

Choose **Export RustEdit 2 Monument** from the File menu. The `.asset`
contains the prefab placements and embeds height, splat, biome, alpha, and
topology when the project has native ground. The terrain stamp blends out at
its edge and stays within the saved ground patch.

For the full saved composition, run one command from the Windows package
directory.

```bat
Rustmatics.cmd "C:\maps\Project.ruster" --export-prefab "C:\maps\Monument.asset" --require-ground
```

Open the result directly with **File > Open** in RustEdit 2. To place it on
another map, put the file in `CustomMonuments`, refresh **Monuments** in the
Content Browser, and apply its terrain modifiers after placing it.

Legacy `.prefab` inputs with companion images can also be converted to
`.asset` in one command. Keep their companion images beside the input. Use
legacy `.prefab` re-export when preserving linked IO, paths, or other
RustEdit 1 extension records.

## Legacy prefab format

The `.prefab` format targets the RustEdit 1.2.19 serializer. RustEdit 2.0.16
also reads these bundles through its legacy monument importer. Rustmatics ships
its own implementation of the format.

Files start with little-endian uint32 version 1, followed by the legacy LZ4Stream
envelope around protobuf data. The top-level fields are options (1), terrain
textures (2), prefab placements (3), paths (4), IO (5), anchored paths (6), NPC
spawners (7), APC paths (8), vehicles (9), building grades (10), and checksum (11).
Placement records use the same ID and Unity transforms as Rust world files.

The checksum is lowercase MD5 over each ordered placement's little-endian uint32
ID, then its position, Euler rotation and scale. Each float32 contributes its
high three bytes, dropping the least significant byte. Existing checksums are
preserved for unchanged inputs, including older prefabs with different hashes.

An unchanged import/export preserves the entire original file byte-for-byte.
Unknown fields remain intact. IO entity positions and both connection ends follow
placement movements when IDs and ordering remain intact and positions identify
unique placements. Ambiguous references, IO-linked replacement/deletion, and
changes with other populated extension families are rejected to avoid corrupting
links. New placement-only compositions can be edited and exported normally.

## Companion terrain files

**Export RustEdit Prefab** and `--export-prefab` with a `.prefab` destination
write the companion images beside the prefab when the project has native ground.

| Suffix after `.prefab` | Contents |
| --- | --- |
| `.heights` | Signed height offsets in the red and blue channels, relative to the prefab pivot |
| `.splat0`, `.splat1` | The eight ground texture weights, four per RGBA image |
| `.biome` | The first four biome weights in RGBA. RustEdit derives Jungle from their remainder. |
| `.alpha` | Solid ground and holes in the alpha channel |
| `.topology` | The 31 RustEdit topology bits packed in little-endian RGBA order |
| `.png` | Optional browser thumbnail supplied as a PNG |

Keep the companion files with the `.prefab` under the same stem. The terrain
stamp is centred at the same pivot as the prefab placements. Automatic width
fits the selected placements inside RustEdit's circular stamp with room for
the edge fade. Set **Terrain Width (m)** to include a larger area. RustEdit's
terrain modifier controls apply these masks after placement.

**Include Map Terrain** controls capture from the current map. Imported RustEdit
companion images are kept in saved Rustmatics projects for re-export at the
imported prefab's pivot. In MCP, `export_prefab` accepts `include_terrain`,
`terrain_width`, `terrain_fade`, and `preview_png`. Omit `include_terrain` for
automatic capture on native map projects. The result lists the actual companion
paths, and an explicit overwrite replaces the files of the bundle.

## Opening the bundle in RustEdit 2

1. Copy the `.prefab` and its companion images into `CustomMonuments`.
2. Right-click empty space in the Content Browser and choose **Refresh Monuments**.
3. Right-click the entry under **Monuments** and choose **Edit Monument**.
   Saving it there creates a RustEdit 2 `.asset` with the terrain layers embedded.

Green Thumb was tested in RustEdit 2.0.16 with all five terrain layers enabled.
In a test world, both `.map` export modes kept the five layers, including the
imported Jungle biome and a separately painted Arid patch.
RustEdit 2 saves editable maps as `.scene`
and exports game maps as `.map`. See the [RustEdit 2 legacy import guide](https://rustedit.rusthq.com/maps/rustedit-1-maps).

## Validation

Validation uses synthetic fixtures plus the locally supplied Maze prefab. To
check an export against RustEdit's actual protobuf deserializer and checksum
without starting RustEdit or loading game bundles:

```powershell
.\tools\check_rustedit_interop.ps1 `
  -RustEditPath 'C:\Program Files (x86)\RustEdit' `
  -Prefab '.\build\example.prefab' -RequireChecksum
```

The optional check reads installed assemblies and the prefab without changing
either. Local reverse-engineering outputs and user fixtures are not distributed.
Reader budgets are 32 MiB compressed, 64 MiB decompressed, and 100,000 placements.
