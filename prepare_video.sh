#!/bin/bash
# ==============================================================================
# Script Name: prepare_video.sh
# Description: Deinterlace and master a capture for the AI upscaling pipeline.
#              Handles raw DV/Hi8 captures, DVD MPEG-2, and progressive digital
#              sources, detecting scan type and field order rather than being
#              told them.
#
#              Output is a yuv444p CRF 12 master. That is the pipeline's input:
#              video_upscale_pipeline.py refuses a raw capture.
#
# NOTE: The entire script is wrapped in main() so that bash reads the complete
# file into memory before execution begins. This prevents mid-run file
# replacement from affecting an in-progress encode.
# ==============================================================================
set -euo pipefail
# Python started from here writes its output as it goes, even into a pipe
# such as `| tee`, where it would otherwise hold 8 KB at a time.
export PYTHONUNBUFFERED=1

# A duration or rate ffprobe cannot read is an ABSENCE, not a zero. ffprobe
# exits 0 while printing "N/A" for a stream it cannot measure, so a -z test
# lets that through, and awk -v reads "N/A" as a strnum worth 0. Two unreadable
# durations then agree with each other and a comparison against a tolerance
# passes, having compared nothing. Every value that reaches a numeric
# comparison is checked here first.
is_seconds() { [[ $1 =~ ^-?[0-9]+([.][0-9]+)?$ ]]; }

# Length of the PICTURE, not of the file. format=duration reports the longest
# stream, which for an AAC master is the audio: AAC pads its final 1024-sample
# block, so the container claims ~21ms more than the video runs. A comparison
# against that figure spends its tolerance on padding rather than on real
# discrepancy. Falls back to the container only when a format carries no
# stream-level duration.
video_stream_seconds() {
    local value
    value=$(ffprobe -v error -select_streams v:0 -show_entries stream=duration \
        -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null)
    if ! is_seconds "$value"; then
        value=$(ffprobe -v error -show_entries format=duration \
            -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null)
    fi
    printf '%s' "$value"
}

# Seconds occupied by one source picture -- the finest the encode can be cut
# to, and so the only defensible tolerance for a duration comparison.
frame_seconds_of() {
    local rate num den
    rate=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate \
        -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null)
    num=${rate%%/*}; den=${rate##*/}
    [[ $num =~ ^[0-9]+$ && $den =~ ^[0-9]+$ && $num -gt 0 ]] \
        || { printf '0.034'; return; }   # one NTSC frame
    awk -v n="$num" -v d="$den" 'BEGIN { printf "%.6f", d / n }'
}

# What preparation did to the audio's extent, in the operator's terms.
# Silence the master carries where the source has none is named as such: a
# DVD recorder starts its audio track before the first picture and stops it a
# few hundred milliseconds before the last, and the master trims the one and
# fills the other so the pipeline's length checks compare two streams of one
# extent. The filling must not be quiet, because the master then holds audio
# the source does not.
#   audio_timing_report notices <plan.json> <indent>   lines for the summary
#   audio_timing_report tag <plan.json>                clause for the container comment
audio_timing_report() {
    python3 - "$@" <<'PYREPORT'
import json, sys
from fractions import Fraction
mode, plan = sys.argv[1], json.load(open(sys.argv[2]))
indent = sys.argv[3] if len(sys.argv) > 3 else ''
if not plan['audio_present']:
    sys.exit(0)
rate = plan['sample_rate']
frame = Fraction(plan['video_frame_seconds'])
trim, lead, tail = plan['trim_samples'], plan['pad_samples'], plan['tail_pad_samples']
def span(n):
    return f"{n} samples ({n / rate * 1000:.1f} ms, {float(n / rate / frame):.1f} pictures)"
def pictures(n):
    return f"{float(n / rate / frame):.1f} pictures"
if mode == 'notices':
    if trim:
        print(f"{indent}Lead-in dropped: the source audio starts {span(trim)} before the first")
        print(f"{indent}    picture; that audio-only lead-in is left out of the master.")
    if lead:
        print(f"{indent}⚠️  SILENCE PREPENDED: the source audio starts {span(lead)} after the")
        print(f"{indent}    first picture, so the first {pictures(lead)} have no audio in the source.")
        print(f"{indent}    The master puts silence under them. No pictures are removed.")
    if tail:
        print(f"{indent}⚠️  SILENCE APPENDED: the source audio ends {span(tail)} before the last")
        print(f"{indent}    picture, so the last {pictures(tail)} have no audio in the source. The master")
        print(f"{indent}    puts silence under them so both streams end together. No pictures are removed.")
else:
    parts = []
    if trim:
        parts.append(f"{trim}-sample lead-in dropped")
    if lead:
        parts.append(f"{lead} samples ({lead / rate * 1000:.0f} ms) of silence prepended "
                     "under the first pictures, which have no audio in the source")
    if tail:
        parts.append(f"{tail} samples ({tail / rate * 1000:.0f} ms) of silence appended "
                     "under the last pictures, which have no audio in the source")
    if parts:
        print("audio: " + "; ".join(parts))
PYREPORT
}


# Command-line errors are exit 2 and point at --help rather than reprinting
# it, per dev_tools/SCRIPT_CONVENTIONS.md.
usage_error() { printf '[ERROR] %s\nTry %q --help for more information.\n' "$*" "$0" >&2; exit 2; }

