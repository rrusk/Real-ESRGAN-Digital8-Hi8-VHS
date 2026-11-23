# Choosing a DV prefilter profile

Choose a profile from how the recording was made. Both a native digital
recording and an analog tape transferred through a DV converter can appear
as `dvvideo` to a probe; the codec alone does not distinguish them.

| Recording path | Starting profile |
|---|---|
| Native MiniDV or Digital8 recording | `dv` |
| Analog Hi8 played through a Digital8 camcorder | `hi8dv` |
| VHS through an S-Video-to-DV conversion path | `vhsdv` |
| VHS through a composite-to-DV conversion path | `vhsdv_composite` |

Other profiles are listed by `python video_upscale_pipeline.py --help`.
These are starting points for inspection, not a guarantee of the best result
for every camera, converter or recording.

## What the native DV profile addresses

Native DV may show compression blocks, ringing around edges and chroma
stair-stepping. Low-light camera noise can also be present. Analog transfers
can add different tape and playback defects, which is why their profiles are
separate.

The pipeline filters the prepared progressive master before neural upscaling.
The `dv` profile combines modest denoising, postprocessing and light sharpening.
Excess filtering can remove real detail or exaggerate edges, so compare a short
representative clip before committing to a long run.

## Current DV settings

For prepared input at or below 45 frames per second:

```text
hqdn3d=1.5:1.5:4:4,pp=ac,unsharp=3:3:0.25
```

For input above 45 frames per second:

```text
hqdn3d=1.5:1.5:2:2,pp=ac,unsharp=3:3:0.25
```

The temporal denoise values are halved for the higher frame rate because
successive pictures are closer together. These are the settings in
`video_upscale_pipeline.py`; the script remains authoritative.

## Why the filters are kept moderate

- `hqdn3d` reduces spatial and temporal noise before upscaling. Native DV often
  needs less filtering than an analog transfer; excessive denoising can smear
  movement and fine texture.
- `pp=ac` combines horizontal and vertical deblocking with deringing. It targets
  compression blocks and ringing around edges that the upscaler can exaggerate.
  See FFmpeg's [postprocessing filter documentation](https://ffmpeg.org/ffmpeg-filters.html#pp).
- `unsharp=3:3:0.25` adds light edge sharpening. Stronger sharpening can create
  halos and amplify existing ringing, so the profile keeps this step modest.

A profile with the same name in another project can use different settings;
compare the actual filter chain when comparing outputs.

## Check the result on your material

Use the [model-comparison tools](../ai_model_tests/README.md) to inspect short
clips with faces, fine texture, movement and low-light noise. Keep the input,
preparation, scale and encode settings consistent when comparing models.

Look for smearing, lost texture, halos and unstable details as well as reduced
noise. Retain the original capture and choose the profile based on the result
you can inspect.
