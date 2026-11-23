#!/usr/bin/env bash
# EXAMPLE: choose prepare_video.sh's bottom mask from a capture's filename.
#
# This is one collection's naming conventions, not a general tool. They live in
# capture_names_lib.sh beside this script, which enhance_tape.sh reads too, and
# which says why a filename and not a probe decides the mask. Edit the table
# there for your own names; nothing here needs to change.

set -euo pipefail

VERSION="1.0.0"

usage_error() {
    printf '[ERROR] %s\n' "$*" >&2
    printf "Try '%s --help' for more information.\n" "${0##*/}" >&2
    exit 2
}

usage() {
    cat <<'EOF'
Usage:
  prepare_tape_capture.sh [OPTIONS] SOURCE [-- PREPARE_ARGS...]

Description:
  Call prepare_video.sh with the bottom mask this example's naming
  conventions imply, and report the pipeline profile the capture will need
  afterwards.

Arguments:
  SOURCE                Capture to prepare.
  PREPARE_ARGS          Anything after -- is passed to prepare_video.sh
                        unchanged. An explicit --mask there wins.

Options:
  --prepare PATH        prepare_video.sh to call
                        (default: the copy in the repository containing this
                        script, or $ESRGAN_DIR/prepare_video.sh when set).
  --dry-run             Print the command that would run; run nothing.
  -h, --help            Show this help and exit.
  -V, --version         Show the version and exit.

EOF
    capture_names_help
    cat <<'EOF'

To prepare and upscale in one command, use enhance_tape.sh instead.

Examples:
  prepare_tape_capture.sh hi8_capture01.dv
  prepare_tape_capture.sh dv.20200115-120000.avi -- --pcm --yes
  prepare_tape_capture.sh --dry-run vhs_capture01.dv

Dependencies:
  prepare_video.sh, capture_names_lib.sh (beside this script)
EOF
}

main() {
    local prepare="" dry_run=false source=""
    local -a passthrough=()
    local names
    names="$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")/capture_names_lib.sh"
    [[ -f $names ]] || { printf '[ERROR] Missing: %s\n' "$names" >&2; exit 1; }
    # shellcheck source=capture_names_lib.sh
    source "$names"

    while (($#)); do
        case $1 in
            -h|--help)    usage; exit 0 ;;
            -V|--version) printf '%s %s\n' "${0##*/}" "$VERSION"; exit 0 ;;
            --prepare)    (($# >= 2)) || usage_error "--prepare requires a path"
                          prepare=$2; shift 2 ;;
            --dry-run)    dry_run=true; shift ;;
            --)           shift; passthrough=("$@"); break ;;
            -*)           usage_error "unknown option: $1" ;;
            *)            [[ -z $source ]] || usage_error "unexpected extra argument: $1"
                          source=$1; shift ;;
        esac
    done

    [[ -n $source ]] || usage_error "a source file is required"
    [[ -f $source ]] || { printf '[ERROR] Source file not found: %s\n' "$source" >&2; exit 1; }

    if [[ -z $prepare ]]; then
        if [[ -n ${ESRGAN_DIR:-} && -x $ESRGAN_DIR/prepare_video.sh ]]; then
            prepare="$ESRGAN_DIR/prepare_video.sh"
        else
            # Resolved from this script's own location so a checkout works
            # wherever it sits, rather than from a hardcoded home directory.
            prepare="$(cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")/.." && pwd)/prepare_video.sh"
        fi
    fi
    [[ -f $prepare ]] || {
        printf '[ERROR] prepare_video.sh not found: %s\n' "$prepare" >&2
        printf '        Set ESRGAN_DIR or pass --prepare PATH.\n' >&2
        exit 1
    }

    local base settings mask profile why
    base=$(basename -- "$source")
    settings=$(capture_settings "$base") || { capture_name_refused "$base"; exit 1; }
    IFS=$'\t' read -r mask profile why <<<"$settings"

    local -a command=(bash "$prepare" "$source" --mask "$mask")
    ((${#passthrough[@]})) && command+=("${passthrough[@]}")

    printf 'Capture:  %s\n' "$base"
    printf 'Mask:     %s pixels  (%s)\n' "$mask" "$why"
    printf 'Next:     video_upscale_pipeline.py <master> --profile %s\n\n' "$profile"

    if $dry_run; then
        printf 'Would run:'; printf ' %q' "${command[@]}"; printf '\n'
        return 0
    fi
    "${command[@]}"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
