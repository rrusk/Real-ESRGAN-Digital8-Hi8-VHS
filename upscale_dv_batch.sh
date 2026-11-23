#!/bin/bash
# ==============================================================================
# Script Name: upscale_dv_batch.sh
# Description: Batch upscaling of Digital8 DV captures, grouped into recording
#              days from their resolved timestamps. Each .avi goes through
#              prepare_video.sh then video_upscale_pipeline.py, and each day's
#              outputs are assembled into one MP4 with a chapter per capture.
#
# INPUT_ROOT is searched recursively for dv.*.avi. By default a capture's own
# recording time decides which day it belongs to, so any directory layout works,
# flat included. With --group-by directory the enclosing YYYYMMDD[_Description]
# directory decides instead, which keeps an archive already sorted by hand
# sorted the same way. Recording times are still
# resolved either way, because chapter labels and within-day ordering come from
# them; under directory grouping, a capture whose tape disagrees with the
# directory holding it is reported.
#
# Everything the batch writes goes into one run directory, described by
# pipeline_docs/storage-layout.md. Re-running with the same settings resumes
# that run: completed and failed files are skipped, assembled days are skipped,
# and an interrupted file is retried from the start -- the pipeline handles
# chunk-level resume within a file itself.
#
# NOTE: The entire script is wrapped in main() so bash reads the complete file
# into memory before execution begins, preventing mid-run file replacement
# from affecting an in-progress run.
#
# Run with --help for options.
# ==============================================================================
set -euo pipefail
# Python started from here writes its output as it goes, even into a pipe
# such as `| tee`, where it would otherwise hold 8 KB at a time.
export PYTHONUNBUFFERED=1

# A duration or rate ffprobe cannot read is an ABSENCE, not a zero. ffprobe
# exits 0 while printing "N/A" for a stream it cannot measure, so a -z test
# lets that through -- and once values are passed with `awk -v` rather than
# interpolated, "N/A" is a strnum worth 0. Two unreadable durations then agree
# with each other and a comparison against a tolerance PASSES, having compared
# nothing. Interpolated, the same input made the awk program malformed and the
# check failed loudly, so this guard is what keeps that behaviour.
is_seconds() { [[ $1 =~ ^-?[0-9]+([.][0-9]+)?$ ]]; }


# Command-line errors are exit 2 and point at --help rather than reprinting
# it, per dev_tools/SCRIPT_CONVENTIONS.md.
usage_error() { printf '[ERROR] %s\nTry %q --help for more information.\n' "$*" "$0" >&2; exit 2; }

main() {

# Preserve the invocation before option parsing shifts it away. The manifest's
# command is otherwise just the script name, which is not enough to reproduce
# either a plan or a real run.
local -a ORIGINAL_ARGS=("$@")

# ------------------------------------------------------------------------------
# 1. Argument Parsing
# ------------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Search --input-root recursively for native DV captures and assemble one video
per recording day. Options default below VIDEO_PRESERVATION_ROOT (or
~/VideoPreservation); set --input-root to the directory holding your captures.

Options:
  --input-root DIR    Root searched recursively for dv.*.avi captures. Files
                      may use a flat or arbitrary directory layout; recording
                      timestamps determine their day groups unless --group-by
                      directory says the dated directories do.
                      (default: ~/VideoPreservation/sources/dv/digital8-native)
  --run-dir DIR       The run directory this batch writes everything into:
                      day videos, logs, resume state, and working files.
                      By default the batch continues the most recent
                      unfinished run with matching settings under
                      ~/VideoPreservation/ai/, or creates a new one when there
                      is none, so a stopped batch resumes into the directory it
                      started. See pipeline_docs/storage-layout.md.
                      Cannot be combined with --dry-run; plans always get a
                      fresh temporary directory.
  --run-id NAME       Name the run directory explicitly instead of deriving
                      it from the time. Implies the run under ~/VideoPreservation/ai/.
  --esrgan-dir DIR    Path to the Real-ESRGAN directory.
                      (default: directory containing this script)
  --day YYYYMMDD      Process only this day. May be repeated. Matches the
                      resolved recording day, or under --group-by directory the
                      date prefix of the day directory's name.
  --group-by WHAT       How captures are divided into days:
                          recording-time  each capture's own resolved recording
                                          time (default). Any directory layout
                                          works, including a flat one.
                          directory       the enclosing YYYYMMDD[_Description]
                                          directory, for an archive already
                                          sorted that way. The day video keeps
                                          that directory's descriptive name. Every
                                          capture must sit under such a
                                          directory or the batch stops and says
                                          which ones do not; a capture whose DV
                                          subcode disagrees with its directory
                                          is reported as a warning.
  --time-source WHAT    Which recording time should define the day plan:
                          auto     prefer a usable filename and cross-check it
                                   against DV subcode (default)
                          subcode  use the camcorder's DV subcode; a missing or
                                   conflicting subcode is an anomaly
  --on-anomaly WHAT     What to do when a capture's filename and the DV
                        subcode recorded on the tape disagree about when it was
                        recorded, or when neither gives a time. Checked during
                        pre-flight, before any upscaling starts.
                          ask           report and prompt (default)
                          use-filename  trust the filename
                          prefer-subcode  prefer the tape. A capture whose
                                          first frame lacks subcode but whose
                                          filename supplies the start keeps the
                                          filename: a value recovered from a
                                          later frame dates the recording but
                                          not the capture start.
                          skip          leave those captures out
                          abort         stop without processing anything
                        With no terminal available and no explicit value, the
                        batch stops during pre-flight rather than guessing.
  --scale N             ESRGAN upscale factor: 2 or 4 (default: 2).
  --crf CRF             Pass --crf through to video_upscale_pipeline.py
                        to use a different quality for the final deliverable.
                        Integer 10-30; the pipeline's own default is 23 when
                        this is not given. See video_upscale_pipeline.py --help.
  --retry-failed        Back up the state file, clear failed file/day records,
                        and retry them while resuming all successful work.
  --dry-run             Print what would be done and save the plan in
                        /tmp/upscale-dv-plan-<group-by>-<time-source>, replacing
                        the previous plan made with those two settings, without
                        processing media.
  --help, -h            Show this help.

Examples:
  # Process captures from your input directory (default CRF 23):
  $0 --input-root /path/to/captures

  # Test on one day only:
  $0 --input-root /path/to/captures --day 20200115

  # Reprocess one day at a different quality:
  $0 --input-root /path/to/captures --day 20200115 --crf 18

  # Dry run to preview the full file list:
  $0 --input-root /path/to/captures --dry-run

  # Preview the plan made strictly from camcorder DV subcode:
  $0 --input-root /path/to/captures --dry-run --time-source subcode --on-anomaly prefer-subcode

  # Preview the plan for an archive already sorted into YYYYMMDD[_Description]
  # directories, keeping those directories as the day boundaries:
  $0 --input-root /path/to/captures --dry-run --group-by directory

  # Resume after fixing the cause of a previous failure:
  $0 --input-root /path/to/captures --retry-failed
EOF
}

PRESERVATION_ROOT="${VIDEO_PRESERVATION_ROOT:-${HOME}/VideoPreservation}"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INPUT_ROOT="${PRESERVATION_ROOT}/sources/dv/digital8-native"
RUN_DIR=""      # --run-dir; empty means adopt an unfinished run or create one
RUN_ID=""       # --run-id; names a run under the AI run root
ESRGAN_DIR="$SCRIPT_DIR"
SCALE=2
DRY_RUN=false
RETRY_FAILED=false
# KEEP IN SYNC with DEFAULT_CRF in video_upscale_pipeline.py --
# if one changes, change the other too. There is no single source of truth
# shared between the two scripts; this constant exists here purely to
# normalise the batch configuration signature recorded in STATE_FILE (see
# state_check_config) when CRF_ARG is the empty "not passed" sentinel.
# video_upscale_pipeline.py resolves its own default independently -- this
# value is never actually passed through to it.
DEFAULT_CRF=23
# CRF_ARG holds the empty string when --crf was not passed
# explicitly (video_upscale_pipeline.py's own default CRF applies -- 23, the
# current default mode), or a specific CRF value to pass through to its
# --crf otherwise. A CRF of 0 is never valid for libx264 in this
# pipeline (see PREFILTER_CRF/ESRGAN_WRITER_CRF in video_upscale_pipeline.py,
# all > 0), so the empty string is unambiguous as the "flag not passed"
# sentinel.
CRF_ARG=""
# DAY_FILTER holds zero or more recording dates to process.
DAY_FILTER=()
# What to do when a capture's filename and its DV subcode disagree, or when
# neither yields a recording time. "ask" prompts; with no terminal and no
# explicit value the batch stops during pre-flight rather than guessing.
ON_ANOMALY=ask
# "auto" preserves the capture filename as the usual label when its timestamp
# agrees with the tape. "subcode" makes the camcorder timestamp authoritative,
# including when the filename also carries a plausible time.
TIME_SOURCE=auto
# What decides which day a capture belongs to. "recording-time" uses the time
# resolved for the capture itself, which needs nothing of the directory layout.
# "directory" uses the enclosing YYYYMMDD[_Description] directory, which is what
# this batch did before recording times were resolved at all: an archive already
# sorted by hand keeps the sorting, and the descriptive part of the directory
# name survives into the day video's name. Recording times are still resolved
# under both, because chapter labels and within-day ordering come from them.
GROUP_BY=recording-time

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input-root)  INPUT_ROOT="$2";  shift 2 ;;
        --run-dir)     RUN_DIR="$2";     shift 2 ;;
        --run-id)      RUN_ID="$2";      shift 2 ;;
        --esrgan-dir)  ESRGAN_DIR="$2";  shift 2 ;;
        --scale)       SCALE="$2";       shift 2 ;;
        --crf)
            # CRF is a required numeric argument; omitting it is a command-line error.
            [[ -n "${2:-}" ]] || usage_error "--crf requires a CRF"
            CRF_ARG="$2"; shift 2 ;;
        --day)
            [[ -n "${2:-}" ]] || usage_error "--day requires YYYYMMDD"
            DAY_FILTER+=("$2"); shift 2 ;;
        --group-by)
            [[ -n "${2:-}" ]] || usage_error "--group-by requires recording-time or directory"
            case "$2" in
                recording-time|directory) GROUP_BY="$2" ;;
                *) usage_error "--group-by must be recording-time or directory" ;;
            esac
            shift 2 ;;
        --time-source)
            [[ -n "${2:-}" ]] || usage_error "--time-source requires auto or subcode"
            case "$2" in
                auto|subcode) TIME_SOURCE="$2" ;;
                *) usage_error "--time-source must be auto or subcode" ;;
            esac
            shift 2 ;;
        --on-anomaly)
            case "$2" in
                ask|use-filename|prefer-subcode|skip|abort) ON_ANOMALY="$2" ;;
                use-probe) usage_error "unsupported anomaly policy use-probe; use prefer-subcode" ;;
                *) usage_error "--on-anomaly must be ask, use-filename, prefer-subcode, skip or abort" ;;
            esac
            shift 2 ;;
        --retry-failed) RETRY_FAILED=true; shift ;;
        --dry-run)     DRY_RUN=true;     shift ;;
        --help|-h)     usage; exit 0 ;;
        *) usage_error "Unknown argument: '$1'" ;;
    esac
done

if [[ "$SCALE" != "2" && "$SCALE" != "4" ]]; then
    echo "Error: --scale must be 2 or 4 (got: '$SCALE')." >&2
    exit 1
fi

for day in "${DAY_FILTER[@]}"; do
    [[ "$day" =~ ^[0-9]{8}$ ]] \
        || usage_error "--day must be exactly YYYYMMDD (got: '$day')"
done

if [[ "$RETRY_FAILED" == true && "$DRY_RUN" == true ]]; then
    echo "Error: --retry-failed and --dry-run cannot be combined." >&2
    echo "       --retry-failed changes state after creating a backup." >&2
    exit 1
fi

if [[ $DRY_RUN == true && ( -n $RUN_DIR || -n $RUN_ID ) ]]; then
    usage_error "--dry-run cannot be combined with --run-dir or --run-id; its plan is written under /tmp"
fi

if [[ -n "$CRF_ARG" ]]; then
    # Sanity bound only -- video_upscale_pipeline.py and libx264 are the
    # actual authorities on a valid CRF; this just catches an obvious typo
    # (e.g. a stray day-filter value swallowed as the CRF) before a multi-day
    # batch run starts. libx264's own range is 0-51; the lower end of that is
    # never sensible for a deliverable (0 is lossless, larger than the
    # master this is meant to shrink), so the floor here is
    # tighter than libx264's to keep the most likely typos in the visible-loss
    # comparison range of 10-30, while still leaving headroom for a deliberate
    # decision to be more or less aggressive after the visual side-by-side.
    if [[ ! "$CRF_ARG" =~ ^[0-9]+$ ]] || (( CRF_ARG < 10 || CRF_ARG > 30 )); then
        echo "Error: --crf must be an integer 10-30 (got: '$CRF_ARG')." >&2
        exit 1
    fi
fi

# ------------------------------------------------------------------------------
# 2. Path Setup and Tool Validation
# ------------------------------------------------------------------------------

# GNU realpath accepts a missing final path component, so validate the source
# before resolving it. Otherwise a missing common-root symlink looks exactly
# like a valid but empty archive and the batch exits successfully without
# processing anything.
if [[ -L "$INPUT_ROOT" && ! -e "$INPUT_ROOT" ]]; then
    echo "Error: input root is a broken symbolic link: $INPUT_ROOT" >&2
    echo "       Link target: $(readlink -- "$INPUT_ROOT")" >&2
    exit 1
fi
if [[ ! -e "$INPUT_ROOT" ]]; then
    echo "Error: input root does not exist: $INPUT_ROOT" >&2
    echo "       Create it, create the expected source symlink, or use --input-root DIR." >&2
    exit 1
fi
if [[ ! -d "$INPUT_ROOT" ]]; then
    echo "Error: input root is not a directory: $INPUT_ROOT" >&2
    exit 1
fi
if [[ ! -r "$INPUT_ROOT" || ! -x "$INPUT_ROOT" ]]; then
    echo "Error: input root is not readable/searchable: $INPUT_ROOT" >&2
    exit 1
fi

# Resolve all paths to absolute form so we can cd freely later. INPUT_ROOT and
# ESRGAN_DIR must already exist and were validated above, so plain realpath is
# right for them. The run directory is resolved with -m below because on a
# first run it does not exist yet, and plain realpath fails on a missing path.
INPUT_ROOT=$(realpath "$INPUT_ROOT")
ESRGAN_DIR=$(realpath "$ESRGAN_DIR")

# shellcheck source=pipeline_tools/run_lib.sh
source "${SCRIPT_DIR}/pipeline_tools/run_lib.sh"

# The run directory: everything this batch writes goes inside it. See
# pipeline_docs/storage-layout.md.
#
# A batch is usually not finished when it stops -- 249 hours of ESRGAN does not
# fit in one sitting -- so resuming has to continue into the run it started,
# not open a second one beside it. With neither --run-dir nor --run-id, the
# newest unfinished run whose settings match this invocation is adopted. The
# settings have to match: appending days upscaled at a different scale or model
# to an earlier run would put output that was never comparable into one
# directory and one README.
RUN_ROOT=$(run_root "$PRESERVATION_ROOT")
# The same signature state_check_config() compares, so a run is adopted on
# exactly the conditions that would let its state file be reused.
BATCH_CRF="${CRF_ARG:-$DEFAULT_CRF}"
# The pipeline picks its model from the scale unless told otherwise, so the
# batch records the same derivation rather than leaving the manifest silent
# about which model produced the videos.
case "$SCALE" in
    4) BATCH_MODEL=RealESRGAN_x4plus ;;
    *) BATCH_MODEL=RealESRGAN_x2plus ;;
esac
BATCH_CONFIG=$(jq -nc \
    --arg scale "$SCALE" --arg crf "$BATCH_CRF" \
    --arg profile "dv" --arg rife "false" --arg model "$BATCH_MODEL" \
    --arg input_root "$INPUT_ROOT" --arg time_source "$TIME_SOURCE" \
    --arg group_by "$GROUP_BY" \
    '{scale: $scale, crf: $crf, profile: $profile, rife: $rife,
      model: $model, input_root: $input_root, time_source: $time_source,
      group_by: $group_by}')

if [[ $DRY_RUN == true ]]; then
    # Dry-run paths include grouping and time source so different plans can be
    # compared. Other option changes replace the plan for that combination;
    # run.json records the command. Refuse to replace unrelated directories.
    RUN_DIR="${TMPDIR:-/tmp}/upscale-dv-plan-${GROUP_BY}-${TIME_SOURCE}"
    if [[ -e $RUN_DIR ]]; then
        # A predictable name is the feature, and it is also what makes it
        # possible for something else to be sitting on this path. Replace only
        # what is recognisably a previous plan: a directory that is empty or
        # holds a run.json. Merging into a stale one is not an option -- the
        # manifest is reused when it already exists, so an earlier archive's
        # sources and exclusions would accumulate into this plan.
        if [[ ! -d $RUN_DIR ]] \
           || { [[ ! -f "${RUN_DIR}/run.json" ]] \
                && [[ -n $(ls -A -- "$RUN_DIR" 2>/dev/null) ]]; }; then
            echo "Error: $RUN_DIR exists and is not a previous dry-run plan." >&2
            echo "       Move it aside, or set TMPDIR to plan somewhere else." >&2
            exit 1
        fi
        rm -rf -- "$RUN_DIR" \
            || { echo "Error: could not replace the previous plan at $RUN_DIR." >&2; exit 1; }
    fi
    mkdir -p -- "$RUN_DIR" \
        || { echo "Error: could not create the dry-run plan directory." >&2; exit 1; }
elif [[ -n "$RUN_DIR" ]]; then
    RUN_DIR=$(realpath -m "$RUN_DIR")
elif [[ -n "$RUN_ID" ]]; then
    RUN_DIR="${RUN_ROOT}/${RUN_ID}"
elif RUN_DIR=$(run_find_adoptable "$RUN_ROOT" digital8 "$BATCH_CONFIG"); then
    :
else
    RUN_DIR="${RUN_ROOT}/$(run_make_id digital8-batch)"
fi
mkdir -p "$RUN_DIR"
RUN_DIR=$(realpath "$RUN_DIR")

# Locate prepare_video.sh relative to this script's own directory, so both
# scripts can live together without requiring PATH manipulation.
PREPARE_SCRIPT="${SCRIPT_DIR}/prepare_video.sh"
DV_TIME_SCRIPT="${SCRIPT_DIR}/pipeline_tools/dv_recording_time.py"
DV_DATE_CHECK_SCRIPT="${SCRIPT_DIR}/pipeline_tools/check_dv_date.py"
PIPELINE_SCRIPT="${ESRGAN_DIR}/video_upscale_pipeline.py"
# Use the repository venv when present, otherwise python3 from PATH. Planning
# does not require model dependencies; inference checks its own environment.
VENV_PYTHON="${ESRGAN_DIR}/venv/bin/python3"
if [[ ! -x "$VENV_PYTHON" ]]; then
    VENV_PYTHON=$(command -v python3 2>/dev/null) || VENV_PYTHON=""
fi
VIDEOS_DIR=$(run_videos_dir "$RUN_DIR")
LOGS_DIR=$(run_logs_dir "$RUN_DIR")
STATE_DIR=$(run_state_dir "$RUN_DIR")
WORK_DIR=$(run_work_dir "$RUN_DIR")
STATE_FILE="${STATE_DIR}/batch_state.json"
MASTERS_DIR="${WORK_DIR}/masters"        # prepare_video.sh output, deleted per file
BATCH_LOG="${RUN_DIR}/run.log"
# Control files a person touches by hand, so they sit at the top of the run
# directory where they can be found without knowing the layout.
STOP_FILE="${RUN_DIR}/STOP"             # touch to stop after current file
STOP_DAY_FILE="${RUN_DIR}/STOP_DAY"     # touch to stop after current day assembles

if [[ ! -f "$PREPARE_SCRIPT" ]]; then
    echo "Error: prepare_video.sh not found at: $PREPARE_SCRIPT" >&2
    exit 1
fi
if [[ ! -f "$PIPELINE_SCRIPT" ]]; then
    echo "Error: video_upscale_pipeline.py not found at: $PIPELINE_SCRIPT" >&2
    exit 1
fi
if [[ -z "$VENV_PYTHON" ]]; then
    echo "Error: no python3 found for the pipeline." >&2
    echo "       Expected a venv at ${ESRGAN_DIR}/venv — run: python3 -m venv venv" >&2
    exit 1
fi
# mkvmerge assembles the day and MP4Box writes its chapters, both only at the
# very end. Checked here so a missing one costs nothing instead of surfacing
# after hours of upscaling, with the day's work done and unassemblable.
for tool in ffmpeg ffprobe python3 jq mkvmerge MP4Box; do
    if ! command -v "$tool" &>/dev/null; then
        echo "Error: Required tool not found in PATH: $tool" >&2
        exit 1
    fi
done

# Remove any stale STOP file left from a previous run so it doesn't cause
# an immediate stop on the next run.
if [[ -f "$STOP_FILE" ]]; then
    rm -f "$STOP_FILE"
    echo "Removed stale STOP file from previous run: $STOP_FILE"
fi
if [[ -f "$STOP_DAY_FILE" ]]; then
    rm -f "$STOP_DAY_FILE"
    echo "Removed stale STOP_DAY file from previous run: $STOP_DAY_FILE"
fi
# Output and intermediate directories are only created on a real run. A dry
# run uses state/ internally because the planning path shares the real resume
# and sorting code, then the EXIT trap discards it after day-plan.tsv and
# run.json have been written.
mkdir -p "$STATE_DIR"
[[ "$DRY_RUN" == false ]] && mkdir -p "$VIDEOS_DIR" "$LOGS_DIR" "$MASTERS_DIR"

# The run's manifest. A run adopted from an earlier invocation already has one
# and keeps it: the run id, its start time and everything recorded against it
# so far belong to the run, not to this invocation of the script. Only the
# command line is refreshed, so the manifest shows how the run was last driven.
if [[ ! -f "$(run_manifest "$RUN_DIR")" ]]; then
    run_manifest_init "$RUN_DIR" digital8 upscale_dv_batch.sh "$PRESERVATION_ROOT" \
        || { echo "Error: could not write $(run_manifest "$RUN_DIR")" >&2; exit 1; }
    run_manifest_set "$RUN_DIR" --argjson settings "$BATCH_CONFIG" '.settings = $settings' \
        || { echo "Error: could not record settings in the run manifest" >&2; exit 1; }
fi
run_manifest_set "$RUN_DIR" --args '.command = $ARGS.positional' -- \
    "$0" "${ORIGINAL_ARGS[@]}" || true
if [[ $DRY_RUN == true ]]; then
    run_manifest_set "$RUN_DIR" '.plan_only = true' || true
fi

# ------------------------------------------------------------------------------
# 3. Logging
# ------------------------------------------------------------------------------
# All output goes to both the terminal and the batch log.
# We use a named pipe so we can track the tee PID and wait for it on exit,
# preventing the terminal from being left in a broken state when tee doesn't
# drain before the shell exits.

TEE_PID=""
DISCOVERY_FILE=""
if [[ "$DRY_RUN" == false ]]; then
    exec > >(tee -a "$BATCH_LOG") 2>&1
    TEE_PID=$!
fi

cleanup_tee() {
    # Flush and close the tee pipe so it drains before the terminal returns
    # to the user. Without this, bash exits leaving tee's buffer unread and
    # the terminal in a state where typed input is not echoed.
    #
    # Nothing to drain when no tee was started -- a --dry-run never starts one
    # -- and nothing to repair either, because the terminal was never put at
    # risk. Returning here matters: the stty below could hang the script.
    if [[ -n "$DISCOVERY_FILE" && -e "$DISCOVERY_FILE" ]]; then
        rm -f -- "$DISCOVERY_FILE"
    fi
    # batch_state.json and recording_times.tsv drive real resume/assembly.
    # They are working data during a plan, not useful plan artifacts, so keep
    # the temporary directory focused on run.json and day-plan.tsv.
    if [[ $DRY_RUN == true && -d $STATE_DIR ]]; then
        rm -rf -- "$STATE_DIR"
    fi
    [[ -n "$TEE_PID" ]] || return 0

    exec 1>&- 2>&-
    wait "$TEE_PID" 2>/dev/null || true

    # Only touch the terminal if there is one, and never let doing so stop the
    # script. Changing terminal settings from a process that is not in the
    # foreground process group raises SIGTTOU, whose default action is to STOP
    # the process -- so `stty sane` did not fail, it hung, until something
    # killed it. Under `timeout` that surfaced as exit 124 after a complete and
    # correct run. Ignoring SIGTTOU makes the call return instead.
    if [[ -t 0 ]]; then
        trap '' TTOU
        stty sane 2>/dev/null || true
        trap - TTOU
    fi
}
trap cleanup_tee EXIT

