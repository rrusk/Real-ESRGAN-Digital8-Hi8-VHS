# Real-ESRGAN for Digital8, Hi8 and VHS

A [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) fork for restoring
camcorder and analog-tape captures. It prepares progressive masters, applies
source-specific filtering, and upscales with neural models. Long videos use
chunked processing with saved checkpoints and delivery checks.

The primary use case is NTSC Hi8 played through a Digital8 camcorder and
captured as DV over FireWire. Native Digital8/MiniDV, VHS transferred through a
DV converter, and DVD-derived MPEG-2 are also supported, with less field testing.

Sources must be NTSC standard definition. Masters and outputs are tagged with
NTSC colour (SMPTE 170M) and the colours are not converted, so preparation
refuses a source tagged with another standard, such as BT.709 (HD and many
H.264 recordings) or BT.470BG (PAL). PAL and HD sources are not supported.

## Requirements and installation

Use Linux with GNU command-line tools, Bash, an NVIDIA CUDA GPU, and **Python
3.10 or 3.11**. The pinned dependencies do not support Python 3.12 or later.
Run commands from the repository root.

```bash
git clone https://github.com/rrusk/Real-ESRGAN-Digital8-Hi8-VHS.git
cd Real-ESRGAN-Digital8-Hi8-VHS
dev_tools/setup_env.sh --system-deps
source venv/bin/activate
```

Setup installs dependencies, repairs BasicSR imports, downloads model weights,
and runs an isolated GPU smoke test. System package installation uses `sudo`;
omit `--system-deps` if the packages are already installed. Use `--python
python3.11` to select an interpreter. See [INSTALL.md](INSTALL.md) for manual
installation, supported configurations, and troubleshooting.

## Prepare and upscale one capture

The commands below are for Hi8 transferred to DV, which starts with an
eight-pixel bottom mask. For any other source, take the mask and profile from
the table further down; a capture that is neither a DV file nor a DVD uses
`balanced`.

```bash
./prepare_video.sh --mask 8 capture.dv
```

Preparation prints the master's exact path. Use that path as the pipeline input:

```bash
python video_upscale_pipeline.py --profile hi8dv /path/to/prepared_master.mp4
```

Preparation deinterlaces with `bwdif` at field rate (59.94 fps) and writes an
H.264/yuv444p CRF-12 master. The default final output is a
2× H.264/yuv420p MP4 at CRF 23, with source audio and sample aspect ratio
preserved. This is a viewing copy; retain the original capture for preservation.
Use `--scale 4` or `--crf N` to change final scale or quality.

