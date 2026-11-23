#!/bin/bash
# ==============================================================================
# Script Name: check_daily_ai_distribution.sh
# Description: Sanity-check that every source DV file for days that have been
#              assembled is represented as a chapter in its output video,
#              and that the assembled video's duration matches the sum of the
#              source .avi durations. Each chapter interval is also compared
#              with its individual source duration.
#
# A run audit reads the exact source paths and resolved recording times saved
# by upscale_dv_batch.sh. It therefore checks the same timestamp-derived day
# groups whether INPUT_ROOT is flat, conventionally grouped or arbitrarily
# nested. With --output-root and no run state, a per-day source view is
# still required because there is no recorded grouping decision to audit.
#
# For each resolved source day:
#   - If an assembled <day>.mp4 exists: verify every source .avi is
#     present as a chapter title.
#   - If no assembled day video exists but a later day IS assembled (i.e. the day
#     falls within the processing frontier): report as a GAP — this indicates
#     a day was skipped out of order.
#   - If the day is beyond the processing frontier (latest assembled day):
#     report as PENDING — not yet reached, not a problem.
#
# Chapter names come from the batch's durable recording_times.tsv when a run
# is checked. This includes captures grouped from DV subcode alone.
#
# Exit codes:
#   0  — all assembled days pass chapter and duration checks; no gaps detected
#   1  — a chapter, duration, segment-duration, or gap check failed
#   2  — usage or configuration error
#
# Usage:
#   pipeline_tools/check_daily_ai_distribution.sh [--input-root DIR] [--run-dir DIR]
#       [--day YYYYMMDD[_Loc]]
#   pipeline_tools/check_daily_ai_distribution.sh --help
#
# Requirements: ffprobe (ffmpeg package), python3, jq, bash >= 4.
# ==============================================================================
set -euo pipefail

# Command-line errors are exit 2 and point at --help rather than reprinting
# it, per dev_tools/SCRIPT_CONVENTIONS.md.
usage_error() { printf '[ERROR] %s\nTry %q --help for more information.\n' "$*" "$0" >&2; exit 2; }

main() {

# ------------------------------------------------------------------------------
# 1. Defaults (match upscale_dv_batch.sh)
# ------------------------------------------------------------------------------
PRESERVATION_ROOT="${VIDEO_PRESERVATION_ROOT:-${HOME}/VideoPreservation}"
INPUT_ROOT="${PRESERVATION_ROOT}/sources/dv/digital8-native"
# A run directory's videos/ holds the assembled day videos. --run-dir names it;
# with none, the newest run under the AI run root is checked. See
# pipeline_docs/storage-layout.md.
RUN_DIR=""
OUTPUT_ROOT=""
# Optional filter: if non-empty, check only these days (frontier logic is
# disabled when a filter is active — all filtered days are checked directly).
DAY_FILTER=()

# ------------------------------------------------------------------------------
# 2. Argument Parsing
# ------------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Verify that every source DV .avi file is represented as a chapter in the
assembled day video for each completed day. The total day duration and every
individual chapter-segment duration are compared with the source AVIs. Days
within the processing frontier that have no assembled day video are reported as gaps.

Options:
  --input-root DIR      Root containing the source DV captures. A run audit
                        supports any layout because it uses the run's recorded
                        source paths and recording times.
                        (default: $INPUT_ROOT)
  --run-dir DIR         Run directory whose videos/ holds the assembled day
                        videos (default: the newest Digital8 run under
                        \$VIDEO_PRESERVATION_ROOT/ai).
  --output-root DIR     Check a directory of assembled day videos directly, instead
                        of a run directory. For output that is not laid out as
                        a run -- an archive copied to another disk, say.
  --day YYYYMMDD[_Loc]  Check only this day.  May be repeated.  Disables
                        frontier-based gap detection.
  --help, -h            Show this help.

Examples:
  # Check all source days, detect gaps:
  $0

  # Check a single day:
  $0 --day 20040707
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input-root)  INPUT_ROOT="$2";    shift 2 ;;
        --run-dir)     RUN_DIR="$2";       shift 2 ;;
        --output-root) OUTPUT_ROOT="$2";   shift 2 ;;
        --day)         DAY_FILTER+=("$2"); shift 2 ;;
        --help|-h)     usage; exit 0 ;;
        *) usage_error "Unknown argument: '$1'" ;;
    esac
