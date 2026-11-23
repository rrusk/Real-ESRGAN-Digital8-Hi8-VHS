# Outputs, resume and storage

A normal run needs one input capture and a place for its outputs. The input
can remain wherever you keep it.

Commands below run from the repository root with the virtual environment active.

## Choose an output location

By default, runs are created under `~/VideoPreservation/ai/`. To use another
root for new work and default run lookup:

```bash
export VIDEO_PRESERVATION_ROOT="$HOME/video-work"
```

The pipeline then uses `$HOME/video-work/ai/`. Entry points that accept
`--run-dir /path/to/run` let you select a run directory directly. Changing the
root does not move existing inputs or runs. The root may be on another disk
or reached through a symbolic link.

The files printed by the command and recorded in `run.md` identify the result.
Keep your original capture independently of the generated viewing copy.

## One run, one directory

A run groups the output, settings and restart information for one invocation:

```text
<run-id>/
├── run.json       settings, input paths, outputs and completion metadata
├── run.md         readable summary
├── run.log        driver log, when the entry point writes one
├── videos/        finished videos
├── logs/          preparation, processing and validation reports
├── state/         information needed to resume
└── work/          temporary masters and processing checkpoints
```

Directories appear when needed, so a run may not contain every item above.
Retain the manifests, logs and state with the finished videos.

| Entry point | What its run contains |
|---|---|
| `video_upscale_pipeline.py` | One prepared source processed with the selected profile and model |
| `upscale_dv_batch.sh` | Native DV captures grouped into recording days |
| `ai_model_tests/run_test_comparisons.py` | The same clip processed by several models, with comparison results |

A preparation-only run retains its master under `work/masters/`. Preparation
and upscaling can share a run when their caller coordinates both stages.

## Run identity

The usual directory name is `<label>_<YYYYMMDD-HHMMSS>`: a source filename
stem, batch label or model-test label followed by its creation time. A prepared
master's recorded original capture supplies its label when available; otherwise
the generated preparation suffix is removed. Labels are made safe for use as a
directory name. The batch driver's `--run-id` can set the whole name.

The name stays the same when a run resumes. Automatic selection uses the
manifest's `started` timestamp, including its time zone, rather than the
directory's spelling. Use `--run-dir` when a run has missing or invalid start
metadata.

The manifest also records a class: `digital8` for native DV batches,
`hi8-dv` or `other` for single-source processing, and `model-test` for model
comparisons. This identifies the kind of output; it does not require a
particular archive layout.

## Interrupt and resume

For a standalone pipeline run, restore its saved input and settings with:

```bash
python video_upscale_pipeline.py --resume --run-dir /path/to/run
```

The saved state supplies the input and processing settings, even if a disposable
prepared master has already been removed. With `--resume`, the only additional
options accepted are `--run-dir` and `--verify-full-decode`; do not add an input
filename or replacement processing settings. Starting again with an input
filename and no `--run-dir` creates a new standalone run.

To resume a batch, repeat its original `upscale_dv_batch.sh` command with the
same input and settings. It adopts matching unfinished work. Add
`--retry-failed` after correcting a recorded failure. For a run started by an
[example wrapper](../examples/README.md), use that wrapper's resume command.

`ai/current_run` is a shortcut to the current unfinished standalone or example
tape run. `python video_upscale_pipeline.py --resume` uses it when the run
belongs to that entry point. If you have several unfinished runs, select one
explicitly with `--run-dir`. Starting another run can change the shortcut;
the previous run and its state remain available. The shortcut is removed
when its run finishes.

Ctrl+C stops the standalone pipeline and retains resumable work. Its exit
status is 130 for Ctrl+C or 129 when the terminal closes. Use `tmux` or
`screen` for work that should outlive a terminal window. A batch accepts
`STOP` or `STOP_DAY` at the top of its run directory to stop after the
current capture or recording day.

A new run declined before processing begins is removed. A failed attempt keeps
its logs. An existing run selected explicitly or resumed is left in place when
a processing prompt is declined.

### Required resume records

`--resume` reads the saved recipe, including its verification choice. A resumed
job requires processing metadata with a positive chunk duration. Every completed
chunk must have its original frame count in
`work/processing_chunks/chunk_source_frames.json`. Exact chunk coverage is
mandatory; container durations cannot replace missing frame counts.

