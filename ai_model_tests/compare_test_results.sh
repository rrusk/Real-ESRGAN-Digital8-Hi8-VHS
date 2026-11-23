#!/usr/bin/env bash
# Build a four-panel grid: source, two AI models, and a Lanczos baseline.

set -euo pipefail

# Command-line errors are exit 2 and point at --help rather than reprinting
# it, per dev_tools/SCRIPT_CONVENTIONS.md.
usage_error() { printf '[ERROR] %s\nTry %q --help for more information.\n' "$*" "$0" >&2; exit 2; }

main() {

usage() {
    cat <<'EOF'
Usage: compare_test_results.sh [OPTIONS] SOURCE MODEL_A MODEL_B

Options:
  --label-a TEXT   Label for MODEL_A (default: Model A)
  --label-b TEXT   Label for MODEL_B (default: Model B)
  -o, --output F   Output file (default: beside the model outputs)
  -h, --help       Show this help
EOF
}

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    usage
    exit 0
fi
LABEL_A="Model A"
LABEL_B="Model B"
OUTPUT=""

# Options may appear before, after, or among the three inputs. Consuming the
# first three arguments as positionals first would reject
# `--label-a X source a b`, which reads perfectly naturally and is the order
# every other script in this repository accepts.
positional=()
while (( $# > 0 )); do
    case "$1" in
        --label-a|--label-b|-o|--output)
            [[ -n "${2:-}" ]] || usage_error "$1 requires a value"
            case "$1" in
                --label-a) LABEL_A=$2 ;;
                --label-b) LABEL_B=$2 ;;
                -o|--output) OUTPUT=$2 ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --) shift; while (( $# > 0 )); do positional+=("$1"); shift; done ;;
        -*) usage_error "Unknown option: $1" ;;
        *)  positional+=("$1"); shift ;;
    esac
done

(( ${#positional[@]} == 3 )) \
    || usage_error "expected SOURCE MODEL_A MODEL_B, got ${#positional[@]} argument(s)"
SOURCE=${positional[0]}
MODEL_A=${positional[1]}
MODEL_B=${positional[2]}

for command in ffmpeg ffprobe; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "[ERROR] Required command not found: $command" >&2
        exit 2
    }
done
for file in "$SOURCE" "$MODEL_A" "$MODEL_B"; do
    [[ -f $file ]] || { echo "[ERROR] File not found: $file" >&2; exit 2; }
done

IFS=, read -r panel_w panel_h < <(ffprobe -v error -select_streams v:0 \
    -show_entries stream=width,height -of csv=p=0 "$MODEL_A")
IFS=, read -r b_w b_h < <(ffprobe -v error -select_streams v:0 \
    -show_entries stream=width,height -of csv=p=0 "$MODEL_B")
[[ $panel_w == "$b_w" && $panel_h == "$b_h" ]] || {
    echo "[ERROR] Model outputs differ in size: ${panel_w}x${panel_h} vs ${b_w}x${b_h}" >&2
    exit 1
}

source_sar=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=sample_aspect_ratio -of default=nw=1:nk=1 "$SOURCE")
if [[ -z $source_sar || $source_sar == N/A || $source_sar == 0:1 ]]; then
    source_sar=1:1
fi
filter_sar=${source_sar/:/\/}

if [[ -z $OUTPUT ]]; then
    # Beside the model outputs, not beside the source clip: the grid is part of
    # the comparison's result and belongs in the run's videos/ directory with
    # the outputs it was built from. See pipeline_docs/storage-layout.md.
    source_stem=$(basename "$SOURCE")
    OUTPUT="$(dirname "$MODEL_A")/${source_stem%.*}_model_comparison.mkv"
fi
[[ ! -e $OUTPUT ]] || { echo "[ERROR] Output already exists: $OUTPUT" >&2; exit 1; }

# Escape text characters that have meaning in FFmpeg drawtext.
escape_drawtext() {
    printf '%s' "$1" | sed "s/\\\\/\\\\\\\\/g; s/:/\\\\:/g; s/'/\\\\'/g"
}
label_a=$(escape_drawtext "$LABEL_A")
label_b=$(escape_drawtext "$LABEL_B")

ffmpeg -hide_banner -loglevel warning -stats \
    -i "$SOURCE" -i "$MODEL_A" -i "$MODEL_B" \
    -filter_complex "
        [0:v]split=2[src_in][base_in];
        [src_in]scale=${panel_w}:${panel_h}:flags=neighbor,drawtext=text='Source (nearest)':x=20:y=20:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.6[src];
        [1:v]drawtext=text='${label_a}':x=20:y=20:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.6[a];
        [2:v]drawtext=text='${label_b}':x=20:y=20:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.6[b];
        [base_in]scale=${panel_w}:${panel_h}:flags=lanczos,drawtext=text='Lanczos baseline':x=20:y=20:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.6[base];
        [src][a]hstack[top]; [base][b]hstack[bottom]; [top][bottom]vstack,setsar=${filter_sar}[v]
    " \
    -map '[v]' -map '0:a?' -c:v libx264 -crf 18 -preset slow -pix_fmt yuv420p \
    -c:a copy -shortest "$OUTPUT"

echo "Created comparison grid: $OUTPUT"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