done

# ------------------------------------------------------------------------------
# 3. Tool Check
# ------------------------------------------------------------------------------
for tool in ffprobe python3 jq; do
    if ! command -v "$tool" &>/dev/null; then
        echo "Error: Required tool not found in PATH: $tool" >&2
        [[ "$tool" == "ffprobe" ]] && echo "       ffprobe is part of the ffmpeg package" >&2
        exit 2
    fi
done

INPUT_ROOT=$(realpath "$INPUT_ROOT")
if [[ -z $OUTPUT_ROOT ]]; then
    if [[ -z $RUN_DIR ]]; then
        # The newest DIGITAL8 run by its manifest's started time. The AI root
        # holds every class, and checking
        # a Hi8 or model-test run for Digital8 day videos would report every
        # day as a gap -- a confident answer to a question that was never asked
        # of that run.
        local candidate
        while IFS= read -r candidate; do
            [[ -f $candidate/run.json ]] || continue
            [[ $(jq -r '.class // empty' "$candidate/run.json" 2>/dev/null) == digital8 ]] || continue
            RUN_DIR=$candidate
            break
        done < <(python3 "$(dirname -- "${BASH_SOURCE[0]}")/run_names.py" \
            "${PRESERVATION_ROOT}/ai")
        [[ -n $RUN_DIR ]] || {
            echo "Error: no Digital8 run found under ${PRESERVATION_ROOT}/ai" >&2
            echo "       Pass --run-dir to check a specific run." >&2
            exit 1
        }
        echo "Run:          $RUN_DIR"
    fi
    OUTPUT_ROOT="${RUN_DIR}/videos"
fi
OUTPUT_ROOT=$(realpath "$OUTPUT_ROOT")

if [[ ! -d "$INPUT_ROOT" ]]; then
    echo "Error: INPUT_ROOT does not exist: $INPUT_ROOT" >&2
    exit 2
fi
if [[ ! -d "$OUTPUT_ROOT" ]]; then
    echo "Error: OUTPUT_ROOT does not exist: $OUTPUT_ROOT" >&2
    exit 2
fi

# Materialize the batch's recorded grouping as a private symlink view for the
# checks below. This keeps one implementation of the detailed chapter and
# duration checks while removing their former dependency on source directory
# names and timestamped source filenames. The links are never published and
# the original captures remain untouched.
AUDIT_SOURCE_ROOT="$INPUT_ROOT"
AUDIT_VIEW=""
cleanup_audit_view() {
    local status=$?
    trap - EXIT INT TERM
    [[ -z "$AUDIT_VIEW" || ! -d "$AUDIT_VIEW" ]] || rm -rf -- "$AUDIT_VIEW"
    exit "$status"
}
trap cleanup_audit_view EXIT INT TERM

