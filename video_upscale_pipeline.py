#!/usr/bin/env python3
"""Upscale legacy video (VHS, Hi8, DV camcorder, DVD) with Real-ESRGAN.

Input is a progressive master from prepare_video.sh; a raw capture is refused.
Output is one H.264 yuv420p MP4 at --crf, muxed with the source audio, written
into a run directory described by pipeline_docs/storage-layout.md.

Long videos are split into chunks, processed one at a time and concatenated, so
an interrupted run resumes from the last completed chunk. RIFE frame
interpolation is available with --rife and is off by default.

Quality constants and the reasoning behind each are defined together below.
"""

import subprocess
import os
import shutil
import shlex
import signal
import termios
import json
import math
import glob
import time
import sys
import argparse
import re
from fractions import Fraction
from datetime import datetime, timedelta
import statistics

from pipeline_tools.run_names import clean_label, source_stem

# --- Config ---
PRESERVATION_ROOT = os.environ.get(
    "VIDEO_PRESERVATION_ROOT",
    os.path.join(os.path.expanduser("~"), "VideoPreservation"),
)
# Every file one run produces lands inside one run directory under
# PRESERVATION_ROOT/ai/. These module-level values are placeholders: the real
# paths are derived from the run directory in main(), either the one a driver
# passed with --run-dir or one this invocation creates for itself. See
# pipeline_docs/storage-layout.md.
# The model used when --model is not given. Referenced both where the model is
# selected and where the manifest records it, so run.md can never name a
# different model from the one that ran.
DEFAULT_MODELS = {2: "RealESRGAN_x2plus", 4: "RealESRGAN_x4plus"}

RUN_ROOT = os.path.join(PRESERVATION_ROOT, "ai")
RUN_DIR = None          # set in main()
OUTPUT_DIR = None       # <run>/videos
LOG_DIR = None          # <run>/logs/<source stem>
RIFE_BIN = "./rife-ncnn-vulkan/rife-ncnn-vulkan"

# Shared CRF values keep encoding, preflight plans and verification consistent.
PREFILTER_CRF = 12     # pre-filter intermediate (fed to Real-ESRGAN, deleted after)
RIFE_ENCODE_CRF = 14   # RIFE-reassembly intermediate, deleted after concat
ESRGAN_WRITER_CRF = 16  # inference_realesrgan_video.py's own writer; this value must be kept
# in sync with the crf= set in that script's Writer class -- it lives
# in a separate file this script does not control.
#
# Both are INTERMEDIATE values only, and only under --rife: the ESRGAN
# writer's file is then decoded to PNG rather than delivered, so it stays
# near-transparent and the one lossy step is the RIFE encode at --crf.
# With RIFE off (the default) the writer produces the deliverable
# directly at --crf and neither constant is used.

# Default CRF for the final deliverable (yuv420p), used whenever --crf is not
# given. Objective metrics and controlled visual review against a yuv444p/CRF16
# encode showed no repeatable perceptible difference
# between CRF20 and CRF23 on ESRGAN-upscaled output.
#
# The sweeps used preset slow after a CRF16 intermediate, whereas the
# production writer encodes model frames directly at explicitly pinned preset
# medium. Avoiding a generation and using a faster preset have opposing
# effects, so the net production/test difference remains unmeasured. The
# recommendation rests on the documented visual results and size tradeoff.
#
# KEEP IN SYNC with DEFAULT_CRF in upscale_dv_batch.sh -- if one
# changes, change the other too. The wrapper needs its own copy to normalise
# the batch configuration signature it records (see state_check_config)
# without actually passing a CRF through when none was requested; there is
# no single source of truth shared between the two scripts.
DEFAULT_CRF = 23

# --- Quality Config (chroma format) ---
# Expected pix_fmt for every step that writes final-deliverable-bound video
# (the ESRGAN writer's output, and the RIFE-encode output). Both must match
# this exactly, or a multi-chunk concat can silently mix chroma formats --
# confirmed by direct test that ffmpeg's -f concat -c:v copy does not error
# or warn on a pix_fmt mismatch between chunks. Kept as one constant (not
# separate per-site values like the CRF constants) because, unlike CRF where
# different steps legitimately target different numbers, every step here is
# expected to agree on the SAME chroma format -- a mismatch between them is
# never intentional, only ever a sign of a partially-updated pipeline.
EXPECTED_PIX_FMT = "yuv444p"

# --- Quality Config (colorimetry tagging) ---
# The pipeline handles NTSC standard definition: prepare_video.sh refuses a
# source tagged with any other colour standard, and stamps these values on
# the master. inference_realesrgan_video.py's Writer (bgr24 -> yuv444p via
# libx264) stamps the same values, and the concat and mux steps below stamp
# them again. Without an explicit tag, a player has to guess the matrix on
# conversion to RGB, which can produce a visible color/gamma shift.
# smpte170m is the correct tag for NTSC SD (not bt470bg/PAL, and not bt709,
# which is for HD). Supporting another standard means carrying the source's
# tags through every one of those steps, not changing these constants alone.
COLOR_PRIMARIES = "smpte170m"
COLOR_TRC = "smpte170m"
COLOR_SPACE = "smpte170m"

# --- ffmpeg invocation ---
# Every ffmpeg command in this script passes -nostdin. Without it ffmpeg reads
# the terminal it inherits for its interactive keys, and ffmpeg 4.4's 'c' and
# 'd' keys start a line-entry loop that polls stdin without waiting and holds
# the encode until Enter arrives. A key typed into the pipeline's window while
# Real-ESRGAN runs waits in the terminal's input queue -- Real-ESRGAN's own
# ffmpeg processes read pipes -- and is taken by the next pre-filter, which
# then spins one core and encodes nothing. The pipeline's own y/n prompts read
# stdin, so the whole run cannot simply be redirected from /dev/null.

# --- Auto-Tuning Config ---
MIN_CHUNK_SEC = 10
MAX_CHUNK_SEC = 300  # Largest autotuned chunk; sized for an 8GB-class GPU
DISK_SAFETY_MARGIN = 0.5
EST_PNG_COMP_RATIO = 0.4

# --- Chunking Config ---
# Videos shorter than this threshold are processed without chunking: ESRGAN
# runs on the pre-filtered master directly, skipping the split/concat scaffold.
# At 15 minutes the temp PNG footprint (~33 GB at 2x DV/60fps) is well within
# the disk capacity of the primary processing host. Files >= this threshold
# use the normal autotune/chunk/concat path unchanged.
NO_CHUNK_THRESHOLD_SEC = 15 * 60  # 900 seconds

# All derived from PROCESSING_DIR by set_run_paths() once the run directory is
# known. STOP_FILE is touched by hand to request a graceful stop after the
# current chunk.
PROCESSING_DIR = None
INPUT_CHUNKS_DIR = None
ESRGAN_CHUNKS_DIR = None
FINAL_CHUNKS_DIR = None
CONCAT_FILE = None
STOP_FILE = None
TEST_MODE_CHUNKS = None


# --- Helper Functions ---
class Timer:
    """Context manager for simple execution timing."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, *args):
        self.end = time.time()
        duration = self.end - self.start
        print(f"   [Perf] {self.name} took {duration:.2f} seconds.")


def check_venv():
    """Ensures the script is running inside the virtual environment."""
    # sys.prefix != sys.base_prefix is the standard way to detect a venv in Python 3
    if not (sys.prefix != sys.base_prefix or 'VIRTUAL_ENV' in os.environ):
        print("\n[!] ERROR: Virtual environment not detected.")
        print("    This pipeline requires specific versions (e.g., numpy<2.0) found in venv.")
        print("    Please run: source venv/bin/activate")
        sys.exit(1)


def safe_rmtree(path):
    """Safely remove a directory tree."""
    if os.path.isdir(path):
        shutil.rmtree(path)


def ask_yes_no(prompt):
    """Ask until the answer is y or n, and return True for y.

    Anything else, a bare Enter included, asks again; Ctrl+C stops the run
    (KeyboardInterrupt reaches cli_main). Keys typed at the terminal before
    the prompt appeared are discarded first: the prompts follow stages that
    do not read the keyboard, so whatever was typed during them is still
    queued and must not answer a question nobody has seen.
    """
    if sys.stdin.isatty():
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            print("\n[ERROR] Input closed before a y or n answer; stopping.")
            sys.exit(1)
        if answer in ("y", "n"):
            return answer == "y"
        print("  Answer y or n (Ctrl+C to stop).")


def run_class_for_profile(profile):
    """The media class recorded in a standalone run's manifest.

    A run's class says what its videos mean, not where they are stored. The
    pre-filter profile is the only thing a single-source invocation knows about
    the material it was given, so hi8dv identifies a Hi8-over-DV transfer and
    everything else is filed as other. A batch sets its own class directly.
    """
    return "hi8-dv" if profile == "hi8dv" else "other"


def write_run_manifest(run_dir, class_name, settings, sources):
    """(see below) -- an existing manifest's sources win over the input path."""
    """Create run.json for a run this invocation owns.

    Written through a temporary file and a rename so an interrupted run leaves
    either no manifest or a complete one, never a truncated file that no longer
    parses. A driver that created the run directory writes its own manifest;
    this is only for a standalone invocation.
    """
    # An earlier stage may already have recorded what this run is really made
    # from. This pipeline is handed a master, so its own idea of "source" is a
    # file inside the run; the recorded one is the capture.
    recorded = []
    started = None
    try:
        with open(os.path.join(run_dir, "run.json")) as handle:
            previous = json.load(handle)
            recorded = previous.get("sources") or []
            started = previous.get("started")
    except (OSError, json.JSONDecodeError):
        pass

    manifest = {
        "run_id": os.path.basename(run_dir.rstrip("/")),
        "class": class_name,
        "entry_point": "video_upscale_pipeline.py",
        "preservation_root": PRESERVATION_ROOT,
        "started": started or datetime.now().astimezone().isoformat(timespec="seconds"),
        "finished": None,
        "command": sys.argv,
        "settings": settings,
        "sources": recorded or [os.path.abspath(p) for p in sources],
        "videos": [],
        "excluded": [],
    }
    _write_manifest(run_dir, manifest)


def finish_run_manifest(run_dir, video):
    """Record the run's output and the moment it ended."""
    path = os.path.join(run_dir, "run.json")
    try:
        with open(path) as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return
    manifest["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if video:
        manifest["videos"] = [os.path.basename(video)]
    _write_manifest(run_dir, manifest)


def _write_manifest(run_dir, manifest):
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "run.json")
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def make_run_id(label):
    """Build <label>_<YYYYMMDD-HHMMSS>, grouping runs of the same source.

    The label comes from a source filename, so it is reduced to characters
    that make an unambiguous path component.
    """
    return f"{clean_label(label)}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def set_run_paths(run_dir, source_path, log_dir=None):
    """Derive every path this pipeline writes from one run directory.

    pipeline_docs/storage-layout.md is the specification: videos to videos/,
    this source's logs to logs/<stem>/, and the chunk scaffold to work/, which
    is transient and removed when the run finishes.

    log_dir overrides where this invocation's logs go. A driver passes it for
    two reasons: it hands this pipeline a prepared master, whose name is not
    the name of the capture the run is really processing, and it has many
    sources to keep apart, so it gives each one its own logs/<capture>/.

    A run with a single source puts its logs directly in logs/. The extra level
    would be named after the only source there is, which the run directory is
    already named after -- a path that repeats itself twice and makes reaching
    a log on the command line longer for no gain.
    """
    global RUN_DIR, OUTPUT_DIR, LOG_DIR, LOG_FILE
    global PROCESSING_DIR, INPUT_CHUNKS_DIR, ESRGAN_CHUNKS_DIR, FINAL_CHUNKS_DIR
    global CONCAT_FILE, STOP_FILE

    RUN_DIR = os.path.abspath(run_dir)
    OUTPUT_DIR = os.path.join(RUN_DIR, "videos")
    LOG_DIR = os.path.abspath(log_dir) if log_dir else os.path.join(RUN_DIR, "logs")
    LOG_FILE = os.path.join(LOG_DIR, "pipeline.log")
    PROCESSING_DIR = os.path.join(RUN_DIR, "work", "processing_chunks")
    INPUT_CHUNKS_DIR = os.path.join(PROCESSING_DIR, "0_input_chunks")
    ESRGAN_CHUNKS_DIR = os.path.join(PROCESSING_DIR, "1_esrgan_chunks")
    FINAL_CHUNKS_DIR = os.path.join(PROCESSING_DIR, "2_final_chunks")
    CONCAT_FILE = os.path.join(PROCESSING_DIR, "concat_list.txt")
    STOP_FILE = os.path.join(PROCESSING_DIR, "STOP")
    os.makedirs(LOG_DIR, exist_ok=True)


FINAL_INPUT_VIDEO = None   # set in main(); the master finish_run() may release
KEEP_INPUT = False


def set_current_run(run_dir):
    """Atomically publish the standalone run without replacing a real directory."""
    os.makedirs(RUN_ROOT, exist_ok=True)
    link = os.path.join(RUN_ROOT, "current_run")
    if os.path.lexists(link) and not os.path.islink(link):
        raise RuntimeError(f"Refusing to replace non-symlink current_run: {link}")
    # A bare --resume follows this pointer, so taking it from an unfinished
    # run is worth saying out loud: that run is still resumable, but only by
    # naming it with --run-dir.
    previous = os.path.realpath(link) if os.path.islink(link) else None
    if previous and previous != os.path.realpath(run_dir):
        try:
            with open(os.path.join(previous, "run.json")) as handle:
                manifest = json.load(handle)
                if isinstance(manifest, dict) and not manifest.get("finished"):
                    print(f"  [!] current_run leaves unfinished run {previous}; "
                          f"resume it with --resume --run-dir {shlex.quote(previous)}")
        except (OSError, ValueError):
            pass
    temporary = link + f".tmp.{os.getpid()}"
    os.symlink(os.path.realpath(run_dir), temporary)
    try:
        os.replace(temporary, link)
    finally:
        if os.path.islink(temporary):
            os.unlink(temporary)


def clear_current_run(run_dir):
    """Leave a newer run's pointer alone when an older run finishes."""
    link = os.path.join(RUN_ROOT, "current_run")
    if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(run_dir):
        os.unlink(link)


def clear_resume_state(run_dir):
    """Drop the resume recipe once the run is finished.

    state/pipeline_resume.json exists only to restart an unfinished run after
    its master is released; a finished run cannot be resumed (restore refuses
    a finished manifest), and run.json already records the command. Leaving it
    behind makes a completed run look mid-flight.
    """
    state = os.path.join(run_dir, "state")
    try:
        os.remove(os.path.join(state, "pipeline_resume.json"))
    except OSError:
        pass
    try:
        os.rmdir(state)  # removed only when nothing else was left in it
    except OSError:
        pass


