#!/usr/bin/env python3
"""Read the recording date and time a camcorder wrote into a DV capture.

Every Digital8 capture carries the moment it was recorded in two places: the
filename written by the capture software, and the DV subcode the camcorder
itself wrote into the stream. This reads the second one, which is the only
source that can show footage from a different recording left on a reused tape
-- something no filename can reveal.

A DV frame carries the recording timestamp in up to three standard places:
the video auxiliary packs (VAUX), the subcode packs (SSYB), and the audio
auxiliary packs (AAUX). Which of them a camcorder actually populates is a
manufacturer decision, so no source is preferred here. Original Digital8
captures in the reference collection populate all three; a remux can erase
some of that corroborating evidence even when it stream-copies the video.

All three are read and the valid ones must agree. Agreement is the answer;
disagreement is reported rather than resolved, because a rule for choosing
between them would be a guess about hardware this tool cannot identify. A
caller that sees a disagreement should inspect the capture with
pipeline_tools/check_dv_date.py, which reports every source, its coverage
across the file, and any recording-time discontinuities.

Positions are defined, so packs are read from them rather than searched for. A
DV frame is DIF sequences of 150 blocks of 80 bytes. Within a sequence, block 0
is the header; blocks 1-2 are subcode, each holding six 8-byte SSYB packets
whose 5-byte pack follows a 3-byte header; blocks 3-5 are VAUX, each holding
fifteen 5-byte packs after a 3-byte header; and the audio blocks at 6 + n*16
(n 0..8) each hold one AAUX pack after a 3-byte header. Scanning the frame for
a pack id instead would match constantly inside compressed video data.

The first frame remains authoritative when its sources agree. If it has no
usable timestamp or its sources conflict, a bounded recovery scan looks up to
300 frames (about ten seconds of NTSC DV) farther into the stream. The result
records the recovery frame: a later timestamp is useful evidence for the day,
but it is not silently presented as the capture's start time. The batch can
therefore prefer a timestamp in the filename when the head was empty while
still checking the tape date and exposing a disagreement.

This is meaningful for Digital8 captures, whose subcode was written by the
camcorder at record time. An analog Hi8 or VHS transfer carries the date of the
TRANSFER in the same field, because that is when the DV stream was created, so
a cross-check against it would compare a capture to itself.

In an original Digital8 AVI all three sources are populated and agree --
measured on the reference capture: 20 VAUX, 40 SSYB and 20 AAUX timestamp packs
in frame 0, every one of them ff d4 e4 07 / ff a4 c8 d4. Whether that survives
being rewrapped as a raw .dv depends entirely on which ffmpeg muxer did it:

  -f data  writes each packet payload straight out, so the DV is byte-identical
           to the AVI's -- measured at 0 differing bytes across 8,887 frames --
           and all three sources survive.
  -f dv    invokes the DV muxer, which regenerates the auxiliary metadata even
           under -c:v copy: 836 bytes per frame differ, all of them in VAUX and
           subcode, where the timestamp packs come back as the 1970-01-01
           default. Picture and audio are untouched, so AAUX still carries the
           recording date, but the corroboration from the other two is gone.

The same applies to reading: a tool that pipes a container through ffmpeg -f dv
to parse it sees the muxer's regenerated metadata, not the file's, and will
report VAUX and SSYB as unset for an AVI whose packs are intact. Parsing the
container directly, as this does, avoids that entirely.

Usage:
  dv_recording_time.py FILE...        one result per file, in order
  dv_recording_time.py --from-stdin   NUL-separated paths on stdin

Output is one tab-separated line per file: path, timestamp, sources, and a
recovery note. The time remains empty when no bounded, internally consistent
subcode can be found. Every file is reported, so a caller can pair results with
inputs positionally.
"""

import datetime
import struct
import subprocess
import sys

DIF_BLOCK = 80
DIF_BLOCKS_PER_SEQUENCE = 150
SUBCODE_BLOCKS = (1, 2)                         # six 8-byte SSYB packets each
VAUX_BLOCKS = (3, 4, 5)                         # fifteen 5-byte packs each
AAUX_BLOCKS = tuple(6 + n * 16 for n in range(9))
SEQUENCES_525_60, FRAME_BYTES_525_60 = 10, 120000
SEQUENCES_625_50, FRAME_BYTES_625_50 = 12, 144000
HEAD_BYTES = 1 << 20                            # container header plus frames
RECOVERY_FRAMES = 300                           # about 10 seconds of NTSC DV

