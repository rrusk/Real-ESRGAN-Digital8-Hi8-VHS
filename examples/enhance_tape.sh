#!/usr/bin/env bash
# EXAMPLE: run a tape capture through preparation and upscaling in one command.
#
# The settings are one collection's, not a recommendation:
#
#   by name       the bottom mask and the pipeline profile, read from the
#                 capture's file name by capture_names_lib.sh, the table
#                 prepare_tape_capture.sh uses too: hi8* is mask 8 and hi8dv,
#                 vhs* mask 8 and vhsdv, and so on. A name it doesn't know is
#                 refused.
#   preparation   bwdif mode=1 (the default, field-rate ~60fps), AAC 192k in
#                 MP4. Deliberately not --pcm: PCM forces MKV, and ffprobe then
#                 reports N/A for its stream duration, which the pipeline's
#                 audio/video checks need.
#   pipeline      --scale 2, CRF 23, RIFE off.
#   chunking      left at the pipeline's own 900s threshold, except with
#                 --pin-chunks.
#
# Both stages write into one run directory. While the run is unfinished,
# ~/VideoPreservation/ai/current_run points at it, so `--resume` needs neither
# the timestamped directory name nor the source path, and launching the same
# capture again continues that run rather than making a second one beside
# it. A run declined at preparation's prompt is removed again, pointer
# included: it recorded nothing.
#
# Copy and edit this for another collection; the settings above and the table
# in capture_names_lib.sh are the only collection-specific parts.

set -euo pipefail
# Python started from here writes its output as it goes, even into a pipe
# such as `| tee`, where it would otherwise hold 8 KB at a time.
export PYTHONUNBUFFERED=1

VERSION="1.1.0"

# Schema of state/enhance_tape.json. Bumped when its field set changes:
# version 2 replaced archival/distribution_crf with a single crf.
STATE_VERSION=2
STATE_NAME=enhance_tape.json

usage_error() {
    printf '[ERROR] %s\n' "$*" >&2
    printf "Try '%s --help' for more information.\n" "${0##*/}" >&2
    exit 2
}

usage() {
    cat <<'EOF'
Usage:
  enhance_tape.sh [OPTIONS] SOURCE
  enhance_tape.sh --resume [--run-dir DIR] [--yes|--yes-upscale] [--dry-run]

Description:
  Prepare and upscale a capture using this example's filename policy and
  recipe: scale 2, CRF 23, no RIFE. Review the naming conventions below
  before adapting it to your recordings. Both stages write into one run.

Arguments:
  SOURCE                Tape capture (.dv, .avi or .mpg), named by one of the
                        conventions below.

Options:
  --pin-chunks          Pass --chunk-duration 300 --chunk-threshold 300 to the
                        pipeline instead of letting it autotune. Pin it when
                        comparing runs so the chunk boundaries match; available
                        work space can change the automatic choice.
                        Boundaries matter because temporal denoising
                        restarts at each one. The threshold moves with the size
                        so that a tape between 300s and 900s takes the chunked
                        path rather than ignoring the pinned size.
  --run-dir DIR         Use this run directory instead of creating one. When
                        it already holds a prepared master, preparation is
                        skipped. With --resume, select this run instead of
                        ~/VideoPreservation/ai/current_run. Without it, a
                        SOURCE whose current_run is an unfinished run of that
                        same capture and recipe continues in it.
  --resume              Resume ~/VideoPreservation/ai/current_run. The source,
                        prepared-master path and processing settings are read
                        from the run. If the pointer does not exist yet, adopt
                        the newest unfinished Hi8 run. Its saved state is
                        validated after selection. --run-dir may name a
                        particular run instead.
  --yes-upscale         Skip the upscale's "Proceed?" prompt (Stage 2) but keep
                        preparation's. Preparation prompts within seconds of
                        launch, so you confirm settings up front; the upscale
                        then starts on its own once the slower preparation
                        finishes, with no second prompt to wait for. You give up
                        the chance to review the master -- for decode or timing
                        trouble -- before the GPU work.
  --yes                 Skip both prompts, for fully unattended batch use.
                        Without it (or --yes-upscale) each stage shows its
                        settings and waits.
  --dry-run             Print both commands without running them.
  -h, --help            Show this help and exit.
  -V, --version         Show the version and exit.

EOF
    capture_names_help
    cat <<'EOF'

Examples:
  enhance_tape.sh hi8_capture01.dv
  enhance_tape.sh vhs_capture01.dv
  enhance_tape.sh --pin-chunks --dry-run hi8_capture01.dv
  enhance_tape.sh --resume

Dependencies:
  prepare_video.sh, video_upscale_pipeline.py, the repository venv,
  capture_names_lib.sh (beside this script)
EOF
}