if [[ -n "$RUN_DIR" && -f "$RUN_DIR/run.json" \
      && -f "$RUN_DIR/state/recording_times.tsv" ]]; then
    AUDIT_VIEW=$(mktemp -d "${TMPDIR:-/tmp}/daily-ai-audit.XXXXXX")
    local source_count=0 source_file source_stem recorded_at day clock link_name
    while IFS= read -r -d '' source_file; do
        if [[ ! -f "$source_file" ]]; then
            echo "Error: source recorded by the run is missing: $source_file" >&2
            exit 1
        fi
        source_stem=$(basename -- "$source_file"); source_stem=${source_stem%.avi}
        recorded_at=$(awk -F '\t' -v want="$source_stem" '
            $1 == want { value = $2; count++ }
            END { if (count == 1) print value; else exit 1 }
        ' "$RUN_DIR/state/recording_times.tsv") || {
            echo "Error: expected exactly one recorded time for $source_stem in" >&2
            echo "       $RUN_DIR/state/recording_times.tsv" >&2
            exit 1
        }
        day=$(date -d "$recorded_at" +%Y%m%d 2>/dev/null) || {
            echo "Error: invalid recorded time for $source_stem: $recorded_at" >&2
            exit 1
        }
        clock=$(date -d "$recorded_at" +%H%M%S)
        mkdir -p -- "$AUDIT_VIEW/$day"
        link_name="dv.${day}-${clock}.${source_stem}.avi"
        if [[ -e "$AUDIT_VIEW/$day/$link_name" || -L "$AUDIT_VIEW/$day/$link_name" ]]; then
            echo "Error: duplicate recorded source identity: $day/$link_name" >&2
            exit 1
        fi
        ln -s -- "$source_file" "$AUDIT_VIEW/$day/$link_name"
        (( source_count++ )) || true
    done < <(jq -j '.sources[]?, "\u0000"' "$RUN_DIR/run.json")
    (( source_count > 0 )) || {
        echo "Error: run manifest records no Digital8 source captures: $RUN_DIR/run.json" >&2
        exit 1
    }
    INPUT_ROOT="$AUDIT_VIEW"
    echo "Source grouping: $source_count capture(s) from the run's resolved recording times"
    echo "Source archive:  $AUDIT_SOURCE_ROOT"
fi

# ------------------------------------------------------------------------------
# 4. Helper: extract chapter starts and names from a day video via ffprobe
# ------------------------------------------------------------------------------
# Emit each chapter as start_time<TAB>title in container order. JSON parsing
# avoids ffprobe's delimiter escaping ambiguities and preserves title spaces.
get_chapter_rows() {
    local video="$1"
    ffprobe -v error -show_chapters -of json "$video" 2>/dev/null |
        python3 -c '
import json, sys
for chapter in json.load(sys.stdin).get("chapters", []):
    start = chapter.get("start_time", "")
    title = chapter.get("tags", {}).get("title", "")
    print(f"{start}\t{title}")
'
}

# ------------------------------------------------------------------------------
# 5. Helper: sum durations of source .avi files via ffprobe
# ------------------------------------------------------------------------------
# Returns total duration in seconds (decimal) to stdout.
# Size-based estimation is not used because the format (dv1 vs dv2) and
# container overhead of these pre-2008 captures is unknown; ffprobe is the
# only reliable approach.
get_source_duration() {
    local -a files=("$@")
    local f d
    # Emit each file's duration on its own line, then sum in one awk pass.
    # Avoids bash subshell scoping issues with an accumulator variable.
    for f in "${files[@]}"; do
        d=$(ffprobe -v error -show_entries format=duration \
            -of default=noprint_wrappers=1:nokey=1 \
            "${INPUT_ROOT}/${day_name}/${f}" 2>/dev/null)
        if [[ -z "$d" ]]; then
            echo "WARN  [${day_name}] ffprobe failed on source file: $f" >&2
            return 1
        fi
        echo "$d"
    done | awk '{ sum += $1 } END { printf "%.6f\n", sum }'
}

# ------------------------------------------------------------------------------
# 5a. Helper: get duration of an assembled day video via ffprobe
# ------------------------------------------------------------------------------
get_mkv_duration() {
    local mkv="$1"
    ffprobe -v error -show_entries format=duration         -of default=noprint_wrappers=1:nokey=1         "$mkv" 2>/dev/null
}

# ------------------------------------------------------------------------------
# 6. Helper: derive expected chapter name from source .avi filename
# ------------------------------------------------------------------------------
# Input:  basename of the .avi, e.g. dv.20040707-094437.avi
# Output: "2004-07-07 09:44:37"  (or "2004-07-07 09:44" for short format)
# Returns 1 if the filename does not match the expected pattern.
avi_to_chapter_name() {
    local base="$1"
    if [[ "$base" =~ ^dv\.([0-9]{8})[-_]([0-9]{6}) ]]; then
        local d="${BASH_REMATCH[1]}"
        local t="${BASH_REMATCH[2]}"
        echo "${d:0:4}-${d:4:2}-${d:6:2} ${t:0:2}:${t:2:2}:${t:4:2}"
    elif [[ "$base" =~ ^dv\.([0-9]{8})-([0-9]{4}) ]]; then
        local d="${BASH_REMATCH[1]}"
        local t="${BASH_REMATCH[2]}"
        echo "${d:0:4}-${d:4:2}-${d:6:2} ${t:0:2}:${t:2:2}"
    else
        return 1
    fi
}