main() {

# 1. Argument Check
# Validates input count and provides detailed help documentation.
# No arguments is a command-line error, not a request for help: it exits 2 and
# points at --help rather than printing the whole thing, per
# dev_tools/SCRIPT_CONVENTIONS.md. -h/--help still prints in full and exits 0.
if [[ "${1:-}" == "--help" ]] || [[ "${1:-}" == "-h" ]]; then
    echo "Usage: $0 [OPTIONS] <source_video>"
    echo ""
    echo "Arguments:"
    echo "  source_video    Path to the raw DV, AVI, MPG, MP4, or MKV file."
    echo ""
    echo "Options:"
    echo "  --mask N        Pixels to black out at the bottom edge, covering"
    echo "                  head-switching noise on analog tape."
    echo "                  *** Use EVEN numbers (8, 10, 12) for best results. ***"
    echo "                  Default 0. Analog tape (Hi8, VHS) transferred through"
    echo "                  a DV converter reliably needs 8; native DV needs none."
    echo "                  See pipeline_docs/prepare-video.md."
    echo "  --test          (Optional) Process only the first 30s for quick mask review."
    echo "  --pcm           (Optional) Preserve retained PCM samples losslessly instead of"
    echo "                  the default AAC 192k encode. Switches output to .mkv"
    echo "                  (required since MP4 does not support PCM). Use only"
    echo "                  if lossless audio in the master is specifically needed;"
    echo "                  note that ffprobe reports N/A for PCM stream duration"
    echo "                  in MKV, causing spurious ~0.4s mismatches in the"
    echo "                  pipeline's audio/video duration check. The DV source"
    echo "                  file is always the true lossless audio archive."
    echo "                  Audio is aligned to the first picture by trimming leading"
    echo "                  samples or adding leading silence, and ends with the last"
    echo "                  picture: an overrun is clipped, a short tail is padded with"
    echo "                  silence. Any silence added is stated in the pre-flight"
    echo "                  summary, the completion report and the master's comment"
    echo "                  tag. Existing AAC is copied when it already spans the"
    echo "                  pictures; otherwise AAC 192k."
    echo "                  --pcm keeps retained PCM samples lossless when aligning."
    echo "  --crf N         (Optional) x264 CRF quality level (default: 12)."
    echo "                  12 is the archival default for all SD pipeline intermediates."
    echo "                  Lower = higher quality and larger file. Rarely needs changing."
    echo "  --mode0         (Optional) Use bwdif mode=0 (frame-rate output: ~30fps)."
    echo "                  Default is mode=1 (field-rate output: ~60fps)."
    echo "  --field-order ORDER (Optional) tff, bff or progressive. Overrides the"
    echo "                  detected scan type and field order, including the"
    echo "                  bottom-field-first assumption for DV. Use it when the"
    echo "                  pre-flight summary is wrong: a --test master whose motion"
    echo "                  jerks back and forth was deinterlaced in the wrong order,"
    echo "                  and one with comb-like edges on movement was not"
    echo "                  deinterlaced. See pipeline_docs/prepare-video.md."
    echo "  --yes           (Optional) Skip the confirmation prompt. For non-interactive"
    echo "                  / batch use. Equivalent to answering 'y' at the prompt."
    echo "  --loglevel LEVEL (Optional) Pass -loglevel LEVEL to ffmpeg."
    echo "                  FFmpeg levels: quiet error warning info verbose debug trace."
    echo "                  Use 'verbose' to log filter graph negotiation and per-pad pixel"
    echo "                  formats (e.g. confirming bwdif input pixel format). Use 'debug'"
    echo "                  or 'trace' for deeper inspection."
    echo "  --output-dir DIR (Optional) Directory for the finished master."
    echo "  --log-dir DIR   (Optional) Directory for this invocation's log."
    echo ""
    echo "                  Both default to a run directory created for this"
    echo "                  invocation under ~/VideoPreservation/ai/, holding the"
    echo "                  master in work/masters/ and the log in logs/<stem>/."
    echo "                  A pipeline driver passes both so its own run"
    echo "                  directory receives them instead. Created"
    echo "                  automatically if they do not exist."
    echo ""
    echo "                  mode=1 (default): each field becomes its own progressive"
    echo "                  frame (~60fps), preserving field-rate temporal resolution."
    echo "                  Best for fast motion where the ~16.7ms between fields matters."
    echo "                  ESRGAN processes 2x as many frames — expect roughly 2x the"
    echo "                  upscaling pipeline runtime."
    echo "                  Leave RIFE off for mode=1 output: the pipeline's default,"
    echo "                  so don't pass it --rife."
    echo ""
    echo "                  mode=0 (--mode0): combines both interlaced fields into one"
    echo "                  full-resolution progressive frame (~30fps). All source data"
    echo "                  is preserved. ESRGAN processes the 30fps frame count."
    echo ""
    echo "                  Output filename includes '_30fps' when --mode0 is used,"
    echo "                  and '_60fps' for the default mode=1."
    echo ""
    echo "Environment:"
    echo "  VIDEO_PROBE     probe_video.py command or full path (default: search PATH)."
    echo ""
    echo "How to select mask_pixels:"
    echo "  1. Play your video in VLC and look at the bottom edge."
    echo "  2. If you see a flickering/static line (head-switching noise), estimate its height."
    echo "  3. Typical values for Hi8/VHS are 8, 10, or 12 pixels."
    echo "  4. CRITICAL: Always use an EVEN number to maintain correct vertical alignment"
  echo "               for the drawbox mask (applies regardless of chroma subsampling)."
    exit 0
fi

MASK_PIXELS=0
TEST_MODE=false
FORCE_PCM=false
CRF_VALUE=""       # empty until after prefix detection; --crf sets this explicitly
CRF_EXPLICIT=false # true when --crf was supplied on the command line
BWDIF_MODE=1       # default: field-rate output (~60fps); --mode0 sets this to 0 (~30fps)
FIELD_ORDER_OVERRIDE=""   # empty: detect scan type and field order; --field-order tff|bff|progressive
SKIP_CONFIRM=false        # true when --yes is supplied; skips the y/n confirmation prompt
FFMPEG_LOGLEVEL=""        # set by --loglevel LEVEL; adds -loglevel LEVEL to ffmpeg invocation
DV_AUDIO_REMUX_NEEDED=false  # true when DV source has no audio in AVI index; triggers remux pass
OUTPUT_DIR_OVERRIDE=""    # empty: this invocation's own run directory; --output-dir overrides
LOG_DIR_OVERRIDE=""       # empty: this invocation's own run directory; --log-dir overrides

# Argument parsing.
#
# Options and the source path may appear in any order, and every value is
# named. A bare positional number would be indistinguishable from a typo --
# `prepare_video.sh in.dv 8 --yes` gives no clue what the 8 is -- so the mask
# is --mask, matching how every other script in this repository takes its
# parameters.
SOURCE_INPUT=""
while (($#)); do
    case $1 in
        --mask)       (($# >= 2)) || usage_error "--mask requires a value"
                      [[ $2 =~ ^[0-9]+$ ]] || usage_error "--mask requires a whole number of pixels, got: $2"
                      MASK_PIXELS=$2; shift 2 ;;
        --crf)        (($# >= 2)) || usage_error "--crf requires a value"
                      [[ $2 != --* ]] || usage_error "--crf requires a value, got: $2"
                      CRF_VALUE=$2; CRF_EXPLICIT=true; shift 2 ;;
        --loglevel)   (($# >= 2)) || usage_error "--loglevel requires a value"
                      [[ $2 != --* ]] || usage_error "--loglevel requires a value, got: $2"
                      FFMPEG_LOGLEVEL=$2; shift 2 ;;
        --output-dir) (($# >= 2)) || usage_error "--output-dir requires a directory"
                      # A dash-prefixed directory can still be given as ./--dir.
                      [[ $2 != --* ]] || usage_error "--output-dir requires a directory, got: $2"
                      OUTPUT_DIR_OVERRIDE=$2; shift 2 ;;
        --log-dir)    (($# >= 2)) || usage_error "--log-dir requires a directory"
                      [[ $2 != --* ]] || usage_error "--log-dir requires a directory, got: $2"
                      LOG_DIR_OVERRIDE=$2; shift 2 ;;
        --test)       TEST_MODE=true; shift ;;
        --pcm)        FORCE_PCM=true; shift ;;
        --mode0)      BWDIF_MODE=0; shift ;;
        --field-order) (($# >= 2)) || usage_error "--field-order requires tff, bff or progressive"
                      case $2 in
                          tff|bff|progressive) FIELD_ORDER_OVERRIDE=$2 ;;
                          *) usage_error "--field-order must be tff, bff or progressive, got: $2" ;;
                      esac
                      shift 2 ;;
        --yes)        SKIP_CONFIRM=true; shift ;;
        --)           shift; break ;;
        -*)           usage_error "unknown option: $1" ;;
        *)            # Explain how to supply a misplaced numeric mask argument.
                      if [[ $1 =~ ^[0-9]+$ && -n $SOURCE_INPUT ]]; then
                          usage_error "unexpected positional number: use --mask $1"
                      fi
                      [[ -z $SOURCE_INPUT ]] || usage_error "unexpected extra argument: $1"
                      SOURCE_INPUT=$1; shift ;;
    esac