log() {
    # Emit a timestamped message to the log stream.
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

log "========================================================"
log "upscale_dv_batch.sh started"
log "  Input root:   $INPUT_ROOT"
if [[ $DRY_RUN == true ]]; then
    log "  Plan dir:     $RUN_DIR"
else
    log "  Run dir:      $RUN_DIR"
fi
log "  ESRGAN dir:   $ESRGAN_DIR"
log "  Scale:        ${SCALE}x"
[[ $DRY_RUN == false ]] && log "  State file:   $STATE_FILE"
[[ ${#DAY_FILTER[@]} -gt 0 ]] && log "  Day filter:   ${DAY_FILTER[*]}"
[[ "$DRY_RUN"       == true ]] && log "  *** DRY RUN — no media files will be modified ***"
log "========================================================"

# ------------------------------------------------------------------------------
# 3c. Graceful Stop Mechanism
# ------------------------------------------------------------------------------
# Two ways to request a clean stop:
#
#   1. Ctrl+C / SIGINT / SIGTERM — caught by the trap below.
#   2. Touch file: touch <RUN_DIR>/STOP
#      Checked at the start of each file and each day. Useful when the script
#      is running in a background session (tmux/screen) where Ctrl+C is awkward.
#      Remove the file before re-running to resume normally.
#
# In both cases the currently running ESRGAN job is allowed to complete (it
# may be processing thousands of frames and abandoning it mid-run wastes work).
# Any in-flight prefetch job is waited on before exiting. The state file is
# written up to the last completed file so a plain re-run resumes correctly.
#
# The stop flag is NOT set on normal child-process failures (a single file
# failing does not stop the batch — it is marked failed and skipped).

STOP_REQUESTED=false
STOP_DAY_REQUESTED=false

handle_stop_signal() {
    if [[ "$STOP_REQUESTED" == false ]]; then
        log ""
        log "⏸️  STOP SIGNAL received. Finishing current file then exiting cleanly."
        log "   To stop immediately (losing current ESRGAN progress): kill $$"
        log "   To stop after current file:  touch $STOP_FILE"
        log "   To stop after current day:   touch $STOP_DAY_FILE"
        STOP_REQUESTED=true
    fi
}
trap handle_stop_signal SIGINT SIGTERM

check_stop() {
    # Returns 0 (true) if a file-level stop has been requested, 1 otherwise.
    # Removes the touch file when detected so the next run starts cleanly.
    if [[ "$STOP_REQUESTED" == true ]]; then
        return 0
    fi
    if [[ -f "$STOP_FILE" ]]; then
        rm -f "$STOP_FILE"
        log "⏸️  STOP file detected and removed: $STOP_FILE"
        log "   Finishing current file then exiting cleanly."
        STOP_REQUESTED=true
        return 0
    fi
    return 1
}

check_stop_day() {
    # Returns 0 (true) if a day-level stop has been requested, 1 otherwise.
    # Checked at every day boundary. Removes the touch file when detected.
    # Independent of check_stop() — touching STOP_DAY lets the current day
    # finish (successfully or otherwise) before stopping; touching STOP stops
    # after the current file regardless of day completion.
    if [[ "$STOP_DAY_REQUESTED" == true ]]; then
        return 0
    fi
    if [[ -f "$STOP_DAY_FILE" ]]; then
        rm -f "$STOP_DAY_FILE"
        log "⏸️  STOP_DAY file detected and removed: $STOP_DAY_FILE"
        log "   Stopping cleanly at the day boundary."
        STOP_DAY_REQUESTED=true
        return 0
    fi
    return 1
}

# ------------------------------------------------------------------------------
# 4. State File Helpers
# ------------------------------------------------------------------------------
# State is a JSON object with three top-level keys:
#   "files":  { "<abs_source_path>": { "stage": "...", "master": "...",
#                                      "output": "...", "error": "..." } }
#   "days":   { "<recording_date>":  { "stage": "assembled"|"failed",
#                                      "chapter_file": "...", "error": "..." } }
#   "config": { "scale": "...", "crf": "...",
#               "profile": "...", "rife": "...", "input_root": "...",
#               "time_source": "...", "group_by": "..." }
#             -- see state_check_config().
#
# jq is used for all reads/writes to avoid fragile sed/awk JSON manipulation.

# The resume ledger, state/batch_state.json. Two keyed sections:
#
#   .files[<abs path>].stage   "prepared" prepare_video.sh finished, master
#                              path recorded; "upscaled" the pipeline finished,
#                              output path recorded; "failed" a stage failed
#                              and the error is recorded. Anything marked
#                              upscaled or failed is skipped on the next run.
#   .days[<YYYYMMDD>].stage    "assembled" the day video exists and passed its
#                              checks; the components it consumed are gone.
#
# A file with no entry, or one left mid-stage by an interruption, is retried
# from the start. Chunk-level resume within a single file belongs to
# video_upscale_pipeline.py, not here.
#
# .config holds the settings signature these entries were produced under; see
# state_check_config() for why reusing them under different settings is
# refused.
state_init() {
    # Initialise state file if it does not exist.
    if [[ ! -f "$STATE_FILE" ]]; then
        echo '{"files":{},"days":{}}' > "$STATE_FILE"
        [[ $DRY_RUN == true ]] || log "Initialised state file: $STATE_FILE"
    fi
}

state_get_file() {
    # state_get_file <abs_path> <field>
    # Returns the value of .files["<abs_path>"].<field>, or "" if absent.
    local path="$1" field="$2"
    jq -r --arg p "$path" --arg f "$field" \
        '.files[$p][$f] // ""' "$STATE_FILE"
}

state_set_file() {
    # state_set_file <abs_path> <field> <value>
    # Atomically updates a single field for a file entry.
    local path="$1" field="$2" value="$3"
    local tmp
    tmp=$(mktemp "${STATE_FILE}.XXXXXX")
    jq --arg p "$path" --arg f "$field" --arg v "$value" \
        '.files[$p][$f] = $v' "$STATE_FILE" > "$tmp"
    mv "$tmp" "$STATE_FILE"
}

state_get_day() {
    # Day state is keyed by the stable eight-digit recording date, not by the
    # navigational directory name. This lets a directory acquire or change a
    # descriptive suffix without making completed work look like a new day.
    # state_get_day <day_name_or_id> <field>
    local day="$1" field="$2"
    day=${day:0:8}
    jq -r --arg d "$day" --arg f "$field" \
        '.days[$d][$f] // ""' "$STATE_FILE"
}

state_set_day() {
    # state_set_day <day_name_or_id> <field> <value>
    local day="$1" field="$2" value="$3"
    day=${day:0:8}
    local tmp
    tmp=$(mktemp "${STATE_FILE}.XXXXXX")
    jq --arg d "$day" --arg f "$field" --arg v "$value" \
        '.days[$d][$f] = $v' "$STATE_FILE" > "$tmp"
    mv "$tmp" "$STATE_FILE"
}

state_validate_day_keys() {
    # Resume metadata must use the same stable date keys the writer produces.
    # Validate without rewriting the ledger or merging any entries.
    if ! jq -e 'type == "object" and (.files | type == "object")
                and (.days | type == "object")
                and all(.days | keys[]; test("^[0-9]{8}$"))' \
                "$STATE_FILE" >/dev/null; then
        echo "Error: invalid batch state; files and days must be objects, with day keys exactly YYYYMMDD." >&2
        echo "  Restore a complete batch_state.json or select a new --run-dir." >&2
        echo "  The existing state has not been changed." >&2
        exit 1
    fi
}

state_check_config() {
    # Guards against silently reusing "prepared"/"upscaled"/"assembled" state
    # from a run made under different settings -- e.g. resuming after the
    # --crf was given a different value, --scale/--time-source/--group-by
    # changed, or --input-root were pointed elsewhere.
    # --group-by belongs here for the same reason --input-root does: it decides
    # what a day key means. The same captures grouped by directory and by
    # recording time can produce different day memberships, so day state from
    # one grouping says nothing about the other and must not be reused as
    # though it did. Without this, a file marked "upscaled" is
    # skipped purely on the strength of its state entry (see
    # run_prefetch/collect_prefetch), with no check that the CRF/scale/roots
    # it was upscaled under match what THIS invocation actually wants --
    # silently mixing two different CRFs, or pointing at a stale output tree,
    # within what looks like one consistent batch. INPUT_ROOT is included
    # (not just scale/CRF/profile/rife) because day names are not globally
    # unique across archives
    # -- an identically-named day from a different --input-root would
    # otherwise go undetected the same way a CRF change would. The output root
    # needs no such check any more: the state file lives inside the run
    # directory it describes, so the two cannot point at different places.
    #
    # CRF_ARG empty means video_upscale_pipeline.py's own default CRF applies
    # -- normalise to that value ($1) here so the recorded signature reflects
    # the CRF actually used, not the sentinel.
    local default_crf="$1"
    local current_crf="${CRF_ARG:-$default_crf}"

    local stored_config
    stored_config=$(jq -c '.config // empty' "$STATE_FILE")

    local current_config
    current_config=$(jq -nc \
        --arg scale "$SCALE" \
        --arg crf "$current_crf" \
        --arg profile "dv" \
        --arg rife "false" \
        --arg model "$BATCH_MODEL" \
        --arg input_root "$INPUT_ROOT" \
        --arg time_source "$TIME_SOURCE" \
        --arg group_by "$GROUP_BY" \
        '{scale: $scale, crf: $crf, profile: $profile, rife: $rife,
          model: $model, input_root: $input_root, time_source: $time_source,
          group_by: $group_by}')

    if [[ -z "$stored_config" ]]; then
        # No config recorded yet. Only safe to adopt the current invocation
        # as the baseline if .files and .days are BOTH still empty -- a
        # genuinely fresh state file. A state file with existing "prepared"/
        # "upscaled"/"assembled" entries but no .config cannot establish what
        # produced that work. Refuse it rather than assigning today's settings.
        local is_empty
        is_empty=$(jq '(.files | length == 0) and (.days | length == 0)' "$STATE_FILE")
        if [[ "$is_empty" == "true" ]]; then
            local tmp
            tmp=$(mktemp "${STATE_FILE}.XXXXXX")
            jq --argjson c "$current_config" '.config = $c' "$STATE_FILE" > "$tmp"
            mv "$tmp" "$STATE_FILE"
            [[ $DRY_RUN == true ]] \
                || log "Recorded batch configuration signature: $current_config"
            return 0
        fi

        echo "Error: $STATE_FILE has existing \"prepared\"/\"upscaled\"/\"assembled\"" >&2
        echo "  entries but no recorded configuration signature. Requested configuration:" >&2
        echo "    $current_config" >&2
        echo "" >&2
        echo "  Adopting this configuration automatically would risk silently" >&2
        echo "  skipping that existing work as though it were already done under" >&2
        echo "  these settings, even if it was actually produced under different" >&2
        echo "  ones (e.g. a different --crf)." >&2
        echo "" >&2
        echo "  Restore the complete state with its recorded configuration," >&2
        echo "  or use a different --run-dir to process the source captures." >&2
        exit 1
    fi

    if [[ "$stored_config" != "$current_config" ]]; then
        echo "Error: this work directory's existing state was recorded under a" >&2
        echo "  different configuration than what was just requested:" >&2
        echo "    Existing:  $stored_config" >&2
        echo "    Requested: $current_config" >&2
        echo "" >&2
        echo "  Reusing run $RUN_DIR as-is would let already-\"prepared\"," >&2
        echo "  \"upscaled\", or \"assembled\" entries be skipped even though they were" >&2
        echo "  produced under different settings, silently mixing output quality" >&2
        echo "  within what looks like one consistent batch." >&2
        echo "" >&2
        echo "  Options:" >&2
        echo "    1. Match the existing configuration (scale/CRF/roots above), or" >&2
        echo "    2. Use a different --run-dir for this configuration, or" >&2
        echo "    3. Deliberately clear the existing state: edit or remove" >&2
        echo "       $STATE_FILE (this discards all resume progress in it)." >&2
        exit 1
    fi
}

state_remove_file_entries() {
    # state_remove_file_entries <abs_path1> [abs_path2 ...]
    # Removes all listed file entries from .files in the state file.
    # Called after a day is successfully assembled — the individual upscaled
    # files are deleted at that point, so their state entries serve no purpose.
    local tmp
    tmp=$(mktemp "${STATE_FILE}.XXXXXX")
    # Build a jq del() chain operating on .files while preserving .days.
    local jq_filter='.'
    for path in "$@"; do
        jq_filter+=" | del(.files[\"${path}\"])"
    done
    jq "$jq_filter" "$STATE_FILE" > "$tmp"
    mv "$tmp" "$STATE_FILE"
}

state_handle_failed_entries() {
    # A normal restart must never silently skip known failures and continue
    # into later days. --retry-failed makes recovery an explicit, safe batch
    # operation: preserve a timestamped state backup, then remove failed file
    # and day records so normal discovery reprocesses them from source.
    local failed_file_count failed_day_count
    failed_file_count=$(jq '[.files[] | select(.stage == "failed")] | length' "$STATE_FILE")
    failed_day_count=$(jq '[.days[] | select(.stage == "failed")] | length' "$STATE_FILE")

    if (( failed_file_count == 0 && failed_day_count == 0 )); then
        [[ "$RETRY_FAILED" == false ]] || log "No failed state entries found; resuming normally."
        return 0
    fi

    if [[ "$RETRY_FAILED" == false ]]; then
        echo "Error: batch state contains $failed_file_count failed file(s) and" >&2
        echo "  $failed_day_count failed day(s). A normal restart will not silently" >&2
        echo "  skip them and continue into later days." >&2
        echo "" >&2
        echo "  After correcting the reported cause, re-run the same command with:" >&2
        echo "    --retry-failed" >&2
        echo "" >&2
        echo "  The option backs up $STATE_FILE and retries only failed work." >&2
        return 1
    fi

    if [[ "$DRY_RUN" == true ]]; then
        log "DRY RUN — would retry $failed_file_count failed file(s) and clear $failed_day_count failed day record(s)."
        return 0
    fi

    local timestamp backup tmp
    timestamp=$(date '+%Y%m%d_%H%M%S')
    backup="${STATE_FILE}.before-retry-${timestamp}"
    cp -p -- "$STATE_FILE" "$backup"
    tmp=$(mktemp "${STATE_FILE}.XXXXXX")
    jq '
        .files |= with_entries(select(.value.stage != "failed")) |
        .days  |= with_entries(select(.value.stage != "failed"))
    ' "$STATE_FILE" > "$tmp"
    mv "$tmp" "$STATE_FILE"
    log "Retrying $failed_file_count failed file(s); cleared $failed_day_count failed day record(s)."
    log "State backup: $backup"
}

# ------------------------------------------------------------------------------
# 4b. Recording-time resolution and pre-flight probe
# ------------------------------------------------------------------------------
# Every capture carries the moment it was recorded in two places: the filename
# written by whatever software did the capture, and the DV subcode recording
# date the camcorder itself wrote into the stream. This section reconciles
# them BEFORE any GPU work starts.
#
# The filename wins when it carries a timestamp. Measured over 781 captures in
# the reference collection, 649 agreed with the subcode to the second and 130
# minute-only filenames agreed to the minute. The two remaining captures had
# unusable or contradictory subcode. A capture whose filename has no timestamp
# can still be grouped entirely from its DV subcode.
#
# The probe is a cross-check that catches what a filename cannot show: footage
# from an entirely different recording left on a reused tape.
#
# Why this runs up front: the resolved time determines both the day group and
# the chapter label. A source with no usable time must be found before it costs
# hours of GPU work.

# Tolerance for "wildly different". Anything closer than this is treated as
# agreement and never reported: the recording time only labels a chapter (the
# offsets come from measured durations), so a discrepancy of seconds or minutes
# on a twenty-year-old tape is cosmetic. A different calendar day is always an
# anomaly regardless of this value.
PROBE_TOLERANCE_SEC=3600

# Defined here rather than beside the ETA code that also uses it: pre-flight
# calls it when reporting how far a filename and its tape disagree, and a
# function defined further down the file does not exist yet at that point.
fmt_duration() {
    # fmt_duration <seconds>  →  "Xh YYm ZZs" or "YYm ZZs" or "ZZs"
    local total_sec
    total_sec=$(awk -v seconds="$1" 'BEGIN { printf "%d", seconds }')
    local h=$(( total_sec / 3600 ))
    local m=$(( (total_sec % 3600) / 60 ))
    local s=$(( total_sec % 60 ))
    if (( h > 0 )); then
        printf "%dh %02dm %02ds" "$h" "$m" "$s"
    elif (( m > 0 )); then
        printf "%dm %02ds" "$m" "$s"
    else
        printf "%ds" "$s"
    fi
}

# Recording times read from the DV subcode, keyed by absolute path. Filled in
# one pass by preflight_probe_all() before any file is reconciled.
declare -A PROBE_DT=()
# The parser's third TSV column: the agreeing pack sources (for example
# AAUX,SSYB,VAUX), or disagree: followed by the conflicting values. Retaining
# it lets the plan distinguish corroborated tape time from an unreadable or
# internally contradictory camcorder clock.
declare -A PROBE_DETAIL=()
# Empty for normal first-frame evidence. A recovery value records whether the
# first frame was empty or contradictory and the later frame that resolved it.
declare -A PROBE_NOTE=()

preflight_probe_all() {
    # preflight_probe_all <file>...
    #
    # Reads the camcorder's own recording time out of every capture in one
    # pass. pipeline_tools/dv_recording_time.py parses the DV VAUX packs
    # directly from each file's head, so this is one process for the whole
    # batch rather than a subprocess and a temporary file per capture.
    local -a files=("$@")
    local path value sources note check_command
    (( ${#files[@]} > 0 )) || return 0
    # IFS=$'\t' alone is not enough: tab is an IFS *whitespace* character, so
    # bash collapses runs of it and drops the empty middle column that a
    # conflict row carries ("path<TAB><TAB>disagree:..."). The conflict text
    # then landed in `value` and was stored as a recording time, and the
    # conflict branch could never fire. Splitting explicitly keeps the columns.
    local line
    while IFS= read -r line; do
        path=${line%%$'\t'*}
        local rest=${line#*$'\t'} after_value
        value=${rest%%$'\t'*}
        if [[ $rest == *$'\t'* ]]; then
            after_value=${rest#*$'\t'}
            sources=${after_value%%$'\t'*}
            if [[ $after_value == *$'\t'* ]]; then
                note=${after_value#*$'\t'}
            else
                note=""
            fi
        else
            sources=""
            note=""
        fi
        [[ -n $path ]] || continue
        PROBE_DT["$path"]=$value
        PROBE_DETAIL["$path"]=$sources
        PROBE_NOTE["$path"]=$note
        if [[ $note == recovered_after_* ]]; then
            local recovery_frame=${note##*:}
            local recovery_cause=${note#recovered_after_}
            recovery_cause=${recovery_cause%_head:*}
            log "  WARNING: ${path##*/}: $recovery_cause first-frame subcode;"
            log "           all populated sources agree at frame $recovery_frame."
            run_manifest_set "$RUN_DIR" --arg source "$path" \
                --arg cause "$recovery_cause" --argjson frame "$recovery_frame" \
                --arg timestamp "$value" \
                '.subcode_recoveries += [{source: $source, cause: $cause,
                                          frame: $frame, timestamp: $timestamp}]' || true
        fi
        # A capture whose own metadata sources contradict each other has no
        # usable tape time, and the parser deliberately returns none rather
        # than choosing between them. Say so: silently falling back to the
        # filename would hide a capture that genuinely needs looking at.
        if [[ $sources == disagree:* ]]; then
            log "  WARNING: DV metadata sources disagree in ${path##*/}"
            log "           ${sources#disagree:}"
            printf -v check_command 'python3 %q %q' "$DV_DATE_CHECK_SCRIPT" "$path"
            log "           No tape time was chosen. Inspect the complete capture:"
            log "             $check_command"
            run_manifest_set "$RUN_DIR" --arg source "$path" \
                --arg detail "${sources#disagree:}" \
                '.metadata_conflicts += [{source: $source, detail: $detail}]' || true
        fi
    done < <(printf '%s\0' "${files[@]}" \
        | "$DV_TIME_SCRIPT" --from-stdin 2>/dev/null || true)
    unset line rest
}

probe_recording_datetime() {
    # probe_recording_datetime <file>
    #
    # Prints the DV subcode recording datetime at the START of the file, or
    # nothing when its head carries no readable pack. preflight_probe_all()
    # must have run first.
    #
    # Only the head is parsed, deliberately: a DV pack is written per frame, so
    # reading further reports a LATER pack -- measured ~296 s late across 23
    # files -- which would silently mislabel every chapter on a long capture.
    local file=$1
    # Discovery canonicalizes every source before pre-flight, so another
    # realpath here would launch two needless processes per capture (this
    # helper and probe_recording_detail) on a full-archive plan.
    printf '%s\n' "${PROBE_DT[$file]:-}"
}

probe_recording_detail() {
    # probe_recording_detail <file> -- agreeing source names or conflict text.
    local file=$1
    printf '%s\n' "${PROBE_DETAIL[$file]:-}"
}

probe_recording_note() {
    # probe_recording_note <file> -- bounded-recovery status from the parser.
    local file=$1
    printf '%s\n' "${PROBE_NOTE[$file]:-}"
}

datetime_from_filename() {
    # datetime_from_filename <basename>
    #
    # Prints "YYYY-MM-DD HH:MM:SS" parsed from the capture filename, or nothing
    # when the name carries no recognisable date-time.
    local base=$1 d t
    if [[ "$base" =~ ^dv\.([0-9]{8})[-_]([0-9]{6}) ]]; then
        d="${BASH_REMATCH[1]}"; t="${BASH_REMATCH[2]}"
        printf '%s-%s-%s %s:%s:%s\n' "${d:0:4}" "${d:4:2}" "${d:6:2}" \
            "${t:0:2}" "${t:2:2}" "${t:4:2}"
    elif [[ "$base" =~ ^dv\.([0-9]{8})[-_]([0-9]{4})[^0-9] ]]; then
        # Short form with no seconds; second 00 is assumed, which is why the
        # comparison below is to the minute for these.
        d="${BASH_REMATCH[1]}"; t="${BASH_REMATCH[2]}"
        printf '%s-%s-%s %s:%s:00\n' "${d:0:4}" "${d:4:2}" "${d:6:2}" \
            "${t:0:2}" "${t:2:2}"
    fi
}

datetime_to_epoch() {
    date -d "$1" +%s 2>/dev/null
}

filename_subcode_comparison() {
    # filename_subcode_comparison <basename> <filename-time> <subcode-time> <detail> <note>
    #
    # Report the independent evidence comparison, separate from `basis`, which
    # says only which value the chosen policy used. Keep these stable machine
    # values short because they appear in both run.json and day-plan.tsv.
    local base=$1 name_dt=$2 probe_dt=$3 detail=$4 note=$5
    local name_epoch probe_epoch delta
    if [[ $detail == disagree:* ]]; then
        printf 'subcode_conflict\n'
    elif [[ -z $name_dt && -z $probe_dt ]]; then
        printf 'both_missing\n'
    elif [[ -z $name_dt ]]; then
        printf 'filename_missing\n'
    elif [[ -z $probe_dt ]]; then
        printf 'subcode_missing\n'
    elif [[ $name_dt == "$probe_dt" ]]; then
        if [[ $note == recovered_after_conflict_head:* ]]; then
            printf 'match_after_head_conflict\n'
        elif [[ $note == recovered_after_missing_head:* ]]; then
            printf 'match_after_missing_head\n'
        else
            printf 'match_exact\n'
        fi
    elif [[ $base =~ ^dv\.[0-9]{8}[-_][0-9]{4}[^0-9] \
            && ${name_dt:0:16} == "${probe_dt:0:16}" ]]; then
        printf 'match_filename_minute\n'
    elif ! name_epoch=$(datetime_to_epoch "$name_dt"); then
        printf 'filename_invalid\n'
    elif ! probe_epoch=$(datetime_to_epoch "$probe_dt"); then
        printf 'subcode_invalid\n'
    elif [[ ${name_dt:0:10} != "${probe_dt:0:10}" ]]; then
        printf 'mismatch_different_day\n'
    else
        delta=$(( name_epoch > probe_epoch
                  ? name_epoch - probe_epoch : probe_epoch - name_epoch ))
        if (( delta <= PROBE_TOLERANCE_SEC )); then
            printf 'same_day_within_tolerance\n'
        else
            printf 'mismatch_over_tolerance\n'
        fi
    fi
}

# Pre-flight results, indexed in parallel. PREFLIGHT_RESOLVED is keyed by the
# source file's basename stem, which is the prefix the pipeline carries into
# the upscaled filename, so assembly can look a time back up later.
declare -A PREFLIGHT_RESOLVED=()
# Captures pre-flight decided to leave out, keyed by absolute path. The day
# loop skips these before any work is done on them, which is the whole point of
# resolving recording times up front: a capture that cannot be placed in a day
# must cost 30 seconds of probing, not hours of GPU time followed by a failed
# assembly of the entire day it belonged to.
declare -A PREFLIGHT_SKIP=()
# Whether the tape cross-check can run at all. Checked once, so pre-flight can
# say plainly that it is off rather than silently resolving every capture from
# its filename alone.
# The parser ships with this repository and python3 is already required, so
# this is effectively always true. It is still checked rather than assumed: a
# missing parser must be reported as a disabled cross-check, not resolve every
# capture from its filename with nothing in the log to say the tape was never
# read.
SUBCODE_AVAILABLE=true
[[ -f $DV_TIME_SCRIPT ]] || SUBCODE_AVAILABLE=false
declare -a PREFLIGHT_ANOMALY_FILE=()
declare -a PREFLIGHT_ANOMALY_KIND=()
declare -a PREFLIGHT_ANOMALY_NAME_DT=()
declare -a PREFLIGHT_ANOMALY_PROBE_DT=()
declare -a PREFLIGHT_ANOMALY_PROBE_DETAIL=()
declare -a PREFLIGHT_ANOMALY_PROBE_NOTE=()
PREFLIGHT_SUBCODE_READABLE=0
PREFLIGHT_SUBCODE_UNREADABLE=0

preflight_exclude() {
    # preflight_exclude <file> <reason>
    #
    # Record a capture the batch must not process, both in memory for this run
    # and in the manifest, so the run's own record says what it left out and
    # why rather than leaving an unexplained gap between sources and videos.
    local file=$1 reason=$2 abs
    abs=$(realpath -m -- "$file")
    PREFLIGHT_SKIP["$abs"]=$reason
    run_manifest_set "$RUN_DIR" --arg source "$abs" --arg reason "$reason" \
        '.excluded += [{source: $source, reason: $reason}]' || true
}

preflight_table_path() {
    printf '%s/recording_times.tsv\n' "$STATE_DIR"
}

preflight_probe_file() {
    # preflight_probe_file <file>
    #
    # Resolves one capture and records either a resolved time or an anomaly.
    # Returns 0 always; the caller inspects the anomaly arrays.
    local file=$1 base stem name_dt probe_dt probe_detail probe_note
    local name_epoch probe_epoch delta kind
    base=${file##*/}
    stem=${base%.*}
    name_dt=$(datetime_from_filename "$base")
    probe_dt=$(probe_recording_datetime "$file")
    probe_detail=$(probe_recording_detail "$file")
    probe_note=$(probe_recording_note "$file")

    if [[ -n $probe_dt ]]; then
        (( PREFLIGHT_SUBCODE_READABLE++ )) || true
    else
        (( PREFLIGHT_SUBCODE_UNREADABLE++ )) || true
    fi

    # A later timestamp following an empty head does not describe the capture
    # start. When the filename supplies that start, force an explicit review
    # and use it as the fallback even under the subcode-preferred policy.
    if [[ $probe_note == recovered_after_missing_head:* && -n $name_dt ]]; then
        kind=late_subcode
    elif [[ $TIME_SOURCE == subcode ]]; then
        if [[ -z $probe_dt ]]; then
            if [[ $probe_detail == disagree:* ]]; then
                kind=subcode_conflict
            else
                kind=no_subcode
            fi
        elif [[ -z $name_dt ]]; then
            # A filename timestamp is optional in subcode mode. This is the
            # path that makes a flat tree of opaque capture names useful.
            PREFLIGHT_RESOLVED["$stem"]=$probe_dt
            return 0
        else
            name_epoch=$(datetime_to_epoch "$name_dt")
            probe_epoch=$(datetime_to_epoch "$probe_dt")
            if [[ -z $probe_epoch ]]; then
                kind=no_subcode
            elif [[ -z $name_epoch ]]; then
                kind=invalid_filename_time
            elif [[ "${name_dt:0:10}" != "${probe_dt:0:10}" ]]; then
                kind=different_day
            else
                delta=$(( name_epoch > probe_epoch
                          ? name_epoch - probe_epoch : probe_epoch - name_epoch ))
                if (( delta > PROBE_TOLERANCE_SEC )); then
                    kind=far_apart
                else
                    PREFLIGHT_RESOLVED["$stem"]=$probe_dt
                    return 0
                fi
            fi
        fi
    elif [[ -z "$name_dt" && -z "$probe_dt" ]]; then
        if [[ $probe_detail == disagree:* ]]; then
            kind=subcode_conflict
        else
            kind=no_time
        fi
    elif [[ -z "$name_dt" ]]; then
        # Auto mode keeps a missing filename visible rather than silently
        # changing which evidence is authoritative.
        kind=name_unparsed
    elif [[ -z "$probe_dt" ]]; then
        # Auto mode can still place this capture from its filename, but probe
        # coverage is reported below. An explicitly subcode-derived plan takes
        # the no_subcode anomaly branch above instead.
        if [[ $probe_detail == disagree:* ]]; then
            kind=subcode_conflict
        elif ! name_epoch=$(datetime_to_epoch "$name_dt"); then
            kind=invalid_filename_time
        else
            PREFLIGHT_RESOLVED["$stem"]=$name_dt
            return 0
        fi
    else
        name_epoch=$(datetime_to_epoch "$name_dt")
        probe_epoch=$(datetime_to_epoch "$probe_dt")
        if [[ -z "$name_epoch" && -n "$probe_epoch" ]]; then
            kind=invalid_filename_time
        elif [[ -z "$name_epoch" || -z "$probe_epoch" ]]; then
            kind=no_time
        elif [[ "${name_dt:0:10}" != "${probe_dt:0:10}" ]]; then
            kind=different_day
        else
            delta=$(( name_epoch > probe_epoch
                      ? name_epoch - probe_epoch : probe_epoch - name_epoch ))
            if (( delta > PROBE_TOLERANCE_SEC )); then
                kind=far_apart
            else
                # Agreement. The filename is authoritative.
                PREFLIGHT_RESOLVED["$stem"]=$name_dt
                return 0
            fi
        fi
    fi

    PREFLIGHT_ANOMALY_FILE+=("$file")
    PREFLIGHT_ANOMALY_KIND+=("$kind")
    PREFLIGHT_ANOMALY_NAME_DT+=("$name_dt")
    PREFLIGHT_ANOMALY_PROBE_DT+=("$probe_dt")
    PREFLIGHT_ANOMALY_PROBE_DETAIL+=("$probe_detail")
    PREFLIGHT_ANOMALY_PROBE_NOTE+=("$probe_note")
    # A provisional resolution so a "continue" decision has something to use.
    if [[ $kind == late_subcode && -n $name_dt ]]; then
        PREFLIGHT_RESOLVED["$stem"]=$name_dt
    elif [[ $TIME_SOURCE == subcode && -n $probe_dt ]]; then
        PREFLIGHT_RESOLVED["$stem"]=$probe_dt
    elif [[ -n "$name_dt" ]]; then
        PREFLIGHT_RESOLVED["$stem"]=$name_dt
    elif [[ -n "$probe_dt" ]]; then
        PREFLIGHT_RESOLVED["$stem"]=$probe_dt
    fi
    return 0
}

describe_anomaly() {
    # describe_anomaly <index> -- one human-readable block for the report.
    local i=$1
    local file="${PREFLIGHT_ANOMALY_FILE[$i]}"
    local kind="${PREFLIGHT_ANOMALY_KIND[$i]}"
    local name_dt="${PREFLIGHT_ANOMALY_NAME_DT[$i]}"
    local probe_dt="${PREFLIGHT_ANOMALY_PROBE_DT[$i]}"
    local probe_detail="${PREFLIGHT_ANOMALY_PROBE_DETAIL[$i]}"
    local probe_note="${PREFLIGHT_ANOMALY_PROBE_NOTE[$i]}"
    printf '  %s\n' "${file##*/}"
    printf '    source directory: %s\n' "$(dirname "$file")"
    case "$kind" in
        different_day)
            printf '    filename says : %s\n' "$name_dt"
            printf '    tape says     : %s\n' "$probe_dt"
            printf '    A different calendar day. Usually footage left on a\n'
            printf '    reused tape that the camcorder did not overwrite.\n'
            ;;
        far_apart)
            printf '    filename says : %s\n' "$name_dt"
            printf '    tape says     : %s\n' "$probe_dt"
            printf '    Same day, but more than %s apart.\n' \
                "$(fmt_duration "$PROBE_TOLERANCE_SEC")"
            ;;
        late_subcode)
            printf '    filename says : %s\n' "$name_dt"
            printf '    tape first valid: %s  (frame %s)\n' \
                "$probe_dt" "${probe_note##*:}"
            printf '    The first frame has no usable subcode. A later value is\n'
            printf '    evidence for the recording day, but not the capture start.\n'
            printf '    The filename time will be used as the fallback.\n'
            ;;
        name_unparsed)
            printf '    filename says : (no recognisable date-time)\n'
            printf '    tape says     : %s\n' "$probe_dt"
            printf '    The tape time can be used; the filename cannot.\n'
            ;;
        invalid_filename_time)
            printf '    filename says : %s (not a valid calendar time)\n' "$name_dt"
            if [[ -n $probe_dt ]]; then
                printf '    tape says     : %s\n' "$probe_dt"
                printf '    The tape time can be used; the filename cannot.\n'
            else
                printf '    tape says     : (no usable date/time at the head)\n'
                printf '    Neither source can place this capture.\n'
            fi
            ;;
        no_subcode)
            printf '    filename says : %s\n' "${name_dt:-(no recognisable date-time)}"
            if $SUBCODE_AVAILABLE; then
                printf '    tape says     : (no usable date/time at the head)\n'
                printf '    The camcorder clock may have been unset, its packs may\n'
                printf '    have been lost during remuxing, or the file may be damaged.\n'
            else
                printf '    tape says     : (subcode parser unavailable)\n'
            fi
            printf '    A subcode-derived plan cannot place this capture.\n'
            ;;
        subcode_conflict)
            printf '    filename says : %s\n' "${name_dt:-(no recognisable date-time)}"
            printf '    tape sources  : %s\n' "${probe_detail#disagree:}"
            printf '    The camcorder metadata contradicts itself; no tape time\n'
            printf '    is selected automatically. Run check_dv_date.py on it.\n'
            ;;
        no_time)
            printf '    filename says : (no recognisable date-time)\n'
            printf '    tape says     : (no date at the head of the file)\n'
            printf '    Neither source gives a recording time. Chapter\n'
            printf '    assembly needs one, so this file cannot be placed.\n'
            ;;
    esac
}

preflight_write_table() {
    # Persist stem -> recording time. Assembly may happen in a LATER run after
    # a resume, by which point these arrays are gone, so the table is the
    # durable record rather than in-memory state.
    local table tmp stem
    table=$(preflight_table_path)
    tmp=$(mktemp "${table}.XXXXXX") || return 1
    for stem in "${!PREFLIGHT_RESOLVED[@]}"; do
        printf '%s\t%s\n' "$stem" "${PREFLIGHT_RESOLVED[$stem]}"
    done | sort > "$tmp" || { rm -f "$tmp"; return 1; }
    mv -f "$tmp" "$table"
}

output_recording_time() {
    # output_recording_time <upscaled_path>
    #
    # Prints the recording time pre-flight resolved for the source this
    # upscaled file came from. The pipeline appends its own suffixes to the
    # source stem, so peel them off one underscore-delimited token at a time
    # and take the longest prefix that is a known stem.
    local stem
    stem=$(basename "$1")
    stem=${stem%.mkv}; stem=${stem%.mp4}
    while [[ -n "$stem" ]]; do
        if preflight_lookup_time "$stem"; then
            return 0
        fi
        [[ "$stem" == *_* ]] || break
        stem=${stem%_*}
    done
    return 1
}

preflight_lookup_time() {
    # preflight_lookup_time <stem> -- print the recorded time for a stem.
    # Reads the table rather than the array so a resumed run works.
    local stem=$1 table
    table=$(preflight_table_path)
    [[ -f $table ]] || return 1
    awk -F '\t' -v want="$stem" '$1 == want { print $2; found = 1; exit }
                                 END { exit !found }' "$table"
}

run_preflight() {
    # run_preflight <file1> [file2 ...]
    #
    # Resolves every capture about to be processed and, if anything is
    # ambiguous, reports it and decides what to do. Returns 0 to proceed,
    # 1 to abort the batch.
    local -a files=("$@")
    (( ${#files[@]} > 0 )) || return 0

    if ! $SUBCODE_AVAILABLE; then
        # Auto mode can fall back to filenames. Strict subcode mode turns each
        # file into an anomaly below, where the selected policy may skip it or
        # explicitly choose the filename; neither mode silently claims that a
        # tape check happened.
        log "WARNING: the DV tape cross-check is OFF -- parser not found at"
        log "         $DV_TIME_SCRIPT"
        if [[ $TIME_SOURCE == auto ]]; then
            log "         Recording times can come from filenames, but footage from"
            log "         a different recording on a reused tape will not be detected."
        else
            log "         A subcode-derived plan cannot include any capture until the"
            log "         parser is restored or a filename override is selected."
        fi
        run_manifest_set "$RUN_DIR" \
            '.subcode_cross_check = "unavailable: dv_recording_time.py not found"' || true
    fi
    log "Pre-flight: resolving recording times for ${#files[@]} captures..."
    log "  Time source: $TIME_SOURCE"
    local start_ts file
    start_ts=$(date +%s)
    preflight_probe_all "${files[@]}"
    for file in "${files[@]}"; do
        preflight_probe_file "$file"
    done
    local elapsed=$(( $(date +%s) - start_ts ))
    log "  Examined ${#files[@]} capture(s) in ${elapsed}s."
    log "  DV subcode timestamps readable: $PREFLIGHT_SUBCODE_READABLE of ${#files[@]}."
    if (( PREFLIGHT_SUBCODE_UNREADABLE > 0 )); then
        if [[ $TIME_SOURCE == auto ]]; then
            log "  WARNING: $PREFLIGHT_SUBCODE_UNREADABLE capture(s) have no usable,"
            log "           internally consistent DV subcode time. Auto mode can use"
            log "           a valid filename for them, but they were not cross-checked."
        else
            log "  WARNING: $PREFLIGHT_SUBCODE_UNREADABLE capture(s) cannot be placed"
            log "           in a strictly subcode-derived plan without an override."
        fi
    fi

    local n=${#PREFLIGHT_ANOMALY_FILE[@]}
    if (( n == 0 )); then
        if [[ $TIME_SOURCE == subcode ]]; then
            log "  No anomalies: every capture has a usable DV subcode timestamp,"
            log "  and every comparable filename is consistent with it."
        elif (( PREFLIGHT_SUBCODE_UNREADABLE == 0 )); then
            log "  No anomalies: every capture's filename agrees with the tape."
        else
            log "  No filename/tape conflicts were found among readable subcodes."
        fi
        if ! preflight_write_table; then
            log "ERROR: could not write $(preflight_table_path)" >&2
            return 1
        fi
        return 0
    fi

    log ""
    log "Pre-flight found $n capture(s) whose recording time is uncertain:"
    local i
    for (( i = 0; i < n; i++ )); do
        describe_anomaly "$i" | while IFS= read -r line; do log "$line"; done
    done
    log ""
    log "The recording time labels a chapter; chapter positions come from"
    log "measured durations, so a wrong label misnames a chapter but does not"
    log "misplace it. Files with no time at all cannot be assembled."

    local decision="$ON_ANOMALY"
    if [[ "$decision" == "ask" ]]; then
        # Open the terminal rather than testing -r on it: the device node can
        # exist and pass a permission test while having no controlling
        # terminal behind it, in which case every read and write fails.
        # The redirection is inside a group so its own failure message is
        # suppressed: on `exec 3<>/dev/tty 2>/dev/null` the tty redirection is
        # attempted, and reported, before the 2>/dev/null takes effect.
        if { exec 3<>/dev/tty; } 2>/dev/null; then
            local reply=""
            local default_choice=f
            [[ $TIME_SOURCE == subcode ]] && default_choice=p
            log ""
            printf '  [f] use the filename time   [p] use the tape time\n' >&3
            printf '  [s] skip these files        [a] abort\n' >&3
            printf '  Choice [f/p/s/a, default %s]: ' "$default_choice" >&3
            if read -r reply <&3; then
                case "${reply,,}" in
                    f)    decision=use-filename ;;
                    p)    decision=prefer-subcode ;;
                    s)    decision=skip ;;
                    a)    decision=abort ;;
                    '')   if [[ $default_choice == p ]]; then
                              decision=prefer-subcode
                          else
                              decision=use-filename
                          fi ;;
                    *)    log "  Unrecognised choice '$reply' -- aborting."
                          decision=abort ;;
                esac
            else
                # EOF on the terminal (input closed mid-prompt).
                decision=abort
            fi
            exec 3>&-
            log "  Chose: $decision"
        else
            # Non-interactive and no explicit policy. Aborting here costs the
            # 30 seconds of pre-flight and nothing else; proceeding would mean
            # silently guessing on someone's archive.
            log "ERROR: anomalies found, no terminal to ask, and --on-anomaly" >&2
            log "       was not given. Re-run with --on-anomaly to proceed." >&2
            return 1
        fi
    fi

    case "$decision" in
        abort)
            log "Aborting before any processing, as requested."
            return 1
            ;;
        skip)
            for (( i = 0; i < n; i++ )); do
                local sk
                sk=${PREFLIGHT_ANOMALY_FILE[$i]##*/}
                unset 'PREFLIGHT_RESOLVED[${sk%.*}]'
                preflight_exclude "${PREFLIGHT_ANOMALY_FILE[$i]}" \
                    "filename and tape disagree, or neither gives a time"
                log "  Skipping: $sk"
            done
            preflight_write_table || return 1
            ;;
        prefer-subcode)
            # "prefer", not "use": the tape wins where it can, but the first
            # branch below deliberately takes the filename, and the third is
            # barred under --time-source subcode. A name promising the probe
            # outright would misdescribe all three.
            #
            # The filename fallback below is barred under --time-source
            # subcode. A plan declared strictly subcode-derived must not place
            # a capture from its name: an unset camcorder clock writes
            # 1970-01-01, and the plausible-looking filename beside it is
            # exactly the evidence that mode exists to refuse -- and pre-flight
            # has already warned that such a capture "cannot be placed in a
            # strictly subcode-derived plan without an override". Under auto the
            # fallback applies, because there the filename is a declared source
            # of truth.
            for (( i = 0; i < n; i++ )); do
                local pb="${PREFLIGHT_ANOMALY_PROBE_DT[$i]}"
                local nd="${PREFLIGHT_ANOMALY_NAME_DT[$i]}"
                local kind="${PREFLIGHT_ANOMALY_KIND[$i]}"
                local st
                st=${PREFLIGHT_ANOMALY_FILE[$i]##*/}; st=${st%.*}
                if [[ $kind == late_subcode && -n $nd ]]; then
                    PREFLIGHT_RESOLVED["$st"]=$nd
                    log "  Using filename fallback for $st: $nd"
                elif [[ -n "$pb" ]]; then
                    PREFLIGHT_RESOLVED["$st"]=$pb
                    log "  Using tape time for $st: $pb"
                elif [[ $TIME_SOURCE != subcode && -n $nd ]] \
                        && datetime_to_epoch "$nd" >/dev/null; then
                    PREFLIGHT_RESOLVED["$st"]=$nd
                    log "  No reliable tape time for $st -- using filename fallback: $nd"
                else
                    unset 'PREFLIGHT_RESOLVED[$st]'
                    preflight_exclude "${PREFLIGHT_ANOMALY_FILE[$i]}" \
                        "no recording time on the tape"
                    log "  No tape time for $st -- skipping it."
                fi
            done
            preflight_write_table || return 1
            ;;
        use-filename)
            for (( i = 0; i < n; i++ )); do
                local nd="${PREFLIGHT_ANOMALY_NAME_DT[$i]}"
                local st
                st=${PREFLIGHT_ANOMALY_FILE[$i]##*/}; st=${st%.*}
                if [[ -n "$nd" ]] && datetime_to_epoch "$nd" >/dev/null; then
                    PREFLIGHT_RESOLVED["$st"]=$nd
                    log "  Using filename time for $st: $nd"
                else
                    unset 'PREFLIGHT_RESOLVED[$st]'
                    preflight_exclude "${PREFLIGHT_ANOMALY_FILE[$i]}" \
                        "no recording time in the filename"
                    log "  No filename time for $st -- skipping it."
                fi
            done
            preflight_write_table || return 1
            ;;
    esac
    log "  Final plan includes ${#PREFLIGHT_RESOLVED[@]} of ${#files[@]} capture(s)."
    return 0
}

