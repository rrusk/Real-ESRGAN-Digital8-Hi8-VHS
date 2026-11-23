#!/usr/bin/env bash
# Build the GPU environment described in INSTALL.md, in one command.
#
# Every step here is a step in INSTALL.md, in the same order. The document
# remains the explanation; this is the same procedure without the opportunity
# to paste a line wrong or leave one out. Skipping the basicsr patch in
# particular produces an installation that imports nothing, and is the single
# easiest step to miss by hand.
#
# Safe to re-run. An existing virtual environment is reused unless --force is
# given, already-downloaded weights are skipped, and the patch is idempotent.
# That makes a second checkout for testing cheap: clone, run this, done.
#
# System packages are NOT installed unless you ask. Missing ones are named and
# the run stops, because installing distribution packages is a decision about
# the machine rather than about this repository.
#
# Usage: dev_tools/setup_env.sh [OPTIONS]
#
# Options:
#   --system-deps    Install the required apt packages first (uses sudo).
#   --skip-models    Do not download model weights (~140MB).
#   --skip-smoke     Skip the end-of-run upscale test.
#   --venv DIR       Virtual environment directory (default: venv).
#   --python EXE     Python 3.10 or 3.11 interpreter (default: python3).
#   --force          Recreate the virtual environment from scratch. Refused
#                    unless DIR holds pyvenv.cfg.
#   --dry-run        Print the steps that would run, then exit.
#   -h, --help       Show this help and exit.
#   -V, --version    Show the version and exit.
#
# Exit status: 0 success, 1 a step failed, 2 command-line error.

set -euo pipefail

VERSION="1.0.0"