# The class a run of this profile is filed under in its manifest: the rule
# video_upscale_pipeline.py's run_class_for_profile applies to its own runs.
run_class_for_profile() {
    if [[ $1 == hi8dv ]]; then
        printf 'hi8-dv\n'
    else
        printf 'other\n'
    fi
}

write_wrapper_state() {
    # write_wrapper_state RUN SOURCE MASTER PROFILE SCALE MODEL RIFE ARCHIVAL
    #                     CRF CHUNK_DURATION THRESHOLD
    local run=$1 source=$2 master=$3 profile=$4 scale=$5 model=$6 rife=$7
    local crf=$8 chunk_duration=$9 threshold=${10}
    local state_dir="$run/state" state="$run/state/$STATE_NAME" tmp
    mkdir -p "$state_dir"
    tmp=$(mktemp "$state.XXXXXX")
    jq -n \
        --argjson version "$STATE_VERSION" \
        --arg source "$source" \
        --arg master "$master" \
        --arg profile "$profile" \
        --argjson scale "$scale" \
        --arg model "$model" \
        --argjson rife "$rife" \
        --argjson crf "${crf:-null}" \
        --argjson chunk_duration "${chunk_duration:-null}" \
        --argjson threshold "$threshold" \
        '{version: $version, source_capture: $source, prepared_master: $master,
          pipeline: {profile: $profile, scale: $scale,
                     model: (if $model == "" then null else $model end), rife: $rife,
                     crf: $crf,
                     chunk_duration: $chunk_duration,
                     chunk_threshold: $threshold}}' > "$tmp"
    mv -f -- "$tmp" "$state"
}

