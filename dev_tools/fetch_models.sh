#!/usr/bin/env bash
# Download the Real-ESRGAN model weights this pipeline uses.
#
# The weights are upstream's released binaries, not this repository's work, so
# they are downloaded rather than committed: they total roughly 140MB, they are
# byte-identical for everyone, and Git would carry every future copy of them
# forever. weights/ is ignored for the same reason.
#
# Re-running skips files already present, so it is safe after a partial
# download. Use --force to fetch again over an existing file.
#
# Usage: dev_tools/fetch_models.sh [OPTIONS]
#   --force        Re-download even if the file is already present.
#   --dest DIR     Destination directory (default: weights/ beside this repo).
#   -h, --help     Show this help and exit.

set -euo pipefail

usage_error() { printf '[ERROR] %s\nTry %q --help for more information.\n' "$*" "$0" >&2; exit 2; }
usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
    local force=0 dest
    dest=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/weights

    while (($#)); do
        case $1 in
            --force) force=1; shift ;;
            --dest) (($# >= 2)) || usage_error "--dest requires a directory"
                    dest=$2; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) usage_error "unknown option: $1" ;;
        esac
    done

    command -v curl >/dev/null 2>&1 || usage_error "curl is required but not installed"
    mkdir -p -- "$dest"

    # Release tags differ per model; they are upstream's, not arbitrary.
    local -a models=(
        "v0.2.1/RealESRGAN_x2plus.pth"
        "v0.1.0/RealESRGAN_x4plus.pth"
        "v0.2.5.0/realesr-general-x4v3.pth"
        "v0.2.5.0/realesr-general-wdn-x4v3.pth"
    )

    local base='https://github.com/xinntao/Real-ESRGAN/releases/download'
    local entry name url rc=0
    for entry in "${models[@]}"; do
        name=${entry##*/}
        url="$base/$entry"
        if [[ -s "$dest/$name" && $force -eq 0 ]]; then
            printf '[SKIP] %s already present\n' "$name"
            continue
        fi
        printf '[GET ] %s\n' "$name"
        # Download beside the target and move into place, so an interrupted
        # transfer never leaves a truncated file that the skip check accepts.
        if curl -fL --retry 3 --progress-bar -o "$dest/.$name.part" "$url"; then
            mv -f -- "$dest/.$name.part" "$dest/$name"
        else
            rm -f -- "$dest/.$name.part"
            printf '[FAIL] could not download %s\n' "$url" >&2
            rc=1
        fi
    done

    (( rc == 0 )) || { printf '[ERROR] one or more downloads failed\n' >&2; return 1; }
    printf '\nWeights are in %s\n' "$dest"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
