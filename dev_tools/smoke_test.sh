#!/usr/bin/env bash
# Prove the installed environment can actually upscale, not merely import.
#
# verify_env.py checks that the pieces load. This runs video_upscale_pipeline.py
# itself on a synthetic clip -- pre-filter, Real-ESRGAN inference, timing checks
# and audio mux -- because that is what "the pipeline runs" actually means.
#
# The clip includes audio to exercise extraction, priming checks and muxing.
# The preservation root and run directory are private temporary paths, keeping
# the test's current_run pointer separate from production runs.
#
# The input is generated, so nothing is downloaded and no sample media is
# committed. It is deliberately tiny: a few frames at 64x48, which is seconds
# of GPU time, because the question is whether the path works at all.
#
# Usage: dev_tools/smoke_test.sh [OPTIONS]
#
# Options:
#   --venv DIR     Virtual environment to use (default: venv).
#   --model NAME   Model to exercise (default: realesr-general-x4v3).
#   --scale N      Upscale factor to request (default: 2).
#   --keep         Keep the working directory for inspection.
#   -h, --help     Show this help and exit.
#   -V, --version  Show the version and exit.
#
# Exit status: 0 the pipeline upscaled the clip, 1 it did not, 2 usage error.

set -euo pipefail

VERSION="1.0.0"

usage_error() { printf '[ERROR] %s\nTry %q --help for more information.\n' "$*" "$0" >&2; exit 2; }
usage() { sed -n '2,/^set -euo pipefail/{ /^set -euo pipefail/d; p; }' "$0" | sed 's/^# \{0,1\}//'; }

main() {
    local venv_dir=venv model=realesr-general-x4v3 keep=0 scale=2

    while (($#)); do
        case $1 in
            --venv)  (($# >= 2)) || usage_error "--venv requires a directory"; venv_dir=$2; shift 2 ;;
            --model) (($# >= 2)) || usage_error "--model requires a name";     model=$2;    shift 2 ;;
            --scale) (($# >= 2)) || usage_error "--scale requires a number";    scale=$2;    shift 2 ;;
            --keep)  keep=1; shift ;;
            -h|--help) usage; exit 0 ;;
            -V|--version) printf '%s %s\n' "${0##*/}" "$VERSION"; exit 0 ;;
            *) usage_error "unknown option: $1" ;;
        esac
    done

    local root; root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
    cd -- "$root"
    local python_bin="$venv_dir/bin/python"
    [[ -x $python_bin ]] || { printf '[ERROR] No environment at %s. Run dev_tools/setup_env.sh first.\n' "$venv_dir" >&2; exit 1; }
    [[ -f "weights/$model.pth" ]] || { printf '[ERROR] Missing weights/%s.pth. Run dev_tools/fetch_models.sh.\n' "$model" >&2; exit 1; }

    local work; work=$(mktemp -d "${TMPDIR:-/tmp}/realesrgan-smoke.XXXXXX")
    if (( keep )); then
        printf '[INFO] Working directory kept: %s\n' "$work"
    else
        # shellcheck disable=SC2064  # expand now: $work must be captured here
        trap "rm -rf -- '$work'" EXIT
    fi

    printf '[1/3] Generating a synthetic clip (with audio)\n'
    ffmpeg -v error -y -f lavfi -i testsrc=size=64x48:rate=10 \
        -f lavfi -i sine=frequency=440:sample_rate=48000 -t 0.5 \
        -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$work/in.mp4" \
        || { printf '[ERROR] could not generate the test clip\n' >&2; exit 1; }

    printf '[2/3] Running video_upscale_pipeline.py at %sx with %s\n' "$scale" "$model"
    # Isolate both the deliverable and the shared current_run pointer.
    if ! VIDEO_PRESERVATION_ROOT="$work/preservation" \
            "$python_bin" video_upscale_pipeline.py "$work/in.mp4" \
            -s "$scale" --model "$model" --force \
            --run-dir "$work/run" \
            </dev/null >"$work/log" 2>&1; then
        printf '[ERROR] The pipeline failed. Last lines:\n' >&2
        tail -20 "$work/log" >&2
        exit 1
    fi

    printf '[3/3] Checking the result\n'
    local produced; produced=$(find "$work/run/videos" -type f -name '*.mp4' -print -quit 2>/dev/null || true)
    [[ -n $produced && -s $produced ]] \
        || { printf '[ERROR] No output video was produced.\n' >&2; tail -20 "$work/log" >&2; exit 1; }

    # 64x48 at -s 2 must come back 128x96. A file that exists but was not
    # upscaled would otherwise pass as success.
    local expect_w=$(( 64 * scale )) expect_h=$(( 48 * scale )) dims
    dims=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
           -of csv=p=0:s=x "$produced")
    [[ $dims == "${expect_w}x${expect_h}" ]] \
        || { printf '[ERROR] Expected %sx%s, got %s\n' "$expect_w" "$expect_h" "$dims" >&2; exit 1; }

    # The mux stage is where a video-only input fails, so confirm audio survived.
    local acodec
    acodec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name \
             -of default=nw=1:nk=1 "$produced" 2>/dev/null || true)
    [[ -n $acodec ]] \
        || { printf '[ERROR] The output has no audio stream; the mux stage did not run.\n' >&2; exit 1; }

    printf '\n[OK] Pipeline upscaled 64x48 -> %s (audio: %s) using %s.\n' \
        "$dims" "$acodec" "$model"
    printf '     video_upscale_pipeline.py runs end to end in this environment.\n'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
