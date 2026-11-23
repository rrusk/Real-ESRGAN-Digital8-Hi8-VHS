# Inspection and helper tools

Start with the [single-capture workflow](../README.md#prepare-and-upscale-one-capture)
to prepare and upscale a video. The tools here inspect sources, check results,
or support those entry points. Pass input paths explicitly to use them with
your recordings.

Run commands from the repository root with the virtual environment active.

## Inspect sources and check results

| Tool | Purpose |
|---|---|
| `probe_video.py` | Report scan type, field order, display geometry, chroma and bitrate health; used by preparation |
| `check_dv_date.py` | Audit recording-time metadata throughout a native DV capture |
| `check_daily_ai_distribution.sh` | Check that a native DV batch's assembled day videos contain the expected captures, chapters and duration |
| `prepare_audio_timing.py` | Plan and verify preparation's audio alignment against decoded picture boundaries |

For options and input formats:

```bash
python pipeline_tools/probe_video.py --help
python pipeline_tools/check_dv_date.py --help
```

### Recording dates in native DV

A native MiniDV or Digital8 file can contain the camcorder's recording time.
An analog tape converted to DV instead records the transfer time, so that
metadata does not establish when its footage was recorded.

The batch's fast probe reads the first frame. `check_dv_date.py` checks all
frames for missing metadata, disagreement and clock discontinuities. A plausible
but incorrect camera clock still needs independent evidence to identify it.

To review a batch's proposed day grouping without processing the captures:

```bash
./upscale_dv_batch.sh --input-root /path/to/captures --dry-run \
    --time-source subcode --on-anomaly prefer-subcode
```

The plan reports unusable or conflicting timestamps and saves `day-tree.txt`,
`day-plan.tsv` and `run.json` under the temporary plan directory printed by
the command. Repeating the same grouping and time-source combination replaces
that plan.

### Check an assembled batch

```bash
./pipeline_tools/check_daily_ai_distribution.sh --run-dir /path/to/run
```

The audit checks source captures against the assembled outputs, including a
capture whose day video was never produced. It reports missing day videos,
chapter-name differences and duration mismatches. With no arguments it selects
the newest run.

## Output directories and internal helpers

The [output and resume guide](../pipeline_docs/storage-layout.md) describes the
default location, `VIDEO_PRESERVATION_ROOT`, saved state, and copying a
finished run. You can keep inputs anywhere and choose another output root.

| Helper | Role |
|---|---|
| `dv_recording_time.py` | Read first-frame DV recording times for batch grouping |
| `run_lib.sh` | Shared shell functions for run directories and manifests; source this library rather than executing it |
| `write_run_md.py` | Render a readable summary from a run manifest |
| `run_names.py` | Resolve source labels and order runs by manifest start time |

## Specialized repairs

### Shortened MP4 video edit

An MP4 can store every encoded picture while its edit list tells a player to
stop showing video too soon. Joining clips with GPAC 2.0's `MP4Box -cat`
produces this: the gaps opened by each clip's audio endpoint are left out of
the video's edit duration.

The symptom is missing final pictures, although a packet count still looks
correct. `repair_mp4_video_edit.py` extends that video window without
re-encoding or changing sample timestamps or audio edits.

The pipeline never runs this repair. Its per-capture delivery check fails when
a video edit hides stored pictures and leaves the file unchanged, and batch day
assembly must pass its decoded picture-count check. See
[MP4 edit windows](../pipeline_docs/av-timing.md#mp4-edit-windows-can-hide-intact-pictures)
for both checks and their limits. Use the tool on an existing file once you
have diagnosed the fault.

For an existing affected **complete pipeline output**, work on a copy:

```bash
cp /path/to/complete-output.mp4 /path/to/repaired-output.mp4
python pipeline_tools/repair_mp4_video_edit.py /path/to/repaired-output.mp4
```

The command changes the named file **in place** when an extension is needed;
there is no preview or `--apply` mode. It validates the supported single-edit
layout and replaces the file atomically. It rejects hidden leading pictures and
unsupported layouts.

Do not use it on deliberately trimmed MP4s: extending their edit window could
expose pictures you meant to exclude. It cannot recover pictures missing from
the encoded media.

### Missing AAC encoder delay

AAC saved in Matroska by FFmpeg 4.4 loses the instruction to skip the encoder's
startup samples. The soundtrack then plays one block late (1024 samples, about
21.33 ms at 48 kHz). The pipeline extracts audio to `.m4a` and fails a
delivered file whose delay is missing, unless `--force` overrides that check.

The defect can therefore be present in outputs made before this repository's
first public release, in their unfinished runs' saved `.mka` audio, and in any
output delivered under `--force` after a failed delay check.

`repair_aac_priming.py` repairs such files. It compares decoded audio with the
original source at three positions, and repairs only the diagnosed one-block
offset. The source and the run's
`logs/audio_timing_plan.json` must remain available. A different offset or an
inconclusive measurement needs review, rather than an automatic adjustment.

Measure and report without writing, then optionally write repaired copies:

```bash
python pipeline_tools/repair_aac_priming.py /path/to/run
python pipeline_tools/repair_aac_priming.py --apply \
    --output-dir /path/to/repaired-copies /path/to/run
```

Deliverable repair leaves the originals unchanged. Copies go under
`<output-dir>/<run>/videos/` with repair reports and checksum manifests. The
compressed picture and audio packets must remain identical, the measured
offset must be zero, and delivery checks must pass.

For an unfinished run whose saved `.mka` lacks the delay, stop processing and
diagnose its saved audio first:

```bash
python pipeline_tools/repair_aac_priming.py --run-audio /path/to/run
python pipeline_tools/repair_aac_priming.py --apply --run-audio /path/to/run
```

This mode writes `.m4a` inside the run and updates its resume state, keeping
the previous state as `source_measurements.json.before-aac-priming`. Resume
through the entry point that owns the run. If its master is still available,
resume can re-extract audio instead of using this repair. The repair mode does
not use `--output-dir`.