# ------------------------------------------------------------------------------
# 5. Chapter Assembly Helper
# ------------------------------------------------------------------------------
extract_pipeline_suffix() {
    # extract_pipeline_suffix <filename>
    #
    # Extracts the profile/scale/model/fps/CRF/pix_fmt token from an upscaled
    # output filename, e.g. "dv_x2_x2plus_60fps_crf23_yuv420". This token is
    # the filename-visible
    # proxy for the quality settings a file was actually produced with, and is
    # used both to name the day's assembled video and to confirm every file being
    # merged into it shares the same settings (see the mismatch check at the
    # day-assembly call site) -- the assembly appends tracks without
    # re-encoding, so it will not itself catch or warn about mixing CRF/
    # pix_fmt levels the way ffmpeg's -f concat demuxer was confirmed NOT to
    # (see check_output_pix_fmt in video_upscale_pipeline.py).
    #
    # Prints the extracted suffix and returns 0 on match, or returns 1 with
    # nothing printed if the filename doesn't match the expected pattern.
    local base="$1"
    # The pipeline writes one shape of name: _yuv420 in .mp4. Nothing else is
    # matched, deliberately -- admitting a name this pipeline does not produce
    # only carries the file as far as validate_output_file(), which rejects it
    # for the same reason.
    if [[ "$base" =~ _([a-z0-9]+_x[0-9]+_[a-z0-9_]+_[0-9]+fps(_rife)?_crf[0-9]+_yuv420)\.mp4$ ]]; then
        echo "${BASH_REMATCH[1]}"
        return 0
    fi
    return 1
}

validate_upscaled_output() {
    # validate_upscaled_output <file> <filename_quality_suffix>
    # Verifies the media stream itself agrees with the quality policy encoded
    # in its filename. This is especially important on resume, where the
    # pipeline's own post-encode checks are not rerun.
    local file="$1" suffix="$2"
    if [[ ! -f "$file" || ! -s "$file" ]]; then
        echo "  [output validate] ERROR: missing or empty output: $file" >&2
        return 1
    fi

    local expected_crf expected_pix
    if [[ "$suffix" =~ _crf([0-9]+) ]]; then
        expected_crf="${BASH_REMATCH[1]}"
    else
        echo "  [output validate] ERROR: no CRF token in suffix '$suffix'" >&2
        return 1
    fi
    # The pipeline writes one kind of deliverable: yuv420p in MP4 with AAC.
    # A suffix without the token is not something this batch produced.
    if [[ "$suffix" != *_yuv420 ]]; then
        echo "  [output validate] ERROR: suffix '$suffix' has no _yuv420 token: $file" >&2
        return 1
    fi
    expected_pix="yuv420p"

    local codec pix_fmt actual_crf color_space color_trc color_primaries audio_codec
    codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
        -of default=noprint_wrappers=1:nokey=1 "$file" 2>/dev/null)
    pix_fmt=$(get_master_pix_fmt "$file")
    actual_crf=$(get_master_crf "$file")
    color_space=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_space \
        -of default=noprint_wrappers=1:nokey=1 "$file" 2>/dev/null)
    color_trc=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_transfer \
        -of default=noprint_wrappers=1:nokey=1 "$file" 2>/dev/null)
    color_primaries=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_primaries \
        -of default=noprint_wrappers=1:nokey=1 "$file" 2>/dev/null)
    audio_codec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name \
        -of default=noprint_wrappers=1:nokey=1 "$file" 2>/dev/null)

    if [[ "$codec" != "h264" ]]; then
        echo "  [output validate] ERROR: expected H.264, found '${codec:-none}': $file" >&2
        return 1
    fi
    if [[ "$pix_fmt" != "$expected_pix" ]]; then
        echo "  [output validate] ERROR: expected $expected_pix, found '${pix_fmt:-unknown}': $file" >&2
        return 1
    fi
    if [[ -z "$actual_crf" ]] || ! awk -v actual_crf="$actual_crf" -v expected_crf="$expected_crf" 'BEGIN { exit (actual_crf == expected_crf) ? 0 : 1 }'; then
        echo "  [output validate] ERROR: expected CRF $expected_crf, found '${actual_crf:-unknown}': $file" >&2
        return 1
    fi
    if [[ "$color_space" != "smpte170m" || "$color_trc" != "smpte170m" \
          || "$color_primaries" != "smpte170m" ]]; then
        echo "  [output validate] ERROR: expected smpte170m color tags, found" \
             "space=${color_space:-unknown} trc=${color_trc:-unknown}" \
             "primaries=${color_primaries:-unknown}: $file" >&2
        return 1
    fi
    if [[ "$audio_codec" != "aac" ]]; then
        echo "  [output validate] ERROR: output audio is '${audio_codec:-missing}', expected AAC: $file" >&2
        return 1
    fi
    return 0
}

