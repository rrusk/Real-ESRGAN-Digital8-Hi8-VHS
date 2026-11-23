# External references

The AI pipeline is self-contained apart from its system tools, Python packages,
model weights, and optional RIFE binary. See [installation](INSTALL.md).
No runtime step requires a video-preservation-tools checkout.

## Bundled inspection tools

Preparation uses [pipeline_tools/probe_video.py](pipeline_tools/probe_video.py)
for scan type, field order, aspect ratio and input suitability. Set
`VIDEO_PROBE=/path/to/probe_video.py` to override it. If the bundled tool is
absent, preparation also looks for `probe_video.py` on `PATH`.

[pipeline_tools/check_dv_date.py](pipeline_tools/check_dv_date.py) audits DV
recording times throughout a capture. It shares a parser with the batch's
first-frame timestamp reader, `dv_recording_time.py`.

## Optional preservation tools

The separately maintained companion project `video-preservation-tools` provides
capture guides and surrounding preservation workflows. The paths below refer to
that project's checkout, not to files bundled with this pipeline:

| Tool or guide | Purpose |
|---|---|
| `capture/Video_Capture.md` | Capture principles and workflows |
| `video_processing/enhance_video.sh` | CPU Lanczos alternative to neural upscaling |
| `compare/dual_compare.sh` | Synchronized comparison playback |
| `chapters/chapters.sh` | Mark, edit and add chapters to an MKV or MP4 without re-encoding |

Install optional commands on `PATH` to use them. The repositories maintain their
own tools and profiles; matching profile names describe the same source types,
but filter strengths can differ. Each repository documents its own interfaces.