done
# Anything after -- is a source path too.
if (($#)); then
    [[ -z $SOURCE_INPUT ]] || usage_error "unexpected extra argument: $1"
    SOURCE_INPUT=$1; shift
    (($# == 0)) || usage_error "unexpected extra argument: $1"
fi

[[ -n $SOURCE_INPUT ]] || usage_error "a source video is required"

# Fail fast with a clear message if the source file is missing or unreadable.
# Without this check, the first ffprobe call on SOURCE_INPUT (further below)
# fails with its stderr suppressed (2>/dev/null), and 'set -e' at the top of
# this script then exits immediately and silently -- no error, no exit
# message, nothing to indicate why. This check exists specifically to avoid
# that silent-failure path for the single most common mistake (typo'd path).
if [[ ! -e "$SOURCE_INPUT" ]]; then
    echo "Error: Source file not found: $SOURCE_INPUT" >&2
    exit 1
elif [[ ! -f "$SOURCE_INPUT" ]]; then
    echo "Error: Source is not a regular file: $SOURCE_INPUT" >&2
    exit 1
elif [[ ! -r "$SOURCE_INPUT" ]]; then
    echo "Error: Source file is not readable (check permissions): $SOURCE_INPUT" >&2
    exit 1
fi

# pipeline_tools/probe_video.py is used unless VIDEO_PROBE names another, with
# a PATH lookup as a last resort for a checkout whose pipeline_tools/ is
# missing. Resolved before any lengthy processing so an absent probe cannot
# look like an ffmpeg or encode failure later.
BUNDLED_PROBE="$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")/pipeline_tools/probe_video.py"
if [[ -n "${VIDEO_PROBE:-}" ]]; then
    PROBE_CANDIDATE="$VIDEO_PROBE"
elif [[ -r "$BUNDLED_PROBE" ]]; then
    PROBE_CANDIDATE="$BUNDLED_PROBE"
else
    PROBE_CANDIDATE="probe_video.py"
fi
if [[ "$PROBE_CANDIDATE" == */* ]]; then
    if [[ ! -f "$PROBE_CANDIDATE" || ! -r "$PROBE_CANDIDATE" ]]; then
        echo "Error: video probe is not a readable file: $PROBE_CANDIDATE" >&2
        echo "       Set VIDEO_PROBE to the full path of a probe_video.py." >&2
        exit 1
    fi
    PROBE_SCRIPT="$PROBE_CANDIDATE"
else
    PROBE_SCRIPT=$(command -v -- "$PROBE_CANDIDATE" 2>/dev/null) || {
        echo "Error: no video probe found." >&2
        echo "       Expected $BUNDLED_PROBE, or probe_video.py on PATH," >&2
        echo "       or VIDEO_PROBE set to its full path." >&2
        exit 1
    }
fi

command -v python3 >/dev/null 2>&1 || {
    echo "Error: Required command not found: python3" >&2
    exit 1
}

# CRF Validation
# --crf overrides the default. Validates that the value is a whole number
# within the x264 range and warns if it seems unreasonable for an SD pipeline
# intermediate. Hard error above 51 (x264 maximum).
# Default is CRF 12 for all source types. As a pipeline intermediate fed to
# Real-ESRGAN, maximum input quality applies regardless of the source's noise
# floor: detail lost here cannot be recovered by upscaling.
if [[ "$CRF_EXPLICIT" == false ]]; then
    CRF_VALUE=12
fi
if ! [[ "$CRF_VALUE" =~ ^[0-9]+$ ]]; then
    echo "Error: --crf requires a whole number (got: '$CRF_VALUE')." >&2
    exit 1
fi
if (( CRF_VALUE > 51 )); then
    echo "Error: CRF $CRF_VALUE is out of range — x264 maximum is 51." >&2
    exit 1
fi
if (( CRF_VALUE < 10 )); then
    echo -e "⚠️  WARNING: CRF $CRF_VALUE is very low — files will be very large" \
            "with negligible quality benefit over CRF 12."
elif (( CRF_VALUE > 16 )); then
    echo -e "⚠️  WARNING: CRF $CRF_VALUE is lossy for an SD pipeline intermediate." \
            "Detail loss will reduce Real-ESRGAN output quality."
    echo "    Recommended: 12 (default). Use --crf only if you have a specific reason."
fi

# Early Codec Probe
# IS_DV_SOURCE is set here for field order detection later in the script.
# The informational DV messages are emitted after the probe report to keep
# user-visible output in logical order.
#
# DV codec (dvsd, dv25, dvvideo) notes:
#   - Field order is always BFF on NTSC but ffprobe cannot read it from
#     the AVI container headers and returns 'unknown'.
#   - The dvvideo decoder emits 'AC EOB marker is absent' warnings for
#     malformed frames in the tape leader (garbage timecode region).
#     These are cosmetic and stop after the first few seconds.
# The '|| true' on both calls below is required, not cosmetic: ffprobe exits
# non-zero when it finds no matching stream (e.g. an empty/corrupt file), and
# under 'set -e' a plain VAR=$(...) assignment aborts the script immediately
# at that point -- before the emptiness check further below ever runs, and
# with no message since -v error + 2>/dev/null suppress ffprobe's own output.
# '|| true' makes the assignment itself always succeed so SOURCE_CODEC ends
# up empty (not an early, silent exit), and the check below can then report
# a clear error instead.
SOURCE_CODEC=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name \
    -of default=noprint_wrappers=1:nokey=1 \
    "$SOURCE_INPUT" 2>/dev/null) || true
SOURCE_PIX_FMT=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=pix_fmt \
    -of default=noprint_wrappers=1:nokey=1 \
    "$SOURCE_INPUT" 2>/dev/null) || true
# The sample aspect ratio is what makes 720x480 display as 4:3. It travels
# with the frames through the filter chain, but only implicitly; it is set
# on the way out and checked on the master below, so the summary can state
# it rather than assume it. A source that declares none (some AVI captures)
# is left as decoded and said to be.
SOURCE_WIDTH=""; SOURCE_HEIGHT=""; SOURCE_SAR=""; SOURCE_DAR=""
IFS=, read -r SOURCE_WIDTH SOURCE_HEIGHT SOURCE_SAR SOURCE_DAR < <(ffprobe -v error \
    -select_streams v:0 \
    -show_entries stream=width,height,sample_aspect_ratio,display_aspect_ratio \
    -of csv=p=0 "$SOURCE_INPUT" 2>/dev/null) || true
SAR_DECLARED=false
if [[ "$SOURCE_SAR" =~ ^[1-9][0-9]*:[1-9][0-9]*$ ]]; then
    SAR_DECLARED=true
fi

# The existence/readability check earlier catches missing or unreadable
# files, but not a file that exists and is readable yet isn't a video
# ffprobe can parse (corrupt, empty, wrong format entirely).
if [[ -z "$SOURCE_CODEC" ]]; then
    echo "Error: Could not detect a video stream in: $SOURCE_INPUT" >&2
    echo "       ffprobe found no codec_name -- the file may be corrupt, empty," >&2
    echo "       or not a video file ffmpeg/ffprobe can read." >&2
    exit 1
fi
# SOURCE_PIX_FMT reflects the container/stream pixel format as ffprobe reports
# it — for DV this is yuv411p (the native DV chroma), not the yuv420p that
# libavcodec outputs on decode. This is intentional: we want to show the user
# what the source actually contains, not what the decoder produces internally.
IS_DV_SOURCE=false
if [[ "$SOURCE_CODEC" == dv* ]]; then
    IS_DV_SOURCE=true
fi

# The master, and everything the pipeline makes from it, is tagged smpte170m:
# NTSC standard-definition colour. The tags only label the pixels; nothing
# converts them. A source tagged with another standard, such as BT.709 (HD and
# many H.264 recordings) or BT.470BG (PAL), would leave labelled as what it is
# not, and players would decode its colours with the wrong matrix. Untagged
# sources (DV, HuffYUV and most captures) are taken to be NTSC SD. Checked
# before any run directory exists, so a refused source leaves nothing behind.
SOURCE_COLOUR=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=color_space,color_transfer,color_primaries \
    -of default=noprint_wrappers=1 "$SOURCE_INPUT" 2>/dev/null) || true
COLOUR_CONFLICTS=$(awk -F= '$2 != "" && $2 != "unknown" && $2 != "smpte170m"' <<<"$SOURCE_COLOUR")
if [[ -n $COLOUR_CONFLICTS ]]; then
    echo "Error: the source is tagged with colour this pipeline does not support:" >&2
    sed 's/^/         /' <<<"$COLOUR_CONFLICTS" >&2
    echo "       Masters and upscales are tagged smpte170m (NTSC standard definition)" >&2
    echo "       and their colours are not converted, so these tags would be replaced" >&2
    echo "       by ones that misdescribe the picture. Only NTSC SD sources are supported." >&2
    exit 1
fi

# The bottom mask is whatever --mask says, and 0 when it says nothing.
#
# The mask follows the SIGNAL PATH, not the file format: anything that passed
# through an analog tape head carries head-switching noise and needs one,
# anything recorded digitally end to end does not. A tape played through a DV
# converter is dvvideo in every respect a probe can see, identical to a
# camcorder's own digital recording, and the two need opposite masks -- so
# nothing in the file can settle it. Only whoever captured the tape knows.
#
# Nothing is inferred from the filename. A convention that means something to
# one collection means nothing to another: a capture named by anyone else would
# get no mask, and a MiniDV file ending in .dv would have real picture blacked
# out. A wrapper that knows a given collection's naming belongs beside that
# collection and passes an explicit --mask. See pipeline_docs/prepare-video.md.

BASE_NAME=$(basename "$SOURCE_INPUT")
FILE_STEM="${BASE_NAME%.*}"

# Odd Number Sanity Check
# Prevents alignment artifacts at the drawbox mask boundary. Even numbers
# are required for correct macroblock alignment regardless of chroma subsampling.
if (( MASK_PIXELS % 2 != 0 )); then
    echo -e "\n⚠️  WARNING: Mask ($MASK_PIXELS) is an odd number."
    echo "    This can cause alignment artifacts at the mask boundary."
    echo "    Recommend using $((MASK_PIXELS + 1)) instead."
fi

# ISO Warning Guard
# Prevents ffmpeg from attempting to read raw disk images.
if [[ "${SOURCE_INPUT,,}" == *.iso ]]; then
    echo -e "\n[!] ERROR: Cannot process .ISO files directly."
    echo "    1. Mount the ISO (e.g., open it in your file manager)."
    echo "    2. Extract content VOBs: run dvd_extract.sh on the ISO (handles menu VOB exclusion)."
    echo "       Or manually: 'cat VIDEO_TS/VTS_*_[1-9]*.VOB > master.mpg'"
    echo "       Note: VTS_*_0.VOB files are menu/navigation VOBs — exclude them."
    echo "    3. Run this script on the resulting .mpg file."
    exit 1
fi

# Path and Filename Setup
#
# Every file this script writes lands inside one run directory. A pipeline
# driver passes --output-dir and --log-dir pointing into the run it already
# created, so its master and log join the rest of that run. Run standalone,
# this script creates a run of its own rather than writing to a shared
# location, which keeps "one run, one directory" true however it is invoked.
# See pipeline_docs/storage-layout.md.
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
PRESERVATION_ROOT="${VIDEO_PRESERVATION_ROOT:-${HOME}/VideoPreservation}"

OWN_RUN_DIR=""   # set only when this invocation has to create a run of its own
if [[ -z $OUTPUT_DIR_OVERRIDE || -z $LOG_DIR_OVERRIDE ]]; then
    # shellcheck source=pipeline_tools/run_lib.sh
    source "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")/pipeline_tools/run_lib.sh"
    OWN_RUN_DIR="$(run_root "$PRESERVATION_ROOT")/$(run_make_id "$FILE_STEM")"
    mkdir -p "$OWN_RUN_DIR"
    # Record the real source now, while it is still known. The pipeline runs
    # next and is handed the MASTER, so if provenance were left to it the run
    # would record a file inside itself as its own source and the chain back to
    # the capture would be lost. Ownership is left unclaimed: whichever tool
    # finishes the run takes it over and keeps these sources.
    if command -v jq >/dev/null 2>&1 && [[ ! -f $(run_manifest "$OWN_RUN_DIR") ]]; then
        run_manifest_init "$OWN_RUN_DIR" other prepare_video.sh "$PRESERVATION_ROOT" || true
        run_manifest_set "$OWN_RUN_DIR" --arg src "$(realpath -m -- "$SOURCE_INPUT")" \
            '.sources = [$src]' || true
    fi
fi

OUTPUT_DIR="${OUTPUT_DIR_OVERRIDE:-$(run_work_dir "$OWN_RUN_DIR")/masters}"
mkdir -p "$OUTPUT_DIR"

# MP4 unless the audio strategy below needs Matroska for PCM.
MASTER_EXT="mp4"

# The log directory holds everything recorded about processing this one
# source, so the log inside it needs no name beyond what it is. The master
# keeps its descriptive name: it is an intermediate that other tools locate by
# filename, and it sits beside masters for other sources.
# A run of this script's own making has one source, so its logs go straight
# into logs/. A driver with many sources passes --log-dir to keep them apart.
LOG_DIR="${LOG_DIR_OVERRIDE:-$(run_logs_dir "$OWN_RUN_DIR")}"
mkdir -p "$LOG_DIR"

# Masters carry a marker in the name and a matching tag in the container. Both
# say the same thing -- this file is a regenerable intermediate, not anyone's
# source -- so a tool asked to reclaim space can be sure of what it is deleting,
# and a person browsing the directory can see it without knowing the pipeline.
# The name is the visible half and the tag is the half that survives a rename.
# video_upscale_pipeline.py strips MASTER_MARKER when it names its own output.
MASTER_MARKER="_master_from_prepare_video"
MASTER_TAG="prepare_video.sh master; regenerable intermediate, not a source"

if [ "$TEST_MODE" = true ]; then
    LOG_FILE="${LOG_DIR}/prepare_TEST.log"
    LIMIT_CMD="-t 30"
    TEST_LABEL=" (TEST MODE: 30s)"
else
    LOG_FILE="${LOG_DIR}/prepare.log"
    LIMIT_CMD=""
    TEST_LABEL=""
fi

# Redirect all subsequent stdout and stderr through tee so every message
# printed to the terminal is also captured in the log file. The log stem
# matches the output file stem so the two can be correlated by name.
# exec is used so the redirect covers the rest of main() without needing
# to wrap individual commands.
exec > >(tee -a "$LOG_FILE") 2>&1
echo "Log: $LOG_FILE"
echo "Started: $(date)"

# 2. Run the Probe and Capture Results
echo "--- Step 1: Probing Video ---"
echo "[INFO] Video probe: $PROBE_SCRIPT"
PROBE_LOG=$(python3 "$PROBE_SCRIPT" "$SOURCE_INPUT")
echo "$PROBE_LOG"

# 3. Emit DV-Specific Info Messages
# SOURCE_CODEC and IS_DV_SOURCE identify the decoded format.
# Messages are deferred until here so they appear after the probe report
# in the user-visible output, keeping the log in logical order.
if [[ "$IS_DV_SOURCE" == true ]]; then
    if [[ -n $FIELD_ORDER_OVERRIDE ]]; then
        echo "[INFO] DV codec detected ($SOURCE_CODEC) — --field-order $FIELD_ORDER_OVERRIDE replaces the BFF assumption."
    else
        echo "[INFO] DV codec detected ($SOURCE_CODEC) — BFF field order assumed (NTSC standard)."
    fi
    echo "[INFO] Note: 'AC EOB marker' warnings at encode start are normal for DV tape"
    echo "       leader frames and will stop after the first few seconds."
fi

# 4. Audio Strategy Detection
# Detects source codec and determines re-encoding or preservation strategy.
#
# Aligned AAC can be copied; other audio defaults to AAC 192k for compatibility.
# PCM preservation requires Matroska: ffprobe reports N/A for PCM stream
# duration in MKV, causing spurious ~0.4s mismatches in the pipeline's
# audio/video duration check. AAC has reliable stream-level duration. The DV
# source file remains the lossless audio archive.
#
# Use --pcm to opt into lossless PCM preservation (switches output to .mkv).
#
# DV AUDIO NOTE: DV AVI files with poor interleaving can have their audio stream
# absent from the AVI header / idx1 index entirely, causing ffprobe to report no
# audio even though PCM audio data is present in the movi chunks (and audible in
# players that scan linearly, such as VLC). The ffmpeg demuxer discovers the stream
# at decode time via a "New audio stream" message, but ffprobe's probe phase never
# reaches it. Since DV always carries PCM audio in the DIF block bitstream, treat
# an empty ffprobe audio result for a DV source as "PCM present" and encode to AAC
# accordingly (or PCM if --pcm is specified).
AUDIO_FORMAT=$(ffprobe -v error -select_streams a:0 \
    -show_entries stream=codec_name \
    -of default=noprint_wrappers=1:nokey=1 \
    "$SOURCE_INPUT" 2>/dev/null)

if [[ "$AUDIO_FORMAT" == pcm* ]]; then
    if [[ "$FORCE_PCM" == true ]]; then
        # Opt-in lossless PCM copy. Requires MKV since MP4 does not support PCM.
        # Note: ffprobe reports N/A for PCM stream duration in MKV, which causes
        # spurious ~0.4s mismatches in video_upscale_pipeline.py's audio/video
        # duration check. See --pcm flag description.
        AUDIO_PLAN="LOSSLESS (PCM preserved bit-for-bit — .mkv output, --pcm specified)"
        MASTER_EXT="mkv"
        echo -e "\n[INFO] --pcm specified: PCM audio preserved, output container switched to .mkv."
    else
        # AAC 192k default: ffprobe reports N/A for PCM stream duration in MKV,
        # causing spurious ~0.4s mismatches in video_upscale_pipeline.py's
        # audio/video duration check (format-level vs stream-level measurement
        # disagreement). AAC has reliable stream-level duration, eliminating that
        # ambiguity. MP4 output is retained. The DV source file remains the
        # lossless audio archive.
        AUDIO_PLAN="AAC 192k (PCM source — AAC default: reliable stream duration for pipeline sync check)"
    fi
elif [[ -z "$AUDIO_FORMAT" && "$IS_DV_SOURCE" == true ]]; then
    # No audio stream visible to ffprobe, but source is DV. DV always embeds
    # PCM audio in the DIF block bitstream; a missing AVI header/index entry is
    # a container defect, not genuine silence.
    #
    # Neither a -c copy remux nor increased -probesize/-analyzeduration helps:
    # the AVI demuxer commits its output stream mapping before the late audio
    # chunk is discovered during linear frame reading, so audio:0kB results
    # regardless. The fix is to bypass the AVI container entirely by forcing
    # '-f dv' (Step 2a below), which reads the raw DV bitstream directly and
    # always exposes both video and audio. The encode pass then reads that temp
    # file as ENCODE_INPUT. AAC is used for the same reason as the PCM branch
    # above: reliable stream-level duration in the output file.
    if [[ "$FORCE_PCM" == true ]]; then
        AUDIO_PLAN="LOSSLESS (PCM assumed — DV/AVI remux + .mkv output, --pcm specified)"
        MASTER_EXT="mkv"
        echo -e "\n[INFO] --pcm specified: PCM audio preserved, output container switched to .mkv."
    else
        AUDIO_PLAN="AAC 192k (PCM assumed — DV source with no audio in AVI index; -f dv remux recovers audio)"
    fi
    DV_AUDIO_REMUX_NEEDED=true
    echo -e "\n[INFO] DV source: no audio stream found by ffprobe, but DV always carries"
    echo "       PCM audio in the bitstream. Assuming PCM present (malformed AVI index)."
    echo "       A raw DV demux pass (-f dv) will recover the audio before encoding."
elif [[ "$AUDIO_FORMAT" == aac ]]; then
    AUDIO_PLAN="LOSSLESS (Bitstream copy of AAC)"
elif [[ -z "$AUDIO_FORMAT" ]]; then
    AUDIO_PLAN="No audio track"
else
    # DVD AC-3 and MP2 need AAC for broadly compatible MP4 playback.
    AUDIO_PLAN="AAC 192k ($AUDIO_FORMAT source — compatibility encode)"
fi

# 4b. Suggested Pipeline Profile
# Derives a recommended --profile value for video_upscale_pipeline.py based on
# the source codec and bitrate. Shown in the pre-flight summary as a hint — the
# user is free to override it when invoking the pipeline, and should when the
# capture's provenance says more than its codec does.
#
# Decision logic:
#   dvvideo                       → dv              (native DV capture)
#   mpeg2video, bitrate > 6 Mbps  → hi8dv           (high-bitrate MPEG-2: pp=ac dering beneficial)
#   mpeg2video, bitrate 3–6 Mbps  → balanced        (typical DVD bitrate)
#   mpeg2video, bitrate < 3 Mbps  → aggressive      (low-bitrate DVD: heavy blocking)
#   anything else                 → balanced        (safe default)
#
# Bitrate is extracted from the probe report's "Data Health" line, which includes
# the source bitrate in Mbps (e.g. "8.21 Mbps").
SOURCE_BITRATE_MBPS=$(echo "$PROBE_LOG" | grep -oP '\d+\.\d+(?= Mbps)' || true)

SUGGESTED_PROFILE="balanced"
SUGGESTED_PROFILE_REASON=""
# Derived from what the file IS -- its codec and bitrate -- and not from what
# it is called. This is a hint for the NEXT stage, printed as a convenience;
# --profile is an option of video_upscale_pipeline.py and not of this script.
# A codec cannot distinguish an analog transfer from a digital recording, so
# the suggestion is worth overriding whenever the provenance is known.
if [[ "$IS_DV_SOURCE" == true ]]; then

    SUGGESTED_PROFILE="dv"
    SUGGESTED_PROFILE_REASON="DV codec — native DV capture. An analog tape transferred through a DV converter is also DV: use --profile hi8dv or vhsdv for those"
elif [[ "$SOURCE_CODEC" == "mpeg2video" ]]; then
    if [[ -n "$SOURCE_BITRATE_MBPS" ]]; then
        # Bitrate says how hard the compression was, not what was recorded,
        # so it can only move the suggestion towards heavier deblocking. Which
        # tape a DVD was made from is for the caller to say with --profile.
        if awk -v source_bitrate_mbps="$SOURCE_BITRATE_MBPS" 'BEGIN { exit (source_bitrate_mbps < 3.0) ? 0 : 1 }'; then
            SUGGESTED_PROFILE="aggressive"
            SUGGESTED_PROFILE_REASON="MPEG-2 source at ${SOURCE_BITRATE_MBPS} Mbps — low bitrate, heavier deblock/dering needed"
        else
            SUGGESTED_PROFILE="balanced"
            SUGGESTED_PROFILE_REASON="MPEG-2 source at ${SOURCE_BITRATE_MBPS} Mbps — DVD-level compression or lighter"
        fi
    else
        SUGGESTED_PROFILE="balanced"
        SUGGESTED_PROFILE_REASON="MPEG-2 source (bitrate unknown) — balanced is safe default"
    fi
else
    SUGGESTED_PROFILE="balanced"
    SUGGESTED_PROFILE_REASON="non-DV/MPEG-2 source — balanced is safe default"
fi


#
# Detection priority:
#   0. --field-order — the caller knows the source; nothing is detected
#   1. DV codec shortcut — always BFF on NTSC, ffprobe cannot read from AVI headers
#   2. ffprobe stream=field_order — progressive, tt and bb settle it
#   3. probe_video.py — frame flags, then idet. It also takes the swapped orders
#      tb and bt, which FFmpeg writes contrary to its own definitions of them:
#      they mark the stream interlaced, and the order still has to be found.
#      An interlaced source whose order nothing establishes stops preparation
#      with directions for --field-order.
#
FFPROBE_FIELD_ORDER=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=field_order \
    -of default=noprint_wrappers=1:nokey=1 \
    "$SOURCE_INPUT" 2>/dev/null)

# Normalise to upper-case for consistent matching
FFPROBE_FIELD_ORDER_UC="${FFPROBE_FIELD_ORDER^^}"

if [[ $FIELD_ORDER_OVERRIDE == tff ]]; then
    IS_INTERLACED=true
    PARITY="0"
    FIELD_ORDER="TFF (Top Field First, set by --field-order)"
elif [[ $FIELD_ORDER_OVERRIDE == bff ]]; then
    IS_INTERLACED=true
    PARITY="1"
    FIELD_ORDER="BFF (Bottom Field First, set by --field-order)"
elif [[ $FIELD_ORDER_OVERRIDE == progressive ]]; then
    IS_INTERLACED=false
    PARITY="-1"
    FIELD_ORDER="Progressive (set by --field-order)"
elif [[ "$IS_DV_SOURCE" == true ]]; then
    # DV from FireWire capture is always BFF on NTSC regardless of what
    # ffprobe reports. The AVI container does not store field order metadata
    # for DV streams, causing ffprobe to return 'unknown'. We shortcut here
    # to avoid falling through to the auto-detect path.
    IS_INTERLACED=true
    PARITY="1"
    FIELD_ORDER="BFF (Bottom Field First — DV/NTSC, codec-based detection)"

elif [[ "$FFPROBE_FIELD_ORDER_UC" == "PROGRESSIVE" ]]; then
    IS_INTERLACED=false
    PARITY="-1"
    FIELD_ORDER="Progressive"
elif [[ "$FFPROBE_FIELD_ORDER_UC" == "TT" ]]; then
    IS_INTERLACED=true
    PARITY="0"
    FIELD_ORDER="TFF (Top Field First)"
elif [[ "$FFPROBE_FIELD_ORDER_UC" == "BB" ]]; then
    IS_INTERLACED=true
    PARITY="1"
    FIELD_ORDER="BFF (Bottom Field First)"
else
    # ffprobe returned 'unknown', empty or a swapped order for a non-DV
    # source. probe_video.py's scan type decides. Its text can itself contain
    # ': ', so everything after the label is kept.
    SCAN_TYPE=$(sed -n 's/^Scan Type:[[:space:]]*//p' <<<"$PROBE_LOG" | head -n 1)
    if [[ $SCAN_TYPE == Interlaced* ]]; then
        IS_INTERLACED=true
        # Only the label's opening names the order: the rest can quote idet
        # counts that mention both.
        if [[ $SCAN_TYPE == "Interlaced (TFF"* ]]; then
            PARITY="0"
            FIELD_ORDER="TFF (Top Field First, from probe fallback)"
        elif [[ $SCAN_TYPE == "Interlaced (BFF"* ]]; then
            PARITY="1"
            FIELD_ORDER="BFF (Bottom Field First, from probe fallback)"
        else
            # Interlaced, with nothing showing which field comes first. The
            # wrong order makes motion jerk back and forth through the whole
            # master, so the caller decides rather than bwdif guessing.
            echo "[ERROR] The source is interlaced, but its field order was not established:" >&2
            echo "        $SCAN_TYPE" >&2
            echo "        Prepare a --test master with --field-order tff and another with" >&2
            echo "        --field-order bff. The right order moves smoothly; the wrong one" >&2
            echo "        jerks back and forth. Then prepare the whole capture with that" >&2
            echo "        order. See pipeline_docs/prepare-video.md." >&2
            exit 1
        fi
    else
        IS_INTERLACED=false
        PARITY="-1"
        FIELD_ORDER="Progressive (from probe fallback)"
        if [[ $SCAN_TYPE != Progressive* ]]; then
            # Treated as progressive because nothing showed interlacing, not
            # because anything showed progressive. Say so before the encode.
            FIELD_ORDER="Not established, treated as progressive (${SCAN_TYPE:-no scan type reported})"
            echo "⚠️  WARNING: Scan type not established (${SCAN_TYPE:-no scan type reported});"
            echo "   the source is treated as progressive and will not be deinterlaced."
            echo "   Check a --test master for comb-like edges on movement, and pass"
            echo "   --field-order tff or bff if it shows them."
        fi
    fi
fi

# PTS Regeneration if probe_video.py detects broken or unrealistic durations.
FFLAGS=""
if echo "$PROBE_LOG" | grep -q "unrealistic"; then
    echo "Detected broken timestamps. Enabling PTS regeneration..."
    FFLAGS="-fflags +genpts"
fi
# --loglevel: pass caller-specified ffmpeg log level (e.g. verbose, debug, trace).
# verbose logs filter graph negotiation including per-pad pixel formats.
# debug/trace provide deeper inspection (e.g. confirming bwdif input pixel format).
if [[ -n "$FFMPEG_LOGLEVEL" ]]; then
    FFLAGS="$FFLAGS -loglevel $FFMPEG_LOGLEVEL"
fi

# ENCODE_INPUT is normally the original source file. When DV_AUDIO_REMUX_NEEDED
# is set (malformed AVI index hides the audio stream), we first extract the raw
# DV bitstream using '-f dv' (Step 2a). The DV demuxer reads DIF blocks directly,
# bypassing the broken AVI container headers entirely, and always exposes both
# video and audio streams. The resulting temp MKV is used as ENCODE_INPUT so that
# -c:a copy in the deinterlace encode has an actual audio stream to copy.
# The temp file is removed on exit via trap.
ENCODE_INPUT="$SOURCE_INPUT"
REMUX_TMPFILE=""
MASTER_IN_PROGRESS=""
cleanup_preparation() {
    local status=$?
    [[ -z "$REMUX_TMPFILE" ]] || rm -f -- "$REMUX_TMPFILE"
    [[ -z "$MASTER_IN_PROGRESS" ]] || rm -f -- "$MASTER_IN_PROGRESS"
    return "$status"
}
trap cleanup_preparation EXIT
if [[ "$DV_AUDIO_REMUX_NEEDED" == true ]]; then
    REMUX_TMPFILE=$(mktemp --suffix=.mkv)
    echo -e "\n--- Step 2a: Raw DV demux to recover hidden audio stream ---"
    echo "  > Reading $SOURCE_INPUT as raw DV bitstream (-f dv) → $REMUX_TMPFILE"
    # -f dv forces the DV bitstream demuxer, bypassing the AVI container.
    # The DV demuxer reads audio directly from the DIF blocks, so it is
    # unaffected by the missing AVI index entry. -nostdin: see the ffmpeg
    # argument notes before the master encode.
    ffmpeg -nostdin -y -f dv -i "$SOURCE_INPUT" -c copy "$REMUX_TMPFILE"
    ENCODE_INPUT="$REMUX_TMPFILE"
    echo "  > Raw DV demux complete."
fi

# Remove temp file on exit (normal, error, or signal).
# Guard prevents a spurious rm error when REMUX_TMPFILE was never set.


# Measure decoded timestamps before the encode and use one picture origin for
# both streams. The PCM/DV path is measured after hidden-audio recovery too.
AUDIO_TIMING_TOOL="$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")/pipeline_tools/prepare_audio_timing.py"
AUDIO_TIMING_PLAN="$LOG_DIR/audio_timing_plan.json"
AUDIO_TIMING_OPTIONS=()
[[ "$FORCE_PCM" == false ]] || AUDIO_TIMING_OPTIONS+=(--pcm)
[[ "$TEST_MODE" == false ]] || AUDIO_TIMING_OPTIONS+=(--test)
echo -e "\n--- Measuring audio timing (decodes the whole audio stream; no output until done) ---"
python3 "$AUDIO_TIMING_TOOL" plan "$ENCODE_INPUT" \
    --output "$AUDIO_TIMING_PLAN" "${AUDIO_TIMING_OPTIONS[@]}"
mapfile -t AUDIO_ARGS < <(python3 - "$AUDIO_TIMING_PLAN" <<'PYARGS'
import json, sys
for arg in json.load(open(sys.argv[1]))['ffmpeg_audio_args']:
    print(arg)
PYARGS
)
AUDIO_PLAN=$(python3 - "$AUDIO_TIMING_PLAN" <<'PYPLAN'
import json, sys
p = json.load(open(sys.argv[1]))
if not p['audio_present']:
    print('No audio track')
else:
    operation = 'stream copy' if p['copy'] else 'sample-aligned encode'
    print(f"{p['output_codec']} ({operation}); trim {p['trim_samples']} samples, "
          f"prepend {p['pad_samples']} and append {p['tail_pad_samples']} silence samples "
          f"at {p['sample_rate']} Hz")
PYPLAN
)

# 5b. Output naming
#
# The fps label states the rate of the master, which is why it is decided here
# rather than with the other paths: deinterlacing to field rate doubles it,
# and a progressive source keeps whatever rate it already had.
SOURCE_FPS=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate \
    -of default=noprint_wrappers=1:nokey=1 "$SOURCE_INPUT" 2>/dev/null)
SOURCE_FPS_NUM=$(awk -F/ -v r="$SOURCE_FPS" 'BEGIN {
    n = split(r, p, "/"); if (n == 2 && p[2] > 0) printf "%.6f", p[1] / p[2];
    else if (r + 0 > 0) printf "%.6f", r; else print "" }')
if [ "$IS_INTERLACED" = true ]; then
    if [ "$BWDIF_MODE" -eq 0 ]; then FPS_LABEL="_30fps"; else FPS_LABEL="_60fps"; fi
elif [[ -n "$SOURCE_FPS_NUM" ]] \
     && awk -v f="$SOURCE_FPS_NUM" 'BEGIN { exit (f >= 45) ? 0 : 1 }'; then
    FPS_LABEL="_60fps"
else
    FPS_LABEL="_30fps"
fi

if [ "$TEST_MODE" = true ]; then
    PROG_OUTPUT="${OUTPUT_DIR}/${FILE_STEM}_mask${MASK_PIXELS}${FPS_LABEL}_TEST${MASTER_MARKER}.${MASTER_EXT}"
else
    PROG_OUTPUT="${OUTPUT_DIR}/${FILE_STEM}_mask${MASK_PIXELS}${FPS_LABEL}_${TIMESTAMP}${MASTER_MARKER}.${MASTER_EXT}"
fi

# 6. Pre-Flight Summary & Confirmation
# Summarizes both Video and Audio plans before committing to a long encode.
echo -e "\n========================================="
echo "       PRE-FLIGHT SUMMARY$TEST_LABEL"
echo "========================================="
echo "Source File:   $SOURCE_INPUT"
echo "Source Codec:  $SOURCE_CODEC  |  Pixel fmt: $SOURCE_PIX_FMT"
if [[ -n "$SOURCE_BITRATE_MBPS" ]]; then
    echo "Source Bitrate: ${SOURCE_BITRATE_MBPS} Mbps"
fi
echo "Output File:   $PROG_OUTPUT"
echo "Output Chroma: yuv444p  (upsampled from decoded $SOURCE_PIX_FMT — avoids second chroma subsampling round-trip)"
if [[ "$SAR_DECLARED" == true ]]; then
    echo "Aspect:        SAR ${SOURCE_SAR} set on the master as on the source — DAR ${SOURCE_DAR} at ${SOURCE_WIDTH}x${SOURCE_HEIGHT}; checked after the encode"
else
    echo "Aspect:        source declares no sample aspect ratio (${SOURCE_SAR:-none}); the master is left as decoded at ${SOURCE_WIDTH}x${SOURCE_HEIGHT}"
fi
echo "Audio Plan:    $AUDIO_PLAN"
audio_timing_report notices "$AUDIO_TIMING_PLAN" "               "
echo "Bottom Mask:   ${MASK_PIXELS} pixels"
echo "CRF:           ${CRF_VALUE}"
if [[ -n "$FFMPEG_LOGLEVEL" ]]; then
    echo "Log Level:     ${FFMPEG_LOGLEVEL}  (-loglevel ${FFMPEG_LOGLEVEL} passed to ffmpeg)"
fi
echo "Pipeline profile suggestion:"
echo "  --profile ${SUGGESTED_PROFILE}  (${SUGGESTED_PROFILE_REASON})"

if [ "$IS_INTERLACED" = true ]; then
    if [ "$BWDIF_MODE" -eq 1 ]; then
        VIDEO_PLAN="HIGH-QUALITY DEINTERLACING (bwdif mode=1, field-rate ~60fps) — Field Order: ${FIELD_ORDER}"
    else
        VIDEO_PLAN="HIGH-QUALITY DEINTERLACING (bwdif mode=0, frame-rate ~30fps) — Field Order: ${FIELD_ORDER}"
    fi
    echo "Video Plan:    $VIDEO_PLAN"
    echo -e "\n⚠️  WARNING: Deinterlacing is a CPU-intensive process."
    echo "    Depending on video length, this will take quite awhile."
    if [ "$BWDIF_MODE" -eq 1 ]; then
        echo "    NOTE: mode=1 output is ~60fps (one frame per field)."
        echo "          ESRGAN will process 2x as many frames — expect ~2x upscaling runtime."
        echo "          Leave RIFE off in video_upscale_pipeline.py: it is off unless you pass --rife."
    else
        echo "    NOTE: mode=0 output is ~30fps. Both fields combined into full-resolution"
        echo "          progressive frames. Use the default (mode=1) for field-rate 60fps output."
    fi
else
    VIDEO_PLAN="Progressive Copy (No deinterlacing) — Scan: ${FIELD_ORDER}"
    echo "Video Plan:    $VIDEO_PLAN"
fi
echo "========================================="

# --yes suppresses the prompt for non-interactive / batch use.
if [[ "$SKIP_CONFIRM" == true ]]; then
    echo "Skipping confirmation (--yes)."
else
    # Only y or n is an answer: anything else, a bare Enter included, asks
    # again, and Ctrl+C stops the script. The whole line is read, Enter and
    # all, so nothing typed here stays queued for the upscale's prompt. A
    # last line without its newline still counts, as from `printf n |`.
    while true; do
        if ! read -r -p "Do you wish to begin? (y/n): " answer && [[ -z $answer ]]; then
            echo
            echo "[ERROR] Input closed before a y or n answer; stopping." >&2
            exit 1
        fi
        [[ ${answer,,} == [yn] ]] && break
        echo "Answer y or n (Ctrl+C to stop)."
    done
    if [[ ${answer,,} == n ]]; then
        echo "Operation cancelled by user."
        # Exit 3 is the declined-prompt status a driver keys on to tell a
        # decline from a failed preparation: examples/enhance_tape.sh
        # removes its own run on 3 and keeps it, log included, on 1. A run
        # this invocation made for itself is removed here for the same
        # reason; it holds only the summary that was just declined. A run
        # a driver handed in through --output-dir/--log-dir is its to keep.
        if [[ -n $OWN_RUN_DIR ]] && run_discard_unstarted "$OWN_RUN_DIR"; then
            echo "[INFO] Removed the unused run directory: $OWN_RUN_DIR"
        fi
        exit 3
    fi
fi

# 7. Processing
#
# The 4:4:4 upsample stage, told where the source's chroma samples actually
# sit. The decoder knows and says so: ffprobe reports chroma_location as
# topleft for NTSC DV, left for MPEG-2 and MPEG-4. Reading it per file beats
# assuming per codec, and beats swscale's behaviour, which ignores the tag and
# defaults unset locations to CENTER.
#
# Declaring the wrong siting is not a no-op. left and topleft differ vertically
# by half a chroma row, so upsampling an MPEG-2 DVD as though it were DV shifts
# its chroma against its luma -- the same class of registration error the DV
# handling exists to remove, applied in reverse.
#
# Only the INPUT location matters here. The output is 4:4:4, where every sample
# is co-sited by definition and the location has nothing left to describe.
SOURCE_CHROMA_LOC=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=chroma_location \
    -of default=noprint_wrappers=1:nokey=1 "$SOURCE_INPUT" 2>/dev/null)
case "$SOURCE_CHROMA_LOC" in
    left|center|topleft|top|bottomleft|bottom)
        CHROMA_STAGE="zscale=chromalin=${SOURCE_CHROMA_LOC}:chromal=${SOURCE_CHROMA_LOC}:filter=lanczos,format=yuv444p"
        ;;
    *)
        # Nothing declared: no basis for overriding, so let the plain
        # conversion apply its own default rather than inventing a siting.
        CHROMA_STAGE="format=yuv444p"
        ;;
