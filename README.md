# RimWorld Art Pipeline

`rw-art` bridges provider generation and deterministic RimWorld packaging. A
mod keeps its prompts, provider parameters, and output contract in a TOML
manifest. Scenario jobs are resumable, and original API or Web UI downloads
remain outside Git; reviewed, normalized PNGs enter the mod only after explicit
selection and approval commands.

## Setup

Python 3.11 and Pillow are required. Install the command from this checkout:

```bash
python3 -m pip install -e .
```

The command also works without installation from this checkout:

```bash
python3 -m rw_art_pipeline --help
```

## Bundled templates

`rw-art` carries provider-independent artwork templates for reuse across mods
and other projects. Stable catalog IDs hide package-internal paths, and export
preserves the exact approved PNG bytes:

```bash
rw-art templates list
rw-art templates show sanicek-badge

# Canonical 1024x1024 RGBA source for unrestricted downstream composition.
rw-art templates export sanicek-badge source ./sanicek-logo.png

# Normalized 256x256 RGBA frame for RimWorld's About/ModIcon.png.
rw-art templates export sanicek-badge rimworld-mod-icon ./About/ModIcon.png

# Standalone metallic S logo alternative with a transparent background.
rw-art templates export sanicek-s-logo rimworld-mod-icon ./About/ModIcon.png

# South-facing 1x1 workbench base and its CutoutComplex recolor mask.
rw-art templates export generic-workbench-1x1 rimworld-texture ./Textures/Things/Building/GenericWorkbench.png
rw-art templates export generic-workbench-1x1 rimworld-color-mask ./Textures/Things/Building/GenericWorkbench_m.png

# Boxier enclosed desk/workbench alternative.
rw-art templates export generic-desk-workbench-1x1 rimworld-texture ./Textures/Things/Building/GenericDeskWorkbench.png
rw-art templates export generic-desk-workbench-1x1 rimworld-color-mask ./Textures/Things/Building/GenericDeskWorkbench_m.png

# Symmetric blank cube for a fixed graphic with a rotatable interaction spot.
rw-art templates export generic-cube-workbench-1x1 rimworld-texture ./Textures/Things/Building/GenericCubeWorkbench.png
rw-art templates export generic-cube-workbench-1x1 rimworld-color-mask ./Textures/Things/Building/GenericCubeWorkbench_m.png
```

Exports never overwrite an existing file unless `--replace` is supplied. The
catalog records dimensions, color mode, semantic role, and SHA-256 for every
variant; installed wheels contain both the catalog and exact image resources.
Templates are distributed under this repository's MIT license and contain no
Scenario project IDs, credentials, or local generation receipts.

### Generic workbench composition

`generic-workbench-1x1` is a `128x128` industrial base for one-cell work
tables. It uses RimWorld's south-facing pseudo-orthographic view: the top is
foreshortened while the front apron remains visible. The diffuse texture is
neutral white/gray so recoloring is not biased by a baked hue. Put mod-specific
tools inside the inset surface, bounded approximately by SVG coordinates
`(24,31)` through `(104,71)`, and draw them at the same viewing angle. The
bundled texture is suitable for `Graphic_Single` buildings that keep a fixed
presentation. Treat it as the south texture when deriving a true
`Graphic_Multi` set; other facings need corresponding perspective changes.

`generic-desk-workbench-1x1` uses the same perspective and color-channel
contract but has a broader rectangular top over a full-width two-door cabinet.
It has no visible legs or cast shadow. Its overlay-safe work surface spans
approximately `(20,32)` through `(108,64)` in the SVG coordinate system.

`generic-cube-workbench-1x1` is the canonical broad, horizontally symmetric
blank slab for one-cell buildings whose graphic stays fixed while their
interaction spot can rotate. Its plain top and front apron share the red
stuff-color channel, so the complete cube follows its material. The darker
apron meets the top directly, so their tonal change defines the front edge
without a black divider. The visible `92x103` slab leaves enough transparent
canvas to fit one standard bench cell on a square draw mesh. A typical Def uses
`Graphic_Single`, `<drawSize>(1.5,1.5)</drawSize>`,
`<drawRotated>false</drawRotated>`, and `<allowFlip>false</allowFlip>`.

The complete, validated RimWorld 1.6 setup is
[`reference-assets/generic-cube-workbench-1x1/ThingDef.xml`](reference-assets/generic-cube-workbench-1x1/ThingDef.xml).
It includes the exact `Graphic_Single`, `CutoutComplex`, square draw mesh,
rotation, shadow, interaction-cell, and common southward offset settings used
for the accepted in-game appearance. A consuming Def must also be stuffable for
the red-channel mask to receive its material color. Keep directional draw
offsets absent because they override the common `(0,0,-0.1)` alignment offset.