def save_resume_arguments(args, run_dir):
    """Persist the full recipe independently of disposable input media."""
    state = dict(vars(args))
    state.update(input_video=os.path.abspath(args.input_video),
                 run_dir=os.path.realpath(run_dir), resume=False)
    if state.get("log_dir"):
        state["log_dir"] = os.path.abspath(state["log_dir"])
    directory = os.path.join(run_dir, "state")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "pipeline_resume.json")
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump({"version": 1, "arguments": state}, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def restore_resume_arguments(parser, args, argv):
    # Recipe changes belong to explicit invocations; --resume means replay the
    # saved recipe, without silently applying today's command-line defaults.
    check = argparse.ArgumentParser(add_help=False)
    check.add_argument("--resume", action="store_true")
    check.add_argument("--run-dir")
    check.add_argument("--verify-full-decode", action="store_true")
    _, extra = check.parse_known_args(argv)
    if extra:
        parser.error("--resume accepts only --run-dir and --verify-full-decode; "
                     "input and settings come from saved state")
    link = os.path.join(RUN_ROOT, "current_run")
    if not args.run_dir and not os.path.islink(link):
        parser.error("no current run; start a new run with a prepared master")
    run = os.path.realpath(args.run_dir or link)
    try:
        with open(os.path.join(run, "run.json")) as handle:
            manifest = json.load(handle)
        if manifest.get("entry_point") != "video_upscale_pipeline.py":
            raise ValueError("current run belongs to another entry point; resume it with that tool")
        if manifest.get("finished"):
            raise ValueError("the selected run is already finished")
        with open(os.path.join(run, "state", "pipeline_resume.json")) as handle:
            saved = json.load(handle)
        state = saved["arguments"]
        if saved["version"] != 1 or set(state) != set(vars(args)):
            raise ValueError("unsupported or incomplete pipeline resume state")
        if not isinstance(state["verify_full_decode"], bool):
            raise ValueError("resume verification choice must be a boolean")
        if not isinstance(state["input_video"], str) or not os.path.isabs(state["input_video"]):
            raise ValueError("resume state has no absolute input path")
        if os.path.realpath(state["run_dir"]) != run:
            raise ValueError("resume state belongs to a different run directory")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.error(f"cannot resume {run}: {exc}")
    state["resume"] = True
    state["verify_full_decode"] = state["verify_full_decode"] or args.verify_full_decode
    return argparse.Namespace(**state)


def finish_run(owns_run, final_video):
    """Complete an owned run from either processing path.

    Seal its manifest, render its summary, prune work and clear its pointer."""
    if not owns_run:
        return
    # Release any remaining disposable master after delivery checks. This is
    # idempotent with cleanup at the durable processing checkpoint.
    free_master_if_disposable(FINAL_INPUT_VIDEO, RUN_DIR, KEEP_INPUT)
    prune_empty_work(RUN_DIR)
    finish_run_manifest(RUN_DIR, final_video)
    write_run_md(RUN_DIR)
    clear_current_run(RUN_DIR)
    clear_resume_state(RUN_DIR)


def write_run_md(run_dir):
    """Write the human-readable summary beside run.json.

    A run directory holding video and a JSON file tells whoever finds it
    nothing about what the video is or whether the directory can be deleted,
    so it eventually gets deleted. run.md is that answer, rendered from the
    manifest by pipeline_tools/write_run_md.py -- one renderer shared by every
    entry point, so the readable file cannot disagree with the machine-readable
    one. Failure to write it is reported but does not fail a finished run.
    """
    renderer = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pipeline_tools", "write_run_md.py")
    try:
        subprocess.run([sys.executable, renderer, run_dir],
                       check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  [!] could not write run.md: {exc}")


# Cleanup requires a run-owned location plus the preparation filename, codec,
# metadata tag and CRF. A standalone preparation run may hand its master to a
# separate upscale run; its manifest establishes ownership in that case.
MASTER_MARKER = "_master_from_prepare_video"
MASTER_TAG_FRAGMENT = "prepare_video.sh master"


# Codecs that identify an unprepared capture rather than a prepared master.
# A capture off tape is interlaced: Real-ESRGAN has no concept of fields and
# would upscale each interlaced frame as though it were progressive, doubling
# the comb artefacts instead of resolving them. The result looks processed and
# is quietly ruined.
UNPREPARED_CAPTURE_CODECS = ("dvvideo", "mpeg2video")


def check_input_is_prepared(input_video, allow_unprepared):
    """Require a progressive master rather than an interlaced or raw capture.

    prepare_video.sh deinterlaces and optionally masks captures before inference."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,field_order", "-of", "default=nw=1:nk=1", input_video],
        capture_output=True, text=True).stdout.split()
    codec = probe[0] if probe else ""
    field_order = probe[1] if len(probe) > 1 else "unknown"

    problems = []
    if codec in UNPREPARED_CAPTURE_CODECS:
        problems.append(f"it is a {codec} capture, not a prepared master")
    if field_order in ("tt", "bb", "tb", "bt"):
        problems.append(f"it is interlaced (field order {field_order})")
    if not problems:
        return

    print("\n[!] ERROR: this input has not been prepared for upscaling.")
    for problem in problems:
        print(f"      - {problem}")
    print("    Real-ESRGAN upscales frames, not fields. An interlaced frame is")
    print("    two half-pictures from different moments; upscaling it doubles")
    print("    the combing rather than resolving it, and the damage is not")
    print("    visible in any log.")
    print(f"    Prepare it first:  ./prepare_video.sh {input_video} --mask N")
    print("    then run this pipeline on the master that produces.")
    # MPEG-2 is refused even so. Its open GOPs begin with B-frames that need
    # the GOP before them: the stream-copy split starts chunks on such GOPs,
    # whose leading pictures then never decode, and the packet counts every
    # timing check relies on include pictures that never decode.
    if allow_unprepared and codec != "mpeg2video":
        print("    --allow-unprepared-input given: continuing anyway.")
        return
    if allow_unprepared:
        print("    --allow-unprepared-input does not cover MPEG-2: chunks split")
        print("    from it lose the pictures that open each GOP, and its packets")
        print("    are not a count of its pictures. prepare_video.sh decodes it")
        print("    whole into a master this pipeline can count and split.")
    sys.exit(1)


def extract_source_audio(input_video, audio_path, force):
    """Extract AAC audio before GPU work and return its duration and priming.

    MP4 retains the encoder delay needed by the final mux. Durable audio and
    measurements let disposable masters be released and allow A/V mismatches
    to be caught before lengthy inference. Return (None, None) for silent input."""
    has_audio = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name", "-of", "default=nw=1:nk=1", input_video],
        capture_output=True, text=True).stdout.strip()
    if not has_audio:
        print("  > Source has no audio stream.")
        return None, None
    copied = has_audio == "aac"

    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
        print(f"  > Audio already extracted: {os.path.basename(audio_path)}")
    else:
        # MP4 cannot hold PCM and several other codecs, so the deliverable
        # needs AAC. Already-AAC audio is copied rather than re-encoded.
        if copied:
            command = ["ffmpeg", "-nostdin", "-y", "-i", input_video, "-vn", "-c:a", "copy", audio_path]
            note = "stream-copied (already AAC)"
        else:
            command = ["ffmpeg", "-nostdin", "-y", "-i", input_video, "-vn",
                       "-c:a", "aac", "-b:a", "192k", "-ac", "2", audio_path]
            note = f"encoded to AAC 192k stereo ({has_audio} -> AAC, required for MP4)"

        subprocess.run(command, check=True, capture_output=True, text=True)
        if not (os.path.exists(audio_path) and os.path.getsize(audio_path) > 0):
            raise RuntimeError(f"Audio extraction produced nothing: {audio_path}")
        print(f"  > Audio {note}.")
    verify_audio_video_duration(input_video, audio_path, force=force)
    # A copy keeps the delay the master's encoder recorded and must decode to
    # exactly what the master decodes to; an encode carries this encoder's own.
    if copied:
        priming = audio_priming_samples(input_video)
        verify_audio_priming(audio_path, priming, "Extracted source audio",
                             reference=input_video, force=force)
    else:
        priming = AAC_ENCODER_DELAY_SAMPLES
        verify_audio_priming(audio_path, priming, "Extracted source audio",
                             force=force)
    return get_video_duration(audio_path), priming


def free_master_if_disposable(input_video, run_dir, keep_input):
    """Delete the input once nothing else needs it, if it is safe to do so."""
    # A chunked run may already have released this master at its durable resume
    # checkpoint. finish_run() calls this function again for the no-chunk path.
    if not input_video or not os.path.exists(input_video):
        return
    if keep_input:
        print("  > --keep-input given: leaving the input in place.")
        return
    disposable, reasons = input_is_disposable_master(input_video, run_dir)
    if not disposable:
        print("  > Input kept (not a disposable master):")
        for reason in reasons:
            print(f"      - {reason}")
        return
    try:
        freed = os.path.getsize(input_video) / (1024 ** 3)
        os.remove(input_video)
        # Only prune the master's two staging directories. rmdir refuses
        # nonempty directories, preserving other masters and active chunks.
        masters = os.path.dirname(os.path.realpath(input_video))
        work = os.path.dirname(masters)
        prep_run = os.path.dirname(work)
        pruned = os.path.basename(masters) == "masters" and os.path.basename(work) == "work"
        if pruned:
            for directory in (masters, work):
                try:
                    os.rmdir(directory)
                except OSError:
                    pruned = False
                    break
        # A separate prepare_video.sh run is now an empty shell: work/ held the
        # master and is gone. Fold it into this run so the result matches the
        # single run enhance_tape.sh produces -- prepare's log joins
        # pipeline.log in logs/, and the capture prepare recorded replaces the
        # now-deleted master as this run's source -- then drop the orphaned
        # directory. The realpath guard makes this a no-op when the master
        # lived under this run's own work/.
        if pruned and os.path.realpath(prep_run) != os.path.realpath(run_dir):
            prep_logs = os.path.join(prep_run, "logs")
            if os.path.isdir(prep_logs):
                dest_logs = os.path.join(run_dir, "logs")
                os.makedirs(dest_logs, exist_ok=True)
                for name in os.listdir(prep_logs):
                    dst = os.path.join(dest_logs, name)
                    if not os.path.exists(dst):  # never clobber this run's own logs
                        try:
                            os.replace(os.path.join(prep_logs, name), dst)
                        except OSError:
                            pass
                try:
                    os.rmdir(prep_logs)
                except OSError:
                    pass
            prep_manifest = os.path.join(prep_run, "run.json")
            try:
                with open(prep_manifest) as fh:
                    prep_sources = json.load(fh).get("sources") or []
            except (OSError, ValueError):
                prep_sources = None
            folded = prep_sources is not None and not prep_sources
            if prep_sources:
                run_manifest = os.path.join(run_dir, "run.json")
                try:
                    with open(run_manifest) as fh:
                        data = json.load(fh)
                    data["sources"] = prep_sources  # the capture, not the master
                    _write_manifest(run_dir, data)
                    folded = True
                except (OSError, ValueError):
                    folded = False
            if folded or prep_sources is None:
                # Provenance is in this run's manifest now, or the preparation
                # manifest carried none; either way it is redundant.
                try:
                    os.remove(prep_manifest)
                except OSError:
                    pass
            else:
                # Could not fold the capture into run.json; keep prepare's
                # manifest rather than lose the only record of it.
                try:
                    os.replace(prep_manifest, os.path.join(run_dir, "master_run.json"))
                except OSError:
                    pass
            try:
                os.rmdir(prep_run)  # only when nothing unexpected remains in it
            except OSError:
                pass
        print(f"  > Freed the prepared master, {freed:.2f} GiB: "
              f"{os.path.basename(input_video)}")
    except OSError as exc:
        print(f"  [!] could not remove the master: {exc}")


def measure_source(input_video):
    """Duration, fps and picture count of the source, taken before processing.

    Deliberately probe_video_timing, the same function the end-of-run check
    uses on the finished video: the two figures are compared against each
    other, so they must be measured the same way or the comparison is between
    two different definitions of "frames".

    Recorded to disk so the check still works after the master is freed, and
    across a resume.
    """
    return probe_video_timing(input_video)


SOURCE_STATE_REQUIRED = {
    "duration", "fps", "frames", "span", "container_duration",
    "input_video", "input_extension", "width", "height", "sar",
    "use_chunking", "audio_present", "audio_file", "audio_duration",
}


def measure_source_state(input_video, use_chunking):
    """Capture everything startup or final validation needs from the master."""
    state = measure_source(input_video)
    width, height = get_video_dimensions(input_video)
    state.update({
        "input_video": os.path.abspath(input_video),
        "input_extension": os.path.splitext(input_video)[1],
        "fps_text": get_video_fps(input_video),
        "width": width,
        "height": height,
        "sar": get_video_sar(input_video),
        "use_chunking": bool(use_chunking),
    })
    return state


def record_source_measurements(processing_dir, measurements):
    """Persist the source's measurements alongside the run's working state.

    Written to disk rather than kept in memory so a resumed run -- and, once
    the master can be freed, a run whose source no longer exists -- can still
    check its output against what went in.
    """
    try:
        os.makedirs(processing_dir, exist_ok=True)
        path = os.path.join(processing_dir, "source_measurements.json")
        tmp = f"{path}.tmp"
        with open(tmp, "w") as handle:
            json.dump(measurements, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise RuntimeError(f"Could not record source measurements: {exc}") from exc


def load_source_measurements(processing_dir, expected_input=None,
                             require_resume_state=False):
    """Read and validate the durable measurements used without the master."""
    path = os.path.join(processing_dir, "source_measurements.json")
    try:
        with open(path) as handle:
            measurements = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        if require_resume_state:
            raise RuntimeError(
                f"The prepared master is absent and its resume state cannot be "
                f"read: {path}: {exc}"
            ) from exc
        return None

    if require_resume_state:
        missing = sorted(SOURCE_STATE_REQUIRED - measurements.keys())
        if missing:
            raise RuntimeError(
                "The prepared master is absent and source_measurements.json "
                f"is incomplete (missing: {', '.join(missing)}). Restore the "
                "complete record or start a new run from a prepared master."
            )
        if expected_input and os.path.abspath(measurements["input_video"]) != os.path.abspath(expected_input):
            raise RuntimeError(
                "The saved source state belongs to a different master:\n"
                f"  saved:     {measurements['input_video']}\n"
                f"  requested: {os.path.abspath(expected_input)}"
            )
    return measurements


def reconcile_durations(source_seconds, video_seconds, video_label,
                        audio_seconds, fps=None, audio_file=None):
    """Compare source, post-split video and audio durations. True when they agree.

    Each side is held to what may legitimately move it, not to one shared
    number. The picture can differ from the source by a frame, the finest it
    can be cut to; the audio by that plus two blocks of its codec, which primes
    with a leading block and pads its final partial block out to a whole one.
    This is the same budget verify_audio_video_duration() applies, and for the
    same reason: both compare a duration against the one extracted audio file.
    A shared figure in seconds would be the wrong shape here:
    2.0s is about 60 frames at 29.97fps, and anything a check that exists to
    stop before the GPU work lets through costs the hours it exists to save.

    Run before any GPU work rather than after it. A split that dropped content
    or a pre-filter that truncated is invisible in every later stage -- each one
    faithfully processes whatever it was given -- and would otherwise surface
    only at the final timing check, hours later. The audio side is checkable
    here because the audio is extracted before the GPU work, not at the end.
    """
    frame_sec = frames_to_seconds(1, fps)
    quantum = audio_quantum_seconds(audio_file) if audio_file else 0.0
    print("\n  > Reconciling source, video and audio durations...")
    ok = True
    checks = ((video_label, video_seconds, frame_sec),
              ("audio", audio_seconds, frame_sec + 2 * quantum))
    for label, value, tolerance in checks:
        if value is None or source_seconds is None:
            print(f"    [!] {label}: not measurable -- cannot compare")
            ok = False
            continue
        delta = abs(value - source_seconds)
        verdict = "OK" if delta <= tolerance else "MISMATCH"
        print(f"    source {source_seconds:.3f}s vs {label} {value:.3f}s "
              f"-- delta {delta*1000:.1f}ms "
              f"(tolerance {tolerance*1000:.1f}ms) {verdict}")
        if delta > tolerance:
            ok = False
    return ok


def input_is_disposable_master(path, run_dir):
    """Recognize a temporary master owned by this run or a preparation run."""
    reasons = []
    resolved = os.path.realpath(path)
    work = os.path.realpath(os.path.join(run_dir, "work"))
    inside_current_work = resolved.startswith(work + os.sep)
    if not inside_current_work:
        # Resolve before checking the layout: a symlink in masters/ must not
        # authorize deleting a capture stored somewhere else.
        masters = os.path.dirname(resolved)
        prep_work = os.path.dirname(masters)
        prep_run = os.path.dirname(prep_work)
        preparation_owned = False
        if (os.path.basename(masters) == "masters"
                and os.path.basename(prep_work) == "work"):
            try:
                with open(os.path.join(prep_run, "run.json")) as handle:
                    manifest = json.load(handle)
                preparation_owned = (
                    isinstance(manifest, dict)
                    and manifest.get("entry_point") == "prepare_video.sh"
                    and manifest.get("run_id") == os.path.basename(prep_run))
            except (OSError, ValueError):
                pass
        if not preparation_owned:
            reasons.append("not inside this run's work/ or a recorded "
                           "prepare_video.sh run's work/masters/")
    if MASTER_MARKER not in os.path.basename(resolved):
        reasons.append(f"filename lacks {MASTER_MARKER}")

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,pix_fmt", "-of", "default=nw=1:nk=1", resolved],
        capture_output=True, text=True)
    fields = probe.stdout.split()
    if fields[:2] != ["h264", "yuv444p"]:
        reasons.append(f"not h264/yuv444p (got {' '.join(fields) or 'nothing'})")

    tags = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=comment",
         "-of", "default=nw=1:nk=1", resolved],
        capture_output=True, text=True).stdout
    if MASTER_TAG_FRAGMENT not in tags:
        reasons.append("container tag does not identify it as a master")

    # get_embedded_x264_crf rather than check_output_crf: this is a question
    # about identity, not a quality gate, so it must return a value rather than
    # log a verdict or exit on a mismatch.
    crf = get_embedded_x264_crf(resolved)
    if crf is None or abs(crf - PREFILTER_CRF) > 0.01:
        reasons.append(f"CRF is {crf}, expected {PREFILTER_CRF}")

    return (not reasons), reasons


def prune_empty_work(run_dir):
    """Remove the run's work/ tree, deepest first, wherever it is empty.

    work/ is transient, and a finished run has none: its presence is what says
    a run stopped part-way through a file. A driver prunes work/ after the last
    capture, but a standalone run has no driver, so it prunes its own.

    Directories are discovered rather than listed by name -- a list has to be
    extended every time the pipeline grows a new scratch directory, and
    forgetting leaves work/ behind holding one empty child. Only empty
    directories are removed, so anything unexpected survives and stays visible.
    """
    work = os.path.join(run_dir, "work")
    if not os.path.isdir(work):
        return
    # Emptiness is re-checked with listdir rather than taken from os.walk's
    # own lists: walk captures a directory's children before the walk descends,
    # so a parent still looks non-empty after its last child has just been
    # removed, and work/ survives holding nothing.
    for current, _directories, _files in os.walk(work, topdown=False):
        try:
            if not os.listdir(current):
                os.rmdir(current)
        except OSError:
            pass
    if not os.path.isdir(work):
        print(f"  Removed empty working directory: {work}/")


def finalize_processing_dir(processing_dir, audio_file, log_dir):
    """Retain a completed run's provenance, then delete its processing state.

    metadata.json records the job identity. Keep it beside this source's
    pipeline.log in the designated log directory.
    Everything else under processing_dir is regenerable working data, and
    nearly all of a run's bytes are in it, so the directory is removed rather
    than archived: work/ is transient and a finished run has none. See
    pipeline_docs/storage-layout.md.

    pipeline.log needs no copying -- it is written directly into log_dir
    from the start of the run, so an interrupted run keeps its log too.

    Args:
        processing_dir: the processing state to retire (str).
        audio_file:     extracted source audio to delete, if any (str or None).
        log_dir:        this source's logs/ or logs/<stem>/ directory (str).
    """
    if not os.path.isdir(processing_dir):
        print(f"  (no processing directory found at {processing_dir} — nothing to finalize)")
        return

    metadata = os.path.join(processing_dir, "metadata.json")
    if os.path.isfile(metadata):
        os.makedirs(log_dir, exist_ok=True)
        shutil.copy2(metadata, os.path.join(log_dir, "metadata.json"))
        print(f"  Kept: {os.path.join(log_dir, 'metadata.json')}")

    if audio_file and os.path.isfile(audio_file):
        os.remove(audio_file)
        print(f"  Removed: {audio_file}")

    safe_rmtree(processing_dir)
    print(f"  Removed working directory: {processing_dir}/")


def load_chunk_durations_from_log(log_file):
    """
    Reconstruct the full-chunk timing history from pipeline.log.

    Scans for lines of the form written at the end of every successfully
    completed full chunk::

        "  > Chunk finished in 12345.67 seconds."

    Partial-resume chunks are never logged with this line (they hit
    ``continue`` before the timing block), so the list naturally contains
    only the same timings that would have been appended to chunk_durations
    during the original run — no additional filtering required.

    Returns an empty list if the log does not exist or contains no matches,
    so callers can treat the result as a plain (possibly empty) list without
    special-casing.
    """
    durations = []
    _FINISHED_RE = re.compile(r"^\s*>\s+Chunk finished in ([0-9]+(?:\.[0-9]+)?)\s+seconds\.")
    try:
        with open(log_file, "r", errors="replace") as fh:
            for line in fh:
                m = _FINISHED_RE.match(line)
                if m:
                    durations.append(float(m.group(1)))
    except FileNotFoundError:
        pass  # first run — log does not exist yet
    except Exception as e:
        print(f"  [WARN] Could not read chunk timings from log: {e}")
    return durations


# --- Logging Helper ---
LOG_FILE = None   # <run>/logs/<source stem>/pipeline.log; set in main()


class TeeLogger:
    """
    Duplicate a stream (stdout or stderr) to console + logfile.
    Resume-safe (append mode). Used for both sys.stdout and sys.stderr so
    neither console output nor exception tracebacks are ever visible on a
    live terminal without also reaching pipeline.log.
    """

    def __init__(self, logfile):
        self.terminal = sys.stdout
        # Line-buffered whatever the destination. Writing to a pipe, as under
        # `| tee`, Python otherwise holds 8 KB before passing anything on, and
        # the terminal falls hours behind pipeline.log.
        try:
            self.terminal.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
        os.makedirs(os.path.dirname(logfile), exist_ok=True)
        self.log = open(logfile, "a", buffering=1)

    def write(self, message):
        # The log first, and regardless of the terminal. Ctrl+C and a closed
        # window stop the tee along with the pipeline, and the report of why
        # the run stopped and how to resume it is written after that; a tee
        # that stops on its own must not cost the run either.
        self.log.write(message)
        if self.terminal is not None:
            try:
                self.terminal.write(message)
            except OSError:
                self._lose_terminal()
        return len(message)

    def flush(self):
        self.log.flush()
        if self.terminal is not None:
            try:
                self.terminal.flush()
            except OSError:
                self._lose_terminal()

    def _lose_terminal(self):
        """Stop writing to a terminal whose reader has gone.

        Its descriptor, and stderr's when that is the same pipe (`2>&1`), is
        pointed at /dev/null. The text left in the stream's buffer then
        drains there at exit instead of raising BrokenPipeError again, and a
        child process started afterwards that inherits the descriptor is not
        killed by SIGPIPE.
        """
        stream, self.terminal = self.terminal, None
        try:
            fd = stream.fileno()
            pipe = os.fstat(fd)
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                targets = {fd}
                stderr = os.fstat(2)
                if (stderr.st_dev, stderr.st_ino) == (pipe.st_dev, pipe.st_ino):
                    targets.add(2)
                for target in targets:
                    os.dup2(null, target)
            finally:
                os.close(null)
        except (AttributeError, OSError, ValueError):
            pass
        self.log.write("[INFO] The terminal stopped reading; output continues "
                       "in this log only.\n")


def get_video_duration(video_file):
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", video_file
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    duration = float(json.loads(result.stdout)["format"]["duration"])
    return duration


# Tolerances are declared in FRAMES and converted here, never written as a
# number of milliseconds. A constant 0.100s reads as a small number and is
# three pictures at 29.97 fps and two and a half at PAL 25 -- it hides both its
# size and its dependence on the frame rate. One picture is the smallest unit
# any of these checks can meaningfully be wrong by, and is the default.
TOLERANCE_FRAMES = 1


def frames_to_seconds(frames, fps):
    """Seconds occupied by a whole number of pictures at fps."""
    if not fps or fps <= 0:
        # Without a frame rate there is nothing to convert; fall back to a
        # single NTSC frame, the tighter of the two systems this handles.
        return frames / 29.97
    return frames / fps


CHUNK_FRAMES_FILE = "chunk_source_frames.json"


def resume_chunk_duration(metadata):
    """Require the recorded split size before reusing any processing state."""
    duration = metadata.get("chunk_duration") if isinstance(metadata, dict) else None
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration <= 0):
        raise RuntimeError(
            "Resume metadata has no valid chunk_duration. Restore the complete "
            "metadata.json or start a new run from a prepared master; existing "
            "chunks will not be retuned or deleted.")
    return duration


def no_chunk_checkpoint_duration(measurements):
    """Read a complete pre-filter checkpoint without inferring it from output."""
    if isinstance(measurements, dict):
        duration = measurements.get("no_chunk_prefilter_duration")
        validated = measurements.get("no_chunk_prefilter_validated")
        if (isinstance(validated, bool) and not isinstance(duration, bool)
                and isinstance(duration, (int, float))
                and math.isfinite(duration) and duration > 0):
            return duration
    raise RuntimeError(
        "Saved no-chunk state lacks a valid pre-filter checkpoint. Restore "
        "source_measurements.json or start a new run from a prepared master; "
        "the checkpoint will not be inferred from processed video.")


def _final_chunk_frame_total(counts, final_chunk_files):
    missing = [os.path.basename(chunk) for chunk in final_chunk_files
               if os.path.basename(chunk).removesuffix("_final.mp4") not in counts]
    if missing:
        raise RuntimeError(
            "Exact chunk coverage requires recorded source-frame counts for "
            "every completed chunk. Missing: " + ", ".join(missing) + ". "
            "Restore chunk_source_frames.json or start a new run from a prepared "
            "master; existing chunks will not be altered.")
    return sum(counts[os.path.basename(chunk).removesuffix("_final.mp4")]
               for chunk in final_chunk_files)


def chunk_source_frame_total(processing_dir, input_chunk_files, final_chunk_files):
    """Count chunks in the source frame domain, requiring every final's record."""
    counts = read_chunk_source_frames(processing_dir)
    total = _final_chunk_frame_total(counts, final_chunk_files)
    return total + sum(probe_video_timing(chunk)["frames"] for chunk in input_chunk_files)


def record_chunk_source_frames(processing_dir, chunk_files, final_chunk_files):
    """Persist each input chunk's picture count, keyed by chunk name.

    Recorded when the chunks are cut, because that is the only moment the
    counts are knowable. Once a chunk has been processed it no longer holds its
    original pictures -- with RIFE it holds roughly twice as many, and the RIFE
    stage permits up to two endpoint frames of variance, so 120 input pictures
    may legitimately come back as 238, 239 or 240. Dividing the output count by
    the interpolation factor to recover the input count therefore yields 119 or
    120 depending on which, and an exact coverage check rejects a correct run.

    Merged into any existing record so a resumed run keeps counts for chunks
    already consumed.
    """
    path = os.path.join(processing_dir, CHUNK_FRAMES_FILE)
    counts = read_chunk_source_frames(processing_dir)
    _final_chunk_frame_total(counts, final_chunk_files)
    for chunk in chunk_files:
        key = os.path.splitext(os.path.basename(chunk))[0]
        frames = probe_video_timing(chunk)["frames"]
        if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
            raise RuntimeError(f"Cannot record an invalid source-frame count for {chunk}: {frames}")
        counts[key] = frames
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as handle:
            json.dump(counts, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise RuntimeError(
            f"Could not record chunk picture counts: {exc}"
        ) from exc
    return counts


def read_chunk_source_frames(processing_dir):
    """Read positive integer counts; an absent record is valid before processing."""
    try:
        with open(os.path.join(processing_dir, CHUNK_FRAMES_FILE)) as handle:
            counts = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read chunk source-frame counts: {exc}") from exc
    if (not isinstance(counts, dict)
            or any(not isinstance(key, str) or not key
                   or isinstance(value, bool) or not isinstance(value, int) or value <= 0
                   for key, value in counts.items())):
        raise RuntimeError("Invalid chunk source-frame counts; expected positive integers keyed by chunk name.")
    return counts


def probe_presentation_span(video_file):
    """Seconds from the first video packet to the end of the last one.

    An independent measurement of the actual timeline, taken from packet
    timestamps rather than from a frame count or a container's own duration
    field. It is the only one of the three that can see a discontinuity: a
    stream with pictures spaced further apart than its declared frame rate has
    the same picture count and the same nominal frame rate, and differs only
    here.

    Returns None when timestamps are unavailable, so callers treat it as a
    check that could not run rather than as a pass.
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "packet=pts_time,duration_time", "-of", "csv=p=0", video_file],
        capture_output=True, text=True)
    rows = [r for r in result.stdout.strip().split("\n") if r.strip()]
    if not rows:
        return None
    # Packets come in DECODE order, and with B-frames the last one decoded is
    # not the last one shown -- taking rows[-1] under-measured the span by
    # about a frame on every real file, which put correct output exactly at the
    # one-frame limit. Presentation order is what a timeline means, so the
    # extremes are found by timestamp.
    first, last_end = None, None
    for row in rows:
        parts = row.split(",")
        try:
            pts = float(parts[0])
        except (ValueError, IndexError):
            continue
        try:
            dur = float(parts[1])
        except (ValueError, IndexError):
            dur = 0.0
        if first is None or pts < first:
            first = pts
        if last_end is None or pts + dur > last_end:
            last_end = pts + dur
    if first is None or last_end is None:
        return None
    return last_end - first


def write_concat_list(list_file, chunk_paths):
    """Join video chunks at their track ends, without MP4 movie rounding.

    MP4 format durations round up to milliseconds. Letting the concat demuxer
    use them inserts a small gap at every boundary, enough to fail the final
    one-frame timing limit on a long run. Track duration_ts retains the actual
    video time base; explicit durations avoid accumulating that padding.
    """
    entries = []
    for path in chunk_paths:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=duration_ts,time_base", "-of", "json",
            os.fspath(path),
        ], capture_output=True, text=True, check=True)
        stream = json.loads(result.stdout)["streams"][0]
        duration = int(stream["duration_ts"]) * Fraction(stream["time_base"])
        if duration <= 0:
            raise ValueError(f"Invalid video track duration for {path}")
        escaped = os.path.abspath(path).replace("'", "'\\''")
        entries.append(f"file '{escaped}'\nduration {float(duration):.9f}\n")
    with open(list_file, "w") as handle:
        handle.writelines(entries)


def probe_video_timing(video_file):
    """Return measured duration, FPS, and picture count for one video.

    The pipeline's DV and H.264 streams contain one encoded picture per video
    packet, so ``-count_packets`` gives the frame count without decoding every
    picture. It still scans the full stream and therefore catches truncated
    media, while adding little CPU overhead relative to ``-count_frames``.
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-count_packets", "-show_entries",
        "stream=duration,nb_frames,nb_read_packets,r_frame_rate,avg_frame_rate:format=duration",
        "-of", "json", video_file,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        probe = json.loads(result.stdout)
        streams = probe.get("streams", [])
        if not streams:
            raise ValueError("no video stream reported")
        stream = streams[0]

        raw_duration = stream.get("duration")
        if raw_duration in (None, "N/A"):
            raw_duration = probe.get("format", {}).get("duration")
        duration = float(raw_duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"invalid duration {raw_duration!r}")

        raw_fps = stream.get("avg_frame_rate")
        if raw_fps in (None, "0/0", "N/A"):
            raw_fps = stream.get("r_frame_rate")
        fps = float(Fraction(raw_fps))
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError(f"invalid frame rate {raw_fps!r}")

        raw_frames = stream.get("nb_read_packets")
        if raw_frames in (None, "N/A"):
            raw_frames = stream.get("nb_frames")
        frames = int(raw_frames)
        if frames <= 0:
            raise ValueError(f"invalid video-picture count {raw_frames!r}")
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError,
            TypeError, ValueError, ZeroDivisionError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or "").strip()
        suffix = f": {detail}" if detail else f": {exc}"
        raise RuntimeError(
            f"Could not measure video timing for {video_file}{suffix}"
        ) from exc

    # The duration used for comparison is DERIVED from the pictures counted and
    # the frame rate, not taken from the container.
    #
    # A container's duration is a claim, and the claims disagree: an MKV chunk
    # cut with -c copy reported 301.233s while holding 18054 pictures, which at
    # 59.940063 fps is 301.201s -- an overhang of two pictures. Comparing one
    # container's claim against another's therefore failed a check on output
    # whose picture count was exactly right, and the same artefact in other
    # forms produced late chapter marks and a spurious 36ms duration mismatch.
    #
    # Pictures and frame rate are measurements of the video itself. The
    # container's own figure is kept for reporting, so a discrepancy between
    # what a file contains and what it claims stays visible.
    # Three independent figures, deliberately kept apart:
    #   duration   -- pictures / frame rate. What a continuous stream SHOULD
    #                 run for. Free of the audio padding that pollutes a
    #                 container's own duration, but blind to timestamp gaps,
    #                 because it is computed from the very count it would need
    #                 to be checked against.
    #   span       -- measured from packet timestamps. The only one that can
    #                 see a discontinuity.
    #   container  -- what the file claims. Retained for reporting so a file
    #                 that misrepresents itself stays visible.
    derived = frames / fps if (frames and fps) else duration
    return {"duration": derived, "fps": fps, "frames": frames,
            "span": probe_presentation_span(video_file),
            "container_duration": duration}


def validate_timing_measurements(source, output, fps_multiplier,
                                 frame_tolerance, duration_tolerance_sec=None):
    """Validate final timing measurements and return comparison details."""
    if fps_multiplier <= 0:
        raise ValueError("fps_multiplier must be positive")
    if frame_tolerance < 0:
        raise ValueError("frame_tolerance must not be negative")

    expected_fps = source["fps"] * fps_multiplier
    expected_frames = round(source["frames"] * fps_multiplier)
    fps_delta = abs(output["fps"] - expected_fps)
    frame_delta = output["frames"] - expected_frames
    duration_delta = output["duration"] - source["duration"]

    # Accommodate the explicitly allowed endpoint-frame variance while still
    # enforcing a tight duration bound. No-RIFE normally uses the 100 ms floor;
    # RIFE may use a slightly larger bound when many chunks each lose one of
    # the two endpoint frames its own per-chunk check permits.
    if duration_tolerance_sec is None:
        # The permitted frames, plus one for the container's own rounding of
        # the final picture's duration. Derived from the frame budget rather
        # than floored at a constant: a floor lets the duration side permit
        # drift the frame side forbids, which is invisible when frame_tolerance
        # is zero.
        duration_tolerance_sec = frames_to_seconds(
            frame_tolerance + TOLERANCE_FRAMES, expected_fps)

    problems = []
    if fps_delta > 0.010:
        problems.append(
            f"frame rate is {output['fps']:.6f} fps, expected {expected_fps:.6f} fps"
        )
    # Picture count and nominal FPS alone cannot reveal timestamp gaps. Compare
    # measured presentation span with the duration implied by picture count.
    for role, measured in (("source", source), ("output", output)):
        span = measured.get("span")
        if span is None:
            continue
        expected_span = measured["duration"]
        gap = abs(span - expected_span)
        if gap > duration_tolerance_sec:
            problems.append(
                f"{role} timeline is discontinuous: pictures span {span:.3f}s "
                f"but {measured['frames']} pictures at {measured['fps']:.6f} fps "
                f"account for {expected_span:.3f}s "
                f"(difference {span - expected_span:+.3f}s, "
                f"tolerance {duration_tolerance_sec:.3f}s)"
            )

    if abs(frame_delta) > frame_tolerance:
        problems.append(
            f"frame count is {output['frames']}, expected {expected_frames} "
            f"(difference {frame_delta:+d}, tolerance ±{frame_tolerance})"
        )
    if abs(duration_delta) > duration_tolerance_sec:
        problems.append(
            f"duration is {output['duration']:.3f}s, expected {source['duration']:.3f}s "
            f"(difference {duration_delta:+.3f}s, tolerance ±{duration_tolerance_sec:.3f}s)"
        )

    return {
        "expected_fps": expected_fps,
        "expected_frames": expected_frames,
        "frame_delta": frame_delta,
        "duration_delta": duration_delta,
        "duration_tolerance": duration_tolerance_sec,
        "problems": problems,
    }


def validate_final_video_timing(source_file, output_file, fps_multiplier,
                                frame_tolerance, source_measurements=None):
    """Check source and delivered timelines, using durable source measurements
    when the disposable master has been released."""
    print("  [timing check] Scanning source and final video picture counts...")
    if source_measurements and source_measurements.get("frames"):
        source = source_measurements
        print("  [timing check] Source figures taken before processing began.")
    else:
        source = probe_video_timing(source_file)
    output = probe_video_timing(output_file)
    result = validate_timing_measurements(
        source, output, fps_multiplier=fps_multiplier,
        frame_tolerance=frame_tolerance,
    )

    print(
        "  [timing check] Source: "
        f"{source['duration']:.3f}s, {source['frames']} frames @ {source['fps']:.6f} fps"
    )
    print(
        "  [timing check] Final:  "
        f"{output['duration']:.3f}s, {output['frames']} frames @ {output['fps']:.6f} fps"
    )

    if result["problems"]:
        for problem in result["problems"]:
            print(f"  [timing check] [ERROR] {problem}")
        raise RuntimeError(
            "Final video timing validation failed. The delivered file is incomplete "
            "or has the wrong frame rate/frame count. Processing intermediates have "
            "been preserved; do not publish or mirror this output."
        )

    print(
        "  [timing check] Passed: "
        f"frames {result['frame_delta']:+d}, duration {result['duration_delta']:+.3f}s."
    )
    return result


def get_embedded_x264_crf(video_file):
    """
    Reads the actual CRF an x264 encode used.

    mediainfo --Full is the primary method: it accesses the container's
    actual structure (codec-private data / SEI) directly, the same way
    ffprobe locates codec_name/width/etc. without scanning a whole file --
    confirmed fast (effectively instant) on a real ~18GB master via direct
    test, versus a full sequential byte scan which has no concept of
    container structure and must read everything in front of a match.

    Falls back to a bounded raw-byte scan (read only the first 256KB, not
    the whole file) ONLY if mediainfo is not installed. x264 embeds its full
    settings string (e.g. "... rc=crf / mbtree=1 / crf=16.0 / qcomp=0.60
    ...") into the encoded stream as an SEI message, plain ASCII, near the
    start of the stream -- confirmed by direct measurement (building real
    MKVs with this project's actual filter chain/encode settings at multiple
    durations): the string lands at the SAME byte offset regardless of total
    file length, since it is fixed container/first-frame overhead, not
    proportional to size. 256KB is ~200x that confirmed offset.

    Do not simplify this to f.read(). Reading a multi-GB chunk file whole
    duplicates it in memory (raw bytes plus decoded string, ~2x file size),
    at several call sites per chunk across a multi-day run. The cost is
    memory, not just I/O.

    This whole check exists because a CRF mismatch between what a step
    *intends* to write and what it *actually* wrote is otherwise invisible: a
    writer that omits -crf silently takes x264's built-in 23, and pipeline.log
    shows nothing wrong because a subprocess exiting zero says nothing about
    the file it produced. Only inspecting the file catches it.

    Returns the CRF as a float, or None if no x264 settings string is found
    (e.g. a stream-copied file, a non-x264 codec, or a corrupt/unreadable
    file -- all treated as "unknown" rather than an error, since this is a
    best-effort diagnostic, not a codec parser).
    """
    if shutil.which("mediainfo"):
        try:
            result = subprocess.run(
                ["mediainfo", "--Full", video_file],
                capture_output=True, text=True, timeout=30
            )
            m = re.search(r"crf=([0-9]+(?:\.[0-9]+)?)", result.stdout)
            if m:
                return float(m.group(1))
            # mediainfo ran but found no Encoding settings field (e.g. a
            # stream-copied file, or a non-x264 codec) -- fall through to
            # the byte-scan fallback below rather than returning None
            # immediately, since the fallback is cheap (256KB) and catches
            # the rare case where mediainfo's parsing misses something the
            # raw bytes still contain.
        except Exception as e:
            print(f"  [WARN] mediainfo failed on {video_file}, falling back to "
                  f"bounded byte scan: {e}")

    try:
        with open(video_file, "rb") as f:
            data = f.read(256 * 1024)
    except OSError as e:
        print(f"  [WARN] Could not read {video_file} for CRF check: {e}")
        return None
    text = data.decode("latin-1", errors="ignore")
    m = re.search(r"crf=([0-9]+(?:\.[0-9]+)?)", text)
    return float(m.group(1)) if m else None


def check_output_crf(video_file, expected_crf, label, fatal=True):
    """
    Probes video_file's actual embedded CRF (see get_embedded_x264_crf) and
    compares it against expected_crf. Always logs the finding -- this is the
    actual point of this function: making real encode quality visible in
    pipeline.log for every run, not just when something looks wrong.

    A HIGHER CRF than expected means LOWER quality than intended (x264's
    scale is inverted: bigger number = more compression). That is the only
    direction worth stopping the pipeline for -- a lower-than-expected CRF
    means the file is at least as good as intended, which is never a defect
    worth halting a multi-hour run over.

    If no CRF could be read at all (get_embedded_x264_crf returned None --
    e.g. a stream-copied file with no x264 SEI string to find), this is
    logged as unknown and treated as non-fatal regardless of `fatal`, since
    there is nothing wrong to report, only nothing to report.

    fatal=True raises RuntimeError on a worse-than-expected CRF, stopping the
    pipeline the same way chunk-coverage and audio/video-duration mismatches
    already do elsewhere in this script -- consistent with the project's
    existing policy of never letting a quality regression pass silently.
    fatal=False logs a warning instead, for intermediates where a different
    call site downstream will perform its own check anyway.
    """
    actual_crf = get_embedded_x264_crf(video_file)
    if actual_crf is None:
        print(f"  [CRF check] {label}: no x264 CRF found in {os.path.basename(video_file)} "
              f"(stream copy, non-x264 codec, or unreadable -- not an error).")
        return None

    if actual_crf > expected_crf:
        msg = (f"{label}: {os.path.basename(video_file)} was encoded at CRF "
               f"{actual_crf:g}, expected CRF {expected_crf:g}. A higher CRF means "
               f"LOWER quality than intended for this step.")
        if fatal:
            print(f"  [CRF check] [ERROR] {msg}")
            raise RuntimeError(
                f"{msg}\nThis file would become part of (or feed into) a permanent "
                f"deliverable at the wrong quality. Fix the encode command for this "
                f"step, or pass an explicit --crf matching {expected_crf:g} if this is "
                f"intentional, then re-run."
            )
        else:
            print(f"  [CRF check] [WARN] {msg}")
    else:
        print(f"  [CRF check] {label}: {os.path.basename(video_file)} confirmed at "
              f"CRF {actual_crf:g} (expected {expected_crf:g} or better).")
    return actual_crf


def get_pix_fmt(video_file):
    """
    Reads the actual pix_fmt of video_file's first video stream via ffprobe.
    Unlike CRF, pix_fmt is a standard stream property ffprobe always exposes
    directly -- no SEI-string byte-scan workaround needed.

    Returns the pix_fmt string, or None if it could not be determined (e.g.
    missing file, no video stream, ffprobe failure) -- treated as "unknown"
    rather than an error by the caller, same convention as
    get_embedded_x264_crf().
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=pix_fmt",
             "-of", "default=noprint_wrappers=1:nokey=1", video_file],
            capture_output=True, text=True, timeout=15
        )
        pix_fmt = result.stdout.strip()
        return pix_fmt if pix_fmt else None
    except Exception as e:
        print(f"  [WARN] Could not read pix_fmt from {video_file}: {e}")
        return None


def check_output_pix_fmt(video_file, expected_pix_fmt, label, fatal=True):
    """Require the expected pixel format before chunks are concatenated.

    FFmpeg stream-copy concatenation accepts mismatched formats, so each chunk
    must be checked independently. Pixel formats are not an ordered quality
    scale: every mismatch is significant. fatal=False reports a warning."""
    actual = get_pix_fmt(video_file)
    if actual is None:
        print(f"  [pix_fmt check] {label}: could not determine pix_fmt of "
              f"{os.path.basename(video_file)} (not an error -- treated as unknown).")
        return None

    if actual != expected_pix_fmt:
        msg = (f"{label}: {os.path.basename(video_file)} has pix_fmt '{actual}', "
               f"expected '{expected_pix_fmt}'.")
        if fatal:
            print(f"  [pix_fmt check] [ERROR] {msg}")
            raise RuntimeError(
                f"{msg}\nChunks with inconsistent pixel formats cannot be combined safely. "
                f"FFmpeg's concat demuxer does not reject this mismatch. "
                f"delete processing_chunks/ and start fresh with the current code, or "
                f"identify and reprocess just the mismatched chunk(s)."
            )
        else:
            print(f"  [pix_fmt check] [WARN] {msg}")
    else:
        print(f"  [pix_fmt check] {label}: {os.path.basename(video_file)} confirmed "
              f"pix_fmt '{actual}' as expected.")
    return actual


def get_color_tags(video_file):
    """
    Reads color_space, color_transfer, and color_primaries from video_file's
    first video stream via ffprobe. Returns a dict with those three keys;
    any field ffprobe doesn't report (or reports as the literal string
    "unknown") comes back as None, since "unknown" means absent, not a real
    value -- confirmed by direct test against this pipeline's own encodes.

    Returns None (not a dict) if ffprobe itself fails, so callers can tell
    "could not inspect the file" apart from "inspected it, fields are unset".
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_space,color_transfer,color_primaries",
             "-of", "default=noprint_wrappers=1", video_file],
            capture_output=True, text=True, timeout=15
        )
        tags = {"color_space": None, "color_transfer": None, "color_primaries": None}
        for line in result.stdout.strip().splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key in tags and value and value != "unknown":
                tags[key] = value
        return tags
    except Exception as e:
        print(f"  [WARN] Could not read color tags from {video_file}: {e}")
        return None