esac

# Both scan types produce a master. A progressive source is not passed through
# untouched: it still needs the mask, CRF 12, yuv444p, the master tag and the
# duration and frame-count checks, and every caller of this script expects a
# file to exist in --output-dir when it returns. The only difference is that
# bwdif is absent from the filter chain.
if [ "$IS_INTERLACED" = true ]; then
    echo -e "\n--- Step 2: High-Quality Deinterlacing (${FIELD_ORDER}) ---"

    # Filter Chain: bwdif mode=$BWDIF_MODE, yuv444p format, and optional drawbox.
    #   mode=1 (default): field-rate output — one progressive frame per field (~60fps).
    #                     Preserves full temporal resolution. Run the upscale pipeline without
    #                     --rife (RIFE is off by default), which would double it to ~120fps.
    #   mode=0 (--mode0): frame-rate output — one progressive frame per interlaced frame (~30fps).
    #
    #   format=yuv444p: placed BEFORE bwdif to prevent the implicit yuv411p->yuv420p
    #   conversion FFmpeg would otherwise insert, since bwdif does not support yuv411p
    #   natively. Native NTSC DV is 4:1:1; the dvvideo decoder outputs yuv411p frames.
    #   Without an explicit format conversion first, FFmpeg silently converts yuv411p to
    #   yuv420p before bwdif runs, halving vertical chroma resolution before deinterlacing.
    #   Converting to yuv444p first means bwdif operates in 4:4:4, keeping the pipeline
    #   end-to-end yuv444p from this point through to the ESRGAN intermediate.
    #   Real-ESRGAN decodes intermediates to float32 RGB regardless of pix_fmt, so feeding
    #   it a yuv444p master costs only slightly more disk space with no processing overhead.
    #
    #   setpts=PTS-STARTPTS resets decoded timestamps so frame ordinal zero
    #   stays ordinal zero. A source whose first decoded frame carries a
    #   positive PTS -- an edit list, or a B-frame timestamp representation --
    #   reaches FFmpeg's constant-frame-rate output synchroniser, which fills
    #   the apparent gap by duplicating leading frames. Those duplicates shift
    #   the whole picture against the audio and survive every later stage.
    #   Harmless for input that already begins at zero, and the pipeline
    #   applies the same reset before each of its own pre-filter encodes.
    #
    #   The zscale stage performs the actual chroma reconstruction to 4:4:4;
    #   the format=yuv444p that follows only pins the target layout for bwdif.
    #
    #   The siting matters. The dvvideo decoder tags decoded frames
    #   chroma_sample_location=TOPLEFT, DV's real co-sited chroma position (see
    #   libavcodec/dvdec.c), but a plain "format" conversion goes through
    #   swscale, which does not read that tag and defaults unset chroma location
    #   to CENTER. A decoded frame through "format=yuv444p" differs byte-for-byte
    #   from the same frame through zscale with the siting stated.
    #
    #   zscale (libzimg) rather than scale: scale's own in_chroma_loc and
    #   out_chroma_loc options do not exist in FFmpeg 4.4.2, the version this
    #   targets. zscale's chromal/chromalin have existed since 2016 and this
    #   build has --enable-libzimg.
    #
    #   The siting comes from the source, not from this codec: see
    #   SOURCE_CHROMA_LOC above. DV declares topleft; MPEG-2 and MPEG-4 declare
    #   left, and upsampling one of those as though it were DV changes 8% of
    #   the bytes in a frame.
    #
    #   For DV specifically the correction is small and real. An isolation test using the same
    #   lanczos kernel with deliberately wrong (center) siting tracked the
    #   uncorrected output, so the shift comes from the siting rather than the
    #   kernel; scanline profiling across 15 real colour edges shows a
    #   consistent -0.75 to -2.75 px registration shift, the direction and
    #   magnitude expected from DV's ~1.5 px co-siting offset. Whole-clip
    #   chroma statistics move only ~2%, which is what a geometry correction
    #   looks like on a source whose analog chroma bandwidth is already
    #   oversampled by 4:1:1 sampling -- not evidence the fix is marginal.
    FILTER_CHAIN="setpts=PTS-STARTPTS,${CHROMA_STAGE},bwdif=mode=${BWDIF_MODE}:parity=${PARITY}:deint=0"
    FRAME_MULTIPLIER=1
    [[ "$BWDIF_MODE" -eq 1 ]] && FRAME_MULTIPLIER=2