# Standard DV pack ids. Video and subcode carry 0x62/0x63; audio carries
# 0x52/0x53. All three describe the same event.
PACK_VIDEO_DATE, PACK_VIDEO_TIME = 0x62, 0x63
PACK_AUDIO_DATE, PACK_AUDIO_TIME = 0x52, 0x53

SOURCES = ("VAUX", "SSYB", "AAUX")
SOURCE_PACK_IDS = {
    "VAUX": (PACK_VIDEO_DATE, PACK_VIDEO_TIME),
    "SSYB": (PACK_VIDEO_DATE, PACK_VIDEO_TIME),
    "AAUX": (PACK_AUDIO_DATE, PACK_AUDIO_TIME),
}


def _bcd(value, tens_mask):
    return (value & 0x0F) + 10 * ((value >> 4) & tens_mask)


def _decode_date(pack):
    """Decode a DV recording-date pack: day, month, two-digit year.

    A decoded 1970-01-01 is rejected. It appears in DV streams as a generated
    or default value rather than a recording, and admitting it would let an
    unpopulated source contradict a populated one. This is a plausibility
    safeguard, not an assumption about any manufacturer.
    """
    day = _bcd(pack[2], 0x03)
    month = _bcd(pack[3], 0x01)
    year = _bcd(pack[4], 0x0F)
    # DV carries two digits. The conventional mapping used by DV software is
    # 00-24 -> 2000-2024 and 25-99 -> 1925-1999, which covers the lifetime of
    # the format; it cannot represent a recording made from 2025 onward.
    year += 2000 if year < 25 else 1900
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if (year, month, day) == (1970, 1, 1):
        return None
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None
    return (year, month, day)


def _decode_time(pack):
    """Decode a DV recording-time pack: second, minute, hour."""
    second = _bcd(pack[2], 0x07)
    minute = _bcd(pack[3], 0x07)
    hour = _bcd(pack[4], 0x03)
    if second > 59 or minute > 59 or hour > 23:
        return None
    return (hour, minute, second)


def _pack_positions(base, sequences):
    """Yield (source, offset) for every defined timestamp pack in one frame."""
    for sequence in range(sequences):
        seq_base = base + sequence * DIF_BLOCKS_PER_SEQUENCE * DIF_BLOCK
        for block in SUBCODE_BLOCKS:
            block_base = seq_base + block * DIF_BLOCK
            for packet in range(6):
                yield "SSYB", block_base + 3 + packet * 8 + 3
        for block in VAUX_BLOCKS:
            block_base = seq_base + block * DIF_BLOCK
            for packet in range(15):
                yield "VAUX", block_base + 3 + packet * 5
        for block in AAUX_BLOCKS:
            yield "AAUX", seq_base + block * DIF_BLOCK + 3


def inspect_frame(frame, sequences, base=0):
    """Return detailed recording-date/time evidence from one DV frame.

    Values are decoded from the defined pack positions, never found by scanning
    compressed picture bytes for a coincidental pack id.  The raw pack and its
    offset are retained for a diagnostic caller.  When every matching pack is
    invalid (the common 1970 default, for example), the first raw pack remains
    visible even though the decoded value is ``None``.
    """
    found = {
        source: {
            "date": None,
            "time": None,
            "date_offset": None,
            "time_offset": None,
            "date_raw": None,
            "time_raw": None,
        }
        for source in SOURCES
    }

    for source, offset in _pack_positions(base, sequences):
        pack = frame[offset:offset + 5]
        if len(pack) != 5:
            continue
        date_id, time_id = SOURCE_PACK_IDS[source]
        if pack[0] == date_id:
            value = _decode_date(pack)
            if found[source]["date_raw"] is None:
                found[source]["date_offset"] = offset - base
                found[source]["date_raw"] = bytes(pack)
            if value is not None and found[source]["date"] is None:
                found[source]["date"] = value
                found[source]["date_offset"] = offset - base
                found[source]["date_raw"] = bytes(pack)
        elif pack[0] == time_id:
            value = _decode_time(pack)
            if found[source]["time_raw"] is None:
                found[source]["time_offset"] = offset - base
                found[source]["time_raw"] = bytes(pack)
            if value is not None and found[source]["time"] is None:
                found[source]["time"] = value
                found[source]["time_offset"] = offset - base
                found[source]["time_raw"] = bytes(pack)

    return found