Preparation detects whether the capture is interlaced and which field comes
first, and its pre-flight summary states the result. It stops and asks for the
order when a capture is interlaced but shows neither. If a `--test` master
shows jerky motion or comb-like edges, set the order with
`--field-order tff|bff|progressive`; see
[when the detected field order is wrong](pipeline_docs/prepare-video.md#when-the-detected-field-order-is-wrong).

**Masking is explicit.** `prepare_video.sh` defaults to `--mask 0`, regardless
of filename or profile. Use a suitable even value for analog head-switching
noise; native digital recordings normally need no mask. `--test` prepares only
the first 30 seconds for inspection. Profile suggestions use codec and bitrate,
which cannot establish whether a DV file contains an analog transfer.

Choose the AI profile from the capture's signal path:

| Source | Profile | Typical preparation mask |
|---|---|---|
| Hi8 via Digital8/FireWire DV capture | `hi8dv` | 8 |
| Native Digital8 or MiniDV | `dv` | 0 |
| VHS via S-Video to DV converter | `vhsdv` | 8 |
| VHS via composite to DV converter | `vhsdv_composite` | 8 |
| DVD-derived MPEG-2 | `balanced` (`aggressive` below 3 Mbps) | Inspect source |
| Any other capture path | `balanced` | 8 for analog tape, 0 for a digital recording |

**Not a DV file or a DVD? Use `balanced`.** The `dv`, `hi8dv` and `vhsdv`
profiles are tuned for captures that passed through a DV codec. A capture made
any other way does not have DV's compression artefacts: for example, a USB or
PCIe capture device, a lossless AVI (HuffYUV, FFV1, UtVideo, Lagarith), or an
H.264 file from a capture box or transfer service.

Such a file must still be NTSC standard definition; H.264 recordings are often
tagged BT.709 and are then refused (see the colour note at the top).

`balanced` is the pipeline's default, and preparation suggests it for every
codec except DV and low-bitrate MPEG-2, so `--profile` can be left out:

```bash
./prepare_video.sh --mask 8 capture.avi
python video_upscale_pipeline.py /path/to/prepared_master.mp4
```

The mask follows the tape, not the capture device. Analog tape carries
head-switching noise however it was digitised, so start at 8 and check it with
`--test`. Footage a camera recorded digitally needs no mask.

If `balanced` leaves heavy noise, compare `aggressive`. If it shows white ghost
lines or ringing around dark edges, compare `halo`. To compare, cut a short
clip with [`extract_test_clip.py`](ai_model_tests/README.md) and run the
pipeline on it once per profile.

Pass `--profile` explicitly for DV captures. `--help` lists all profiles and
model options. Adapt the
[example wrappers](examples/README.md) if you want filenames to select a mask
and profile for your own collection.

## Interrupt and resume

Resume the current standalone pipeline run with its saved input and settings:

```bash
python video_upscale_pipeline.py --resume
```

Select a particular unfinished run when another run has become current:

```bash
python video_upscale_pipeline.py --resume --run-dir /path/to/run
```

Use these resume commands even if the prepared master is gone. A standalone
run can delete a verified disposable master once its audio, measurements, and
video checkpoint are durable. `--keep-input` retains it. Starting again with
only the original input command creates a new standalone run.

Ctrl+C stops processing and preserves resumable work. Touch
`<run-dir>/work/processing_chunks/STOP` to stop between chunks, or use
`--max-runtime HOURS`. For a run started by `examples/enhance_tape.sh`, resume
with that wrapper's `--resume` instead.

## Batch native DV captures by recording day

The batch driver searches recursively for `dv.*.avi`, resolves recording times,
upscales each capture with profile `dv` and mask 0, and assembles a chaptered MP4
for each day. Use it for native digital recordings: an analog transfer's DV
subcode dates the transfer rather than the original footage.

```bash
./upscale_dv_batch.sh --input-root /path/to/dv --dry-run
./upscale_dv_batch.sh --input-root /path/to/dv
```

The default `--group-by recording-time` uses resolved recording timestamps.
`--group-by directory` uses enclosing `YYYYMMDD[_Description]` directories.
`--time-source auto` prefers a usable filename timestamp and checks DV subcode.
To plan from subcode and exclude unusable subcode timestamps:

```bash
./upscale_dv_batch.sh --input-root /path/to/dv --dry-run \
  --time-source subcode --on-anomaly prefer-subcode
```

Plans contain `day-tree.txt`, `day-plan.tsv`, and `run.json` under
`${TMPDIR:-/tmp}/upscale-dv-plan-<group-by>-<time-source>`. Each invocation
replaces the plan for that combination. Dry runs do not process media and
cannot take `--run-dir` or `--run-id`. See `--help` for anomaly policies and
`pipeline_tools/check_dv_date.py --help` for an all-frame subcode audit.

Rerun the same batch command to adopt matching unfinished work. After correcting
a recorded failure, add `--retry-failed`. A batch also accepts top-level `STOP`
and `STOP_DAY` files to stop after the current capture or day.

## Output and verification

Runs live under `~/VideoPreservation/ai/<source>_<YYYYMMDD-HHMMSS>/`, with
`videos/`, `logs/`, resume `state/`, and temporary `work/`. Manifests `run.json`
and `run.md` record settings and sources. Set `VIDEO_PRESERVATION_ROOT` to
relocate the root, or `--run-dir` to select a run directory. Single-source output
names include processing settings; assembled day videos use their day name.

Your input files can remain wherever you keep them. A separate capture tree,
DVD transfer, companion checkout or second archive drive is not required for
the single-capture workflow.

Delivery checks cover picture counts, timing, audio priming and A/V alignment,
color tags, CRF, pixel format, and display geometry. The [audio and picture
timing guide](pipeline_docs/av-timing.md) explains why these checks are
separate, how chunking preserves sync, and when repairs apply.

Add `--verify-full-decode` to decode every final video frame as well. Copying,
checksum comparison, master cleanup, and run ownership are documented in the
[storage guide](pipeline_docs/storage-layout.md).

RIFE interpolation is optional. It synthesizes intermediate pictures and can
introduce motion or overlay artifacts. Use `--rife` only with a frame-rate
master made with `prepare_video.sh --mode0`; the pipeline rejects it on inputs
above 45 fps. The normal workflow preserves field-rate motion without RIFE.
See [installation](INSTALL.md#optional-rife-interpolation) for the binary.

## Further documentation

| Guide | Contents |
|---|---|
| [Enhancement process](Enhancement_Process.md) | Processing stages, quality, models and validation |
| [Preparation](pipeline_docs/prepare-video.md) | Deinterlacing, masking, audio alignment and masters |
| [Audio and picture timing](pipeline_docs/av-timing.md) | Source offsets, AAC delay, chunking, MP4 edit windows and validation limits |
| [Outputs and resume](pipeline_docs/storage-layout.md) | Find results, resume work, copy and verify a finished run |
| [DV filtering](pipeline_docs/dv_profile.md) | Profile parameters and tradeoffs |
| [Pipeline tools](pipeline_tools/README.md) | Probing, audits and repair utilities |
| [Model comparisons](ai_model_tests/README.md) | Controlled clips and comparison grids |
| [Contributing](docs/CONTRIBUTING.md) | Development checks and pull requests |

The optional companion project `video-preservation-tools` provides capture
guides, a CPU Lanczos alternative, chapters and comparison playback. These tools
are maintained separately and are not bundled here. This pipeline has no runtime
dependency on that checkout. See [external references](EXTERNAL_REFERENCES.md).

## License and upstream project

Distributed under the [BSD 3-Clause license](LICENSE). The Real-ESRGAN code and
research are by Xintao Wang and the upstream contributors. Retained upstream
research and training documentation starts at [README_Original.md](README_Original.md);
it describes upstream interfaces, not this fork's installation procedure.