else
    echo -e "\n--- Step 2: Progressive Source (no deinterlacing) ---"
    # The same chain without bwdif: one output picture per input picture.
    FILTER_CHAIN="setpts=PTS-STARTPTS,${CHROMA_STAGE}"
    FRAME_MULTIPLIER=1
fi

if [[ "$SAR_DECLARED" == true ]]; then
    # Stated, not inherited: the filters above keep the sample aspect ratio,
    # but the master's geometry should not depend on each of them doing so.
    # setsar takes a/b; a colon would read as the option separator.
    FILTER_CHAIN="${FILTER_CHAIN},setsar=${SOURCE_SAR/:/\/}"
fi

if [ "$MASK_PIXELS" -gt 0 ]; then
    echo "Applying mask: Bottom $MASK_PIXELS pixels"
    FILTER_CHAIN="${FILTER_CHAIN},drawbox=y=ih-${MASK_PIXELS}:h=${MASK_PIXELS}:color=black:t=fill"
fi

echo "Generating master: $PROG_OUTPUT"

# ffmpeg argument order notes:
#   -nostdin keeps ffmpeg off the terminal. Without it ffmpeg reads stdin for
#   its interactive keys, and ffmpeg 4.4's 'c' and 'd' keys start a line-entry
#   loop that polls without waiting and holds the encode until Enter arrives;
#   any key typed into this window earlier, while nothing was reading it, is
#   still queued for ffmpeg to take. This script's own prompts read stdin, so
#   the whole run cannot simply be redirected from /dev/null.
#
#   $LIMIT_CMD limits the aligned output to 30s in test mode. An input limit
#   would cut against the source container origin, which can precede the first
#   picture, and would disagree with the sample-alignment plan.
#
#   -threads 0 lets libx264 auto-detect the optimal thread count for the
#   host CPU rather than hardcoding a value that may under or over-utilize
#   available cores.
#
#   -crf defaults to 12 for all SD source types — maximum quality input
#   for Real-ESRGAN regardless of source noise floor.
#   -preset fast: this is a temporary intermediate consumed frame-by-frame
#   by Real-ESRGAN, which does not benefit from inter-frame compression
#   efficiency. fast encodes significantly quicker than slower/medium with
#   no perceptible quality difference for an intermediate at any CRF.
#
#   -pix_fmt yuv444p: must be stated explicitly; libx264 does not infer
#   pix_fmt from the filter chain. Ensures the encoded master matches the
#   yuv444p frames produced by the filter chain above.
#
#   -colorspace/-color_primaries/-color_trc: stamp smpte170m (BT.601 NTSC)
#   explicitly so downstream consumers (video_upscale_pipeline.py's pre-filter
#   decode, ESRGAN Reader) use the correct YCbCr<->RGB matrix rather than
#   inferring it from SD resolution heuristics. A source tagged with other
#   colour was refused at the start.
#
#   -movflags +faststart is MP4-specific but harmless on MKV (ignored silently).
# Mark the destination as incomplete until every post-encode validation
# succeeds. EXIT also covers Ctrl-C and termination signals, preventing a
# partial file from looking like a reusable prepared master.
# The tag also states any silence the master carries that the source does
# not, and a field order the caller set rather than one detected, so those
# facts survive with the file and not only in the run's logs.
MASTER_COMMENT="$MASTER_TAG"
AUDIO_TAG=$(audio_timing_report tag "$AUDIO_TIMING_PLAN")
[[ -z $AUDIO_TAG ]] || MASTER_COMMENT="$MASTER_TAG; $AUDIO_TAG"
[[ -z $FIELD_ORDER_OVERRIDE ]] \
    || MASTER_COMMENT="$MASTER_COMMENT; field order: $FIELD_ORDER_OVERRIDE set by --field-order"
