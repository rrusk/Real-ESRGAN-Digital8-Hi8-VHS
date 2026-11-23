#!/usr/bin/env bash
# The AI pipeline run directory: creation, adoption, manifest, and layout.
# Sourced by every AI entry point. Callers must define die().
#
# pipeline_docs/storage-layout.md is the specification. This file is the only
# place that turns it into paths, so the layout is defined once rather than
# reassembled from string concatenation in four scripts.

# Tolerances are declared in FRAMES and converted here, never written as a
# number of milliseconds. A constant 0.100s reads as a small number and is
# three pictures at 29.97 fps and two and a half at PAL 25 -- it hides both its
# size and its dependence on the frame rate. One picture is the smallest unit
# these checks can meaningfully be wrong by.
# Assigned rather than declared readonly. This library is sourced by tools and
# again by tests that extract a function from a tool, and a second `readonly`
# on an already-readonly name is fatal: it aborts the sourcing script partway
# through, leaving every function defined after this point missing.
: "${TOLERANCE_FRAMES:=1}"

# Compare video stream durations directly. Container duration can include AAC
# block padding, which is not video drift and must not expand the tolerance.

frames_to_seconds() {
    # frames_to_seconds <frames> <fps>  ->  seconds, to milliseconds
    awk -v frames="$1" -v fps="$2" 'BEGIN {
        if (fps <= 0) fps = 29.97
        printf "%.3f", frames / fps
    }'
}

# The directory holding every run. Nothing else lives here.
run_root() {
    local preservation_root=${1:-${VIDEO_PRESERVATION_ROOT:-$HOME/VideoPreservation}}
    printf '%s/ai\n' "$preservation_root"
}

# A run id groups the same source together, oldest run first:
#   <label>_<YYYYMMDD-HHMMSS>
# The label is sanitised because it is derived from a source filename, which
# may contain characters that would make the id awkward as a path component.
run_make_id() {
    local label=$1 clean
    clean=$(printf '%s' "$label" | tr -c '[:alnum:]._-' '_' | sed 's/__*/_/g; s/^_//; s/_$//')
    [[ -n $clean ]] || clean=run
    printf '%s_%s\n' "$clean" "$(date +%Y%m%d-%H%M%S)"
}

run_dirs_newest_first() {
    # Parse time zones too: lexical timestamp order is not instant order.
    python3 "$(dirname -- "${BASH_SOURCE[0]}")/run_names.py" "$1"
}

# Create the run directory and the subdirectories a run always has. videos/,
# logs/, state/ and work/ are created on demand by the code that writes into
# them, so a run that never resumes has no state/ and a finished run has no
# work/ -- an absent directory means nothing was written there.
run_create() {
    local run_dir=$1
    mkdir -p "$run_dir" || return 1
    printf '%s\n' "$run_dir"
}

# A run records work. One that was declined at a preflight prompt records
# nothing, and leaving it beside real runs is clutter that the next listing has
# to explain away. Callers use this only for a run they created in this
# invocation; a resumed or adopted run, or one named by --run-dir that already
# existed, is someone else's record. The videos and work check is a guard, not
# the rule: anything produced -- a master, a chunk, an output -- makes the run a
# record and it stays. A current_run pointer at the run is the caller's to
# clear, and to clear first: run_clear_current resolves the link, which it
# cannot do once the directory is gone.
run_discard_unstarted() {
    local run_dir=$1
    [[ -d $run_dir ]] || return 0
    if find "$run_dir/videos" "$run_dir/work" -type f -print -quit 2>/dev/null | grep -q .; then
        return 1
    fi
    rm -rf -- "$run_dir"
}

run_videos_dir()  { printf '%s/videos\n' "$1"; }
run_logs_dir()    { printf '%s/logs\n' "$1"; }
run_state_dir()   { printf '%s/state\n' "$1"; }
run_work_dir()    { printf '%s/work\n' "$1"; }
run_manifest()    { printf '%s/run.json\n' "$1"; }

# Everything recorded about processing one source lives together, named for the
# source rather than for the moment the log was archived. This is for a driver
# with several sources to keep apart; a run with a single source writes its
# logs directly into logs/, since the extra level would only repeat the name
# the run directory already carries.
run_source_log_dir() {
    local run_dir=$1 source_path=$2 stem
    stem=$(basename -- "$source_path"); stem=${stem%.*}
    printf '%s/logs/%s\n' "$run_dir" "$stem"
}

# --- Manifest ----------------------------------------------------------------
#
# run.json is written once at creation and updated in place afterwards. Every
# update goes through a temporary file and a rename, so an interrupted run
# leaves either the previous manifest or the new one, never a truncated file
# that no longer parses -- which would make the run unadoptable and its state
# unreadable.