Once the prepared master is released, resuming also requires complete saved
source measurements in `work/processing_chunks/source_measurements.json`.
Whole-video processing requires its saved pre-filter checkpoint after that
intermediate has been consumed, even if the master remains.

Batch state uses exactly eight-digit `YYYYMMDD` day keys. Descriptive suffixes
are allowed on source directory names, but are not stored as state keys.
Incomplete or incompatible state is rejected without automatic migration.
Keep the run intact and restore complete records from a backup, or start a
new run from a prepared master or the original captures. Saved processing
settings must match the retained work. Preserve the remaining processing files
while diagnosing the problem.

## What to retain

### `run.json` and `run.md`

These record the entry point, command, source paths, processing settings,
outputs, start and finish times, and exclusions. The readable summary describes
the files that exist and can be opened without this checkout.

A nonempty `finished` timestamp marks completion. Confirm the processing and
validation succeeded and no process is still writing before copying or deleting
a run.

### `state/` — resume ledger

Keep this directory. It contains information that cannot be reconstructed just
by looking at the output files: batch progress and recording-time decisions,
or the standalone pipeline's `pipeline_resume.json` recipe. A batch's
recording-time decisions determine how its captures are assembled.

The example tape wrapper uses `enhance_tape.json` for its own source and recipe.
`current_run` selects the containing run; its saved files remain the source of
truth for resumption.

### `videos/` — finished output

A standalone run produces one video. A native DV batch produces one
`<day>.mp4` per recording day, with chapters for its captures. Once a day is
assembled, the batch removes the individual upscaled clips used in that video.
The original captures may use any directory layout.

Default single-source filenames include processing settings. Assembled day videos use
their day names. Model-test outputs and their grid are meaningful as a set.

### `logs/` — processing and validation reports

A single-source run puts its logs directly under `logs/`, for example
`prepare.log`, `pipeline.log` and `metadata.json`. A multi-source run uses
`logs/<source-stem>/` to keep each source's reports separate. Driver logs
append when a run resumes.

### `work/` — temporary files and checkpoints

While processing, this directory can be large. It contains the prepared master,
filtered video and chunk checkpoints needed to continue an interrupted run.
Keep it until processing completes.

#### When the master is released

A standalone pipeline can release a disposable prepared master once its source
audio, measurements and display geometry are durably recorded and the remaining
video checkpoints are validated:

- A chunked run requires the complete split, validated picture coverage and
  timing, and its saved chunk-frame ledger.
- A no-chunk run requires its validated whole-file pre-filter checkpoint and
  matching audio/video durations.

The file must also be recognized as a generated master. Its resolved path must
be inside this run's `work/`, or a separate preparation run's `work/masters/`
whose `run.json` identifies `prepare_video.sh` and that run's directory. A
symbolic link does not establish that ownership.

The master must meet all three identity checks:

- Its filename contains `_master_from_prepare_video`.
- Its video is H.264/YUV 4:4:4 at CRF 12.
- Its container comment identifies a `prepare_video.sh master`.

`--keep-input` retains a recognized master. When a separate preparation run
becomes empty after cleanup, its logs and original capture provenance can be
folded into the processing run. Resume using the saved run directory rather
than the removed master filename.

A batch keeps its current master until that capture completes. Preparation-only
runs keep their masters. Successful finished runs normally have no remaining
`work/`, except for inputs deliberately retained.

## Copying a run to an external device

Copy a completed run as a directory. Here, `run` stands for its actual
directory name and the destination already exists:

```bash
rsync -a /path/to/run /media/archive/
rsync -acni --delete /path/to/run/ /media/archive/run/
```

The second command verifies contents by checksum and reports differences.
Accept the copy only when rsync exits successfully and prints no itemized
changes or errors. Exit status zero alone is insufficient: a dry run also
returns zero when it finds differences. Here `--delete` reports extra
destination files and `-n` prevents it from changing anything.

Keep both locations unchanged during verification. Retain the whole saved run,
including manifests, logs and state. Remove the local copy only after the saved
copy verifies successfully.

## Resolution order

1. An explicit script option such as `--run-dir` or `--preservation-root`.
2. A path derived from `VIDEO_PRESERVATION_ROOT` when it is set.
3. The corresponding path below `~/VideoPreservation`.
