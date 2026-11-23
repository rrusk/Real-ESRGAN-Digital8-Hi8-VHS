# prepare_video.sh

`prepare_video.sh` turns a capture into a *master*: one progressive, full-chroma
file that Real-ESRGAN can be pointed at. It is the first of two processing stages.

```text
capture  ──prepare_video.sh──▶  master  ──video_upscale_pipeline.py──▶  upscaled video
```

Each stage has one job. `prepare_video.sh` deinterlaces, optionally masks the
bottom edge, and encodes a near-transparent intermediate. It does not
upscale, denoise for the model, or choose model settings; those belong to the
pipeline, which reads the master and writes the final video.

## What it does

**Deinterlaces.** Tape sources are interlaced and Real-ESRGAN operates on
progressive frames. `bwdif` mode 1 (the default) turns each field into its own
frame, giving field-rate output near 60 fps and preserving the ~16.7 ms between
fields — worth having for fast motion, at roughly double the upscaling time
because there are twice as many frames.

`--mode0` produces one frame near 30 fps per input frame instead, reducing
temporal resolution relative to field-rate output.

Leave RIFE off for mode 1 output, since the frame rate is already field-rate.
The pipeline runs RIFE only when given `--rife`, so pass nothing.

**Masks the bottom edge, if asked.** `--mask N` blacks out N pixels. See below.

**Encodes at CRF 12, yuv444p.** This is an intermediate that will be read by a
model and then re-encoded, so it is kept well above the quality the final
output needs; detail discarded here cannot be recovered later. `--crf` changes
it, and rarely should. Chroma is upsampled to 4:4:4 to avoid a second
subsampling round trip.

**Tags NTSC colour.** The master is tagged SMPTE 170M, the colour of NTSC
standard definition, without converting its pixels. Preparation therefore
refuses a source whose colour tags name another standard, such as BT.709 (HD
and many H.264 recordings) or BT.470BG (PAL), before creating anything.
Untagged sources, which include DV and most lossless captures, are taken to be
NTSC.

**Preserves audio.** By default the audio is encoded to AAC 192k and the master
is an `.mp4`. This includes AC-3 and MP2 from DVD-derived MPG files. Existing
AAC is copied without re-encoding when no timing adjustment is needed. Sources
without audio stay silent.

`--pcm` preserves retained PCM samples losslessly and switches the container to
`.mkv`, since the supported FFmpeg MP4 muxer cannot hold this PCM audio.

The default avoids an extra audio encode during the AI stage: already-AAC
audio can be copied to the final MP4. `upscale_dv_batch.sh` passes `--pcm` for
native Digital8 captures so preparation retains PCM samples. The original
capture remains the preservation source in either workflow.

## The one decision: `--mask`

Analog tape has a rotating head, and the switch between heads leaves a band of
noise along the bottom edge of every frame. Masking replaces it with black so
the model is not asked to reconstruct it. The value is a pixel count and should
be even; an odd number misaligns the mask boundary.

**The mask follows the signal path, not the file format.** Anything that passed
through an analog tape head has the noise and needs a mask. Anything recorded
digitally end to end does not, and masking it blacks out real picture. Around 8
pixels is typical for analog tape; the default is 0.

This is why `prepare_video.sh` infers nothing from filenames. A tape played
through a DV converter is `dvvideo` in every respect a probe can see, identical
to a camcorder's own digital recording, and the two need opposite masks. Only
whoever captured the tape knows which it was, and a naming convention is
usually how that was written down.

Encode it in a wrapper that passes an explicit `--mask`, and the shared tool
stays correct for captures named by anyone.

`examples/prepare_tape_capture.sh` is one such wrapper, and
`examples/enhance_tape.sh` another that goes on to upscale; both read one table
of names, `examples/capture_names_lib.sh`, written to be copied and edited.

To find the right value, `--test` processes the first 30 seconds only:

```sh
./prepare_video.sh capture.dv --mask 8 --test
```

Play the result and look at the bottom edge. A flickering line still showing
means the mask is too small; a black band eating picture means it is too large.

For a 30-second test of a DVD-derived Hi8 MPG, AAC is automatic:

```sh
./prepare_video.sh --test --yes /path/to/dvd-extract.mpg
```

Add `--mask N` if the DVD picture has visible head-switching noise.