run_manifest_init() {
    # run_manifest_init <run_dir> <class> <entry_point> <preservation_root>
    local run_dir=$1 class=$2 entry=$3 preservation_root=$4
    local manifest tmp
    manifest=$(run_manifest "$run_dir")
    tmp=$(mktemp "${manifest}.XXXXXX") || return 1
    jq -n \
        --arg run_id "$(basename -- "$run_dir")" \
        --arg class "$class" \
        --arg entry_point "$entry" \
        --arg preservation_root "$preservation_root" \
        --arg started "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{run_id: $run_id, class: $class, entry_point: $entry_point,
          preservation_root: $preservation_root,
          started: $started, finished: null,
          command: [], settings: {}, sources: [], videos: [], excluded: []}' \
        > "$tmp" || { rm -f "$tmp"; return 1; }
    mv -f "$tmp" "$manifest"
}

run_manifest_set() {
    # run_manifest_set <run_dir> <jq filter> [jq args...]
    # Applies a filter to the manifest in place.
    local run_dir=$1; shift
    local manifest tmp
    manifest=$(run_manifest "$run_dir")
    [[ -f $manifest ]] || return 1
    tmp=$(mktemp "${manifest}.XXXXXX") || return 1
    jq "$@" > "$tmp" < "$manifest" || { rm -f "$tmp"; return 1; }
    mv -f "$tmp" "$manifest"
}

run_manifest_get() {
    # run_manifest_get <run_dir> <jq filter>
    local manifest
    manifest=$(run_manifest "$1")
    [[ -f $manifest ]] || return 1
    jq -r "$2" < "$manifest" 2>/dev/null
}

run_manifest_finish() {
    run_manifest_set "$1" --arg finished "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '.finished = $finished'
}

# --- Adoption ----------------------------------------------------------------
#
# A run id is created once and survives resumption: a batch stopped and re-run
# continues into its existing directory rather than opening a second one. A run
# is adoptable when its manifest is unfinished and its configuration matches
# what the caller is about to do, which is what stops a resume with different
# settings from silently appending incompatible output to an earlier run.

run_find_adoptable() {
    # run_find_adoptable <root> <class> <config_json>
    # Prints the newest adoptable run directory, or nothing.
    local root=$1 class=$2 config=$3 dir
    [[ -d $root ]] || return 1
    while IFS= read -r dir; do
        [[ -f $dir/run.json ]] || continue
        jq -e --arg class "$class" --argjson want "$config" \
            'select(.finished == null) | select(.class == $class)
             | select(.settings == $want)' "$dir/run.json" >/dev/null 2>&1 || continue
        printf '%s\n' "$dir"
        return 0
    done < <(run_dirs_newest_first "$root")
    return 1
}

# --- Current run -------------------------------------------------------------
#
# A stable name removes the timestamped run id from the normal resume command.
# It is a symlink, so current_run/run.json and current_run/logs/ are ordinary
# paths and no second directory or copied state can drift away from the run.

run_current_path() {
    printf '%s/current_run\n' "$1"
}

run_set_current() {
    # run_set_current <root> <run_dir>
    local root=$1 run_dir=$2 link tmp target
    mkdir -p "$root" || return 1
    link=$(run_current_path "$root")
    if [[ -e $link && ! -L $link ]]; then
        printf '[ERROR] Refusing to replace non-symlink current-run path: %s\n' "$link" >&2
        return 1
    fi
    run_dir=$(realpath -m -- "$run_dir") || return 1
    target=$run_dir
    if [[ $(dirname -- "$run_dir") == $(realpath -m -- "$root") ]]; then
        target=$(basename -- "$run_dir")
    fi
    # Keep a failed staging link visible. If mv cannot install it, the
    # current_run.tmp.<pid> name explains what was being attempted and can be
    # inspected or removed without needing `ls -a`.
    tmp="$root/current_run.tmp.$$"
    ln -s -- "$target" "$tmp" || return 1
    mv -Tf -- "$tmp" "$link"
}

run_get_current() {
    # run_get_current <root>; prints a valid current run or nothing.
    local link resolved
    link=$(run_current_path "$1")
    [[ -L $link ]] || return 1
    resolved=$(realpath -e -- "$link" 2>/dev/null) || return 1
    [[ -d $resolved ]] || return 1
    printf '%s\n' "$resolved"
}

run_clear_current() {
    # Remove the pointer only if it still names this run. A newer invocation
    # may have made another run current while this one was finishing.
    local root=$1 run_dir=$2 link resolved
    link=$(run_current_path "$root")
    [[ -L $link ]] || return 0
    resolved=$(realpath -e -- "$link" 2>/dev/null) || return 0
    if [[ $resolved == $(realpath -m -- "$run_dir") ]]; then
        rm -f -- "$link"
    fi
}

run_find_latest_unfinished() {
    # run_find_latest_unfinished <root> <class>
    # Adopt an unfinished Hi8 run when no active-run pointer is available.
    local root=$1 class=$2 dir
    [[ -d $root ]] || return 1
    while IFS= read -r dir; do
        [[ -f $dir/run.json ]] || continue
        jq -e --arg class "$class" \
            'select(.finished == null) | select(.class == $class)' \
            "$dir/run.json" >/dev/null 2>&1 || continue
        printf '%s\n' "$dir"
        return 0
    done < <(run_dirs_newest_first "$root")
    return 1
}
