# Optional workflow examples

The normal preparation and upscaling commands accept your input path and
explicit settings. Start with the [single-capture guide](../README.md#prepare-and-upscale-one-capture)
if you have not run the pipeline before.

These scripts demonstrate how to automate repeated work by choosing settings
from filenames. Their naming rules and processing recipe are example policies.
Read and adapt them before using them on another collection.

## Available examples

| Script | Purpose |
|---|---|
| `prepare_tape_capture.sh` | Choose a bottom mask from a filename and run preparation |
| `enhance_tape.sh` | Choose a mask and profile, then prepare and upscale within one run |
| `capture_names_lib.sh` | Share the filename-to-settings policy between the two wrappers |

A DV file's codec cannot identify whether it came from an analog transfer or
a native digital recording. These examples use filename prefixes as a record of
that provenance. Check the real recording path before adopting their policy.

## Naming policy in these examples

| Name | Assumed recording path | Mask | Profile |
|---|---|---|---|
| `hi8*` | Hi8 analog via Digital8/FireWire | 8 | `hi8dv` |
| `vhs*` | VHS through camcorder S-Video passthrough | 8 | `vhsdv` |
| `YYYYMMDD*` (including date ranges) | A Hi8 analog-to-DVD transfer | 8 | `hi8dv` |
| `dv.YYYYMMDD-HHMMSS.*` | A native Digital8 capture | 0 | `dv` |

This policy refuses unrecognized names. A wrong mask can leave head-switching
noise or black out real picture, and a wrong profile filters for another
source. Those mistakes may become apparent only after a long GPU run.

A date-prefix filename is only a naming convention here; it does not establish
the signal path by itself. Edit `capture_settings` and `capture_names_help` in
`capture_names_lib.sh` to match your own filenames and equipment.

## Prepare and upscale

Replace the example filename with an existing capture:

```bash
examples/enhance_tape.sh --dry-run hi8_capture01.dv
examples/enhance_tape.sh hi8_capture01.dv
```

The example uses scale 2, CRF 23, no RIFE, field-rate preparation and AAC in MP4.
Change the example script when you need another recipe. Preparation and
upscaling both ask for confirmation. `--yes-upscale` skips the upscale prompt;
`--yes` skips both. `--dry-run` prints the commands without processing media.

The source, prepared-master identity and recipe are saved in
`state/enhance_tape.json`. The wrapper can resume its current unfinished run:

```bash
examples/enhance_tape.sh --resume
examples/enhance_tape.sh --resume --run-dir /path/to/run
```

Launching the same source with the same recipe also continues matching
unfinished work. A different source or recipe gets a new run. The standard
[output and resume guide](../pipeline_docs/storage-layout.md) explains the
run directory and current-run shortcut.

## Preparation only

```bash
examples/prepare_tape_capture.sh --dry-run hi8_capture01.dv
examples/prepare_tape_capture.sh hi8_capture01.dv -- --pcm --yes
```

Options after `--` are passed to `prepare_video.sh`. Run either example with
`--help` for its options.