validate_source_output_duration() {
    # validate_source_output_duration <source> <enhanced>
    # Fast header-level guard for resumed or manually replaced outputs. Fresh
    # outputs receive the stronger decoded-frame validation inside
    # video_upscale_pipeline.py; this closes the historical/resume gap before
    # files are appended into a day and the individual outputs are deleted.
    local source="$1" enhanced="$2"
    local source_duration enhanced_duration delta
    # The VIDEO stream, not the container. format=duration reports the longest
    # stream, which is the audio: AAC is quantised to 1024-sample frames and
    # carries encoder padding, so it overhangs the picture by up to ~21ms,
    # while the DV source's PCM audio has no such padding. Comparing the two
    # containers therefore compared a PCM-exact length against an AAC-padded
    # one and charged the difference to the video -- 36ms on a capture whose
    # picture count was exact. Measuring the video stream removes the
    # discrepancy rather than widening a tolerance to accommodate it.
    source_duration=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=duration \
        -of default=noprint_wrappers=1:nokey=1 "$source" 2>/dev/null)
    enhanced_duration=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=duration \
        -of default=noprint_wrappers=1:nokey=1 "$enhanced" 2>/dev/null)
    # DV in AVI reports no per-stream duration; fall back to the container,
    # whose PCM audio matches the video exactly for these captures.
    [[ -n $source_duration && $source_duration != "N/A" ]] || source_duration=$(ffprobe -v error \
        -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$source" 2>/dev/null)
    [[ -n $enhanced_duration && $enhanced_duration != "N/A" ]] || enhanced_duration=$(ffprobe -v error \
        -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$enhanced" 2>/dev/null)
    if ! is_seconds "$source_duration" || ! is_seconds "$enhanced_duration"; then
        echo "  [output validate] ERROR: could not compare source/output duration:" >&2
        echo "    Source:   $source" >&2
        echo "    Enhanced: $enhanced" >&2
        return 1
    fi
    delta=$(awk -v enhanced_duration="$enhanced_duration" -v source_duration="$source_duration" 'BEGIN { d = enhanced_duration - source_duration; \
                         if (d < 0) d = -d; printf "%.3f", d }')
    local source_duration_tolerance
    # Measured against the SOURCE's frame rate, not the output's. The output
    # runs at double rate after deinterlacing, so a tolerance expressed in
    # output frames is half the size it appears -- and the discrepancy being
    # measured is container rounding on a source picture, not on half of one.
    # Observed across the reference days: 0.020s to 0.053s, up to 1.6 source
    # pictures, on captures whose frame counts were verified exact.
    source_duration_tolerance=$(frames_to_seconds "$TOLERANCE_FRAMES" \
        "$(get_numeric_frame_rate "$source" 2>/dev/null || echo 29.97)")
    if awk -v delta="$delta" -v tol="$source_duration_tolerance" 'BEGIN { exit (delta > tol) ? 0 : 1 }'; then
        echo "  [output validate] ERROR: enhanced duration differs from source." >&2
        echo "    Source: ${source_duration}s  Enhanced: ${enhanced_duration}s" \
             " Delta: ${delta}s (tolerance: ${source_duration_tolerance}s = ${TOLERANCE_FRAMES} frame)" >&2
        return 1
    fi
    log "  [output validate] Duration OK: source ${source_duration}s," \
        "enhanced ${enhanced_duration}s, delta ${delta}s"
    return 0
}

get_append_signature() {
    # Properties that must agree across every file the assembly appends.
    # CRF is checked separately against the filename policy above. Frame rate
    # is also deliberately excluded: ffprobe's avg_frame_rate is measured from
    # frame count/duration and equivalent CFR files can expose slightly
    # different rational strings due to timestamp rounding. It is compared
    # numerically with a tolerance by get_numeric_frame_rate() below.
    local file="$1"
    ffprobe -v error -show_entries \
stream=codec_type,codec_name,width,height,pix_fmt,color_space,color_transfer,color_primaries,sample_rate,channels \
        -of compact=p=0:nk=0 "$file" 2>/dev/null
}

packet_bounds() {  # packet_bounds <file> <stream-specifier>
    # Prints "<start> <end>": the earliest packet timestamp and the latest
    # packet timestamp plus that packet's duration, both on the file's own
    # timeline.
    #
    # ABSOLUTE bounds, not a span. A span subtracts each stream's own first
    # timestamp, so it is identical for an aligned track and for one shifted
    # by any constant -- exactly the desynchronisation this check exists to
    # find. A half-second audio offset left the span unchanged to within 22ms
    # and passed.
    #
    # Deliberately not the container's stream duration: an append that leaves
    # gaps between clips reports a duration including them, so audio and video
    # disagree on a file whose streams are both intact. Packet timestamps
    # describe the same quantity on both.
    local file=$1 stream=$2
    # No sort: the minimum and maximum are computed directly. Sorting by PTS
    # and taking the last row gives the end of the LATEST-STARTING packet,
    # which is not the same as the greatest pts+duration; they coincide for
    # constant-duration streams and the helper claimed the general case.
    #
    # A field that is not a number fails the measurement rather than being
    # read as zero. ffprobe writes N/A where a value is unavailable, and 0
    # would silently shrink the measured bounds towards the origin -- turning
    # an unreadable file into an apparently well-aligned one.
    ffprobe -v error -select_streams "$stream" \
        -show_entries packet=pts_time,duration_time \
        -of csv=p=0 "$file" 2>/dev/null | awk -F, '
        function numeric(v) { return v ~ /^-?[0-9]+([.][0-9]+)?$/ }
        # ffprobe ends its CSV with a blank line; that is formatting, not a
        # malformed packet, and counting it as one failed every measurement.
        /^[[:space:]]*$/ { next }
        # Exactly two fields. NF < 2 caught a truncated row but let a row with
        # extra fields through, reading its first two and guessing about the
        # rest -- the same laxness the Python side had.
        NF != 2 { bad++; next }
        { if (!numeric($1) || !numeric($2)) { bad++; next }
          pts = $1 + 0; end = pts + $2
          if (seen == 0 || pts < min_start) min_start = pts
          if (seen == 0 || end > max_end)   max_end   = end
          seen++ }
        END { if (seen == 0 || bad > 0) exit 1
              printf "%.6f %.6f", min_start, max_end }'
}

get_numeric_frame_rate() {
    # Prints avg_frame_rate as a decimal. The pipeline produces CFR output, but
    # comparing this value numerically avoids false mismatches between
    # equivalent or near-equivalent rational representations.
    local file="$1" rate
    rate=$(ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate \
        -of default=noprint_wrappers=1:nokey=1 "$file" 2>/dev/null)
    if [[ "$rate" =~ ^([0-9]+)\/([0-9]+)$ && "${BASH_REMATCH[2]}" -ne 0 ]]; then
        awk -v num="${BASH_REMATCH[1]}" -v den="${BASH_REMATCH[2]}" \
            'BEGIN { printf "%.9f", num / den }'
        return 0
    fi
    return 1
}

write_output_readme() {
    # Regenerates <RUN_DIR>/run.md from the run manifest.
    #
    # The rendering lives in pipeline_tools/write_run_md.py, shared with every
    # entry point, so the readable file and run.json are one description of the
    # run rather than two that can drift. It is rebuilt in full on each call, so
    # it stays correct across resumes and reprocessing.
    "$VENV_PYTHON" "${SCRIPT_DIR}/pipeline_tools/write_run_md.py" "$RUN_DIR" >/dev/null
}

assemble_day_chapters() {
    # assemble_day_chapters <day_name> <output_dir> <upscaled_file1> [file2 ...]
    #                       -- <source1> [source2 ...]
    #
    # Concatenates a day's upscaled outputs into one MP4 with chapter marks.
    # Chapter timestamps are derived from the recording start time encoded in
    # each source filename (dv.YYYYMMDD[-_]HHMMSS).
    #
    # The day is assembled in MKV with mkvmerge and muxed to MP4 at the end;
    # the picture is stream-copied throughout and never re-encoded. ffmpeg's
    # concat demuxer is not used: its h264_mp4toannexb filter rewrites the
    # bitstream and loses track header metadata. MP4Box -cat is not used
    # either -- it merges each clip's edit list without accounting for the gap
    # the clip's audio opens, which leaves trailing pictures outside the
    # presented window. See the mux step below.
    # pipeline_tools/repair_mp4_video_edit.py extends such an edit on an
    # existing file; neither this assembly nor the per-capture pipeline runs
    # it, because both check that their own muxing needs no repair.
    #
    # The day's audio is NOT appended from the clips. Each clip carries its own
    # AAC, so appending them accumulates an encoder priming delay and a final
    # block of padding per join. The sources' PCM is concatenated instead --
    # each clip held to its own picture length -- and encoded once.
    #
    # The container is MP4 because everything else the pipeline writes is MP4,
    # and this is the file people actually watch. It carries chapters in a
    # movie-level chapter list -- not a text track, so no player shows a stray
    # subtitle -- at the same measured timestamps the MKV build produced, and
    # `mkvmerge -o day.mkv day.mp4` still yields an MKV, chapters and SAR
    # intact, for any player that wants one.
    #
    # Output: <output_dir>/<day_name>.mp4 -- written directly into the shared
    # output directory, not a per-day subdirectory. Assembly deletes the
    # individual upscaled files it consumed, so a per-day directory would be
    # left holding exactly one file.
    #
    # The name carries no settings token and no "_chapters_" marker. Every
    # assembled day video has chapters, and repeating
    # "dv_x2_x2plus_60fps_crf23_yuv420" in every filename says the same thing
    # once per file; write_output_readme() states it once for the directory
    # instead. The caller still compares the token across a day's files; it
    # just does not end up in the name.
    #
    # Returns 0 on success, 1 on any error (caller logs and marks day failed).

    local day_name="$1"
    local out_dir="$2"
    shift 2
    # Upscaled clips, then "--", then the source capture for each clip in the
    # same order. The sources supply the day's audio: they hold the original
    # PCM (prepare_video.sh runs with --pcm and copies it bit-for-bit), while
    # each clip carries a separately encoded AAC whose priming and padding are
    # exactly what assembling from them accumulates.
    local -a upscaled_files=() source_files=()
    local _seen_sep=false _arg
    for _arg in "$@"; do
        if [[ $_arg == "--" ]]; then _seen_sep=true; continue; fi
        if $_seen_sep; then source_files+=("$_arg"); else upscaled_files+=("$_arg"); fi
    done
    local -A SOURCE_OF=()
    if (( ${#source_files[@]} )); then
        if (( ${#source_files[@]} != ${#upscaled_files[@]} )); then
            echo "  [assemble] ERROR: ${#upscaled_files[@]} clip(s) but ${#source_files[@]} source(s)." >&2
            return 1
        fi
        local _i
        for _i in "${!upscaled_files[@]}"; do
            SOURCE_OF["${upscaled_files[$_i]}"]="${source_files[$_i]}"
        done
    fi

    local day_video="${out_dir}/${day_name}.mp4"
    local chapter_file
    chapter_file=$(mktemp /tmp/chapters_XXXXXX.txt)

    # Guard: require at least one file.
    if [[ ${#upscaled_files[@]} -eq 0 ]]; then
        echo "  [assemble] No upscaled files provided for day $day_name." >&2
        rm -f "$chapter_file"
        return 1
    fi

    # Order by the resolved recording time rather than by filename. For
    # captures whose names carry a timestamp the two orders are identical, but
    # sorting on the name only works because the name happens to start with
    # one -- the recording time is what actually orders the chapters.
    local sorted upscaled dt
    local -a keyed=()
    for upscaled in "${upscaled_files[@]}"; do
        if ! dt=$(output_recording_time "$upscaled"); then
            echo "  [assemble] ERROR: no recording time recorded for:" >&2
            echo "    $(basename "$upscaled")" >&2
            echo "  Pre-flight should have resolved this; re-run to rebuild" >&2
            echo "  $(preflight_table_path)." >&2
            rm -f "$chapter_file"
            return 1
        fi
        keyed+=("${dt}"$'\t'"${upscaled}")
    done
    sorted=$(printf '%s\n' "${keyed[@]}" | sort | cut -f2-)

    # One frame rate for the whole day. The caller has already refused to
    # assemble a day whose clips disagree on frame rate, so reading it from the
    # first clip describes them all.
    local chapter_fps
    chapter_fps=$(get_numeric_frame_rate "${upscaled_files[0]}" 2>/dev/null)
    if [[ -z "$chapter_fps" ]] || ! awk -v f="$chapter_fps" 'BEGIN { exit (f > 0) ? 0 : 1 }'; then
        echo "  [assemble] could not read a frame rate from: ${upscaled_files[0]}" >&2
        rm -f "$chapter_file"
        return 1
    fi

    # Accumulated in PICTURES and converted once, not per clip. Converting each
    # component to milliseconds separately truncates every one of them, and the
    # error accumulates across a day -- forty-four clips could drift by tens of
    # milliseconds before any container effect is considered.
    local cumulative_ms=0
    local cumulative_frames=0
    local -a chapter_names=()
    local -a chapter_frame_index=()
    local -a clip_frames=()
    local -a ordered_files=()
    local chapter_num=0

    while IFS= read -r upscaled; do
        # The recording time was resolved during pre-flight, which reconciled
        # the capture filename against the camcorder's own DV subcode and got
        # the user's decision on anything ambiguous. Look it up rather than
        # re-parsing here: by this point the name has acquired the pipeline's
        # own suffixes, and re-deriving would silently discard whatever the
        # user chose.
        local base
        base=$(basename "$upscaled")
        local rec_dt=""
        rec_dt=$(output_recording_time "$upscaled") || rec_dt=""
        if [[ -z "$rec_dt" ]]; then
            echo "  [assemble] ERROR: no recording time recorded for:" >&2
            echo "    $base" >&2
            echo "  Pre-flight should have resolved this. Re-run to rebuild" >&2
            echo "  the recording-time table: $(preflight_table_path)." >&2
            rm -f "$chapter_file"
            return 1
        fi

        # Count decoded component pictures, not audio-padded container
        # durations or packets that an edit list may hide. These counts locate
        # each clip's first picture in the joined stream. Its measured PTS,
        # rather than count/fps, determines the chapter time after appending.
        local dur_frames
        dur_frames=$(ffprobe -v error -select_streams v:0 -count_frames \
            -show_entries stream=nb_read_frames \
            -of default=noprint_wrappers=1:nokey=1 "$upscaled" 2>/dev/null)
        if [[ ! "$dur_frames" =~ ^[0-9]+$ ]] || (( dur_frames == 0 )); then
            echo "  [assemble] could not count pictures in: $upscaled" >&2
            rm -f "$chapter_file"
            return 1
        fi
        # Chapter POSITIONS are not written yet. Where a clip's first picture
        # lands in the merged file is a property of the merge, and cannot be
        # known before it happens: appending does not guarantee that clip N
        # begins exactly at the sum of the preceding clips' picture counts.
        # Audio block padding and input timestamps can move a boundary, and a
        # calculated mark then names a moment slightly inside the previous
        # clip. Verified against a post-mux calculation, that error is
        # invisible -- both sides come from the same arithmetic.
        #
        # What IS known here is the running picture index at which each clip
        # starts. After the merge, the timestamp of that picture is read out of
        # the merged file and used as the chapter position.
        (( chapter_num++ )) || true
        chapter_names+=("$rec_dt")
        chapter_frame_index+=("$cumulative_frames")
        clip_frames+=("$dur_frames")
        ordered_files+=("$upscaled")
        cumulative_frames=$(( cumulative_frames + dur_frames ))
        cumulative_ms=$(awk -v f="$cumulative_frames" -v fps="$chapter_fps" \
            'BEGIN { printf "%d", f * 1000 / fps }')

    done <<< "$sorted"

    log "  [assemble] $day_name: ${#ordered_files[@]} files, total ${cumulative_ms}ms"
    log "  [assemble] Output: $day_video"

    if [[ "$DRY_RUN" == true ]]; then
        log "  [assemble] DRY RUN — would create: $day_video"
        rm -f "$chapter_file"
        return 0
    fi

    # ----------------------------------------------------------------
    # Display dimensions come from the first input file's own pixel dimensions
    # and SAR, never from a constant.
    #
    # A constant would only be right for one combination -- 720x480 at --scale 2
    # with 8:9 SAR gives 1280x960 -- and --scale 4 is equally valid here, so any
    # other scale would write the wrong display dimensions on every assembled
    # day. The verification below must derive its expectation from the same real
    # file for the same reason: checked against a constant it would confirm the
    # wrong number rather than catch it.
    # ----------------------------------------------------------------
    local pix_w pix_h
    pix_w=$(ffprobe -v error -select_streams v:0 -show_entries stream=width \
        -of default=noprint_wrappers=1:nokey=1 "${ordered_files[0]}" 2>/dev/null)
    pix_h=$(ffprobe -v error -select_streams v:0 -show_entries stream=height \
        -of default=noprint_wrappers=1:nokey=1 "${ordered_files[0]}" 2>/dev/null)
    if [[ -z "$pix_w" || -z "$pix_h" ]]; then
        echo "  [assemble] ERROR: could not read pixel dimensions from: ${ordered_files[0]}" >&2
        rm -f "$chapter_file"
        return 1
    fi

    local sar_str
    sar_str=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=sample_aspect_ratio \
        -of default=noprint_wrappers=1:nokey=1 "${ordered_files[0]}" 2>/dev/null)

    local display_w="$pix_w" sar_par=
    if [[ -n "$sar_str" && "$sar_str" != "N/A" && "$sar_str" != "0:1" && "$sar_str" != "1:1" ]]; then
        local sar_num="${sar_str%%:*}"
        local sar_den="${sar_str##*:}"
        if [[ ! "$sar_num" =~ ^[0-9]+$ || ! "$sar_den" =~ ^[0-9]+$ || "$sar_den" -eq 0 ]]; then
            echo "  [assemble] ERROR: could not parse SAR '$sar_str' from: ${ordered_files[0]}" >&2
            rm -f "$chapter_file"
            return 1
        fi
        display_w=$(awk -v pix_w="$pix_w" -v sar_num="$sar_num" -v sar_den="$sar_den" 'BEGIN { printf "%d", pix_w * sar_num / sar_den + 0.5 }')
        sar_par="${sar_num}:${sar_den}"
    fi
    local expected_display_dims="${display_w}x${pix_h}"
    log "  [assemble] Pixel dims: ${pix_w}x${pix_h}  SAR: ${sar_str:-1:1 (square)}" \
        " -> display: $expected_display_dims"

    # Assemble in MKV, encode the day's audio once, then mux the delivery MP4.
    # Chapters are applied afterwards, once the merged timeline can be measured.
    #
    # MP4Box -cat merges each clip's edit list by adding the incoming video
    # media duration without accounting for the gap its audio opens, so the
    # video edit ends short of the samples and trailing pictures are not
    # presented (pipeline_tools/repair_mp4_video_edit.py describes the fault).
    # mkvmerge append has no such step.
    #
    # The audio is rebuilt rather than appended. Every clip carries its own
    # AAC, so appending clips accumulates one encoder priming delay and one
    # final-block padding PER JOIN -- measured at 33ms to 53ms each, and a
    # constant ~32ms audio lag on top. Concatenating the sources' PCM and
    # encoding once leaves a single priming delay for the muxer to compensate.
    local day_mkv="${out_dir}/.${day_name}.assembly.mkv"
    local day_pcm="${out_dir}/.${day_name}.assembly.pcm"
    local day_aac="${out_dir}/.${day_name}.assembly.m4a"
    rm -f "$day_mkv" "$day_pcm" "$day_aac"

    local -a mkv_cmd=( mkvmerge --quiet --output "$day_mkv" )
    [[ -n "$sar_par" ]] && mkv_cmd+=( --display-dimensions "0:${expected_display_dims}" )
    local f first=true
    for f in "${ordered_files[@]}"; do
        $first || mkv_cmd+=( "+" )
        mkv_cmd+=( --no-audio "$f" )
        first=false
    done
    local mkv_rc=0
    "${mkv_cmd[@]}" 2>&1 | while IFS= read -r line; do echo "    [mkvmerge] $line"; done
    mkv_rc=${PIPESTATUS[0]}
    # mkvmerge exits 1 for warnings (output still usable) and 2 for errors.
    if (( mkv_rc >= 2 )); then
        echo "  [assemble] ERROR: mkvmerge append failed (exit $mkv_rc) for $day_name." >&2
        rm -f "$chapter_file" "$day_mkv"
        return 1
    fi

    # Day audio from the sources' PCM, each clip padded or trimmed to its own
    # picture length. Laying clips end to end instead would let a sub-frame
    # per-clip error accumulate: half a millisecond each is 22ms adrift by the
    # 44th clip, and the drift grows with the day. Matching every clip to its
    # pictures holds each one's audio to its own start, whatever the count.
    local audio_rate audio_ch
    audio_rate=$(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate \
        -of default=nw=1:nk=1 "${SOURCE_OF[${ordered_files[0]}]}" 2>/dev/null)
    audio_ch=$(ffprobe -v error -select_streams a:0 -show_entries stream=channels \
        -of default=nw=1:nk=1 "${SOURCE_OF[${ordered_files[0]}]}" 2>/dev/null)
    if [[ ! $audio_rate =~ ^[0-9]+$ || ! $audio_ch =~ ^[0-9]+$ ]]; then
        echo "  [assemble] ERROR: could not read source audio format for $day_name." >&2
        rm -f "$chapter_file" "$day_mkv"; return 1
    fi
    # Measure the extracted PCM itself rather than a container duration.
    # ffprobe reports N/A for a PCM-in-AVI stream duration, and format duration
    # is the longest stream -- so it cannot see audio SHORTER than the picture,
    # which is the case this check exists to catch. The decoded byte count is
    # exact and container-independent, and the extraction has to happen anyway.
    local av_tolerance total_adjust_ms=0 idx bytes_per_sample
    bytes_per_sample=$(( 2 * audio_ch ))
    av_tolerance=$(frames_to_seconds "$TOLERANCE_FRAMES" \
        "$(get_numeric_frame_rate "${SOURCE_OF[${ordered_files[0]}]}" 2>/dev/null || echo 29.97)")
    local clip_pcm="${out_dir}/.${day_name}.clip.pcm"
    : > "$day_pcm"
    for idx in "${!ordered_files[@]}"; do
        local src="${SOURCE_OF[${ordered_files[$idx]}]}"
        local vdur want_bytes have_bytes delta
        vdur=$(awk -v n="${clip_frames[$idx]}" -v fps="$chapter_fps" \
            'BEGIN { printf "%.6f", n / fps }')
        # -map 0:a:0 pins this to the same track the format above was read
        # from. Without it ffmpeg picks the "best" audio stream, which is the
        # default-flagged one -- so a capture carrying a second audio track
        # would deliver that track's sound while every length check, taken
        # from a:0, still passed.
        if ! ffmpeg -nostdin -v error -i "$src" -map 0:a:0 -vn \
             -ac "$audio_ch" -ar "$audio_rate" \
             -c:a pcm_s16le -f s16le -y "$clip_pcm" 2>/dev/null; then
            echo "  [assemble] ERROR: could not read source audio: $src" >&2
            rm -f "$chapter_file" "$day_mkv" "$day_pcm" "$clip_pcm"; return 1
        fi
        have_bytes=$(stat -c %s "$clip_pcm")
        want_bytes=$(awk -v d="$vdur" -v r="$audio_rate" -v b="$bytes_per_sample" \
            'BEGIN { printf "%d", int(d * r + 0.5) * b }')
        delta=$(awk -v h="$have_bytes" -v w="$want_bytes" -v r="$audio_rate" -v b="$bytes_per_sample" \
            'BEGIN { d = (h - w) / b / r; if (d < 0) d = -d; printf "%.6f", d }')
        if awk -v d="$delta" -v t="$av_tolerance" 'BEGIN { exit (d > t) ? 0 : 1 }'; then
            echo "  [assemble] ERROR: $(basename "$src"): audio and pictures differ by ${delta}s," >&2
            echo "    beyond ${av_tolerance}s (${TOLERANCE_FRAMES} source frame)." >&2
            echo "    Pictures ${vdur}s; audio $(awk -v h="$have_bytes" -v r="$audio_rate" -v b="$bytes_per_sample" 'BEGIN{printf "%.6f", h/b/r}')s." >&2
            echo "    DV interleaves audio with every frame, so a gap this size means the" >&2
            echo "    capture's audio and picture genuinely disagree. Extract the audio per" >&2
            echo "    DV frame from the raw capture and re-check before assembling this day." >&2
            rm -f "$chapter_file" "$day_mkv" "$day_pcm" "$clip_pcm"; return 1
        fi
        total_adjust_ms=$(awk -v t="$total_adjust_ms" -v d="$delta" 'BEGIN { printf "%.3f", t + d * 1000 }')
        # Hold every clip's audio to its own picture length, so a sub-frame
        # error stays with its clip instead of moving each later clip.
        if (( have_bytes > want_bytes )); then
            head -c "$want_bytes" "$clip_pcm" >> "$day_pcm"
        else
            cat "$clip_pcm" >> "$day_pcm"
            dd if=/dev/zero bs=1 count=$(( want_bytes - have_bytes )) \
                >> "$day_pcm" 2>/dev/null
        fi
    done
    rm -f "$clip_pcm"
    log "  [assemble] Day audio: ${#ordered_files[@]} source clip(s) held to their pictures," \
        "${total_adjust_ms}ms total adjustment (tolerance ${av_tolerance}s per clip)."

    if ! ffmpeg -nostdin -v error -f s16le -ar "$audio_rate" -ac "$audio_ch" -i "$day_pcm" \
         -c:a aac -b:a 192k -y "$day_aac" 2>/dev/null; then
        echo "  [assemble] ERROR: could not encode the day's audio for $day_name." >&2
        rm -f "$chapter_file" "$day_mkv" "$day_pcm" "$day_aac"; return 1
    fi
    rm -f "$day_pcm"

    if ! ffmpeg -nostdin -v error -i "$day_mkv" -i "$day_aac" -map 0:v:0 -map 1:a:0 \
         -c copy -movflags +faststart -y "$day_video" 2>/dev/null; then
        echo "  [assemble] ERROR: could not mux the delivery MP4 for $day_name." >&2
        rm -f "$chapter_file" "$day_mkv" "$day_aac" "$day_video"; return 1
    fi
    rm -f "$day_mkv" "$day_aac" "$chapter_file"

    # Pixel aspect ratio, the counterpart of mkvmerge's --display-dimensions.
    # -cat carries the sources' SAR through, so this states it rather than
    # repairs it -- and fails loudly if it could not be written.

    # ----------------------------------------------------------------
    # Chapter positions, read from the merged file rather than predicted.
    #
    # Every picture from every component survives the append in order, so the
    # first picture of clip N is picture number chapter_frame_index[N] of the
    # merged file. Its presentation timestamp is where the chapter belongs.
    # Reading it makes the mark describe the file that exists, instead of
    # describing an append behaviour that was assumed.
    # ----------------------------------------------------------------
    local merged_pts_file
    merged_pts_file=$(mktemp "${TMPDIR:-/tmp}/merged_pts_XXXXXX.txt") || return 1
    if ! ffprobe -v error -select_streams v:0 -show_entries packet=pts_time \
        -of csv=p=0 "$day_video" 2>/dev/null | sort -n > "$merged_pts_file"; then
        echo "  [assemble] ERROR: could not read presentation timestamps from: $day_video" >&2
        rm -f "$chapter_file" "$merged_pts_file"; [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi
    local merged_frames
    merged_frames=$(wc -l < "$merged_pts_file")
    if (( merged_frames != cumulative_frames )); then
        echo "  [assemble] ERROR: merged file holds $merged_frames pictures," \
             "components held $cumulative_frames." >&2
        rm -f "$chapter_file" "$merged_pts_file"; [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi

    : > "$chapter_file"
    local -a measured_chapter_starts=()
    local ci
    for ci in "${!chapter_frame_index[@]}"; do
        local idx=$(( chapter_frame_index[ci] + 1 ))   # sed is 1-based
        local pts
        pts=$(sed -n "${idx}p" "$merged_pts_file")
        if [[ ! "$pts" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
            echo "  [assemble] ERROR: no timestamp for picture ${chapter_frame_index[ci]}" >&2
            rm -f "$chapter_file" "$merged_pts_file"; [[ -f "$day_video" ]] && rm -f "$day_video"
            return 1
        fi
        measured_chapter_starts+=("$pts")
        local ts_h ts_m ts_s ts_ms total_ms
        total_ms=$(awk -v p="$pts" 'BEGIN { printf "%d", p * 1000 }')
        ts_h=$(( total_ms / 3600000 )); ts_m=$(( (total_ms % 3600000) / 60000 ))
        ts_s=$(( (total_ms % 60000) / 1000 )); ts_ms=$(( total_ms % 1000 ))
        # MP4Box's chapter form: one "HH:MM:SS.mmm Name" per line.
        printf "%02d:%02d:%02d.%03d %s\n" "$ts_h" "$ts_m" "$ts_s" "$ts_ms" \
            "${chapter_names[ci]}" >> "$chapter_file"
    done
    rm -f "$merged_pts_file"

    if ! MP4Box -quiet -chap "$chapter_file" "$day_video" >/dev/null 2>&1; then
        echo "  [assemble] ERROR: MP4Box could not write chapters into: $day_video" >&2
        rm -f "$chapter_file"; [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi
    rm -f "$chapter_file"
    log "  [assemble] Chapters written from measured timestamps of the merged file."

    # Check what a player actually decodes after ALL metadata operations.
    # Packet counts do not detect pictures hidden by an edit list.
    #
    # Nothing repairs the edit window before this. This path must write a
    # correct one itself, and running pipeline_tools/repair_mp4_video_edit.py
    # here would paper over a muxing regression rather than surface it -- the
    # gate would then pass on every file the repair could rescue, which is
    # exactly the class of fault it exists to catch. A failure leaves the
    # day's components in place for investigation (the caller only deletes
    # them after this function returns successfully).
    local decoded_frames
    decoded_frames=$(ffprobe -v error -select_streams v:0 -count_frames \
        -show_entries stream=nb_read_frames -of csv=p=0 "$day_video")
    if [[ ! "$decoded_frames" =~ ^[0-9]+$ ]] || (( decoded_frames != cumulative_frames )); then
        echo "  [assemble] ERROR: final file decodes ${decoded_frames:-unknown} pictures; components decoded $cumulative_frames." >&2
        rm -f "$day_video"
        return 1
    fi
    log "  [assemble] Decoded pictures OK: $decoded_frames (matches components)."

    # --- Verification ---
    # All checks below must pass before we declare success and allow the
    # caller to delete the individual input files.

    # 1. Readable video stream.
    if ! ffprobe -v error -select_streams v:0 \
            -show_entries stream=codec_type \
            -of default=noprint_wrappers=1:nokey=1 \
            "$day_video" 2>/dev/null | grep -q video; then
        echo "  [assemble] ERROR: assembled day video has no readable video stream: $day_video" >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi

    # 1b. Exactly one AAC track, and audio that spans the same timeline as the
    # picture. The components are deleted immediately after this function
    # returns, so an append that dropped or truncated audio would be
    # unrecoverable and, until now, unreported: every other check here looks
    # only at the video.
    #
    # Absolute packet bounds, not stream durations. A container reports a
    # stream duration including any gaps left at append boundaries, which
    # makes it unusable for the frames-divided-by-fps comparison the rest of
    # this function relies on. Packet timestamps describe the same thing on
    # both streams -- and being absolute, they show an offset that a span,
    # measured from each stream's own first packet, cannot.
    local audio_codecs audio_track_count
    audio_codecs=$(ffprobe -v error -select_streams a \
        -show_entries stream=codec_name \
        -of default=noprint_wrappers=1:nokey=1 "$day_video" 2>/dev/null)
    audio_track_count=$(grep -c . <<<"$audio_codecs")
    if (( audio_track_count != 1 )) || [[ "$audio_codecs" != "aac" ]]; then
        echo "  [assemble] ERROR: expected exactly one AAC track, found" \
             "${audio_track_count} (${audio_codecs:-none}): $day_video" >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi

    local v_bounds a_bounds v_start v_end a_start a_end
    v_bounds=$(packet_bounds "$day_video" v:0) || v_bounds=""
    a_bounds=$(packet_bounds "$day_video" a:0) || a_bounds=""
    read -r v_start v_end <<<"$v_bounds"
    read -r a_start a_end <<<"$a_bounds"
    if ! is_seconds "$v_start" || ! is_seconds "$v_end" \
       || ! is_seconds "$a_start" || ! is_seconds "$a_end"; then
        echo "  [assemble] ERROR: could not measure packet bounds" \
             "(video='${v_bounds:-unreadable}' audio='${a_bounds:-unreadable}'): $day_video" >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi

    # One video frame plus one AAC block, for the finished file -- not
    # multiplied by the number of components. Appending does not accumulate
    # offset: measured endpoints agree within a few milliseconds regardless of
    # how many clips went in, while a per-component allowance reached 1.67s
    # across 44 clips and would have hidden real truncation. An aligned file
    # still needs the block: AAC priming puts the first audio packet slightly
    # before zero.
    # The block is 1024 samples, but a block's DURATION depends on the rate.
    # prepare_video.sh encodes AAC without forcing -ar, so the master keeps the
    # source's rate: DV carries 32kHz as well as 48kHz, and at 32kHz a block is
    # 32ms rather than 21.3ms. Hardcoding 48000 made the tolerance 38ms where
    # 48.7ms was intended, so a correctly aligned 32kHz day would be rejected.
    local audio_rate av_tolerance start_delta end_delta
    audio_rate=$(ffprobe -v error -select_streams a:0 \
        -show_entries stream=sample_rate \
        -of default=noprint_wrappers=1:nokey=1 "$day_video" 2>/dev/null)
    if [[ ! "$audio_rate" =~ ^[0-9]+$ ]] || (( audio_rate == 0 )); then
        echo "  [assemble] ERROR: could not read the audio sample rate from: $day_video" >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi
    av_tolerance=$(awk -v fps="$chapter_fps" -v rate="$audio_rate" \
        'BEGIN { printf "%.6f", 1 / fps + 1024 / rate }')
    start_delta=$(awk -v a="$v_start" -v b="$a_start" \
        'BEGIN { d = a - b; if (d < 0) d = -d; printf "%.6f", d }')
    end_delta=$(awk -v a="$v_end" -v b="$a_end" \
        'BEGIN { d = a - b; if (d < 0) d = -d; printf "%.6f", d }')
    local which=""
    awk -v d="$start_delta" -v t="$av_tolerance" 'BEGIN { exit (d > t) ? 0 : 1 }' && which="start"
    awk -v d="$end_delta" -v t="$av_tolerance" 'BEGIN { exit (d > t) ? 0 : 1 }' && \
        which="${which:+$which and }end"
    if [[ -n "$which" ]]; then
        echo "  [assemble] ERROR: audio and video do not share one timeline (${which})." >&2
        echo "    video ${v_start}s..${v_end}s  audio ${a_start}s..${a_end}s" >&2
        echo "    start delta ${start_delta}s  end delta ${end_delta}s" \
             "tolerance ${av_tolerance}s" >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi
    log "  [assemble] Audio OK: one AAC track at ${audio_rate}Hz, start within" \
        "${start_delta}s and end within ${end_delta}s of the picture" \
        "(tolerance ${av_tolerance}s)."

    # 2. Duration matches the summed component durations. Allow 10 ms per
    # append boundary with a 100 ms floor for container timestamp rounding.
    local actual_dur_sec
    # Measured the same way the expectation was computed: pictures divided by
    # frame rate. The expectation is the sum of the components' picture counts,
    # so reading the container's duration here would compare a picture count
    # against an AAC-padded audio length -- which is precisely how the chapter
    # marks came to be five frames late. Like against like, and the comparison
    # is then exact rather than approximately right.
    local actual_frames
    actual_frames=$(ffprobe -v error -select_streams v:0 -count_packets \
        -show_entries stream=nb_read_packets \
        -of default=noprint_wrappers=1:nokey=1 "$day_video" 2>/dev/null)
    if [[ ! "$actual_frames" =~ ^[0-9]+$ ]] || (( actual_frames == 0 )); then
        echo "  [assemble] ERROR: could not count pictures in day video: $day_video" >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi
    actual_dur_sec=$(awk -v frames="$actual_frames" -v fps="$chapter_fps" \
        'BEGIN { printf "%.6f", frames / fps }')
    if ! is_seconds "$actual_dur_sec"; then
        echo "  [assemble] ERROR: could not read duration from day video: $day_video" >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi
    local expected_dur_sec
    expected_dur_sec=$(awk -v cumulative_ms="$cumulative_ms" 'BEGIN { printf "%.3f", cumulative_ms / 1000 }')
    local assembly_dur_tolerance
    # One picture per component. A rule in milliseconds -- ten per component,
    # floored at 0.100s, say -- is under half a picture each and then three
    # pictures at the floor, so it lands tighter or looser than intended
    # depending only on how many clips the day holds.
    # Frame rate read from the assembled file itself rather than inherited
    # from the caller's scope: bash would resolve a caller's local here, but a
    # tolerance that silently depends on where the function is called from is
    # the kind of thing that breaks when the caller changes.
    local assembly_fps
    assembly_fps=$(get_numeric_frame_rate "$day_video" 2>/dev/null) || assembly_fps=29.97
    assembly_dur_tolerance=$(frames_to_seconds \
        "$(( ${#ordered_files[@]} * TOLERANCE_FRAMES ))" "$assembly_fps")
    local dur_delta
    dur_delta=$(awk -v actual_dur_sec="$actual_dur_sec" -v expected_dur_sec="$expected_dur_sec" 'BEGIN { d = actual_dur_sec - expected_dur_sec; \
                             if (d < 0) d = -d; printf "%.3f", d }')
    local dur_ok
    dur_ok=$(awk -v dur_delta="$dur_delta" -v assembly_dur_tolerance="$assembly_dur_tolerance" 'BEGIN { print (dur_delta <= assembly_dur_tolerance) ? "yes" : "no" }')
    if [[ "$dur_ok" != "yes" ]]; then
        echo "  [assemble] ERROR: day video duration mismatch." >&2
        echo "    Expected: ${expected_dur_sec}s  Actual: ${actual_dur_sec}s" \
             " Delta: ${dur_delta}s (tolerance: ${assembly_dur_tolerance}s)" >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi
    log "  [assemble] Frame-derived duration OK: ${actual_dur_sec}s (pictures/fps;" \
        "expected ${expected_dur_sec}s, delta ${dur_delta}s, tolerance ${assembly_dur_tolerance}s)"

    # pictures/fps is not what a player spends on the file. -cat holds the last
    # picture of each clip while that clip's audio finishes, so the presented
    # span runs longer than the picture count implies, by the sum of those
    # holds -- 33ms to 53ms per join across the days measured, accumulating
    # with clip count. Both figures are useful, but only one is the played
    # length, and the check above compares two frame-derived numbers, so the
    # difference between them is invisible to it by construction. Reported,
    # not enforced: the holds are how -cat keeps each clip's audio and picture
    # starting together, so a tolerance here would fail correct assemblies.
    local span_sec span_excess
    span_sec=$(ffprobe -v error -select_streams v:0 -show_entries stream=duration \
        -of default=noprint_wrappers=1:nokey=1 "$day_video" 2>/dev/null) || span_sec=""
    if is_seconds "$span_sec"; then
        span_excess=$(awk -v s="$span_sec" -v f="$actual_dur_sec" \
            'BEGIN { printf "%+.1f", (s - f) * 1000 }')
        # Signed, and worded for either sign: a positive figure is time held at
        # joins, a negative one is the span landing just inside pictures/fps
        # through container rounding.
        log "  [assemble] Presented span: ${span_sec}s vs ${actual_dur_sec}s from" \
            "pictures/fps (${span_excess}ms over $(( ${#ordered_files[@]} - 1 )) join(s))."
    else
        log "  [assemble] WARNING: could not read the presented span from: $day_video"
    fi

    # 3. Chapter count matches the number of input files.
    local actual_chapters
    actual_chapters=$(ffprobe -v error -show_chapters \
        "$day_video" 2>/dev/null \
        | grep -c '^\[CHAPTER\]' || true)
    local expected_chapters=${#ordered_files[@]}
    if [[ "$actual_chapters" -ne "$expected_chapters" ]]; then
        echo "  [assemble] ERROR: chapter count mismatch." >&2
        echo "    Expected: $expected_chapters  Actual: $actual_chapters" >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi
    log "  [assemble] Chapters OK: $actual_chapters chapter(s) verified."

    # 4. Chapter starts match the cumulative component boundaries. A correct
    # count alone would not catch chapters shifted onto the wrong clip.
    local -a actual_chapter_starts=()
    mapfile -t actual_chapter_starts < <(
        ffprobe -v error -show_chapters \
            -show_entries chapter=start_time:chapter_tags= \
            -of csv=p=0 "$day_video" 2>/dev/null
    )
    local chapter_start_tolerance
    chapter_start_tolerance=$(frames_to_seconds "$TOLERANCE_FRAMES" "$assembly_fps")
    local chapter_index chapter_delta
    # Compares the chapters READ BACK from the finished file against the
    # positions MEASURED from its picture timestamps before they were written.
    # Two different arrays, deliberately: actual_chapter_starts comes from
    # ffprobe reading the muxed chapters, measured_chapter_starts from the
    # packet timeline. An earlier revision of this loop had both sides naming
    # the same array, so every delta was zero by construction and the check
    # confirmed nothing.
    if (( ${#actual_chapter_starts[@]} != ${#measured_chapter_starts[@]} )); then
        echo "  [assemble] ERROR: file has ${#actual_chapter_starts[@]} chapters," \
             "${#measured_chapter_starts[@]} were written." >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi
    for chapter_index in "${!measured_chapter_starts[@]}"; do
        if [[ ! "${actual_chapter_starts[$chapter_index]:-}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
            echo "  [assemble] ERROR: invalid chapter start returned by ffprobe at chapter $((chapter_index + 1)):" >&2
            echo "    '${actual_chapter_starts[$chapter_index]:-missing}'" >&2
            [[ -f "$day_video" ]] && rm -f "$day_video"
            return 1
        fi
        chapter_delta=$(awk -v actual="${actual_chapter_starts[$chapter_index]}" \
                            -v expected="${measured_chapter_starts[$chapter_index]}" \
            'BEGIN { d = actual - expected; if (d < 0) d = -d; printf "%.6f", d }')
        if awk -v chapter_delta="$chapter_delta" -v tol="$chapter_start_tolerance" \
            'BEGIN { exit (chapter_delta > tol) ? 0 : 1 }'; then
            echo "  [assemble] ERROR: chapter start mismatch at chapter $((chapter_index + 1))." >&2
            echo "    Expected: ${measured_chapter_starts[$chapter_index]}s" \
                 " Actual: ${actual_chapter_starts[$chapter_index]}s" \
                 " Delta: ${chapter_delta}s (tolerance: ${chapter_start_tolerance}s = ${TOLERANCE_FRAMES} frame)" >&2
            [[ -f "$day_video" ]] && rm -f "$day_video"
            return 1
        fi
    done
    log "  [assemble] Chapter starts OK: all boundaries within ${chapter_start_tolerance}s (${TOLERANCE_FRAMES} frame)."

    # 5. Aspect ratio carried into the assembled file.
    #
    # MP4 stores a pixel aspect ratio, where MKV stored display dimensions, so
    # this reads the SAR back and derives the display width the same way the
    # expectation was computed. Checking the derived dimensions rather than the
    # raw ratio keeps the assertion in the terms the log reports and catches a
    # SAR that was written but does not mean what it should.
    local actual_sar actual_display_w actual_display_dims
    actual_sar=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=sample_aspect_ratio \
        -of default=noprint_wrappers=1:nokey=1 "$day_video" 2>/dev/null)
    actual_display_w="$pix_w"
    if [[ -n "$actual_sar" && "$actual_sar" != "N/A" && "$actual_sar" != "0:1" \
          && "$actual_sar" != "1:1" ]]; then
        local a_num="${actual_sar%%:*}" a_den="${actual_sar##*:}"
        if [[ ! "$a_num" =~ ^[0-9]+$ || ! "$a_den" =~ ^[0-9]+$ || "$a_den" -eq 0 ]]; then
            echo "  [assemble] ERROR: unreadable SAR '$actual_sar' in: $day_video" >&2
            [[ -f "$day_video" ]] && rm -f "$day_video"
            return 1
        fi
        actual_display_w=$(awk -v pix_w="$pix_w" -v n="$a_num" -v d="$a_den" \
            'BEGIN { printf "%d", pix_w * n / d + 0.5 }')
    fi
    actual_display_dims="${actual_display_w}x${pix_h}"
    if [[ "$actual_display_dims" != "$expected_display_dims" ]]; then
        echo "  [assemble] ERROR: aspect ratio not carried into the assembled file." >&2
        echo "    Expected display: $expected_display_dims  Actual: $actual_display_dims" \
             "(SAR ${actual_sar:-not set})" >&2
        [[ -f "$day_video" ]] && rm -f "$day_video"
        return 1
    fi
    log "  [assemble] Display dimensions OK: $actual_display_dims (SAR ${actual_sar:-1:1})"

    log "  [assemble] $day_name: assembled day video verified OK: $(basename "$day_video")"
    return 0
}

# Preparation's default intermediate quality, shared by prefetch and sync
# paths (--mask 0 --pcm --yes). Validate resumed masters against these values.
MASTER_EXPECTED_CRF=12
MASTER_EXPECTED_PIX_FMT="yuv444p"
MASTER_EXPECTED_COLOR="smpte170m"

get_master_crf() {
    # get_master_crf <file>
    # Reads the embedded x264 CRF the same way prepare_video.sh's own
    # post-encode check and video_upscale_pipeline.py's get_embedded_x264_crf
    # do: mediainfo --Full first (reads container structure directly), falling
    # back to a 256KB-bounded raw scan only if mediainfo isn't installed.
    # Echoes the CRF as a bare number, or nothing if it could not be read.
    local file="$1"
    local crf=""
    if command -v mediainfo &>/dev/null; then
        crf=$(mediainfo --Full "$file" 2>/dev/null \
            | grep -i -A1 "Encoding settings" \
            | grep -oP 'crf=\K[0-9]+(\.[0-9]+)?' | head -1)
    fi
    if [[ -z "$crf" ]]; then
        crf=$(head -c 256K "$file" 2>/dev/null | grep -a -oP 'crf=\K[0-9]+(\.[0-9]+)?' | head -1)
    fi
    echo "$crf"
}

get_master_pix_fmt() {
    # get_master_pix_fmt <file>
    local file="$1"
    ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt \
        -of default=noprint_wrappers=1:nokey=1 "$file" 2>/dev/null
}

validate_master() {
    # validate_master <master_file> <src_abs>
    #
    # Confirms that a prepared master MKV is valid by checking:
    #   1. The file exists and is non-empty.
    #   2. ffprobe can read a video stream from it.
    #   3. Its duration is within one picture of the source AVI duration.
    #   4. Its embedded CRF is at least as good as MASTER_EXPECTED_CRF.
    #   5. Its pix_fmt matches MASTER_EXPECTED_PIX_FMT exactly.
    #   6. All colorimetry tags are smpte170m.
    #   7. Its source PCM audio is present and remains PCM.
    #
    # Checks 4-5 exist because this function gates every path that lets the
    # wrapper reuse an on-disk master instead of re-running prepare_video.sh:
    # normal resume ("prepared" state), the prefetch/sync paths right after
    # prepare_video.sh runs, and the orphaned-master recovery pass in section
    # 6a. Without them a master whose CRF or chroma siting does not meet the
    # current policy -- one carried over from a different setting, or encoded
    # with an explicit --crf override -- would be silently accepted and fed to
    # ESRGAN as if it did. Every other stage treats exactly this mismatch as
    # fatal (prepare_video.sh's own post-encode check,
    # video_upscale_pipeline.py's check_output_crf/check_output_pix_fmt).
    #
    # Returns 0 on success, 1 on any failure (caller should treat master
    # as unusable and re-run prepare_video.sh).
    local master="$1"
    local src="$2"

    # 1. Exists and non-empty.
    if [[ ! -f "$master" || ! -s "$master" ]]; then
        echo "  [validate] ERROR: master file missing or empty: $master" >&2
        return 1
    fi

    # 2. Readable video stream.
    if ! ffprobe -v error -select_streams v:0 \
            -show_entries stream=codec_type \
            -of default=noprint_wrappers=1:nokey=1 \
            "$master" 2>/dev/null | grep -q video; then
        echo "  [validate] ERROR: master has no readable video stream: $master" >&2
        return 1
    fi

    # 3. Duration within 100 ms of source.
    local src_dur
    src_dur=$(ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$src" 2>/dev/null)
    if [[ -z "$src_dur" ]]; then
        echo "  [validate] ERROR: could not read duration from source: $src" >&2
        return 1
    fi
    local master_dur
    master_dur=$(ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$master" 2>/dev/null)
    if ! is_seconds "$master_dur" || ! is_seconds "$src_dur"; then
        echo "  [validate] ERROR: could not read duration from master: $master" >&2
        return 1
    fi
    local delta
    delta=$(awk -v master_dur="$master_dur" -v src_dur="$src_dur" 'BEGIN { d = master_dur - src_dur; \
                         if (d < 0) d = -d; printf "%.3f", d }')
    local ok
    # Tolerance computed here rather than borrowed from another function's
    # locals. Under set -u, expanding a name that belongs to a different
    # function aborts the shell instead of returning failure to the caller's
    # re-preparation handling -- and in a diagnostic that only runs when
    # something is already wrong, that is exactly where it would happen.
    local master_tolerance
    master_tolerance=$(frames_to_seconds "$TOLERANCE_FRAMES" \
        "$(get_numeric_frame_rate "$src" 2>/dev/null || echo 29.97)")
    ok=$(awk -v delta="$delta" -v tol="$master_tolerance" \
        'BEGIN { print (delta <= tol) ? "yes" : "no" }')
    if [[ "$ok" != "yes" ]]; then
        echo "  [validate] ERROR: master duration mismatch." >&2
        echo "    Source:  ${src_dur}s  Master: ${master_dur}s" \
             " Delta: ${delta}s (tolerance: ${master_tolerance}s = ${TOLERANCE_FRAMES} frame)" >&2
        return 1
    fi

    # 4. Embedded CRF at least as good as expected (higher CRF = lower quality).
    local actual_crf
    actual_crf=$(get_master_crf "$master")
    if [[ -z "$actual_crf" ]]; then
        echo "  [validate] ERROR: could not read embedded CRF from x264 master: $master" >&2
        return 1
    elif awk -v actual_crf="$actual_crf" -v master_expected_crf="$MASTER_EXPECTED_CRF" 'BEGIN { exit (actual_crf > master_expected_crf) ? 0 : 1 }'; then
        echo "  [validate] ERROR: master CRF ${actual_crf} is worse than expected" \
             "${MASTER_EXPECTED_CRF}: $master" >&2
        echo "    This master does not meet the required CRF. Prepare a new" >&2
        echo "    master from the source capture with prepare_video.sh." >&2
        return 1
    fi

    # 5. pix_fmt matches expected chroma format exactly.
    local actual_pix_fmt
    actual_pix_fmt=$(get_master_pix_fmt "$master")
    if [[ -z "$actual_pix_fmt" ]]; then
        echo "  [validate] ERROR: could not read pix_fmt from master: $master" >&2
        return 1
    elif [[ "$actual_pix_fmt" != "$MASTER_EXPECTED_PIX_FMT" ]]; then
        echo "  [validate] ERROR: master pix_fmt '$actual_pix_fmt' does not match" \
             "expected '$MASTER_EXPECTED_PIX_FMT': $master" >&2
        echo "    This master does not have the required chroma format. Prepare" >&2
        echo "    a new master from the source capture with prepare_video.sh." >&2
        return 1
    fi

    # 6. prepare_video.sh explicitly stamps NTSC SMPTE 170M at encode time.
    local color_space color_trc color_primaries
    color_space=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_space \
        -of default=noprint_wrappers=1:nokey=1 "$master" 2>/dev/null)
    color_trc=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_transfer \
        -of default=noprint_wrappers=1:nokey=1 "$master" 2>/dev/null)
    color_primaries=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_primaries \
        -of default=noprint_wrappers=1:nokey=1 "$master" 2>/dev/null)
    if [[ "$color_space" != "$MASTER_EXPECTED_COLOR" \
          || "$color_trc" != "$MASTER_EXPECTED_COLOR" \
          || "$color_primaries" != "$MASTER_EXPECTED_COLOR" ]]; then
        echo "  [validate] ERROR: master color tags do not match $MASTER_EXPECTED_COLOR:" \
             "space=${color_space:-unknown} trc=${color_trc:-unknown}" \
             "primaries=${color_primaries:-unknown}: $master" >&2
        return 1
    fi

    # 7. This wrapper always invokes prepare_video.sh with --pcm. Missing or
    # compressed audio here would be an irreversible loss before distribution.
    local audio_codec
    audio_codec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name \
        -of default=noprint_wrappers=1:nokey=1 "$master" 2>/dev/null)
    if [[ "$audio_codec" != pcm_* ]]; then
        echo "  [validate] ERROR: expected PCM audio in prepared master, found" \
             "'${audio_codec:-missing}': $master" >&2
        return 1
    fi

    return 0
}

# ------------------------------------------------------------------------------
# 6. Main Processing Loop
# ------------------------------------------------------------------------------

# Initialise state file before any state operations.
state_init
state_validate_day_keys
state_check_config "$DEFAULT_CRF"
state_handle_failed_entries

# Discover captures independently of the directory layout. -L admits a source
# tree made from symlinks, as sony_avchd_by_day.sh does, while canonical-path
# deduplication prevents the same capture being processed through two views.
# Capture find's status through a file rather than a process substitution: an
# unreadable subtree must fail discovery, not look like a smaller clean set.
DISCOVERY_FILE=$(mktemp "${STATE_DIR}/dv-discovery.XXXXXX")
if ! find -L "$INPUT_ROOT" -type f -name 'dv.*.avi' -print0 > "$DISCOVERY_FILE"; then
    rm -f -- "$DISCOVERY_FILE"
    DISCOVERY_FILE=""
    log "ERROR: could not search the complete input tree: $INPUT_ROOT" >&2
    exit 1
fi

local -a DISCOVERED_SOURCES=()
local -A seen_source_paths=()
# The path find printed, keyed by the canonical path it resolved to. Directory
# grouping has to read the day directory off this one: a source tree of per-day
# symlinks -- what sony_avchd_by_day.sh builds -- carries the day directory on
# the link path only, while the canonical path leads back to wherever the
# capture is really stored, whose directories say nothing about any day.
local -A SOURCE_FOUND_PATH=()
# Canonical paths reached through more than one path, as "canonical<TAB>other".
# Deduplication keeps the first, which is right for processing but hides a
# capture linked into two different day directories -- an ambiguity only
# directory grouping can see, and only by looking at the paths it discarded.
local -a DUPLICATE_FOUND_PATHS=()
while IFS= read -r -d '' _src; do
    local _src_abs
    if ! _src_abs=$(realpath -- "$_src"); then
        rm -f -- "$DISCOVERY_FILE"
        DISCOVERY_FILE=""
        log "ERROR: could not resolve discovered capture: $_src" >&2
        exit 1
    fi
    if [[ -z ${seen_source_paths[$_src_abs]+x} ]]; then
        seen_source_paths["$_src_abs"]=1
        SOURCE_FOUND_PATH["$_src_abs"]=$_src
        DISCOVERED_SOURCES+=("$_src_abs")
    else
        DUPLICATE_FOUND_PATHS+=("${_src_abs}"$'\t'"${_src}")
    fi
done < "$DISCOVERY_FILE"
rm -f -- "$DISCOVERY_FILE"
DISCOVERY_FILE=""

if (( ${#DISCOVERED_SOURCES[@]} == 0 )); then
    log "No dv.*.avi captures found under $INPUT_ROOT. The day plan will be empty."
else
    log "Discovered ${#DISCOVERED_SOURCES[@]} unique DV capture(s) recursively."
fi

# The pre-flight table and pipeline output names use the source stem. Two
# different files with one stem would overwrite or resume each other's work,
# regardless of their directories, so reject that ambiguity before probing.
local -A source_for_stem=()
for _src in "${DISCOVERED_SOURCES[@]}"; do
    local _base _stem
    _base=${_src##*/}
    _stem=${_base%.avi}
    if [[ -n ${source_for_stem[$_stem]+x} \
          && ${source_for_stem[$_stem]} != "$_src" ]]; then
        log "ERROR: two captures have the same basename stem '$_stem':" >&2
        log "         ${source_for_stem[$_stem]}" >&2
        log "         $_src" >&2
        log "       Output and resume names would be ambiguous; rename one capture." >&2
        exit 1
    fi
    source_for_stem["$_stem"]=$_src
done

# ------------------------------------------------------------------------------
# 6a. Pre-flight: resolve every capture's recording time before any GPU work
# ------------------------------------------------------------------------------
# The result supplies the grouping key as well as the chapter label. A flat
# directory, descriptive folders and a conventional by-day tree therefore all
# produce the same day groups from the same captures.
if ! run_preflight "${DISCOVERED_SOURCES[@]}"; then
    log "Batch stopped during pre-flight. No files were processed."
    exit 1
fi

day_directory_for_source() {
    # day_directory_for_source <path-as-found>
    #
    # Prints the name of the nearest enclosing YYYYMMDD[_Description] directory,
    # searching from the capture's own directory upwards and stopping at
    # INPUT_ROOT. Prints nothing and returns 1 when there is none.
    #
    # Nearest rather than "the child of INPUT_ROOT", which is all the batch used
    # to look at: an archive whose day directories sit one level down (under a
    # tape or volume directory, say) is still sorted by day, and refusing it
    # would send someone back to recording-time grouping over a layout that
    # answers the question perfectly well. INPUT_ROOT itself is examined too, so
    # pointing --input-root straight at one day directory processes that day.
    local dir=$1 base
    dir=${dir%/*}
    while :; do
        base=${dir##*/}
        if [[ $base =~ ^[0-9]{8}([^0-9].*)?$ ]]; then
            printf '%s\n' "$base"
            return 0
        fi
        [[ $dir == "$INPUT_ROOT" || $dir != */* ]] && return 1
        dir=${dir%/*}
    done
}

local -A SOURCE_DAY=()
local -A discovered_days=()
# Captures with no enclosing dated directory. Collected rather than reported one
# at a time: someone who asked for directory grouping over a tree that does not
# have it needs the whole list and the way out, not the first offender.
local -a UNGROUPED_SOURCES=()
# Directories whose date prefix repeats. Day state and the resume ledger are
# keyed by YYYYMMDD, so two directories for one date are two days that would
# share one state entry and overwrite each other's record of being assembled.
local -A day_date_owner=()
# Captures whose directory disagrees with the tape inside them, or with the
# recording time that will label their chapters. A warning, not an anomaly: the
# directory was named as the authority and the run is describable either way.
# What must not happen is the disagreement going unmentioned.
local -a DIRECTORY_DAY_MISMATCHES=()
for _src in "${DISCOVERED_SOURCES[@]}"; do
    local _base _stem _recorded_at _recording_day _src_abs _day_key
    _src_abs=$_src
    [[ -z ${PREFLIGHT_SKIP[$_src_abs]+x} ]] || continue
    _base=${_src##*/}
    _stem=${_base%.avi}
    _recorded_at=${PREFLIGHT_RESOLVED[$_stem]:-}
    if [[ -z $_recorded_at ]]; then
        log "ERROR: pre-flight neither resolved nor excluded: $_src" >&2
        exit 1
    fi
    if [[ $_recorded_at =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})\  ]]; then
        _recording_day="${BASH_REMATCH[1]}${BASH_REMATCH[2]}${BASH_REMATCH[3]}"
    else
        log "ERROR: invalid resolved recording time '$_recorded_at': $_src" >&2
        exit 1
    fi

    if [[ $GROUP_BY == directory ]]; then
        # The directory name in full, suffix included, so a day assembled from
        # 20070414_Vernon is written as 20070414_Vernon.mp4 the way it was
        # before recording times decided the grouping. State keys keep only the
        # date (see state_get_day), so renaming the directory's descriptive part
        # does not orphan work already done under it.
        if ! _day_key=$(day_directory_for_source "${SOURCE_FOUND_PATH[$_src_abs]:-$_src_abs}"); then
            UNGROUPED_SOURCES+=("${SOURCE_FOUND_PATH[$_src_abs]:-$_src_abs}")
            continue
        fi
        local _dir_date=${_day_key:0:8} _probe_dt _probe_day=""
        if [[ -n ${day_date_owner[$_dir_date]+x} \
              && ${day_date_owner[$_dir_date]} != "$_day_key" ]]; then
            log "ERROR: two day directories carry the date $_dir_date:" >&2
            log "         ${day_date_owner[$_dir_date]}" >&2
            log "         $_day_key" >&2
            log "       Resume state and the assembled day video are keyed by the" >&2
            log "       date alone, so the two would overwrite each other. Merge" >&2
            log "       them, or group by recording time: --group-by recording-time" >&2
            exit 1
        fi
        day_date_owner["$_dir_date"]=$_day_key
        _probe_dt=$(probe_recording_datetime "$_src_abs")
        [[ $_probe_dt =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2}) ]] \
            && _probe_day="${BASH_REMATCH[1]}${BASH_REMATCH[2]}${BASH_REMATCH[3]}"
        if [[ $_recording_day != "$_dir_date" \
              || ( -n $_probe_day && $_probe_day != "$_dir_date" ) ]]; then
            DIRECTORY_DAY_MISMATCHES+=("$_src_abs")
            log "  WARNING: $_base is filed under $_day_key but was recorded elsewhere:"
            log "           directory says: $_dir_date"
            log "           tape says     : ${_probe_dt:-(no usable subcode time)}"
            log "           chapter label : $_recorded_at"
            run_manifest_set "$RUN_DIR" --arg source "$_src_abs" \
                --arg directory "$_day_key" --arg directory_day "$_dir_date" \
                --arg subcode_time "$_probe_dt" --arg recording_time "$_recorded_at" \
                '.directory_day_mismatches += [{source: $source,
                                                directory: $directory,
                                                directory_day: $directory_day,
                                                subcode_time: $subcode_time,
                                                recording_time: $recording_time}]' || true
        fi
    else
        _day_key=$_recording_day
    fi

    SOURCE_DAY["$_src_abs"]=$_day_key
    discovered_days["$_day_key"]=1
done

if [[ $GROUP_BY == directory ]]; then
    if (( ${#UNGROUPED_SOURCES[@]} > 0 )); then
        log "ERROR: --group-by directory, but ${#UNGROUPED_SOURCES[@]} capture(s) sit under no" >&2
        log "       YYYYMMDD[_Description] directory below $INPUT_ROOT:" >&2
        local _ungrouped
        for _ungrouped in "${UNGROUPED_SOURCES[@]}"; do
            log "         $_ungrouped" >&2
        done
        log "       Either sort them into dated directories, or group them by the" >&2
        log "       recording time each capture carries, which needs no particular" >&2
        log "       layout: --group-by recording-time" >&2
        exit 1
    fi
    # A capture linked into two different day directories is grouped by
    # whichever path find reached first -- an arbitrary choice this must not
    # make silently.
    local _duplicate _dup_abs _dup_found _dup_day
    for _duplicate in "${DUPLICATE_FOUND_PATHS[@]}"; do
        _dup_abs=${_duplicate%%$'\t'*}
        _dup_found=${_duplicate#*$'\t'}
        [[ -n ${SOURCE_DAY[$_dup_abs]+x} ]] || continue
        _dup_day=$(day_directory_for_source "$_dup_found") || _dup_day=""
        [[ -n $_dup_day && $_dup_day != "${SOURCE_DAY[$_dup_abs]}" ]] || continue
        log "ERROR: one capture appears in two day directories:" >&2
        log "         ${SOURCE_FOUND_PATH[$_dup_abs]}" >&2
        log "         $_dup_found" >&2
        log "       Remove one of them, or group by recording time instead:" >&2
        log "       --group-by recording-time" >&2
        exit 1
    done
    if (( ${#DIRECTORY_DAY_MISMATCHES[@]} > 0 )); then
        log "  ${#DIRECTORY_DAY_MISMATCHES[@]} capture(s) disagree with the directory holding them."
        log "  Grouping follows the directory, as asked; the times above label the"
        log "  chapters. Check them against day-plan.tsv before a long run."
    fi
fi

local -a ALL_DAYS=()
if (( ${#discovered_days[@]} > 0 )); then
    mapfile -t ALL_DAYS < <(printf '%s\n' "${!discovered_days[@]}" | sort)
fi

# Apply --day to the day names grouping produced. Comparing the date prefix
# rather than the whole name is what lets --day 20070414 select the directory
# 20070414_Vernon, as it always could; under recording-time grouping a day name
# is that date and nothing else, so the same comparison is exact.
if (( ${#DAY_FILTER[@]} > 0 )); then
    local -a filtered=()
    for day in "${ALL_DAYS[@]}"; do
        for wanted in "${DAY_FILTER[@]}"; do
            if [[ "${day:0:8}" == "$wanted" ]]; then
                filtered+=("$day")
                break
            fi
        done
    done
    ALL_DAYS=("${filtered[@]}")
    if (( ${#ALL_DAYS[@]} == 0 )); then
        if [[ $GROUP_BY == directory ]]; then
            log "ERROR: --day filter matched no day directories: ${DAY_FILTER[*]}" >&2
        else
            log "ERROR: --day filter matched no resolved recording dates: ${DAY_FILTER[*]}" >&2
        fi
        exit 1
    fi
fi

local -A selected_days=()
for day in "${ALL_DAYS[@]}"; do selected_days["$day"]=1; done
local -a BATCH_SOURCES=()
for _src in "${DISCOVERED_SOURCES[@]}"; do
    local _src_abs _source_day
    _src_abs=$_src
    _source_day=${SOURCE_DAY[$_src_abs]:-}
    [[ -n $_source_day && -n ${selected_days[$_source_day]+x} ]] || continue
    BATCH_SOURCES+=("$_src_abs")
done

# The manifest accumulates the actual source paths assigned to selected days,
# rather than a directory view which may contain unrelated material. Union is
# required when separate --day invocations share one run: replacing the list
# would erase the sources behind day videos assembled by an earlier invocation.
run_manifest_set "$RUN_DIR" --args \
    '.sources |= (. + $ARGS.positional | unique)' \
    "${BATCH_SOURCES[@]}" || true

if [[ $GROUP_BY == directory ]]; then
    log "Days to process: ${#ALL_DAYS[@]} (grouped by dated source directory)"
else
    log "Days to process: ${#ALL_DAYS[@]} (grouped by resolved recording time)"
fi

source_files_for_day() {
    # source_files_for_day <YYYYMMDD>
    # NUL-separated canonical paths, ordered by resolved recording time and
    # then path for a deterministic tie break. The NUL format preserves every
    # pathname character except NUL, which filesystems themselves forbid.
    local wanted_day=$1 source source_abs stem recorded_at
    for source in "${DISCOVERED_SOURCES[@]}"; do
        # DISCOVERED_SOURCES was canonicalized once at discovery. Repeating
        # realpath here once per source per day made a 99-day plan launch about
        # 77,000 processes while producing exactly the same paths.
        source_abs=$source
        [[ ${SOURCE_DAY[$source_abs]:-} == "$wanted_day" ]] || continue
        stem=${source##*/}; stem=${stem%.avi}
        recorded_at=${PREFLIGHT_RESOLVED[$stem]:-}
        printf '%s\t%s\0' "$recorded_at" "$source_abs"
    done | sort -z -t $'\t' -k1,1 -k2,2 | cut -z -f2-
}

if [[ $DRY_RUN == true ]]; then
    log ""
    log "Resolved day plan (time source: $TIME_SOURCE):"
    if [[ $GROUP_BY == directory ]]; then
        log "  Days below are the dated source directories. Each day video keeps"
        log "  its directory's descriptive name; the tree names the date alone so"
        log "  a plan made either way can be diffed against the other."
    fi
    if (( ${#ALL_DAYS[@]} == 0 )); then
        log "  No captures remain in the plan."
    fi
    # The plan as the tree `tree` would print for an archive already sorted by
    # day: each day, then the captures that landed in it. Which capture went
    # where is the question a plan is read to answer, and only naming them
    # answers it -- a range of times cannot, at any length.
    #
    # Written to a file as well as the log, because comparing one grouping
    # against the other is the point: every log line carries a timestamp, so a
    # diff of two logs reports every line as changed.
    #
    # The day is named by its eight-digit date under both groupings, never by
    # the directory's descriptive name. A day appearing as 20070414 in one plan
    # and 20070414_Vernon in the other would mark every day as changed and bury
    # the captures that actually moved.
    local _plan_tree="${RUN_DIR}/day-tree.txt"
    local _plan_day _plan_base _plan_index _plan_last_index _plan_line
    : > "$_plan_tree" \
        || { log "ERROR: could not write $_plan_tree" >&2; exit 1; }
    for _plan_day in "${ALL_DAYS[@]}"; do
        local -a _plan_files=()
        mapfile -d '' -t _plan_files < <(source_files_for_day "$_plan_day")
        printf '%s (%d capture(s))\n' "${_plan_day:0:8}" "${#_plan_files[@]}" \
            >> "$_plan_tree"
        _plan_last_index=$(( ${#_plan_files[@]} - 1 ))
        for _plan_index in "${!_plan_files[@]}"; do
            _plan_base=${_plan_files[$_plan_index]##*/}
            if (( _plan_index == _plan_last_index )); then
                printf '└── %s\n' "$_plan_base" >> "$_plan_tree"
            else
                printf '├── %s\n' "$_plan_base" >> "$_plan_tree"
            fi
        done
    done
    # A full archive is around 99 days of some 780 captures, so printing every
    # capture scrolls the entire plan off the screen and buries the pre-flight
    # warnings above it. Past a screenful, the console gets one line per day and
    # the captures stay in the file, which is where they are read from anyway --
    # a tree is compared with diff, not by eye. Under it, a single --day plan
    # prints whole, because there the captures ARE the answer.
    #
    # One line per day is not itself a bound: at 99 days that form is still two
    # screenfuls of headers whose totals the summary below already gives. So the
    # day list is capped in turn, and past the cap only the first and last few
    # days print. They bound the archive's span -- a stray 1970 or 2038 day from
    # an unset camcorder clock sorts to one end or the other, which is the one
    # thing worth catching by eye here -- while the elided middle stays in the
    # file along with every capture.
    local _plan_tree_inline_max=60
    local _plan_day_inline_max=10
    local _plan_tree_lines _plan_capture_lines _plan_day_count
    local _plan_head _plan_tail _plan_elided
    local -a _plan_day_lines=()
    _plan_tree_lines=$(wc -l < "$_plan_tree")
    if (( _plan_tree_lines <= _plan_tree_inline_max )); then
        while IFS= read -r _plan_line; do log "$_plan_line"; done < "$_plan_tree"
    else
        _plan_capture_lines=$(( _plan_tree_lines - ${#ALL_DAYS[@]} ))
        # Day headers only. Capture rows start with the tree connectors.
        while IFS= read -r _plan_line; do
            [[ $_plan_line == [0-9]* ]] && _plan_day_lines+=("$_plan_line")
        done < "$_plan_tree"
        _plan_day_count=${#_plan_day_lines[@]}
        if (( _plan_day_count <= _plan_day_inline_max )); then
            for _plan_line in "${_plan_day_lines[@]}"; do log "$_plan_line"; done
        else
            _plan_head=$(( _plan_day_inline_max / 2 ))
            _plan_tail=$(( _plan_day_inline_max - _plan_head ))
            _plan_elided=$(( _plan_day_count - _plan_head - _plan_tail ))
            for (( _plan_index = 0; _plan_index < _plan_head; _plan_index++ )); do
                log "${_plan_day_lines[$_plan_index]}"
            done
            log "  ... $_plan_elided more day(s) ..."
            for (( _plan_index = _plan_day_count - _plan_tail;
                   _plan_index < _plan_day_count; _plan_index++ )); do
                log "${_plan_day_lines[$_plan_index]}"
            done
        fi
        log ""
        log "  All $_plan_day_count day(s) and $_plan_capture_lines capture(s) are listed in"
        log "  $_plan_tree"
    fi

    # Keep one complete, machine-readable plan in run.json. The transient
    # recording_times.tsv only maps stems to resolved times because real
    # assembly needs that lookup; by itself it omits source paths, raw evidence
    # and exclusions. day_plan carries all of those, and day-plan.tsv is the
    # same information in a form convenient to read or open in a spreadsheet.
    local -a _plan_fields=()
    local _plan_status _plan_day _plan_time _plan_basis _plan_name _plan_probe
    local _plan_detail _plan_note _plan_reason _plan_abs _plan_base _plan_stem _plan_tmp
    local _plan_comparison _plan_count _plan_label
    local -A _plan_comparison_counts=()
    for _plan_file in "${DISCOVERED_SOURCES[@]}"; do
        _plan_abs=$_plan_file
        _plan_base=${_plan_file##*/}
        _plan_stem=${_plan_base%.avi}
        _plan_name=$(datetime_from_filename "$_plan_base")
        _plan_probe=$(probe_recording_datetime "$_plan_file")
        _plan_detail=$(probe_recording_detail "$_plan_file")
        _plan_note=$(probe_recording_note "$_plan_file")
        _plan_day=${SOURCE_DAY[$_plan_abs]:-}
        _plan_time=${PREFLIGHT_RESOLVED[$_plan_stem]:-}
        _plan_reason=${PREFLIGHT_SKIP[$_plan_abs]:-}
        _plan_basis=""
        _plan_comparison=$(filename_subcode_comparison "$_plan_base" \
            "$_plan_name" "$_plan_probe" "$_plan_detail" "$_plan_note")
        _plan_comparison_counts["$_plan_comparison"]=$((
            ${_plan_comparison_counts[$_plan_comparison]:-0} + 1))

        if [[ -n $_plan_reason ]]; then
            _plan_status=excluded
        elif [[ -n $_plan_day && -n ${selected_days[$_plan_day]+x} ]]; then
            _plan_status=include
        else
            _plan_status=filtered
        fi

        if [[ -n $_plan_time && $TIME_SOURCE == subcode \
              && $_plan_time == "$_plan_probe" ]]; then
            _plan_basis=subcode
        elif [[ -n $_plan_time && $_plan_time == "$_plan_name" ]]; then
            _plan_basis=filename
        elif [[ -n $_plan_time && $_plan_time == "$_plan_probe" ]]; then
            _plan_basis=subcode
        fi

        _plan_fields+=("$_plan_status" "$_plan_comparison" "$_plan_base" \
            "$_plan_day" "$_plan_time" "$_plan_basis" "$_plan_name" \
            "$_plan_probe" "$_plan_detail" "$_plan_note" "$_plan_abs" \
            "$_plan_reason")
    done

    _plan_tmp=$(mktemp "${RUN_DIR}/day-plan.XXXXXX") || exit 1
    jq -n --args '
        $ARGS.positional as $p
        | [range(0; ($p | length); 12) as $i
           | {status: $p[$i], filename_vs_subcode: $p[$i + 1],
              capture: $p[$i + 2], day: $p[$i + 3],
              recording_time: $p[$i + 4], basis: $p[$i + 5],
              filename_time: $p[$i + 6], subcode_time: $p[$i + 7],
              dv_sources: $p[$i + 8], subcode_observation: $p[$i + 9],
              source: $p[$i + 10], reason: $p[$i + 11]}
           | with_entries(select(.value != ""))]
        | sort_by([
            (if .status == "include" then 0
             elif .status == "filtered" then 1 else 2 end),
            (.day // ""), (.recording_time // ""), .source])
    ' "${_plan_fields[@]}" > "$_plan_tmp" \
        || { rm -f -- "$_plan_tmp"; exit 1; }
    run_manifest_set "$RUN_DIR" --slurpfile plan "$_plan_tmp" \
        '.day_plan = $plan[0]' \
        || { rm -f -- "$_plan_tmp"; exit 1; }
    rm -f -- "$_plan_tmp"

    jq -r '
        ["status", "filename_vs_subcode", "capture", "day",
         "recording_time", "basis", "filename_time", "subcode_time",
         "dv_sources", "subcode_observation", "source", "reason"],
        (.day_plan[]
         | [.status, .filename_vs_subcode, .capture, (.day // ""),
            (.recording_time // ""), (.basis // ""),
            (.filename_time // ""), (.subcode_time // ""),
            (.dv_sources // ""), (.subcode_observation // ""),
            .source, (.reason // "")])
        | @tsv
    ' "$(run_manifest "$RUN_DIR")" > "${RUN_DIR}/day-plan.tsv" \
        || { log "ERROR: could not write ${RUN_DIR}/day-plan.tsv" >&2; exit 1; }

    log ""
    log "Filename versus DV subcode (all discovered captures):"
    for _plan_comparison in match_exact match_filename_minute \
            match_after_head_conflict match_after_missing_head \
            same_day_within_tolerance mismatch_different_day \
            mismatch_over_tolerance filename_missing filename_invalid \
            subcode_missing subcode_conflict subcode_invalid both_missing; do
        _plan_count=${_plan_comparison_counts[$_plan_comparison]:-0}
        (( _plan_count > 0 )) || continue
        case $_plan_comparison in
            match_exact) _plan_label="exact matches" ;;
            match_filename_minute) _plan_label="matches to minute-only filename precision" ;;
            match_after_head_conflict) _plan_label="matches after a contradictory first frame recovered" ;;
            match_after_missing_head) _plan_label="matches after an empty first frame recovered" ;;
            same_day_within_tolerance) _plan_label="same day but different times within the accepted tolerance" ;;
            mismatch_different_day) _plan_label="different-day mismatches" ;;
            mismatch_over_tolerance) _plan_label="same-day mismatches beyond the accepted tolerance" ;;
            filename_missing) _plan_label="filenames with no timestamp" ;;
            filename_invalid) _plan_label="filenames with an invalid timestamp" ;;
            subcode_missing) _plan_label="captures with no usable subcode timestamp" ;;
            subcode_conflict) _plan_label="captures with conflicting subcode sources" ;;
            subcode_invalid) _plan_label="captures with an invalid parsed subcode timestamp" ;;
            both_missing) _plan_label="captures with neither timestamp" ;;
        esac
        log "  $_plan_count  $_plan_label"
    done

    # A plan has nothing to resume and no reason to enter the real processing
    # loop. Give one rough total using the same initial rate as the live ETA;
    # actual runs refine that rate from completed files.
    local _plan_bytes=0 _plan_size _plan_estimate
    if (( ${#BATCH_SOURCES[@]} > 0 )); then
        while IFS= read -r _plan_size; do
            [[ $_plan_size =~ ^[0-9]+$ ]] || _plan_size=0
            _plan_bytes=$(( _plan_bytes + _plan_size ))
        done < <(stat -c %s -- "${BATCH_SOURCES[@]}" 2>/dev/null)
    fi
    _plan_estimate=$(awk -v bytes="$_plan_bytes" \
        'BEGIN { printf "%.1f", bytes / 3600000 * 18.54 }')
    log ""
    log "Plan totals: ${#BATCH_SOURCES[@]} included capture(s), ${#ALL_DAYS[@]} day(s),"
    log "  about $(fmt_duration "$_plan_estimate") of ESRGAN at the initial 18.54x rate."

    run_manifest_finish "$RUN_DIR" \
        || log "WARNING: could not mark the plan manifest complete"
    log "Dry-run results were written to: $RUN_DIR"
    log "  Day tree:  ${RUN_DIR}/day-tree.txt   (diff this against another plan)"
    log "  Day plan:  ${RUN_DIR}/day-plan.tsv   (the same, with the evidence)"
    log "  Manifest:  $(run_manifest "$RUN_DIR")"
    log "A plan, not a resumable run: its internal state is discarded, and a real"
    log "run probes the sources again."
    exit 0
fi

# Counters for the final summary.
total_files=0
skipped_files=0
prepared_files=0
upscaled_file_count=0
planned_upscale_files=0
failed_files=0
assembled_days_this_run=0
failed_days=0

# ------------------------------------------------------------------------------
# ETA Tracking
# ------------------------------------------------------------------------------
# DV25 NTSC bitrate used to estimate source duration from file size.
# 3,600,000 bytes/sec = 25 Mbps / 8.
readonly DV_BYTES_PER_SEC=3600000

# ESRGAN realtime ratio: seconds of ESRGAN time per second of source video.
# Each completed file logs both its actual ratio and the exponential moving
# average. On restart, restore the most recent smoothed value from the end of
# batch.log so a clean stop does not reset the ETA model. Fall back to the
# original measured test value (218s source -> 4046s ESRGAN = 18.54x) for a
# new log or one written before ratio updates were recorded.
# Stored as an awk-compatible decimal string.
esrgan_ratio="18.54"
local restored_ratio=""
if [[ -s "$BATCH_LOG" ]]; then
    restored_ratio=$(tac -- "$BATCH_LOG" 2>/dev/null \
        | grep -m1 -oE 'smoothed [0-9]+([.][0-9]+)?x' \
        | awk '{ sub(/x$/, "", $2); print $2 }' || true)
fi
if [[ "$restored_ratio" =~ ^[0-9]+([.][0-9]+)?$ ]] \
    && awk -v restored_ratio="$restored_ratio" 'BEGIN { exit !(restored_ratio > 0) }'; then
    esrgan_ratio="$restored_ratio"
    log "Restored ESRGAN timing ratio from batch log: ${esrgan_ratio}x"
else
    log "No saved ESRGAN timing ratio found; using initial ${esrgan_ratio}x"
fi

# Precompute estimated ESRGAN seconds for every file across all days.
# esrgan_ratio is updated via EMA after each file; compute_overall_remaining()
# uses it to give a live forward-looking batch estimate.

compute_overall_remaining() {
    # Computes remaining ESRGAN seconds by summing estimates for all
    # not-yet-upscaled files across all days, using the current esrgan_ratio.
    # Called after each file completes so the estimate stays accurate as the
    # ratio is updated via EMA. Skips files already marked upscaled in state.
    local remaining=0
    local _day
    for _day in "${ALL_DAYS[@]}"; do
        local _day_stage
        _day_stage=$(state_get_day "$_day" "stage")
        [[ "$_day_stage" == "assembled" ]] && continue
        while IFS= read -r -d '' _f; do
            local _abs
            _abs=$(realpath "$_f")
            local _stage
            _stage=$(state_get_file "$_abs" "stage")
            [[ "$_stage" == "upscaled" ]] && continue
            local _sz
            _sz=$(stat -c %s "$_abs" 2>/dev/null) || continue
            local _dur _est
            _dur=$(awk -v _sz="$_sz" -v dv_bytes_per_sec="$DV_BYTES_PER_SEC" 'BEGIN { printf "%.2f", _sz / dv_bytes_per_sec }')
            _est=$(awk -v _dur="$_dur" -v esrgan_ratio="$esrgan_ratio" 'BEGIN { printf "%.1f", _dur * esrgan_ratio }')
            remaining=$(awk -v remaining="$remaining" -v _est="$_est" 'BEGIN { printf "%.1f", remaining + _est }')
        done < <(source_files_for_day "$_day")
    done
    echo "$remaining"
}

eta_timestamp() {
    # eta_timestamp <remaining_seconds>  →  "ETA: Wed May 28 14:32"
    local remaining
    remaining=$(awk -v seconds="$1" 'BEGIN { printf "%d", seconds }')
    local eta_epoch=$(( $(date +%s) + remaining ))
    date -d "@${eta_epoch}" '+%a %b %d %H:%M'
}

for day_name in "${ALL_DAYS[@]}"; do
    day_out_dir="$VIDEOS_DIR"

    # This boundary check deliberately precedes every per-day path. The
    # previous day may have ended through a failure, a dry run,
    # missing outputs, or another `continue`; none of those may bypass a
    # STOP_DAY request and allow work on the next day to begin.
    if check_stop_day; then
        log "⏸️  STOP_DAY requested — not starting day $day_name."
        log "   Re-run to continue from this day."
        break
    fi

    # A STOP detected at a per-file boundary in the previous day can reach
    # this loop iteration through that day's incomplete-assembly `continue`.
    # Check it before printing the separator/day header; otherwise the log
    # misleadingly announces the next day even though no work starts there.
    if check_stop; then
        log "Stop requested — ending batch before the next day."
        break
    fi

    log "------------------------------------------------------------"
    log "Day: $day_name"

    # Check day-level assembly state first (skip fully assembled days).
    local day_stage
    day_stage=$(state_get_day "$day_name" "stage")
    if [[ "$day_stage" == "assembled" ]]; then
        log "  Day already assembled — skipping."
        continue
    fi

    # Collect every recursively discovered capture assigned to this resolved
    # recording date, ordered by the complete resolved timestamp.
    #
    # Captures pre-flight excluded are dropped here, before anything is done
    # with them. Assembly needs a resolved recording time for every file in a
    # day, so a capture without one cannot be part of the day video; leaving it
    # in the list would spend hours upscaling a file whose only possible
    # outcome is to fail the assembly of every other file in its day.
    local -a all_day_files=()
    mapfile -d '' -t all_day_files < <(source_files_for_day "$day_name")
    local -a day_files=()
    local _cand _cand_abs
    for _cand in "${all_day_files[@]}"; do
        _cand_abs=$(realpath -m -- "$_cand")
        if [[ -n ${PREFLIGHT_SKIP[$_cand_abs]+x} ]]; then
            log "  Excluded by pre-flight: $(basename -- "$_cand")" \
                "(${PREFLIGHT_SKIP[$_cand_abs]})"
            continue
        fi
        day_files+=("$_cand")
    done

    if [[ ${#day_files[@]} -eq 0 ]]; then
        log "  No included captures assigned to this recording day — skipping."
        continue
    fi

    log "  Files: ${#day_files[@]}"

    # Per-day ETA: sum estimated ESRGAN seconds for not-yet-upscaled files
    # in this day and log before processing begins.
    local day_est_sec=0
    for _df in "${day_files[@]}"; do
        local _da
        _da=$(realpath "$_df")
        local _dstage
        _dstage=$(state_get_file "$_da" "stage")
        if [[ "$_dstage" != "upscaled" && "$_dstage" != "failed" ]]; then
            local _dsz
            _dsz=$(stat -c %s "$_df" 2>/dev/null) || _dsz=0
            local _ddur
            _ddur=$(awk -v _dsz="$_dsz" -v dv_bytes_per_sec="$DV_BYTES_PER_SEC" 'BEGIN { printf "%.2f", _dsz / dv_bytes_per_sec }')
            day_est_sec=$(awk -v day_est_sec="$day_est_sec" -v _ddur="$_ddur" -v esrgan_ratio="$esrgan_ratio" 'BEGIN { printf "%.1f", \
                day_est_sec + _ddur * esrgan_ratio }')
        fi
    done
    if awk -v day_est_sec="$day_est_sec" 'BEGIN { exit (day_est_sec > 0) ? 0 : 1 }'; then
        log "  Day ETA: ~$(fmt_duration "$day_est_sec") of ESRGAN" \
            "($(eta_timestamp "$day_est_sec"))"
    fi

    [[ "$DRY_RUN" == false ]] && mkdir -p "$VIDEOS_DIR"

    # Track upscaled output paths for this day's chapter assembly.
    local -a day_upscaled_outputs=()

    # -- Per-file loop with one-ahead prefetch of prepare_video.sh --
    #
    # While ESRGAN processes file N, prepare_video.sh runs concurrently on
    # file N+1 as a background job. Since ESRGAN uses the GPU and prepare_video.sh
    # uses only the CPU (bwdif + x264), they do not compete for the same resource.
    #
    # Synchronisation: before launching ESRGAN on file N+1, we call
    # `wait $prefetch_pid` which blocks until the background prepare job
    # finishes. This is correct in all timing scenarios:
    #   - Prefetch finishes before ESRGAN: master waits on disk (no idle time)
    #   - ESRGAN finishes before prefetch: wrapper blocks briefly at wait
    #     (small idle time, still better than fully sequential)
    #
    # State interactions:
    #   - If file N+1 is already "upscaled" in state, no prefetch is launched
    #     for it (it doesn't need a master).
    #   - If file N+1 is already "prepared" in state (interrupted run left a
    #     master on disk), no prefetch is launched; the existing master is used.
    #   - If the prefetch background job fails, the error is recorded and the
    #     file is marked failed; ESRGAN is not attempted for that file.
    #
    # Variables carried across iterations:
    #   prefetch_pid:    PID of the background prepare job for file N+1,
    #                    or "" if none was launched.
    #   prefetch_src:    absolute path of the source file being prefetched.
    #   prefetch_stem:   stem of the source file being prefetched.
    #   prefetch_before: file list snapshot taken before the background job ran,
    #                    used to identify the new master after it completes.

    local prefetch_pid=""
    local prefetch_src=""
    local prefetch_stem=""
    local prefetch_before=""
    # prepare_video.sh is invoked below with --output-dir "$MASTERS_DIR", so it
    # writes masters directly here -- no separate ESRGAN_DIR/outputs staging
    # directory or subsequent mv is needed.
    #
    # Not on a dry run. work/ now means "this run stopped part-way through a
    # file", and a dry run that left an empty one behind would say that about a
    # run which had not touched anything.
    if [[ "$DRY_RUN" == false ]]; then
        mkdir -p "$MASTERS_DIR"
    fi

    # run_prefetch <src_abs> <src_stem>
    # Launches prepare_video.sh in the background for the given file.
    # Sets prefetch_pid, prefetch_src, prefetch_stem, prefetch_before.
    # No-ops if the file is already upscaled or prepared in state.
    run_prefetch() {
        local next_abs="$1"
        local next_stem="$2"
        local next_stage
        next_stage=$(state_get_file "$next_abs" "stage")

        if [[ "$next_stage" == "upscaled" || "$next_stage" == "prepared" \
              || "$next_stage" == "failed" ]]; then
            log "    Prefetch: skipping $next_stem (stage: $next_stage)"
            prefetch_pid=""
            prefetch_src="$next_abs"
            prefetch_stem="$next_stem"
            prefetch_before=""
            return
        fi

        log "    Prefetch: starting prepare_video.sh for $next_stem (background)"
        prefetch_before=$(find "$MASTERS_DIR" \
            -maxdepth 1 -name "${next_stem}_mask0_60fps_*.mkv" \
            -type f 2>/dev/null | sort)

        # Redirect all output to a per-file log so it doesn't flood the
        # terminal while ESRGAN is running. It is kept with everything else
        # recorded about this capture; collect_prefetch checks the exit code
        # via wait.
        local prefetch_log
        prefetch_log="$(run_source_log_dir "$RUN_DIR" "$next_abs")/prefetch.log"
        mkdir -p "$(dirname -- "$prefetch_log")"
        (
            cd "$ESRGAN_DIR"
            # Preserve the DV source's PCM audio through the preparation
            # master, so the pipeline performs its single required AAC encode
            # from PCM instead of transcoding an earlier AAC intermediate.
            bash "$PREPARE_SCRIPT" "$next_abs" --mask 0 --pcm --yes \
                --output-dir "$MASTERS_DIR" \
                --log-dir "$(run_source_log_dir "$RUN_DIR" "$next_abs")"
        ) >"$prefetch_log" 2>&1 &
        prefetch_pid=$!
        prefetch_src="$next_abs"
        prefetch_stem="$next_stem"
    }

    # collect_prefetch
    # Waits for the background prepare job (if any), identifies the new master,
    # moves it to MASTERS_DIR, and updates state.
    # On success: sets master_file and returns 0.
    # On failure: logs error, marks file failed, sets master_file="", returns 1.
    # Also handles the no-prefetch case (file already prepared/upscaled).
    collect_prefetch() {
        local src_abs="$prefetch_src"
        local src_stem="$prefetch_stem"

        # No background job was launched — file was already prepared/upscaled/failed.
        if [[ -z "$prefetch_pid" ]]; then
            local existing_stage
            existing_stage=$(state_get_file "$src_abs" "stage")
            if [[ "$existing_stage" == "prepared" ]]; then
                master_file=$(state_get_file "$src_abs" "master")
                if [[ ! -f "$master_file" ]]; then
                    local err="state shows prepared but master file missing: $master_file"
                    log "    ERROR: $err"
                    state_set_file "$src_abs" "stage" "failed"
                    state_set_file "$src_abs" "error" "$err"
                    master_file=""
                    return 1
                fi
                if ! validate_master "$master_file" "$src_abs"; then
                    local err="previously prepared master failed validation"
                    log "    ERROR: $err: $master_file"
                    log "    Deleting invalid master — will regenerate on next run."
                    rm -f "$master_file"
                    # Remove the stale prepared entry instead of marking it
                    # failed: failed entries are intentionally skipped forever,
                    # which would contradict the promised next-run regeneration.
                    state_remove_file_entries "$src_abs"
                    master_file=""
                    return 1
                fi
                log "    Master (from prior run): $(basename "$master_file")"
                return 0
            fi
            # upscaled or failed — caller handles these before calling collect_prefetch
            master_file=""
            return 0
        fi

        # Wait for the background prepare job and capture its exit code.
        local prepare_rc=0
        local prefetch_log
        prefetch_log="$(run_source_log_dir "$RUN_DIR" "$src_abs")/prefetch.log"
        wait "$prefetch_pid" || prepare_rc=$?
        prefetch_pid=""

        if [[ $prepare_rc -ne 0 ]]; then
            local err="prepare_video.sh (prefetch) exited $prepare_rc"
            log "    ERROR: $err for $src_stem"
            log "    Prefetch log: $prefetch_log"
            # Print the tail of the prefetch log so the error is visible
            # without requiring the user to open a separate file.
            if [[ -f "$prefetch_log" ]]; then
                log "    Last 10 lines of prefetch log:"
                tail -10 "$prefetch_log" | while IFS= read -r line; do
                    log "      $line"
                done
            fi
            state_set_file "$src_abs" "stage" "failed"
            state_set_file "$src_abs" "error" "$err"
            master_file=""
            return 1
        fi

        # Identify the newly created master. prepare_video.sh was invoked with
        # --output-dir "$MASTERS_DIR", so it's already in its final location.
        local after_masters
        after_masters=$(find "$MASTERS_DIR" \
            -maxdepth 1 -name "${src_stem}_mask0_60fps_*.mkv" \
            -type f 2>/dev/null | sort)
        local new_master
        new_master=$(comm -13 \
            <(echo "$prefetch_before") \
            <(echo "$after_masters") | head -1)

        if [[ -z "$new_master" ]]; then
            local err="prepare_video.sh (prefetch) succeeded but no master found in $MASTERS_DIR"
            log "    ERROR: $err"
            state_set_file "$src_abs" "stage" "failed"
            state_set_file "$src_abs" "error" "$err"
            master_file=""
            return 1
        fi

        master_file="$new_master"
        if ! validate_master "$master_file" "$src_abs"; then
            local err="master validation failed after prefetch prepare_video.sh"
            log "    ERROR: $err for $src_stem"
            rm -f "$master_file"
            state_set_file "$src_abs" "stage" "failed"
            state_set_file "$src_abs" "error" "$err"
            master_file=""
            return 1
        fi
        # Append the prefetch log to batch.log so there is a complete record
        # of prepare_video.sh output for every file, then remove the temp file.
        if [[ -f "$prefetch_log" ]]; then
            log "    --- prefetch prepare_video.sh output for $src_stem ---"
            cat "$prefetch_log"
            log "    --- end prefetch output ---"
            rm -f "$prefetch_log"
        fi
        log "    Master (prefetch): $(basename "$master_file")"
        state_set_file "$src_abs" "stage"  "prepared"
        state_set_file "$src_abs" "master" "$master_file"
        (( prepared_files++ )) || true
        return 0
    }

    local num_files=${#day_files[@]}
    local master_file=""  # set by collect_prefetch, consumed by Stage 2

    for (( fi=0; fi < num_files; fi++ )); do
        local src_file="${day_files[$fi]}"
        (( total_files++ )) || true
        local src_abs
        src_abs=$(realpath "$src_file")
        local src_base
        src_base=$(basename "$src_file")
        local src_stem="${src_base%.avi}"

        log "  File: $src_base"

        # Compute per-file ESRGAN estimate now (used both for the pre-ESRGAN
        # banner and for the post-completion ratio update).
        local src_size
        src_size=$(stat -c %s "$src_abs" 2>/dev/null) || src_size=0
        local src_dur_est
        src_dur_est=$(awk -v src_size="$src_size" -v dv_bytes_per_sec="$DV_BYTES_PER_SEC" 'BEGIN { printf "%.2f", src_size / dv_bytes_per_sec }')
        local file_esrgan_est
        file_esrgan_est=$(awk -v src_dur_est="$src_dur_est" -v esrgan_ratio="$esrgan_ratio" 'BEGIN { printf "%.1f", src_dur_est * esrgan_ratio }')

        # Check for a stop request before starting a new file. ESRGAN on the
        # current file is always allowed to finish; we stop before the next one.
        if check_stop; then
            log "    Stop requested — not processing $src_base."
            # Wait for any in-flight prefetch before breaking.
            if [[ -n "$prefetch_pid" ]]; then
                log "    Waiting for in-flight prefetch (pid $prefetch_pid)..."
                wait "$prefetch_pid" || true
                prefetch_pid=""
            fi
            break
        fi

        # Read current state for this file.
        local file_stage
        file_stage=$(state_get_file "$src_abs" "stage")

        # --- Resume: already upscaled ---
        if [[ "$file_stage" == "upscaled" ]]; then
            local existing_output
            existing_output=$(state_get_file "$src_abs" "output")
            log "    Already upscaled — skipping. Output: $(basename "$existing_output")"
            day_upscaled_outputs+=("$existing_output")
            (( skipped_files++ )) || true
            # Launch prefetch for the next file if we skipped this one.
            if (( fi + 1 < num_files )); then
                local next_file="${day_files[$((fi+1))]}"
                local next_abs next_stem next_base
                next_abs=$(realpath "$next_file")
                next_base=$(basename "$next_file")
                next_stem="${next_base%.avi}"
                run_prefetch "$next_abs" "$next_stem"
            fi
            continue
        fi

        # --- Resume: previously failed ---
        if [[ "$file_stage" == "failed" ]]; then
            local prev_error
            prev_error=$(state_get_file "$src_abs" "error")
            log "    Previously failed ($prev_error) — skipping. Remove from state to retry."
            (( failed_files++ )) || true
            # Cancel any in-flight prefetch that was for this file, then
            # launch prefetch for the next file.
            if [[ -n "$prefetch_pid" && "$prefetch_src" == "$src_abs" ]]; then
                log "    Waiting for in-flight prefetch to finish before skipping..."
                wait "$prefetch_pid" || true
                prefetch_pid=""
            fi
            if (( fi + 1 < num_files )); then
                local next_file="${day_files[$((fi+1))]}"
                local next_abs next_stem next_base
                next_abs=$(realpath "$next_file")
                next_base=$(basename "$next_file")
                next_stem="${next_base%.avi}"
                run_prefetch "$next_abs" "$next_stem"
            fi
            continue
        fi

        if [[ "$DRY_RUN" == true ]]; then
            log "    DRY RUN — would prepare and upscale."
            (( planned_upscale_files++ )) || true
            continue
        fi

        # ----------------------------------------------------------------
        # Stage 1: Collect prepare_video.sh result (prefetch or direct)
        # ----------------------------------------------------------------
        # On the very first file (fi==0) or after a skip, there may be no
        # in-flight prefetch for this file. If prefetch_src doesn't match
        # this file, run prepare synchronously now and handle the master
        # identification and move inline — collect_prefetch is only for
        # background jobs.
        if [[ "$prefetch_src" != "$src_abs" ]]; then
            log "    Stage 1: prepare_video.sh (synchronous — no prefetch for this file)"
            local sync_before
            sync_before=$(find "$MASTERS_DIR" \
                -maxdepth 1 -name "${src_stem}_mask0_60fps_*.mkv" \
                -type f 2>/dev/null | sort)
            local sync_rc=0
            (
                cd "$ESRGAN_DIR"
                # Keep this synchronous path identical to run_prefetch(): the
                # prepared master must preserve source PCM and use MKV, which
                # is also the container expected by master discovery below.
                bash "$PREPARE_SCRIPT" "$src_abs" --mask 0 --pcm --yes \
                    --output-dir "$MASTERS_DIR" \
                    --log-dir "$(run_source_log_dir "$RUN_DIR" "$src_abs")"
            ) || sync_rc=$?
            if [[ $sync_rc -ne 0 ]]; then
                local err="prepare_video.sh exited $sync_rc"
                log "    ERROR: $err"
                state_set_file "$src_abs" "stage" "failed"
                state_set_file "$src_abs" "error" "$err"
                (( failed_files++ )) || true
                if (( fi + 1 < num_files )); then
                    local next_file="${day_files[$((fi+1))]}"
                    local next_abs next_stem next_base
                    next_abs=$(realpath "$next_file")
                    next_base=$(basename "$next_file")
                    next_stem="${next_base%.avi}"
                    run_prefetch "$next_abs" "$next_stem"
                fi
                continue
            fi
            # Identify the newly created master. prepare_video.sh was invoked
            # with --output-dir "$MASTERS_DIR", so it's already in place.
            local sync_after
            sync_after=$(find "$MASTERS_DIR" \
                -maxdepth 1 -name "${src_stem}_mask0_60fps_*.mkv" \
                -type f 2>/dev/null | sort)
            local sync_new_master
            sync_new_master=$(comm -13 \
                <(echo "$sync_before") \
                <(echo "$sync_after") | head -1)
            if [[ -z "$sync_new_master" ]]; then
                local err="prepare_video.sh succeeded but no master found in $MASTERS_DIR"
                log "    ERROR: $err"
                state_set_file "$src_abs" "stage" "failed"
                state_set_file "$src_abs" "error" "$err"
                (( failed_files++ )) || true
                if (( fi + 1 < num_files )); then
                    local next_file="${day_files[$((fi+1))]}"
                    local next_abs next_stem next_base
                    next_abs=$(realpath "$next_file")
                    next_base=$(basename "$next_file")
                    next_stem="${next_base%.avi}"
                    run_prefetch "$next_abs" "$next_stem"
                fi
                continue
            fi
            master_file="$sync_new_master"
            if ! validate_master "$master_file" "$src_abs"; then
                local err="master validation failed after prepare_video.sh"
                log "    ERROR: $err"
                rm -f "$master_file"
                state_set_file "$src_abs" "stage" "failed"
                state_set_file "$src_abs" "error" "$err"
                (( failed_files++ )) || true
                if (( fi + 1 < num_files )); then
                    local next_file="${day_files[$((fi+1))]}"
                    local next_abs next_stem next_base
                    next_abs=$(realpath "$next_file")
                    next_base=$(basename "$next_file")
                    next_stem="${next_base%.avi}"
                    run_prefetch "$next_abs" "$next_stem"
                fi
                continue
            fi
            log "    Master: $(basename "$master_file")"
            state_set_file "$src_abs" "stage"  "prepared"
            state_set_file "$src_abs" "master" "$master_file"
            (( prepared_files++ )) || true

            # Launch prefetch for file N+1 now that ESRGAN is about to start.
            if (( fi + 1 < num_files )); then
                local next_file="${day_files[$((fi+1))]}"
                local next_abs next_stem next_base
                next_abs=$(realpath "$next_file")
                next_base=$(basename "$next_file")
                next_stem="${next_base%.avi}"
                run_prefetch "$next_abs" "$next_stem"
            fi
        else
            # A prefetch job was (or may have been) running for this file.
            log "    Stage 1: waiting for prepare_video.sh (prefetch)..."
            master_file=""
            local collect_rc=0
            collect_prefetch || collect_rc=$?

            # Launch prefetch for file N+1 immediately after collect_prefetch
            # returns — ESRGAN is about to start on file N, so this is the
            # earliest moment to start overlapping prepare on file N+1.
            if (( fi + 1 < num_files )); then
                local next_file="${day_files[$((fi+1))]}"
                local next_abs next_stem next_base
                next_abs=$(realpath "$next_file")
                next_base=$(basename "$next_file")
                next_stem="${next_base%.avi}"
                run_prefetch "$next_abs" "$next_stem"
            fi

            if [[ $collect_rc -ne 0 || -z "$master_file" ]]; then
                (( failed_files++ )) || true
                continue
            fi
        fi

        # ----------------------------------------------------------------
        # Stage 2: video_upscale_pipeline.py
        # ----------------------------------------------------------------
        local overall_remaining
        overall_remaining=$(compute_overall_remaining)
        # Day remaining = estimate for current file + estimates for all
        # not-yet-started files in this day. This stays consistent with
        # the per-file estimate shown on the same line.
        local day_remaining
        day_remaining="$file_esrgan_est"
        for (( ri = fi + 1; ri < num_files; ri++ )); do
            local r_file="${day_files[$ri]}"
            local r_abs
            r_abs=$(realpath "$r_file")
            local r_stage
            r_stage=$(state_get_file "$r_abs" "stage")
            if [[ "$r_stage" == "upscaled" ]]; then
                continue
            fi
            local r_size
            r_size=$(stat -c %s "$r_abs" 2>/dev/null) || r_size=0
            local r_dur r_est
            r_dur=$(awk -v r_size="$r_size" -v dv_bytes_per_sec="$DV_BYTES_PER_SEC" 'BEGIN { printf "%.2f", r_size / dv_bytes_per_sec }')
            r_est=$(awk -v r_dur="$r_dur" -v esrgan_ratio="$esrgan_ratio" 'BEGIN { printf "%.1f", r_dur * esrgan_ratio }')
            day_remaining=$(awk -v day_remaining="$day_remaining" -v r_est="$r_est" 'BEGIN { printf "%.1f", day_remaining + r_est }')
        done
        local esrgan_eta_str
        esrgan_eta_str="est. $(fmt_duration "$file_esrgan_est") | day done: ~$(fmt_duration "$day_remaining") ($(eta_timestamp "$day_remaining")) | batch: ~$(fmt_duration "$overall_remaining")"
        log "    Stage 2: ESRGAN starting at $(date '+%H:%M:%S')" \
            "— $esrgan_eta_str"
        # Stop markers are useful only when more batch work could follow this
        # file. On the final file of the final selected day, both requests
        # would merely ask the batch to stop where it will end naturally.
        if (( fi + 1 < num_files )) || [[ "$day_name" != "${ALL_DAYS[-1]}" ]]; then
            log "    To stop after this file: touch $STOP_FILE"
            log "    To stop after this day:  touch $STOP_DAY_FILE"
        fi

        local esrgan_start
        esrgan_start=$(date +%s)
        local pipeline_rc=0
        # Built as an array (not a single string) so --crf is cleanly
        # omitted when unset, rather than passing an empty "--crf ''"
        # that video_upscale_pipeline.py would have to additionally guard
        # against.
        local pipeline_args=(
            "$master_file"
            --profile dv
            --scale "$SCALE"
            --run-dir "$RUN_DIR"
            --log-dir "$(run_source_log_dir "$RUN_DIR" "$src_abs")"
            --esrgan-eta "$esrgan_eta_str"
            --force
        )
        if [[ -n "$CRF_ARG" ]]; then
            pipeline_args+=( --crf "$CRF_ARG" )
        fi
        (
            cd "$ESRGAN_DIR"
            # Activate when the venv is present; otherwise run the interpreter
            # that was resolved above and let check_venv() refuse it, which is
            # where that requirement is stated.
            # shellcheck disable=SC1091
            [[ -f venv/bin/activate ]] && source venv/bin/activate
            "$VENV_PYTHON" video_upscale_pipeline.py "${pipeline_args[@]}"
        ) || pipeline_rc=$?
        local esrgan_end
        esrgan_end=$(date +%s)
        local esrgan_actual=$(( esrgan_end - esrgan_start ))

        if [[ $pipeline_rc -ne 0 ]]; then
            local err="video_upscale_pipeline.py exited $pipeline_rc"
            log "    ERROR: $err"
            state_set_file "$src_abs" "stage" "failed"
            state_set_file "$src_abs" "error" "$err"
            [[ -f "$master_file" ]] && rm -f "$master_file"
            (( failed_files++ )) || true
            continue
        fi

        # Locate the upscaled output. The pipeline names it:
        #   {src_stem}_mask0_{TIMESTAMP}_dv_x{N}_{model}_{fps}_crfN[...].mp4
        #
        # Intermediates are excluded by name rather than trusted to be absent.
        # The no-chunk path writes files such as "<stem>_nc_prefiltered.mp4",
        # and "nc_" sorts after "dv_", so one left behind by an interrupted run
        # would win the `sort | tail -1` below and be delivered as the day's
        # video. They are written under work/ now, but the guard costs nothing
        # and the failure it prevents is silent.
        local upscaled_output=""
        upscaled_output=$(find "$day_out_dir" \
            -maxdepth 1 -type f \
            -name "${src_stem}_*.mp4" \
            ! -name '*_nc_*' \
            2>/dev/null \
            | sort -t_ -k1 | tail -1)

        if [[ -z "$upscaled_output" ]]; then
            local err="pipeline succeeded but no MP4 output found in $day_out_dir"
            log "    ERROR: $err"
            state_set_file "$src_abs" "stage" "failed"
            state_set_file "$src_abs" "error" "$err"
            [[ -f "$master_file" ]] && rm -f "$master_file"
            (( failed_files++ )) || true
            continue
        fi

        # Validate the delivered file before recording successful state.
        # Assembly repeats the check for outputs reused on resume.
        local immediate_suffix
        if ! immediate_suffix=$(extract_pipeline_suffix "$(basename "$upscaled_output")"); then
            local err
            err="pipeline output has an unrecognised quality suffix: $(basename "$upscaled_output")"
            log "    ERROR: $err"
            state_set_file "$src_abs" "stage" "failed"
            state_set_file "$src_abs" "error" "$err"
            [[ -f "$master_file" ]] && rm -f "$master_file"
            (( failed_files++ )) || true
            continue
        fi
        if ! validate_upscaled_output "$upscaled_output" "$immediate_suffix"; then
            local err="pipeline output failed delivered-file quality validation"
            log "    ERROR: $err: $upscaled_output"
            state_set_file "$src_abs" "stage" "failed"
            state_set_file "$src_abs" "error" "$err"
            [[ -f "$master_file" ]] && rm -f "$master_file"
            (( failed_files++ )) || true
            continue
        fi

        log "    Output: $(basename "$upscaled_output")"
        log "    ESRGAN actual: $(fmt_duration "$esrgan_actual")" \
            "(estimated $(fmt_duration "$file_esrgan_est"))"

        # Update the ESRGAN ratio using exponential moving average (alpha=0.2).
        # This adapts the estimate to actual GPU performance as files complete.
        # Only update if source duration is non-trivial (>5s) to avoid
        # very short files skewing the ratio due to fixed startup overhead.
        if awk -v src_dur_est="$src_dur_est" 'BEGIN { exit (src_dur_est > 5.0) ? 0 : 1 }'; then
            local actual_ratio
            actual_ratio=$(awk -v esrgan_actual="$esrgan_actual" -v src_dur_est="$src_dur_est" 'BEGIN { printf "%.4f", \
                esrgan_actual / src_dur_est }')
            esrgan_ratio=$(awk -v esrgan_ratio="$esrgan_ratio" -v actual_ratio="$actual_ratio" 'BEGIN { printf "%.4f", \
                0.8 * esrgan_ratio + 0.2 * actual_ratio }')
            log "    Ratio update: actual ${actual_ratio}x →" \
                "smoothed ${esrgan_ratio}x"
        fi

        # Record success before calculating remaining work. Otherwise the
        # current file is still counted in the batch ETA, producing a stale
        # full-file estimate after the final file has already completed.
        state_set_file "$src_abs" "stage"  "upscaled"
        state_set_file "$src_abs" "output" "$upscaled_output"
        day_upscaled_outputs+=("$upscaled_output")
        (( upscaled_file_count++ )) || true

        # Print updated ETAs after ESRGAN finishes — last wrapper output before
        # the next file starts, so it is visible at the point the user is watching.
        local overall_remaining_now
        overall_remaining_now=$(compute_overall_remaining)
        # Day remaining after this file = sum of estimates for not-yet-started files.
        local day_remaining_now="0"
        for (( ri = fi + 1; ri < num_files; ri++ )); do
            local rn_file="${day_files[$ri]}"
            local rn_abs
            rn_abs=$(realpath "$rn_file")
            local rn_stage
            rn_stage=$(state_get_file "$rn_abs" "stage")
            if [[ "$rn_stage" == "upscaled" ]]; then
                continue
            fi
            local rn_size
            rn_size=$(stat -c %s "$rn_abs" 2>/dev/null) || rn_size=0
            local rn_dur rn_est
            rn_dur=$(awk -v rn_size="$rn_size" -v dv_bytes_per_sec="$DV_BYTES_PER_SEC" 'BEGIN { printf "%.2f", rn_size / dv_bytes_per_sec }')
            rn_est=$(awk -v rn_dur="$rn_dur" -v esrgan_ratio="$esrgan_ratio" 'BEGIN { printf "%.1f", rn_dur * esrgan_ratio }')
            day_remaining_now=$(awk -v day_remaining_now="$day_remaining_now" -v rn_est="$rn_est" 'BEGIN { printf "%.1f", day_remaining_now + rn_est }')
        done
        if awk -v day_remaining_now="$day_remaining_now" 'BEGIN { exit (day_remaining_now > 0) ? 0 : 1 }'; then
            log "    Day remaining: ~$(fmt_duration "$day_remaining_now")" \
                "($(eta_timestamp "$day_remaining_now"))" \
                "| Batch remaining: ~$(fmt_duration "$overall_remaining_now")"
        elif awk -v overall_remaining_now="$overall_remaining_now" 'BEGIN { exit (overall_remaining_now > 0) ? 0 : 1 }'; then
            log "    Day complete | Batch remaining: ~$(fmt_duration "$overall_remaining_now")"
        fi

        log "    Removing master intermediate: $(basename "$master_file")"
        rm -f "$master_file"

    done  # end per-file loop

    # If a prefetch job is still running at end of day loop (e.g. all remaining
    # files were skipped), wait for it to finish cleanly before moving on.
    if [[ -n "$prefetch_pid" ]]; then
        log "  Waiting for trailing prefetch job (pid $prefetch_pid)..."
        wait "$prefetch_pid" || true
        prefetch_pid=""
    fi

    # ----------------------------------------------------------------
    # Stage 3: Chapter assembly for the day
    # ----------------------------------------------------------------
    if [[ ${#day_upscaled_outputs[@]} -eq 0 ]]; then
        if [[ "$DRY_RUN" == true ]]; then
            log "  DRY RUN — would assemble the day video (skipped: no real outputs yet)."
        else
            log "  No upscaled outputs available for $day_name — skipping assembly."
        fi
        continue
    fi

    # Only assemble if all non-failed files for this day are upscaled.
    # Query state for each source file rather than relying on the in-memory
    # day_upscaled_outputs array, which may not include files skipped on resume.
    local available_this_day=0
    local failed_this_day=0
    for src_file in "${day_files[@]}"; do
        local sa
        sa=$(realpath "$src_file")
        local st
        st=$(state_get_file "$sa" "stage")
        [[ "$st" == "upscaled" ]] && (( available_this_day++ )) || true
        [[ "$st" == "failed"   ]] && (( failed_this_day++ ))    || true
    done

    if [[ $failed_this_day -gt 0 ]]; then
        log "  WARNING: $failed_this_day file(s) failed for $day_name."
        log "  Skipping chapter assembly until all files succeed."
        log "  Fix failed files (check batch.log), clear their state, and re-run."
        state_set_day "$day_name" "stage" "failed"
        state_set_day "$day_name" "error" \
            "${failed_this_day} file(s) failed — assembly skipped"
        (( failed_days++ )) || true
        continue
    fi

    if [[ $available_this_day -lt ${#day_files[@]} ]]; then
        log "  Only $available_this_day / ${#day_files[@]} files upscaled for $day_name."
        log "  Skipping assembly — re-run to complete remaining files."
        continue
    fi

    log "  Assembling day video for $day_name ($available_this_day / ${#day_files[@]} files)..."

    if [[ "$DRY_RUN" == true ]]; then
        log "  DRY RUN — would assemble the day video."
        continue
    fi

    # Rebuild the full list of upscaled outputs for this day from state
    # (covers both newly upscaled and previously skipped/resumed files).
    local -a all_day_outputs=()
    local -a all_day_sources=()
    for src_file in "${day_files[@]}"; do
        local sa
        sa=$(realpath "$src_file")
        local out
        out=$(state_get_file "$sa" "output")
        if [[ -n "$out" ]]; then
            all_day_outputs+=("$out")
            all_day_sources+=("$sa")
        fi
    done

    # Extract the pipeline suffix (including the visible CRF/pixel-format
    # policy) from the first output filename. Current patterns end in either:
    #   ..._<profile>_x<N>_<model>_<fps>fps_crfN_yuv420.mp4
    #   ..._<profile>_x<N>_<model>_<fps>fps_crfN_yuv420.mp4
    local pipeline_suffix=""
    local first_out_base
    first_out_base=$(basename "${all_day_outputs[0]}")
    if ! pipeline_suffix=$(extract_pipeline_suffix "$first_out_base"); then
        log "  ERROR: could not extract pipeline suffix from upscaled filename:"
        log "    $first_out_base"
        log "  Expected pattern: ..._<profile>_x<N>_<model>_<fps>fps[_rife]_crfN_yuv420.mp4"
        log "  This indicates an unexpected output filename format."
        log "  Check video_upscale_pipeline.py output naming and update the regex."
        exit 1
    fi

    # Confirm every file in the day matches that same quality profile before
    # merging. The assembly appends tracks without re-encoding, so it will
    # not catch or warn if a day is reprocessed partway through with
    # different settings (e.g. two different --crf values) --
    # it would silently produce a day video mixing quality levels across
    # its own timeline. See extract_pipeline_suffix() for why this token is
    # the right proxy check.
    local quality_mismatch=false
    local append_signature_ref=""
    local frame_rate_ref=""
    local frame_rate_tolerance="0.01"
    local output_index
    for output_index in "${!all_day_outputs[@]}"; do
        out_file="${all_day_outputs[$output_index]}"
        local source_file="${all_day_sources[$output_index]}"
        local out_base out_suffix
        out_base=$(basename "$out_file")
        if ! out_suffix=$(extract_pipeline_suffix "$out_base"); then
            log "  ERROR: could not extract pipeline suffix from upscaled filename:"
            log "    $out_base"
            log "  Expected pattern: ..._<profile>_x<N>_<model>_<fps>fps[_rife]_crfN_yuv420.mp4"
            exit 1
        fi
        if [[ "$out_suffix" != "$pipeline_suffix" ]]; then
            log "  ERROR: quality-profile mismatch within day $day_name — refusing to assemble."
            log "    $first_out_base: $pipeline_suffix"
            log "    $out_base: $out_suffix"
            log "  These files were produced under different CRF/pix_fmt/profile settings"
            log "    (e.g. different --crf values). Merging them would silently mix"
            log "    quality levels within a single day video."
            log "  Reprocess the mismatched file(s) with consistent settings (clear its state"
            log "    entry and re-run), or split this day's output manually."
            quality_mismatch=true
            break
        fi

        # Do not trust the filename alone. Probe the actual stream so resumed,
        # moved, or manually replaced outputs receive the same effective
        # safeguards as files just checked by video_upscale_pipeline.py.
        if ! validate_upscaled_output "$out_file" "$out_suffix"; then
            log "  ERROR: delivered-file validation failed — refusing to assemble:"
            log "    $out_file"
            quality_mismatch=true
            break
        fi
        if ! validate_source_output_duration "$source_file" "$out_file"; then
            log "  ERROR: source/enhanced duration validation failed — refusing to assemble:"
            log "    Source:   $source_file"
            log "    Enhanced: $out_file"
            quality_mismatch=true
            break
        fi

        local append_signature
        append_signature=$(get_append_signature "$out_file")
        if [[ -z "$append_signature" ]]; then
            log "  ERROR: could not read append-compatibility stream properties: $out_file"
            quality_mismatch=true
            break
        fi
        if [[ -z "$append_signature_ref" ]]; then
            append_signature_ref="$append_signature"
        elif [[ "$append_signature" != "$append_signature_ref" ]]; then
            log "  ERROR: stream-property mismatch within day $day_name — refusing to assemble."
            log "    First file: $first_out_base"
            log "    Mismatch:   $out_base"
            log "  Codec, dimensions, chroma, colorimetry, sample rate,"
            log "    channel count, and audio codec must agree for a safe append."
            quality_mismatch=true
            break
        fi

        local frame_rate
        if ! frame_rate=$(get_numeric_frame_rate "$out_file"); then
            log "  ERROR: could not read a valid average frame rate: $out_file"
            quality_mismatch=true
            break
        fi
        if [[ -z "$frame_rate_ref" ]]; then
            frame_rate_ref="$frame_rate"
        else
            local frame_rate_delta
            frame_rate_delta=$(awk -v frame_rate="$frame_rate" -v frame_rate_ref="$frame_rate_ref" 'BEGIN { d = frame_rate - frame_rate_ref; \
                                           if (d < 0) d = -d; printf "%.9f", d }')
            if awk -v frame_rate_delta="$frame_rate_delta" -v frame_rate_tolerance="$frame_rate_tolerance" 'BEGIN { exit (frame_rate_delta > frame_rate_tolerance) ? 0 : 1 }'; then
                log "  ERROR: frame-rate mismatch within day $day_name — refusing to assemble."
                log "    First file: $first_out_base (${frame_rate_ref} fps)"
                log "    Mismatch:   $out_base (${frame_rate} fps)"
                log "    Difference ${frame_rate_delta} exceeds tolerance ${frame_rate_tolerance} fps."
                quality_mismatch=true
                break
            fi
        fi
    done
    if [[ "$quality_mismatch" == true ]]; then
        state_set_day "$day_name" "stage" "failed"
        state_set_day "$day_name" "error" \
            "quality or stream-property mismatch across day's files — assembly skipped"
        (( failed_days++ )) || true
        continue
    fi

    local assemble_rc=0
    assemble_day_chapters "$day_name" "$VIDEOS_DIR" \
        "${all_day_outputs[@]}" -- "${all_day_sources[@]}" \
        || assemble_rc=$?

    if [[ $assemble_rc -ne 0 ]]; then
        local err="chapter assembly failed (exit $assemble_rc)"
        log "  ERROR: $day_name — $err"
        log "  The individual upscaled files are intact. Resolve the issue and re-run."
        state_set_day "$day_name" "stage" "failed"
        state_set_day "$day_name" "error" "$err"
        (( failed_days++ )) || true
        # Assembly failure is non-fatal: continue to next day rather than
        # aborting the whole batch, but exit non-zero at end.
        continue
    fi

    local chapter_file="${VIDEOS_DIR}/${day_name}.mp4"
    state_set_day "$day_name" "stage"           "assembled"
    state_set_day "$day_name" "chapter_file"    "$chapter_file"
    # The manifest lists what the run produced. Assembly is idempotent across
    # resumes, so add the name only if it is not already there.
    run_manifest_set "$RUN_DIR" --arg v "${day_name}.mp4" \
        '.videos |= (. + [$v] | unique)' || true
    # Recorded so write_output_readme() can restate the settings for this day
    # on any later run, including one that assembles no days itself.
    state_set_day "$day_name" "pipeline_suffix" "$pipeline_suffix"
    (( assembled_days_this_run++ )) || true
    log "  Day $day_name: assembled OK."

    # Delete individual upscaled files now that they are contained in the
    # verified day video.
    log "  Removing individual upscaled files for $day_name..."
    for out_file in "${all_day_outputs[@]}"; do
        if [[ -f "$out_file" ]]; then
            rm -f "$out_file"
            log "    Removed: $(basename "$out_file")"
        fi
    done

    # Remove per-file state entries for this day. The individual upscaled files
    # are gone and cannot be reassembled, so the entries serve no purpose.
    # The day-level "assembled" entry with chapter_file is the complete record.
    local -a day_src_paths=()
    for src_file in "${day_files[@]}"; do
        day_src_paths+=("$(realpath "$src_file")")
    done
    state_remove_file_entries "${day_src_paths[@]}"
    log "  Removed $((${#day_src_paths[@]})) file state entries for $day_name."

    if ! write_output_readme; then
        # Non-fatal: the day video is assembled and verified, and the settings
        # remain in the batch state and run logs.
        log "  WARNING: could not write ${RUN_DIR}/run.md"
    fi

    # Check immediately after successful assembly as well as at the top of the
    # loop. This gives the clearest message on the normal success path; the
    # top-of-loop check covers every early `continue` path above.
    if check_stop_day; then
        log "⏸️  Stopping after day $day_name as requested."
        log "   Re-run to continue from the next day."
        break
    fi

done  # end day loop

# ------------------------------------------------------------------------------
# 7. Retire the run's working directory
# ------------------------------------------------------------------------------
# work/ is transient: masters are deleted as each file finishes upscaling and
# the pipeline removes its own chunk scaffold, so by now it holds nothing but
# empty directories. Removing it when it is empty makes its presence mean
# something -- a run directory with a work/ is one that stopped mid-file. It is
# removed only when empty, so anything unexpected left behind stays visible
# rather than being deleted along with the scaffold. See
# pipeline_docs/storage-layout.md.
#
# Empty directories are removed from the deepest first, and by discovery rather
# than from a list of names. A list has to be extended every time the pipeline
# grows a new scratch directory, and forgetting leaves work/ behind holding one
# empty child -- which then says the run stopped part-way through a file when
# it did not. -type d -empty -delete removes whatever is actually there and
# only if it is empty, so anything unexpected still survives and stays visible.
if [[ "$DRY_RUN" == false && -d "$WORK_DIR" ]]; then
    find "$WORK_DIR" -depth -type d -empty -delete 2>/dev/null || true
    if [[ ! -d "$WORK_DIR" ]]; then
        log "Removed empty work directory: $WORK_DIR"
    else
        log "work/ retained: it is not empty — $(find "$WORK_DIR" -mindepth 1 | wc -l) item(s) remain"
    fi
fi

# ------------------------------------------------------------------------------
# 8. Summary
# ------------------------------------------------------------------------------
local assembled_days_total=0
local assembled_before_run=0
local remaining_days=0
for day_name in "${ALL_DAYS[@]}"; do
    day_stage=$(state_get_day "$day_name" "stage")
    if [[ "$day_stage" == "assembled" ]]; then
        (( assembled_days_total++ )) || true
    else
        (( remaining_days++ )) || true
    fi
done
assembled_before_run=$(( assembled_days_total - assembled_days_this_run ))

log "========================================================"
if [[ "$STOP_REQUESTED" == true || "$STOP_DAY_REQUESTED" == true ]]; then
    log "Batch stopped cleanly before completion."
else
    log "Batch pass complete."
fi
log "  Files encountered this run:  $total_files"
log "  Files already done/resumed:   $skipped_files"
log "  Masters prepared this run:    $prepared_files"
log "  Files upscaled this run:      $upscaled_file_count"
if [[ "$DRY_RUN" == true ]]; then
    log "  Files that would be upscaled: $planned_upscale_files"
fi
log "  Files failed this run:        $failed_files"
log "  Days assembled before run:    $assembled_before_run"
log "  Days assembled this run:      $assembled_days_this_run"
log "  Days assembled in state:      $assembled_days_total / ${#ALL_DAYS[@]}"
log "  Days still requiring work:    $remaining_days"
log "  Days failed this run:         $failed_days"
log "========================================================"

if [[ $failed_files -gt 0 || $failed_days -gt 0 ]]; then
    log "Some items failed. Review $BATCH_LOG for details."
    log "After correcting the cause, re-run the same command with --retry-failed."
    log "The script will back up its state and retry failed work automatically."
    exit 1
fi

if [[ "$STOP_REQUESTED" == true || "$STOP_DAY_REQUESTED" == true ]]; then
    log "Re-run with the same arguments to resume."
    exit 2
fi

# The run is finished only when every day it set out to process is assembled.
# `finished` is what says the directory is no longer being written to, so it is
# recorded on completion and on nothing else. Dry-run plans exit earlier after
# marking their separate temporary manifest complete.
if [[ "$DRY_RUN" == false ]] && (( remaining_days == 0 && failed_days == 0 )); then
    run_manifest_finish "$RUN_DIR" || log "  WARNING: could not finalise the run manifest"
    log "Run complete: $RUN_DIR"
fi

exit 0

} # end main
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