def check_input_color_tags_unset(video_file, label, fatal=True):
    """Accept absent color tags or tags matching this pipeline's smpte170m output.

    The final mux stamps the expected tags. Conflicting values require review
    rather than silently overwriting potentially correct color information.
    fatal=False reports a warning instead of failing."""
    tags = get_color_tags(video_file)
    if tags is None:
        print(f"  [color tag check] {label}: could not inspect "
              f"{os.path.basename(video_file)} (not an error -- treated as unknown).")
        return None

    already_set = {k: v for k, v in tags.items() if v is not None}
    if not already_set:
        print(f"  [color tag check] {label}: {os.path.basename(video_file)} confirmed "
              f"no color tags set, safe to stamp {COLOR_SPACE}.")
        return already_set

    # Tags present -- check whether they match the expected values.
    expected = {"color_space": COLOR_SPACE, "color_transfer": COLOR_TRC,
                "color_primaries": COLOR_PRIMARIES}
    mismatched = {k: v for k, v in already_set.items() if expected.get(k) != v}
    if mismatched:
        msg = (f"{label}: {os.path.basename(video_file)} has color tag(s) set to "
               f"unexpected values: {mismatched} (expected {COLOR_SPACE}). Refusing to "
               f"stamp {COLOR_SPACE}/{COLOR_TRC}/{COLOR_PRIMARIES} over a mismatched value.")
        if fatal:
            print(f"  [color tag check] [ERROR] {msg}")
            raise RuntimeError(
                f"{msg}\nInvestigate before proceeding -- "
                f"do not assume {COLOR_SPACE} is still the correct tag to apply."
            )
        else:
            print(f"  [color tag check] [WARN] {msg}")
    else:
        print(f"  [color tag check] {label}: {os.path.basename(video_file)} already tagged "
              f"{COLOR_SPACE} -- consistent with Writer tagging; mux will confirm.")
    return already_set


def check_output_color_tags(video_file, label, fatal=True):
    """
    Confirms video_file's color_space/color_transfer/color_primaries match
    COLOR_SPACE/COLOR_TRC/COLOR_PRIMARIES, i.e. that the -colorspace/
    -color_trc/-color_primaries flags passed at the mux/concat step actually
    landed on the delivered file. Mirrors check_output_crf/
    check_output_pix_fmt's policy of confirming the file that actually ships
    rather than trusting that the command which was supposed to tag it did
    so without error.
    """
    tags = get_color_tags(video_file)
    if tags is None:
        print(f"  [color tag check] {label}: could not inspect "
              f"{os.path.basename(video_file)} (not an error -- treated as unknown).")
        return None

    expected = {"color_space": COLOR_SPACE, "color_transfer": COLOR_TRC,
                "color_primaries": COLOR_PRIMARIES}
    mismatches = {k: (tags[k], expected[k]) for k in expected if tags[k] != expected[k]}
    if mismatches:
        msg = f"{label}: {os.path.basename(video_file)} color tag mismatch: {mismatches} (got, expected)."
        if fatal:
            print(f"  [color tag check] [ERROR] {msg}")
            raise RuntimeError(msg)
        else:
            print(f"  [color tag check] [WARN] {msg}")
    else:
        print(f"  [color tag check] {label}: {os.path.basename(video_file)} confirmed "
              f"color tags stamped as expected ({COLOR_SPACE}).")
    return tags


def check_output_presentation(video_file, label, full_decode=False):
    """Fail if the MP4's video edit hides stored pictures; optionally decode.

    The check reads timing metadata only and never writes the file. This
    pipeline's muxing writes a video edit that covers every stored picture, so
    a short one is a muxing fault to investigate. Repairing it here would let
    every later check pass on the very file that showed the fault; the file is
    left as written instead. pipeline_tools/repair_mp4_video_edit.py extends
    such an edit on a copy once the cause is understood.
    """
    import struct
    from pipeline_tools.repair_mp4_video_edit import repair_plan

    print(f"  [picture check] {label}: checking MP4 presentation boundaries...", flush=True)
    try:
        hidden_tail = bool(repair_plan(video_file))
    except (OSError, ValueError, KeyError, IndexError, StopIteration, struct.error) as exc:
        raise RuntimeError(f"{label}: MP4 presentation boundary check failed: {exc}") from exc
    if hidden_tail:
        msg = (f"{label}: {os.path.basename(video_file)} has a video edit that "
               f"ends before its last stored pictures, so players would not "
               f"show them. This is a muxing fault; the file is left unchanged "
               f"for investigation. pipeline_tools/repair_mp4_video_edit.py "
               f"can extend the edit on a copy.")
        print(f"  [picture check] [ERROR] {msg}")
        raise RuntimeError(msg)
    print("  [picture check] MP4 presentation boundaries passed.")
    if full_decode:
        check_output_presents_every_picture(video_file, label=label, fatal=True)


def check_output_presents_every_picture(video_file, label, fatal=True):
    """
    Confirms every stored picture in video_file actually decodes.

    Stored pictures and presented pictures are different counts. An MP4 edit
    list defines the presented window, and a sample whose presentation falls
    outside it stays in mdat but never reaches a player -- ffprobe's
    -count_packets still sees it, so every packet-based check passes while the
    delivered file is short. A correct packet count is therefore no evidence
    that the pictures reach a viewer.

    probe_video_timing() counts packets deliberately, to avoid decoding whole
    files on every timing check. This decodes once, on the delivered file only,
    to close the one gap that costs.
    """
    print(f"  [picture check] {label}: full decode verification; this may take many minutes...", flush=True)
    stored = ffprobe_field(video_file, "-count_packets", "stream=nb_read_packets")
    presented = ffprobe_field(video_file, "-count_frames", "stream=nb_read_frames")
    if stored is None or presented is None:
        msg = (f"{label}: could not count pictures in "
               f"{os.path.basename(video_file)}.")
        if fatal:
            print(f"  [picture check] [ERROR] {msg}")
            raise RuntimeError(msg)
        print(f"  [picture check] [WARN] {msg}")
        return None
    if presented != stored:
        msg = (f"{label}: {os.path.basename(video_file)} stores {stored} pictures "
               f"but {presented} decode. Inspect decoder errors and presentation "
               f"metadata to determine the cause.")
        if fatal:
            print(f"  [picture check] [ERROR] {msg}")
            raise RuntimeError(msg)
        print(f"  [picture check] [WARN] {msg}")
    else:
        print(f"  [picture check] {label}: {os.path.basename(video_file)} decodes "
              f"all {presented} stored pictures.")
    return presented


def ffprobe_field(video_file, count_flag, entries):
    """One integer ffprobe field, or None when it cannot be read."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", count_flag,
         "-show_entries", entries, "-of", "default=nw=1:nk=1", video_file],
        capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except (TypeError, ValueError):
        return None


def get_video_sar(video_file):
    """
    Returns the sample aspect ratio (SAR) of the video as a string like '8:9' or '1:1'.
    Returns None if SAR is not set or is 1:1 (square pixels, no correction needed).
    Handles all common legacy formats:
      NTSC 4:3  (VHS/Hi8/DV):     720x480  SAR 8:9   -> display 640x480
      NTSC 16:9 (widescreen DV):  720x480  SAR 32:27 -> display 853x480
      PAL 4:3   (PAL DV/Hi8):     720x576  SAR 16:15 -> display 768x576
      PAL 16:9  (PAL widescreen): 720x576  SAR 64:45 -> display 1024x576
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=sample_aspect_ratio",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_file
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    sar = result.stdout.strip()
    if not sar or sar in ("N/A", "0:1", "1:1"):
        return None
    return sar


def compute_display_width(pixel_width, pixel_height, sar_str):
    """
    Computes the correct display width from pixel dimensions and SAR string.
    e.g. pixel_width=1440, pixel_height=960, sar='8:9' -> display_width=1280
    Returns pixel_width unchanged if SAR is None or 1:1.
    """
    if not sar_str or sar_str in ("N/A", "0:1", "1:1"):
        return pixel_width
    try:
        sar_num, sar_den = map(int, sar_str.split(":"))
        return round(pixel_width * sar_num / sar_den)
    except Exception:
        return pixel_width


def display_aspect_label(pixel_width, pixel_height, sar_str):
    """'1280x960, DAR 4:3' for 1440x960 at SAR 8:9."""
    display_width = compute_display_width(pixel_width, pixel_height, sar_str)
    divisor = math.gcd(display_width, pixel_height) or 1
    return (f"{display_width}x{pixel_height}, "
            f"DAR {display_width // divisor}:{pixel_height // divisor}")


def apply_sar_to_file(filepath, sar_str, pixel_width, pixel_height):
    """
    Writes the pixel aspect ratio into the MP4 with MP4Box -par, which edits
    the container's pasp atom and the AVC stream's VUI in place, without
    re-encoding. ESRGAN works in square pixels, so this is where the source's
    geometry returns to the picture; a deliverable without it displays 3:2
    instead of 4:3, which is as wrong as the wrong CRF, and is treated the
    same way: a failure here stops the run.

    MP4 is the only container this pipeline writes.
    """
    ext = os.path.splitext(filepath)[1].lower()
    sar_num, sar_den = sar_str.split(":")
    if ext != ".mp4":
        raise RuntimeError(f"cannot write SAR {sar_str} into a {ext} container; "
                           f"the pipeline writes MP4")
    tool = "MP4Box"
    if not shutil.which(tool):
        raise RuntimeError(f"{tool} not found (sudo apt install gpac); without it the "
                           f"final output would not carry SAR {sar_str}")
    result = subprocess.run([tool, "-par", f"1={sar_num}:{sar_den}", filepath],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{tool} could not write SAR {sar_str} into "
                           f"{os.path.basename(filepath)}: {result.stderr.strip()}")
    print(f"  > SAR {sar_str} written (container pasp and AVC VUI) to {os.path.basename(filepath)}")
    verify_output_sar(filepath, sar_str, pixel_width, pixel_height)
    return True


def verify_output_sar(filepath, expected_sar, pixel_width, pixel_height):
    """The delivered file's sample aspect ratio, as the container declares it
    and as the first decoded frame carries it. Players read one or the other;
    both must say what the source said."""
    container = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=sample_aspect_ratio", "-of", "csv=p=0", filepath],
        capture_output=True, text=True).stdout.strip()
    frame = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
         "-show_entries", "frame=sample_aspect_ratio", "-of", "csv=p=0", filepath],
        capture_output=True, text=True).stdout.strip().splitlines()
    frame = frame[0].strip() if frame else ""
    if container != expected_sar or frame != expected_sar:
        raise RuntimeError(
            f"final output aspect ratio is wrong: container says {container or 'unset'}, "
            f"stream says {frame or 'unset'}, source is {expected_sar}. The file would "
            f"display at the wrong shape; it is not a deliverable.")
    print(f"  [Aspect check] final output: SAR {container} in the container and the stream; "
          f"{pixel_width}x{pixel_height} displays as "
          f"{display_aspect_label(pixel_width, pixel_height, container)}.")


# Audio-video sync: threshold at which a viewer watching lip movement against
# speech will start to perceive the offset as a mismatch. 80ms is consistent
# with broadcast standards (±90ms) and psychoacoustic research on speech sync
# perception; trained observers can detect as little as ±45ms. Applied as a
# warning-and-prompt level below the hard-abort tolerance, so the operator
# can decide whether to continue if a small but potentially perceptible
# difference is detected.
# Audio/video alignment, expressed in pictures rather than milliseconds.
#
# A flat figure in milliseconds hides both its size and its dependence on the
# frame rate: 80ms is two and a half pictures at 29.97 and near the limit of
# what a viewer tolerates, rather than near the limit of what the pipeline
# should produce. A correct upscale changes the picture, not the timeline, so
# the standard is one picture.
#
# The two directions are not equally objectionable. Audio arriving BEFORE the
# picture that produced it is detected far sooner than audio arriving after --
# sound never precedes its cause in the physical world, so the ear treats it as
# wrong immediately, while a small lag reads as distance. ITU-R BT.1359 puts
# undetectability at roughly -100ms (audio late) against +25ms (audio early).
# The lead bound is therefore the tighter of the two.
AV_SYNC_LAG_FRAMES = 1     # audio may end up to one picture behind
AV_SYNC_LEAD_FRAMES = 1    # audio ahead: one picture, and never more

# AAC pads its final 1024-sample block. Account for that codec quantum
# separately from the tolerance for actual audio drift.
AAC_FRAME_SAMPLES = 1024

# Beyond the codec's own block, a little slack for the container writing a
# duration rounded to its timebase. One millisecond is generous for that and
# far below anything audible.
AUDIO_EXTRACTION_SLACK_SEC = 0.001

# The start-up output ffmpeg's native AAC encoder (-c:a aac) emits ahead of
# the first programme sample. MP4 records it in the audio track's edit list so
# the decoder discards it; a container that does not carry it plays the block
# as programme audio, and everything after it lands one block late.
AAC_ENCODER_DELAY_SAMPLES = 1024

# How much decoded audio two files carrying the same packets must agree on
# exactly. A lost encoder delay shifts every sample, so the opening seconds
# show it as well as the whole file would, at a fraction of the decode.
AUDIO_HEAD_SECONDS = 10


def audio_quantum_seconds(media_file):
    """Seconds of length a lossy audio codec may legitimately add or drop."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,sample_rate", "-of", "default=nw=1:nk=1", media_file],
        capture_output=True, text=True).stdout.split()
    if len(probe) < 2 or probe[0] not in ("aac", "mp3"):
        return 0.0
    try:
        return AAC_FRAME_SAMPLES / float(probe[1])
    except (TypeError, ValueError):
        return 0.0


def measure_audio_seconds(media_file):
    """Audio length in seconds, or None when the file carries no audio.

    Matroska stores no per-stream duration, so ffprobe reports N/A for audio in
    an MKV -- which is what the assembled day videos are. Reading only the
    stream duration therefore skipped the check on exactly the files it most
    needed to run on.

    Fields are read BY NAME from ffprobe's JSON. Parsing the unkeyed output and
    guessing -- treating any number over 1000 as a sample rate, say -- swaps
    duration and sample rate for any track longer than 1000 seconds, which is
    every tape of any length, and returns a duration in the tens of thousands
    of seconds.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-print_format",
         "json", "-show_streams", media_file],
        capture_output=True, text=True)
    try:
        streams = json.loads(probe.stdout).get("streams", [])
    except json.JSONDecodeError:
        return None
    if not streams:
        return None
    stream = streams[0]

    duration = stream.get("duration")
    if duration not in (None, "N/A"):
        try:
            return float(duration)
        except (TypeError, ValueError):
            pass

    # No stream duration (Matroska). Count the audio frames actually present
    # and multiply by the codec's own block size. frame_size is reported by
    # ffprobe per codec, so MP3's 1152 and AAC's 1024 are both handled rather
    # than assuming one of them.
    try:
        sample_rate = float(stream.get("sample_rate"))
    except (TypeError, ValueError):
        return None
    block = stream.get("frame_size")
    try:
        block = int(block)
    except (TypeError, ValueError):
        block = AAC_FRAME_SAMPLES if stream.get("codec_name") == "aac" else None
    if not block:
        return None

    frames = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1",
         media_file], capture_output=True, text=True).stdout.strip()
    if not frames.isdigit():
        return None
    return int(frames) * block / sample_rate


def audio_priming_samples(media_file):
    """Leading decoded samples the container tells the decoder to discard.

    ffmpeg presents a signalled encoder delay by placing the first audio packet
    before zero: an MP4 audio track whose edit list skips 1024 primed samples
    starts at pts -1024. A first packet at or after zero discards nothing, and
    the decoder then plays the encoder's start-up block as programme audio.

    Returns the count in samples, or None when the file has no audio stream or
    its first packet cannot be read. 0 is a real answer, not a failure: it is
    correct for audio that was never primed, and wrong for AAC that was.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,time_base:packet=pts",
         "-read_intervals", "%+#1", "-of", "json", media_file],
        capture_output=True, text=True)
    try:
        data = json.loads(probe.stdout)
        stream = data["streams"][0]
        pts = int(data["packets"][0]["pts"])
        lead = -pts * Fraction(stream["time_base"]) * int(stream["sample_rate"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError,
            ZeroDivisionError):
        return None
    return max(0, round(lead))


def audio_sample_rate(media_file):
    """The first audio stream's sample rate in Hz, or None."""
    rate = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=sample_rate", "-of", "default=nw=1:nk=1", media_file],
        capture_output=True, text=True).stdout.strip()
    return int(rate) if rate.isdigit() and int(rate) > 0 else None


def decoded_audio_head(media_file, seconds=AUDIO_HEAD_SECONDS):
    """The opening seconds of a file's audio as a player decodes it, or None."""
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", media_file, "-map", "0:a:0",
         "-t", str(seconds), "-f", "s16le", "-"],
        capture_output=True)
    return result.stdout if result.returncode == 0 and result.stdout else None


def verify_audio_priming(media_file, expected, label, reference=None,
                         force=False):
    """Fail unless media_file discards exactly the encoder delay it should.

    The delay is a property of how the audio was encoded, so a copy step can
    only preserve it or lose it. Matroska as ffmpeg 4.4 writes it loses it for
    AAC: the packets survive, the instruction to discard the first block does
    not, and the soundtrack plays one block late. Neither the length check nor
    the start-position check sees that -- the packets keep their length, and
    both streams still begin at zero.

    expected is the delay recorded when the audio was extracted: the master's
    own when the audio is copied, the encoder's when the pipeline encodes it.
    None means it is unknown, which fails rather than passing unchecked.

    With a reference carrying the same packets, the two must also decode to
    identical opening samples. That compares what a player hears rather than
    how the container describes it, so a delay represented some other way
    cannot pass while the audio is still shifted.

    Under --force a failure is reported and the run continues; returns False.
    """
    found = audio_priming_samples(media_file)
    problems = []
    if expected is None:
        problems.append("the encoder delay this audio should discard was never "
                        "recorded, so whether it survived cannot be checked")
    elif found != expected:
        problems.append(
            f"it discards {'an unreadable number of' if found is None else found} "
            f"leading samples; the audio was encoded to discard {expected}")
    if reference is not None:
        ours, theirs = decoded_audio_head(media_file), decoded_audio_head(reference)
        if ours is None or theirs is None:
            problems.append(f"the opening audio of it or of "
                            f"{os.path.basename(reference)} could not be decoded")
        elif ours != theirs:
            problems.append(f"its opening {AUDIO_HEAD_SECONDS}s decode differently "
                            f"from {os.path.basename(reference)}, whose packets it copies")
    if problems:
        message = (f"{label}: {os.path.basename(media_file)} does not carry its "
                   f"audio's encoder delay: " + "; ".join(problems) + ".")
        if not force:
            raise RuntimeError(message)
        print(f"  ⚠️  WARNING: {message}")
        print("     --force specified — continuing.")
        return False
    compared = (f"; decodes identically to {os.path.basename(reference)}"
                if reference is not None else "")
    print(f"  [priming check] {label}: {os.path.basename(media_file)} discards "
          f"{found} leading samples as encoded{compared}.")
    return True


def missing_audio_priming_error(audio_file, run_dir):
    """Why a saved extraction without a recorded encoder delay cannot be used."""
    return RuntimeError(
        f"The saved source audio does not record the encoder delay it must "
        f"discard: {audio_file}\n"
        f"Restore its complete source measurements before resuming. If the "
        f"audio itself lacks that delay, diagnose and repair it explicitly "
        f"(leave out --apply to preview):\n"
        f"  venv/bin/python pipeline_tools/repair_aac_priming.py --apply "
        f"--run-audio {shlex.quote(run_dir)}")