MASTER_IN_PROGRESS="$PROG_OUTPUT"
ffmpeg -nostdin -y $FFLAGS -i "$ENCODE_INPUT" \
    -vf "$FILTER_CHAIN" \
    -c:v libx264 -threads 0 -crf "$CRF_VALUE" -preset fast \
    -pix_fmt yuv444p \
    -colorspace smpte170m \
    -color_primaries smpte170m \
    -color_trc smpte170m \
    -movflags +faststart \
    -metadata "comment=${MASTER_COMMENT}" \
    -map 0:v:0 -map '0:a:0?' \
    "${AUDIO_ARGS[@]}" $LIMIT_CMD \
    "$PROG_OUTPUT"

# Verify completeness before this master can be consumed by ESRGAN. A
# successful ffmpeg exit alone does not prove the full timeline was
# written, while a duration probe is effectively free compared with the
# encode that just completed.
# The reference is the picture span the audio timing plan measured from the
# decoded source, not the container's advertised stream duration: the two
# disagree whenever the container origin precedes the first picture, and the
# plan's span is what the encode above was asked to produce. `|| true` lets
# an unreadable plan reach the diagnostic below despite the Python failure.
EXPECTED_DURATION=$(python3 - "$AUDIO_TIMING_PLAN" <<'PYSPAN' || true
import json, sys
from fractions import Fraction
print(float(Fraction(json.load(open(sys.argv[1]))['picture_span_seconds'])))
PYSPAN
)
OUTPUT_DURATION=$(video_stream_seconds "$PROG_OUTPUT")
DURATION_TOLERANCE=$(frame_seconds_of "$SOURCE_INPUT")
if ! is_seconds "$EXPECTED_DURATION" || ! is_seconds "$OUTPUT_DURATION"; then
    echo "[ERROR] Could not verify the master's duration." >&2
    echo "        Plan:   $AUDIO_TIMING_PLAN" >&2
    echo "        Master: $PROG_OUTPUT" >&2
    exit 1