# True when RUN is an unfinished run of this same capture with this same
# recipe, so continuing it is the same job this invocation would start --
# with its prepared master already made.
same_job() {
    # same_job RUN SOURCE PROFILE SCALE MODEL RIFE CRF CHUNK_DURATION THRESHOLD
    local run=$1 source=$2 profile=$3 scale=$4 model=$5 rife=$6 crf=$7
    local chunk_duration=$8 threshold=$9
    local state="$run/state/$STATE_NAME" class
    class=$(run_class_for_profile "$profile")
    [[ -f $(run_manifest "$run") && -f $state ]] || return 1
    [[ $(run_manifest_get "$run" ".finished == null and .class == \"$class\"") == true ]] || return 1
    jq -e --argjson version "$STATE_VERSION" --arg source "$source" \
        --arg profile "$profile" --argjson scale "$scale" --arg model "$model" \
        --argjson rife "$rife" --argjson crf "${crf:-null}" \
        --argjson chunk_duration "${chunk_duration:-null}" --argjson threshold "$threshold" \
        '.version == $version and .source_capture == $source
         and .pipeline.profile == $profile and .pipeline.scale == $scale
         and (.pipeline.model // "") == $model and .pipeline.rife == $rife
         and .pipeline.crf == $crf and .pipeline.chunk_duration == $chunk_duration
         and .pipeline.chunk_threshold == $threshold' "$state" >/dev/null 2>&1
}

main() {
    local source="" run="" pin_chunks=false dry_run=false
    local yes_prepare=false yes_upscale=false resume=false recipe_option=false
    local continuing=false mask="" profile="" why="" settings
    local names
    names="$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")/capture_names_lib.sh"
    [[ -f $names ]] || { printf '[ERROR] Missing: %s\n' "$names" >&2; exit 1; }
    # shellcheck source=capture_names_lib.sh
    source "$names"
    while (($#)); do
        case $1 in
            -h|--help)     usage; exit 0 ;;
            -V|--version)  printf '%s %s\n' "${0##*/}" "$VERSION"; exit 0 ;;
            --run-dir)     (($# >= 2)) || usage_error "--run-dir requires a directory"
                           run=$2; shift 2 ;;
            --resume)      resume=true; shift ;;
            --pin-chunks)  pin_chunks=true; recipe_option=true; shift ;;
            --yes-upscale) yes_upscale=true; shift ;;
            --yes)         yes_prepare=true; yes_upscale=true; shift ;;
            --dry-run)     dry_run=true; shift ;;
            --)            shift
                           # Everything after -- is the operand. Breaking here
                           # without consuming it left source empty and the
                           # wrapper rejected its own argument.
                           (($#)) || break
                           [[ -z $source ]] || usage_error "unexpected extra argument: $1"
                           source=$1; shift
                           (($# == 0)) || usage_error "unexpected extra argument: $1"
                           break ;;
            -*)            usage_error "unknown option: $1" ;;
            *)             [[ -z $source ]] || usage_error "unexpected extra argument: $1"
                           source=$1; shift ;;
        esac
    done
    if $resume; then
        [[ -z $source ]] || usage_error "SOURCE cannot be supplied with --resume"
        $recipe_option && usage_error "--resume uses the recorded recipe; do not pass --pin-chunks"
    else
        [[ -n $source ]] || usage_error "a source capture is required (or use --resume)"
        [[ -f $source ]] || { printf '[ERROR] Source not found: %s\n' "$source" >&2; exit 1; }
        # Absolute before anything changes directory. The pipeline runs from
        # the repository, so paths must survive that directory change.
        source=$(realpath -- "$source")
        settings=$(capture_settings "$source") || { capture_name_refused "$source"; exit 1; }
        IFS=$'\t' read -r mask profile why <<<"$settings"
        printf 'Capture:  %s\n' "$(basename -- "$source")"
        printf 'Settings: mask %s, profile %s  (%s)\n' "$mask" "$profile" "$why"
    fi

    local repo
    repo=$(cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")/.." && pwd)
    local prepare="$repo/prepare_video.sh"
    local pipeline="$repo/video_upscale_pipeline.py"
    # The repository venv when there is one, otherwise whatever python3 is on
    # PATH. Requiring $repo/venv specifically was stricter than the pipeline's
    # own rule: video_upscale_pipeline.py's check_venv() accepts any active
    # virtual environment, so a conda or system-venv install was refused here
    # for a condition the pipeline itself would have been satisfied by. The
    # requirement is not dropped, only left to the one place that states it.
    local python="$repo/venv/bin/python3"
    if [[ ! -x $python ]]; then
        python=$(command -v python3 2>/dev/null) \
            || { printf '[ERROR] No python3 found for the pipeline.\n' >&2; exit 1; }
    fi
    for required in "$prepare" "$pipeline" "$python"; do
        [[ -e $required ]] || { printf '[ERROR] Missing: %s\n' "$required" >&2; exit 1; }
    done
    command -v jq >/dev/null 2>&1 || { printf '[ERROR] Missing dependency: jq\n' >&2; exit 1; }

    # shellcheck source=../pipeline_tools/run_lib.sh
    source "$repo/pipeline_tools/run_lib.sh"
    local preservation_root="${VIDEO_PRESERVATION_ROOT:-$HOME/VideoPreservation}"
    local ai_root; ai_root=$(run_root "$preservation_root")
    local state_file metadata_file
    local scale=2 model="" rife=false crf=23
    local chunk_duration="" chunk_threshold=900

    if $resume; then
        if [[ -z $run ]]; then
            run=$(run_get_current "$ai_root" 2>/dev/null || true)
            if [[ -z $run ]]; then
                run=$(run_find_latest_unfinished "$ai_root" hi8-dv 2>/dev/null || true)
                [[ -n $run ]] || {
                    printf '[ERROR] No current run, and no unfinished Hi8 run to adopt, under %s\n' "$ai_root" >&2
                    exit 1
                }
                printf '[INFO] No current_run pointer; adopting newest unfinished Hi8 run: %s\n' "$run"
            fi
        fi
        run=$(realpath -e -- "$run") || {
            printf '[ERROR] Run directory not found: %s\n' "$run" >&2
            exit 1
        }
        [[ -f $(run_manifest "$run") ]] || {
            printf '[ERROR] Run has no manifest: %s\n' "$run" >&2
            exit 1
        }
        [[ $(run_manifest_get "$run" '.finished == null') == true ]] || {
            printf '[ERROR] Run is already finished: %s\n' "$run" >&2
            exit 1
        }
        state_file="$run/state/$STATE_NAME"
        # A run is this script's when it holds this script's state. A Hi8 run
        # without one is adopted too, its recipe read from the pipeline's
        # metadata below.
        if [[ ! -f $state_file && $(run_manifest_get "$run" '.class == "hi8-dv"') != true ]]; then
            printf '[ERROR] Not a run of this script, nor a Hi8 run it can adopt: %s\n' "$run" >&2
            exit 1
        fi
        metadata_file="$run/work/processing_chunks/metadata.json"
        source=$(run_manifest_get "$run" '.sources[0] // empty' || true)
        if [[ -f $state_file ]]; then
            # Not a migration: this version reads exactly one schema, and every
            # field below has a `// default` fallback, so an older or newer
            # layout would be read as a set of silent defaults rather than
            # refused. That is how a state recorded at CRF 18 could come back
            # as a CRF 23 run. Refusing an unrecognised version keeps a future
            # schema change from doing the same thing again.
            local state_version
            state_version=$(jq -r '.version // 0' "$state_file")
            if [[ $state_version != "$STATE_VERSION" ]]; then
                printf '[ERROR] %s is schema version %s; this script writes and reads version %s.\n' \
                    "$state_file" "$state_version" "$STATE_VERSION" >&2
                printf '        Start a new run rather than resuming from a state it cannot read.\n' >&2
                exit 1
            fi
            local state_source
            state_source=$(jq -r '.source_capture // empty' "$state_file")
            [[ -z $state_source ]] || source=$state_source
            profile=$(jq -r '.pipeline.profile // "hi8dv"' "$state_file")
            scale=$(jq -r '.pipeline.scale // 2' "$state_file")
            model=$(jq -r '.pipeline.model // empty' "$state_file")
            rife=$(jq -r '.pipeline.rife // false' "$state_file")
            crf=$(jq -r '.pipeline.crf // 23' "$state_file")
            chunk_duration=$(jq -r '.pipeline.chunk_duration // empty' "$state_file")
            chunk_threshold=$(jq -r '.pipeline.chunk_threshold // 900' "$state_file")
        elif [[ -f $metadata_file ]]; then
            profile=$(jq -r '.profile // "hi8dv"' "$metadata_file")
            scale=$(jq -r '.scale_factor // 2' "$metadata_file")
            model=$(jq -r '.model // empty' "$metadata_file")
            rife=$(jq -r '(.no_rife // true) | not' "$metadata_file")
            crf=$(jq -r '.crf // 23' "$metadata_file")
            chunk_duration=$(jq -r '.chunk_duration // empty' "$metadata_file")
        else
            # Only an adopted Hi8 run gets here: anything else was refused above.
            profile=hi8dv
        fi
    else
        # The run directory is created here and handed to both stages, rather
        # than created by preparation and found again afterwards. A stem is
        # sanitised when a run is named, so a capture called "hi8 family.dv"
        # produces a run ending _hi8_family that no search for the original
        # stem can find.
        local stem; stem=$(basename -- "$source"); stem=${stem%.*}
        $pin_chunks && { chunk_duration=300; chunk_threshold=300; }
        if [[ -z $run ]]; then
            # The current run is continued when it is this same job: an
            # unfinished run of this capture with this recipe, typically one
            # declined at the upscale prompt or interrupted, whose master
            # took an hour to make. Any other current run is displaced and
            # named, since a bare --resume will no longer reach it.
            local current
            current=$(run_get_current "$ai_root" 2>/dev/null || true)
            if [[ -n $current ]] && same_job "$current" "$source" "$profile" "$scale" \
                    "$model" "$rife" "$crf" "$chunk_duration" "$chunk_threshold"; then
                run=$current
                continuing=true
                printf '[INFO] Continuing the unfinished run of this capture: %s\n' "$run"
            else
                if [[ -n $current ]] \
                        && [[ $(run_manifest_get "$current" '.finished == null' 2>/dev/null) == true ]]; then
                    printf '[WARN] current_run leaves unfinished run %s; resume it with --resume --run-dir %q\n' \
                        "$current" "$current"
                fi
                run="$ai_root/$(run_make_id "$stem")"
            fi
        fi
    fi
    run=$(realpath -m -- "$run")

    local masters="$run/work/masters" logs="$run/logs"
    local master=""
    if [[ -d $masters ]]; then
        master=$(find "$masters" -maxdepth 1 -type f \( -name '*.mp4' -o -name '*.mkv' \) \
            2>/dev/null | sort | tail -1)
    fi

    state_file="$run/state/$STATE_NAME"
    metadata_file="$run/work/processing_chunks/metadata.json"
    if [[ -z $master && -f $state_file ]]; then
        master=$(jq -r '.prepared_master // empty' "$state_file")
    fi
    if [[ -z $master && -f $metadata_file ]]; then
        master=$(jq -r '.input_video // empty' "$metadata_file")
    fi

    local source_state="$run/work/processing_chunks/source_measurements.json"
    local prepare_needed=true
    if [[ -n $master && -f $master ]]; then
        prepare_needed=false
    elif { $resume || $continuing; } && [[ -n $master && -f $source_state ]]; then
        # The pipeline performs the detailed identity, metadata, chunk and
        # audio checks. The missing path is intentional: it identifies the
        # master whose persisted state must match before work can continue.
        # A continued run is a resumed one that was named by its capture.
        prepare_needed=false
    fi
    if $prepare_needed && [[ ! -f $source ]]; then
        printf '[ERROR] Neither a resumable split nor the source capture is available.\n' >&2
        printf '        Source: %s\n' "${source:-<not recorded>}" >&2
        printf '        Run:    %s\n' "$run" >&2
        exit 1
    fi
    # A resumed run records its recipe but not its mask, which follows from
    # the capture's name as it did when the run began.
    if $prepare_needed && [[ -z $mask ]]; then
        settings=$(capture_settings "$source") || { capture_name_refused "$source"; exit 1; }
        IFS=$'\t' read -r mask _ why <<<"$settings"
    fi

    local -a prepare_cmd=(bash "$prepare" "$source" --mask "$mask"
        --output-dir "$masters" --log-dir "$logs")
    $yes_prepare && prepare_cmd+=(--yes)
    local -a pipeline_cmd=("$python" "$pipeline" "${master:-<master>}"
        --profile "$profile" --scale "$scale" --run-dir "$run" --log-dir "$logs")
    [[ -n $model ]] && pipeline_cmd+=(--model "$model")
    pipeline_cmd+=(--crf "$crf")
    # RIFE is off by passing nothing: the pipeline stores the flag as no_rife
    # with store_false, so there is no --no-rife to pass.
    $rife && pipeline_cmd+=(--rife)
    [[ -n $chunk_duration ]] && pipeline_cmd+=(--chunk-duration "$chunk_duration")
    pipeline_cmd+=(--chunk-threshold "$chunk_threshold")
    # This "Proceed?" prompt stops the run after the master exists but before
    # any GPU work -- where a master with decode or timing trouble is caught.
    # --yes-upscale skips just this one; --yes skips both for a batch. There is
    # no skip for preparation's prompt alone: it appears within seconds of
    # launch, so the user is still at the keyboard to answer it, and the only
    # prompt worth leaving behind is this slower stage's. Confirmation-only: it
    # does not carry --force's separate authority to delete incompatible resume
    # state. A resumed pipeline skips it regardless.
    $yes_upscale && pipeline_cmd+=(--yes)

    if $dry_run; then
        printf 'Run directory: %s\n' "$run"
        if ! $prepare_needed; then
            if [[ -f $master ]]; then
                printf 'Stage 1: skipped, master present: %s\n' "$(basename -- "$master")"
            else
                printf 'Stage 1: skipped, saved split is resume-ready; master was released: %s\n' \
                    "$(basename -- "$master")"
            fi
        else
            printf 'Stage 1:'; printf ' %q' "${prepare_cmd[@]}"; printf '\n'
        fi
        printf 'Stage 2 (run from %s):' "$repo"; printf ' %q' "${pipeline_cmd[@]}"; printf '\n'
        return 0
    fi

    # Whether this invocation made the run decides whether it may remove it
    # again: a run that already existed, through --resume or --run-dir, is
    # someone else's record whatever this invocation does with it.
    local run_created_here=false
    [[ -d $run ]] || run_created_here=true
    mkdir -p "$run" "$masters" "$logs"
    # Record the capture as the run's source while it is still known. The
    # pipeline is handed the MASTER, so left to itself the run would name a
    # file inside itself as its own origin.
    if [[ ! -f $(run_manifest "$run") ]]; then
        run_manifest_init "$run" "$(run_class_for_profile "$profile")" prepare_video.sh \
            "$preservation_root" || true
        run_manifest_set "$run" --arg src "$source" '.sources = [$src]' || true
    fi

    run_set_current "$ai_root" "$run"
    write_wrapper_state "$run" "$source" "$master" "$profile" "$scale" \
        "$model" "$rife" "$crf" "$chunk_duration" "$chunk_threshold"

    if ! $prepare_needed; then
        if [[ -f $master ]]; then
            printf '=== Stage 1: skipped, master already present ===\n%s\n' "$master"
        else
            printf '=== Stage 1: skipped, prepared master already released ===\n%s\n' "$master"
        fi
    else
        printf '=== Stage 1: preparation ===\n'
        local prepare_status=0
        "${prepare_cmd[@]}" || prepare_status=$?
        if (( prepare_status == 3 )); then
            # Declined at preparation's prompt (prepare_video.sh exits 3 for
            # that and 1 for a failure). The run holds only the summary that
            # was declined, so a run made here is removed rather than left
            # beside real runs as one more unfinished run for --resume to
            # find. The pointer is cleared first: run_clear_current resolves
            # the link, which it cannot do once the directory is gone.
            if $run_created_here; then
                run_clear_current "$ai_root" "$run"
                run_discard_unstarted "$run" \
                    && printf '[INFO] Removed the unused run directory: %s\n' "$run"
            fi
            exit 3
        fi
        (( prepare_status == 0 )) || exit "$prepare_status"
        master=$(find "$masters" -maxdepth 1 -type f \( -name '*.mp4' -o -name '*.mkv' \) \
            2>/dev/null | sort | tail -1)
        [[ -n $master ]] || { printf '[ERROR] No master was produced in %s\n' "$masters" >&2; exit 1; }
        pipeline_cmd[2]=$master
        write_wrapper_state "$run" "$source" "$master" "$profile" "$scale" \
            "$model" "$rife" "$crf" "$chunk_duration" "$chunk_threshold"
    fi

    printf '\n=== Stage 2: upscaling ===\n'
    printf 'Run:    %s\n' "$run"
    printf 'Master: %s\n' "$(basename -- "$master")"
    printf 'Resume: %s --resume\n' "$0"
    # From the repository: the pipeline resolves inference_realesrgan_video.py
    # relative to the working directory, so launching it from the caller's
    # directory fails to find it.
    ( cd "$repo" && PIPELINE_RESUME_COMMAND="$0 --resume" "${pipeline_cmd[@]}" )

    if [[ $(run_manifest_get "$run" '.finished != null' 2>/dev/null || true) == true ]]; then
        run_clear_current "$ai_root" "$run"
    fi
    printf '\nRun directory: %s\n' "$run"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