def probe_stream_bounds(media_file, stream):
    """Earliest packet timestamp and latest packet timestamp + its duration.

    Absolute positions on the file's own timeline, so two streams can be
    compared against each other rather than each against itself.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream,
         "-show_entries", "packet=pts_time,duration_time",
         "-of", "csv=p=0", media_file],
        capture_output=True, text=True)
    # A probe that failed is not a probe that found nothing. Skipping malformed
    # rows and returning whatever parsed meant one good row followed by "N/A,N/A"
    # produced confident-looking bounds from a failed probe -- which then read as
    # a measured position rather than an unmeasurable one, so the caller's
    # unmeasurable-position failure never fired.
    if probe.returncode != 0:
        return None
    start = end = None
    for line in probe.stdout.splitlines():
        if not line.strip():
            continue          # ffprobe ends its CSV with a blank line
        parts = line.split(",")
        # Exactly two fields: a row carrying more than pts and duration is not
        # the output this parses, and reading its first two values would be a
        # guess about what the rest meant.
        if len(parts) != 2:
            return None
        try:
            pts, dur = float(parts[0]), float(parts[1])
        except ValueError:
            return None       # N/A, or anything else non-numeric
        # float() accepts "nan" and "inf", which the shell helper's numeric
        # regex rejects. A nan start is the dangerous one: every comparison
        # against it is False, so an unmeasurable position would pass the
        # offset check rather than fail it.
        if not (math.isfinite(pts) and math.isfinite(dur)):
            return None
        if start is None or pts < start:
            start = pts
        if end is None or pts + dur > end:
            end = pts + dur
    if start is None or end is None:
        return None
    return start, end


def verify_final_av_sync(final_video, fps, *, encoder_delay, force=False):
    """Fail unless the delivered file's audio and picture describe one timeline.

    The other checks establish that the pictures survived and that the audio
    survived. Neither compares the two against each other, so a file whose
    video gained frames its audio did not -- the leading-duplicate defect that
    shifted a whole batch of enhanced files -- passes both and is only found by
    watching it.

    Two independent comparisons, because either alone can be fooled:

    LENGTH -- pictures divided by frame rate against the audio stream's own
    duration, so no container-level value enters the comparison. This catches
    audio that ran short or long.

    POSITION -- where the programme audio begins against the first picture.
    Length alone is blind to a constant offset: audio shifted half a second
    keeps exactly the duration it had. The audio's first packet is not where
    the programme begins: AAC's packets start with the encoder's start-up
    block, which the container is meant to discard. So the audio is placed at
    its first packet plus encoder_delay, the delay in samples recorded when it
    was extracted -- where its first programme sample falls. A container that
    does not discard the delay then shows as audio starting that much late,
    and one discarding more than the delay as audio starting early.

    Both use the pipeline's one-picture standard, and the lead side never gets
    more: see AV_SYNC_LEAD_FRAMES. The length check's lag side alone adds one
    codec block, which AAC pads onto the end of the last one. Whether the
    container discards the delay, and decodes to what was extracted, is
    verify_audio_priming()'s question, asked of the delivered file first.

    Both must pass. An unmeasurable position -- including an unknown encoder
    delay -- is a failure, not a note: the length check cannot see an offset.
    Under --force each failure is reported and the run continues, but the
    success line is then not printed, because it would not be true.
    """
    print("  > Verifying audio and picture describe the same timeline...")
    video = probe_video_timing(final_video)
    audio_seconds = measure_audio_seconds(final_video)
    if audio_seconds is None:
        print("    No audio stream to compare.")
        return
    picture_seconds = video["frames"] / fps
    delta = audio_seconds - picture_seconds        # positive: audio runs longer

    quantum = audio_quantum_seconds(final_video)
    lag_limit = frames_to_seconds(AV_SYNC_LAG_FRAMES, fps) + quantum
    lead_limit = frames_to_seconds(AV_SYNC_LEAD_FRAMES, fps)
    direction = "audio behind picture" if delta >= 0 else "AUDIO AHEAD OF PICTURE"
    limit = lag_limit if delta >= 0 else lead_limit

    print(f"    picture {picture_seconds:.4f}s ({video['frames']} frames)  "
          f"audio {audio_seconds:.4f}s  delta {delta*1000:+.1f}ms  "
          f"({direction}, limit {limit*1000:.1f}ms)")

    def fail(message):
        """Raise, or under --force report and mark this check failed."""
        if not force:
            raise RuntimeError(message)
        print(f"  ⚠️  WARNING: {message}")
        print("     --force specified — continuing.")
        return False

    # Where the streams begin, not just how long they run.
    position_ok = True
    v_bounds = probe_stream_bounds(final_video, "v:0")
    a_bounds = probe_stream_bounds(final_video, "a:0")
    if not (v_bounds and a_bounds):
        missing = [name for name, bounds
                   in (("picture", v_bounds), ("audio", a_bounds)) if not bounds]
        unreadable = " and ".join(missing)
        verb = "stream begins" if len(missing) == 1 else "streams begin"
        position_ok = fail(
            f"Could not measure where the {unreadable} {verb} in "
            f"{final_video}, so audio and picture cannot be placed against "
            f"each other. Only their lengths were compared, which a constant "
            f"offset does not change."
        )
    else:
        rate = audio_sample_rate(final_video)
        if encoder_delay is None or rate is None:
            unknown = "encoder delay" if encoder_delay is None else "sample rate"
            position_ok = fail(
                f"The {unknown} of the audio in {final_video} is not known, so "
                f"where its programme audio begins cannot be placed against the "
                f"picture. Only the lengths were compared, which a constant "
                f"offset does not change."
            )
        else:
            audio_start = a_bounds[0] + encoder_delay / rate
            offset = audio_start - v_bounds[0]      # positive: audio starts later
            late = offset >= 0
            offset_limit = frames_to_seconds(
                AV_SYNC_LAG_FRAMES if late else AV_SYNC_LEAD_FRAMES, fps)
            where = "audio behind picture" if late else "AUDIO AHEAD OF PICTURE"
            print(f"    picture starts {v_bounds[0]:+.4f}s  audio starts "
                  f"{audio_start:+.4f}s (first packet {a_bounds[0]:+.4f}s, then a "
                  f"{encoder_delay}-sample encoder delay)  offset "
                  f"{offset*1000:+.1f}ms  ({where}, limit {offset_limit*1000:.1f}ms)")
            if abs(offset) > offset_limit:
                position_ok = fail(
                    f"Audio and picture do not start together in {final_video}: "
                    f"the programme audio begins {offset*1000:+.1f}ms from the "
                    f"picture ({where}, limit {offset_limit*1000:.1f}ms). The "
                    f"streams may each be the right length but sit at different "
                    f"places on the timeline."
                )

    length_ok = True
    if abs(delta) > limit:
        length_ok = fail(
            f"Audio and picture do not describe the same timeline: "
            f"{delta*1000:+.1f}ms ({direction}), limit {limit*1000:.1f}ms.\n"
            f"picture {picture_seconds:.4f}s from {video['frames']} frames at "
            f"{fps:.6f} fps, audio {audio_seconds:.4f}s."
        )

    if position_ok and length_ok:
        print("    Alignment OK.")


def verify_audio_video_duration(video_file, audio_file, tolerance_sec=None,
                                force=False):
    """
    Compares audio stream duration in the source video against audio stream
    duration in the extracted audio file, aborting if they differ beyond
    tolerance_sec.

    The abort limit is derived, not fixed: two blocks of the target audio codec
    plus one video frame. Those are the only quantities that can move a correct
    extraction -- a frame is the finest the picture can be aligned to, and the
    codec contributes up to two blocks, not one: it primes with a leading block
    AND pads the final partial block out to a whole one. A source whose sample
    count lands just past a block boundary needs almost all of both. Anything
    past their sum is lost or gained audio.

    A flat limit in seconds would be the wrong shape: two seconds is roughly
    60 frames at 29.97fps, and audio displaced that far is a ruined deliverable
    rather than a subtle defect for a duration check to find.

    Both sides use audio stream duration (not container format duration) so
    the comparison is apples-to-apples. AAC gives a precise stream-level
    duration on both sides. PCM in MKV does not -- ffprobe reports N/A and the
    comparison falls back to format duration on one side only, a systematic
    ~0.4s discrepancy between two different measurements. That is why
    prepare_video.sh encodes AAC by default.

    Beyond one block of the target codec the operator is warned and prompted,
    since the extraction has then lost or gained audio rather than merely
    re-quantised its length. Pass force=True (mirrors --force behaviour
    elsewhere in this pipeline) to skip the prompt and continue automatically.

    This is extraction fidelity only. Whether the audio and the picture agree
    is a different question, answered by verify_final_av_sync() on the
    delivered file.

    The DV capture chain should never produce a perceptible drift in normal
    operation, so a warning at this level almost always indicates a real
    problem worth investigating before committing multiple hours of GPU time
    to the upscale.
    """
    print("  > Verifying audio/video duration match...")
    try:
        quantum = audio_quantum_seconds(audio_file)
        try:
            # get_video_fps() returns a formatted string; every other caller
            # floats it.
            frame_sec = frames_to_seconds(1, float(get_video_fps(video_file)))
        except (RuntimeError, ValueError, TypeError, ZeroDivisionError,
                subprocess.CalledProcessError):
            # A source whose frame rate cannot be read still gets a bounded
            # check rather than an unbounded one.
            frame_sec = frames_to_seconds(1, 29.97)
        extraction_limit = quantum + AUDIO_EXTRACTION_SLACK_SEC
        if tolerance_sec is None:
            tolerance_sec = 2 * quantum + frame_sec

        def get_audio_stream_duration(filepath):
            """
            Returns audio stream duration from filepath, falling back to
            container format duration if stream-level duration is unavailable
            (e.g. PCM-in-MKV where ffprobe reports N/A at stream level).
            """
            cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-show_format", "-select_streams", "a:0",
                filepath
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            probe = json.loads(result.stdout)
            streams = probe.get("streams", [])
            stream_duration = streams[0].get("duration") if streams else None
            format_duration = probe.get("format", {}).get("duration")
            raw = stream_duration or format_duration
            if raw is None:
                raise RuntimeError(
                    f"ffprobe could not determine audio duration from '{filepath}'"
                )
            return float(raw)

        video_audio_duration = get_audio_stream_duration(video_file)
        extracted_audio_duration = get_audio_stream_duration(audio_file)

        diff = abs(video_audio_duration - extracted_audio_duration)
        print(f"    Source audio: {video_audio_duration:.3f}s  |  "
              f"Extracted audio: {extracted_audio_duration:.3f}s  |  "
              f"Diff: {diff*1000:.1f}ms  "
              f"(codec block {quantum*1000:.1f}ms, "
              f"warn beyond {extraction_limit*1000:.1f}ms, "
              f"abort beyond {tolerance_sec*1000:.1f}ms)")

        if diff > tolerance_sec:
            raise RuntimeError(
                f"Audio duration mismatch between source and extracted file: "
                f"source={video_audio_duration:.3f}s, "
                f"extracted={extracted_audio_duration:.3f}s, "
                f"diff={diff:.3f}s exceeds the {tolerance_sec*1000:.1f}ms "
                f"limit (two {quantum*1000:.1f}ms codec blocks plus one "
                f"{frame_sec*1000:.1f}ms frame).\n"
                f"Delete the audio file and re-run to force re-extraction:\n"
                f"  rm '{audio_file}'"
            )

        # One block of the target codec is what a re-encode changes in the
        # ordinary case -- AAC pads its final 1024-sample block, so re-encoded
        # audio comes out marginally longer. Warning here rather than at the
        # abort limit keeps that early signal: a second block is legitimate
        # (the encoder also primes with a leading block) but only when the
        # source lands near a block boundary, so it is worth showing the
        # operator rather than passing silently.
        #
        # Derived from the codec's own block rather than set as a flat figure in
        # milliseconds: a constant spends an unknown part of its budget on
        # padding before any real drift exists, so the headroom it reports is
        # not the headroom it has.
        if diff > extraction_limit:
            print(f"\n  ⚠️  WARNING: extracted audio differs from the source by "
                  f"{diff*1000:.0f}ms.")
            print(f"     Source audio: {video_audio_duration:.3f}s  |  "
                  f"Extracted: {extracted_audio_duration:.3f}s")
            print(f"     A re-encode adds one {quantum*1000:.0f}ms codec block in the ordinary")
            print("     case, and a second when the source ends near a block boundary;")
            print(f"     beyond {tolerance_sec*1000:.0f}ms the extraction has lost or gained audio.")
            if force:
                print("     --force specified — continuing despite the mismatch.")
            else:
                if not ask_yes_no("  Continue anyway? (y/n): "):
                    raise RuntimeError(
                        f"Aborted by user: extracted audio differs from source by "
                        f"{diff*1000:.0f}ms (limit {extraction_limit*1000:.0f}ms)."
                    )

        print("    Duration check passed.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe failed during duration check: {e.stderr}")


def is_valid_video(filepath):
    """
    Probes a video file with ffprobe to confirm it has a readable video stream.
    Catches corrupt files (e.g. missing moov atom) that have non-zero size but
    are unplayable due to an interrupted write.
    """
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.returncode == 0 and result.stdout.strip() == "video"
    except Exception:
        return False


def get_video_fps(video_file):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_file
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    fps_str = result.stdout.strip()

    if not fps_str or fps_str == "0/0":
        print("Warning: Could not detect r_frame_rate, falling back to avg_frame_rate.")
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_file
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        fps_str = result.stdout.strip()

    if not fps_str or fps_str == "0/0":
        raise RuntimeError(f"Could not detect FPS for {video_file}")

    if "/" in fps_str:
        try:
            num, den = map(float, fps_str.split('/'))
            if den == 0:
                raise ValueError("Denominator is zero")
            fps_val = num / den
            return f"{fps_val:.3f}"
        except Exception as e:
            raise RuntimeError(f"Could not parse FPS fraction '{fps_str}'. Error: {e}")
    else:
        return fps_str


def get_video_dimensions(video_file):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        video_file
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    try:
        w, h = map(int, result.stdout.strip().split('x'))
        return w, h
    except Exception as e:
        raise RuntimeError(f"Could not parse video dimensions. Output: '{result.stdout}'. Error: {e}")


def check_disk_space(path, required_gb):
    total, used, free = shutil.disk_usage(path)
    free_gb = free / (1024**3)
    print(f"Disk Check: {free_gb:.2f} GB free at {path}")
    if free_gb < required_gb:
        raise RuntimeError(
            f"Insufficient disk space! Need {required_gb} GB, "
            f"but only {free_gb:.2f} GB is available at {path}."
        )
    return free_gb


def cleanup_intermediate_files(input_chunk, esrgan_file, rife_in_dir, rife_out_dir):
    """Cleans up all temporary files for a chunk."""
    if os.path.exists(input_chunk):
        print(f"  > Cleaning up input chunk: {input_chunk}")
        os.remove(input_chunk)
    if os.path.exists(esrgan_file):
        print(f"  > Cleaning up ESRGAN file: {esrgan_file}")
        os.remove(esrgan_file)

    # Conditional on existence (matching the checks above) rather than always
    # printing: with RIFE off these directories are never created, and an
    # unconditional "Cleaning up RIFE ... frames" message here would be
    # actively misleading on a run that never used RIFE at all.
    if os.path.isdir(rife_in_dir):
        print(f"  > Cleaning up interpolation input frames: {rife_in_dir}")
        safe_rmtree(rife_in_dir)

    if os.path.isdir(rife_out_dir):
        print(f"  > Cleaning up interpolation output frames: {rife_out_dir}")
        safe_rmtree(rife_out_dir)


def autotune_chunk_size(input_video_path, scale_factor):
    """Calculates optimal chunk size based on free disk and video properties."""
    print("--- Auto-Tuning Chunk Size ---")
    try:
        # PROCESSING_DIR, not ".": chunk intermediates are written there, and
        # with --run-dir it can be on a different filesystem than the
        # repo/cwd -- checking "." would inspect the wrong disk entirely.
        free_disk_gb = check_disk_space(PROCESSING_DIR, 10)
        in_width, in_height = get_video_dimensions(input_video_path)
        out_width = in_width * scale_factor
        out_height = in_height * scale_factor
        print(f"Input res: {in_width}x{in_height}, Upscaled res: {out_width}x{out_height}")

        fps = float(get_video_fps(input_video_path))

        bytes_per_pixel = 3
        pixels = out_width * out_height
        est_png_mb = (pixels * bytes_per_pixel * EST_PNG_COMP_RATIO) / (1024**2)
        print(f"Estimated PNG frame size: {est_png_mb:.2f} MB")

        # Peak disk usage occurs when RIFE input and output frame dirs coexist on disk
        # simultaneously: 2x the source frame count (1x input + 1x output frames).
        # With RIFE off this is a safe overestimate; with a 60fps mode=1 master fps
        # is already ~60 so *2 remains a reasonable worst-case. The conservative
        # overestimate is intentional — disk space is the hard constraint here.
        pngs_per_sec = fps * 2
        mb_per_sec = pngs_per_sec * est_png_mb
        print(f"Estimated temp disk usage per sec: {mb_per_sec:.2f} MB/s")

        usable_temp_gb = free_disk_gb * DISK_SAFETY_MARGIN
        print(f"Reserving {usable_temp_gb:.2f} GB (50%) for temp files.")

        max_chunk_sec_disk = (usable_temp_gb * 1024) / mb_per_sec
        print(f"Disk capacity allows for {max_chunk_sec_disk:.0f} second chunks.")

        if max_chunk_sec_disk < MIN_CHUNK_SEC:
            # Not enough disk even for the minimum chunk size on a fresh run.
            # Abort here rather than picking a smaller size — on a resumed run
            # this path is never reached (autotune is skipped entirely).
            needed_gb = (MIN_CHUNK_SEC * mb_per_sec) / (DISK_SAFETY_MARGIN * 1024)
            print("\n[!] ERROR: Insufficient disk space to start a new run.")
            print(f"    Minimum chunk size ({MIN_CHUNK_SEC}s) requires ~{needed_gb:.1f} GB free.")
            print(f"    Available: {free_disk_gb:.2f} GB free at current path.")
            print(f"    Free up at least {needed_gb - free_disk_gb:.1f} GB and retry.")
            raise RuntimeError("Insufficient disk space for minimum chunk size.")

        final_chunk_sec = max(MIN_CHUNK_SEC, min(max_chunk_sec_disk, MAX_CHUNK_SEC))

        print(f"Clamped to {final_chunk_sec:.0f} seconds (Min: {MIN_CHUNK_SEC}s, Max: {MAX_CHUNK_SEC}s)")
        print("--------------------------------")
        return int(final_chunk_sec)

    except RuntimeError:
        raise  # propagate disk space errors directly — do not swallow
    except Exception as e:
        print(f"WARNING: Auto-tuning failed ({e}). Falling back to safe 10s chunks.")
        print("--------------------------------")
        return 10


def parse_arguments(argv=None):
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Upscale and interpolate legacy video files (VHS, Hi8, DV camcorder, etc.).",
        epilog="""
Examples:
  %(prog)s video.avi                          # 2x upscale, balanced profile, distribution CRF 23 (all defaults)
  %(prog)s video.avi --crf 18                # deliverable at a different quality
  %(prog)s video.avi --scale 4               # 4x upscale, balanced profile
  %(prog)s video.avi -s 4 --yes              # 4x upscale, accept the plan
  %(prog)s video.avi --profile aggressive    # heavy denoise for noisy/composite sources
  %(prog)s video.avi --profile halo          # halo/ringing suppression
  %(prog)s video.avi --profile dv            # optimised for MiniDV/Digital8/DV AVI sources
  %(prog)s video.avi --profile hi8dv         # Hi8 tape via Digital8/FireWire direct capture
  %(prog)s video_60fps.mp4                   # 60fps mode=1 master: upscale only, 60fps out (default)
  %(prog)s video_30fps.mp4 --rife            # 30fps mode=0 master: RIFE doubles to ~60fps out
  %(prog)s camcorder.mp4                    # works with DV, Hi8, and other camcorder formats
  %(prog)s video.avi --model realesr-general-x4v3   # use general degradation model at 2x output
  %(prog)s video.avi -s 4 --model realesr-general-x4v3  # general model at 4x output

ESRGAN models (--model):
  RealESRGAN_x2plus        Default for --scale 2. Trained for 2x output in one step.
  RealESRGAN_x4plus        Default for --scale 4. More parameters, strong 4x detail.
  realesr-general-x4v3     General degradation model (noise, blur, compression artifacts).
                           Internally always 4x; downsamples to 2x when --scale 2 is used.
                           The downsampling acts as a natural anti-aliasing pass, which can
                           suit noisy VHS/Hi8 sources better than RealESRGAN_x2plus.

Pre-filter profiles (--profile):
  balanced   Default. DVD MPEG-2 and captures not made via DV. hqdn3d=2:2:6:6,     pp=fd, unsharp=3:3:0.2
  aggressive Heavy noise, composite captures, low-bitrate DVD. hqdn3d=3:3:6:6,     pp=ac, unsharp=3:3:0.6
  halo       White ghost lines / ringing around dark edges.    hqdn3d=4:4:8:8,     pp=fd, unsharp=3:3:0.2
  dv         MiniDV / Digital8 / DV AVI sources.               hqdn3d=1.5:1.5:4:4, pp=ac, unsharp=3:3:0.25
  hi8dv      Hi8 analog tape via DCR-TRV330 FireWire capture.  hqdn3d=2:2:5:5,     pp=ac, unsharp=3:3:0.2
  vhsdv      VHS via TRV330 S-Video passthrough.               hqdn3d=2.5:3:5:5,   pp=ac, unsharp=3:3:0.2
  vhsdv_nosharp    As vhsdv, no pre-sharpening. For clean S-Video captures where ESRGAN
                   sharpening alone suffices.                   hqdn3d=2.5:3:5:5,   pp=ac
  vhsdv_lightdenoise  As vhsdv_nosharp, lighter luma spatial denoise (1.5 vs 2.5).
                   Preserves face/skin texture for ESRGAN. Use when faces look waxy
                   or pore-free after upscaling.               hqdn3d=1.5:3:5:5,   pp=ac
  vhsdv_composite  VHS via TRV330 composite passthrough.       hqdn3d=2.5:3.5:5:5, pp=ac, unsharp=3:3:0.2