## When the detected field order is wrong

Preparation detects whether a capture is interlaced and which field comes
first, and its pre-flight summary states the result and how it was decided.
[Enhancement
process](../Enhancement_Process.md#source-inspection-and-timestamp-repair)
lists the steps.

DV, DVD MPEG-2 and interlace-flagged files are settled by their metadata. A
lossless capture without flags, such as HuffYUV from a USB capture device, is
judged from its pixels, sampled across the recording.

When detection cannot settle the result:

- If the source is known to be interlaced but its field order is unknown,
  preparation stops with directions for choosing the order. This can happen
  when all sampled frames are still.
- If the scan type itself is unknown, preparation treats the source as
  progressive and prints a warning.

A `--test` master shows a wrong result:

- motion that jerks back and forth means the fields were taken in the wrong
  order;
- comb-like teeth along the edges of moving objects mean an interlaced source
  was not deinterlaced.

`--field-order tff`, `bff` or `progressive` replaces the detection, including
the bottom-field-first assumption for DV:

```sh
./prepare_video.sh --test --mask 8 --field-order tff capture.avi
```

The pre-flight summary then reports the order as set by `--field-order`, and
the master's comment tag records it. To see what FFmpeg's own pixel check
finds, run `ffmpeg -i capture.avi -vf idet -frames:v 1000 -an -f null -`.
The last lines count frames judged top-field-first, bottom-field-first and
progressive.

## Where the output goes

Both the master and the log go inside one run directory. A driver passes
`--output-dir` and `--log-dir` pointing into the run it already created;
run standalone, `prepare_video.sh` creates a run of its own, and removes it
again if the preflight prompt is declined (exit 3). See
`pipeline_docs/storage-layout.md`.

## Handing the master to the pipeline

```sh
./prepare_video.sh capture.dv --mask 8 --yes
python3 video_upscale_pipeline.py <master> --scale 2 --profile <profile>
```

Which `--profile` to use is a decision for `video_upscale_pipeline.py`, not for
this script — it selects the pre-filter chain applied before the model, and
depends on the source's noise and artefacts.

`prepare_video.sh` prints a suggestion from the source codec and bitrate as a
convenience, but a codec cannot tell an analog transfer from a digital
recording, so override it when you know the provenance. Run
`video_upscale_pipeline.py --help` for the full list.

For Digital8 captures, `upscale_dv_batch.sh` searches its input tree
recursively, groups files by their resolved recording dates, runs both stages,
and assembles one video per day; it needs no prearranged directory layout.

The [timing guide](av-timing.md) explains why day assembly rebuilds one soundtrack
from source PCM instead of appending AAC clips. It also describes MP4 edit
windows: both the per-capture AI output and the assembled day video fail their
checks when the video edit hides stored pictures, and neither is repaired
automatically.

### Audio timing validation

Audio and video need not start or end together in a capture. For example, a
DVD-recorder MPG can begin audio before its first picture. Simply setting both
streams' starts to zero would shift their relationship. Preparation measures
their decoded boundaries instead; see [audio and picture timing](av-timing.md)
for how the AI stage then preserves that alignment.

Preparation uses the first decoded video picture as the shared time origin.
Audio preceding that picture is trimmed; audio starting later receives leading
silence. Aligned audio retains its timing.

The audio then spans exactly the pictures: audio running past the last picture
is clipped, and a source whose audio ends before its last picture (a DVD
recorder commonly stops the audio track a few hundred milliseconds early)
receives trailing silence.

The master therefore always presents audio and picture of one extent, which is
what the pipeline's duration reconciliation and final A/V check require.

Added silence fills time for which the source contains no audio. The pre-flight
summary and completion report state it in samples, milliseconds and pictures.

The master's container comment records the same information, so it stays with
the file: `audio: ... silence appended under the last pictures, which have no
audio in the source`.

No picture is removed; only audio is trimmed or filled. Internal audio
timestamp discontinuities cause preparation to fail and should be corrected
upstream.

The log directory contains `audio_timing_plan.json` and, after successful
verification, `audio_timing_verified.json`. Verification checks decoded starts,
sample counts, continuity and picture extent, allowing AAC encoder padding.
`--test` applies the same alignment to the first 30 seconds of output.