def _frame_offset(buf):
    """Offset of the first DV frame, or None if this is not a DV container.

    Type-1 DV AVI interleaves audio and video into one 00__ chunk; type-2
    splits them and the video chunk is 00dc. A raw .dv has no container.
    Anything else returns None, and the caller falls back to ffmpeg.
    """
    if buf[:4] != b"RIFF":
        # A raw DV frame starts with a header DIF block.  Its three-byte DIF
        # ID has section type 0, the reserved low bits set, sequence 0 and
        # block number 0.  Treating every non-RIFF input as raw DV made an MKV
        # or another supported container look like a corrupt frame at offset
        # zero instead of sending it through the metadata-preserving demuxer.
        if (len(buf) >= 4 and buf[0] == 0x1F
                and buf[1] & 0x0F == 0x07 and buf[2] == 0x00):
            return 0
        return None
    movi = buf.find(b"movi")
    if movi < 0:
        return None
    pos = movi + 4
    while pos + 8 <= len(buf):
        chunk_id = buf[pos:pos + 4]
        size = struct.unpack("<I", buf[pos + 4:pos + 8])[0]
        if chunk_id in (b"00__", b"00dc", b"00db"):
            return pos + 8
        if size == 0 or pos + 8 + size > len(buf):
            return None
        pos += 8 + size + (size & 1)
    return None


def _read_head(path, head_bytes):
    """The head of a DV stream, read directly or copied out by ffmpeg.

    A raw .dv or a DV AVI is read directly: no subprocess, and a whole batch
    can be probed in one process. Any other container is handed to ffmpeg,
    which stream-copies the DV frames without decoding them, so formats this
    parser does not know how to open are still readable.
    """
    try:
        with open(path, "rb") as handle:
            buf = handle.read(head_bytes)
    except OSError:
        return None, None
    base = _frame_offset(buf)
    if base is not None:
        return buf, base
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", path,
             "-map", "0:v:0", "-c:v", "copy", "-an", "-frames:v", "1",
             "-f", "data", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None, None
    return (
        (result.stdout, 0)
        if result.returncode == 0 and result.stdout
        else (None, None)
    )


def recording_datetimes(path, head_bytes=HEAD_BYTES):
    """Return {source: "YYYY-MM-DD HH:MM:SS"} for the file's first frame."""
    buf, base = _read_head(path, head_bytes)
    if buf is None or base + 4 > len(buf):
        return {}

    # DSF bit: 0 = 525/60 DV with 10 DIF sequences, 1 = 625/50 with 12.
    # Taken from the stream, so a raw .dv is identified as an AVI is.
    wide = bool(buf[base + 3] & 0x80)
    sequences = SEQUENCES_625_50 if wide else SEQUENCES_525_60
    frame_bytes = FRAME_BYTES_625_50 if wide else FRAME_BYTES_525_60
    if base + frame_bytes > len(buf):
        return {}

    found = inspect_frame(buf, sequences, base=base)

    return {
        name: "%04d-%02d-%02d %02d:%02d:%02d" % (parts["date"] + parts["time"])
        for name, parts in found.items()
        if parts["date"] and parts["time"]
    }


def recording_datetime(path, head_bytes=HEAD_BYTES):
    """The agreed recording timestamp, or None.

    Returns a value only when every source that produced one agrees. No
    precedence is applied between them: which packs a camcorder populates is a
    manufacturer decision, so preferring one would be a guess about hardware
    this tool cannot identify. Disagreement is a finding, not a tie to break.
    """
    values = set(recording_datetimes(path, head_bytes).values())
    return values.pop() if len(values) == 1 else None