""",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--resume", action="store_true",
                        help="Resume current_run with its saved input and settings; --run-dir selects another run.")
    parser.add_argument("--verify-full-decode", action="store_true",
                        help=("Also decode every final video frame (slow). Default: check "
                              "MP4 presentation boundaries without decoding the whole video."))
    parser.add_argument("input_video", nargs="?", help="Path to the input AVI/MP4/MKV video file.")
    parser.add_argument(
        "-s", "--scale",
        type=int,
        choices=[2, 4],
        default=2,
        help=("Upscale factor: 2 or 4. Default: 2. Controls output resolution. Default model per scale: "
              "2=RealESRGAN_x2plus, 4=RealESRGAN_x4plus. Override with --model.")
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="Number of FFmpeg threads (default: half of logical CPU cores, minimum 4)"
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force deletion of old processing chunks without prompting."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Accept the pre-flight plan without prompting. Unlike --force, "
            "this never authorizes deletion of incompatible resume state or "
            "continuation after a failed validation."
        )
    )
    parser.add_argument(
        "--profile",
        choices=["balanced", "aggressive", "halo", "dv", "hi8dv", "vhsdv",
                 "vhsdv_nosharp", "vhsdv_lightdenoise", "vhsdv_composite"],
        default="balanced",
        help=(
            "Pre-filter profile (default: balanced). "
            "balanced:   MPEG-2 DVD sources (~4.6Mbps). Light denoise, temporal 6:6 stability, "
            "fast deblock, minimal sharpen. "
            "aggressive: Heavy noise, composite captures, or low-bitrate DVD. Stronger denoise, "
            "full deblock+dering, more sharpen. "
            "halo:       White ghost lines around dark edges. Heavy denoise, fast deblock, minimal sharpen. "
            "dv:         MiniDV/Digital8/DV AVI. Lighter denoise, stronger deblock+dering, gentle sharpen. "
            "hi8dv:      Hi8 analog tape via DCR-TRV330 FireWire capture. Moderate spatial denoise, "
            "full deblock+dering, lighter temporal than balanced."
        )
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL_NAME",
        help=(
            "Override the default ESRGAN model (default: RealESRGAN_x2plus for --scale 2, "
            "RealESRGAN_x4plus for --scale 4). "
            "Example: --model realesr-general-x4v3. "
            "The model name must match the .pth filename in the weights/ directory (without extension). "
            "Note: realesr-general-x4v3 is always a 4x model internally; when used with --scale 2 "
            "it upscales to 4x then downsamples to 2x, which can reduce over-hallucination on noisy sources."
        )
    )
    parser.add_argument(
        "--max-runtime",
        type=float,
        default=None,
        metavar="HOURS",
        help=("Maximum runtime in hours before graceful shutdown. Script will not start a new chunk "
              "if (elapsed time + last chunk duration) would exceed this limit. Example: --max-runtime 8")
    )
    parser.add_argument(
        "--rife",
        dest="no_rife",
        action="store_false",
        default=True,
        help=(
            "Enable RIFE frame interpolation (disabled by default). Doubles output FPS. "
            "Intended for ~30fps (bwdif mode=0) masters where RIFE interpolates between "
            "frames to produce ~60fps output. Do NOT use on ~60fps (bwdif mode=1) masters — "
            "that would produce ~120fps output. The pipeline will abort if --rife is passed "
            "with a >45fps input."
        )
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=DEFAULT_CRF,
        metavar="CRF",
        help=(
            f"Quality of the final deliverable (default: {DEFAULT_CRF}). Lower is "
            "larger and closer to the input. Applies to whichever stage writes the "
            "deliverable -- the ESRGAN writer (by default) or the "
            "RIFE-encode stage (with --rife) -- so the two paths never disagree on the "
            "quality target. The pre-filter intermediate is unaffected and always "
            "stays yuv444p/CRF 12: it is deleted after processing and never reaches "
            "a player. Example: --crf 18."
        )
    )
    parser.add_argument(
        "--keep-input",
        action="store_true",
        help=(
            "Never delete the input. By default a verified prepared master in this "
            "run's work/ or a recorded preparation run's work/masters/ is "
            "removed once the audio and source "
            "state are durable and either its complete split or validated "
            "whole-file pre-filter can resume the job. Anything that is not demonstrably such "
            "a master is kept regardless."
        )
    )
    parser.add_argument(
        "--allow-unprepared-input",
        action="store_true",
        help=(
            "Process an input that is a raw capture rather than a prepared "
            "master. Refused by default: Real-ESRGAN upscales frames, not "
            "fields, so an interlaced capture comes out with its combing "
            "amplified and nothing in the log to say so. MPEG-2 is refused "
            "even with this option; prepare it with prepare_video.sh."
        )
    )
    parser.add_argument(
        "--chunk-duration",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Seconds of video per chunk, overriding the size auto-tuned from "
            "free disk and video properties. Mainly useful for exercising the "
            "chunked path on a short file: with a small value and a lowered "
            "--chunk-threshold, a few seconds of video produces several chunks "
            "and the whole split, process and concat sequence runs in a minute "
            "instead of hours."
        )
    )
    parser.add_argument(
        "--chunk-threshold",
        type=int,
        default=NO_CHUNK_THRESHOLD_SEC,
        metavar="SECONDS",
        help=(
            "Videos shorter than this bypass chunking entirely (default: "
            f"{NO_CHUNK_THRESHOLD_SEC}). Lower it to force a short video down "
            "the chunked path."
        )
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        metavar="DIR",
        help=(
            "The run directory every file this invocation writes goes into: the "
            "final video to videos/, this source's logs to logs/<stem>/, and the "
            "chunk scaffold to work/, which is deleted when the run completes. "
            "A pipeline driver passes the run it already created. Standalone, a "
            "run directory is created under ~/VideoPreservation/ai/, named for "
            "the time and the source. See pipeline_docs/storage-layout.md."
        )
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        metavar="DIR",
        help=(
            "Directory for this invocation's pipeline.log and metadata.json "
            "(default: logs/<input stem>/ inside the run directory). A driver "
            "passes the capture's own log directory, since the input it hands "
            "this pipeline is a prepared master named after neither."
        )
    )
    parser.add_argument(
        "--esrgan-eta",
        default=None,
        metavar="DESC",
        help=(
            "Optional ETA description string to display on the Real-ESRGAN banner line, "
            "e.g. 'est. 12m 01s | day done: ~28m 41s | batch: ~249h'. "
            "Set by the batch wrapper so the estimate is visible in the pipeline log "
            "alongside the No-Chunk 2 banner line."
        )
    )
    command = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(command)
    if args.resume:
        return restore_resume_arguments(parser, args, command)
    if not args.input_video:
        parser.error("input_video is required unless --resume is used")
    return args


def main(args):
    """Main processing pipeline."""
    # --- 0. Pre-Flight Checks ---
    # Record wall-clock time at the very top of main() so we can measure how
    # long Python/venv/import startup takes before any real work begins.
    _t_main_entry = time.time()
    check_venv()

    run_dir = args.run_dir
    # Ownership comes from the manifest's entry point. A batch or comparison
    # driver owns its multi-source run; this pipeline owns standalone and
    # preparation runs, including their completion and readable summary.
    owns_run = True
    if run_dir:
        try:
            with open(os.path.join(run_dir, "run.json")) as handle:
                # prepare_video.sh creates the run and records provenance but
                # does not finish it, so it leaves ownership open. A driver
                # (upscale_dv_batch.sh, run_test_comparisons.py) names itself
                # and keeps ownership, because it alone knows when a run
                # spanning many sources is complete.
                entry = json.load(handle).get("entry_point")
                owns_run = entry in (None, "", "prepare_video.sh",
                                     os.path.basename(__file__))
        except (OSError, json.JSONDecodeError):
            owns_run = True
    if not run_dir:
        if not os.path.exists(args.input_video):
            print(f"Error: Input video not found at: {args.input_video}")
            sys.exit(1)
        run_dir = os.path.join(RUN_ROOT, make_run_id(source_stem(args.input_video)))
    set_run_paths(run_dir, args.input_video, args.log_dir)
    # Logging starts as soon as the run's log path is known, so a refused
    # input or an unusable resume state is recorded in pipeline.log. Only the
    # venv check and the missing-input error come earlier, before any run
    # directory exists to log into.
    # Both stdout AND stderr are redirected: the interpreter writes an
    # unhandled exception traceback to stderr, and redirecting only stdout
    # leaves it visible on a live terminal but absent from pipeline.log.
    # Both share the SAME TeeLogger instance (and therefore
    # the same single open file handle) rather than two separate instances
    # each independently opening the log path -- confirmed by direct test
    # that two independent handles on the same append-mode file produce
    # garbled/duplicated output due to separate buffering, which would be
    # exactly the wrong failure mode for the one situation (a crash) where
    # the log matters most.
    _tee = TeeLogger(LOG_FILE)
    sys.stdout = _tee
    sys.stderr = _tee
    print(
        f"\n--- Pipeline started {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} "
        f"| Logging to {LOG_FILE} ---\n")
    input_exists = os.path.exists(args.input_video)
    saved_source_measurements = load_source_measurements(
        PROCESSING_DIR, expected_input=args.input_video,
        require_resume_state=not input_exists)
    if input_exists:
        check_input_is_prepared(args.input_video, args.allow_unprepared_input)
    else:
        print("[INFO] Prepared master has already been released; using the "
              "saved source measurements and resumable video state.")
    global FINAL_INPUT_VIDEO, KEEP_INPUT
    FINAL_INPUT_VIDEO, KEEP_INPUT = args.input_video, args.keep_input
    if owns_run:
        write_run_manifest(
            run_dir,
            run_class_for_profile(args.profile),
            {
                # --rife is stored as dest="no_rife" with store_false, and
                # --model defaults to None meaning "the default for this
                # scale". The manifest records what the run actually used, so
                # both are resolved here rather than copied raw: a reader of
                # run.md must not have to know this script's argument spelling.
                "scale": args.scale,
                "model": args.model or DEFAULT_MODELS.get(args.scale, ""),
                "profile": args.profile,
                "rife": not args.no_rife,
                "crf": args.crf,
            },
            [args.input_video],
        )

    if owns_run:
        save_resume_arguments(args, run_dir)
        set_current_run(run_dir)

    # Capture invocation command for resume messages (shell-safe). The run
    # is named explicitly: current_run moves to whichever run started last,
    # so a bare --resume could pick up a different run than this one.
    RECONSTRUCTED_CMD = os.environ.get("PIPELINE_RESUME_COMMAND") or \
        (f"{shlex.quote(os.path.abspath(sys.argv[0]))} --resume --run-dir {shlex.quote(os.path.realpath(run_dir))}"
         if owns_run else " ".join(shlex.quote(arg) for arg in sys.argv))
    global _resume_cmd
    _resume_cmd = RECONSTRUCTED_CMD

    print("[INFO] Resume command:")
    print(f"  {RECONSTRUCTED_CMD}\n")
    # Startup timing: measures Python interpreter + venv import overhead.
    # Logged here (after TeeLogger) so it appears in pipeline.log as well.
    print(f"[Perf] Python/venv startup: {time.time() - _t_main_entry:.2f}s "
          f"(from process launch to first line of main())")

    # --- 1. Set up variables based on args ---
    INPUT_VIDEO = args.input_video
    SCALE_FACTOR = args.scale
    # Detect extension to support both .avi and .mp4
    INPUT_EXT = os.path.splitext(INPUT_VIDEO)[1]

    # Sanity bound only -- libx264 and check_output_crf below are the actual
    # authorities on a valid CRF; this just catches an obvious typo (e.g. an
    # extra digit) before any work starts. Without it, a value like
    # --crf 40 sails straight through: check_output_crf only
    # compares the actual encode against whatever was requested, so a
    # mistyped, far-too-lossy CRF would be reported as "confirmed as
    # expected" rather than caught. Mirrors upscale_dv_batch.sh's identical
    # 10-30 bound on the same flag, so direct invocation and the batch
    # wrapper agree on what counts as a plausible CRF.
    if not 10 <= args.crf <= 30:
        print(f"Error: --crf must be an integer 10-30 (got: {args.crf}).",
              file=sys.stderr)
        sys.exit(1)

    # Set by set_run_paths() above from the run directory.
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # --- Pre-filter Profile Selection ---
    # Selected via --profile. Default: balanced.
    # Filter chains are defined in PROFILES_30FPS; PROFILES_60FPS holds variants
    # with halved temporal hqdn3d values for ~60fps (bwdif mode=1) masters.
    #
    # At 60fps the inter-frame delta per field is half that of 30fps, so the same
    # temporal strength over-smooths relative to actual motion. The temporal
    # parameters (the last two values of hqdn3d=luma_sp:chroma_sp:luma_tmp:chroma_tmp)
    # are approximately halved in the 60fps variants. Spatial and sharpening values
    # are identical between the two sets — only temporal smoothing changes.
    # The correct set is selected automatically based on source_fps_float below.
    #
    # 30fps profiles (bwdif mode=0 masters, or any source <= 45fps):
    #
    #   balanced   hqdn3d=2:2:6:6, pp=fd, unsharp=3:3:0.2
    #     Light spatial denoise preserves texture for ESRGAN. Temporal at 6:6 stabilises
    #     frame-to-frame flicker without touching real detail. pp=fd handles DVD
    #     macroblocking. Minimal sharpening avoids pre-upscale halos.
    #     Tuned for MPEG-2 DVD sources (~4.6Mbps). Use hi8dv for higher-bitrate
    #     MPEG-2 sources where pp=ac dering is beneficial.
    #
    #   aggressive hqdn3d=3:3:6:6, pp=ac, unsharp=3:3:0.6
    #     Stronger denoise and sharpening for heavy noise or composite-captured sources.
    #     pp=ac full deblock+dering suited to low-bitrate DVD or VHS via composite.
    #
    #   halo       hqdn3d=4:4:8:8, pp=fd, unsharp=3:3:0.2
    #     Heavy denoise suppresses source ringing that ESRGAN would otherwise amplify.
    #     Use only if you see white ghost lines around dark edges in output.
    #
    #   dv         hqdn3d=1.5:1.5:4:4, pp=ac, unsharp=3:3:0.25
    #     DV compression produces DCT block noise and mosquito ringing rather than
    #     analog grain. Lighter spatial denoise preserves genuine DV detail; pp=ac
    #     targets block/ringing artifacts; moderate temporal smoothing handles
    #     low-light flicker without over-filtering clean digital footage.
    #
    #   hi8dv      hqdn3d=2:2:5:5, pp=ac, unsharp=3:3:0.2
    #     For Hi8 analog tapes captured via Sony DCR-TRV330 over FireWire as a
    #     DV stream (analog passthrough capture). Also suitable for higher-bitrate
    #     MPEG-2 sources (>~6Mbps) where pp=ac dering is preferable to pp=fd.
    #     Spatial denoise matches balanced (2:2); temporal at 5:5 (lighter than
    #     balanced's 6:6) since high-bitrate sources have less inter-frame
    #     compression flicker to suppress. pp=ac provides full deblock+dering.
    #
    #   vhsdv      hqdn3d=2.5:3:5:5, pp=ac, unsharp=3:3:0.2
    #     For VHS tape via TRV330 passthrough using S-Video input. Slightly
    #     stronger chroma spatial denoise (3 vs 2) vs hi8dv to address VHS
    #     color-under chroma noise. Same temporal values as hi8dv.
    #
    #   vhsdv_nosharp    hqdn3d=2.5:3:5:5, pp=ac   (no unsharp)
    #     Same as vhsdv but with pre-sharpening removed entirely. Use when
    #     RealESRGAN's own sharpening is sufficient and unsharp is producing
    #     a cartoonish or plastic look on faces.
    #
    #   vhsdv_lightdenoise  hqdn3d=1.5:3:5:5, pp=ac   (no unsharp, lighter luma spatial)
    #     As vhsdv_nosharp but luma spatial denoise reduced from 2.5 to 1.5.
    #     Preserves face and skin texture so ESRGAN has real detail to work
    #     with rather than hallucinating texture over a pre-smoothed surface.
    #     Chroma spatial stays at 3 to keep VHS color-under noise suppressed.
    #     Use when faces look waxy or pore-free after vhsdv_nosharp upscaling.
    #
    #   vhsdv_composite  hqdn3d=2.5:3.5:5:5, pp=ac, unsharp=3:3:0.2
    #     For VHS tape via TRV330 passthrough using composite input. Elevated
    #     chroma spatial (3.5) targets dot crawl and cross-colour artifacts
    #     introduced by comb filter luma/chroma separation.
    #
    # 60fps profiles (bwdif mode=1 masters, source > 45fps):
    #   Temporal values halved vs. 30fps counterparts. All other values identical.
    #
    #   balanced   hqdn3d=2:2:3:3, pp=fd, unsharp=3:3:0.2
    #   aggressive hqdn3d=3:3:3:3, pp=ac, unsharp=3:3:0.6
    #   halo       hqdn3d=4:4:4:4, pp=fd, unsharp=3:3:0.2
    #   dv         hqdn3d=1.5:1.5:2:2, pp=ac, unsharp=3:3:0.25
    #   hi8dv      hqdn3d=2:2:2.5:2.5, pp=ac, unsharp=3:3:0.2

    PROFILES_30FPS = {
        "balanced": "hqdn3d=2:2:6:6,pp=fd,unsharp=3:3:0.2",
        "aggressive": "hqdn3d=3:3:6:6,pp=ac,unsharp=3:3:0.6",
        "halo": "hqdn3d=4:4:8:8,pp=fd,unsharp=3:3:0.2",
        "dv": "hqdn3d=1.5:1.5:4:4,pp=ac,unsharp=3:3:0.25",
        "hi8dv": "hqdn3d=2:2:5:5,pp=ac,unsharp=3:3:0.2",
        # VHS via TRV330 passthrough (S-Video input).
        # Slightly stronger chroma spatial denoise (3 vs 2) vs hi8dv to
        # address VHS color-under chroma noise. Temporal values match hi8dv.
        "vhsdv": "hqdn3d=2.5:3:5:5,pp=ac,unsharp=3:3:0.2",
        # vhsdv without pre-sharpening. ESRGAN's internal sharpening is
        # sufficient for clean S-Video captures; unsharp stacked on top
        # produces cartoonish/plastic faces.
        "vhsdv_nosharp": "hqdn3d=2.5:3:5:5,pp=ac",
        # As vhsdv_nosharp but luma spatial denoise reduced from 2.5 to 1.5.
        # Preserves face/skin texture for ESRGAN to upscale rather than
        # hallucinate. Chroma spatial stays at 3 (VHS color-under noise).
        # Use when faces still look waxy/pore-free after vhsdv_nosharp.
        "vhsdv_lightdenoise": "hqdn3d=1.5:3:5:5,pp=ac",
        # VHS via TRV330 passthrough (composite input).
        # Elevated chroma spatial (3.5) targets dot crawl and cross-colour
        # artifacts introduced by comb filter luma/chroma separation.
        "vhsdv_composite": "hqdn3d=2.5:3.5:5:5,pp=ac,unsharp=3:3:0.2",
    }

    # Temporal hqdn3d values (luma_tmp:chroma_tmp) halved for 60fps field-rate
    # masters. At twice the frame rate, consecutive frames are closer in time,
    # so the same temporal strength would over-smooth genuine motion.
    PROFILES_60FPS = {
        "balanced": "hqdn3d=2:2:3:3,pp=fd,unsharp=3:3:0.2",
        "aggressive": "hqdn3d=3:3:3:3,pp=ac,unsharp=3:3:0.6",
        "halo": "hqdn3d=4:4:4:4,pp=fd,unsharp=3:3:0.2",
        "dv": "hqdn3d=1.5:1.5:2:2,pp=ac,unsharp=3:3:0.25",
        "hi8dv": "hqdn3d=2:2:2.5:2.5,pp=ac,unsharp=3:3:0.2",
        "vhsdv": "hqdn3d=2.5:3:2.5:2.5,pp=ac,unsharp=3:3:0.2",
        "vhsdv_nosharp": "hqdn3d=2.5:3:2.5:2.5,pp=ac",
        "vhsdv_lightdenoise": "hqdn3d=1.5:3:2.5:2.5,pp=ac",
        "vhsdv_composite": "hqdn3d=2.5:3.5:2.5:2.5,pp=ac,unsharp=3:3:0.2",
    }

    profile = args.profile

    # --- Distribution mode: derive the ACTIVE final-deliverable CRF/pix_fmt ---
    # Distribution is the DEFAULT mode (yuv420p, required for broad player
    # compatibility -- many hardware decoders and TVs do not support
    # yuv444p). With RIFE off the ESRGAN writer produces the deliverable
    # directly, so it takes --crf and yuv420p. With --rife the writer's file is
    # only an intermediate that is decoded to PNG; it stays CRF 16/yuv444p so
    # RIFE receives full-chroma input and the requested loss is applied once,
    # by the final RIFE encode. PREFILTER_CRF and its yuv444p pix_fmt are never
    # changed either way.
    #
    # One deliverable shape, yuv420p in MP4. The capture is the preservation
    # master; this output is a derived viewing copy, regenerable from it.
    #
    # Local names below (not reassigned module constants) so every later
    # reference at an encode/check call site is an explicit, deliberate choice
    # rather than a global mutation that could be missed by a future reader
    # scanning for ESRGAN_WRITER_CRF/RIFE_ENCODE_CRF/EXPECTED_PIX_FMT.
    active_esrgan_writer_crf = args.crf if args.no_rife else ESRGAN_WRITER_CRF
    active_esrgan_writer_pix_fmt = "yuv420p" if args.no_rife else EXPECTED_PIX_FMT
    active_rife_encode_crf = args.crf
    active_final_pix_fmt = "yuv420p"

    profile_descriptions = {
        "balanced": (
            "Light spatial denoise, temporal stability, fast deblock, minimal sharpen. "
            "Optimised for Hi8/S-Video capture -> DVD (~4.6Mbps source)."
        ),
        "aggressive": (
            "Strong denoise, full deblock+dering, more sharpen. "
            "Recommended for heavy noise, composite captures, or low-bitrate DVD sources."
        ),
        "halo": (
            "Heavy denoise, fast deblock, minimal sharpen. "
            "Use if you see white ghost lines around dark edges."
        ),
        "dv": (
            "Light spatial denoise, moderate temporal smoothing, full deblock+dering, gentle sharpen. "
            "Optimised for MiniDV / Digital8 / DV AVI sources."
        ),
        "hi8dv": (
            "Light spatial denoise, moderate temporal smoothing, full deblock+dering, minimal sharpen. "
            "Optimised for Hi8 tape via Digital8/FireWire direct DV capture."
        ),
        "vhsdv": (
            "Slightly stronger chroma spatial denoise vs hi8dv to address VHS color-under noise. "
            "For VHS via TRV330 passthrough using S-Video input."
        ),
        "vhsdv_nosharp": (
            "As vhsdv but with pre-sharpening removed. Use when ESRGAN's internal sharpening "
            "suffices and unsharp produces cartoonish/plastic faces on clean S-Video captures."
        ),
        "vhsdv_lightdenoise": (
            "As vhsdv_nosharp but luma spatial denoise reduced from 2.5 to 1.5. Preserves "
            "face/skin texture so ESRGAN upscales real detail rather than hallucinating texture "
            "over a pre-smoothed surface. Chroma spatial stays at 3 for VHS color-under noise."
        ),
        "vhsdv_composite": (
            "Elevated chroma spatial denoise targeting dot crawl and cross-colour from comb filter "
            "luma/chroma separation. For VHS via TRV330 passthrough using composite input."
        ),
    }

    # Calculate FFmpeg thread count
    if args.threads and args.threads > 0:
        threads = args.threads
    else:
        cpu_count = os.cpu_count() or 4
        threads = max(4, cpu_count // 2)
    print(f"[INFO] Using {threads} FFmpeg threads")

    if args.model:
        REALSRGAN_MODEL = args.model
        print(f"[INFO] ESRGAN model overridden via --model: {REALSRGAN_MODEL}")
    else:
        REALSRGAN_MODEL = DEFAULT_MODELS[SCALE_FACTOR]

    # Disable the general model's internal DNI blend. The pipeline applies its
    # source-specific filters externally; blending a denoised checkpoint would
    # apply another noise-removal stage outside those profile settings.
    DENOISE_STRENGTH_ARGS = []
    if REALSRGAN_MODEL == "realesr-general-x4v3":
        DENOISE_STRENGTH_ARGS = ["-dn", "1"]
        print("[INFO] realesr-general-x4v3 selected: forcing --denoise_strength 1 "
              "to disable inference_realesrgan_video.py's internal DNI denoise blend.")

    # Profile description is printed here, in the setup summary, so it appears
    # alongside scale/model/thread config. The resolved filter string (which
    # depends on source_fps_float) is printed later at deferred profile selection.
    print(f"   [Profile] {profile}: {profile_descriptions[profile]}")

    input_basename = os.path.splitext(os.path.basename(INPUT_VIDEO))[0]
    # Strip any fps/rife suffix that prepare_video.sh may have embedded in the
    # master filename (e.g. _60fps, _30fps). The pipeline appends its own
    # authoritative suffix later, so leaving these in would produce duplicates
    # like _60fps_..._60fps in the output filename.
    # The lambda preserves the trailing timestamp (_YYYYMMDD_HHMMSS) if present
    # while removing only the fps/rife token that precedes it.
    # prepare_video.sh marks its masters so they can be told apart from a
    # source. The marker belongs to the intermediate, not to the deliverable
    # made from it, so it is removed before the output name is composed --
    # otherwise the finished video would be named "..._master_from_prepare_
    # video_hi8dv_x2_..." and claim to be a master.
    input_basename = re.sub(r'_master_from_prepare_video$', '', input_basename)
    input_basename = re.sub(
        r'_(60fps_rife|60fps|30fps)(_\d{8}_\d{6})?$',
        lambda m: m.group(2) or '',
        input_basename
    )
    MODEL_SHORT = {"RealESRGAN_x2plus": "x2plus", "RealESRGAN_x4plus": "x4plus",
                   "realesr-general-x4v3": "gen-x4v3"}.get(REALSRGAN_MODEL, REALSRGAN_MODEL)
    # FINAL_VIDEO_FILE_BASE is the name without the fps/rife suffix, which is
    # appended later once source_fps_float and args.no_rife are both known.
    FINAL_VIDEO_FILE_BASE = os.path.join(OUTPUT_DIR, f"{input_basename}_{profile}_x{SCALE_FACTOR}_{MODEL_SHORT}")
    # always AAC; MP4 keeps its encoder delay
    ORIGINAL_AUDIO_FILE = os.path.join(PROCESSING_DIR, f"{input_basename}_original.m4a")

    print(f"--- Starting processing for: {INPUT_VIDEO} ---")
    print(f"Detected Input Extension: {INPUT_EXT}")
    print(f"Selected Scale Factor: {SCALE_FACTOR}x")
    print(f"Selected Model: {REALSRGAN_MODEL}")
    print("Final Output File: (determined after FPS detection)")

    # --- Check if processing_chunks contains data from a different video/scale ---
    metadata_file = os.path.join(PROCESSING_DIR, "metadata.json")
    current_metadata = {
        "input_video": os.path.abspath(INPUT_VIDEO),
        "scale_factor": SCALE_FACTOR,
        "profile": profile,
        "model": REALSRGAN_MODEL,
        # no_rife is included so that a resumed run cannot accidentally mix
        # RIFE-interpolated and non-interpolated chunks in the same output.
        "no_rife": args.no_rife,
        # crf is included for the same reason: resuming at a different --crf
        # after some chunks were already written would otherwise go undetected
        # and mix two quality targets in one concatenated output.
        "crf": args.crf,
    }

    # is_resume is set True only when metadata existed and matched exactly,
    # confirming we are continuing an interrupted run with completed chunks.
    is_resume = False
    old_metadata = None

    if os.path.exists(metadata_file):
        # Attempt to read and parse the metadata file.
        # Any failure (corrupt JSON, permission error, etc.) is treated as an
        # unknown state — we NEVER delete chunks silently. The user must confirm
        # or pass --force explicitly after being shown the error.
        metadata_read_error = None
        try:
            with open(metadata_file, 'r') as f:
                old_metadata = json.load(f)
        except json.JSONDecodeError as e:
            metadata_read_error = f"metadata.json is corrupt or contains invalid JSON: {e}"
        except Exception as e:
            metadata_read_error = f"Could not read metadata.json: {e}"

        if metadata_read_error:
            # Metadata is unreadable — could mean a partial write during a
            # previous crash. Completed chunk files may still be valid and
            # resumable. Never auto-delete; always ask.
            print(f"\n⚠️  WARNING: {metadata_read_error}")
            print(f"    Metadata file: {metadata_file}")
            print(f"    Processing chunks directory: {PROCESSING_DIR}")
            print("    There may be completed chunks worth preserving.")
            if not input_exists:
                raise RuntimeError(
                    "The prepared master has already been released, so this "
                    "processing state is the only resumable copy and will not "
                    "be deleted. Repair metadata.json or restore the master."
                )
            if args.force:
                print("Deleting processing_chunks... (--force specified)")
                safe_rmtree(PROCESSING_DIR)
            else:
                print("\nOptions:")
                print("  y = delete processing_chunks and start fresh")
                print("  n = exit so you can inspect the chunks manually")
                if ask_yes_no("Delete processing_chunks and start fresh? (y/n): "):
                    print("Deleting processing_chunks...")
                    safe_rmtree(PROCESSING_DIR)
                else:
                    print("Exiting. Inspect or repair metadata.json before restarting.")
                    print(f"  To repair: python3 -c \"import json; print(open(\'{metadata_file}\').read())\"")
                    print(f"  To reset:  rm -rf {PROCESSING_DIR}")
                    sys.exit(1)

        else:
            # Compare metadata excluding chunk_duration, which is added to
            # current_metadata only after autotune runs. Stored metadata may
            # contain it from a previous run; excluding it from both sides
            # prevents a false mismatch on resume.
            comparable_keys = [k for k in current_metadata if k != "chunk_duration"]
            old_comparable = {k: old_metadata.get(k) for k in comparable_keys}
            new_comparable = {k: current_metadata[k] for k in comparable_keys}

            if old_comparable == new_comparable:
                # Core parameters matched — this is a resume of an existing run.
                is_resume = True
            else:
                # Genuine mismatch — processing_chunks belongs to a different job.
                # Never offer a simple y/n delete prompt: completed chunks may
                # represent days of processing. Deletion requires --force to be
                # passed explicitly on the command line as a deliberate act.
                print("\n[!] ERROR: processing_chunks contains data from a different job.")
                print(f"  Existing job: {old_metadata}")
                print(f"  Current job:  {current_metadata}")
                print("")
                print("  The existing chunks in processing_chunks/ belong to a different run.")
                print("  To avoid accidental data loss, deletion requires --force.")
                print("")
                print("  Options:")
                print("    1. Move or rename processing_chunks/ to preserve the existing work,")
                print("       then re-run this command.")
                print("    2. Run with --force to delete the existing chunks and start fresh.")
                if args.force and input_exists:
                    print("\n[INFO] --force specified — deleting existing chunks and starting fresh.")
                    safe_rmtree(PROCESSING_DIR)
                else:
                    if args.force and not input_exists:
                        print("  --force cannot discard the only resumable copy "
                              "after its master has been released.")
                    sys.exit(1)

    if not input_exists and not is_resume:
        raise RuntimeError(
            "The prepared master is absent, but this invocation does not match "
            "the recorded job metadata. The pipeline cannot safely resume it."
        )

    os.makedirs(PROCESSING_DIR, exist_ok=True)
    # Metadata is written after autotune sets chunk_duration (below).
    # --- End of metadata check ---

    # --- Step 0: Setup, Auto-Tune, and Splitting ---
    print("\n--- 0. Setup and Splitting ---")

    os.makedirs(INPUT_CHUNKS_DIR, exist_ok=True)
    os.makedirs(ESRGAN_CHUNKS_DIR, exist_ok=True)
    os.makedirs(FINAL_CHUNKS_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    completed_final_chunks = glob.glob(os.path.join(FINAL_CHUNKS_DIR, "*_final.mp4"))
    existing_input_chunks = glob.glob(os.path.join(INPUT_CHUNKS_DIR, f"*{INPUT_EXT}"))

    # A resumed split must use its recorded size. Retuning could change the
    # chunk boundaries and mix pictures from different divisions of the source.
    if is_resume:
        CHUNK_DURATION_SECONDS = resume_chunk_duration(old_metadata)
        print(f"[INFO] Resuming with stored chunk duration: {CHUNK_DURATION_SECONDS}s "
              f"({len(completed_final_chunks)} chunks already completed).")
        print("[INFO] Skipping autotune — chunk size must match the existing split.")
        # Warn if disk space is tight for the stored chunk size, but never
        # suggest deleting existing work — the user must free space themselves.
        try:
            # See autotune_chunk_size: must check PROCESSING_DIR, not ".".
            free_disk_gb = check_disk_space(PROCESSING_DIR, 10)
            if input_exists:
                fps = float(get_video_fps(INPUT_VIDEO))
                in_width, in_height = get_video_dimensions(INPUT_VIDEO)
            else:
                fps = float(saved_source_measurements["fps"])
                in_width = int(saved_source_measurements["width"])
                in_height = int(saved_source_measurements["height"])
            out_width, out_height = in_width * SCALE_FACTOR, in_height * SCALE_FACTOR
            bytes_per_pixel = 3
            est_png_mb = (out_width * out_height * bytes_per_pixel * EST_PNG_COMP_RATIO) / (1024**2)
            mb_per_sec = fps * 2 * est_png_mb
            needed_gb = (CHUNK_DURATION_SECONDS * mb_per_sec) / (DISK_SAFETY_MARGIN * 1024)
            if free_disk_gb < needed_gb:
                print("\n⚠️  WARNING: Low disk space for resumed run.")
                print(f"    Stored chunk size ({CHUNK_DURATION_SECONDS}s) requires ~{needed_gb:.1f} GB free.")
                print(f"    Available: {free_disk_gb:.2f} GB free at current path.")
                print(f"    Free up at least {needed_gb - free_disk_gb:.1f} GB before continuing.")
                print("    DO NOT delete processing_chunks/ — your completed chunks are there.")
                if not ask_yes_no("    Continue anyway? (y/n): "):
                    print("    Exiting. Free disk space and retry.")
                    sys.exit(0)
        except Exception:
            pass  # disk check failure is non-fatal on resume
    else:
        if args.chunk_duration:
            CHUNK_DURATION_SECONDS = args.chunk_duration
            print(f"--- Chunk size set by --chunk-duration: "
                  f"{CHUNK_DURATION_SECONDS}s (auto-tuning skipped) ---")
        else:
            if not input_exists:
                raise RuntimeError(
                    "The prepared master is absent and no stored chunk duration "
                    "exists; this run cannot be resumed safely."
                )
            CHUNK_DURATION_SECONDS = autotune_chunk_size(INPUT_VIDEO, SCALE_FACTOR)

    # Add chunk_duration now that autotune has run, then write metadata
    # only if the content has changed. On a clean resume the file already
    # contains the correct values — rewriting it unnecessarily changes its
    # mtime and makes it look like something changed when nothing did.
    current_metadata["chunk_duration"] = CHUNK_DURATION_SECONDS
    _existing_metadata = None
    try:
        with open(metadata_file, "r") as f:
            _existing_metadata = json.load(f)
    except Exception:
        pass
    if _existing_metadata != current_metadata:
        metadata_tmp = f"{metadata_file}.tmp"
        with open(metadata_tmp, "w") as f:
            json.dump(current_metadata, f, indent=4)
            f.write("\n")
        os.replace(metadata_tmp, metadata_file)

    if input_exists:
        duration = get_video_duration(INPUT_VIDEO)
        source_fps_str = get_video_fps(INPUT_VIDEO)
    else:
        duration = float(saved_source_measurements["container_duration"])
        source_fps_str = saved_source_measurements.get(
            "fps_text", f"{float(saved_source_measurements['fps']):.3f}")
    source_fps_float = float(source_fps_str)

    # Guard: ~60fps input + RIFE would produce ~120fps output, which is never
    # the intent. This indicates a bwdif mode=1 master was passed in with
    # --rife. Abort early with a clear message rather than silently producing
    # a 120fps file after hours of processing.
    if source_fps_float > 45 and not args.no_rife:
        print(f"\n[!] ERROR: Input is ~{source_fps_float:.3f}fps (bwdif mode=1 master) "
              f"but --rife was specified.")
        print("    Running RIFE on a 60fps master would produce ~120fps output.")
        print("    Either:")
        print("      - Remove --rife to process at 60fps without interpolation (default)")
        print("      - Re-run prepare_video.sh with --mode0 to get a 30fps master, then use --rife")
        sys.exit(1)

    # With RIFE enabled, output is double the source rate.
    # With RIFE off (the default), output matches source rate exactly.
    output_fps_float = source_fps_float if args.no_rife else source_fps_float * 2

    # Build output filename suffix reflecting fps and interpolation method:
    #   mode=1 (default, no-rife): _60fps       (59.94fps input, no interpolation)
    #   mode=0 + --rife:           _60fps_rife   (29.97fps input doubled to 59.94fps)
    #   mode=0 + no-rife:          _30fps        (29.97fps input, no interpolation)
    if args.no_rife:
        fps_suffix = "_60fps" if source_fps_float > 45 else "_30fps"
    else:
        fps_suffix = "_60fps_rife"

    # Include quality settings in deliverable names. The batch validates these
    # tokens when selecting compatible files for assembly.
    final_deliverable_crf = active_esrgan_writer_crf if args.no_rife else active_rife_encode_crf
    crf_suffix = f"_crf{final_deliverable_crf}_yuv420"
    FINAL_VIDEO_FILE = f"{FINAL_VIDEO_FILE_BASE}{fps_suffix}{crf_suffix}.mp4"

    # --- Deferred profile selection (requires source_fps_float) ---
    # 60fps masters (bwdif mode=1) use halved temporal hqdn3d values to avoid
    # over-smoothing frames that are already half the inter-frame distance apart.
    # The threshold of 45fps cleanly separates 29.97/25fps masters from 59.94/50fps.
    if source_fps_float > 45:
        prefilter_vf = PROFILES_60FPS[profile]
        fps_label = "60fps"
    else:
        prefilter_vf = PROFILES_30FPS[profile]
        fps_label = "30fps"

    # Stream-copy segmentation of H.264/MP4 can leave the first decoded video
    # frame at a positive PTS (observed as 0.033s for 59.94fps input chunks).
    # If that timestamp reaches FFmpeg's default constant-frame-rate output
    # synchronizer, it fills the apparent gap by duplicating two leading
    # frames. Reset decoded timestamps before every pre-filter encode so frame
    # ordinal zero remains ordinal zero regardless of the input container's
    # edit-list/B-frame timestamp representation. This is harmless for MKV and
    # whole-file inputs that already begin at zero.
    prefilter_vf_with_normalized_pts = f"setpts=PTS-STARTPTS,{prefilter_vf}"

    source_sar = (get_video_sar(INPUT_VIDEO) if input_exists
                  else saved_source_measurements.get("sar"))

    # --- No-Chunk Fast Path ---
    # Videos shorter than NO_CHUNK_THRESHOLD_SEC bypass the split/chunk/concat
    # scaffold entirely. ESRGAN runs on the pre-filtered master in one pass.
    # The chunking path (below) is taken only for longer files.
    # --chunk-threshold makes the path selectable, which is what lets the
    # chunked path be tested at all: without it, exercising split/process/
    # concat needs a video over fifteen minutes long and hours of GPU time, so
    # in practice it was only ever exercised by real work.
    chunk_threshold = args.chunk_threshold
    use_chunking = ((duration >= chunk_threshold) if input_exists
                    else bool(saved_source_measurements["use_chunking"]))

    if not use_chunking:
        total_chunks = 1  # informational only; not used in the no-chunk path
        duration_label = f"{duration:.1f}s  (no chunking — under {chunk_threshold}s threshold)"
    else:
        total_chunks = math.ceil(duration / CHUNK_DURATION_SECONDS)
        duration_label = f"{duration:.1f}s  →  {total_chunks} chunks × {CHUNK_DURATION_SECONDS}s"

    # --- Pre-Flight Plan ---
    # Always printed so the user can verify settings before hours of processing.
    # On a resume the plan is shown as a reminder. Confirmation is skipped only
    # once durable, expensive work exists: a completed final chunk or a
    # no-chunk intermediate. A split alone is cheap, so it is deliberately not
    # treated as enough persisted progress to suppress another confirmation.
    # --yes and --force bypass confirmation for non-interactive use. Only
    # --force authorizes destructive reset or continuing after a failed gate.
    print(f"\n{'=' * 55}")
    print("  PRE-FLIGHT PLAN")
    print(f"{'=' * 55}")
    print(f"  Input:         {INPUT_VIDEO}")
    print(f"  Input FPS:     {source_fps_str} ({fps_label} variant profile)")
    print(f"  Input SAR:     {source_sar if source_sar else '1:1 (square pixels)'}")
    if input_exists:
        plan_in_width, plan_in_height = get_video_dimensions(INPUT_VIDEO)
    else:
        plan_in_width = int(saved_source_measurements["width"])
        plan_in_height = int(saved_source_measurements["height"])
    plan_out_width, plan_out_height = plan_in_width * SCALE_FACTOR, plan_in_height * SCALE_FACTOR
    if source_sar:
        print(f"  Output SAR:    {source_sar}, written into the MP4 after inference (ESRGAN works "
              f"in square pixels; MP4Box sets the container and AVC stream, no re-encode) "
              f"-> {plan_out_width}x{plan_out_height} displays as "
              f"{display_aspect_label(plan_out_width, plan_out_height, source_sar)}; "
              f"checked on the delivered file")
    else:
        print(f"  Output SAR:    1:1 (square pixels; {plan_out_width}x{plan_out_height} displays as is)")
    print(f"  Duration:      {duration_label}")
    print(f"  Scale:         {SCALE_FACTOR}x  ({REALSRGAN_MODEL})")
    print(f"  Profile:       {profile}  →  {prefilter_vf}")
    print(
        f"  Pre-filter:    CRF {PREFILTER_CRF}  |  preset fast  |  yuv444p  |  {COLOR_SPACE}  "
        "(intermediate, deleted after processing)")
    if args.no_rife:
        print(f"  ESRGAN output: CRF {active_esrgan_writer_crf}  |  {active_esrgan_writer_pix_fmt}  "
              f"(inference_realesrgan_video.py's own writer; "
              f"THIS IS THE FINAL DELIVERABLE, RIFE being off)")
    if not args.no_rife:
        print(f"  ESRGAN output: CRF {active_esrgan_writer_crf}  |  {active_esrgan_writer_pix_fmt}  "
              f"(full-chroma intermediate decoded to PNG for RIFE)")
        print(f"  RIFE encode:   CRF {active_rife_encode_crf}  |  {active_final_pix_fmt}  |  preset fast  "
              f"(THIS IS THE FINAL DELIVERABLE with --rife)")
    rife_description = 'disabled (default)' if args.no_rife else 'enabled (--rife): frames will be doubled'
    print(f"  RIFE:          {rife_description}")
    print(f"  Output:        yuv420p CRF {final_deliverable_crf} in MP4")
    print(f"  Output FPS:    {output_fps_float:.3f}")
    print("  Verification: MP4 boundaries, frame counts, timing, A/V alignment and aspect ratio" +
          (" + full video decode (slow)" if args.verify_full_decode else ""))
    print(f"  Output file:   {FINAL_VIDEO_FILE}")

    # Warn prominently when the output will not be ~60fps — this is the most
    # common configuration mistake and can waste hours of processing time.
    if output_fps_float < 50:
        print(f"\n  ⚠️  WARNING: Output will be {output_fps_float:.3f}fps — NOT ~60fps.")
        if args.no_rife and source_fps_float <= 45:
            print("     This is a ~30fps input with RIFE disabled.")
            print("     For ~60fps output, either:")
            print("       (a) Re-prepare with prepare_video.sh (default mode=1) → re-run pipeline, or")
            print("       (b) Add --rife to interpolate this 30fps master to ~60fps.")

    # Warn when SCALE_FACTOR doesn't match the selected model's native scale.
    # Confirmed by direct read of realesrgan/utils.py upstream:
    # RealESRGANer.enhance() silently runs a cv2.resize(..., INTER_LANCZOS4)
    # post-pass whenever outscale != the model's own scale -- e.g.
    # realesr-general-x4v3 is always a 4x model internally, so --scale 2 with
    # that model forces this extra resampling step. Neither this script nor
    # inference_realesrgan_video.py prints anything when that branch fires, so
    # without this check it would be exactly as invisible as the CRF-23
    # default was before it was found by inspecting an output file by hand.
    # NETSCALE mirrors the netscale values inference_realesrgan_video.py
    # itself assigns per model (see its model_name branch).
    NETSCALE = {"RealESRGAN_x4plus": 4, "RealESRNet_x4plus": 4,
                "RealESRGAN_x4plus_anime_6B": 4, "RealESRGAN_x2plus": 2,
                "realesr-animevideov3": 4, "realesr-general-x4v3": 4}
    model_netscale = NETSCALE.get(REALSRGAN_MODEL)
    if model_netscale is not None and SCALE_FACTOR != model_netscale:
        print(f"\n  ⚠️  WARNING: --scale {SCALE_FACTOR} does not match {REALSRGAN_MODEL}'s "
              f"native scale ({model_netscale}x).")
        print(f"     RealESRGANer.enhance() will silently run an extra cv2.resize "
              f"(Lanczos4) pass to reconcile the two -- the model's native "
              f"{model_netscale}x output gets resampled to {SCALE_FACTOR}x afterward.")
        print("     This is real, separate resampling beyond the model's own output, "
              "not flagged anywhere else in this pipeline's logs.")
    print(f"{'=' * 55}\n")

    # A resume skips confirmation only when durable, expensive work exists.
    # Matching metadata and a cheap input split do not qualify: a run that
    # crashes before finishing its first chunk must not resume into hours of
    # GPU work with no prompt. The decline path below removes that setup state;
    # a crash has no such opportunity, so the condition is checked here rather
    # than trusted to cleanup that may never run.
    existing_no_chunk_work = glob.glob(
        os.path.join(RUN_DIR, "work", "no_chunk", "*"))
    if is_resume and (completed_final_chunks or existing_no_chunk_work):
        print(f"[INFO] Resuming previous run ({len(completed_final_chunks)} chunk(s) "
              f"completed, {len(existing_input_chunks)} input chunk(s), "
              f"{len(existing_no_chunk_work)} no-chunk artifact(s) retained) "
              "— confirmation skipped.")
    elif args.force or args.yes:
        print(f"[INFO] --{'force' if args.force else 'yes'} specified — "
              "skipping confirmation.")
    else:
        if is_resume:
            print("[INFO] Settings match an existing run, but no chunk has "
                  "completed — confirming as a fresh start.")
        if not ask_yes_no("  Proceed? (y/n): "):
            # If no chunk has actually completed yet, processing_chunks/ at
            # this point contains only setup state: the 0/1/2_*_chunks/
            # scaffold, metadata.json, and possibly the cheap input split.
            # It contains no approved GPU result a future run should silently
            # resume. Left in place, that metadata.json would make the next
            # invocation's metadata comparison match and silently set
            # is_resume=True, skipping confirmation on a run that never
            # actually started. Removing it here keeps "decline" meaning
            # exactly that, with no leftover state to misread as progress.
            # completed_final_chunks is never reassigned between its
            # computation above and here, so it still reflects this run's
            # actual on-disk state at the moment of decline.
            if not input_exists:
                print("  Prepared master has already been released — leaving "
                      f"the resumable state in {PROCESSING_DIR}/ intact.")
            elif not completed_final_chunks:
                print(f"  No completed chunks yet — removing unapproved setup state "
                      f"from {PROCESSING_DIR}/.")
                safe_rmtree(PROCESSING_DIR)
            else:
                print(f"  {len(completed_final_chunks)} chunk(s) already completed — "
                      f"leaving {PROCESSING_DIR}/ in place. Re-run to resume.")
            print("  Exiting. Add --yes to accept the plan without granting "
                  "--force's reset/validation overrides.")
            sys.exit(0)

    # Emit final-output-file line after confirmation so it appears in the log
    # at the point processing actually begins (consistent with prior behaviour).
    print(f"Final Output File: {FINAL_VIDEO_FILE}")
    print(f"   [Profile filter] {profile} ({fps_label} variant): {prefilter_vf}")
    if source_sar:
        print(f"Source SAR detected: {source_sar} (non-square pixels — will be preserved in final output.)")
    else:
        print("Source SAR: 1:1 (square pixels)")
    print(f"Video detected: {duration:.2f}s, {source_fps_str} FPS.")
    if use_chunking:
        print(f"Using {CHUNK_DURATION_SECONDS}s chunks, splitting into {total_chunks} total chunks.")
    else:
        print("No-chunk path: processing entire video in one pass.")
    if args.no_rife:
        print(f"RIFE disabled (default): output will be {output_fps_float:.3f} FPS (matches input).")
    else:
        print(f"RIFE enabled (--rife): output will be {output_fps_float:.3f} FPS (2x input).")

    # ==========================================================================
    # NO-CHUNK FAST PATH
    # Taken when duration < NO_CHUNK_THRESHOLD_SEC. Bypasses the entire
    # split/chunk-loop/concat scaffold. Steps mirror the per-chunk logic but
    # operate on the full video directly:
    #   1. Pre-filter → prefiltered master (libx264 CRF 12 yuv444p, temp)
    #   2. Real-ESRGAN inference on prefiltered master → esrgan output (temp)
    #   3. [RIFE or rename] → final_output (temp)
    #   4. Audio extract + mux → FINAL_VIDEO_FILE
    #   5. SAR correction (MP4Box -par, no re-encode)
    #   6. Remove temp files, print timing
    # metadata.json and source_measurements.json remain under PROCESSING_DIR so
    # this path can also resume after releasing its prepared master.
    # ==========================================================================
    if not use_chunking:
        total_start_time = time.time()
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # The chunk scaffold is created before the path is chosen, so a
        # no-chunk run inherits four empty directories it will never write to.
        # Removing them here keeps work/ showing only what this run is actually
        # using; Step 7 below removes the parent once the run succeeds.
        for stale in (INPUT_CHUNKS_DIR, ESRGAN_CHUNKS_DIR, FINAL_CHUNKS_DIR):
            if os.path.isdir(stale) and not os.listdir(stale):
                try:
                    os.rmdir(stale)
                except OSError:
                    pass

        # Working files go under the run's work/ directory, never into videos/.
        # videos/ holds the run's deliverables; work/ holds what the run can
        # regenerate, and its presence after a run is what says the run stopped
        # part-way through a file (see pipeline_docs/storage-layout.md).
        #
        # These are cleaned up on success, so putting them in videos/ was
        # invisible most of the time -- but a crashed or killed run left them
        # sitting among the deliverables with nothing to tidy them, and
        # upscale_dv_batch.sh locates its output by globbing videos/ for
        # "<source stem>_*.mp4" and taking the last by sort order, which
        # "_nc_prefiltered.mp4" wins over the real "_dv_x2_..." output.
        nc_work_dir = os.path.join(RUN_DIR, "work", "no_chunk")
        os.makedirs(nc_work_dir, exist_ok=True)
        nc_prefiltered = os.path.join(nc_work_dir, f"{input_basename}_nc_prefiltered.mp4")
        nc_esrgan_dir = os.path.join(nc_work_dir, f"{input_basename}_nc_esrgan_work")
        nc_esrgan_out = os.path.join(nc_work_dir, f"{input_basename}_nc_esrgan.mp4")
        nc_final_out = os.path.join(nc_work_dir, f"{input_basename}_nc_final.mp4")
        nc_audio = os.path.join(nc_work_dir, f"{input_basename}_nc_audio.m4a")
        nc_audio_mp3 = os.path.join(nc_work_dir, f"{input_basename}_nc_audio.mp3")

        nc_prefilter_ready = (os.path.isfile(nc_prefiltered)
                              and is_valid_video(nc_prefiltered))
        nc_esrgan_ready = (os.path.isfile(nc_esrgan_out)
                           and is_valid_video(nc_esrgan_out))
        nc_final_ready = (os.path.isfile(nc_final_out)
                          and is_valid_video(nc_final_out))
        if not input_exists:
            if not saved_source_measurements.get("no_chunk_prefilter_validated"):
                raise RuntimeError(
                    "The prepared master is absent, but the no-chunk pre-filter "
                    "checkpoint was not recorded as validated. Restore the master."
                )
            if saved_source_measurements.get("no_chunk_prefilter_duration") is None:
                raise RuntimeError(
                    "The prepared master is absent and the saved no-chunk "
                    "checkpoint has no pre-filter duration. Restore the master."
                )
            if not (nc_prefilter_ready or nc_esrgan_ready or nc_final_ready):
                raise RuntimeError(
                    "The prepared master is absent and no validated no-chunk "
                    "video intermediate remains. This run cannot resume."
                )

        # --- Step 1: Pre-filter ---
        print("\n--- No-Chunk 1: Pre-filtering ---")
        if nc_prefilter_ready or nc_esrgan_ready or nc_final_ready:
            if nc_prefilter_ready:
                print(f"  > Reusing validated pre-filter intermediate: {nc_prefiltered}")
            else:
                print("  > Pre-filter already consumed by a validated downstream "
                      "no-chunk intermediate.")
        else:
            print(f"  > Applying profile '{profile}' filter: {prefilter_vf_with_normalized_pts}")
            cmd_nc_prefilter = [
                "ffmpeg", "-nostdin", "-y",
                "-i", INPUT_VIDEO,
                "-vf", prefilter_vf_with_normalized_pts,
                "-c:v", "libx264", "-threads", str(threads), "-crf", str(PREFILTER_CRF),
                "-preset", "fast",
                "-pix_fmt", "yuv444p",
                # Explicit colorimetry on the pre-filter intermediate so
                # the ESRGAN Reader's ffmpeg decode uses the correct YCbCr->RGB matrix
                # rather than guessing from SD resolution heuristics.
                "-colorspace", COLOR_SPACE,
                "-color_primaries", COLOR_PRIMARIES,
                "-color_trc", COLOR_TRC,
                "-an",  # audio handled separately below
                nc_prefiltered,
            ]
            try:
                with Timer("No-chunk pre-filter"):
                    subprocess.run(cmd_nc_prefilter, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                print("\n--- ERROR: Pre-filter failed ---")
                print("STDOUT:", e.stdout)
                print("STDERR:", e.stderr)
                raise
            if not is_valid_video(nc_prefiltered):
                raise RuntimeError(f"Pre-filter produced a corrupt output: {nc_prefiltered}")
            nc_prefilter_ready = True

        # Reconcile before any GPU work, on this path as well as the chunked
        # one. Without it a pre-filter that silently truncated would run through
        # ESRGAN and surface only at the final timing check -- hours later, with
        # the work already spent.
        if input_exists:
            SOURCE_MEASUREMENTS = measure_source_state(
                INPUT_VIDEO, use_chunking=False)
        else:
            SOURCE_MEASUREMENTS = saved_source_measurements
        if nc_prefilter_ready:
            nc_prefiltered_duration = get_video_duration(nc_prefiltered)
        else:
            nc_prefiltered_duration = no_chunk_checkpoint_duration(saved_source_measurements)

        # Audio is extracted up front. Together with the validated pre-filter
        # intermediate and source measurements, it replaces every remaining
        # use of the prepared master.
        print("\n--- No-Chunk 1b: Audio extraction ---")
        if input_exists:
            nc_audio_seconds, nc_audio_priming = extract_source_audio(
                INPUT_VIDEO, nc_audio, args.force)
        elif SOURCE_MEASUREMENTS["audio_present"]:
            saved_audio = SOURCE_MEASUREMENTS["audio_file"]
            if "audio_priming_samples" not in SOURCE_MEASUREMENTS:
                raise missing_audio_priming_error(saved_audio, RUN_DIR)
            if os.path.abspath(saved_audio) != os.path.abspath(nc_audio):
                raise RuntimeError(
                    "Saved no-chunk audio path does not match this run:\n"
                    f"  saved:    {saved_audio}\n  expected: {nc_audio}"
                )
            if not (os.path.isfile(nc_audio) and os.path.getsize(nc_audio) > 0):
                raise RuntimeError(
                    f"Saved no-chunk audio is missing or empty: {nc_audio}"
                )
            nc_audio_seconds = get_video_duration(nc_audio)
            nc_audio_priming = SOURCE_MEASUREMENTS["audio_priming_samples"]
            print(f"  > Using saved source audio: {os.path.basename(nc_audio)}")
            verify_audio_priming(nc_audio, nc_audio_priming, "Saved source audio",
                                 force=args.force)
        else:
            nc_audio_seconds = nc_audio_priming = None
            print("  > Saved source state records no audio stream.")
        nc_durations_validated = reconcile_durations(
            SOURCE_MEASUREMENTS["duration"],
            nc_prefiltered_duration, "pre-filtered", nc_audio_seconds,
            fps=SOURCE_MEASUREMENTS.get("fps"), audio_file=nc_audio)
        if not nc_durations_validated:
            if not args.force:
                raise RuntimeError(
                    "Source, pre-filtered video and audio durations disagree. "
                    "Re-run with --force to process anyway.")
            print("    [!] --force given: continuing despite the mismatch.")
        if nc_prefilter_ready:
            check_output_crf(nc_prefiltered, expected_crf=PREFILTER_CRF,
                             label="No-chunk pre-filter intermediate", fatal=True)
            check_output_pix_fmt(nc_prefiltered, expected_pix_fmt="yuv444p",
                                 label="No-chunk pre-filter intermediate", fatal=True)
            validate_final_video_timing(
                INPUT_VIDEO,
                nc_prefiltered,
                fps_multiplier=1,
                frame_tolerance=0,
                source_measurements=SOURCE_MEASUREMENTS,
            )

        if input_exists:
            SOURCE_MEASUREMENTS.update({
                "audio_present": nc_audio_seconds is not None,
                "audio_file": (os.path.abspath(nc_audio)
                               if nc_audio_seconds is not None else None),
                "audio_duration": nc_audio_seconds,
                "audio_priming_samples": nc_audio_priming,
                "no_chunk_prefilter_validated": nc_durations_validated,
                "no_chunk_prefilter_duration": nc_prefiltered_duration,
            })
            record_source_measurements(PROCESSING_DIR, SOURCE_MEASUREMENTS)
            if owns_run and nc_durations_validated:
                free_master_if_disposable(
                    INPUT_VIDEO, RUN_DIR, args.keep_input)
            elif not owns_run:
                print("  > Prepared master retained for the owning batch driver; "
                      "it controls per-file resume and cleanup.")
            else:
                print("  > Prepared master retained because the pre-filter/audio "
                      "checkpoint did not validate completely.")
            input_exists = os.path.exists(INPUT_VIDEO)

        # --- Step 2: Real-ESRGAN ---
        eta_suffix = f"  [{args.esrgan_eta}]" if args.esrgan_eta else ""
        print(f"\n--- No-Chunk 2: Real-ESRGAN ---{eta_suffix}")
        if nc_final_ready:
            print("  > Reusing validated final no-chunk video; ESRGAN already completed.")
        elif nc_esrgan_ready:
            print(f"  > Reusing completed Real-ESRGAN output: {nc_esrgan_out}")
        else:
            # Timestamp before subprocess.run() so we can correlate with nvtop.
            # GPU activity will appear some time after this line — the gap is the
            # inference_realesrgan_video.py startup cost (torch/CUDA init, model
            # load, and full-file frame decode before the first inference call).
            print(f"[Perf] inference_realesrgan_video.py launching at "
                  f"{datetime.now().astimezone().strftime('%H:%M:%S')}"
                  f" (t+{time.time() - _t_main_entry:.1f}s from pipeline start)")
            os.makedirs(nc_esrgan_dir, exist_ok=True)
            cmd_nc_esrgan = [
                # sys.executable, not "python3": the child must run in the same
                # interpreter as this process. Hardcoding "python3" takes whatever
                # is first on PATH, so launching the pipeline as
                # `venv/bin/python video_upscale_pipeline.py` -- without activating
                # the venv -- silently handed inference a DIFFERENT interpreter,
                # which found a user-site basicsr and died with "Numpy is not
                # available". It worked only because the documented workflow
                # activates the venv first.
                sys.executable, "inference_realesrgan_video.py",
                "-i", nc_prefiltered,
                "-n", REALSRGAN_MODEL,
                "-o", nc_esrgan_dir,
                "-s", str(SCALE_FACTOR),
                "--fps", source_fps_str,
                "--crf", str(active_esrgan_writer_crf),
                "--out_pix_fmt", active_esrgan_writer_pix_fmt,
            ] + DENOISE_STRENGTH_ARGS
            try:
                with Timer("No-chunk ESRGAN inference"):
                    subprocess.run(cmd_nc_esrgan, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                print("\n--- ERROR: Real-ESRGAN failed ---")
                print("STDOUT:", e.stdout)
                print("STDERR:", e.stderr)
                raise

            # inference_realesrgan_video.py writes one MP4 into the output dir.
            mp4_files = glob.glob(os.path.join(nc_esrgan_dir, "*.mp4"))
            if len(mp4_files) != 1:
                raise RuntimeError(
                    f"Expected 1 MP4 from ESRGAN, found {len(mp4_files)} in {nc_esrgan_dir}"
                )
            os.replace(mp4_files[0], nc_esrgan_out)
            safe_rmtree(nc_esrgan_dir)
            nc_esrgan_ready = True
            print(f"  > Real-ESRGAN complete: {nc_esrgan_out}")
        # This is either the deliverable (RIFE off) or the source decoded to
        # PNG for RIFE. Loss at this point survives every downstream step, so
        # enforce the intended CRF and chroma format in both cases.
        if not nc_final_ready:
            check_output_crf(nc_esrgan_out, expected_crf=active_esrgan_writer_crf,
                             label="No-chunk ESRGAN output (inference_realesrgan_video.py)",
                             fatal=True)
            check_output_pix_fmt(nc_esrgan_out, expected_pix_fmt=active_esrgan_writer_pix_fmt,
                                 label="No-chunk ESRGAN output (inference_realesrgan_video.py)",
                                 fatal=True)

        # The pre-filtered master has been consumed by ESRGAN.
        if nc_esrgan_ready and os.path.exists(nc_prefiltered):
            os.remove(nc_prefiltered)

        # --- Step 3: RIFE or rename ---
        if nc_final_ready:
            print("\n--- No-Chunk 3: Reusing completed final video ---")
        elif args.no_rife:
            print("\n--- No-Chunk 3: Skipping RIFE (off; --rife not given) ---")
            os.replace(nc_esrgan_out, nc_final_out)
            nc_final_ready = True
        else:
            print("\n--- No-Chunk 3: RIFE frame interpolation ---")
            # Also under work/: these hold one PNG per frame and are by far the
            # largest thing the no-chunk path writes.
            nc_rife_in_frames = os.path.join(nc_work_dir, f"{input_basename}_nc_rife_in")
            nc_rife_out_frames = os.path.join(nc_work_dir, f"{input_basename}_nc_rife_out")
            os.makedirs(nc_rife_in_frames, exist_ok=True)
            os.makedirs(nc_rife_out_frames, exist_ok=True)

            # Extract frames from ESRGAN output
            print("  > Extracting frames for RIFE...")
            expected_frames = int(source_fps_float * duration)
            extraction_timeout = 120 + (expected_frames * 10)
            cmd_nc_extract = [
                "ffmpeg", "-nostdin", "-i", nc_esrgan_out,
                os.path.join(nc_rife_in_frames, "frame_%08d.png"),
            ]
            try:
                with Timer("No-chunk RIFE frame extraction"):
                    subprocess.run(cmd_nc_extract, check=True, capture_output=True,
                                   text=True, timeout=extraction_timeout)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"Frame extraction timed out after {extraction_timeout}s")
            except subprocess.CalledProcessError as e:
                print("\n--- ERROR: Frame extraction failed ---")
                print("STDOUT:", e.stdout)
                print("STDERR:", e.stderr)
                raise

            in_frames = glob.glob(os.path.join(nc_rife_in_frames, "*.png"))
            print(f"  > Running RIFE on {len(in_frames)} frames...")
            cmd_nc_rife = [
                RIFE_BIN,
                "-i", nc_rife_in_frames,
                "-o", nc_rife_out_frames,
                "-s", "0.5",
            ]
            try:
                with Timer("No-chunk RIFE interpolation"):
                    subprocess.run(cmd_nc_rife, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                print("\n--- ERROR: RIFE failed ---")
                print("STDOUT:", e.stdout)
                print("STDERR:", e.stderr)
                raise

            out_frames = glob.glob(os.path.join(nc_rife_out_frames, "*.png"))
            if len(out_frames) < len(in_frames) * 2 - 2:
                raise RuntimeError(
                    f"RIFE failed to double frames: "
                    f"input {len(in_frames)}, output {len(out_frames)}"
                )
            print(f"  > RIFE check passed (in: {len(in_frames)}, out: {len(out_frames)}).")

            print("  > Encoding RIFE frames...")
            cmd_nc_encode = [
                "ffmpeg", "-nostdin",
                "-framerate", str(output_fps_float),
                "-i", os.path.join(nc_rife_out_frames, "%08d.png"),
                "-c:v", "libx264",
                "-threads", str(threads),
                "-pix_fmt", active_final_pix_fmt,
                # yuv444p, not the yuv420p default. Input PNG frames are full
                # RGB (full chroma resolution); encoding to yuv420p here would
                # halve chroma regardless of CRF -- the same trap avoided
                # in inference_realesrgan_video.py's Writer, one stage further
                # down this path (RIFE decodes that writer's output back to
                # PNG and re-encodes here, so this site needed the same fix
                # independently).
                # In distribution mode this is the one and only yuv420p/target-
                # CRF conversion; the upstream ESRGAN intermediate stays
                # CRF 16/yuv444p so RIFE receives full-chroma frames.
                "-crf", str(active_rife_encode_crf),
                "-preset", "fast",
                # Explicit colorimetry, matching the pre-filter and
                # ESRGAN-writer sites. This is a
                # final-deliverable write site for --rife (the same role the
                # ESRGAN writer has with RIFE off), so it must carry the same
                # tags rather than relying on the final concat-mux step's
                # -c:v copy to stamp them after the fact.
                "-colorspace", COLOR_SPACE,
                "-color_primaries", COLOR_PRIMARIES,
                "-color_trc", COLOR_TRC,
                nc_final_out,
            ]
            try:
                with Timer("No-chunk RIFE frame encoding"):
                    subprocess.run(cmd_nc_encode, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                print("\n--- ERROR: RIFE frame encoding failed ---")
                print("STDOUT:", e.stdout)
                print("STDERR:", e.stderr)
                raise
            # This file becomes the final deliverable verbatim (no further
            # re-encode follows in the RIFE path), so a bad CRF here is fatal.
            check_output_crf(nc_final_out, expected_crf=active_rife_encode_crf,
                             label="No-chunk RIFE-encode output", fatal=True)
            check_output_pix_fmt(nc_final_out, expected_pix_fmt=active_final_pix_fmt,
                                 label="No-chunk RIFE-encode output", fatal=True)
            nc_final_ready = True

            safe_rmtree(nc_rife_in_frames)
            safe_rmtree(nc_rife_out_frames)
            if os.path.exists(nc_esrgan_out):
                os.remove(nc_esrgan_out)

        # --- Step 4: Extract audio + mux ---
        print("\n--- No-Chunk 4: Mux ---")
        # Audio was extracted and validated at the durable checkpoint before
        # ESRGAN. Do not consult the prepared master here: a normal standalone
        # run has released it by now, and a resume is specifically expected to
        # reach this point without it.

        print(f"  > Muxing video and audio into {FINAL_VIDEO_FILE}...")
        # Confirm nc_final_out carries no color tag of its own before this mux
        # stamps COLOR_SPACE/COLOR_TRC/COLOR_PRIMARIES onto it -- see
        # check_input_color_tags_unset() docstring for why this must be
        # checked rather than assumed.
        check_input_color_tags_unset(nc_final_out, label="No-chunk pre-mux video", fatal=True)
        cmd_nc_mux = [
            "ffmpeg", "-nostdin", "-y",
            "-i", nc_final_out,
            "-i", nc_audio,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "copy",
            # Stamp colorimetry on the delivered file. This is a
            # -c:v copy mux, so these flags only write container/stream
            # metadata (no re-encode, no pixel change) -- confirmed by direct
            # test. All current sources are NTSC SD; see COLOR_* constants.
            "-colorspace", COLOR_SPACE,
            "-color_trc", COLOR_TRC,
            "-color_primaries", COLOR_PRIMARIES,
            # Do not use -shortest here. Damaged DV can contain fewer audio
            # packets than video frames; ending the mux at the shorter audio
            # stream would silently discard otherwise valid final pictures.
            # Preserve both streams at their actual lengths instead.
        ]
        # Moves the moov atom to the front so players can start playback
        # before the whole file is read -- the USB-stick/general-player case
        # this output exists for.
        cmd_nc_mux += ["-movflags", "+faststart"]
        cmd_nc_mux.append(FINAL_VIDEO_FILE)
        try:
            subprocess.run(cmd_nc_mux, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            print("\n--- ERROR: Final mux failed ---")
            print("STDOUT:", e.stdout)
            print("STDERR:", e.stderr)
            print("If this is a 'non-monotonic DTS' error, change -c:v copy to -c:v libx264 and re-run.")
            raise

        # --- Step 5: SAR correction ---
        if source_sar:
            out_width, out_height = get_video_dimensions(FINAL_VIDEO_FILE)
            print(f"Applying SAR {source_sar} to final output "
                  f"(pixel {out_width}x{out_height} -> "
                  f"display {display_aspect_label(out_width, out_height, source_sar)})...")
            apply_sar_to_file(FINAL_VIDEO_FILE, source_sar, out_width, out_height)
        else:
            print("Source has square pixels (SAR 1:1) — no SAR correction needed.")

        # Final confirmation on the actual delivered file. The mux above is a
        # stream copy, so this should simply confirm whatever the last real
        # encode (ESRGAN writer with RIFE off, RIFE-encode otherwise) already
        # produced -- but checking the delivered file itself, rather than
        # trusting that nothing in between silently re-encoded it, is the
        # point: this is the file that actually ships.
        check_output_crf(FINAL_VIDEO_FILE, expected_crf=final_deliverable_crf,
                         label="Final delivered output", fatal=True)
        check_output_pix_fmt(FINAL_VIDEO_FILE, expected_pix_fmt=active_final_pix_fmt,
                             label="Final delivered output", fatal=True)
        check_output_color_tags(FINAL_VIDEO_FILE, label="Final delivered output", fatal=True)
        check_output_presentation(
            FINAL_VIDEO_FILE, label="Final delivered output", full_decode=args.verify_full_decode)
        validate_final_video_timing(
            INPUT_VIDEO,
            FINAL_VIDEO_FILE,
            fps_multiplier=(1 if args.no_rife else 2),
            frame_tolerance=(0 if args.no_rife else 2),
            source_measurements=SOURCE_MEASUREMENTS,
        )
        if SOURCE_MEASUREMENTS["audio_present"]:
            verify_audio_priming(
                FINAL_VIDEO_FILE, SOURCE_MEASUREMENTS.get("audio_priming_samples"),
                "Final delivered output", reference=nc_audio, force=args.force)
        verify_final_av_sync(
            FINAL_VIDEO_FILE, probe_video_timing(FINAL_VIDEO_FILE)["fps"],
            encoder_delay=SOURCE_MEASUREMENTS.get("audio_priming_samples"),
            force=args.force)

        # Clean up remaining temp files only after every final-deliverable
        # check has passed. A timing failure leaves these available for
        # diagnosis or recovery instead of deleting the evidence.
        for tmp in (nc_prefiltered, nc_esrgan_out, nc_final_out,
                    nc_audio, nc_audio_mp3):
            if os.path.exists(tmp):
                os.remove(tmp)
                print(f"  > Removed temp: {tmp}")
        for tmp_dir in (nc_esrgan_dir,):
            if os.path.isdir(tmp_dir):
                safe_rmtree(tmp_dir)
                print(f"  > Removed temp directory: {tmp_dir}")

        # --- Step 6: Timing summary ---
        total_end_time = time.time()
        total_duration_sec = total_end_time - total_start_time
        hours = int(total_duration_sec // 3600)
        minutes = int((total_duration_sec % 3600) // 60)
        seconds = int(total_duration_sec % 60)
        print(f"\nTotal processing time: {hours:02d}:{minutes:02d}:{seconds:02d}")

        # --- Step 7: Remove completed no-chunk resume state ---
        # Both paths retain metadata.json in the logs before removing their
        # processing state. No-chunk audio was already cleaned up above.
        finalize_processing_dir(PROCESSING_DIR, None, LOG_DIR)

        finish_run(owns_run, FINAL_VIDEO_FILE)

        print(f"\n🎉 Pipeline complete! Final file: {FINAL_VIDEO_FILE}")
        return 0  # no-chunk path complete; skip chunking path entirely

    # ==========================================================================
    # END NO-CHUNK FAST PATH
    # ==========================================================================

    # Use dynamic extension for chunk file pattern
    chunk_file_pattern = os.path.join(INPUT_CHUNKS_DIR, f"chunk_%03d{INPUT_EXT}")
    # Never re-split if completed final-stage chunks exist. A re-split at a
    # different chunk size would produce different boundaries, causing
    # overlap or gaps when the new input chunks are concatenated with
    # existing final-stage chunks.
    if completed_final_chunks:
        print(f"Chunks already split and {len(completed_final_chunks)} chunks complete — "
              f"skipping split to preserve existing boundaries.")
    elif not os.path.exists(os.path.join(INPUT_CHUNKS_DIR, f"chunk_000{INPUT_EXT}")):
        if not input_exists:
            raise RuntimeError(
                "The prepared master is absent and the saved run has no input "
                "or completed chunks. It cannot be resumed."
            )
        print(f"Splitting video into chunks (video only, extension {INPUT_EXT})...")
        cmd_split = [
            "ffmpeg", "-nostdin", "-i", INPUT_VIDEO,
            "-an", "-c:v", "copy", "-map", "0:v",
            "-segment_time", str(CHUNK_DURATION_SECONDS),
            "-f", "segment", "-reset_timestamps", "1",
            chunk_file_pattern
        ]
        # capture_output=True (matching every other subprocess.run call in this
        # script) so ffmpeg's stdout/stderr route through Python's print() and
        # therefore through TeeLogger into pipeline.log. Without it, ffmpeg
        # writes directly to the inherited terminal file descriptors, bypassing
        # sys.stdout entirely: its whole output -- version banner, codec
        # warnings, per-chunk "Opening ... for writing", the final speed=
        # summary -- is then visible on a live terminal and absent from
        # pipeline.log, which is the copy that survives the run.
        try:
            with Timer("Input split"):
                subprocess.run(cmd_split, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            print("\n--- ERROR: Input split failed ---")
            print("STDOUT:", e.stdout)
            print("STDERR:", e.stderr)
            raise
    else:
        print("Input chunks already exist, skipping split.")

    # --- Chunk Coverage Check ---
    # Verify that all input chunks and completed final-stage chunks account
    # for every source picture. This catches split problems
    # (overlap, gaps, mixed chunk sizes) before committing to hours of ESRGAN.
    #
    # On a fresh run: all chunks are in 0_input_chunks/.
    # On a resume: completed chunks have been deleted from 0_input_chunks/ and
    # their processed counterparts are in 2_final_chunks/. We sum both.
    #
    # Coverage is decided on picture counts, which the -c copy split cannot
    # change, so no tolerance applies to it. The durations printed alongside are
    # both derived from those counts and agree exactly.
    # Record picture counts from the remaining input chunks and retain the
    # recorded counts for completed chunks. On resume, re-probe the input
    # chunks before verifying coverage.
    record_chunk_source_frames(
        PROCESSING_DIR,
        sorted(glob.glob(os.path.join(INPUT_CHUNKS_DIR, f"*{INPUT_EXT}"))),
        sorted(glob.glob(os.path.join(FINAL_CHUNKS_DIR, "*_final.mp4"))))

    # Measured before the coverage check below, which compares the chunks
    # against the source's picture count, and recorded so the comparison
    # survives both a resume and the master being freed a few lines later.
    if input_exists:
        SOURCE_MEASUREMENTS = measure_source_state(
            INPUT_VIDEO, use_chunking=True)
        # This first atomic record supports the coverage check. Audio fields
        # are added below before the master can be released.
        record_source_measurements(PROCESSING_DIR, SOURCE_MEASUREMENTS)
    else:
        SOURCE_MEASUREMENTS = saved_source_measurements

    print("\n  > Verifying chunk coverage...")
    # A failed check stops the run before audio extraction or master cleanup.
    try:
        input_chunk_files = sorted(glob.glob(os.path.join(INPUT_CHUNKS_DIR, f"*{INPUT_EXT}")))
        final_stage_chunk_files = sorted(glob.glob(os.path.join(FINAL_CHUNKS_DIR, "*_final.mp4")))
        all_chunk_files = input_chunk_files + final_stage_chunk_files

        if not all_chunk_files:
            raise RuntimeError("No chunks found; exact source coverage cannot be verified.")
        else:
            # Summed from containers, so each chunk contributes its own AAC
            # padding -- up to one 21ms block apiece, ~1.7s across an 80-chunk
            # tape. Kept only as a diagnostic; this check and
            # reconcile_durations() use the exact source-frame total.
            chunks_container_sec = sum(
                float(subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", f],
                    capture_output=True, text=True, check=True
                ).stdout.strip())
                for f in all_chunk_files
            )
            # Frame counts, not durations, decide whether the split is
            # complete. The split is -c copy: lossless, reordering nothing, so
            # the chunks must hold exactly the source's pictures and no
            # tolerance is defensible. Durations cannot make that statement --
            # each chunk's container rounds its own length, and the keyframe
            # boundaries the segmenter cuts on make the last chunk short by
            # design. A duration-based check has to allow a whole short chunk
            # plus rounding, which at 300s chunks means 302 seconds of missing
            # video would pass as normal.
            # Counted in the SOURCE's frame domain. Input chunks are still in
            # it; completed final chunks are not when RIFE is enabled, because
            # interpolation doubles their pictures. Summing the two together
            # and comparing against the source count makes a resumed RIFE run
            # look like it holds far more video than it started with, and an
            # exact comparison then aborts a run that is entirely correct.
            # Durations do not have this problem -- a RIFE chunk covers the
            # same seconds as the input it replaced, only the picture count
            # changes -- but they cannot make the exact statement picture
            # counts can.
            #
            # Counted in the SOURCE's picture domain, from the counts recorded
            # when the chunks were cut. A processed chunk cannot supply its own
            # original count: RIFE roughly doubles it and permits endpoint
            # variance, so recovering it by division is wrong by a picture
            # whenever that variance occurred.
            chunks_total_frames = chunk_source_frame_total(
                PROCESSING_DIR, input_chunk_files, final_stage_chunk_files)
            source_frames = SOURCE_MEASUREMENTS["frames"]
            frame_diff = chunks_total_frames - source_frames
            print(f"  > Source frames:    {source_frames}")
            print(f"  > Chunk frames:     {chunks_total_frames} "
                  f"[{len(input_chunk_files)} input + {len(final_stage_chunk_files)} final]")
            if frame_diff != 0:
                raise RuntimeError(
                    f"Chunk coverage: the chunks hold {chunks_total_frames} pictures, "
                    f"the source has {source_frames} ({frame_diff:+d}).\n"
                    f"A -c copy split cannot lose or add pictures, so the chunks are\n"
                    f"not a faithful division of the source -- they may have been split\n"
                    f"at a different chunk size, or a chunk is missing or duplicated.\n"
                    "Preserve this run and restore its matching chunks, or start "
                    "a new run from a prepared master."
                )
            print("  > Frame coverage exact.")

            # The pictures are now known to be exactly the source's, so the
            # chunks' length is that count at the source's rate -- an exact
            # figure, not a measurement. Summing the chunks' container
            # durations instead adds a block of AAC padding per chunk, and
            # tolerating that inflation means tolerating seconds of genuinely
            # missing video along with it.
            chunks_total_sec = frames_to_seconds(
                chunks_total_frames, SOURCE_MEASUREMENTS.get("fps"))
            print(f"  > Container sum:    {chunks_container_sec:.3f}s "
                  f"(padding {(chunks_container_sec - chunks_total_sec)*1000:+.1f}ms; "
                  f"not used for the comparison)")

            # SOURCE_MEASUREMENTS["duration"] is the source's pictures at its
            # rate. `duration` is get_video_duration(), the container's claim,
            # which for an AAC master is the padded audio length -- comparing a
            # picture-derived total against it would fail by one codec block on
            # a correct run. Both sides of this comparison are pictures.
            source_sec = SOURCE_MEASUREMENTS["duration"]
            diff = chunks_total_sec - source_sec

            h = int(chunks_total_sec // 3600)
            m = int((chunks_total_sec % 3600) // 60)
            s = int(chunks_total_sec % 60)
            src_h = int(source_sec // 3600)
            src_m = int((source_sec % 3600) // 60)
            src_s = int(source_sec % 60)
            print(f"  > Source duration:  {src_h:02d}:{src_m:02d}:{src_s:02d} ({source_sec:.3f}s)")
            print(f"  > Chunks total:     {h:02d}:{m:02d}:{s:02d} ({chunks_total_sec:.3f}s) "
                  f"[{len(input_chunk_files)} input + {len(final_stage_chunk_files)} final]")
            print(f"  > Difference:       {diff*1000:+.1f}ms")

            # Both sides are now picture-derived, so they can only disagree if
            # the source's own frame count and duration disagree -- which the
            # exact check above has already ruled out for the chunks. One frame
            # covers the source's own rounding and nothing else.
            if abs(diff) > frames_to_seconds(1, SOURCE_MEASUREMENTS.get("fps")):
                raise RuntimeError(
                    f"Chunk coverage: the chunks hold exactly the source's "
                    f"{chunks_total_frames} pictures, but at "
                    f"{SOURCE_MEASUREMENTS.get('fps')}fps that is "
                    f"{chunks_total_sec:.3f}s against the source's "
                    f"{source_sec:.3f}s ({diff:+.3f}s).\n"
                    f"The source's frame count and its duration disagree; its "
                    f"measurements cannot both be right."
                )
            print("  > Coverage check passed.")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Could not verify exact chunk coverage: {e}") from e

    # --- Audio out, source measured, master freed ---
    #
    # The chunks now hold the whole picture, so the master's only remaining
    # uses are its audio and its measurements. Both are taken here, and the
    # master goes: in this path it is at its most expensive, because a complete
    # second copy of it exists as chunks at the same time, before any frame
    # working set is added on top.
    #
    # The audio duration also completes the coverage check above. That check
    # compares the chunks against the source; with the audio extracted it can
    # compare all three, and a source whose audio and video disagree is caught
    # before the chunk loop rather than after it.
    print("\n--- Audio extraction ---")
    if input_exists:
        chunked_audio_seconds, chunked_audio_priming = extract_source_audio(
            INPUT_VIDEO, ORIGINAL_AUDIO_FILE, args.force)
    elif SOURCE_MEASUREMENTS["audio_present"]:
        ORIGINAL_AUDIO_FILE = SOURCE_MEASUREMENTS["audio_file"]
        if "audio_priming_samples" not in SOURCE_MEASUREMENTS:
            raise missing_audio_priming_error(ORIGINAL_AUDIO_FILE, RUN_DIR)
        if not (ORIGINAL_AUDIO_FILE and os.path.isfile(ORIGINAL_AUDIO_FILE)
                and os.path.getsize(ORIGINAL_AUDIO_FILE) > 0):
            raise RuntimeError(
                "The prepared master has been released, but its saved audio "
                f"is missing or empty: {ORIGINAL_AUDIO_FILE}"
            )
        chunked_audio_seconds = get_video_duration(ORIGINAL_AUDIO_FILE)
        chunked_audio_priming = SOURCE_MEASUREMENTS["audio_priming_samples"]
        print(f"  > Using saved source audio: {os.path.basename(ORIGINAL_AUDIO_FILE)}")
        verify_audio_priming(ORIGINAL_AUDIO_FILE, chunked_audio_priming,
                             "Saved source audio", force=args.force)
    else:
        chunked_audio_seconds = chunked_audio_priming = None
        print("  > Saved source state records no audio stream.")
    durations_validated = reconcile_durations(
        SOURCE_MEASUREMENTS["duration"], chunks_total_sec, "chunks",
        chunked_audio_seconds, fps=SOURCE_MEASUREMENTS.get("fps"),
        audio_file=ORIGINAL_AUDIO_FILE)
    if not durations_validated:
        if not args.force:
            raise RuntimeError(
                "Source, chunk and audio durations disagree. Re-run with "
                "--force to process anyway.")
        print("    [!] --force given: continuing despite the mismatch.")

    if input_exists:
        SOURCE_MEASUREMENTS.update({
            "audio_present": chunked_audio_seconds is not None,
            "audio_file": (os.path.abspath(ORIGINAL_AUDIO_FILE)
                           if chunked_audio_seconds is not None else None),
            "audio_duration": chunked_audio_seconds,
            "audio_priming_samples": chunked_audio_priming,
        })
        # The rename in record_source_measurements() is the durability barrier:
        # a crash leaves either the previous record and the master, or the full
        # source-free resume record. Only after it lands may the master go.
        record_source_measurements(PROCESSING_DIR, SOURCE_MEASUREMENTS)
        if owns_run and durations_validated:
            free_master_if_disposable(INPUT_VIDEO, RUN_DIR, args.keep_input)
        elif not owns_run:
            print("  > Prepared master retained for the owning batch driver; "
                  "it controls per-file resume and cleanup.")
        else:
            print("  > Prepared master retained because the split/audio "
                  "checkpoint did not validate completely.")
        input_exists = os.path.exists(INPUT_VIDEO)

    # --- Steps 1 & 2: Process Chunks in a Loop ---
    print("\n--- 1. & 2. Processing All Chunks ---")

    # Clear any stale STOP file left over from a previous session (e.g. a session
    # that hit the runtime limit before it could reach the STOP-file check).
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
        print(f"[INFO] Removed stale STOP file from previous session: {STOP_FILE}")

    total_start_time = time.time()
    # Use actual chunks after coverage verification. Segment boundaries may leave
    # fewer files than ceil(duration / requested chunk size) predicts.
    actual_chunks = len(sorted(glob.glob(os.path.join(INPUT_CHUNKS_DIR, f"*{INPUT_EXT}")))) \
        + len(sorted(glob.glob(os.path.join(FINAL_CHUNKS_DIR, "*_final.mp4"))))
    if actual_chunks and actual_chunks != total_chunks:
        print(f"  > Chunk count: {actual_chunks} on disk "
              f"(estimate was {total_chunks}); using the actual count.")
        total_chunks = actual_chunks

    chunks_to_process = total_chunks
    # Rolling history for median ETA.  On a resume, reconstruct from the log
    # so the first chunk in this session shows an ETA rather than "unknown".
    if is_resume and os.path.exists(LOG_FILE):
        chunk_durations = load_chunk_durations_from_log(LOG_FILE)
        if chunk_durations:
            print(f"[INFO] Loaded {len(chunk_durations)} prior chunk timing(s) from log "
                  f"(median: {statistics.median(chunk_durations)/3600:.2f}h).")
    else:
        chunk_durations = []
    if TEST_MODE_CHUNKS is not None:
        chunks_to_process = min(total_chunks, TEST_MODE_CHUNKS)
        print(f"*** TEST MODE: Only processing {chunks_to_process} chunk(s) ***")

    # Probe the last input chunk once so every project-completion ETA in the
    # loop can use (chunks_remaining - 1 + last_chunk_fraction) × median
    # rather than chunks_remaining × median, which over-estimates whenever the
    # final chunk is shorter than CHUNK_DURATION_SECONDS (the common case).
    last_chunk_fraction = 1.0  # assume full chunk if probe fails
    last_chunk_path = os.path.join(INPUT_CHUNKS_DIR,
                                   f"chunk_{total_chunks - 1:03d}{INPUT_EXT}")
    try:
        _last_sec = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", last_chunk_path],
            capture_output=True, text=True, check=True
        ).stdout.strip())
        last_chunk_fraction = min(_last_sec / CHUNK_DURATION_SECONDS, 1.0)
        print(f"[INFO] Last chunk: {_last_sec:.0f}s / {CHUNK_DURATION_SECONDS}s nominal"
              f" = {last_chunk_fraction:.2f} of a full chunk.")
    except Exception:
        print("[INFO] Last chunk size probe failed — ETA will treat it as a full chunk.")

    for i in range(chunks_to_process):
        chunk_name = f"chunk_{i:03d}"
        chunk_start_time = time.time()
        local_start = datetime.now().astimezone()
        elapsed_hours = (chunk_start_time - total_start_time) / 3600
        print(f"\nProcessing Chunk {i+1} / {total_chunks} ({chunk_name})")
        print(f"  > Started at: {local_start.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        # Define paths here so the last-chunk ETA probe below can reference input_chunk.
        input_chunk = os.path.join(INPUT_CHUNKS_DIR, f"{chunk_name}{INPUT_EXT}")
        esrgan_temp_work_dir = os.path.join(ESRGAN_CHUNKS_DIR, f"{chunk_name}_temp_work")
        esrgan_output_file = os.path.join(ESRGAN_CHUNKS_DIR, f"{chunk_name}_esrgan.mp4")
        rife_in_frames_dir = os.path.join(ESRGAN_CHUNKS_DIR, f"{chunk_name}_rife_in_frames")
        rife_out_frames_dir = os.path.join(FINAL_CHUNKS_DIR, f"{chunk_name}_rife_out_frames")
        final_output_file = os.path.join(FINAL_CHUNKS_DIR, f"{chunk_name}_final.mp4")

        if chunk_durations:
            median_sec_pre = statistics.median(chunk_durations)
            # Last chunk is often shorter than CHUNK_DURATION_SECONDS because
            # ffmpeg's segment splitter cuts on keyframe boundaries.  Probe the
            # actual input chunk duration and scale the median proportionally so
            # the ETA reflects the real remaining work instead of a full chunk.
            if i == total_chunks - 1:
                try:
                    actual_last_sec = float(subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", input_chunk],
                        capture_output=True, text=True, check=True
                    ).stdout.strip())
                    ratio = actual_last_sec / CHUNK_DURATION_SECONDS
                    eta_sec = median_sec_pre * ratio
                    chunk_eta = local_start + timedelta(seconds=eta_sec)
                    print(f"  > Estimated completion: {chunk_eta.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                          f"(last chunk {actual_last_sec:.0f}s / {CHUNK_DURATION_SECONDS}s nominal"
                          f" → {ratio:.2f}× median {median_sec_pre/3600:.2f}h)")
                except Exception:
                    # Probe failed — fall back to unscaled median.
                    chunk_eta = local_start + timedelta(seconds=median_sec_pre)
                    print(f"  > Estimated completion: {chunk_eta.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                          f"(median {median_sec_pre/3600:.2f}h, last-chunk size probe failed)")
            else:
                chunk_eta = local_start + timedelta(seconds=median_sec_pre)
                print(f"  > Estimated completion: {chunk_eta.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                      f"(median {median_sec_pre/3600:.2f}h)")
        else:
            print("  > Estimated completion: unknown (first chunk)")
        print(f"  > Project elapsed: {elapsed_hours:.2f}h")
        print(f"  > To stop after this chunk: touch {STOP_FILE}")

        # Track which steps were skipped so partial-resume chunks are excluded
        # from the median ETA — they represent far less work than a full chunk.
        skipped_esrgan = False
        skipped_frame_extraction = False

        if os.path.exists(final_output_file) and os.path.getsize(final_output_file) > 0:
            if is_valid_video(final_output_file):
                print("  > Chunk already processed. Skipping.")
                cleanup_intermediate_files(input_chunk, esrgan_output_file, rife_in_frames_dir, rife_out_frames_dir)
                continue
            else:
                print(
                    f"  > WARNING: {final_output_file} exists but is corrupt "
                    "(missing moov atom or no video stream). Reprocessing.")
                os.remove(final_output_file)

        if not os.path.exists(input_chunk):
            print(f"  > WARNING: Input chunk {input_chunk} missing. Skipping.")
            continue

        # --- Step 1: Run Real-ESRGAN ---
        if (os.path.exists(esrgan_output_file) and os.path.getsize(esrgan_output_file) > 0
                and not is_valid_video(esrgan_output_file)):
            print(f"  > WARNING: {esrgan_output_file} is corrupt. Deleting and reprocessing from ESRGAN.")
            os.remove(esrgan_output_file)
            safe_rmtree(esrgan_temp_work_dir)
        if not os.path.exists(esrgan_output_file) or os.path.getsize(esrgan_output_file) == 0:

            os.makedirs(esrgan_temp_work_dir, exist_ok=True)

            print("  > Pre-filtering (denoise, deblock, sharpen)...")
            prefiltered_chunk = os.path.join(esrgan_temp_work_dir, f"{chunk_name}_prefiltered.mp4")
            cmd_prefilter = [
                "ffmpeg", "-nostdin", "-y",
                "-i", input_chunk,
                "-vf", prefilter_vf_with_normalized_pts,  # reset segment PTS before the profile filters
                # near-transparent: preserves maximum detail for ESRGAN
                "-c:v", "libx264", "-threads", str(threads), "-crf", str(PREFILTER_CRF),
                "-preset", "fast",  # temporary intermediate decoded frame-by-frame: preset does not affect quality
                "-pix_fmt", "yuv444p",
                # yuv444p, not yuv420p. The master from prepare_video.sh is
                # already yuv444p; re-encoding to yuv420p here would apply a second lossy
                # chroma subsampling step before ESRGAN sees the frames. yuv444p avoids
                # that round-trip. libx264 requires pix_fmt to be stated explicitly —
                # it does not infer it from the input stream.
                # Explicit colorimetry so the ESRGAN Reader's ffmpeg decode
                # uses the correct YCbCr->RGB matrix rather than guessing from SD heuristics.
                "-colorspace", COLOR_SPACE,
                "-color_primaries", COLOR_PRIMARIES,
                "-color_trc", COLOR_TRC,
                prefiltered_chunk
            ]
            try:
                with Timer(f"{chunk_name} Pre-filter"):
                    subprocess.run(cmd_prefilter, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                print(f"\n--- ERROR: FFmpeg pre-filtering failed on {chunk_name} ---")
                print("STDOUT:", e.stdout)
                print("STDERR:", e.stderr)
                raise

            if not is_valid_video(prefiltered_chunk):
                raise RuntimeError(f"Pre-filter produced a corrupt output file: {prefiltered_chunk}")
            check_output_crf(prefiltered_chunk, expected_crf=PREFILTER_CRF,
                             label=f"{chunk_name} pre-filter intermediate", fatal=False)
            validate_final_video_timing(
                input_chunk,
                prefiltered_chunk,
                fps_multiplier=1,
                frame_tolerance=0,
            )

            print("  > Running Real-ESRGAN...")
            # Timestamp before subprocess.run() so we can see how long the gap
            # between this line and actual GPU activity in nvtop really is.
            print(f"  [Perf] inference_realesrgan_video.py launching at "
                  f"{datetime.now().astimezone().strftime('%H:%M:%S')}"
                  f" (t+{time.time() - _t_main_entry:.1f}s from pipeline start)")
            cmd_realesrgan = [
                sys.executable, "inference_realesrgan_video.py",
                "-i", prefiltered_chunk,
                "-n", REALSRGAN_MODEL,
                "-o", esrgan_temp_work_dir, "-s", str(SCALE_FACTOR),
                "--fps", source_fps_str,
                "--crf", str(active_esrgan_writer_crf),
                "--out_pix_fmt", active_esrgan_writer_pix_fmt,
            ] + DENOISE_STRENGTH_ARGS
            try:
                with Timer(f"{chunk_name} ESRGAN Inference"):
                    subprocess.run(cmd_realesrgan, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                print(f"\n--- ERROR: Real-ESRGAN failed on {chunk_name} ---")
                print("STDOUT:", e.stdout)
                print("STDERR:", e.stderr)
                raise

            mp4_files = glob.glob(os.path.join(esrgan_temp_work_dir, "*.mp4"))
            output_candidates = glob.glob(os.path.join(esrgan_temp_work_dir, "*_out.mp4"))

            if not output_candidates:
                output_candidates = [f for f in mp4_files if f != prefiltered_chunk]

            if len(output_candidates) == 1:
                os.rename(output_candidates[0], esrgan_output_file)
            elif len(output_candidates) == 0:
                raise RuntimeError(
                    f"Real-ESRGAN did not produce an output file in {esrgan_temp_work_dir}. Found files: {mp4_files}")
            else:
                raise RuntimeError(
                    f"Expected 1 output MP4, found {len(output_candidates)}. Candidates: {output_candidates}")

            safe_rmtree(esrgan_temp_work_dir)
            print(f"  > Real-ESRGAN complete: {esrgan_output_file}")
        else:
            skipped_esrgan = True
            print("  > Found existing Real-ESRGAN output, skipping ESRGAN.")

        # --- Step 2: Run RIFE (Multi-Step) or bypass ---
        if not args.no_rife:
            # This file is decoded to PNG for RIFE. Any compression or chroma
            # loss here survives the later PNG and final-encode stages.
            check_output_crf(esrgan_output_file, expected_crf=active_esrgan_writer_crf,
                             label=f"{chunk_name} ESRGAN intermediate for RIFE", fatal=True)
            check_output_pix_fmt(esrgan_output_file,
                                 expected_pix_fmt=active_esrgan_writer_pix_fmt,
                                 label=f"{chunk_name} ESRGAN intermediate for RIFE", fatal=True)
        if args.no_rife:
            # RIFE off: promote the ESRGAN output directly to the rife chunk slot.
            # All downstream logic (concat, cleanup, ETA) is undisturbed because it
            # only ever references final_output_file, never esrgan_output_file directly.
            # cleanup_intermediate_files() tolerates a missing esrgan_output_file
            # (it was renamed, not deleted) and missing frame dirs (never created).
            print("  > Skipping RIFE (off; --rife not given): promoting ESRGAN output to final chunk slot.")
            os.rename(esrgan_output_file, final_output_file)
            print(f"  > No-RIFE complete: {final_output_file}")
            # This chunk file becomes part of the final deliverable verbatim
            # (the concat below is a stream copy) -- checked after rename so
            # this runs identically whether ESRGAN ran fresh this session or
            # was found pre-existing on a resume (skipped_esrgan case).
            check_output_crf(final_output_file, expected_crf=active_esrgan_writer_crf,
                             label=f"{chunk_name} ESRGAN output (inference_realesrgan_video.py, RIFE off)",
                             fatal=True)
            check_output_pix_fmt(final_output_file, expected_pix_fmt=active_final_pix_fmt,
                                 label=f"{chunk_name} ESRGAN output (inference_realesrgan_video.py, RIFE off)",
                                 fatal=True)
            validate_final_video_timing(
                input_chunk,
                final_output_file,
                fps_multiplier=1,
                frame_tolerance=0,
            )

            # --- CRITICAL: Aggressive Cleanup (no-RIFE path) ---
            # esrgan_output_file was renamed above so it no longer exists at its
            # original path; cleanup_intermediate_files() handles that gracefully.
            # rife_in_frames_dir and rife_out_frames_dir were never created.
            cleanup_intermediate_files(input_chunk, esrgan_output_file, rife_in_frames_dir, rife_out_frames_dir)

        else:
            # Check if RIFE input frames already exist and are complete
            existing_in_frames = glob.glob(os.path.join(rife_in_frames_dir, "*.png")
                                           ) if os.path.isdir(rife_in_frames_dir) else []
            if existing_in_frames:
                skipped_frame_extraction = True
                print(f"  > Found {len(existing_in_frames)} existing RIFE input frames, skipping extraction.")
            else:
                print("  > Extracting frames for RIFE...")
                os.makedirs(rife_in_frames_dir, exist_ok=True)
                cmd_extract = [
                    "ffmpeg", "-nostdin", "-i", esrgan_output_file,
                    os.path.join(rife_in_frames_dir, "frame_%08d.png")
                ]
                # Timeout: allow 10s per expected frame plus a 120s fixed overhead.
                # Normal extraction takes 20-45s. This catches silent hangs (e.g. a
                # subprocess pipe buffer deadlock with capture_output=True) that would
                # otherwise block the pipeline indefinitely without any error output.
                # capture_output=True is intentionally kept to preserve error messages
                # on genuine failures; the timeout is the safeguard against deadlock.
                expected_frames = int(source_fps_float * CHUNK_DURATION_SECONDS)
                extraction_timeout = 120 + (expected_frames * 10)
                try:
                    with Timer(f"{chunk_name} RIFE Frame Extraction"):
                        subprocess.run(cmd_extract, check=True, capture_output=True,
                                       text=True, timeout=extraction_timeout)
                except subprocess.TimeoutExpired:
                    print(f"\n--- ERROR: FFmpeg frame extraction timed out on {chunk_name} ---")
                    print(f"    Timeout: {extraction_timeout}s "
                          f"(expected ~{expected_frames} frames at {source_fps_float:.3f} fps)")
                    print(f"    Partial frames in {rife_in_frames_dir} will be wiped on next run.")
                    print("    If this recurs, check disk I/O and available space.")
                    raise RuntimeError(f"Frame extraction timed out after {extraction_timeout}s on {chunk_name}")
                except subprocess.CalledProcessError as e:
                    print(f"\n--- ERROR: FFmpeg frame extraction failed on {chunk_name} ---")
                    print("STDOUT:", e.stdout)
                    print("STDERR:", e.stderr)
                    raise

            in_frames = glob.glob(os.path.join(rife_in_frames_dir, "*.png"))
            existing_out_frames = glob.glob(os.path.join(rife_out_frames_dir, "*.png")
                                            ) if os.path.isdir(rife_out_frames_dir) else []
            expected_out = len(in_frames) * 2
            if len(existing_out_frames) >= expected_out - 2:
                print(
                    f"  > Found {len(existing_out_frames)} existing RIFE output frames "
                    f"(expected ~{expected_out}), skipping interpolation.")
                out_frames = existing_out_frames
            else:
                if existing_out_frames:
                    print(
                        f"  > WARNING: Found only {len(existing_out_frames)}/{expected_out} RIFE output frames "
                        "(partial). Wiping and re-interpolating.")
                    safe_rmtree(rife_out_frames_dir)
                print("  > Running RIFE (directory mode)...")
                os.makedirs(rife_out_frames_dir, exist_ok=True)
                cmd_rife = [
                    RIFE_BIN,
                    "-i", rife_in_frames_dir,
                    "-o", rife_out_frames_dir,
                    "-s", "0.5"
                ]
                try:
                    with Timer(f"{chunk_name} RIFE Interpolation"):
                        subprocess.run(cmd_rife, check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError as e:
                    print(f"\n--- ERROR: RIFE failed on {chunk_name} ---")
                    print("STDOUT:", e.stdout)
                    print("STDERR:", e.stderr)
                    raise
                out_frames = glob.glob(os.path.join(rife_out_frames_dir, "*.png"))

            print("  > Verifying RIFE frame count...")
            out_frames = glob.glob(os.path.join(rife_out_frames_dir, "*.png"))

            if len(out_frames) < len(in_frames) * 2 - 2:
                raise RuntimeError(
                    f"RIFE failed to double frames! "
                    f"Input: {len(in_frames)} frames, "
                    f"Output: {len(out_frames)} frames."
                )
            print(f"  > RIFE check passed (Input: {len(in_frames)}, Output: {len(out_frames)}).")

            print("  > Encoding RIFE frames to video...")
            cmd_encode = [
                "ffmpeg", "-nostdin",
                "-framerate", str(output_fps_float),
                "-i", os.path.join(rife_out_frames_dir, "%08d.png"),
                "-c:v", "libx264",
                "-threads", str(threads),
                "-pix_fmt", active_final_pix_fmt,
                # yuv444p, not the yuv420p default. Same reasoning as the
                # no-chunk RIFE encode and inference_realesrgan_video.py's
                # Writer -- input PNG frames are full RGB, so yuv420p here
                # would halve chroma independently of CRF.
                # In distribution mode this is the one and only yuv420p/target-
                # CRF conversion; the upstream ESRGAN intermediate stays
                # CRF 16/yuv444p so RIFE receives full-chroma frames.
                "-crf", str(active_rife_encode_crf),  # --crf: this stage writes the deliverable
                "-preset", "fast",  # temporary intermediate decoded at concat: preset does not affect quality
                # Explicit colorimetry, matching the pre-filter and
                # ESRGAN-writer sites. This chunk
                # becomes part of the final deliverable verbatim (concat below
                # is a stream copy) for --rife, the same role the ESRGAN
                # writer's promoted chunk has with RIFE off, so it must carry
                # the same tags rather than relying on the final concat-mux
                # step's -c:v copy to stamp them after the fact.
                "-colorspace", COLOR_SPACE,
                "-color_primaries", COLOR_PRIMARIES,
                "-color_trc", COLOR_TRC,
                final_output_file
            ]
            try:
                with Timer(f"{chunk_name} RIFE Frame Encoding"):
                    subprocess.run(cmd_encode, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                print(f"\n--- ERROR: FFmpeg frame encoding failed on {chunk_name} ---")
                print("STDOUT:", e.stdout)
                print("STDERR:", e.stderr)
                raise

            print(f"  > RIFE complete: {final_output_file}")
            check_output_crf(final_output_file, expected_crf=active_rife_encode_crf,
                             label=f"{chunk_name} RIFE-encode output", fatal=True)
            check_output_pix_fmt(final_output_file, expected_pix_fmt=active_final_pix_fmt,
                                 label=f"{chunk_name} RIFE-encode output", fatal=True)
            validate_final_video_timing(
                input_chunk,
                final_output_file,
                fps_multiplier=2,
                frame_tolerance=2,
            )

            # --- CRITICAL: Aggressive Cleanup ---
            cleanup_intermediate_files(input_chunk, esrgan_output_file, rife_in_frames_dir, rife_out_frames_dir)

        chunk_end_time = time.time()
        duration_sec = chunk_end_time - chunk_start_time
        local_end = datetime.now().astimezone()

        # Only include full chunk timings in the median. Partial-resume chunks
        # (where ESRGAN or frame extraction was skipped) represent far less work
        # and would skew the ETA estimate significantly downward.
        is_full_chunk = not skipped_esrgan and not skipped_frame_extraction
        if is_full_chunk:
            chunk_durations.append(duration_sec)
        else:
            skipped_steps = []
            if skipped_esrgan:
                skipped_steps.append("ESRGAN")
            if skipped_frame_extraction:
                skipped_steps.append("frame extraction")
            print(f"  > Partial resume (skipped: {', '.join(skipped_steps)}) — "
                  f"chunk time excluded from median ETA.")

        # Rolling median ETA (stable across noisy chunks)
        if chunk_durations:
            median_sec = statistics.median(chunk_durations)
        else:
            median_sec = duration_sec  # fallback if no full chunks yet
        chunks_remaining = total_chunks - (i + 1)

        # Effective remaining work in full-chunk units:
        #   (chunks_remaining - 1) full chunks + last_chunk_fraction of a chunk.
        # When chunks_remaining == 0 this is 0. When the probe failed,
        # last_chunk_fraction == 1.0 so the formula reduces to chunks_remaining × median.
        if chunks_remaining > 0:
            effective_remaining = (chunks_remaining - 1) + last_chunk_fraction
        else:
            effective_remaining = 0.0
        remaining_sec = median_sec * effective_remaining
        eta_project = local_end + timedelta(seconds=remaining_sec)

        print(f"  > Chunk finished in {duration_sec:.2f} seconds.")
        print(f"  > Finished at: {local_end.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        if chunks_remaining > 0:
            next_eta = local_end + timedelta(seconds=median_sec)
            print(f"  > Next chunk ETA (median): {next_eta.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            print(f"  > Project completion ETA: {eta_project.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                  f"({effective_remaining:.2f}× median {median_sec/3600:.2f}h)")

        # --- Runtime Limit Check ---
        if args.max_runtime is not None:
            elapsed_hours = (chunk_end_time - total_start_time) / 3600
            median_hours = median_sec / 3600
            max_runtime_hours = args.max_runtime

            # Check if we should continue to the next chunk
            if i + 1 < chunks_to_process:  # Only check if there are more chunks
                projected_hours = elapsed_hours + median_hours

                print("\n  [Runtime Check]")
                print(f"    Elapsed: {elapsed_hours:.2f}h / {max_runtime_hours:.2f}h")
                print(f"    Last chunk: {duration_sec/3600:.2f}h  |  Median: {median_hours:.2f}h")
                print(f"    Projected next completion: {projected_hours:.2f}h")

                if projected_hours > max_runtime_hours:
                    print(f"\n  ⏸️  GRACEFUL SHUTDOWN: Would exceed {max_runtime_hours}h runtime limit.")
                    print(f"    Processed {i+1}/{total_chunks} chunks successfully.")
                    print(f"    Resume anytime - script will continue from chunk {i+1}.")
                    print("    Run the following command to resume:")
                    print(f"      {RECONSTRUCTED_CMD}")
                    # Remove any STOP file so it doesn't interfere with the next run
                    if os.path.exists(STOP_FILE):
                        os.remove(STOP_FILE)
                        print("    (Removed stale STOP file to prevent interference on next run)")
                    # Exit the chunk loop cleanly
                    break
                else:
                    remaining = max_runtime_hours - elapsed_hours
                    eta_finish_dt = datetime.now().astimezone() + timedelta(hours=remaining)
                    print(f"    Continuing  ({remaining:.2f}h remaining)")
                    print(f"    This run will complete at or before {eta_finish_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        # --- Graceful Stop File Check ---
        if os.path.exists(STOP_FILE):
            os.remove(STOP_FILE)
            print(f"\n  ⏸️  GRACEFUL STOP: '{STOP_FILE}' detected.")
            print(f"    Finished chunk {i+1}/{total_chunks}.")
            print(f"    Resume anytime — script will continue from chunk {i+1}.")
            print("    Run the following command to resume:")
            print(f"      {RECONSTRUCTED_CMD}")
            break

    # --- Step 3: Concatenate All Processed Chunks ---
    print("\n--- 3: Concatenating Chunks & Muxing Audio ---")

    if TEST_MODE_CHUNKS is not None:
        if chunks_to_process == 0:
            print("TEST MODE: 0 chunks processed.")
        else:
            print(f"TEST MODE: Skipping final concatenation after {chunks_to_process} chunk(s).")
            print("🎉 Test processing complete!")
        sys.exit(0)

    final_chunk_files = glob.glob(os.path.join(FINAL_CHUNKS_DIR, "*_final.mp4"))
    total_processed_chunks = len(final_chunk_files)
    print(f"Found {total_processed_chunks} processed chunks to concatenate.")

    # Check if all chunks are complete before attempting final concatenation
    if total_processed_chunks < total_chunks:
        print(f"\n⚠️  Incomplete processing: {total_processed_chunks}/{total_chunks} chunks complete.")
        print(f"   Run the following command to resume from chunk {total_processed_chunks}:")
        print(f"     {RECONSTRUCTED_CMD}")
        print(f"   Final video will be created once all {total_chunks} chunks are processed.")
        sys.exit(0)

    write_concat_list(CONCAT_FILE, [
        os.path.join(FINAL_CHUNKS_DIR, f"chunk_{i:03d}_final.mp4")
        for i in range(total_processed_chunks)
    ])

    print(f"Concatenation list created: {CONCAT_FILE}")

    # Confirm every chunk being concatenated carries no color tag of its own
    # before this step stamps COLOR_SPACE/COLOR_TRC/COLOR_PRIMARIES onto the
    # result. Checked per-chunk, not just the first: ffmpeg's concat demuxer
    # with -c:v copy is confirmed (see check_output_pix_fmt) to silently
    # accept chunks that disagree with each other, so a single check at the
    # front could miss a chunk that picked up a tag the others didn't.
    for i in range(total_processed_chunks):
        chunk_path = os.path.join(FINAL_CHUNKS_DIR, f"chunk_{i:03d}_final.mp4")
        if os.path.exists(chunk_path):
            check_input_color_tags_unset(chunk_path, label=f"Pre-concat chunk {i:03d}", fatal=True)

    # Concat: always use plain stream copy, apply SAR after via container tools
    cmd_concat = [
        "ffmpeg", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", CONCAT_FILE,
        "-i", ORIGINAL_AUDIO_FILE,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "copy",
        # Stamp colorimetry on the delivered file. This is a
        # -c:v copy concat, so these flags only write container/stream
        # metadata (no re-encode, no pixel change) -- confirmed by direct
        # test. All current sources are NTSC SD; see COLOR_* constants.
        "-colorspace", COLOR_SPACE,
        "-color_trc", COLOR_TRC,
        "-color_primaries", COLOR_PRIMARIES,
        # Do not use -shortest here. A source with a short or damaged audio
        # stream must not truncate the concatenated video timeline. Matroska
        # and MP4 both permit the audio stream to end before the video stream.
    ]
    # See matching no-chunk-path comment.
    cmd_concat += ["-movflags", "+faststart"]
    cmd_concat.append(FINAL_VIDEO_FILE)

    print("Running final concatenation and audio muxing...")
    try:
        subprocess.run(cmd_concat, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print("\n--- ERROR: Final concatenation failed ---")
        print("STDOUT:", e.stdout)
        print("STDERR:", e.stderr)
        print("If this is a 'non-monotonic DTS' error, change -c:v copy to -c:v libx264 in the script and re-run.")
        raise

    # Apply SAR to final output using container-native tools (no re-encode)
    if source_sar:
        out_width, out_height = get_video_dimensions(FINAL_VIDEO_FILE)
        print(f"Applying SAR {source_sar} to final output (pixel {out_width}x{out_height} -> "
              f"display {display_aspect_label(out_width, out_height, source_sar)})...")
        apply_sar_to_file(FINAL_VIDEO_FILE, source_sar, out_width, out_height)
    else:
        print("Source has square pixels (SAR 1:1) — no SAR correction needed.")

    # Final confirmation on the actual delivered file. NOTE: this is a
    # multi-chunk concat, and get_embedded_x264_crf() finds the FIRST crf=
    # string in the file's raw bytes -- it confirms the first chunk's encode
    # settings, not every individual chunk's. Sufficient to catch a systemic
    # misconfiguration (the kind that motivated adding this check at all:
    # every chunk sharing the same code path and therefore the same mistake),
    # not a guarantee that no single chunk differs from the rest.
    #
    # pix_fmt consistency across ALL chunks (not just the first) is actually
    # guaranteed upstream: the per-chunk checks at the no-rife promotion site
    # and the RIFE-encode site (above, fatal=True) already verify every
    # single chunk before it is ever allowed into 2_final_chunks/, so by the
    # time concat runs, every input to it has already been confirmed
    # individually. This final check confirms the delivered file itself
    # (ffprobe reports the container's first-stream pix_fmt, same as for
    # CRF) rather than assuming the mux step didn't silently alter anything.
    check_output_crf(FINAL_VIDEO_FILE, expected_crf=final_deliverable_crf,
                     label="Final delivered output (first chunk's settings)", fatal=True)
    check_output_pix_fmt(FINAL_VIDEO_FILE, expected_pix_fmt=active_final_pix_fmt,
                         label="Final delivered output", fatal=True)
    check_output_color_tags(FINAL_VIDEO_FILE, label="Final delivered output", fatal=True)
    check_output_presentation(
        FINAL_VIDEO_FILE, label="Final delivered output", full_decode=args.verify_full_decode)
    validate_final_video_timing(
        INPUT_VIDEO,
        FINAL_VIDEO_FILE,
        source_measurements=SOURCE_MEASUREMENTS,
        fps_multiplier=(1 if args.no_rife else 2),
        # RIFE's endpoint handling can add or drop pictures at each chunk's
        # ends, so each chunk may differ by two in either direction.
        # No-RIFE is ordinal-preserving and must match exactly: any tolerance
        # here is enough to hide the MP4 leading-duplicate defect.
        frame_tolerance=(0 if args.no_rife else max(2, total_chunks * 2)),
    )
    if SOURCE_MEASUREMENTS["audio_present"]:
        verify_audio_priming(
            FINAL_VIDEO_FILE, SOURCE_MEASUREMENTS.get("audio_priming_samples"),
            "Final delivered output", reference=ORIGINAL_AUDIO_FILE, force=args.force)
    verify_final_av_sync(
        FINAL_VIDEO_FILE, probe_video_timing(FINAL_VIDEO_FILE)["fps"],
        encoder_delay=SOURCE_MEASUREMENTS.get("audio_priming_samples"),
        force=args.force)

    # --- Final Cleanup ---
    total_end_time = time.time()
    total_duration = total_end_time - total_start_time

    hours = int(total_duration // 3600)
    minutes = int((total_duration % 3600) // 60)
    seconds = int(total_duration % 60)
    print(f"\nTotal processing time: {hours:02d}:{minutes:02d}:{seconds:02d}")
    print("--- 4. Final Cleanup ---")
    finalize_processing_dir(PROCESSING_DIR, ORIGINAL_AUDIO_FILE, LOG_DIR)

    finish_run(owns_run, FINAL_VIDEO_FILE)

    print(f"\n🎉 Pipeline complete! Final file: {FINAL_VIDEO_FILE}")
    return 0


class TerminalClosed(BaseException):
    """The terminal running the pipeline was closed (SIGHUP).

    A BaseException, like KeyboardInterrupt, so that the `except Exception`
    recovery in the helpers cannot swallow it: the handler ignores further
    hangups once it has fired, and a swallowed one would leave the run going
    with nothing able to stop it that way again.
    """


def _terminal_closed(signum, frame):
    # Ignored from here on, so a second hangup cannot cut the report short.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    raise TerminalClosed()


def report_stopped(headline, *advice):
    """Say why the run stopped and how to resume it; pipeline.log keeps it."""
    print(f"\n\n{headline}")
    print("    Any chunk currently in progress has been abandoned.")
    print("    Completed chunks are safe and will be resumed automatically.")
    print("    On next run the pipeline will skip completed chunks and")
    print("    reprocess the interrupted chunk from the beginning.")
    for line in advice:
        print(f"    {line}")
    print(f"    Resume command:\n      {_resume_cmd}")


def cli_main(argv=None):
    """Run the command-line interface and return a process exit status."""
    args = parse_arguments(argv)
    # Closing the terminal stops the run the way Ctrl+C does, with the reason
    # and the resume command in pipeline.log, rather than killing it
    # mid-write. A run meant to outlive its window belongs in tmux or screen.
    signal.signal(signal.SIGHUP, _terminal_closed)
    # Module-level variable so the KeyboardInterrupt handler can print the
    # resume command even though RECONSTRUCTED_CMD is built inside main().
    global _resume_cmd
    command_argv = sys.argv if argv is None else [sys.argv[0], *argv]
    _resume_cmd = " ".join(shlex.quote(arg) for arg in command_argv)
    try:
        return main(args)
    except subprocess.CalledProcessError:
        print("\nA critical command failed. Exiting.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        report_stopped("⏸️  INTERRUPTED: Ctrl+C detected.")
        return 130
    except TerminalClosed:
        report_stopped("⏹️  STOPPED: the terminal was closed (SIGHUP).",
                       "To keep a run going after its window closes, start it in tmux or screen.")
        return 129


if __name__ == "__main__":
    raise SystemExit(cli_main())
