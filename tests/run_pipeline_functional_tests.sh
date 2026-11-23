#!/usr/bin/env bash
# Functional tests for the AI artifact tools.
# Version: 1.0.0
#
# Where run_pipeline_shell_tests.sh checks that the pipeline scripts present a
# correct interface, this suite checks the behaviour the storage layout depends
# on: that a run directory is built where pipeline_docs/storage-layout.md says
# it is, that its manifest survives interruption, and that a stopped batch
# resumes into the run it started rather than opening a second one beside it.
#
# Adoption is the one with teeth. A batch is usually not finished when it
# stops, so nearly every real run is resumed at least once; if adoption picked
# the wrong run -- or none -- a run's output would be split across directories,
# which is the failure the whole layout exists to prevent.
#
# No GPU, no model weights and no real media: run directories are ordinary
# files and JSON, so the real code paths run against synthetic ones.
#
# Usage: tests/run_pipeline_functional_tests.sh [OPTIONS]
#   -v, --verbose   Print every passing check.
#   --only PATTERN  Run only tests whose name contains PATTERN.
#   -V, --version   Print version and exit.
#   -h, --help      Show this help and exit.

set -uo pipefail

VERSION="1.0.0"
VERBOSE=false
ONLY=""

RUN=0; PASS=0; FAIL=0; SKIP=0
declare -a FAILURES=()

# shellcheck source=../pipeline_tools/run_lib.sh
die() { printf '[ERROR] %s\n' "$*" >&2; return 1; }

want() { [[ -z $ONLY ]] && return 0; [[ $1 == *"$ONLY"* ]]; }

# The pipeline is stdlib-only and these checks stop before inference. Use an
# available Python interpreter and export VIRTUAL_ENV at the top level so the
# pipeline guard accepts the test environment in child processes.
pipeline_python() {
    [[ -n $PIPELINE_PY ]] || return 1
    printf '%s' "$PIPELINE_PY"
}

run_test() {  # run_test <name> <function>
    want "$1" || return 0
    RUN=$((RUN+1))
    printf '  [%2d] %-52s ' "$RUN" "$1"
    local out rc
    out=$("$2" 2>&1); rc=$?
    if (( rc == 77 )); then SKIP=$((SKIP+1)); printf 'skip  (%s)\n' "$out"
    elif (( rc == 0 )); then PASS=$((PASS+1)); printf 'ok\n'
        $VERBOSE && [[ -n $out ]] && printf '%s\n' "$out"
    else FAIL=$((FAIL+1)); FAILURES+=("$1 -- $out"); printf 'FAIL\n'
        printf '%s\n' "$out" | sed 's/^/       /'
    fi
    return 0
}

# A run directory built by the library the tools use, so the tests and the
# tools cannot drift apart about what the layout is.
mkrun() {  # mkrun <root> <label> <class> <settings-json> [finished]
    local root=$1 label=$2 class=$3 settings=$4 finished=${5:-}
    local dir
    dir="$root/$(run_make_id "$label")"
    mkdir -p "$dir"
    run_manifest_init "$dir" "$class" test.sh "$root" || return 1
    run_manifest_set "$dir" --argjson s "$settings" '.settings = $s' || return 1
    [[ -z $finished ]] || run_manifest_set "$dir" --arg f "$finished" '.finished = $f' || return 1
    printf '%s\n' "$dir"
}

# Assembly runs against real media: chapter placement is a property of what
# MP4Box actually produces, and cannot be exercised with stubs. The clips are
# tiny and synthetic, and carry AAC audio deliberately -- audio block padding is
# what moves append boundaries away from the picture counts, which is the
# condition the placement has to survive.
extract_assembly() {  # extract_assembly <dir>
    local d=$1
    {
        # Prints rather than discards: assembly reports its own verification
        # results through log(), so a silent stub hides the very statements a
        # test may need to assert on.
        printf 'log() { printf "%%s\\n" "$*"; }\n'
        printf 'DRY_RUN=false\nSCALE=2\n'
        printf 'SCRIPT_DIR=%q\n' "$ROOT"
        printf 'source %q\n' "$ROOT/pipeline_tools/run_lib.sh"
        # Extracted by brace depth from a literal match on the definition
        # line. A line-anchored range ("to the next line starting with }")
        # silently swallowed the functions that followed, because some of these
        # close on an indented brace, and produced a file that would not parse.
        local fn
        for fn in is_seconds get_numeric_frame_rate packet_bounds assemble_day_chapters; do
            awk -v want="$fn() {" '
                index($0, want) == 1 { infn = 1 }
                infn {
                    print
                    depth += gsub(/[{]/, "{") - gsub(/[}]/, "}")
                    if (depth == 0) exit
                }' "$ROOT/upscale_dv_batch.sh"
        done
        # Recording times come from the batch's pre-flight, which is not part
        # of assembly. Supplied here so the chapter NAMES resolve; this test is
        # about where the marks land, not what they are called.
        printf 'output_recording_time() { printf "2004-04-23 17:34:19\\n"; }\n'
    } >"$d/assembly.sh"
    grep -q 'assemble_day_chapters' "$d/assembly.sh"
}

make_clip() {  # make_clip <path> <seconds>
    ffmpeg -v error -f lavfi -i "testsrc=size=64x48:rate=60000/1001:duration=$2" \
        -f lavfi -i "sine=frequency=440:sample_rate=32000:duration=$2" \
        -c:v libx264 -preset ultrafast -crf 40 -pix_fmt yuv420p \
        -c:a aac -shortest -y "$1" 2>/dev/null
}

frames_of() { ffprobe -v error -select_streams v:0 -count_packets \
    -show_entries stream=nb_read_packets -of csv=p=0 "$1" 2>/dev/null; }