fi
DURATION_DELTA=$(awk -v output_duration="$OUTPUT_DURATION" -v expected_duration="$EXPECTED_DURATION" 'BEGIN { d = output_duration - expected_duration; \
                              if (d < 0) d = -d; printf "%.3f", d }')
if awk -v duration_delta="$DURATION_DELTA" -v tol="$DURATION_TOLERANCE" \
       'BEGIN { exit (duration_delta > tol) ? 0 : 1 }'; then
    echo "[ERROR] Master duration differs from the planned picture span." >&2
    echo "        Expected (audio timing plan): ${EXPECTED_DURATION}s  Master: ${OUTPUT_DURATION}s" \
         " Delta: ${DURATION_DELTA}s (tolerance: ${DURATION_TOLERANCE}s = 1 source frame)" >&2
    exit 1
fi
echo "  > Duration check: expected ${EXPECTED_DURATION}s (audio timing plan), master ${OUTPUT_DURATION}s," \
     "delta ${DURATION_DELTA}s (tolerance ${DURATION_TOLERANCE}s = 1 source frame)."

# For a complete encode, field-rate bwdif must produce exactly two output
# frames per source frame; mode 0 must remain one-for-one. This catches
# leading CFR duplicates that duration-only validation cannot detect.
if [[ "$TEST_MODE" == false ]]; then
    if [[ "$SOURCE_CODEC" == dv* && "$SOURCE_INPUT" == *.dv ]]; then
        SOURCE_BYTES=$(stat -c %s "$SOURCE_INPUT")
        (( SOURCE_BYTES % 120000 == 0 )) || { echo "[ERROR] Raw NTSC DV size is not frame-aligned." >&2; exit 1; }
        SOURCE_VIDEO_FRAMES=$(( SOURCE_BYTES / 120000 ))
    else
        # Decoded pictures, not packets: a packet need not yield a picture.
        # An MPEG-2 program cut at an open GOP starts with B-frames whose
        # forward reference was cut away, and the decoder drops them. The
        # packet count reads the whole file anyway, so decoding on every core
        # adds CPU but little wall time.
        SOURCE_VIDEO_FRAMES=$(ffprobe -v error -threads 0 -select_streams v:0 -count_frames \
            -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "$SOURCE_INPUT")
    fi
    OUTPUT_VIDEO_FRAMES=$(ffprobe -v error -select_streams v:0 -count_packets \
        -show_entries stream=nb_read_packets -of default=nw=1:nk=1 "$PROG_OUTPUT")
    [[ "$SOURCE_VIDEO_FRAMES" =~ ^[0-9]+$ && "$OUTPUT_VIDEO_FRAMES" =~ ^[0-9]+$ ]] || {
        echo "[ERROR] Could not verify source/master frame counts." >&2; exit 1;
    }
    # Frames per source frame is the right expectation only when the source's
    # own frame count agrees with its duration. Some containers carry gaps:
    # MPEG-4 in AVI can hold 60 frames on a 61-frame-period timeline, its
    # packet timestamps stepping 0.000 -> 0.0667 with nothing at 0.0334. The
    # master is constant frame rate, so it fills the hole and legitimately
    # holds one more picture than the source did.
    #
    # Where the two agree -- every DV capture, every well-formed source -- the
    # exact multiple is required, unchanged. Where they do not, the
    # expectation comes from the source's DURATION at the output rate, which
    # still catches a truncated encode and still catches leading CFR
    # duplicates, because either changes the count away from what the source's
    # own timeline implies.
    SOURCE_CFR_FRAMES=""
    if [[ -n "$SOURCE_FPS_NUM" ]] && is_seconds "$EXPECTED_DURATION"; then
        SOURCE_CFR_FRAMES=$(awk -v d="$EXPECTED_DURATION" -v f="$SOURCE_FPS_NUM" \
            'BEGIN { printf "%d", d * f + 0.5 }')
    fi
    if [[ -n "$SOURCE_CFR_FRAMES" && "$SOURCE_CFR_FRAMES" != "$SOURCE_VIDEO_FRAMES" ]]; then
        EXPECTED_VIDEO_FRAMES=$(( SOURCE_CFR_FRAMES * FRAME_MULTIPLIER ))
        echo "  > Source holds $SOURCE_VIDEO_FRAMES pictures on a" \
             "${SOURCE_CFR_FRAMES}-picture timeline (gapped timestamps);" \
             "the master is constant frame rate and fills them."
    else
        EXPECTED_VIDEO_FRAMES=$(( SOURCE_VIDEO_FRAMES * FRAME_MULTIPLIER ))
    fi
    [[ "$OUTPUT_VIDEO_FRAMES" -eq "$EXPECTED_VIDEO_FRAMES" ]] || {
        echo "[ERROR] Master frame count differs from source: expected $EXPECTED_VIDEO_FRAMES, found $OUTPUT_VIDEO_FRAMES." >&2
        exit 1
    }
    echo "  > Frame-count check: $OUTPUT_VIDEO_FRAMES frames (expected $EXPECTED_VIDEO_FRAMES)."
