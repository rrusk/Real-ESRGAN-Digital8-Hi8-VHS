# AI Video Enhancement Process

The maintained AI workflow has two explicit stages:

1. `prepare_video.sh` converts a source into a high-quality progressive master.
2. `video_upscale_pipeline.py` filters that master and upscales it with Real-ESRGAN.

RIFE interpolation is optional. It is not used in the normal 60 fps workflow.
The command-line interfaces and validation in the scripts are authoritative;
this document explains how the stages fit together.

## Stage 1: Prepare a progressive master

This example uses Hi8 footage transferred to DV. Choose the mask and profile
from your recording's signal path; the [quick-start table](README.md#prepare-and-upscale-one-capture)
lists the other supported source types.

```bash
./prepare_video.sh --mask 8 source.dv
```

The default output and log locations are:

```text
~/VideoPreservation/ai/<run-id>/work/masters/
~/VideoPreservation/ai/<run-id>/logs/
```

The capture can live anywhere. Set `VIDEO_PRESERVATION_ROOT` to choose another
working root; the [output and resume guide](pipeline_docs/storage-layout.md)
explains the generated directories.

### Source inspection and timestamp repair

`prepare_video.sh` detects scan type and field order using source metadata,
frame flags, and `idet` fallbacks. Sources with unreliable timestamps are read
with regenerated presentation timestamps. DV-in-AVI files whose index hides the
embedded audio are temporarily remuxed so that the audio is recovered.

Field order is resolved in this order:

1. `--field-order tff|bff|progressive` sets it, and nothing is detected.
2. A DV codec uses the pipeline's NTSC bottom-field-first convention. DV-in-AVI
   commonly has no readable field-order metadata.
3. Otherwise, ffprobe's stream `field_order` settles it when it is
   `progressive`, `tt` or `bb`. The swapped values `tb` and `bt` establish
   only that the source is interlaced, because FFmpeg writes them contrary to
   its own definitions; the order comes from the next steps.
4. Then the frames' interlace flags: any interlaced frame settles interlacing,
   and the flags give the order. Frames with no flag set are not evidence of
   progressive video, because lossless capture codecs such as HuffYUV carry no
   interlace flag at all.
5. Otherwise, FFmpeg's `idet` filter examines 500 frames' pixels, read in five
   windows spread across the recording so that a still leader or blue screen
   at the start does not decide it. Interlacing is accepted only when one field
   order clearly dominates. Fine progressive detail can look like combing of
   both orders.

When a source is interlaced but nothing establishes its order, preparation
stops and asks for `--field-order`. When nothing establishes the scan type,
the source is treated as progressive and preparation prints a warning naming
`--field-order`. The pre-flight summary reports the resolved scan type and
field order and how it was decided. Inspect it before a long encode.

Sources must be NTSC standard definition. The master is tagged with NTSC
colour (SMPTE 170M) without converting the pixels, so preparation refuses a
source whose colour tags name another standard, such as BT.709 or BT.470BG.
Untagged sources, which include DV and most lossless captures, are taken to
be NTSC.

### Deinterlacing and chroma handling

Interlaced video is converted to progressive video with `bwdif`. The default is
field-rate output (`mode=1`), 59.94 fps for NTSC. Use
`--mode0` only when a frame-rate master is specifically wanted, normally so RIFE
can later interpolate it to field rate.

Before `bwdif`, chroma is resampled to YUV 4:4:4 with explicit chroma location.
This prevents FFmpeg from silently converting NTSC DV's YUV 4:1:1 to YUV 4:2:0
before deinterlacing and losing additional vertical chroma resolution. The
master is encoded as H.264/YUV 4:4:4 at CRF 12 with the `fast` preset. It is a
temporary high-fidelity AI input, not a distribution copy.

### Bottom mask

The default mask is zero. Pass `--mask N` explicitly to black out head-switching
noise from analog tape; eight pixels is a useful starting point. Native digital
recordings normally need no mask. Use an even height and inspect a short test.
Preparation does not infer the signal path from filenames. The example wrappers
can supply a collection-specific mask and profile from naming conventions.

### Audio

PCM is normally encoded as AAC 192 kbit/s for a reliable MP4 master. `--pcm`
preserves PCM in an MKV master. Audio already compatible with the selected
container is stream-copied.

The pipeline later checks audio and video durations before committing to a long
upscale. When timing alignment requires trimming or silence, preparation
processes the audio and records those adjustments in its reports and the
master's comment tag.

### Profile suggestion

Preparation prints a suggested `--profile` for the AI stage based on the source
type. It does not bake that profile's denoise or sharpening into the master.

## Stage 2: Real-ESRGAN enhancement

Use the master path printed by preparation:

```bash
python video_upscale_pipeline.py --profile hi8dv /path/to/prepared_master.mp4
```

The default operation is:

- 2x output using `RealESRGAN_x2plus`;
- no RIFE interpolation;
- an H.264/YUV 4:2:0 result at CRF 23 in MP4;
- output under `~/VideoPreservation/ai/<run-id>/videos/`.

Use `--crf CRF` to select another quality.

### Pre-filter profiles

Each chunk is filtered before Real-ESRGAN with a source-specific combination of
`hqdn3d`, `pp`, and optional `unsharp`. Current profiles include:

- `dv` for native MiniDV and Digital8 DV;
- `hi8dv` for Hi8 played through a Digital8 camcorder and captured over FireWire;
- `vhsdv` for VHS through S-Video-to-DV passthrough, tested with a DCR-TRV330;
- `vhsdv_nosharp` and `vhsdv_lightdenoise` when faces or clean captures lose texture;
- `vhsdv_composite` for composite passthrough;
- `balanced` (the default), `aggressive`, and `halo` for DVD-derived MPEG-2 and
  any capture that did not pass through a DV codec.

The pre-filter intermediate remains YUV 4:4:4 at CRF 12 regardless of the
final output's quality.

### Model selection

At 2x, the default `RealESRGAN_x2plus` performs a native 2x upscale.
`realesr-general-x4v3` may be selected for comparison; it upscales internally to
4x and then downsamples to 2x. That can suppress over-sharpening on noisy sources,
but the choice should be made from matched test clips. The tools in
`ai_model_tests/` automate that comparison.

At 4x, the default is `RealESRGAN_x4plus`. A 4x result is substantially larger
and should be compared against 2x output before spending the extra time and
storage on standard-definition tape footage.

### Chunking, validation, and resume

The pipeline chooses a chunk duration from available disk space, processes each
chunk independently, and records state in a per-job processing directory.
Outputs are validated for picture counts, timing, audio priming, A/V alignment,
CRF, pixel format, color metadata and display geometry. `--verify-full-decode`
adds a full decode scan of the final video.

Audio is extracted once for the input and muxed back after its enhanced video
chunks are joined, so chunking does not accumulate per-chunk AAC delays. The
[timing guide](pipeline_docs/av-timing.md) explains source alignment, encoder
delay, picture-count tolerances and the final checks, including the MP4
edit-window check that fails a delivered file whose video edit hides pictures.

Resume a standalone job with `python video_upscale_pipeline.py --resume`, adding
`--run-dir DIR` to select another unfinished run. It restores the saved recipe
and reuses validated checkpoints, including when the disposable master has
already been deleted. `--force` authorizes discarding incompatible work and can
permit continuation after some validation failures; use `--yes` to accept only
the processing plan. `--max-runtime HOURS` stops between chunks.

The two commands above accept your source path and explicit settings. To
automate repeated work, the [optional examples](examples/README.md) demonstrate
choosing a mask and profile from filenames and coordinating both stages in
one run. Adapt their naming policy before using them on your recordings, and
resume a wrapper-owned run through that same wrapper.

The prepared master is released before GPU processing, or deliberately kept,
according to who owns the run and what it has recorded. The
[master-release conditions](pipeline_docs/storage-layout.md#when-the-master-is-released)
describe the ownership checks and checkpoints required for resumption.

### RIFE

RIFE is disabled by default. Use `--rife` only with a roughly 30 fps master made
using `prepare_video.sh --mode0`; it doubles that master to approximately 60 fps.
The pipeline rejects RIFE on an existing 50/60 fps master because that would
produce an unnecessary 100/120 fps output.

### Measured processing cost

Across about 620 processed chunks, RIFE took **0.31 times the ESRGAN stage's
time**, with observed ratios from 0.24 to 0.43. This compares two routes to a
roughly 60 fps output, assuming ESRGAN cost scales with input picture count:

| Route | Relative GPU processing time |
|---|---|
| 30 fps master, ESRGAN plus RIFE | `E + 0.31E = 1.31E` |
| 60 fps master, ESRGAN without RIFE | `2E` |

On those measurements, the field-rate, no-RIFE route costs about **1.5 times as
much GPU time** (about 1.4–1.6 across the observed range). Its benefit is using
captured field-rate motion rather than synthesizing the intermediate pictures.
Interpolation can blur changing on-screen timestamp digits and introduce other
motion artifacts.

These figures describe the measured workload and GPU, not a speed guarantee for
another model, source size or hardware configuration.

ESRGAN measurements on an 8 GB RTX 4060 Ti showed **92% average GPU utilization
(100% peak)** while inference used about **1.2 GB of VRAM**, or about 2.2 GB
including the desktop. For this standard-definition workload, compute is the
bottleneck and tiling is unnecessary. More VRAM alone would not improve that
measured throughput; faster compute can.

Larger inputs, different models and other GPU workloads can change the memory
requirement.

Tiling is available only when invoking `inference_realesrgan_video.py` directly
with `--tile N`. `video_upscale_pipeline.py` does not expose or forward that
option.

### Final assembly and display geometry

Processed chunks are stream-concatenated, the preserved audio is muxed back in,
and the source sample aspect ratio is applied to the final container with
`MP4Box -par`. This metadata operation does not re-encode the video.

## Current quality summary

| Stage | Default |
|---|---|
| Prepared master | H.264, YUV 4:4:4, CRF 12, `fast` preset |
| Deinterlacing | `bwdif mode=1` (field-rate); `--mode0` is optional |
| AI scale/model | 2x, `RealESRGAN_x2plus` |
| AI pre-filter intermediate | H.264, YUV 4:4:4, CRF 12 |
| Final output | H.264, YUV 4:2:0, CRF 23, MP4 |
| RIFE | Disabled unless `--rife` is explicitly requested |
| SAR | Preserved with container-native metadata tools |
