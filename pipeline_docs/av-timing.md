# Audio and picture timing

A successful encode can still have late sound, duplicated pictures or a final
picture that players never show. The pipeline therefore checks the delivered
file, as well as the intermediate streams. This guide explains the problems
those checks address and when a repair tool is useful.

## Sources do not always start and end together

Audio and video have separate timestamps. A DVD-recorder MPG can contain audio
before the first decoded picture, or stop its audio before the last picture.
Native DV normally interleaves sound with its pictures, but the pipeline still
measures the input rather than assuming that a codec guarantees alignment.

Preparation uses the first decoded picture as time zero. It trims audio before
that point or inserts leading silence if audio starts later, then clips or pads
the end to cover the pictures.

For example, audio beginning 100 ms before the first picture loses those
opening 100 ms; audio ending 200 ms before the last picture receives 200 ms of
trailing silence.

No picture is removed to make the lengths agree. Added silence is reported
because it fills time for which the source contains no sound.

See [preparation's timing validation](prepare-video.md#audio-timing-validation)
for the reports and treatment of discontinuities. This preserves the source's
timestamp relationship; it cannot diagnose speech that was already out of sync
with lip movement in the original recording. Check representative source and
output passages by listening and watching.

## AAC encoder delay is a separate problem

An AAC encoder produces startup samples before the intended soundtrack. With
the FFmpeg AAC encoder used here, this delay is 1024 samples, about 21.33 ms at
48 kHz. The MP4 container records an instruction to skip them. Keeping the
compressed audio packets while losing that instruction makes the soundtrack
play one block late, even when both streams appear to start at zero.

FFmpeg 4.4 does not write that instruction into Matroska (`.mka`), so the
pipeline saves extracted audio in an MP4 audio container (`.m4a`). Already-AAC
audio is copied with its recorded delay; other supported source audio is
encoded to AAC once for the AI stage.

The pipeline checks the delay after extraction, on reuse of saved audio, and in
the final MP4. Where packets are copied, it also compares the opening decoded
samples with the saved reference.

This is why a duration comparison alone is insufficient: the same packets can
have the right length while the sound a player produces is shifted. AAC's
padding at the *end* of its last block is accounted for separately.

`repair_aac_priming.py` restores a delay lost this way. The pipeline never
saves audio in Matroska, and it fails a delivered file whose delay is missing
unless `--force` overrides that check.

The tool is for outputs made before this repository's first public release,
their unfinished runs' saved `.mka` audio, and any output delivered under
`--force` after a failed delay check.

It measures decoded sound against the original source before deciding whether
the specific one-block repair applies. It is not a general lip-sync adjuster.
See [repair usage](../pipeline_tools/README.md#missing-aac-encoder-delay).

## Chunking preserves one soundtrack

The AI stage extracts audio once for the whole input and processes the video
chunks separately. It joins the enhanced pictures and muxes the saved audio
back into the final MP4. It does not encode and append a separate soundtrack
for each chunk, so chunk boundaries do not accumulate AAC startup delays.
Saved audio and source measurements are part of the resume state.

Picture checks remain necessary. Without RIFE, enhancement must preserve the
exact input picture count: even one duplicated opening picture would move the
picture timeline relative to the soundtrack.

RIFE doubles the expected frame rate and picture count. Its endpoint handling
allows a difference of ±2 output pictures per chunk. The final allowance is:

- Without chunking: ±2 output pictures.
- With chunking: ±`max(2, 2 × chunk_count)` output pictures.

The allowance applies in both directions, although missing endpoint pictures
are its motivation. Frame rate, duration and timestamp continuity are checked
as well, and the final audio alignment must still pass.

## Combining captures into a day video

The optional native-DV batch workflow combines independently processed
captures. Simply appending their AAC tracks would accumulate startup delay and
final-block padding at each join. Instead, day assembly joins the pictures
with `mkvmerge`, concatenates PCM decoded from the original captures, and
encodes one AAC soundtrack for the whole day.

Each capture's PCM is trimmed or padded to its own processed picture length
before concatenation, so a small rounding difference cannot shift every later
capture. A difference greater than one **source** frame rejects the assembly.

Chapter starts come from the joined picture timestamps. The final day file must
decode exactly the sum of the component picture counts before assembly can
succeed and release the components.

## MP4 edit windows can hide intact pictures

An MP4 edit list tells a player which part of a track to present. If its video
window ends too soon, trailing encoded pictures remain in the file but are
outside that window. A packet count can look correct while normal playback or
decoding omits the last pictures.

Joining MP4 clips with GPAC 2.0's `MP4Box -cat` produces this: concatenation
extends the video edit without including the gaps that each clip's audio
endpoint opens. The pipeline does not join files that way. It joins chunks with
FFmpeg's concat demuxer and day videos with `mkvmerge`, and both delivery paths
check that the result needs no repair:

| Output path | Check |
|---|---|
| Per-capture AI output, with or without chunks (including batch components) | Reads the delivered MP4's timing metadata before final timing validation. A video edit that hides stored pictures fails the run, and the file is left unchanged for investigation. |
| Batch day assembly | Requires the final file to decode exactly the sum of its components' pictures. A hidden picture fails the assembly and leaves the components in place. |

Neither path repairs the edit first. A repair before the check would let it pass
on exactly the muxing fault it exists to find.

`repair_mp4_video_edit.py` extends the end of a supported single video edit to
include all stored pictures, for an existing file you have diagnosed. It
changes duration metadata without re-encoding, moving sample timestamps or
changing audio edits.

It rejects a hidden leading picture and unsupported layouts. An intentional end
trim cannot be distinguished from this defect, so only use it on a file known
to represent a complete pipeline output. See [edit-window repair
usage](../pipeline_tools/README.md#shortened-mp4-video-edit).

## What the final checks establish

For each capture, the AI pipeline checks several independent properties:

| Check | Why it is needed |
|---|---|
| Picture count, frame rate and timestamp span against the input | Detects missing or duplicated pictures, speed changes and timestamp gaps. |
| MP4 presentation boundaries | Detects pictures stored outside the player's video window. The file is not changed; a hidden picture fails the run. |
| AAC delay and decoded audio against the saved reference | Detects a lost skip instruction even if the compressed soundtrack survived. |
| Audio duration against picture count ÷ frame rate | Detects a soundtrack that ends too early or too late, independently of the container's overall duration. |
| First intended audio sample against the first picture | Detects a constant start offset that equal durations cannot reveal. The calculation accounts for the recorded AAC encoder delay. |

The start-position limit is one **output** picture in either direction. The
length limits are:

- Audio shorter than the pictures: at most one output picture.
- Audio longer than the pictures: at most one output picture plus one audio
  codec block for end padding.

At 60000/1001 fps and 48 kHz AAC, those length limits are about 16.68 ms short
and 38.02 ms long; the start-position limit remains ±16.68 ms. The extra codec
block is not permission for the sound to start late.

Unknown delay or unreadable start positions fail the check. Silent inputs have
no audio to compare.

Normally the boundary check reads MP4 timing metadata without decoding the
whole video. Add `--verify-full-decode` to the normal processing command to
also compare every normally decoded picture with the stored picture count:

```bash
python video_upscale_pipeline.py /path/to/prepared_master.mp4 \
    --profile dv --verify-full-decode
```

This can take many minutes. It checks decoding and coverage; watching remains
necessary to assess visual artifacts and the source recording's lip sync.
Some audio checks can be overridden with `--force`; a run that reports such a
failure has not established alignment. Keep the logs and investigate the
reported property before treating its output as verified.