usage_error() { printf '[ERROR] %s\nTry %q --help for more information.\n' "$*" "$0" >&2; exit 2; }
usage() { sed -n '2,/^set -euo pipefail/{ /^set -euo pipefail/d; p; }' "$0" | sed 's/^# \{0,1\}//'; }
step()  { printf '\n=== %s ===\n' "$*"; }
note()  { printf '[INFO] %s\n' "$*"; }
die()   { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

APT_PACKAGES=(ffmpeg python3-venv python3-pip curl jq mediainfo gpac mkvtoolnix)
# Commands the pipeline needs at run time, and the package that supplies each.
declare -A REQUIRED_COMMANDS=(
    [ffmpeg]=ffmpeg [ffprobe]=ffmpeg [curl]=curl
    # mkvmerge concatenates a day's captures; MP4Box writes their chapters and
    # the SAR atom. jq reads and writes every run manifest and batch state file,
    # so the batch driver and the Hi8 wrapper both refuse to start without it.
    #
    # Keep this list matched to what the scripts check at startup: a mismatch
    # either blocks an install on a tool nothing needs, or lets one complete
    # without a tool the scripts refuse to start without.
    [jq]=jq [MP4Box]=gpac [mkvmerge]=mkvtoolnix
)
# Installed by --system-deps but not required: without mediainfo the CRF
# verification falls back to a bounded byte scan.
OPTIONAL_COMMANDS=(mediainfo)

# The pinned NumPy supports up to Python 3.11; the pipeline requires 3.10 syntax.
require_python() {  # require_python <interpreter>
    "$1" -c 'import sys; sys.exit(not (3, 10) <= sys.version_info[:2] < (3, 12))' 2>/dev/null \
        || die "Python 3.10 or 3.11 is required; $1 is $("$1" --version 2>&1 || echo missing)"
}

# --force deletes the --venv directory, so it must be a virtual environment
# (it holds pyvenv.cfg), and must not be /, the repository, the home directory,
# or a directory containing either of them.
check_removable_venv() {  # check_removable_venv <dir> <repository root>
    local target protected
    target=$(realpath -e -- "$1") || usage_error "cannot resolve --venv $1"
    [[ -f $target/pyvenv.cfg ]] \
        || usage_error "--force will not remove $1: it holds no pyvenv.cfg, so it is not a virtual environment"
    [[ $target != / ]] || usage_error "--force will not remove /"
    for protected in "$2" "${HOME:-}"; do
        [[ -n $protected ]] || continue
        protected=$(realpath -m -- "$protected")
        if [[ $protected == "$target" || $protected == "$target"/* ]]; then
            usage_error "--force will not remove $1: it is or contains $protected"
        fi
    done
}

main() {
    local venv_dir=venv python_cmd=python3 force=0 dry_run=0 skip_models=0 system_deps=0 skip_smoke=0

    while (($#)); do
        case $1 in
            --system-deps) system_deps=1; shift ;;
            --skip-models) skip_models=1; shift ;;
            --skip-smoke)  skip_smoke=1; shift ;;
            --venv) (($# >= 2)) || usage_error "--venv requires a directory"
                    venv_dir=$2; shift 2 ;;
            --python) (($# >= 2)) || usage_error "--python requires an interpreter"
                      python_cmd=$2; shift 2 ;;
            --force)   force=1; shift ;;
            --dry-run) dry_run=1; shift ;;
            -h|--help) usage; exit 0 ;;
            -V|--version) printf '%s %s\n' "${0##*/}" "$VERSION"; exit 0 ;;
            *) usage_error "unknown option: $1" ;;
        esac
    done

    local root
    root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
    cd -- "$root" || die "cannot enter the repository root"
    [[ -f requirements.txt && -f setup.py ]] \
        || die "this does not look like the repository root: $root"

    local python_bin="$venv_dir/bin/python"
    if (( force )) && [[ -e $venv_dir ]]; then
        check_removable_venv "$venv_dir" "$root"
    fi

    if (( dry_run )); then
        printf 'Would run, in %s:\n' "$root"
        printf '  1. check system commands: %s, and Python 3.10 or 3.11 (%s)\n' "${!REQUIRED_COMMANDS[*]}" "$python_cmd"
        (( system_deps )) && printf '     and apt install: %s\n' "${APT_PACKAGES[*]}"
        if [[ -d $venv_dir ]] && (( ! force )); then
            printf '  2. reuse existing virtual environment %s\n' "$venv_dir"
        else
            printf '  2. create virtual environment %s%s\n' "$venv_dir" \
                   "$( (( force )) && [[ -e $venv_dir ]] && printf ' (removing the existing one)')"
        fi
        printf '  3. install the pinned CUDA stack, then build BasicSR against it:\n'
        printf '     pip install -c requirements.txt numpy cython torch torchvision torchaudio\n'
        printf '     pip install --no-build-isolation --no-deps -c requirements.txt basicsr\n'
        printf '     pip install -r requirements.txt\n'
        printf '  4. python dev_tools/patch_basicsr.py        (required)\n'
        printf '  5. pip install -e . --no-deps\n'
        (( skip_models )) && printf '  6. skip model download\n' \
                          || printf '  6. dev_tools/fetch_models.sh               (~140MB)\n'
        printf '  7. python verify_env.py\n'
        if (( skip_smoke || skip_models )); then
            printf '  8. skip the upscale smoke test\n'
        else
            printf '  8. dev_tools/smoke_test.sh                 (real 2x upscale on a generated clip)\n'
        fi
        exit 0
    fi

    # --- 1. system commands -------------------------------------------------
    step "Checking system commands"
    if (( system_deps )); then
        note "Installing apt packages: ${APT_PACKAGES[*]}"
        sudo apt install -y "${APT_PACKAGES[@]}" || die "apt install failed"
    fi
    local command_name missing=()
    for command_name in "${!REQUIRED_COMMANDS[@]}"; do
        command -v "$command_name" >/dev/null 2>&1 \
            || missing+=("$command_name (${REQUIRED_COMMANDS[$command_name]})")
    done
    if ((${#missing[@]})); then
        printf '[ERROR] Missing required command(s):\n' >&2
        printf '          %s\n' "${missing[@]}" >&2
        printf '        Install them, or re-run with --system-deps.\n' >&2
        exit 1
    fi
    note "All required commands present"
    for command_name in "${OPTIONAL_COMMANDS[@]}"; do
        command -v "$command_name" >/dev/null 2>&1 \
            || note "Optional command $command_name not found; CRF checks use the slower fallback"
    done

    # Checked before the virtual environment is touched, so --force on a host
    # whose selected interpreter is unsupported leaves the environment in place.
    if (( force )) || [[ ! -x $python_bin ]]; then
        require_python "$python_cmd"
    else
        require_python "$python_bin"
    fi

    if ! command -v nvidia-smi >/dev/null 2>&1; then
        note "nvidia-smi not found: this machine has no usable NVIDIA driver."
        note "Installation continues, but the AI path will not run here."
    else
        nvidia-smi --query-gpu=name,driver_version --format=csv,noheader \
            | sed 's/^/[INFO] GPU: /'
    fi

    # --- 2. virtual environment --------------------------------------------
    step "Python virtual environment"
    if (( force )) && [[ -e $venv_dir ]]; then
        note "Removing existing $venv_dir (--force)"
        rm -rf -- "$venv_dir"
    fi
    if [[ -x $python_bin ]]; then
        note "Reusing $venv_dir ($("$python_bin" --version 2>&1))"
    else
        "$python_cmd" -m venv -- "$venv_dir" || die "could not create $venv_dir"
        note "Created $venv_dir ($("$python_bin" --version 2>&1))"
    fi

    # --- 3-5. python packages ----------------------------------------------
    step "Installing Python packages (this is the long step)"
    "$python_bin" -m pip install --quiet --upgrade pip setuptools wheel \
        || die "could not upgrade pip/setuptools/wheel"
    # Constraints select the pinned CUDA stack without starting a BasicSR build.
    # Its build requirements must already exist before disabling isolation.
    "$python_bin" -m pip install -c requirements.txt numpy cython torch torchvision torchaudio \
        || die "could not install the pinned CUDA stack and BasicSR build requirements"
    "$python_bin" -m pip install --no-build-isolation --no-deps -c requirements.txt basicsr \
        || die "could not build BasicSR against the installed stack"
    "$python_bin" -m pip install -r requirements.txt \
        || die "pip install -r requirements.txt failed"

    step "Patching BasicSR for torchvision >= 0.17"
    # Not optional: basicsr 1.4.2 imports a torchvision module removed in 0.17,
    # and basicsr/data/__init__.py imports every dataset module, so an unpatched
    # install fails on `import basicsr` alone. See INSTALL.md.
    "$python_bin" dev_tools/patch_basicsr.py || die "could not patch basicsr"

    step "Installing this package"
    "$python_bin" -m pip install --quiet -e . --no-deps \
        || die "editable install failed"

    # pytest is a development dependency needed to run the bundled tests.
    "$python_bin" -m pip install --quiet pytest \
        || die "could not install pytest (needed to run the bundled tests)"

    # --- 6. weights ---------------------------------------------------------
    if (( skip_models )); then
        step "Skipping model download (--skip-models)"
    else
        step "Downloading model weights"
        ./dev_tools/fetch_models.sh || die "model download failed"
    fi

    # --- 7. verify ----------------------------------------------------------
    step "Verifying the environment"
    if ! "$python_bin" verify_env.py; then
        printf '\n[ERROR] Verification failed. See the messages above and INSTALL.md.\n' >&2
        exit 1
    fi

    # Verification says the pieces load; this says the pipeline runs. Skipped
    # without weights, since it cannot mean anything then.
    if (( skip_smoke )); then
        step "Skipping the upscale smoke test (--skip-smoke)"
    elif (( skip_models )); then
        step "Skipping the upscale smoke test (no weights were downloaded)"
    else
        step "Smoke test: upscaling a generated clip"
        ./dev_tools/smoke_test.sh --venv "$venv_dir" \
            || die "the environment installed but could not upscale; see the output above"
    fi

    printf '\nDone. Activate the environment with:\n'
    printf '    source %s/bin/activate\n' "$venv_dir"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