fi

# The geometry the summary promised: the master carries the source's sample
# aspect ratio, so it displays at the same shape at any scale.
MASTER_SAR=""; MASTER_DAR=""
IFS=, read -r MASTER_SAR MASTER_DAR < <(ffprobe -v error -select_streams v:0 \
    -show_entries stream=sample_aspect_ratio,display_aspect_ratio \
    -of csv=p=0 "$PROG_OUTPUT" 2>/dev/null) || true
if [[ "$SAR_DECLARED" == true ]]; then
    [[ "$MASTER_SAR" == "$SOURCE_SAR" ]] || {
        echo "[ERROR] Master sample aspect ratio is ${MASTER_SAR:-unset}; the source's is ${SOURCE_SAR}." >&2
        exit 1
    }
    echo "  > Aspect check: SAR ${MASTER_SAR}, DAR ${MASTER_DAR} (matches source)."
else
    echo "  > Aspect check: source declared no SAR; master reports SAR ${MASTER_SAR:-unset}, DAR ${MASTER_DAR:-unset}."
fi

# Verify the requested CRF from x264's embedded settings. Prefer mediainfo's
# structured inspection; without it, bound the byte scan to 256 KB so large
# masters do not cause a full-file read just to inspect encoder metadata.
# In measured pipeline encodes the settings string starts at byte 1281,
# independent of clip duration. The 256 KB window is about 200 times that
# offset; an unreadable setting is reported rather than assumed correct.
if command -v mediainfo >/dev/null 2>&1; then
    ACTUAL_CRF=$(mediainfo --Full "$PROG_OUTPUT" 2>/dev/null \
        | grep -i -A1 "Encoding settings" \
        | grep -oP 'crf=\K[0-9]+(\.[0-9]+)?' | head -1)
else
    ACTUAL_CRF=$(head -c 256K "$PROG_OUTPUT" | grep -a -oP 'crf=\K[0-9]+(\.[0-9]+)?' | head -1)
fi
if [[ -z "$ACTUAL_CRF" ]]; then
    echo "⚠️  WARNING: Could not read embedded CRF from $PROG_OUTPUT (unexpected; encode" >&2
    echo "    succeeded, but the quality actually written could not be confirmed)." >&2
elif awk -v actual_crf="$ACTUAL_CRF" -v crf_value="$CRF_VALUE" 'BEGIN { exit (actual_crf > crf_value) ? 0 : 1 }'; then
    echo "" >&2
    echo "[ERROR] Master was encoded at CRF ${ACTUAL_CRF}, but CRF ${CRF_VALUE} was requested." >&2
    echo "        A higher CRF means LOWER quality than intended for this step." >&2
    echo "        This master would feed Real-ESRGAN (or become a deliverable) at the" >&2
    echo "        wrong quality. File: $PROG_OUTPUT" >&2
    exit 1
else
    echo "  > CRF check: $PROG_OUTPUT confirmed at CRF ${ACTUAL_CRF} (requested ${CRF_VALUE} or better)."
fi

python3 "$AUDIO_TIMING_TOOL" verify --plan "$AUDIO_TIMING_PLAN" \
    --output "$PROG_OUTPUT" --report "$LOG_DIR/audio_timing_verified.json"
audio_timing_report notices "$AUDIO_TIMING_PLAN" "  "

MASTER_IN_PROGRESS=""
echo -e "\n✅ Success! Master Created: $PROG_OUTPUT"

echo ""
echo "Finished: $(date)"
echo "Log: $LOG_FILE"

} # end main
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
