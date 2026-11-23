#!/usr/bin/env python3
"""Probe a capture or master for the facts this pipeline's decisions rest on.

Reports scan type and field order, aspect ratio, chroma format and bitrate
health, then says what prepare_video.sh will do with the file and whether
video_upscale_pipeline.py will accept it.

Used by prepare_video.sh; also useful on its own to see what a capture is
before committing to processing it.
"""
import shutil
import subprocess
import re
import sys
import json
import argparse
import os
from math import gcd

__version__ = "1.0.0"

# ==============================================================================
# CLI ARGUMENT PARSING
# ==============================================================================


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Probe a Digital8, Hi8, VHS or DVD source for the metadata "
                    "this pipeline decides on: interlacing and field order, "
                    "aspect ratio, chroma format and bitrate health.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  pipeline_tools/probe_video.py /path/to/capture.dv
  pipeline_tools/probe_video.py ~/VideoPreservation/ai/<run-id>/work/masters/<name>_master_from_prepare_video.mp4
        """
    )
    parser.add_argument("input", help="Path to the video file to analyze.")

    parser.add_argument(
        "-V", "--version", action="version",
        version=f"{os.path.basename(__file__)} {__version__}")
    return parser.parse_args(argv)

# ==============================================================================
# METADATA EXTRACTION (ffprobe)
# ==============================================================================


def probe_metadata(input_file):
    """Fetches resolution, framerate, bitrate, pixel format, codec, and field order using ffprobe."""
    if input_file.lower().endswith('.iso'):
        raise ValueError(
            "Cannot probe .ISO files directly. Mount the ISO and "
            "probe individual VOB files (e.g., VTS_01_1.VOB)."
        )

    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", ("stream=width,height,r_frame_rate,pix_fmt,bit_rate,duration,"
                          "codec_name,field_order,sample_aspect_ratio,display_aspect_ratio"),
        "-of", "json",
        input_file
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)

    if 'streams' not in data or not data['streams']:
        raise KeyError(f"No video streams found in {input_file}.")

    return data['streams'][0]


def probe_audio(input_file):
    """Fetches audio codec name using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input_file
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() or "none"