def _read_recovery_frames(path, frame_limit=RECOVERY_FRAMES):
    """Return a bounded raw-DV stream for an anomalous first frame."""
    try:
        with open(path, "rb") as handle:
            first_four = handle.read(4)
            if _frame_offset(first_four) == 0:
                # PAL frames are larger; using that ceiling also covers NTSC.
                handle.seek(0)
                return handle.read(frame_limit * FRAME_BYTES_625_50)
    except OSError:
        return None

    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
             "-map", "0:v:0", "-c:v", "copy", "-an",
             "-frames:v", str(frame_limit), "-f", "data", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def _first_agreed_frame(payload, frame_limit=RECOVERY_FRAMES):
    """Return (source values, frame number) for the first agreeing frame."""
    if not payload or len(payload) < 4 or _frame_offset(payload[:4]) != 0:
        return None
    pal = bool(payload[3] & 0x80)
    sequences = SEQUENCES_625_50 if pal else SEQUENCES_525_60
    frame_bytes = FRAME_BYTES_625_50 if pal else FRAME_BYTES_525_60
    available = min(len(payload) // frame_bytes, frame_limit)
    for frame_number in range(available):
        base = frame_number * frame_bytes
        frame = payload[base:base + frame_bytes]
        if _frame_offset(frame[:4]) != 0:
            return None
        inspected = inspect_frame(frame, sequences)
        found = {
            name: "%04d-%02d-%02d %02d:%02d:%02d"
            % (parts["date"] + parts["time"])
            for name, parts in inspected.items()
            if parts["date"] and parts["time"]
        }
        if found and len(set(found.values())) == 1:
            return found, frame_number
    return None


def recording_evidence(path, recovery_frames=RECOVERY_FRAMES):
    """Return (source values, frame, head state) with bounded recovery.

    ``frame`` is zero for normal first-frame evidence, greater than zero when a
    missing or contradictory head was recovered, and ``None`` when recovery
    did not find agreement. ``head_state`` is ``agree``, ``missing`` or
    ``conflict``.
    """
    head = recording_datetimes(path)
    values = set(head.values())
    if len(values) == 1:
        return head, 0, "agree"
    head_state = "missing" if not values else "conflict"
    recovered = _first_agreed_frame(
        _read_recovery_frames(path, recovery_frames), recovery_frames
    )
    if recovered is None:
        return head, None, head_state
    found, frame_number = recovered
    return found, frame_number, head_state


USAGE = """Usage:
  dv_recording_time.py FILE...        one result per file, in order
  dv_recording_time.py --from-stdin   NUL-separated paths on stdin

Description:
  Read the recording date and time a camcorder wrote into a DV capture, from
  the standard VAUX, SSYB and AAUX packs. The first frame is authoritative when
  it agrees. A missing or conflicting head triggers a bounded recovery scan.

Output:
  One tab-separated line per file: path, timestamp, sources, and recovery note.
  An unresolved conflict has an empty timestamp and "disagree:..." sources.
  Every file is reported, so results pair with inputs positionally."""


def main(argv):
    if argv[:1] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if argv[:1] == ["--from-stdin"]:
        paths = [p for p in sys.stdin.read().split("\0") if p]
    else:
        paths = argv
    if not paths:
        print(USAGE, file=sys.stderr)
        return 2
    for path in paths:
        if path.startswith("-"):
            print("[ERROR] unknown option: %s" % path, file=sys.stderr)
            print("Try 'dv_recording_time.py --help' for more information.",
                  file=sys.stderr)
            return 2
    for path in paths:
        found, frame_number, head_state = recording_evidence(path)
        values = set(found.values())
        if frame_number is None:
            note = "unresolved_%s_after_%d_frames" % (
                head_state, RECOVERY_FRAMES
            )
        elif frame_number > 0:
            note = "recovered_after_%s_head:%d" % (head_state, frame_number)
        else:
            note = ""
        if len(values) == 1:
            sys.stdout.write("%s\t%s\t%s\t%s\n" % (
                path, values.pop(), ",".join(sorted(found)), note))
        elif not values:
            sys.stdout.write("%s\t\t\t%s\n" % (path, note))
        else:
            # Reported, never resolved: choosing between populated sources
            # would be a guess about the camcorder that wrote them.
            detail = "; ".join("%s=%s" % (name, found[name])
                               for name in sorted(found))
            sys.stdout.write("%s\t\tdisagree:%s\t%s\n" % (
                path, detail, note
            ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
