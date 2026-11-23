# AI Model Comparison

These tools compare Real-ESRGAN models without mixing model choice with changes
in source frames, pre-filter profile, scale, frame rate, or final encoding.

## Workflow

Start with a progressive master made by `prepare_video.sh`, then extract a short
clip containing representative faces, texture, motion, noise, and edges:

```bash
./ai_model_tests/extract_test_clip.py --duration 15 /path/to/prepared_master.mp4 00:10:00
```

Run the same clip through the default 2x model candidates:

```bash
./ai_model_tests/run_test_comparisons.py --profile hi8dv /path/to/test_clip.mp4
```

The defaults compare `RealESRGAN_x2plus` with `realesr-general-x4v3` at 2x.
Override `--models`, `--scale`, or `--profile` when testing another controlled
question. Use `--crf CRF` only when the encode quality itself is part of the
comparison; every model is then encoded identically, so the comparison stays
about the model.

Use the clip path printed by extraction and select the profile for your source.
`--force` is optional and authorizes discarding stale pipeline work; leave it
out when starting a new comparison.
Each model's pipeline invocation then asks for confirmation at its `Proceed?`
prompt before processing.

After two model runs, the orchestrator prints a command for
`compare_test_results.sh`. Its grid contains the source enlarged with nearest
neighbour, a Lanczos baseline, and the two model results.

Extracted clips default to `~/VideoPreservation/ai/`; select another directory
with `extract_test_clip.py --output-dir DIR`. Comparison outputs and logs go
under `~/VideoPreservation/ai/<run-id>/`. Keep the extracted clip with the
comparison if the run needs to be self-contained.

## Optional synchronized playback

You can inspect the output videos in your preferred player. For synchronized
comparison, `dual_compare.sh` is available separately from video-preservation-tools;
see [external references](../EXTERNAL_REFERENCES.md#optional-preservation-tools).
It is an optional viewer, not part of this checkout. A model test
produces time-aligned outputs by construction — the same clip through different
models — so `--aligned` skips straight to synchronized playback:

```bash
dual_compare.sh --aligned \
    --left-label RealESRGAN_x2plus --right-label realesr-general-x4v3 \
    /path/to/x2plus-output.mp4 \
    /path/to/general-output.mp4
```

Substitute the output paths printed by the comparison run.

Both play in adjacent mpv windows locked together, with the left video's audio
in the left channel and the right video's in the right, so a difference between
them is audible in one ear. `--diff` replaces that with a single window showing
a real-time pixel difference. The settings each output was produced under are
in the run's `run.json`.

Add `--video-aspect 4:3` when comparing files that carry no SAR/DAR metadata,
such as inference intermediates from `work/`. The pipeline writes the source
SAR to the delivered MP4 with `MP4Box -par`.