t_chapter_marks_land_on_the_clip_they_name() {
    local d=$TMPROOT/assembly; mkdir -p "$d"
    extract_assembly "$d" || { echo "could not extract assemble_day_chapters"; return 1; }
    command -v MP4Box >/dev/null || { echo "MP4Box not installed"; return 77; }

    local -a clips=()
    local i
    for i in 0 1 2 3 4; do
        make_clip "$d/clip$i.mp4" 0.217 || { echo "could not build clip $i"; return 1; }
        clips+=("$d/clip$i.mp4")
    done
    local per; per=$(frames_of "$d/clip0.mp4")
    [[ $per =~ ^[0-9]+$ ]] && (( per > 0 )) || { echo "clip has no pictures"; return 1; }

    mkdir -p "$d/out"
    ( source "$d/assembly.sh"; assemble_day_chapters day "$d/out" "${clips[@]}" -- "${clips[@]}" ) >"$d/run.log" 2>&1 \
        || { echo "assembly failed: $(tail -3 "$d/run.log")"; return 1; }

    local merged="$d/out/day.mp4"
    [[ -f $merged ]] || { echo "no assembled file produced"; return 1; }

    # Every chapter must sit on the picture that begins its clip. Predicted
    # positions (cumulative frames / fps) drift from the real ones because
    # appending does not preserve that invariant -- measured at 19 frames by
    # the tenth clip -- so this compares against the MEASURED timeline.
    local -a pts=() marks=()
    mapfile -t pts < <(ffprobe -v error -select_streams v:0 -show_entries packet=pts_time \
        -of csv=p=0 "$merged" 2>/dev/null | sort -n)
    mapfile -t marks < <(ffprobe -v error -show_chapters -of csv=p=0 "$merged" 2>/dev/null | cut -d, -f4)
    (( ${#marks[@]} == ${#clips[@]} )) \
        || { echo "expected ${#clips[@]} chapters, found ${#marks[@]}"; return 1; }

    local n idx delta worst=0
    for n in "${!marks[@]}"; do
        idx=$(( n * per ))
        delta=$(awk -v a="${marks[$n]}" -v b="${pts[$idx]}" \
            'BEGIN { d = a - b; if (d < 0) d = -d; printf "%.6f", d }')
        worst=$(awk -v w="$worst" -v d="$delta" 'BEGIN { print (d > w) ? d : w }')
        awk -v d="$delta" 'BEGIN { exit (d > 0.002) ? 0 : 1 }' && {
            echo "chapter $((n+1)) at ${marks[$n]}s but its clip starts at ${pts[$idx]}s (delta ${delta}s)"
            return 1
        }
    done
    echo "worst chapter placement error: ${worst}s across ${#marks[@]} chapters"
}

t_assembled_day_is_recorded_as_the_file_it_created() {
    # The caller's bookkeeping, which the assembly test does not reach. Assembly
    # wrote <day>.mp4 while the caller recorded <day>.mkv in both the batch
    # state and run.json, then deleted the components and marked the day done:
    # a manifest naming a file that does not exist, a run.md reporting the real
    # output as missing, and a resume trusting all of it. Nothing failed.
    command -v jq >/dev/null 2>&1 || { echo "no jq"; return 77; }
    local recorded manifest
    # The two recording sites, read out of the script rather than re-derived,
    # so this notices an edit that changes only one of them.
    recorded=$(grep -c 'VIDEOS_DIR}/${day_name}\.mp4' "$ROOT/upscale_dv_batch.sh")
    manifest=$(grep -c -- '--arg v "${day_name}\.mp4"' "$ROOT/upscale_dv_batch.sh")
    (( recorded == 1 )) || { echo "batch state does not record <day>.mp4 (found $recorded)"; return 1; }
    (( manifest == 1 )) || { echo "run.json does not record <day>.mp4 (found $manifest)"; return 1; }
    ! grep -q 'day_name}\.mkv' "$ROOT/upscale_dv_batch.sh" \
        || { echo "a <day>.mkv path is still recorded somewhere"; return 1; }

    # And the name the assembler actually writes agrees with what is recorded.
    local d="$TMPROOT/day-name" out
    mkdir -p "$d/out"
    extract_assembly "$d" || { echo "could not extract assemble_day_chapters"; return 1; }
    command -v MP4Box >/dev/null || { echo "MP4Box not installed"; return 77; }
    make_clip "$d/a.mp4" 0.217 || return 1
    ( source "$d/assembly.sh"; assemble_day_chapters 20040423 "$d/out" "$d/a.mp4" -- "$d/a.mp4" ) >"$d/log" 2>&1 \
        || { echo "assembly failed: $(tail -3 "$d/log")"; return 1; }
    out=$(find "$d/out" -maxdepth 1 -type f -name '20040423.*' -printf '%f\n')
    [[ $out == "20040423.mp4" ]] \
        || { echo "assembly wrote '$out', but the caller records 20040423.mp4"; return 1; }
}

t_packet_bounds_handle_awkward_probe_output() {
    # Probe bounds must include every packet's end, handle out-of-order rows,
    # and reject malformed or failed probes. Stubs exercise those cases directly.
    command -v python3 >/dev/null 2>&1 || { echo "no python3"; return 77; }
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    local d="$TMPROOT/bounds"; mkdir -p "$d/bin"

    # Out-of-order rows whose greatest end does NOT belong to the last-starting
    # packet: the 1.0 packet runs to 3.0, the later 2.0 packet ends at 2.1.
    cat >"$d/rows_overlap" <<'ROWS'
1.000000,2.000000

2.000000,0.100000
0.500000,0.250000
ROWS
    # One good row, then a failed probe's N/A.
    printf '0.000000,0.016683\nN/A,N/A\n' >"$d/rows_na"
    # float() accepts these where a numeric regex does not. nan is the one that
    # matters: comparisons against it are false, so an unmeasurable position
    # would satisfy the offset check instead of failing it.
    printf 'nan,0.016683\n' >"$d/rows_nan"
    printf '0.000000,inf\n' >"$d/rows_inf"
    # More fields than the two this parses. Reading the first two would be a
    # guess about what the rest meant.
    printf '0.000000,0.016683,unexpected\n' >"$d/rows_extra"
    # Nonzero exit with plausible output.
    printf '0.000000,0.016683\n' >"$d/rows_rc"

    make_stub() {  # make_stub <rows-file> <exit-code>
        cat >"$d/bin/ffprobe" <<STUB
#!/usr/bin/env bash
cat $(printf '%q' "$1")
exit $2
STUB
        chmod +x "$d/bin/ffprobe"
    }

    # --- shell helper ---
    local shell_bounds
    eval "$(awk '/^packet_bounds\(\) \{/{i=1} i{print; d+=gsub(/[{]/,"{")-gsub(/[}]/,"}"); if(d==0)exit}' \
        "$ROOT/upscale_dv_batch.sh")"

    make_stub "$d/rows_overlap" 0
    shell_bounds=$(PATH="$d/bin:$PATH" packet_bounds any v:0) \
        || { echo "shell: rejected well-formed out-of-order rows"; return 1; }
    [[ $shell_bounds == "0.500000 3.000000" ]] \
        || { echo "shell: expected '0.500000 3.000000', got '$shell_bounds'"; return 1; }

    local rows
    for rows in rows_na rows_nan rows_inf rows_extra; do
        make_stub "$d/$rows" 0
        PATH="$d/bin:$PATH" packet_bounds any v:0 >/dev/null 2>&1 \
            && { echo "shell: $rows was accepted"; return 1; }
    done

    # --- Python helper ---
    PATH="$d/bin:$PATH" "$py" - "$ROOT" "$d" <<'PYEOF' || return 1
import importlib.util, os, subprocess, sys
root, d = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("pb", root + "/video_upscale_pipeline.py")
m = importlib.util.module_from_spec(spec); sys.modules["pb"] = m; spec.loader.exec_module(m)

def stub(rows, code=0):
    with open(d + "/bin/ffprobe", "w") as fh:
        fh.write("#!/usr/bin/env bash\ncat %r\nexit %d\n" % (rows, code))
    subprocess.run(["chmod", "+x", d + "/bin/ffprobe"], check=True)

stub(d + "/rows_overlap")
assert m.probe_stream_bounds("any", "v:0") == (0.5, 3.0), m.probe_stream_bounds("any", "v:0")

for rows in ("rows_na", "rows_nan", "rows_inf", "rows_extra"):
    stub(d + "/" + rows)
    assert m.probe_stream_bounds("any", "v:0") is None, rows + " was accepted"

stub(d + "/rows_rc", code=1)
assert m.probe_stream_bounds("any", "v:0") is None, "a failed probe was accepted"
PYEOF
}

t_av_sync_gate_notices_a_constant_offset() {
    # A constant audio offset preserves its length. Check the stream start times
    # as well as durations so such a shift cannot pass the synchronization gate.
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    command -v ffmpeg >/dev/null 2>&1 || { echo "no ffmpeg"; return 77; }
    local d="$TMPROOT/av-offset"; mkdir -p "$d"
    ffmpeg -v error -y -f lavfi -i "testsrc2=size=64x48:rate=60000/1001" \
        -f lavfi -i "sine=frequency=440:sample_rate=48000" -t 2 \
        -c:v libx264 -preset ultrafast -crf 40 -pix_fmt yuv420p \
        -c:a aac -b:a 192k -shortest "$d/ok.mp4" 2>/dev/null || return 1
    ffmpeg -v error -y -i "$d/ok.mp4" -itsoffset 0.5 -i "$d/ok.mp4" \
        -map 0:v -map 1:a -c copy "$d/shifted.mp4" 2>/dev/null || return 1

    "$py" - "$ROOT" "$d" <<'PYEOF' || return 1
import importlib.util, sys, io, contextlib
root, d = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("av", root + "/video_upscale_pipeline.py")
m = importlib.util.module_from_spec(spec); sys.modules["av"] = m; spec.loader.exec_module(m)

def check(path, force=False):
    fps = m.probe_video_timing(path)["fps"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.verify_final_av_sync(path, fps, force=force,
                               encoder_delay=m.AAC_ENCODER_DELAY_SAMPLES)
    return buf.getvalue()

out = check(d + "/ok.mp4")     # aligned: AAC priming must not trip it
assert "Alignment OK" in out, out

try:
    check(d + "/shifted.mp4")
except RuntimeError as exc:
    assert "start together" in str(exc), str(exc)
else:
    raise AssertionError("audio shifted 500ms was accepted as aligned")

# --force reports and continues, but must not then claim success -- and must
# still say what was wrong. Asserting only the absence of "Alignment OK" would
# also pass if the warning were deleted entirely.
out = check(d + "/shifted.mp4", force=True)
assert "Alignment OK" not in out, out
assert "WARNING" in out, out
assert "do not start together" in out, out

# An unmeasurable position is a failure, not a note. Warning and continuing
# left the length check alone to decide -- the offset-blind behaviour this
# check exists to remove -- and the run still ended by printing success.
real_bounds = m.probe_stream_bounds
m.probe_stream_bounds = lambda f, s: None
try:
    try:
        check(d + "/ok.mp4")
    except RuntimeError as exc:
        assert "Could not measure" in str(exc), str(exc)
    else:
        raise AssertionError("an unmeasurable stream position was accepted")
    out = check(d + "/ok.mp4", force=True)
    assert "Alignment OK" not in out, out
    assert "WARNING" in out, out
    assert "Could not measure" in out, out
finally:
    m.probe_stream_bounds = real_bounds
PYEOF
}

t_assembled_day_keeps_its_audio() {
    # The components are deleted the moment assembly returns, so audio that did
    # not survive the append is unrecoverable. Every other post-assembly check
    # looks only at the picture.
    command -v MP4Box >/dev/null || { echo "MP4Box not installed"; return 77; }
    local d="$TMPROOT/day-audio" out
    mkdir -p "$d/out"
    extract_assembly "$d" || { echo "could not extract assemble_day_chapters"; return 1; }
    local -a clips=()
    local i
    for i in 0 1 2; do
        make_clip "$d/c$i.mp4" 0.217 || return 1
        clips+=("$d/c$i.mp4")
    done
    ( source "$d/assembly.sh"; assemble_day_chapters day "$d/out" "${clips[@]}" -- "${clips[@]}" ) >"$d/log" 2>&1 \
        || { echo "assembly failed: $(tail -3 "$d/log")"; return 1; }
    out="$d/out/day.mp4"
    grep -Fq 'Audio OK: one AAC track' "$d/log" \
        || { echo "assembly did not verify the audio"; tail -3 "$d/log"; return 1; }
    # These clips are 32kHz, where an AAC block is 32ms rather than 21.3ms.
    # The tolerance must be derived from the file's own rate: a hardcoded
    # 48000 left only 6ms of margin over the priming delay at this rate.
    grep -Fq 'at 32000Hz' "$d/log" \
        || { echo "the audio sample rate was not read from the file"; tail -3 "$d/log"; return 1; }
    grep -Eq 'tolerance 0\.0486' "$d/log" \
        || { echo "tolerance was not derived from the 32kHz block duration"; tail -3 "$d/log"; return 1; }
    local codecs
    codecs=$(ffprobe -v error -select_streams a -show_entries stream=codec_name \
        -of csv=p=0 "$out" 2>/dev/null)
    [[ $codecs == "aac" ]] || { echo "expected one aac track, found '${codecs:-none}'"; return 1; }

    # A video-only component must be refused, not silently delivered.
    ffmpeg -v error -y -i "$d/c0.mp4" -an -c:v copy "$d/mute.mp4" 2>/dev/null || return 1
    rm -rf "$d/out2"; mkdir -p "$d/out2"
    ( source "$d/assembly.sh"; assemble_day_chapters day "$d/out2" "$d/c0.mp4" "$d/mute.mp4" -- "$d/c0.mp4" "$d/mute.mp4" ) \
        >"$d/log2" 2>&1 && { echo "a component with no audio was accepted"; return 1; }
    [[ ! -f "$d/out2/day.mp4" ]] || { echo "a rejected assembly left its output behind"; return 1; }

    # Assembly discards component audio and builds the day's track from sources.
    # Shifted component audio must not affect the delivered track.
    local -a shifted=()
    local c
    for c in "${clips[@]}"; do
        ffmpeg -v error -y -i "$c" -itsoffset 0.5 -i "$c" \
            -map 0:v -map 1:a -c copy "${c%.mp4}_shift.mp4" 2>/dev/null || return 1
        shifted+=("${c%.mp4}_shift.mp4")
    done
    rm -rf "$d/out3"; mkdir -p "$d/out3"
    ( source "$d/assembly.sh"; assemble_day_chapters day "$d/out3" "${shifted[@]}" -- "${clips[@]}" ) \
        >"$d/log3" 2>&1 \
        || { echo "shifted component audio was not ignored: $(tail -3 "$d/log3")"; return 1; }
    local a_start
    a_start=$(ffprobe -v error -select_streams a:0 -show_entries stream=start_time \
        -of csv=p=0 "$d/out3/day.mp4" 2>/dev/null)
    awk -v s="${a_start:-9}" 'BEGIN { exit (s < 0.05) ? 0 : 1 }' \
        || { echo "the component shift reached the delivered audio (start ${a_start}s)"; return 1; }
}

# A source whose PCM audio is a little shorter or longer than its clip's
# pictures. Real DV captures interleave audio with every frame, so the two agree
# to a fraction of a millisecond -- but assembly must not depend on that.
make_av_source() {  # make_av_source <path> <video_seconds> <audio_seconds>
    ffmpeg -v error -f lavfi -i "testsrc=size=64x48:rate=30000/1001:duration=$2" \
        -f lavfi -i "sine=frequency=440:sample_rate=32000:duration=$3" \
        -c:v libx264 -preset ultrafast -crf 40 -pix_fmt yuv420p \
        -c:a pcm_s16le -y "$1" 2>/dev/null
}

t_source_audio_is_held_to_its_own_pictures() {
    # Each clip's audio is padded or trimmed to that clip's picture length, so a
    # sub-frame error stays with its own clip. Laying clips end to end instead
    # lets the error accumulate: five clips 10ms short would put the day's audio
    # 50ms adrift by the end, and every later chapter with it.
    command -v MP4Box >/dev/null || { echo "MP4Box not installed"; return 77; }
    command -v mkvmerge >/dev/null || { echo "mkvmerge not installed"; return 77; }
    local d=$TMPROOT/audio-policy; rm -rf "$d"; mkdir -p "$d/out"
    extract_assembly "$d" || { echo "could not extract assemble_day_chapters"; return 1; }
    local -a clips=() srcs=(); local i
    for i in 0 1 2 3 4; do
        make_clip "$d/c$i.mp4" 0.5 || return 1
        make_av_source "$d/s$i.mkv" 0.5 0.49 || return 1
        clips+=("$d/c$i.mp4"); srcs+=("$d/s$i.mkv")
    done
    ( source "$d/assembly.sh"; assemble_day_chapters day "$d/out" "${clips[@]}" -- "${srcs[@]}" ) \
        >"$d/log" 2>&1 || { echo "assembly rejected a within-tolerance source: $(tail -4 "$d/log")"; return 1; }
    grep -Fq 'held to their pictures' "$d/log" \
        || { echo "the audio adjustment was not reported"; tail -3 "$d/log"; return 1; }
    local vdur adur delta
    vdur=$(ffprobe -v error -select_streams v:0 -show_entries stream=duration -of csv=p=0 "$d/out/day.mp4")
    adur=$(ffprobe -v error -select_streams a:0 -show_entries stream=duration -of csv=p=0 "$d/out/day.mp4")
    delta=$(awk -v a="$adur" -v v="$vdur" 'BEGIN { x = a - v; if (x < 0) x = -x; printf "%.4f", x }')
    awk -v x="$delta" 'BEGIN { exit (x < 0.034) ? 0 : 1 }' \
        || { echo "day audio ${adur}s vs pictures ${vdur}s -- delta ${delta}s suggests accumulation"; return 1; }
}

t_source_audio_beyond_one_frame_is_refused() {
    # Past a frame the capture's audio and picture genuinely disagree, and
    # guessing which is right would silently reshape the day.
    command -v MP4Box >/dev/null || { echo "MP4Box not installed"; return 77; }
    command -v mkvmerge >/dev/null || { echo "mkvmerge not installed"; return 77; }
    local d=$TMPROOT/audio-policy-reject; rm -rf "$d"; mkdir -p "$d/out"
    extract_assembly "$d" || { echo "could not extract assemble_day_chapters"; return 1; }
    make_clip "$d/c0.mp4" 0.5 || return 1
    make_av_source "$d/s0.mkv" 0.5 0.35 || return 1
    ( source "$d/assembly.sh"; assemble_day_chapters day "$d/out" "$d/c0.mp4" -- "$d/s0.mkv" ) \
        >"$d/log" 2>&1 && { echo "a source short by 150ms was accepted"; return 1; }
    grep -Fq 'source frame' "$d/log" \
        || { echo "rejected, but not for the audio/picture length policy"; tail -4 "$d/log"; return 1; }
    [[ ! -f "$d/out/day.mp4" ]] || { echo "a rejected assembly left its output behind"; return 1; }
}

t_run_id_shape() {
    local id
    id=$(run_make_id digital8-batch)
    [[ $id =~ ^digital8-batch_[0-9]{8}-[0-9]{6}$ ]] \
        || { echo "unexpected run id: $id"; return 1; }

    # The label comes from a source filename, so it must survive characters
    # that would otherwise make the id ambiguous as a path component.
    id=$(run_make_id 'dv 2007/11/24 *odd*')
    [[ $id != */* ]] || { echo "a source name put a separator in the run id: $id"; return 1; }
    [[ $id =~ ^[A-Za-z0-9._-]+_[0-9]{8}-[0-9]{6}$ ]] \
        || { echo "run id is not a clean path component: $id"; return 1; }

    # The same source's runs sort oldest first, and different sources group.
    local a b
    a=x_$(TZ=UTC date -d @1000000000 +%Y%m%d-%H%M%S)
    b=x_$(TZ=UTC date -d @2000000000 +%Y%m%d-%H%M%S)
    [[ $(printf '%s\n%s\n' "$b" "$a" | sort | head -1) == "$a" ]] \
        || { echo "run ids do not sort chronologically"; return 1; }
}

t_run_layout_contract() {
    # The documented layout, asserted against the functions every tool uses to
    # build it. A path that moves without the document moving fails here.
    local r=$TMPROOT/layout/ai/20260101-000000_x
    [[ $(run_videos_dir "$r") == "$r/videos" ]] || { echo "videos/ moved"; return 1; }
    [[ $(run_logs_dir   "$r") == "$r/logs" ]]   || { echo "logs/ moved"; return 1; }
    [[ $(run_state_dir  "$r") == "$r/state" ]]  || { echo "state/ moved"; return 1; }
    [[ $(run_work_dir   "$r") == "$r/work" ]]   || { echo "work/ moved"; return 1; }
    [[ $(run_manifest   "$r") == "$r/run.json" ]] || { echo "run.json moved"; return 1; }

    # Everything recorded about one capture is named for that capture, not for
    # the intermediate the pipeline was actually handed.
    [[ $(run_source_log_dir "$r" /src/day/dv.20071124_103447.avi) \
        == "$r/logs/dv.20071124_103447" ]] \
        || { echo "a capture's logs are not named for the capture"; return 1; }

    [[ $(run_root /tmp/pres) == /tmp/pres/ai ]] || { echo "the run root moved"; return 1; }
}

t_manifest_is_atomic() {
    local d=$TMPROOT/atomic; mkdir -p "$d/ai"
    local run
    run=$(mkrun "$d/ai" batch digital8 '{"scale":"2"}') || { echo "could not build a run"; return 1; }

    jq -e . "$run/run.json" >/dev/null 2>&1 || { echo "manifest is not valid JSON"; return 1; }
    [[ $(run_manifest_get "$run" .class) == digital8 ]] || { echo "class not recorded"; return 1; }
    [[ $(run_manifest_get "$run" .finished) == null ]] || { echo "a new run is already finished"; return 1; }

    run_manifest_finish "$run" || { echo "could not finish the run"; return 1; }
    [[ $(run_manifest_get "$run" .finished) != null ]] || { echo "finished was not recorded"; return 1; }

    # Every write goes through a temporary file and a rename, so a run
    # directory never contains a half-written manifest -- one that no longer
    # parses would make the run unadoptable and its settings unreadable.
    local leftovers
    leftovers=$(find "$run" -maxdepth 1 -name 'run.json.*' | wc -l)
    (( leftovers == 0 )) || { echo "a manifest write left $leftovers temporary file(s)"; return 1; }
}

t_adoption_continues_the_same_run() {
    local d=$TMPROOT/adopt; mkdir -p "$d/ai"
    local cfg='{"scale":"2","crf":"23"}'
    local older newer found
    older=$(mkrun "$d/ai" batch digital8 "$cfg") || return 1
    sleep 1
    newer=$(mkrun "$d/ai" batch digital8 "$cfg") || return 1

    found=$(run_find_adoptable "$d/ai" digital8 "$cfg") \
        || { echo "no run was adopted; a resume would have started a second one"; return 1; }
    [[ $found == "$newer" ]] \
        || { echo "adopted $found, expected the newest unfinished run $newer"; return 1; }
    [[ -n $older ]]
}

t_adoption_refuses_wrong_runs() {
    local d=$TMPROOT/refuse; mkdir -p "$d/ai"
    local cfg='{"scale":"2","crf":"23"}'

    # A finished run is not adoptable: its videos are complete and appending to
    # it would add days it never claimed to contain.
    mkrun "$d/ai" "done" digital8 "$cfg" 2026-01-01T00:00:00Z >/dev/null || return 1
    run_find_adoptable "$d/ai" digital8 "$cfg" >/dev/null \
        && { echo "a finished run was adopted"; return 1; }

    # Neither is an unfinished run upscaled at different settings: its videos
    # were never comparable with what this invocation would produce.
    mkrun "$d/ai" other digital8 '{"scale":"4","mode":"distribution"}' >/dev/null || return 1
    run_find_adoptable "$d/ai" digital8 "$cfg" >/dev/null \
        && { echo "a run with different settings was adopted"; return 1; }

    # Nor a run of a different media class sharing the same root.
    mkrun "$d/ai" hi8 hi8-dv "$cfg" >/dev/null || return 1
    run_find_adoptable "$d/ai" digital8 "$cfg" >/dev/null \
        && { echo "a run of another media class was adopted"; return 1; }
    return 0
}

extract_validator() {  # extract_validator <dir>
    local d=$1
    {
        printf 'log() { :; }\n'
        # The validator uses the shared frame/seconds vocabulary, so the
        # extracted copy needs it too. Sourced rather than duplicated: a second
        # definition here could drift from the real one and the test would go
        # on passing against a tolerance the tools do not use.
        printf 'source %q\n' "$ROOT/pipeline_tools/run_lib.sh"
        sed -n '/^is_seconds() {/,/^}/p;/^is_seconds() { /p' "$ROOT/upscale_dv_batch.sh" | head -1
        sed -n '/^validate_source_output_duration() {/,/^}/p' "$ROOT/upscale_dv_batch.sh"
    } >"$d/validator.sh"
    grep -q 'validate_source_output_duration' "$d/validator.sh"
}

validator_run() {  # validator_run <dir> <source-duration> <enhanced-duration>
    local d=$1 src=$2 enh=$3
    mkdir -p "$d/bin"
    cat >"$d/bin/ffprobe" <<'STUB'
#!/usr/bin/env bash
target=${@: -1}
case $target in
    *source*) printf '%s\n' "$FAKE_SRC" ;;
    *)        printf '%s\n' "$FAKE_ENH" ;;
esac
STUB
    chmod +x "$d/bin/ffprobe"
    : >"$d/source.mkv"; : >"$d/enhanced.mkv"
    PATH="$d/bin:$PATH" FAKE_SRC="$src" FAKE_ENH="$enh" \
        timeout 60 bash -c 'source "$1"; validate_source_output_duration "$2" "$3"' \
        _ "$d/validator.sh" "$d/source.mkv" "$d/enhanced.mkv" 2>&1
}

t_duration_validator_rejects_unreadable() {
    local d=$TMPROOT/duration; mkdir -p "$d"
    extract_validator "$d" || { echo "could not extract the duration validator"; return 1; }
    local out rc

    out=$(validator_run "$d" 10.000 10.000); rc=$?
    (( rc == 0 )) || { echo "matching durations were rejected: $out"; return 1; }

    out=$(validator_run "$d" 10.000 9.000); rc=$?
    (( rc != 0 )) || { echo "a one-second shortfall was accepted"; return 1; }

    # Unreadable durations must fail validation instead of becoming numeric zero.
    out=$(validator_run "$d" 'N/A' 'N/A'); rc=$?
    (( rc != 0 )) || { echo "two unreadable durations were accepted as a match"; return 1; }
    grep -q 'could not compare' <<<"$out" \
        || { echo "an unreadable duration was not reported as uncomparable: $out"; return 1; }

    out=$(validator_run "$d" '' 10.000); rc=$?
    (( rc != 0 )) || { echo "an empty source duration was accepted"; return 1; }
}

printf 'AI pipeline run directories\n\n'
# The duration gates decide whether hours of GPU work start, so they are
# exercised against a real file rather than inspected. A type error in one of
# them is invisible to interface tests and surfaces only after the master has
# been built -- which is exactly how it surfaced once: get_video_fps() returns
# a formatted string, and a caller divided by it.
t_duration_gates_run_and_are_frame_based() {
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    command -v ffmpeg >/dev/null 2>&1 || { echo "no ffmpeg"; return 77; }
    local d="$TMPROOT/gates"; mkdir -p "$d"
    ffmpeg -y -v error -f lavfi -i testsrc2=size=64x48:rate=30 \
        -f lavfi -i sine=frequency=440:sample_rate=48000 -t 2 \
        -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 192k "$d/clip.mp4" || return 1
    ffmpeg -y -v error -i "$d/clip.mp4" -vn -c:a copy "$d/audio.m4a" || return 1
    ffmpeg -y -v error -i "$d/clip.mp4" -vn -t 1 -c:a copy "$d/short.m4a" || return 1

    "$py" - "$ROOT" "$d" <<'PYEOF' || return 1
import importlib.util, sys, io, contextlib
root, d = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("vup", root + "/video_upscale_pipeline.py")
m = importlib.util.module_from_spec(spec); sys.modules["vup"] = m; spec.loader.exec_module(m)

clip, audio, short = d + "/clip.mp4", d + "/audio.m4a", d + "/short.m4a"

# Runs at all, and reports the derived limits rather than a flat constant.
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.verify_audio_video_duration(clip, audio)
line = buf.getvalue()
assert "abort beyond" in line, line
abort_ms = float(line.split("abort beyond")[1].split("ms")[0])
assert abort_ms < 100, f"abort limit {abort_ms}ms is not frame-derived: {line}"

# A truncated extraction is refused.
try:
    with contextlib.redirect_stdout(io.StringIO()):
        m.verify_audio_video_duration(clip, short)
except RuntimeError:
    pass
else:
    raise AssertionError("a 1s-short extraction was accepted")

# reconcile_durations: exact passes, two frames of loss does not, and the
# picture side is not given seconds of slack.
t = m.probe_video_timing(clip)
src, fps, frame = t["duration"], t["fps"], 1.0 / t["fps"]
with contextlib.redirect_stdout(io.StringIO()) as ok_out:
    ok = m.reconcile_durations(src, src, "chunks", src, fps=fps, audio_file=audio)
assert ok, ok_out.getvalue()
with contextlib.redirect_stdout(io.StringIO()):
    bad = m.reconcile_durations(src, src - 2 * frame, "chunks", src,
                                fps=fps, audio_file=audio)
assert not bad, "two frames of missing picture was accepted"
print("abort %.1fms, frame %.1fms" % (abort_ms, frame * 1000))
PYEOF
}

t_current_run_pointer_is_safe_and_precise() {
    local root="$TMPROOT/current/ai" first second
    mkdir -p "$root"
    first=$(mkrun "$root" first hi8-dv '{}') || return 1
    sleep 1
    second=$(mkrun "$root" second hi8-dv '{}') || return 1

    run_set_current "$root" "$first" || return 1
    [[ $(run_get_current "$root") == "$first" ]] \
        || { echo "current_run did not resolve to the selected run"; return 1; }

    # Finishing an older run must not erase a pointer another invocation has
    # moved to a newer run.
    run_set_current "$root" "$second" || return 1
    run_clear_current "$root" "$first" || return 1
    [[ $(run_get_current "$root") == "$second" ]] \
        || { echo "finishing the old run erased the newer pointer"; return 1; }
    run_clear_current "$root" "$second" || return 1
    [[ ! -e $root/current_run && ! -L $root/current_run ]] \
        || { echo "the completed current run pointer remains"; return 1; }

    # A real directory at the conventional name could contain user data. It
    # must never be replaced just because the wrapper wants a symlink there.
    mkdir "$root/current_run"
    if run_set_current "$root" "$first" >/dev/null 2>&1; then
        echo "a non-symlink current_run directory was replaced"
        return 1
    fi
    return 0
}

t_latest_unfinished_hi8_run_can_be_selected() {
    local root="$TMPROOT/current-adoption/ai" older newer wrong found
    mkdir -p "$root"
    older=$(mkrun "$root" older hi8-dv '{}') || return 1
    sleep 1
    wrong=$(mkrun "$root" digital8 digital8 '{}') || return 1
    sleep 1
    newer=$(mkrun "$root" newer hi8-dv '{}') || return 1
    run_manifest_finish "$newer" || return 1

    found=$(run_find_latest_unfinished "$root" hi8-dv) || return 1
    [[ $found == "$older" ]] \
        || { echo "unfinished-run selection chose ${found:-nothing}, expected $older"; return 1; }
    [[ $found != "$wrong" ]] || { echo "unfinished-run selection crossed media classes"; return 1; }
}

t_batch_state_requires_canonical_day_keys() {
    command -v jq >/dev/null || { echo "no jq"; return 77; }
    local d="$TMPROOT/day-state-validation" name contents expected out rc
    mkdir -p "$d"
    awk '/^state_validate_day_keys\(\)/ { printing=1 }
         printing { print }
         printing && /^}/ { exit }' "$ROOT/upscale_dv_batch.sh" >"$d/validate.sh"
    grep -q '^state_validate_day_keys()' "$d/validate.sh" || return 1
    while IFS='|' read -r name expected contents; do
        printf '%s\n' "$contents" >"$d/state.json"
        cp -- "$d/state.json" "$d/before.json"
        out=$(STATE_FILE="$d/state.json" bash -c \
            'source "$1"; state_validate_day_keys' bash "$d/validate.sh" 2>&1); rc=$?
        [[ $rc == "$expected" ]] || { echo "$name returned $rc: $out"; return 1; }
        cmp -s "$d/state.json" "$d/before.json" \
            || { echo "$name validation changed the ledger"; return 1; }
        if (( rc != 0 )); then
            [[ $out == *'day keys exactly YYYYMMDD'* ]] \
                || { echo "$name rejection was not explained: $out"; return 1; }
        fi
    done <<'CASES'
empty|0|{"files":{},"days":{}}
canonical|0|{"files":{},"days":{"20200102":{"stage":"assembled"}}}
suffix|1|{"files":{},"days":{"20200102_Trip":{"stage":"assembled"}}}
duplicate-date|1|{"files":{},"days":{"20200102":{},"20200102_Trip":{}}}
invalid-key|1|{"files":{},"days":{"not-a-date":{}}}
days-array|1|{"files":{},"days":[]}
files-array|1|{"files":[],"days":{}}
invalid-json|1|not JSON
CASES
    [[ -z $(find "$d" -name '*.pre-day-id-*' -print -quit) ]] \
        || { echo "validation created a migration backup"; return 1; }
}

t_batch_entry_point_starts_and_records_its_config() {
    # The batch's own startup, not a helper called in isolation. A script can
    # fail before it reaches any function a helper-level test exercises -- a
    # malformed jq expression in the configuration signature exits 3 having
    # done nothing at all, with every helper still passing. Only running the
    # entry point catches that.
    command -v jq >/dev/null 2>&1 || { echo "no jq"; return 77; }
    local d="$TMPROOT/batch-entry" out config
    mkdir -p "$d/src" "$d/pres"
    out=$(TMPDIR="$d/pres" VIDEO_PRESERVATION_ROOT="$d/pres" \
        timeout 120 bash "$ROOT/upscale_dv_batch.sh" \
        --input-root "$d/src" --dry-run 2>&1) || {
        echo "batch entry point failed to start:"; tail -5 <<<"$out"; return 1; }
    config=$(find "$d/pres" -name run.json -print -quit)
    [[ -n $config ]] || { echo "no run.json was created"; return 1; }
    jq -e '.settings
           | has("crf") and has("scale") and has("model") and has("input_root")
             and (has("mode") | not)' "$config" >/dev/null \
        || { echo "not the current settings schema: $(jq -c .settings "$config")"; return 1; }
    jq -e --arg input "$d/src" \
        '.plan_only == true and .finished != null and
         (.command | index("--input-root")) != null and
         (.command | index($input)) != null' "$config" >/dev/null \
        || { echo "plan manifest did not preserve the invocation"; return 1; }
    [[ -f $(dirname "$config")/day-plan.tsv \
       && ! -e $(dirname "$config")/state ]] \
        || { echo "empty dry run did not leave only completed plan artifacts"; return 1; }

    local rejected_status
    out=$(bash "$ROOT/upscale_dv_batch.sh" --input-root "$d/src" --dry-run \
        --run-dir "$d/not-a-run" 2>&1)
    rejected_status=$?
    [[ $rejected_status == 2 && $out == *'plan is written under /tmp'* ]] \
        || { echo "dry run accepted a resumable run directory: rc=$rejected_status $out"; return 1; }

    # And again with a terminal attached, which is how a person runs it.
    #
    # The run above passes with stdin on a pipe, where `stty` in the EXIT trap
    # fails harmlessly. Give the script a controlling terminal and that same
    # call raises SIGTTOU from a background process group -- default action
    # STOP -- so the batch completed its work, printed everything, and then
    # hung until timeout killed it: a correct run reported as a failure to
    # start. Nothing without a pty can see it.
    command -v script >/dev/null 2>&1 || return 0
    local pd="$d/pty"
    mkdir -p "$pd/src" "$pd/pres"
    local status
    status=$(script -qec "TMPDIR=$pd/pres VIDEO_PRESERVATION_ROOT=$pd/pres timeout 60 bash \
        $ROOT/upscale_dv_batch.sh --input-root $pd/src --dry-run \
        >$pd/out 2>&1; echo \$?" /dev/null | tr -d '\r\n')
    [[ $status == 0 ]] \
        || { echo "with a terminal attached the batch exited $status (124 = hung until killed)"
             tail -3 "$pd/out"; return 1; }
}

t_batch_groups_arbitrary_layout_by_dv_recording_day() {
    # The opaque pair proves that grouping does not require dated names or day
    # directories. The dated pair proves the saved plan explicitly distinguishes
    # an exact filename/subcode match from a minute-only filename match.
    command -v jq >/dev/null 2>&1 || { echo "no jq"; return 77; }
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    local d="$TMPROOT/batch-grouping" src="$TMPROOT/batch-grouping/source tree"
    local preservation="$TMPROOT/batch-grouping/preservation" out manifest
    mkdir -p "$src/unrelated/deep folder" "$preservation"

    "$py" - "$ROOT" "$src/dv.clip-a.avi" \
        "$src/unrelated/deep folder/dv.clip-b.avi" \
        "$src/unrelated/dv.20040423-173419.avi" \
        "$src/unrelated/deep folder/dv.20040424-0805.avi" <<'PYEOF' || return 1
import datetime
import struct
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from pipeline_tools import dv_recording_time as dvmeta

def bcd(value):
    return ((value // 10) << 4) | value % 10

def make_avi(path, value):
    frame = bytearray(dvmeta.FRAME_BYTES_525_60)
    frame[:4] = b"\x1f\x07\x00\x3f"
    positions = {name: [] for name in dvmeta.SOURCES}
    for name, offset in dvmeta._pack_positions(0, dvmeta.SEQUENCES_525_60):
        positions[name].append(offset)
    for name in dvmeta.SOURCES:
        date_id, time_id = dvmeta.SOURCE_PACK_IDS[name]
        date_pack = bytes([
            date_id, 0xff, bcd(value.day), bcd(value.month), bcd(value.year % 100)
        ])
        time_pack = bytes([
            time_id, 0xff, bcd(value.second), bcd(value.minute), bcd(value.hour)
        ])
        first, second = positions[name][:2]
        frame[first:first + 5] = date_pack
        frame[second:second + 5] = time_pack
    # dv_recording_time.py reads a DV AVI's first 00dc chunk directly. The
    # picture payload need not decode because this dry run stops before media
    # processing; it is complete DV metadata evidence for discovery/preflight.
    header = (
        b"RIFF" + struct.pack("<I", len(frame) + 16) + b"AVI " +
        b"movi" + b"00dc" + struct.pack("<I", len(frame))
    )
    Path(path).write_bytes(header + frame)

make_avi(sys.argv[2], datetime.datetime(2004, 4, 23, 17, 34, 19))
make_avi(sys.argv[3], datetime.datetime(2004, 4, 24, 8, 5, 2))
make_avi(sys.argv[4], datetime.datetime(2004, 4, 23, 17, 34, 19))
make_avi(sys.argv[5], datetime.datetime(2004, 4, 24, 8, 5, 2))
PYEOF

    out=$(TMPDIR="$preservation" VIDEO_PRESERVATION_ROOT="$preservation" timeout 120 \
        bash "$ROOT/upscale_dv_batch.sh" --input-root "$src" \
        --time-source subcode --on-anomaly prefer-subcode --dry-run 2>&1) || {
        echo "arbitrary-layout dry run failed:"; tail -12 <<<"$out"; return 1; }

    grep -Fq 'Discovered 4 unique DV capture(s) recursively.' <<<"$out" \
        || { echo "recursive discovery did not find all captures"; return 1; }
    grep -Fq 'Days to process: 2' <<<"$out" \
        || { echo "captures were not divided into two days: $out"; return 1; }
    grep -Fq '20040423 (2 capture(s))' <<<"$out" \
        && grep -Fq '20040424 (2 capture(s))' <<<"$out" \
        || { echo "resolved recording days were not processed: $out"; return 1; }
    grep -Fq 'Resolved day plan (time source: subcode):' <<<"$out" \
        || { echo "dry-run summary did not label the day plan: $out"; return 1; }
    grep -Fq '1  exact matches' <<<"$out" \
        && grep -Fq '1  matches to minute-only filename precision' <<<"$out" \
        && grep -Fq '2  filenames with no timestamp' <<<"$out" \
        || { echo "dry-run summary did not classify timestamp evidence: $out"; return 1; }

    manifest=$(find "$preservation" -name run.json -print -quit)
    [[ -n $manifest ]] || { echo "no run manifest"; return 1; }
    jq -e '.sources | length == 4' "$manifest" >/dev/null \
        || { echo "manifest did not record all canonical sources"; return 1; }
    jq -e '[.day_plan[] | select(.status == "include")]
           | length == 4 and
             any(.[]; .source | endswith("dv.clip-a.avi")) and
             any(.[]; .source | endswith("dv.clip-b.avi")) and
             all(.[]; .basis == "subcode" and
                      .dv_sources == "AAUX,SSYB,VAUX") and
             (map(select(.capture == "dv.clip-a.avi" or
                         .capture == "dv.clip-b.avi")
                  | .filename_vs_subcode) | all(. == "filename_missing")) and
             any(.[]; .capture == "dv.20040423-173419.avi" and
                      .filename_vs_subcode == "match_exact") and
             any(.[]; .capture == "dv.20040424-0805.avi" and
                      .filename_vs_subcode == "match_filename_minute") and
             (map(.recording_time) == (map(.recording_time) | sort))' \
        "$manifest" >/dev/null \
        || { echo "machine-readable day plan is incomplete or lacks DV evidence"; return 1; }
    [[ -f $(dirname "$manifest")/day-plan.tsv \
       && ! -e $(dirname "$manifest")/state ]] \
        || { echo "dry run did not keep only plan artifacts"; return 1; }
    local plan_tree
    plan_tree=$(dirname "$manifest")/day-tree.txt
    [[ -f $plan_tree ]] || { echo "no day-tree.txt beside the plan"; return 1; }
    diff -u - "$plan_tree" <<'TREE_EOF' \
        || { echo "the day tree is not the grouping the tape describes"; return 1; }
20040423 (2 capture(s))
├── dv.clip-a.avi
└── dv.20040423-173419.avi
20040424 (2 capture(s))
├── dv.20040424-0805.avi
└── dv.clip-b.avi
TREE_EOF
    diff -u <(sed 's/^\[[^]]*\] //' <<<"$out" | sed -n '/^20040423 (/,/^$/p' \
             | sed '/^$/d') "$plan_tree" \
        || { echo "the logged tree and day-tree.txt disagree"; return 1; }
    grep -Fq "Dry-run results were written to: $(dirname "$manifest")" <<<"$out" \
        || { echo "dry run did not identify its result directory"; return 1; }

    # --day is a resolved-date filter now. It must work when no directory or
    # source filename contains that date, and the manifest must describe only
    # the capture the filtered run would consume.
    local filtered_preservation="$d/filtered-preservation" filtered_manifest
    mkdir -p "$filtered_preservation"
    out=$(TMPDIR="$filtered_preservation" \
        VIDEO_PRESERVATION_ROOT="$filtered_preservation" timeout 120 \
        bash "$ROOT/upscale_dv_batch.sh" --input-root "$src" \
        --day 20040424 --time-source subcode \
        --on-anomaly prefer-subcode --dry-run 2>&1) || {
        echo "resolved --day dry run failed:"; tail -12 <<<"$out"; return 1; }
    grep -Fq 'Days to process: 1' <<<"$out" \
        && grep -Fq '20040424 (2 capture(s))' <<<"$out" \
        && ! grep -Fq '20040423 (' <<<"$out" \
        || { echo "--day did not filter resolved recording dates: $out"; return 1; }
    filtered_manifest=$(find "$filtered_preservation" -name run.json -print -quit)
    jq -e '.sources | length == 2 and
           any(.[]; endswith("dv.clip-b.avi")) and
           any(.[]; endswith("dv.20040424-0805.avi"))' \
        "$filtered_manifest" >/dev/null \
        || { echo "filtered manifest recorded the wrong source set"; return 1; }
}

t_subcode_plan_exposes_bad_camcorder_clock() {
    # An unset DV clock encodes the generated 1970-01-01 default and must not
    # fall back to a plausible filename in strict subcode mode. A readable
    # clock on a different day is visible too, and --on-anomaly prefer-subcode puts
    # it in the tape's day rather than the capture filename's day.
    command -v jq >/dev/null 2>&1 || { echo "no jq"; return 77; }
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    local d="$TMPROOT/subcode-clock" src="$TMPROOT/subcode-clock/sources"
    local preservation="$TMPROOT/subcode-clock/preservation" out manifest
    mkdir -p "$src" "$preservation"

    "$py" - "$ROOT" "$src/dv.20040425-120000-unset.avi" \
        "$src/dv.20040426-120000-wrong.avi" <<'PYEOF' || return 1
import datetime
import struct
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from pipeline_tools import dv_recording_time as dvmeta

def bcd(value):
    return ((value // 10) << 4) | value % 10

def make_avi(path, value):
    frame = bytearray(dvmeta.FRAME_BYTES_525_60)
    frame[:4] = b"\x1f\x07\x00\x3f"
    positions = {name: [] for name in dvmeta.SOURCES}
    for name, offset in dvmeta._pack_positions(0, dvmeta.SEQUENCES_525_60):
        positions[name].append(offset)
    for name in dvmeta.SOURCES:
        date_id, time_id = dvmeta.SOURCE_PACK_IDS[name]
        date_pack = bytes([
            date_id, 0xff, bcd(value.day), bcd(value.month), bcd(value.year % 100)
        ])
        time_pack = bytes([
            time_id, 0xff, bcd(value.second), bcd(value.minute), bcd(value.hour)
        ])
        first, second = positions[name][:2]
        frame[first:first + 5] = date_pack
        frame[second:second + 5] = time_pack
    header = (
        b"RIFF" + struct.pack("<I", len(frame) + 16) + b"AVI " +
        b"movi" + b"00dc" + struct.pack("<I", len(frame))
    )
    Path(path).write_bytes(header + frame)

make_avi(sys.argv[2], datetime.datetime(1970, 1, 1, 0, 0, 0))
make_avi(sys.argv[3], datetime.datetime(2004, 4, 23, 9, 8, 7))
PYEOF

    out=$(TMPDIR="$preservation" VIDEO_PRESERVATION_ROOT="$preservation" timeout 120 \
        bash "$ROOT/upscale_dv_batch.sh" --input-root "$src" \
        --time-source subcode --on-anomaly prefer-subcode --dry-run 2>&1) || {
        echo "subcode clock dry run failed:"; tail -20 <<<"$out"; return 1; }

    grep -Fq 'DV subcode timestamps readable: 1 of 2.' <<<"$out" \
        && grep -Fq 'The camcorder clock may have been unset' <<<"$out" \
        && grep -Fq 'No tape time for dv.20040425-120000-unset -- skipping it.' <<<"$out" \
        || { echo "unset clock was not exposed and excluded: $out"; return 1; }
    grep -Fq 'A different calendar day.' <<<"$out" \
        && grep -Fq 'Using tape time for dv.20040426-120000-wrong: 2004-04-23 09:08:07' <<<"$out" \
        && grep -Fq '20040423 (1 capture(s))' <<<"$out" \
        && ! grep -Fq '20040425 (' <<<"$out" \
        && ! grep -Fq '20040426 (' <<<"$out" \
        || { echo "wrong clock did not produce the expected tape-based plan: $out"; return 1; }
    manifest=$(find "$preservation" -name run.json -print -quit)
    jq -e '(.excluded | length) == 1 and
           (.excluded[0].source | endswith("dv.20040425-120000-unset.avi")) and
           any(.day_plan[]; .capture == "dv.20040425-120000-unset.avi" and
                            .filename_vs_subcode == "subcode_missing") and
           any(.day_plan[]; .capture == "dv.20040426-120000-wrong.avi" and
                            .filename_vs_subcode == "mismatch_different_day")' \
        "$manifest" >/dev/null \
        || { echo "manifest did not classify the bad clock evidence"; return 1; }
}

t_daily_audit_uses_the_batch_recorded_grouping() {
    # The audit must use the same durable grouping decision as the batch. The
    # source has neither a dated filename nor a dated parent directory, so the
    # old directory walk cannot manufacture the expected day on its own.
    command -v jq >/dev/null 2>&1 || { echo "no jq"; return 77; }
    local d="$TMPROOT/audit-grouping" source run out
    source="$d/source tree/random/depth/dv.opaque-name.avi"
    run="$d/run"
    mkdir -p "$(dirname -- "$source")" "$run/videos" "$run/state"
    : > "$source"
    jq -n --arg source "$source" \
        '{class:"digital8", sources:[$source]}' > "$run/run.json" || return 1
    printf 'dv.opaque-name\t2004-04-23 17:34:19\n' \
        > "$run/state/recording_times.tsv"

    out=$(bash "$ROOT/pipeline_tools/check_daily_ai_distribution.sh" \
        --input-root "$d/source tree" --run-dir "$run" \
        --day 20040423 2>&1) || {
        echo "run-aware audit failed:"; echo "$out"; return 1; }
    grep -Fq "Source grouping: 1 capture(s) from the run's resolved recording times" \
        <<<"$out" || { echo "audit did not load the run grouping: $out"; return 1; }
    grep -Fq 'SKIP  [20040423] No assembled day video found.' <<<"$out" \
        || { echo "audit did not reconstruct resolved day 20040423: $out"; return 1; }
    [[ $out != *'Cannot parse timestamp from filename'* ]] \
        || { echo "audit fell back to the opaque source filename: $out"; return 1; }
}

t_hi8_short_resume_reconstructs_the_recorded_command() {
    # The wrapper resolves its own interpreter, falling back to python3 on
    # PATH, so this needs one to exist but not a venv in the tree.
    pipeline_python >/dev/null || { echo "no python3"; return 77; }
    local preservation="$TMPROOT/hi8-short/VideoPreservation"
    local root="$preservation/ai" run master out
    mkdir -p "$root"
    run=$(mkrun "$root" hi8-short hi8-dv '{}') || return 1
    master="$run/work/masters/tape_master_from_prepare_video.mp4"
    mkdir -p "$run/state" "$run/work/processing_chunks"
    run_manifest_set "$run" --arg src /archive/tape.dv '.sources = [$src]' || return 1
    jq -n --arg master "$master" \
        '{version:2, source_capture:"/archive/tape.dv", prepared_master:$master,
          pipeline:{profile:"hi8dv",scale:2,model:null,rife:false,
                    crf:23,
                    chunk_duration:300,chunk_threshold:300}}' \
        > "$run/state/enhance_tape.json" || return 1
    printf '{}\n' > "$run/work/processing_chunks/source_measurements.json"
    run_set_current "$root" "$run" || return 1

    out=$(VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" --resume --dry-run 2>&1) || {
        echo "$out"
        return 1
    }
    grep -Fq "$master" <<<"$out" \
        || { echo "recorded master was not reconstructed: $out"; return 1; }
    grep -Fq -- '--profile hi8dv --scale 2' <<<"$out" \
        || { echo "Hi8 recipe was not reconstructed: $out"; return 1; }
    grep -Fq -- '--chunk-duration 300 --chunk-threshold 300' <<<"$out" \
        || { echo "pinned chunk recipe was not reconstructed: $out"; return 1; }
    [[ $out != *"--force"* ]] \
        || { echo "short resume unexpectedly authorized force: $out"; return 1; }
}

# Launching a capture again while its run is unfinished continues that run --
# the master it holds took an hour -- when the recipe is the same; a different
# recipe, or a finished run, gets a new run, and an unfinished one that is
# displaced is named so --resume --run-dir can still reach it.
t_hi8_relaunch_continues_the_current_run() {
    pipeline_python >/dev/null || { echo "no python3"; return 77; }
    local preservation="$TMPROOT/hi8-relaunch/VideoPreservation"
    local root="$preservation/ai" run master source out
    mkdir -p "$root" "$TMPROOT/hi8-relaunch/archive"
    source="$TMPROOT/hi8-relaunch/archive/hi8_tape.dv"; : > "$source"
    source=$(realpath -- "$source")
    run=$(mkrun "$root" hi8-relaunch hi8-dv '{}') || return 1
    master="$run/work/masters/tape_master_from_prepare_video.mp4"
    mkdir -p "$run/state" "$run/work/masters"; : > "$master"
    run_manifest_set "$run" --arg src "$source" '.sources = [$src]' || return 1
    jq -n --arg source "$source" --arg master "$master" \
        '{version:2, source_capture:$source, prepared_master:$master,
          pipeline:{profile:"hi8dv",scale:2,model:null,rife:false,crf:23,
                    chunk_duration:null,chunk_threshold:900}}' \
        > "$run/state/enhance_tape.json" || return 1
    run_set_current "$root" "$run" || return 1

    out=$(VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" --dry-run "$source" 2>&1) || { echo "$out"; return 1; }
    grep -Fq "Continuing the unfinished run of this capture: $run" <<<"$out" \
        || { echo "same job did not continue the current run: $out"; return 1; }
    grep -Fq "Run directory: $run" <<<"$out" || { echo "wrong run directory: $out"; return 1; }
    grep -Fq 'Stage 1: skipped, master present' <<<"$out" \
        || { echo "continued run did not reuse its master: $out"; return 1; }

    # The pipeline releases the master once its split is resume-ready; the
    # continued run then needs no preparation either, as --resume knows.
    rm -f -- "$master"
    mkdir -p "$run/work/processing_chunks"
    printf '{}\n' > "$run/work/processing_chunks/source_measurements.json"
    out=$(VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" --dry-run "$source" 2>&1) || { echo "$out"; return 1; }
    grep -Fq 'Stage 1: skipped, saved split is resume-ready' <<<"$out" \
        || { echo "continued run with a released master planned preparation again: $out"; return 1; }
    : > "$master"

    # A different recipe is a different job: a new run, and the displaced
    # unfinished run is named.
    out=$(VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" --dry-run --pin-chunks "$source" 2>&1) || { echo "$out"; return 1; }
    [[ $out != *"Continuing the unfinished run"* ]] \
        || { echo "a different recipe continued the current run: $out"; return 1; }
    grep -Fq "current_run leaves unfinished run $run" <<<"$out" \
        || { echo "displaced unfinished run was not named: $out"; return 1; }
    grep -Eq "Run directory: $root/hi8_tape_[0-9]{8}-[0-9]{6}\$" <<<"$out" \
        || { echo "no new run directory planned: $out"; return 1; }

    # A finished run is a record, not a job to continue, and nothing to warn about.
    run_manifest_set "$run" --arg f "2026-09-18T12:00:00-07:00" '.finished = $f' || return 1
    out=$(VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" --dry-run "$source" 2>&1) || { echo "$out"; return 1; }
    [[ $out != *"Continuing the unfinished run"* && $out != *"leaves unfinished run"* ]] \
        || { echo "a finished run was continued or warned about: $out"; return 1; }
}

t_no_chunk_releases_master_and_resumes_from_prefilter() {
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    command -v ffmpeg >/dev/null 2>&1 || { echo "no ffmpeg"; return 77; }
    local d="$TMPROOT/no-chunk-resume" run master
    run="$d/run"
    master="$run/work/masters/tape_master_from_prepare_video.mp4"
    mkdir -p "$(dirname -- "$master")" "$run/logs"
    ffmpeg -v error -y \
        -f lavfi -i testsrc2=size=64x48:rate=60:duration=2 \
        -f lavfi -i sine=frequency=440:sample_rate=48000:duration=2 \
        -c:v libx264 -preset fast -crf 12 -pix_fmt yuv444p -g 30 \
        -c:a aac -shortest -metadata comment='prepare_video.sh master' \
        "$master" || return 1
    run_manifest_init "$run" hi8-dv prepare_video.sh "$d" || return 1
    run_manifest_set "$run" --arg src /archive/tape.dv '.sources = [$src]' || return 1

    # Stop deliberately at GPU launch. The assertion inside the real main()
    # invocation proves the master was already gone at that exact point.
    local pass
    for pass in initial source-free-resume; do
        PIPELINE_TEST_PASS="$pass" \
            PIPELINE_RESUME_COMMAND='examples/enhance_tape.sh --resume' \
            "$py" - "$ROOT" "$run" "$master" <<'PYEOF' || return 1
import importlib.util
import os
import subprocess
import sys

root, run, master = sys.argv[1:]
spec = importlib.util.spec_from_file_location(
    "pipeline_no_chunk_resume_test", root + "/video_upscale_pipeline.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.RUN_ROOT = os.path.dirname(run)
real_run = m.subprocess.run

def stop_before_gpu(cmd, *args, **kwargs):
    if isinstance(cmd, list) and "inference_realesrgan_video.py" in cmd:
        assert not os.path.exists(master), "master still present at GPU launch"
        raise subprocess.CalledProcessError(
            86, cmd, output="intentional test stop", stderr="intentional test stop")
    return real_run(cmd, *args, **kwargs)

m.subprocess.run = stop_before_gpu
args = m.parse_arguments([
    master, "--profile", "hi8dv", "--scale", "2",
    "--run-dir", run, "--log-dir", run + "/logs",
    "--crf", "23", "--yes",
])
if os.environ["PIPELINE_TEST_PASS"] == "source-free-resume":
    args = m.parse_arguments(["--resume"])
    assert args.run_dir == os.path.realpath(run)
try:
    m.main(args)
except subprocess.CalledProcessError as exc:
    assert exc.returncode == 86
else:
    raise AssertionError("test stop before GPU was not reached")
PYEOF
    done

    [[ ! -e $master ]] || { echo "prepared master was retained"; return 1; }
    jq -e '
        .use_chunking == false and
        .no_chunk_prefilter_validated == true and
        .no_chunk_prefilter_duration > 1.9 and
        .audio_present == true
    ' "$run/work/processing_chunks/source_measurements.json" >/dev/null \
        || { echo "no-chunk source-free checkpoint is incomplete"; return 1; }
    grep -q 'Reusing validated pre-filter intermediate' "$run/logs/pipeline.log" \
        || { echo "source-free resume did not reuse the pre-filter"; return 1; }
}

t_no_chunk_completed_run_retains_metadata() {
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    command -v ffmpeg >/dev/null 2>&1 || { echo "no ffmpeg"; return 77; }
    local d="$TMPROOT/no-chunk-completed" run master
    run="$d/run"
    master="$run/work/masters/tape_master_from_prepare_video.mp4"
    mkdir -p "$(dirname -- "$master")" "$run/logs"
    ffmpeg -v error -y \
        -f lavfi -i testsrc2=size=64x48:rate=60:duration=2 \
        -f lavfi -i sine=frequency=440:sample_rate=48000:duration=2 \
        -c:v libx264 -preset fast -crf 12 -pix_fmt yuv444p -g 30 \
        -c:a aac -shortest -metadata comment='prepare_video.sh master' \
        "$master" || return 1
    run_manifest_init "$run" hi8-dv prepare_video.sh "$d" || return 1

    # Complete the real short-video path with a CPU scale in place of GPU
    # inference. Encoding, audio, delivery checks and cleanup all run normally.
    "$py" - "$ROOT" "$run" "$master" <<'PYEOF' || return 1
import importlib.util
import json
import os
from pathlib import Path
import sys

root, run, master = sys.argv[1:]
spec = importlib.util.spec_from_file_location(
    "pipeline_no_chunk_completion_test", root + "/video_upscale_pipeline.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.RUN_ROOT = os.path.dirname(run)
real_run = m.subprocess.run
recorded = []

def cpu_inference(cmd, *args, **kwargs):
    if isinstance(cmd, list) and "inference_realesrgan_video.py" in cmd:
        recorded.append(json.loads((Path(m.PROCESSING_DIR) / "metadata.json").read_text()))
        assert not os.path.exists(master), "master was not released at its checkpoint"
        source = cmd[cmd.index("-i") + 1]
        destination = str(Path(cmd[cmd.index("-o") + 1]) / "scaled.mp4")
        scale = cmd[cmd.index("-s") + 1]
        return real_run([
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source,
            "-vf", f"scale=iw*{scale}:ih*{scale}", "-an",
            "-c:v", "libx264", "-threads", "2", "-preset", "fast",
            "-crf", cmd[cmd.index("--crf") + 1],
            "-pix_fmt", cmd[cmd.index("--out_pix_fmt") + 1], destination,
        ], *args, **kwargs)
    return real_run(cmd, *args, **kwargs)

m.subprocess.run = cpu_inference
args = m.parse_arguments([
    master, "--profile", "hi8dv", "--scale", "2",
    "--run-dir", run, "--log-dir", run + "/logs",
    "--crf", "23", "--yes",
])
assert m.main(args) == 0
assert len(recorded) == 1, "short video did not use whole-video inference"
metadata = Path(run) / "logs/metadata.json"
assert metadata.is_file(), "completed short run discarded metadata.json"
assert json.loads(metadata.read_text()) == recorded[0], "retained metadata changed"
assert not (Path(run) / "work").exists(), "completed run retained temporary work"
assert json.loads((Path(run) / "run.json").read_text())["finished"]
assert len(list((Path(run) / "videos").glob("*.mp4"))) == 1
PYEOF
}

t_chunked_releases_master_and_resumes_from_split() {
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    command -v ffmpeg >/dev/null 2>&1 || { echo "no ffmpeg"; return 77; }
    local d="$TMPROOT/chunk-resume" run master
    run="$d/run"
    master="$run/work/masters/tape_master_from_prepare_video.mp4"
    mkdir -p "$(dirname -- "$master")" "$run/logs"
    ffmpeg -v error -y \
        -f lavfi -i testsrc2=size=64x48:rate=60:duration=2 \
        -f lavfi -i sine=frequency=440:sample_rate=48000:duration=2 \
        -c:v libx264 -preset fast -crf 12 -pix_fmt yuv444p -g 30 \
        -c:a aac -shortest -metadata comment='prepare_video.sh master' \
        "$master" || return 1
    run_manifest_init "$run" hi8-dv prepare_video.sh "$d" || return 1
    run_manifest_set "$run" --arg src /archive/tape.dv '.sources = [$src]' || return 1

    # Exercise the real split, exact frame ledger, audio extraction, durable
    # source checkpoint and release ordering. Stop at the first GPU launch so
    # this remains a CPU-only test. Then decline a source-free resume to prove
    # its only video copy is preserved, and finally accept the prompt and reach
    # GPU launch again with the master absent.
    local pass
    for pass in initial source-free-decline source-free-resume; do
        PIPELINE_TEST_PASS="$pass" \
            PIPELINE_RESUME_COMMAND='examples/enhance_tape.sh --resume' \
            "$py" - "$ROOT" "$run" "$master" <<'PYEOF' || return 1
import builtins
import importlib.util
import os
import subprocess
import sys

root, run, master = sys.argv[1:]
spec = importlib.util.spec_from_file_location(
    "pipeline_chunk_resume_test", root + "/video_upscale_pipeline.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.RUN_ROOT = os.path.dirname(run)
real_run = m.subprocess.run

def stop_before_gpu(cmd, *args, **kwargs):
    if isinstance(cmd, list) and "inference_realesrgan_video.py" in cmd:
        assert not os.path.exists(master), "master still present at GPU launch"
        if os.environ.get("PIPELINE_TEST_PASS") == "source-free-resume":
            open(run + "/source-free-resume.reached", "w").close()
        raise subprocess.CalledProcessError(
            86, cmd, output="intentional test stop", stderr="intentional test stop")
    return real_run(cmd, *args, **kwargs)

m.subprocess.run = stop_before_gpu
argv = [
    master, "--profile", "hi8dv", "--scale", "2",
    "--run-dir", run, "--log-dir", run + "/logs",
    "--crf", "23", "--chunk-duration", "1",
    "--chunk-threshold", "1",
]
test_pass = os.environ["PIPELINE_TEST_PASS"]
if test_pass == "initial":
    argv.append("--yes")
else:
    prompts = []
    def answer(prompt):
        prompts.append(prompt)
        return "n" if test_pass == "source-free-decline" else "y"
    builtins.input = answer
args = m.parse_arguments(["--resume"] if test_pass == "source-free-resume" else argv)
try:
    m.main(args)
except subprocess.CalledProcessError as exc:
    assert test_pass != "source-free-decline"
    assert exc.returncode == 86
except SystemExit as exc:
    assert test_pass == "source-free-decline"
    assert exc.code == 0
    assert prompts == ["  Proceed? (y/n): "], prompts
    assert os.path.isdir(run + "/work/processing_chunks/0_input_chunks")
else:
    raise AssertionError("expected test stop was not reached")
PYEOF
    done

    [[ ! -e $master ]] || { echo "prepared master was retained"; return 1; }
    jq -e '
        .use_chunking == true and
        .audio_present == true and
        .frames > 0
    ' "$run/work/processing_chunks/source_measurements.json" >/dev/null \
        || { echo "chunked source-free checkpoint is incomplete"; return 1; }
    jq -e 'length > 0 and all(.[]; . > 0)' \
        "$run/work/processing_chunks/chunk_source_frames.json" >/dev/null \
        || { echo "chunk source-frame ledger is incomplete"; return 1; }
    [[ -f $run/source-free-resume.reached ]] \
        || { echo "chunked source-free resume was not reached"; return 1; }
}

t_chunked_resume_with_master_present() {
    # The counterpart of the no-chunk checkpoint test. When the master is still
    # present -- --keep-input, or a batch-owned run -- SOURCE_MEASUREMENTS is a
    # fresh measure_source_state(), which carries none of the checkpoint keys
    # the source-free path writes. Reading one of those keys from it raises
    # KeyError, which is exactly the defect the no-chunk path had. Nothing in
    # the chunked path does that today; this test is what keeps it that way.
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    command -v ffmpeg >/dev/null 2>&1 || { echo "no ffmpeg"; return 77; }
    local d="$TMPROOT/chunk-master-present" run master
    run="$d/run"
    master="$run/work/masters/tape_master_from_prepare_video.mp4"
    mkdir -p "$(dirname -- "$master")" "$run/logs"
    ffmpeg -v error -y \
        -f lavfi -i testsrc2=size=64x48:rate=60:duration=2 \
        -f lavfi -i sine=frequency=440:sample_rate=48000:duration=2 \
        -c:v libx264 -preset fast -crf 12 -pix_fmt yuv444p -g 30 \
        -c:a aac -shortest -metadata comment='prepare_video.sh master' \
        "$master" || return 1
    run_manifest_init "$run" hi8-dv prepare_video.sh "$d" || return 1

    # Both passes keep the master. The second reaches the chunk loop with a
    # complete checkpoint on disk and a freshly measured SOURCE_MEASUREMENTS in
    # memory -- the combination that made the no-chunk branch raise.
    local pass
    for pass in seed resume; do
        PIPELINE_TEST_PASS="$pass" "$py" - "$ROOT" "$run" "$master" <<'PYEOF' || return 1
import importlib.util, os, subprocess, sys
root, run, master = sys.argv[1:]
spec = importlib.util.spec_from_file_location(
    "chunked_master_present", root + "/video_upscale_pipeline.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.RUN_ROOT = os.path.dirname(run)  # never touch the real ai/ tree
real_run = m.subprocess.run

def stop_before_gpu(cmd, *args, **kwargs):
    if isinstance(cmd, list) and "inference_realesrgan_video.py" in cmd:
        assert os.path.exists(master), "--keep-input master was released"
        if os.environ["PIPELINE_TEST_PASS"] == "resume":
            open(run + "/master-present-resume.reached", "w").close()
        raise subprocess.CalledProcessError(
            86, cmd, output="intentional test stop", stderr="intentional test stop")
    return real_run(cmd, *args, **kwargs)

m.subprocess.run = stop_before_gpu
args = m.parse_arguments([
    master, "--profile", "hi8dv", "--scale", "2",
    "--run-dir", run, "--log-dir", run + "/logs",
    "--crf", "23", "--chunk-duration", "1", "--chunk-threshold", "1",
    "--keep-input", "--yes",
])
try:
    m.main(args)
except subprocess.CalledProcessError as exc:
    assert exc.returncode == 86
else:
    raise AssertionError("expected test stop was not reached")
PYEOF
    done

    [[ -e $master ]] || { echo "--keep-input master was removed"; return 1; }
    [[ -f $run/master-present-resume.reached ]] \
        || { echo "resume with the master present did not reach the chunk loop"; return 1; }
    jq -e '.use_chunking == true and .audio_present == true and .frames > 0' \
        "$run/work/processing_chunks/source_measurements.json" >/dev/null \
        || { echo "chunked checkpoint is incomplete"; return 1; }
}

t_split_only_resume_still_confirms() {
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    command -v ffmpeg >/dev/null 2>&1 || { echo "no ffmpeg"; return 77; }
    local d="$TMPROOT/split-confirm" run master processing
    run="$d/run"
    master="$run/work/masters/tape_master_from_prepare_video.mp4"
    processing="$run/work/processing_chunks"
    mkdir -p "$(dirname -- "$master")" "$run/logs" \
        "$processing/0_input_chunks" "$processing/1_esrgan_chunks" \
        "$processing/2_final_chunks"
    ffmpeg -v error -y \
        -f lavfi -i testsrc2=size=64x48:rate=60:duration=2 \
        -f lavfi -i sine=frequency=440:sample_rate=48000:duration=2 \
        -c:v libx264 -preset fast -crf 12 -pix_fmt yuv444p -g 30 \
        -c:a aac -shortest -metadata comment='prepare_video.sh master' \
        "$master" || return 1
    cp -- "$master" "$processing/0_input_chunks/chunk_000.mp4" || return 1
    jq -n --arg input "$master" '
        {input_video:$input, scale_factor:2, profile:"hi8dv",
         model:"RealESRGAN_x2plus", no_rife:true,
         crf:23, chunk_duration:1}
    ' > "$processing/metadata.json" || return 1

    "$py" - "$ROOT" "$run" "$master" <<'PYEOF' || return 1
import builtins
import importlib.util
import sys

root, run, master = sys.argv[1:]
spec = importlib.util.spec_from_file_location(
    "pipeline_split_confirmation_test", root + "/video_upscale_pipeline.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
prompts = []

def decline(prompt):
    prompts.append(prompt)
    return "n"

builtins.input = decline
args = m.parse_arguments([
    master, "--profile", "hi8dv", "--scale", "2",
    "--run-dir", run, "--log-dir", run + "/logs",
    "--crf", "23", "--chunk-duration", "1",
    "--chunk-threshold", "1",
])
try:
    m.main(args)
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError("declining the pre-flight plan did not exit")
assert prompts == ["  Proceed? (y/n): "], prompts
PYEOF
}

t_no_chunk_resume_uses_saved_checkpoint_with_master_present() {
    local py
    py=$(pipeline_python) || { echo "no python3"; return 77; }
    command -v ffmpeg >/dev/null 2>&1 || { echo "no ffmpeg"; return 77; }
    local d="$TMPROOT/no-chunk-master-present" run master work prefilter esrgan
    run="$d/run"
    master="$run/work/masters/tape_master_from_prepare_video.mp4"
    work="$run/work/no_chunk"
    prefilter="$work/tape_nc_prefiltered.mp4"
    esrgan="$work/tape_nc_esrgan.mp4"
    mkdir -p "$(dirname -- "$master")" "$run/logs"
    ffmpeg -v error -y \
        -f lavfi -i testsrc2=size=64x48:rate=60:duration=2 \
        -f lavfi -i sine=frequency=440:sample_rate=48000:duration=2 \
        -c:v libx264 -preset fast -crf 12 -pix_fmt yuv444p -g 30 \
        -c:a aac -shortest -metadata comment='prepare_video.sh master' \
        "$master" || return 1
    run_manifest_init "$run" hi8-dv prepare_video.sh "$d" || return 1

    # First invocation records a valid checkpoint but --keep-input retains the
    # master. Stop before inference, then stand in for a completed ESRGAN stage
    # and remove the consumed pre-filter. The next invocation must read the
    # checkpoint duration from saved_source_measurements even though a fresh
    # SOURCE_MEASUREMENTS object was just measured from the still-present input.
    "$py" - "$ROOT" "$run" "$master" <<'PYEOF' || return 1
import importlib.util, os, subprocess, sys
root, run, master = sys.argv[1:]
spec = importlib.util.spec_from_file_location("checkpoint_seed", root + "/video_upscale_pipeline.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.RUN_ROOT = os.path.dirname(run)  # never touch the real ai/ tree
real_run = m.subprocess.run
def stop(cmd, *args, **kwargs):
    if isinstance(cmd, list) and "inference_realesrgan_video.py" in cmd:
        raise subprocess.CalledProcessError(86, cmd, output="stop", stderr="stop")
    return real_run(cmd, *args, **kwargs)
m.subprocess.run = stop
args = m.parse_arguments([master, "--profile", "hi8dv", "--scale", "2",
    "--run-dir", run, "--log-dir", run + "/logs", "--crf", "23",
    "--keep-input", "--yes"])
try:
    m.main(args)
except subprocess.CalledProcessError as exc:
    assert exc.returncode == 86
else:
    raise AssertionError("checkpoint seed did not stop before GPU")
PYEOF
    [[ -e $master && -e $prefilter ]] \
        || { echo "keep-input checkpoint did not retain master and pre-filter"; return 1; }
    ffmpeg -v error -y -i "$prefilter" -vf scale=128:96 \
        -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -an "$esrgan" \
        || return 1
    rm -f -- "$prefilter"

    "$py" - "$ROOT" "$run" "$master" <<'PYEOF' || return 1
import importlib.util, os, sys
root, run, master = sys.argv[1:]
spec = importlib.util.spec_from_file_location("checkpoint_resume", root + "/video_upscale_pipeline.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.RUN_ROOT = os.path.dirname(run)  # never touch the real ai/ tree
class ReachedESRGANCheck(Exception): pass
def stop_at_esrgan_check(*args, **kwargs):
    if kwargs.get("label", "").startswith("No-chunk ESRGAN output"):
        raise ReachedESRGANCheck
    return None
m.check_output_crf = stop_at_esrgan_check
args = m.parse_arguments([master, "--profile", "hi8dv", "--scale", "2",
    "--run-dir", run, "--log-dir", run + "/logs", "--crf", "23",
    "--keep-input", "--yes"])
try:
    m.main(args)
except ReachedESRGANCheck:
    pass
else:
    raise AssertionError("resume did not reach the saved ESRGAN output check")
PYEOF
    grep -q 'Pre-filter already consumed by a validated downstream' \
        "$run/logs/pipeline.log" \
        || { echo "master-present resume did not use downstream checkpoint"; return 1; }
}


# A two-second interlaced DVD-style source, enough for prepare_video.sh's
# preflight and encode to run for real: 720x480 displayed at 4:3, so its
# sample aspect ratio is 8:9 as on the discs. Skips (77) without the tools.
mkdvd() {  # mkdvd <path>
    local dep
    for dep in ffmpeg ffprobe python3; do
        command -v "$dep" >/dev/null 2>&1 || { echo "no $dep"; return 77; }
    done
    mkdir -p "$(dirname -- "$1")" || return 1
    ffmpeg -v error -f lavfi -i 'testsrc2=size=720x480:rate=30000/1001:duration=2' \
        -f lavfi -i 'sine=frequency=440:sample_rate=48000:duration=2' \
        -c:v mpeg2video -flags +ilme+ildct -top 1 -aspect 4:3 -c:a ac3 -b:a 192k \
        -f dvd "$1"
}

# Exercise the real DVD demux/deinterlace/audio encode path, including awkward paths.
t_prepare_dvd_aac() {
    local d source master codec rc
    d="$TMPROOT/prepare-dvd"
    source="$d/-DVD Hi8 & café.mpg"
    mkdvd "$source" || { rc=$?; return $rc; }
    bash "$ROOT/prepare_video.sh" --yes --output-dir "$d/masters" \
        --log-dir "$d/logs" "$source" >"$d/prepare.log" 2>&1 \
        || { cat "$d/prepare.log"; return 1; }
    local -a masters=("$d/masters/"*.mp4)
    [[ ${#masters[@]} == 1 && -f ${masters[0]} ]] || return 1
    master=${masters[0]}
    codec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name \
        -of default=noprint_wrappers=1:nokey=1 "$master") || return 1
    [[ $codec == aac ]] || { echo "Expected AAC master, found $codec"; return 1; }
    grep -Fq 'Frame-count check: 120 frames (expected 120)' "$d/prepare.log" || return 1
    # The master keeps the source's sample aspect ratio, and the run says so
    # before and after the encode rather than leaving it to the filters.
    local source_sar master_sar
    source_sar=$(ffprobe -v error -select_streams v:0 -show_entries stream=sample_aspect_ratio \
        -of csv=p=0 "$source")
    master_sar=$(ffprobe -v error -select_streams v:0 -show_entries stream=sample_aspect_ratio \
        -of csv=p=0 "$master")
    [[ $source_sar == 8:9 && $master_sar == 8:9 ]] \
        || { echo "Expected SAR 8:9 on source and master, found $source_sar and $master_sar"; return 1; }
    grep -Fq 'Aspect:        SAR 8:9 set on the master as on the source' "$d/prepare.log" \
        || { echo "pre-flight summary did not state the aspect ratio"; return 1; }
    grep -Fq 'Aspect check: SAR 8:9, DAR 4:3 (matches source).' "$d/prepare.log" \
        || { echo "master aspect ratio was not verified"; return 1; }
}

# Declining the preflight prompt is not work. A run made for that invocation
# alone holds nothing but the summary that was declined, so it is removed
# again; left behind, it is one more unfinished run in the listing and, for
# the Hi8 driver, one more candidate for --resume to find. prepare_video.sh
# exits 3 for a decline, which is what lets the driver tell it from a failed
# preparation, whose run keeps its log.
t_prepare_declined_prompt_leaves_no_run() {
    local d source out rc
    d="$TMPROOT/prepare-declined"
    source="$d/src/tape.mpg"
    mkdvd "$source" || { rc=$?; return $rc; }
    local preservation="$d/VideoPreservation" root="$d/VideoPreservation/ai"
    out=$(printf 'n' | VIDEO_PRESERVATION_ROOT="$preservation" \
        bash "$ROOT/prepare_video.sh" "$source" 2>&1); rc=$?
    (( rc == 3 )) || { echo "declining the prompt should exit 3, got $rc: $out"; return 1; }
    grep -Fq 'Removed the unused run directory' <<<"$out" \
        || { echo "decline did not report removing its run: $out"; return 1; }
    [[ -z $(find "$root" -mindepth 1 -print -quit 2>/dev/null) ]] \
        || { echo "declined run was left behind:"; find "$root"; return 1; }

    # A driver's run is the driver's: with --output-dir and --log-dir pointing
    # into it, a decline leaves the directory alone.
    mkdir -p "$d/driver-run"
    out=$(printf 'n' | bash "$ROOT/prepare_video.sh" --output-dir "$d/driver-run/work/masters" \
        --log-dir "$d/driver-run/logs" "$source" 2>&1); rc=$?
    (( rc == 3 )) || { echo "declining the prompt should exit 3, got $rc: $out"; return 1; }
    [[ -f $d/driver-run/logs/prepare.log ]] \
        || { echo "decline removed a run it did not make"; return 1; }
}

# Only y or n answers preparation's prompt: a bare Enter and any other text
# ask again, and input that closes without an answer stops the script with a
# failure rather than being taken as one.
t_prepare_prompt_takes_only_y_or_n() {
    local d source out rc
    d="$TMPROOT/prepare-y-or-n"
    source="$d/src/tape.mpg"
    mkdvd "$source" || { rc=$?; return $rc; }
    local -a prepare=(bash "$ROOT/prepare_video.sh" --output-dir "$d/run/work/masters"
        --log-dir "$d/run/logs" "$source")
    mkdir -p "$d/run"
    out=$(printf '\nyes\nn\n' | VIDEO_PRESERVATION_ROOT="$d/VideoPreservation" \
        "${prepare[@]}" 2>&1); rc=$?
    (( rc == 3 )) || { echo "n after two non-answers should decline with 3, got $rc: $out"; return 1; }
    [[ $(grep -c 'Answer y or n' <<<"$out") -eq 2 ]] \
        || { echo "each non-answer should ask again: $out"; return 1; }
    out=$(printf '\nmaybe' | VIDEO_PRESERVATION_ROOT="$d/VideoPreservation" \
        "${prepare[@]}" 2>&1); rc=$?
    (( rc == 1 )) || { echo "closed input should stop with 1, got $rc: $out"; return 1; }
    grep -Fq 'Input closed before a y or n answer' <<<"$out" \
        || { echo "closed input was not reported: $out"; return 1; }
    [[ -z $(find "$d/run/work/masters" -type f -print -quit 2>/dev/null) ]] \
        || { echo "a master was made without a y"; find "$d/run/work/masters"; return 1; }
}

# The capture's name decides the bottom mask and the pipeline profile, through
# the table both example scripts read, and a name the table doesn't know is
# refused before anything is made.
t_tape_names_choose_mask_and_profile() {
    pipeline_python >/dev/null || { echo "no python3"; return 77; }
    local d="$TMPROOT/tape-names" preservation out rc entry name mask profile
    preservation="$d/VideoPreservation"
    mkdir -p "$d/src"
    for entry in 'vhs_wedding_1991.dv 8 vhsdv' 'hi8_1994_summer.dv 8 hi8dv' \
                 'dv.20071124-103447.avi 0 dv' '19950716_19950903.mpg 8 hi8dv'; do
        read -r name mask profile <<<"$entry"
        : > "$d/src/$name"
        out=$(VIDEO_PRESERVATION_ROOT="$preservation" \
            "$ROOT/examples/enhance_tape.sh" --dry-run "$d/src/$name" 2>&1) || { echo "$out"; return 1; }
        grep -Fq -- "--mask $mask --output-dir" <<<"$out" \
            || { echo "$name was not given mask $mask: $out"; return 1; }
        grep -Fq -- "--profile $profile --scale 2" <<<"$out" \
            || { echo "$name was not given profile $profile: $out"; return 1; }
        out=$("$ROOT/examples/prepare_tape_capture.sh" --dry-run "$d/src/$name" 2>&1) || { echo "$out"; return 1; }
        grep -Fq -- "--mask $mask" <<<"$out" && grep -Fq -- "--profile $profile" <<<"$out" \
            || { echo "prepare_tape_capture.sh disagrees about $name: $out"; return 1; }
    done
    : > "$d/src/tape.dv"
    out=$(VIDEO_PRESERVATION_ROOT="$preservation" "$ROOT/examples/enhance_tape.sh" "$d/src/tape.dv" 2>&1); rc=$?
    (( rc == 1 )) || { echo "an unknown name should be refused with 1, got $rc: $out"; return 1; }
    grep -Fq 'Unrecognised capture name: tape.dv' <<<"$out" \
        || { echo "the refusal was not explained: $out"; return 1; }
    [[ ! -e $preservation ]] || { echo "a refused capture left something behind:"; find "$preservation"; return 1; }
}

# A VHS run is filed as "other", as the pipeline files its own runs of that
# profile. The script reaches it through its own state, for --resume and for a
# relaunch of the same capture, and refuses a run without that state.
t_tape_vhs_run_resumes_and_continues() {
    pipeline_python >/dev/null || { echo "no python3"; return 77; }
    local preservation="$TMPROOT/vhs-run/VideoPreservation"
    local root="$preservation/ai" run master source out rc
    mkdir -p "$root" "$TMPROOT/vhs-run/archive"
    source="$TMPROOT/vhs-run/archive/vhs_wedding_1991.dv"; : > "$source"
    source=$(realpath -- "$source")
    run=$(mkrun "$root" vhs-run other '{}') || return 1
    master="$run/work/masters/vhs_master_from_prepare_video.mp4"
    mkdir -p "$run/state" "$run/work/masters"; : > "$master"
    run_manifest_set "$run" --arg src "$source" '.sources = [$src]' || return 1
    jq -n --arg source "$source" --arg master "$master" \
        '{version:2, source_capture:$source, prepared_master:$master,
          pipeline:{profile:"vhsdv",scale:2,model:null,rife:false,crf:23,
                    chunk_duration:null,chunk_threshold:900}}' \
        > "$run/state/enhance_tape.json" || return 1
    run_set_current "$root" "$run" || return 1

    out=$(VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" --resume --dry-run 2>&1) || { echo "$out"; return 1; }
    grep -Fq -- '--profile vhsdv --scale 2' <<<"$out" \
        || { echo "the VHS recipe was not reconstructed: $out"; return 1; }
    grep -Fq 'Stage 1: skipped, master present' <<<"$out" \
        || { echo "the resumed run did not reuse its master: $out"; return 1; }

    out=$(VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" --dry-run "$source" 2>&1) || { echo "$out"; return 1; }
    grep -Fq "Continuing the unfinished run of this capture: $run" <<<"$out" \
        || { echo "relaunching the VHS capture did not continue its run: $out"; return 1; }

    # With the master gone and no split to resume from, preparation runs
    # again, and the mask comes from the capture's name as it did at first.
    rm -f -- "$master"
    out=$(VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" --resume --dry-run 2>&1) || { echo "$out"; return 1; }
    grep -Fq -- "$source --mask 8 --output-dir" <<<"$out" \
        || { echo "the resumed preparation lost the mask: $out"; return 1; }

    # Without this script's state, an "other" run is not this script's to resume.
    rm -f -- "$run/state/enhance_tape.json"
    out=$(VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" --resume --dry-run 2>&1); rc=$?
    (( rc == 1 )) || { echo "a run without this script's state should be refused with 1, got $rc: $out"; return 1; }
    grep -Fq 'Not a run of this script' <<<"$out" || { echo "the refusal was not explained: $out"; return 1; }
}

# A run the script makes records the class its profile implies, and
# preparation is given the mask the name implies.
t_tape_vhs_run_is_filed_as_other() {
    local d="$TMPROOT/vhs-class" source out rc run
    source="$d/src/vhs_tape.mpg"
    mkdvd "$source" || { rc=$?; return $rc; }
    run="$d/VideoPreservation/ai/existing"
    mkdir -p "$run"
    out=$(printf 'n' | VIDEO_PRESERVATION_ROOT="$d/VideoPreservation" \
        "$ROOT/examples/enhance_tape.sh" --run-dir "$run" "$source" 2>&1); rc=$?
    (( rc == 3 )) || { echo "declining preparation should exit 3, got $rc: $out"; return 1; }
    [[ $(jq -r .class "$run/run.json") == other ]] \
        || { echo "the VHS run was not filed as other: $(cat "$run/run.json")"; return 1; }
    grep -Fq 'Bottom Mask:   8 pixels' "$run/logs/prepare.log" \
        || { echo "preparation was not given mask 8: $(cat "$run/logs/prepare.log")"; return 1; }
}

t_hi8_declined_preparation_leaves_no_run_or_pointer() {
    local d source out rc
    d="$TMPROOT/hi8-declined"
    source="$d/src/hi8_tape.mpg"
    mkdvd "$source" || { rc=$?; return $rc; }
    local preservation="$d/VideoPreservation" root="$d/VideoPreservation/ai"
    out=$(printf 'n' | VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" "$source" 2>&1); rc=$?
    (( rc == 3 )) || { echo "declining preparation should exit 3, got $rc: $out"; return 1; }
    grep -Fq 'Removed the unused run directory' <<<"$out" \
        || { echo "decline did not report removing its run: $out"; return 1; }
    [[ $out != *'No master was produced'* ]] \
        || { echo "decline was reported as a failed preparation: $out"; return 1; }
    [[ ! -L $root/current_run ]] \
        || { echo "current_run still points at the declined run: $(readlink "$root/current_run")"; return 1; }
    [[ -z $(find "$root" -mindepth 1 -print -quit 2>/dev/null) ]] \
        || { echo "declined run was left behind:"; find "$root"; return 1; }

    # A run that already existed is someone else's record, whoever declines
    # in it: --run-dir on a directory that was there before stays.
    local run="$root/existing"
    mkdir -p "$run"
    out=$(printf 'n' | VIDEO_PRESERVATION_ROOT="$preservation" \
        "$ROOT/examples/enhance_tape.sh" --run-dir "$run" "$source" 2>&1); rc=$?
    (( rc == 3 )) || { echo "declining preparation should exit 3, got $rc: $out"; return 1; }
    [[ -f $run/run.json && -f $run/logs/prepare.log ]] \
        || { echo "decline removed a run it did not make: $out"; return 1; }
}

main() {
    while (($#)); do
        case $1 in
            -v|--verbose) VERBOSE=true; shift ;;
            --only) [[ $# -ge 2 ]] || { printf '[ERROR] --only requires a pattern\n' >&2; exit 2; }
                    ONLY=$2; shift 2 ;;
            -V|--version) printf '%s %s\n' "${0##*/}" "$VERSION"; exit 0 ;;
            -h|--help) sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) printf '[ERROR] Unknown option: %s\n' "$1" >&2
               printf "Try '%s --help' for more information.\n" "${0##*/}" >&2; exit 2 ;;
        esac
    done

    ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
    TMPROOT=$(mktemp -d "${TMPDIR:-/tmp}/ai-pipeline-tests.XXXXXX")
    trap 'rm -rf "$TMPROOT"' EXIT

    # Child tools create manifests even when their media output is explicit.
    # Keep those manifests and current_run pointers inside the test cleanup.
    export VIDEO_PRESERVATION_ROOT="$TMPROOT/preservation"
    mkdir -p "$VIDEO_PRESERVATION_ROOT"

    source "$ROOT/pipeline_tools/run_lib.sh"
    PIPELINE_PY=""
    if [[ -x $ROOT/venv/bin/python3 ]]; then
        PIPELINE_PY="$ROOT/venv/bin/python3"
    elif PIPELINE_PY=$(command -v python3 2>/dev/null); then
        export VIRTUAL_ENV="${VIRTUAL_ENV:-$ROOT}"
    fi

    run_test 'prepare: DVD AC-3 becomes AAC in the MP4 master' t_prepare_dvd_aac
    run_test 'prepare: a declined prompt leaves no run behind'          t_prepare_declined_prompt_leaves_no_run
    run_test 'prepare: only y or n answers the prompt'                  t_prepare_prompt_takes_only_y_or_n
    run_test 'hi8: a declined preparation leaves no run or pointer'     t_hi8_declined_preparation_leaves_no_run_or_pointer
    run_test 'run id: groups sources and survives an awkward source name' t_run_id_shape
    run_test 'run layout: paths match pipeline_docs/storage-layout.md'   t_run_layout_contract
    run_test 'manifest: an interrupted write leaves a readable manifest' t_manifest_is_atomic
    run_test 'resume: a stopped run is adopted, not duplicated'          t_adoption_continues_the_same_run
    run_test 'resume: a finished or mismatched run is never adopted'     t_adoption_refuses_wrong_runs
    run_test 'batch: unreadable durations are not a duration match'      t_duration_validator_rejects_unreadable
    run_test 'assembly: chapter marks land on the clip they name'        t_chapter_marks_land_on_the_clip_they_name
    run_test 'assembly: the day is recorded as the file created'  t_assembled_day_is_recorded_as_the_file_it_created
    run_test 'assembly: the assembled day keeps its audio'        t_assembled_day_keeps_its_audio
    run_test 'assembly: source audio is held to its own pictures' t_source_audio_is_held_to_its_own_pictures
    run_test 'assembly: source audio beyond one frame is refused' t_source_audio_beyond_one_frame_is_refused
    run_test 'gates: final A/V sync notices a constant offset'    t_av_sync_gate_notices_a_constant_offset
    run_test 'gates: packet bounds handle awkward probe output' t_packet_bounds_handle_awkward_probe_output
    run_test 'gates: duration checks run and are frame-based'           t_duration_gates_run_and_are_frame_based
    run_test 'resume: current_run follows only the selected live run'   t_current_run_pointer_is_safe_and_precise
    run_test 'resume: unfinished Hi8 run can become current'             t_latest_unfinished_hi8_run_can_be_selected
    run_test 'batch: state keys validated without migration'              t_batch_state_requires_canonical_day_keys
    run_test 'resume: Hi8 short form reconstructs source and recipe'     t_hi8_short_resume_reconstructs_the_recorded_command
    run_test 'relaunch: same capture continues its unfinished run'     t_hi8_relaunch_continues_the_current_run
    run_test 'tape: the name chooses mask and profile, or is refused'   t_tape_names_choose_mask_and_profile
    run_test 'tape: a VHS run resumes and continues on relaunch'        t_tape_vhs_run_resumes_and_continues
    run_test 'tape: a VHS run is filed as other, masked by its name'    t_tape_vhs_run_is_filed_as_other
    run_test 'resume: no-chunk run releases master before GPU'           t_no_chunk_releases_master_and_resumes_from_prefilter
    run_test 'completion: short run retains processing metadata'         t_no_chunk_completed_run_retains_metadata
    run_test 'resume: chunked run releases master before GPU'            t_chunked_releases_master_and_resumes_from_split
    run_test 'resume: split alone still requires confirmation'           t_split_only_resume_still_confirms
    run_test 'resume: master-present no-chunk uses saved checkpoint'      t_no_chunk_resume_uses_saved_checkpoint_with_master_present
    run_test 'resume: master-present chunked run reaches chunk loop' t_chunked_resume_with_master_present
    run_test 'batch: arbitrary layout groups by DV recording day' t_batch_groups_arbitrary_layout_by_dv_recording_day
    run_test 'batch: subcode plan exposes a bad camcorder clock' t_subcode_plan_exposes_bad_camcorder_clock
    run_test 'audit: uses the batch recorded day grouping' t_daily_audit_uses_the_batch_recorded_grouping
    run_test 'batch: entry point starts and records its config'  t_batch_entry_point_starts_and_records_its_config

    printf '\nSummary\n  passed  %d\n  failed  %d\n  skipped %d\n' "$PASS" "$FAIL" "$SKIP"
    if ((FAIL)); then
        printf '\n%d failure(s):\n' "$FAIL"
        printf '  - %s\n' "${FAILURES[@]}"
        exit 1
    fi
    printf '\nAll pipeline functional tests passed.\n'
    exit 0

}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