# ------------------------------------------------------------------------------
# 6. Helper: report any non-AVI files present in a source day directory
# ------------------------------------------------------------------------------
# Prints a NOTE line for each file that is not a dv.*.avi — these are files the
# batch script will silently skip and that may need manual attention.
report_non_avi_files() {
    local day="$1"
    local dir="$2"
    local -a others=()
    mapfile -t others < <(
        find -L "$dir" -maxdepth 1 -type f ! -name 'dv.*.avi' | sort
    )
    for f in "${others[@]}"; do
        echo "NOTE  [$day] Non-AVI file (not processed by batch): $(basename "$f")"
    done
}

# ------------------------------------------------------------------------------
# 7. Helper: extract the sortable YYYYMMDD date prefix from a day directory name
# ------------------------------------------------------------------------------
# Day names are YYYYMMDD or YYYYMMDD_Suffix; the date portion is always first.
day_date_key() {
    echo "${1:0:8}"
}

# ------------------------------------------------------------------------------
# 8. Main check loop
# ------------------------------------------------------------------------------
total_days=0
ok_days=0        # assembled day video passed both chapter and duration checks
fail_days=0      # assembled day video failed either chapter or duration checks
chapter_fail_days=0
gap_days=0       # no assembled day video but within the processing frontier
gap_day_names=() # names of gap days, repeated in the final summary
non_dv_days=()      # "day: files" entries requiring a separate workflow
pending_days=0   # beyond the processing frontier — not yet reached
dur_ok_days=0    # days where the day video's duration matched source total
dur_fail_days=0  # days where the day video's duration differed beyond tolerance
segment_fail_days=0
segment_files_checked=0
segment_files_failed=0
reprocess_days=()
total_files=0
missing_chapters=0
errors=0

