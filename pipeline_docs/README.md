# Pipeline documentation

These guides describe the tape-restoration pipeline. The upstream Real-ESRGAN
research and training documentation is retained separately in [docs/](../docs/).

| Guide | Purpose |
|---|---|
| [Preparation](prepare-video.md) | Progressive masters, masking and audio alignment |
| [Audio and picture timing](av-timing.md) | Why timing can fail, how it is preserved, and what delivery checks establish |
| [Outputs and resume](storage-layout.md) | Output locations, restarting work, copying and verification |
| [DV profiles](dv_profile.md) | DV filter settings and their tradeoffs |
| [Enhancement process](../Enhancement_Process.md) | How the processing stages fit together |

Document supported behavior and the reasons for its constraints. Label
experiments clearly and support quality claims with reproducible comparisons.