The generated vector sources are under matching template directories in
`artwork_sources/`. They are available in a source checkout but are not included
in installed wheels. Named groups separate the shell, work surface, hardware,
and overlay-safe regions provided by each design. The Python generator is
authoritative: change `PALETTE` or geometry in
`tools/generate_generic_workbench.py`, then regenerate the SVG and catalog PNGs
with:

```bash
python3 tools/generate_generic_workbench.py --replace-source
python3 tools/generate_generic_workbench.py --check
```

The explicit `--replace-source` prevents regeneration from silently discarding
manual SVG edits. `--check` also validates PNG dimensions, modes, paths, and
SHA-256 values against the template catalog; after an intentional artwork
change, update the reported catalog hashes before expecting the check to pass.

For in-game coloring, place `rimworld-texture` at the Def's `texPath`, place
`rimworld-color-mask` beside it with the `_m` suffix, and use
`<shaderType>CutoutComplex</shaderType>`. The red mask channel controls the
frame, the green channel controls the inset work surface, and black areas keep
their neutral shading. A typical one-cell Def uses `<size>(1,1)</size>` and
`<drawSize>(1,1)</drawSize>`.

## Workflow

```bash
# Configure Scenario without echoing credentials or writing them to Git.
rw-art auth /path/to/mod/artwork/manifest.toml scenario

# Estimate, generate four finalized choices, then select one.
rw-art generate /path/to/mod/artwork/manifest.toml machine --estimate-only
rw-art generate /path/to/mod/artwork/manifest.toml machine --confirm-cost
# If one submitted option reaches terminal failure, estimate and explicitly replace only that slot.
rw-art generate /path/to/mod/artwork/manifest.toml machine --retry-failed
rw-art generate /path/to/mod/artwork/manifest.toml machine --retry-failed --confirm-cost
rw-art select /path/to/mod/artwork/manifest.toml machine 2
rw-art approve /path/to/mod/artwork/manifest.toml machine

# Manual generation remains a fallback.
rw-art prompt /path/to/mod/artwork/manifest.toml autoloader
rw-art intake /path/to/mod/artwork/manifest.toml autoloader ~/Downloads/image.png
rw-art approve /path/to/mod/artwork/manifest.toml autoloader

# Confirm all approved outputs still satisfy the manifest.
rw-art validate /path/to/mod/artwork/manifest.toml
```

State defaults to `$XDG_DATA_HOME/rw-art-pipeline/<package-id>`, or
`~/.local/share/rw-art-pipeline/<package-id>`. Use `--state-dir` on any command
to select another local archive. Raw files are addressed by checksum and are
never rewritten. Each archive carries a package marker; the tool refuses
unmarked or symlink-redirected state before deleting stale candidates.

Scenario credentials are read from `SCENARIO_API_KEY` and
`SCENARIO_API_SECRET`, or from the mode-0600 file created by `auth scenario` at
`$XDG_CONFIG_HOME/rw-art-pipeline/scenario.json`. Generation receipts contain
model IDs, exact parameters, job and asset IDs, dry-run results, and content
hashes, but never authorization material.

## Manifest

Paths are relative to `project.root`, itself relative to the manifest. Every
request defines one generated source and one or more game-ready outputs. An
output with `accent_color` hue-shifts sufficiently saturated source pixels while
leaving neutral steel untouched.

```toml
[project]
package_id = "Example.AuthorMod"
name = "Example Mod"
root = ".."

[[requests]]
id = "machine"
title = "Machine"
width = 128
height = 128
occupied_fraction = 0.78
alpha_required = true
background_removal = "none"
fit = "contain"
output_mode = "RGBA"
accent_saturation_min = 0.20
prompt = """Generate one isolated sprite ..."""
outputs = [
  { path = "Textures/Things/MachineAmber.png", accent_color = "#c79e38" },
  { path = "Textures/Things/MachineBlue.png", accent_color = "#468dd1" },
]
generation = { provider = "scenario", model_id = "model_openai-gpt-image-2", candidates = 4, parameters = { width = 1024, height = 1024, quality = "medium", background = "opaque", numOutputs = 1 } }
```

`fit = "contain"` preserves the complete sprite inside a transparent canvas.
Use `fit = "cover"`, `alpha_required = false`, and `output_mode = "RGB"` for
opaque promotional artwork that must fill an exact aspect ratio.

Set `background_removal = "light-checkerboard"` only when a generator renders
the transparency grid into an otherwise opaque sprite. The processor removes
only bright, near-neutral pixels connected to the canvas border; the selected
mode is part of the receipt's immutable processing contract.

Image generation cannot reliably produce linked-texture atlas topology. Such
atlases should be assembled by deterministic project-specific code and then
listed as ordinary final outputs once their contract is known.
