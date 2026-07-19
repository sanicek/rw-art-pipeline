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