def probe_format(input_file):
    """
    Fetches container-level duration and bitrate using ffprobe -show_format.

    MKV and other containers often store accurate duration at the format
    (container) level even when the individual stream's duration field is
    missing or zero. This is the preferred duration source for non-MP4
    containers and is used as the primary fallback before the slow
    count_frames path.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,bit_rate,size",
        "-of", "json",
        input_file
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        return data.get('format', {})
    except (json.JSONDecodeError, KeyError):
        return {}


def aspect_ratio_report(meta):
    """Return declared SAR/DAR plus a DAR independently calculated from SAR."""
    sar = meta.get('sample_aspect_ratio') or 'unknown'
    declared_dar = meta.get('display_aspect_ratio') or 'unknown'
    calculated_dar = 'unknown'
    consistent = None

    try:
        sar_num, sar_den = map(int, sar.split(':'))
        width = int(meta['width'])
        height = int(meta['height'])
        if min(sar_num, sar_den, width, height) <= 0:
            raise ValueError
        dar_num = width * sar_num
        dar_den = height * sar_den
        divisor = gcd(dar_num, dar_den)
        dar_num //= divisor
        dar_den //= divisor
        calculated_dar = f"{dar_num}:{dar_den}"

        try:
            declared_num, declared_den = map(int, declared_dar.split(':'))
            if min(declared_num, declared_den) <= 0:
                raise ValueError
            consistent = dar_num * declared_den == declared_num * dar_den
        except (ValueError, AttributeError):
            pass
    except (ValueError, KeyError, AttributeError):
        pass

    return sar, declared_dar, calculated_dar, consistent


def familiar_aspect_ratio(ratio, tolerance_percent=1.0):
    """Return the nearest familiar display ratio when it is within tolerance."""
    common = (
        ('1:1', 1, 1),
        ('5:4', 5, 4),
        ('4:3', 4, 3),
        ('3:2', 3, 2),
        ('16:10', 16, 10),
        ('16:9', 16, 9),
        ('1.85:1', 185, 100),
        ('2.35:1', 235, 100),
        ('2.39:1', 239, 100),
    )
    try:
        num, den = map(int, ratio.split(':'))
        value = num / den
    except (ValueError, ZeroDivisionError, AttributeError):
        return None

    label, common_num, common_den = min(
        common, key=lambda item: abs(value - item[1] / item[2])
    )
    common_value = common_num / common_den
    difference = abs(value - common_value) / common_value * 100
    if difference <= tolerance_percent and ratio != label:
        return label, difference
    return None

# ==============================================================================
# FIELD ORDER AND INTERLACE DETECTION
# ==============================================================================


def detect_interlace(input_file, meta, frames=500):
    """
    Production-grade interlace and field order detection.

    Detection priority mirrors prepare_video.sh:
      1. DV codec shortcut — always BFF on NTSC; ffprobe cannot read field
         order from AVI container headers for DV streams and returns 'unknown'.
      2. ffprobe stream=field_order — progressive, tt and bb settle it. The
         swapped values tb and bt establish interlacing but not the order;
         see below.
      3. ffprobe frame flags — interlaced_frame=1 on any of the first 50
         frames settles interlacing, and top_field_first gives the order.
         Flags that are all zero are no evidence of progressive video:
         lossless capture codecs carry no interlace flag at all.
      4. idet heuristic — pixel-level analysis of windows spread across the
         recording, whenever stream metadata and frame flags leave the scan
         type or the order open.

    Returns a tuple: (scan_type_string, is_interlaced_bool, parity_string)
      parity_string matches prepare_video.sh convention: "0"=TFF, "1"=BFF,
      "-1"=no order: progressive, or interlaced with the order not established
    """

    codec_name = meta.get('codec_name', '')

    # ------------------------------------------------------------------
    # STEP 1 — DV Codec Shortcut
    # DV streams captured via FireWire are always BFF on NTSC. The AVI
    # container does not store field order metadata for DV, so ffprobe
    # returns 'unknown'. Shortcut here to avoid misleading fallthrough.
    # ------------------------------------------------------------------
    if codec_name.startswith('dv'):
        return (
            "Interlaced (BFF — DV/NTSC, codec-based detection)",
            True,
            "1"
        )

    # ------------------------------------------------------------------
    # STEP 2 — ffprobe stream=field_order
    # progressive, tt and bb settle the scan type. The swapped orders settle
    # only that the stream is interlaced: FFmpeg defines tb as "top coded
    # first, bottom displayed first", yet writes tb for top-field-first frames
    # and bt for bottom-field-first ones. Frame flags or pixels give the order.
    # ------------------------------------------------------------------
    field_order = meta.get('field_order', '').upper()
    tagged_interlaced = field_order in ('TB', 'BT')

    if field_order == 'PROGRESSIVE':
        return ("Progressive", False, "-1")
    elif field_order == 'TT':
        return ("Interlaced (TFF — Top Field First, ffprobe stream metadata)", True, "0")
    elif field_order == 'BB':
        return ("Interlaced (BFF — Bottom Field First, ffprobe stream metadata)", True, "1")

    # ------------------------------------------------------------------
    # STEP 3 — Frame Flag Check
    # A set interlaced_frame flag on any of the first 50 frames settles it.
    # Flags that are all zero settle nothing: HuffYUV, UtVideo, Lagarith,
    # MagicYUV and raw video carry no interlace flag, so their frames decode
    # as not interlaced whatever the picture holds. The pixel check decides.
    # ------------------------------------------------------------------
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_frames",
            "-read_intervals", "%+#50",
            input_file
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        interlaced_flags = re.findall(r"interlaced_frame=(\d)", result.stdout)
        top_field_flags = re.findall(r"top_field_first=(\d)", result.stdout)

        if any(int(x) for x in interlaced_flags):
            # Determine field order from top_field_first flags
            if top_field_flags:
                tff_count = sum(int(x) for x in top_field_flags)
                if tff_count > len(top_field_flags) / 2:
                    return (
                        "Interlaced (TFF — Top Field First, frame-flag detection)",
                        True, "0"
                    )
                else:
                    return (
                        "Interlaced (BFF — Bottom Field First, frame-flag detection)",
                        True, "1"
                    )
            else:
                return (
                    "Interlaced (order not established — frame flags give none)",
                    True, "-1"
                )
    except Exception:
        pass

    # ------------------------------------------------------------------
    # STEP 4 — idet Pixel-Level Heuristic
    # ------------------------------------------------------------------
    print(f"Analyzing {frames} frames across the recording for interlacing (idet heuristic)...")

    # Only this step needs ffmpeg; a file whose scan type is settled by
    # stream metadata or frame flags never reaches here, so the check belongs
    # at the call site rather than in main().
    require_tools("ffmpeg")

    verdict = idet_verdict(idet_counts(input_file, meta, frames))
    if tagged_interlaced and not verdict[1]:
        # The stream says interlaced, and still pictures show no combing
        # whatever the scan type, so a verdict without an order leaves the
        # stream interlaced with its order open. prepare_video.sh then asks
        # for --field-order rather than guess.
        return (f"Interlaced (order not established — stream field_order "
                f"{field_order.lower()}, frame flags give none, idet: {verdict[0]})",
                True, "-1")
    return verdict


IDET_WINDOWS = 5


def frame_rate(meta):
    """The stream's frame rate as a float, or 0.0 when ffprobe gives none."""
    num, _, den = str(meta.get('r_frame_rate', '')).partition('/')
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def positive_seconds(value):
    """A duration ffprobe reported, or 0.0 for N/A, absent or non-positive."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    return seconds if 0 < seconds < float('inf') else 0.0


def idet_counts(input_file, meta, frames):
    """idet's multi-frame counts (tff, bff, progressive) summed over windows.

    A tape capture often opens on a still leader, a blue screen or black,
    which shows no combing whatever its scan type. The frames are therefore
    read as IDET_WINDOWS windows spread evenly across the recording; a file
    too short to hold them apart is read from its start. Returns None when
    idet reports no counts.
    """
    duration = (positive_seconds(meta.get('duration'))
                or positive_seconds(probe_format(input_file).get('duration')))

    per_window = frames // IDET_WINDOWS
    fps = frame_rate(meta)
    if per_window and fps > 0 and duration / IDET_WINDOWS > per_window / fps:
        windows = [(duration * i / IDET_WINDOWS, per_window) for i in range(IDET_WINDOWS)]
    else:
        windows = [(0.0, frames)]

    totals = None
    for start, count in windows:
        seek = ["-ss", f"{start:.3f}"] if start > 0 else []
        result = subprocess.run(
            ["ffmpeg", "-nostdin", *seek, "-i", input_file, "-filter:v", "idet",
             "-frames:v", str(count), "-an", "-f", "null", "-"],
            capture_output=True, text=True)
        match = re.search(
            r"Multi frame detection: TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)",
            result.stderr)
        if match:
            counts = tuple(map(int, match.groups()))
            totals = counts if totals is None else tuple(map(sum, zip(totals, counts)))
    return totals


def idet_verdict(counts):
    """Scan type from idet counts, as detect_interlace() returns it."""
    if counts is None:
        return ("Undetermined", False, "-1")

    tff, bff, prog = counts
    total = tff + bff + prog

    if total == 0:
        return ("Undetermined", False, "-1")

    interlace_ratio = (tff + bff) / total

    if interlace_ratio < 0.15:
        return (f"Progressive (idet noise {interlace_ratio:.1%})", False, "-1")

    if interlace_ratio > 0.20:
        # Interlaced video shows combing of one order. Fine progressive detail
        # can read as combing too, but of both orders about equally, so a
        # split verdict is no evidence of interlacing.
        if min(tff, bff) > 0.2 * (tff + bff):
            return (f"Undetermined (idet split: TFF {tff}, BFF {bff}, "
                    f"progressive {prog} of {total})", False, "-1")
        if tff > bff:
            return (
                f"Interlaced (TFF — Top Field First, idet {tff}/{total})",
                True, "0"
            )
        else:
            return (
                f"Interlaced (BFF — Bottom Field First, idet {bff}/{total})",
                True, "1"
            )

    return (f"Mostly Progressive (weak field artifacts {interlace_ratio:.1%})", False, "-1")


# ==============================================================================
# UNIVERSAL DATA HEALTH (Bits Per Pixel Logic)
# ==============================================================================

# Lossless codecs: bitrate density scoring is meaningless for these — they
# encode every pixel exactly and bitrate is determined purely by content
# complexity. Label them archival-grade unconditionally.
LOSSLESS_CODECS = frozenset({
    'ffv1', 'huffyuv', 'ffvhuff', 'utvideo', 'v210', 'v410',
    'magicyuv', 'lagarith', 'ljpeg',
})

# Codec-aware bpp targets, calibrated to SD 720x480 (345,600 px).
# Higher target = codec needs more bits/pixel to achieve the same quality.
# Values represent the bpp expected from a well-encoded archival master at SD.
#
#   DV25:   fixed ~25 Mbps at 720x480/29.97i → bpp ≈ 2.40; target set to match
#   MPEG-2: less efficient than H.264 by ~40-50%; target reflects that
#   H.264:  modern baseline, well-tuned at CRF 12 for archival use
#   H.265:  ~40% more efficient than H.264 at same quality; lower target
#   raw/uncompressed: yuyv422 = 16 bpp by definition
CODEC_BPP_TARGETS = {
    'dvvideo': 2.40,   # DV25: fixed-bitrate, 720x480/29.97i
    'mpeg2video': 0.40,  # MPEG-2: ~2x less efficient than H.264
    'h264': 0.20,   # H.264: archival CRF 12 baseline
    'hevc': 0.12,   # H.265: ~40% more efficient than H.264
}
CODEC_BPP_DEFAULT = 0.20   # Fallback for unknown compressed codecs
CODEC_BPP_RAW = 16.0   # Uncompressed 4:2:2 (yuyv422, etc.)


def check_bitrate_health(meta, input_file, fmt=None, is_bob_deinterlaced=False):
    """
    Universal health check based on Bits Per Pixel (bpp) and Resolution.

    Handles codec-aware thresholds, lossless early-return, resolution
    normalisation, audio bitrate exclusion, and bob-deinterlaced fps correction.

    Duration resolution priority (fastest to slowest):
      1. Stream-level duration  — reliable for AVI/MP4 with intact headers.
      2. Format-level duration  — reliable for MKV and most other containers;
                                  avoids the hang that count_frames causes on
                                  large MKV files.
      3. ffprobe count_packets  — fast packet count; accurate enough for bpp
                                  estimation without decoding every frame.
      4. ffprobe count_frames   — last resort only; decodes every frame and
                                  is extremely slow on long files. Avoided
                                  unless all other sources fail.

    Args:
        meta:               Video stream metadata dict from probe_metadata().
        input_file:         Path to the video file.
        fmt:                Format-level metadata dict from probe_format(),
                            or None to fetch on demand.
        is_bob_deinterlaced: True when the source was 29.97i and bob-deinterlaced
                            to 59.94p (e.g. prepare_video.sh output). The fps used
                            for bpp calculation is halved back to 29.97 to avoid
                            penalising the doubled frame count, since each bob
                            frame carries only one field's worth of unique data.
    """
    try:
        codec_name = meta.get('codec_name', '').lower()

        # ------------------------------------------------------------------
        # EARLY RETURN — Lossless codecs
        # Bitrate density scoring is not meaningful for lossless formats.
        # They encode exactly what's there; bitrate reflects content complexity,
        # not quality. Report the codec and label as archival unconditionally.
        # ------------------------------------------------------------------
        if codec_name in LOSSLESS_CODECS:
            try:
                stream_br = int(meta.get('bit_rate', 0)) / 1_000_000
                br_str = f"{stream_br:.2f} Mbps" if stream_br > 0 else "bitrate N/A"
            except (ValueError, TypeError):
                br_str = "bitrate N/A"
            return f"Lossless ({br_str}) - Archival Quality [{codec_name}]"

        fps_num, fps_den = map(int, meta.get('r_frame_rate', '30000/1001').split('/'))
        fps = fps_num / fps_den

        # ------------------------------------------------------------------
        # FIX 2 — Bob-deinterlaced fps correction
        #
        # Bob deinterlacing doubles the frame rate by promoting each field to
        # a full frame (29.97i → 59.94p). Each output frame carries only one
        # field's worth of unique spatial data, so using the doubled fps in the
        # bpp denominator would halve the apparent bpp and make the file look
        # half as data-rich as it actually is. Correct by using the original
        # field rate (fps / 2) for the bpp calculation.
        # ------------------------------------------------------------------
        bpp_fps = (fps / 2.0) if is_bob_deinterlaced else fps

        # ------------------------------------------------------------------
        # 1. Stream-level duration
        # ------------------------------------------------------------------
        duration = float(meta.get('duration', 0))

        # Hauppauge and some AVI muxers write absurd DTS values that produce
        # durations > 1 day. MKV stream duration is often simply absent (0).
        # Both cases require falling through to a better source.
        BROKEN_DURATION = duration <= 0 or duration > 86400

        # ------------------------------------------------------------------
        # 2. Format-level duration (fast; covers MKV, MOV, and most others)
        # ------------------------------------------------------------------
        if BROKEN_DURATION:
            if fmt is None:
                fmt = probe_format(input_file)
            fmt_duration = float(fmt.get('duration', 0))
            if fmt_duration > 0:
                duration = fmt_duration
                BROKEN_DURATION = False

        # ------------------------------------------------------------------
        # 3. Packet count (fast — no decoding, just reads index)
        # ------------------------------------------------------------------
        if BROKEN_DURATION:
            try:
                cmd = [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-count_packets",
                    "-show_entries", "stream=nb_read_packets",
                    "-of", "csv=p=0",
                    input_file
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                pkt_count = int(result.stdout.strip())
                if pkt_count > 0:
                    duration = pkt_count / fps
                    BROKEN_DURATION = False
            except (ValueError, subprocess.TimeoutExpired):
                pass

        # ------------------------------------------------------------------
        # 4. Frame count (last resort — slow, decodes every frame)
        # ------------------------------------------------------------------
        if BROKEN_DURATION:
            print("[WARN] Duration unresolvable from headers — falling back to frame "
                  "count (this may take a while for large files)...")
            print("[WARN] unrealistic or missing duration detected — enabling PTS regeneration recommended.")
            cmd = [
                "ffprobe", "-v", "error",
                "-count_frames",
                "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_frames",
                "-of", "csv=p=0",
                input_file
            ]
            frames = int(subprocess.check_output(cmd).decode().strip())
            duration = frames / fps

        # ------------------------------------------------------------------
        # FIX 1 — Use video stream bitrate, not file size
        #
        # os.path.getsize() includes audio, subtitles, attachments, and
        # container overhead. For a 2-hour file with PCM audio this can be
        # several hundred MB of non-video data, inflating the apparent video
        # bitrate and skewing the health score.
        #
        # Preference order:
        #   a) Video stream bit_rate from ffprobe (most accurate; absent in MKV)
        #   b) Format bit_rate minus audio stream bit_rate (good for MKV)
        #   c) File size fallback (least accurate)
        # ------------------------------------------------------------------
        video_bits = None

        # (a) Video stream-level bitrate
        try:
            vbr = int(meta.get('bit_rate', 0))
            if vbr > 0:
                video_bits = vbr * duration
        except (ValueError, TypeError):
            pass

        # (b) Format bitrate minus audio bitrate
        if video_bits is None:
            try:
                if fmt is None:
                    fmt = probe_format(input_file)
                fmt_br = int(fmt.get('bit_rate', 0))
                if fmt_br > 0:
                    # Probe audio stream bitrate to subtract
                    audio_br_cmd = [
                        "ffprobe", "-v", "error",
                        "-select_streams", "a:0",
                        "-show_entries", "stream=bit_rate",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        input_file
                    ]
                    abr_result = subprocess.run(
                        audio_br_cmd, capture_output=True, text=True, timeout=10
                    )
                    abr = int(abr_result.stdout.strip() or 0)
                    video_bits = (fmt_br - abr) * duration
            except (ValueError, TypeError, subprocess.TimeoutExpired):
                pass

        # (c) File size fallback
        if video_bits is None or video_bits <= 0:
            video_bits = os.path.getsize(input_file) * 8

        # ------------------------------------------------------------------
        # Calculate Density Metrics
        # ------------------------------------------------------------------
        bitrate_mbps = video_bits / duration / 1_000_000
        width = int(meta.get('width', 0))
        height = int(meta.get('height', 0))

        # bpp uses bpp_fps (bob-corrected if applicable) so doubled frames
        # from bob deinterlacing don't dilute the apparent data density.
        bpp = video_bits / (width * height * (bpp_fps * duration))

        # ------------------------------------------------------------------
        # FIX 3 — Codec-aware bpp target
        #
        # Different codecs have fundamentally different efficiency profiles.
        # Using a single H.264-calibrated target misscores DV (too low —
        # DV is fixed-bitrate and naturally high bpp), MPEG-2 (too high —
        # less efficient than H.264), and H.265 (too low — more efficient).
        # ------------------------------------------------------------------
        pix_fmt = meta.get('pix_fmt', '')
        if 'yuyv' in pix_fmt or 'raw' in pix_fmt:
            target = CODEC_BPP_RAW
        else:
            target = CODEC_BPP_TARGETS.get(codec_name, CODEC_BPP_DEFAULT)

        # ------------------------------------------------------------------
        # Resolution-Normalised BPP
        #
        # The targets above are calibrated for SD 720x480 (345,600 px).
        # H.264/H.265 at higher resolutions is inherently more efficient: the
        # same visual quality requires fewer bits per pixel because larger DCT
        # blocks and better inter-frame prediction reduce redundancy.
        #
        # Raw bpp drops by the pixel-count ratio as resolution increases, so
        # comparing raw bpp against an SD-calibrated target underscores
        # super-SD content as low-quality.  Fix: normalise bpp back to its
        # SD-equivalent before scoring.
        #
        #   normalised_bpp = raw_bpp × (pixel_count / SD_pixels)
        #
        # At 720x480   (345,600 px): multiplier = 1.0  → no change
        # At 1440x960 (1,382,400 px): multiplier = 4.0  → correctly adjusts upward
        # At 360x240   ( 86,400 px): multiplier = 0.25 → still penalises sub-SD
        #
        # Note: DV is always 720x480 so the multiplier is always 1.0 for DV;
        # the normalisation is a no-op but harmless.
        # ------------------------------------------------------------------
        SD_PIXELS = 345_600  # 720×480 NTSC SD reference
        pixel_count = width * height
        normalised_bpp = bpp * (pixel_count / SD_PIXELS)

        # Final Score: resolution-normalised BPP vs codec-calibrated target
        ratio = normalised_bpp / target

        # ------------------------------------------------------------------
        # Universal Labels
        # ------------------------------------------------------------------
        if ratio >= 0.85 and width >= 720:
            return f"Excellent ({bitrate_mbps:.2f} Mbps) - Archival Quality"
        elif ratio >= 0.6:
            return f"Good ({bitrate_mbps:.2f} Mbps) - Standard Quality"
        elif ratio >= 0.3:
            return f"Fair ({bitrate_mbps:.2f} Mbps) - Compressed/Low-Res"
        else:
            return f"Poor ({bitrate_mbps:.2f} Mbps) - Sub-standard Archive"

    except Exception as e:
        return f"Unknown Error: {e}"

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================


def require_tools(*tools):
    """Fail with an actionable message rather than a traceback from subprocess."""
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        print(f"[ERROR] Required command(s) not found: {', '.join(missing)}",
              file=sys.stderr)
        raise SystemExit(1)


def main(argv=None):
    args = parse_args(argv)
    require_tools("ffprobe")

    try:
        meta = probe_metadata(args.input)
        fmt = probe_format(args.input)
        audio_codec = probe_audio(args.input)
        scan_type, is_interlaced, parity = detect_interlace(args.input, meta)
        sar, declared_dar, calculated_dar, aspect_consistent = aspect_ratio_report(meta)
        familiar_dar = familiar_aspect_ratio(calculated_dar)

        # Detect bob-deinterlaced masters: prepare_video.sh deinterlaces 29.97i
        # sources to 59.94p via bwdif (bob). The output is progressive at ~60fps
        # but each frame carries only one field's worth of unique spatial data.
        # We detect this by checking whether a progressive file sits at ≥50fps —
        # native 60fps capture doesn't exist in this corpus; it means bob output.
        fps_num, fps_den = map(int, meta.get('r_frame_rate', '30000/1001').split('/'))
        detected_fps = fps_num / fps_den
        is_bob_deinterlaced = (not is_interlaced) and (detected_fps >= 50.0)

        quality_health = check_bitrate_health(
            meta, args.input, fmt=fmt, is_bob_deinterlaced=is_bob_deinterlaced
        )

        codec_name = meta.get('codec_name', 'unknown')

        # What prepare_video.sh will actually do with this audio.
        #
        # PCM does not mean an MKV master. That is what --pcm produces; the
        # default encodes AAC 192k and keeps MP4, because ffprobe reports N/A
        # for PCM stream duration in MKV and the pipeline's sync checks need a
        # readable one.
        #
        # A DV source reported as having no audio usually has audio. ffprobe
        # can fail to see the stream of a DV/AVI whose header or index omits
        # it, while the PCM is present in the DIF bitstream; prepare_video.sh
        # assumes it, recovers it with an -f dv remux, and encodes it. Saying
        # "none" for such a file would suggest a silent capture.
        is_dv_source = codec_name.startswith('dv')
        if audio_codec.startswith('pcm'):
            audio_display = (f"{audio_codec} (PCM — prepare_video.sh encodes AAC 192k "
                             f"into MP4; --pcm keeps PCM and switches to MKV)")
        elif audio_codec == "none" and is_dv_source:
            audio_display = ("none reported by ffprobe — but DV always carries PCM in "
                             "the bitstream; prepare_video.sh recovers it with an "
                             "-f dv remux, then encodes AAC 192k (--pcm keeps PCM, MKV)")
        elif audio_codec == "none":
            audio_display = "none — prepare_video.sh produces a master with no audio track"
        elif audio_codec == "aac":
            audio_display = "aac (copied when aligned; re-encoded when timing adjustment is needed)"
        else:
            audio_display = f"{audio_codec} (prepare_video.sh encodes AAC 192k for compatibility)"

        # Resolve duration for display: stream level first, format level fallback.
        # Mirrors the priority order used in check_bitrate_health so the displayed
        # duration is consistent with the value used for the health calculation.
        _dur = float(meta.get('duration', 0))
        if _dur <= 0 or _dur > 86400:
            _dur = float(fmt.get('duration', 0))
        if _dur > 0:
            _hh, _rem = divmod(int(_dur), 3600)
            _mm, _ss = divmod(_rem, 60)
            duration_display = f"{_hh}:{_mm:02d}:{_ss:02d} ({_dur:.1f}s)"
        else:
            duration_display = "unknown"

        print("\n--- Video Probe Report ---")
        print(f"File:         {args.input}")
        print(f"Resolution:   {meta['width']}x{meta['height']}")
        print(f"SAR:          {sar}")
        print(f"DAR:          {declared_dar} (ffprobe), {calculated_dar} (from resolution × SAR)")
        if familiar_dar:
            familiar_label, familiar_difference = familiar_dar
            print(f"              ≈ {familiar_label} ({familiar_difference:.3f}% difference)")
        if aspect_consistent is False:
            print("              WARNING: ffprobe DAR disagrees with resolution × SAR")
        print(f"Duration:     {duration_display}")
        print(f"Frame Rate:   {meta['r_frame_rate']} fps")
        print(f"Pixel Format: {meta['pix_fmt']}")
        # For NTSC DV sources, note that the decoder outputs yuv411p frames and
        # that prepare_video.sh converts to yuv444p before bwdif so the full
        # pipeline is end-to-end yuv444p with no intermediate yuv420p step.
        # (PAL DV is yuv420p natively and does not match this condition.)
        if codec_name.startswith('dv') and meta.get('pix_fmt') == 'yuv411p':
            print("              (native DV 4:1:1 — libavcodec decodes to yuv411p;"
                  " prepare_video.sh converts to yuv444p before bwdif)")
        print(f"Video Codec:  {codec_name}")
        print(f"Audio Codec:  {audio_display}")
        print(f"Scan Type:    {scan_type}")
        print(f"Data Health:  {quality_health}")
        print("--------------------------\n")

        # Visual Scannability: High-level Warnings
        if "Poor" in quality_health or "Fair" in quality_health:
            print("⚠️  DATA WARNING: Low bitrate detected. AI upscaling")
            print("   may amplify compression blocks instead of actual detail.")

        # What "ready" means is decided by video_upscale_pipeline.py's
        # check_input_is_prepared(), which refuses a raw capture: a DV/HuffYUV
        # codec or an interlaced field order is rejected outright. Being
        # progressive is necessary and not sufficient -- the pipeline's input is
        # a prepare_video.sh master (yuv444p, CRF 12, tagged as regenerable),
        # and nothing else is a supported input. Calling a progressive file
        # "ready" without that distinction invites feeding a capture straight
        # in, which produces a finished-looking upscale built from interlaced
        # frames.
        if is_interlaced:
            print("⚠️  SCAN WARNING: This file is interlaced.")
            print("   Run prepare_video.sh to deinterlace it into a master;")
            print("   the pipeline refuses an interlaced input.")
        elif codec_name in ("dvvideo", "ffvhuff", "huffyuv", "rawvideo"):
            print("⚠️  This file is a capture, not a master.")
            print(f"   The pipeline refuses a {codec_name} input. Run prepare_video.sh first.")
        else:
            print("✅ Progressive. If this is a prepare_video.sh master it is ready;")
            print("   the pipeline checks that for itself and refuses a raw capture.")

        return 0
    except Exception as e:
        print(f"Error probing file: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