# Build the list of source days to examine, sorted chronologically.
# Day directory names sort correctly as strings because they start with YYYYMMDD.
if [[ ${#DAY_FILTER[@]} -gt 0 ]]; then
    # A bare date matches a directory carrying a descriptive suffix, the same
    # way upscale_dv_batch.sh resolves --day. Taking the filter literally meant
    # --day 20040829 looked for a directory named exactly "20040829" and
    # reported the day missing, when what exists is 20040829_Visiting_Moms_Home
    # -- an audit tool answering "not found" for a day that is there.
    day_list=()
    for wanted in "${DAY_FILTER[@]}"; do
        if [[ -d "$INPUT_ROOT/$wanted" ]]; then
            day_list+=("$wanted")
            continue
        fi
        matched=false
        if [[ "$wanted" =~ ^[0-9]{8}$ ]]; then
            while IFS= read -r candidate; do
                day_list+=("$candidate")
                matched=true
            done < <(find "$INPUT_ROOT" -mindepth 1 -maxdepth 1 -type d \
                -printf '%f\n' 2>/dev/null | awk -v w="$wanted" \
                'substr($0,1,8) == w' | sort)
        fi
        $matched || {
            echo "Error: --day matched no directory under $INPUT_ROOT: $wanted" >&2
            exit 1
        }
    done
    use_frontier=false
else
    # Only dated directories are source-day candidates. This retains dated
    # directories containing non-DV media for explicit manual-gap reporting,
    # while excluding infrastructure directories such as "upscaled".
    mapfile -t day_list < <(
        find "$INPUT_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' |
            awk '/^[0-9]{8}([^0-9].*)?$/' | sort
    )
    use_frontier=true
fi

# Determine the processing frontier: the latest (lexicographically greatest)
# YYYYMMDD for which an assembled day video exists in OUTPUT_ROOT.
# Days with a date key <= frontier but no assembled day video are gaps.
# Days with a date key >  frontier are simply pending.
frontier="00000000"
if [[ "$use_frontier" == true ]]; then
    while IFS= read -r out_mkv; do
        out_day=${out_mkv%.mp4}
        # Only consider names starting with a valid YYYYMMDD. This also
        # excludes README.md and anything else filed alongside the videos.
        [[ "$out_day" =~ ^[0-9]{8} ]] || continue
        key=$(day_date_key "$out_day")
        [[ "$key" > "$frontier" ]] && frontier="$key"
    done < <(find "$OUTPUT_ROOT" -mindepth 1 -maxdepth 1 -type f \
        -name '*.mp4' -printf '%f\n')
    echo "Processing frontier: ${frontier} (latest assembled day)"
fi

for day_name in "${day_list[@]}"; do
    (( total_days++ )) || true

    local_in_dir="${INPUT_ROOT}/${day_name}"

    # Verify source directory exists (guards against bad --day arguments).
    if [[ ! -d "$local_in_dir" ]]; then
        echo "WARN  [$day_name] Input directory not found: $local_in_dir"
        (( errors++ )) || true
        continue
    fi

    # Collect processable DV sources before classifying assembly state. A
    # dated directory containing only other media is important inventory, but
    # upscale_dv_batch.sh cannot process it and must not call it pending DV.
    mapfile -t avi_files < <(
        find -L "$local_in_dir" -maxdepth 1 -name 'dv.*.avi' -type f \
            -printf '%f\n' | sort
    )
    if [[ ${#avi_files[@]} -eq 0 ]]; then
        echo "OTHER [$day_name] No dv.*.avi source files; this dated directory requires a separate media workflow."
        report_non_avi_files "$day_name" "$local_in_dir"
        mapfile -t manual_files < <(
            find -L "$local_in_dir" -maxdepth 1 -type f -printf '%f\n' | sort
        )
        manual_file_list="${manual_files[0]:-(no regular files)}"
        for manual_file in "${manual_files[@]:1}"; do
            manual_file_list+=", $manual_file"
        done
        non_dv_days+=( "$day_name: $manual_file_list" )
        (( errors++ )) || true
        continue
    fi

    # Find the assembled day video (exactly one expected).
    mapfile -t chapter_mkvs < <(
        find "$OUTPUT_ROOT" -maxdepth 1 -name "${day_name}.mp4" \
            -type f 2>/dev/null | sort
    )

    if [[ ${#chapter_mkvs[@]} -eq 0 ]]; then
        # No assembled day video.  Classify based on the frontier.
        if [[ "$use_frontier" == true ]]; then
            key=$(day_date_key "$day_name")
            if [[ ! "$key" > "$frontier" ]]; then
                echo "GAP   [$day_name] No assembled day video but later days are done — possible skip."
                report_non_avi_files "$day_name" "$local_in_dir"
                (( gap_days++ )) || true
                gap_day_names+=("$day_name")
                (( errors++ )) || true
            else
                echo "PEND  [$day_name] Beyond processing frontier — not yet reached."
                (( pending_days++ )) || true
            fi
        else
            echo "SKIP  [$day_name] No assembled day video found."
        fi
        continue
    fi

    if [[ ${#chapter_mkvs[@]} -gt 1 ]]; then
        echo "WARN  [$day_name] Multiple day videos found — manual inspection needed:"
        printf "        %s\n" "${chapter_mkvs[@]}"
        (( errors++ )) || true
        # Continue with the first one so we still check what we can.
    fi

    chapter_mkv="${chapter_mkvs[0]}"

    # Extract chapter starts and names from the day video in timeline order.
    mapfile -t chapter_rows < <(get_chapter_rows "$chapter_mkv")
    mkv_chapters=()
    chapter_starts=()
    for row in "${chapter_rows[@]}"; do
        chapter_starts+=( "${row%%$'\t'*}" )
        mkv_chapters+=( "${row#*$'\t'}" )
    done

    chapter_check_ok=true
    if [[ ${#mkv_chapters[@]} -eq 0 ]]; then
        echo "WARN  [$day_name] Could not extract any chapter names from:"
        echo "        $chapter_mkv"
        chapter_check_ok=false
    fi

    # Build a lookup set of chapter names present in the day video.
    declare -A chapter_set=()
    for ch in "${mkv_chapters[@]}"; do
        chapter_set["$ch"]=1
    done

    # Check each source .avi against the chapter set.
    day_missing=0
    for avi in "${avi_files[@]}"; do
        (( total_files++ )) || true
        expected_name=$(avi_to_chapter_name "$avi") || {
            echo "WARN  [$day_name] Cannot parse timestamp from filename: $avi"
            chapter_check_ok=false
            (( errors++ )) || true
            continue
        }
        if [[ -z "${chapter_set[$expected_name]+_}" ]]; then
            echo "MISS  [$day_name] Missing chapter: '$expected_name'  ($avi)"
            (( day_missing++ )) || true
            (( missing_chapters++ )) || true
        fi
    done
    unset chapter_set

    if [[ $day_missing -gt 0 ]]; then
        echo "FAIL  [$day_name] $day_missing / ${#avi_files[@]} source files missing from chapters."
        chapter_check_ok=false
    fi
    if [[ "$chapter_check_ok" == false ]]; then
        (( chapter_fail_days++ )) || true
        (( errors++ )) || true
    else
        echo "OK    [$day_name] All ${#avi_files[@]} source files present in chapters."
    fi
    report_non_avi_files "$day_name" "$local_in_dir"

    # Check duration independently of chapter correctness. Allow 100 ms per
    # source clip plus 100 ms for final assembly timestamp rounding.
    duration_check_ok=false
    segment_check_ok=true
    src_total=''
    mkv_dur=''
    dur_delta=''
    dur_tolerance=$(awk -v n="${#avi_files[@]}" \
        'BEGIN { printf "%.1f", n * 0.1 + 0.1 }')
    src_total=$(get_source_duration "${avi_files[@]}") || src_total=""
    mkv_dur=$(get_mkv_duration "$chapter_mkv")         || mkv_dur=""
    if [[ -n "$src_total" && -n "$mkv_dur" ]]; then
        dur_delta=$(awk -v a="$mkv_dur" -v b="$src_total" \
            'BEGIN { d = a - b; if (d < 0) d = -d; printf "%.3f", d }')
        if awk -v d="$dur_delta" -v t="$dur_tolerance" \
            'BEGIN { exit (d <= t) ? 0 : 1 }'; then
            echo "DUR   [$day_name] Duration OK: source ${src_total}s  day video ${mkv_dur}s  delta ${dur_delta}s  (tolerance ${dur_tolerance}s)"
            duration_check_ok=true
            (( dur_ok_days++ )) || true
        else
            echo "DUR   [$day_name] Duration MISMATCH: source ${src_total}s  day video ${mkv_dur}s  delta ${dur_delta}s  (tolerance ${dur_tolerance}s)"
            (( dur_fail_days++ )) || true
            (( errors++ )) || true
        fi

        # Validate every assembled segment independently. Whole-day totals can
        # hide a short clip when their tolerance grows with the number of
        # sources. Chapter boundaries expose the duration actually retained
        # for each source: the next chapter start ends the segment, and the
        # assembled container duration ends the final segment.
        if [[ ${#chapter_starts[@]} -ne ${#avi_files[@]} ]]; then
            echo "SEGD  [$day_name] Cannot compare segments: ${#chapter_starts[@]} chapters for ${#avi_files[@]} source files."
            segment_check_ok=false
        else
            for index in "${!avi_files[@]}"; do
                avi="${avi_files[$index]}"
                expected_name=$(avi_to_chapter_name "$avi") || expected_name=""
                actual_name="${mkv_chapters[$index]}"
                (( segment_files_checked++ )) || true
                if [[ "$actual_name" != "$expected_name" ]]; then
                    echo "SEGD  [$day_name] Segment $((index + 1)) order mismatch: '$actual_name', expected '$expected_name'."
                    segment_check_ok=false
                    (( segment_files_failed++ )) || true
                    continue
                fi
                source_duration=$(ffprobe -v error -show_entries format=duration \
                    -of default=noprint_wrappers=1:nokey=1 \
                    "${local_in_dir}/${avi}" 2>/dev/null) || source_duration=""
                segment_start="${chapter_starts[$index]}"
                if (( index + 1 < ${#chapter_starts[@]} )); then
                    segment_end="${chapter_starts[$((index + 1))]}"
                else
                    segment_end="$mkv_dur"
                fi
                if [[ -z "$source_duration" || -z "$segment_start" || -z "$segment_end" ]]; then
                    echo "SEGD  [$day_name] Could not measure segment for $avi."
                    segment_check_ok=false
                    (( segment_files_failed++ )) || true
                    continue
                fi
                segment_duration=$(awk -v e="$segment_end" -v s="$segment_start" \
                    'BEGIN { printf "%.6f", e - s }')
                segment_delta=$(awk -v a="$segment_duration" -v b="$source_duration" \
                    'BEGIN { d = a - b; if (d < 0) d = -d; printf "%.3f", d }')
                if ! awk -v d="$segment_delta" 'BEGIN { exit (d <= 0.100) ? 0 : 1 }'; then
                    echo "SEGD  [$day_name] Segment duration MISMATCH: $avi source ${source_duration}s, assembled ${segment_duration}s, delta ${segment_delta}s (tolerance 0.100s)."
                    segment_check_ok=false
                    (( segment_files_failed++ )) || true
                fi
            done
        fi
        if [[ "$segment_check_ok" == true ]]; then
            echo "SEGD  [$day_name] All ${#avi_files[@]} segment durations OK (tolerance 0.100s each)."
        else
            (( segment_fail_days++ )) || true
            (( errors++ )) || true
        fi
    else
        echo "WARN  [$day_name] Could not determine duration for comparison."
        (( dur_fail_days++ )) || true
        (( errors++ )) || true
    fi

    if [[ "$chapter_check_ok" == true && "$duration_check_ok" == true &&
          "$segment_check_ok" == true ]]; then
        (( ok_days++ )) || true
    else
        (( fail_days++ )) || true
        reasons=()
        [[ "$chapter_check_ok" == true ]] || reasons+=( "chapters" )
        [[ "$duration_check_ok" == true ]] || reasons+=( "whole-day duration" )
        [[ "$segment_check_ok" == true ]] || reasons+=( "segment duration" )
        reason_text="${reasons[0]}"
        for reason in "${reasons[@]:1}"; do
            reason_text+=", $reason"
        done
        reprocess_days+=( "$day_name ($reason_text)" )
    fi

done

# ------------------------------------------------------------------------------
# 9. Summary
# ------------------------------------------------------------------------------
echo ""
echo "========================================================"
echo "Chapter check complete."
echo ""
echo "  Days (from source):"
printf "    %-27s %s\n" "Examined:" "$total_days"
printf "    %-27s %s\n" "OK (all checks):" "$ok_days"
printf "    %-27s %s\n" "Failed assembled:" "$fail_days"
if [[ ${#reprocess_days[@]} -gt 0 ]]; then
    echo "    REPROCESS REQUIRED:"
    printf "      %s\n" "${reprocess_days[@]}"
fi
printf "    %-27s %s\n" "Failed chapters:" "$chapter_fail_days"
printf "    %-27s %s\n" "DV gaps:" "$gap_days"
if [[ ${#gap_day_names[@]} -gt 0 ]]; then
    printf "      Gap day:           %s\n" "${gap_day_names[@]}"
fi
printf "    %-27s %s\n" "Other-media days:" "${#non_dv_days[@]}"
if [[ ${#non_dv_days[@]} -gt 0 ]]; then
    printf "      %s\n" "${non_dv_days[@]}"
fi
printf "    %-27s %s\n" "Pending (not yet reached):" "$pending_days"
echo ""
echo "  DV AVI source files (dv.*.avi only):"
echo "    Total checked:       $total_files"
echo "    Missing from day video: $missing_chapters"
echo ""
echo "  Duration checks:"
echo "    Whole days:          $dur_ok_days passed, $dur_fail_days failed"
echo "    Source segments:     $((segment_files_checked - segment_files_failed)) passed, $segment_files_failed failed across $segment_fail_days day(s)"
echo "========================================================"

if [[ $errors -gt 0 ]]; then
    exit 1
fi
exit 0

}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
