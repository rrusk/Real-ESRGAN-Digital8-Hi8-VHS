#!/usr/bin/env python3
"""Restore the AAC encoder delay missing from delivered and saved run audio.

A deliverable assembled from a Matroska copy of its master's audio carries no
AAC encoder delay: ffmpeg 4.4 writes none into Matroska for AAC, and the final
MP4 mux copies the packets without it. A player then decodes the encoder's
start-up block as programme audio, and the whole soundtrack plays one block --
1024 samples, 21.33 ms at 48 kHz -- late. An unfinished run holds the same
defect in its saved .mka, which a resume would mux onto the finished video.

The repair is a stream copy. The AAC is taken out as raw ADTS, which carries no
timestamps, and muxed back one block earlier, so the MP4 writes an edit list
that discards that block. Picture and audio packets stay byte for byte the
same; only the audio's timing and the container around it are rewritten.

No file is changed on the strength of its metadata. Its decoded audio is
aligned against the source the run prepared it from, through the timing plan
the preparation recorded, and only a file measured exactly one encoder delay
late at every probe is rebuilt. The rebuilt copy must measure zero, carry the
same packets, and pass the pipeline's own delivered-file checks before it
takes its final name. Repaired deliverables are written beside nothing they
replace: they go under the output directory as <run>/videos/<file>, mirroring
the archive, with a <file>.repair.json report each. Each directory the
originals came from gets its own <directory>.SHA256SUMS listing only its
repairs, so the copies put back there can be checked without reporting files
that belong somewhere else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

__version__ = "1.0.0"

# Probe positions as fractions of the audio, and how long each one is. Three
# spread over the whole file show the shift is constant rather than drift, and
# twenty seconds of programme audio correlates unambiguously.
PROBE_FRACTIONS = (0.05, 0.5, 0.95)
PROBE_SECONDS = 20
# Lags searched either side of zero: room for the one-block shift and well
# past it, short of where programme audio could repeat itself convincingly.
SEARCH_SAMPLES = 4096
# A probe counts only where the source has programme audio and the two
# decodes plainly match; near-silence correlates with anything.
MIN_CORRELATION = 0.9
MIN_RMS = 1e-3
# Needed beyond the repaired copy itself before one is written.
SPACE_MARGIN_BYTES = 1 << 30
REPORT_SUFFIX = ".repair.json"
MANIFEST_SUFFIX = ".SHA256SUMS"
STATE_BACKUP_SUFFIX = ".before-aac-priming"

vup = None   # video_upscale_pipeline, imported once dependencies are checked
np = None


class NeedsReview(RuntimeError):
    """The file is not the defect this tool repairs, or cannot be shown to be."""


class RepairError(RuntimeError):
    """A repair or its verification failed."""


class ArgumentParser(argparse.ArgumentParser):
    def format_help(self):
        return super().format_help().replace("usage:", "Usage:", 1)

    def format_usage(self):
        return super().format_usage().replace("usage:", "Usage:", 1)

    def error(self, message):
        self.exit(2, f"[ERROR] {message}\n"
                     f"Try '{self.prog} --help' for more information.\n")


def default_output_dir():
    root = os.environ.get("VIDEO_PRESERVATION_ROOT") or os.path.expanduser(
        "~/VideoPreservation")
    return Path(root) / "repairs" / "aac_priming"


def build_parser():
    parser = ArgumentParser(
        prog=Path(sys.argv[0]).name,
        usage=("%(prog)s [OPTIONS] PATH...\n"
               "       %(prog)s [OPTIONS] --run-audio RUN_DIR"),
        description=(
            "Description:\n"
            "  Find Real-ESRGAN deliverables whose AAC soundtrack plays one encoder\n"
            "  delay (1024 samples) late, and write repaired copies with the delay\n"
            "  restored. Each file's decoded audio is aligned against its source\n"
            "  before and after; picture and audio packets are copied unchanged.\n"
            "  With --run-audio, repair an unfinished run's saved source audio and\n"
            "  record it in the run's resume state instead.\n"
            "  Without --apply nothing is written."),
        epilog=(
            "Examples:\n"
            "  %(prog)s /media/archive/enhanced/ai\n"
            "  %(prog)s --apply /media/archive/enhanced/ai\n"
            "  %(prog)s --apply --run-audio ~/VideoPreservation/ai/<run-id>\n\n"
            "Dependencies:\n"
            "  ffmpeg and ffprobe; numpy (run it with the repository venv)"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    arguments = parser.add_argument_group("Arguments")
    arguments.add_argument(
        "paths", metavar="PATH", nargs="*",
        help="a delivered MP4, a run directory, or a directory of runs")
    options = parser.add_argument_group("Options")
    options.add_argument(
        "--output-dir", metavar="DIR", type=Path, default=None,
        help=("where repaired deliverables are written, as <run>/videos/<file> "
              f"(default: {default_output_dir()})"))
    options.add_argument(
        "--run-audio", metavar="RUN_DIR", type=Path,
        help="repair an unfinished run's saved source audio for resume")
    options.add_argument(
        "--apply", action="store_true",
        help="write repairs; without it, measure and report only")
    options.add_argument(
        "--force", action="store_true",
        help="replace a repaired copy that already exists")
    options.add_argument("-h", "--help", action="help", help="show this help and exit")
    options.add_argument("-V", "--version", action="version",
                         version="%(prog)s " + __version__,
                         help="show version and exit")
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.run_audio and args.paths:
        parser.error("--run-audio takes no PATH arguments")
    if args.run_audio and args.output_dir:
        parser.error("--run-audio writes into the run; --output-dir does not apply")
    if not args.run_audio and not args.paths:
        parser.error("missing PATH")
    for path in args.paths:
        if not os.path.exists(path):
            parser.error(f"no such file or directory: {path}")
    if args.run_audio and not args.run_audio.is_dir():
        parser.error(f"not a directory: {args.run_audio}")
    return args


def load_dependencies():
    """Import what measuring needs, once --help and argument errors are done."""
    global vup, np
    for command in ("ffmpeg", "ffprobe"):
        if shutil.which(command) is None:
            raise RepairError(f"required command not found: {command}")
    try:
        import numpy
    except ImportError as exc:
        raise RepairError(
            "numpy not found; run this with the repository venv's python") from exc
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import video_upscale_pipeline
    vup, np = video_upscale_pipeline, numpy


def run(command, **kwargs):
    """Run a command, raising RepairError with its stderr if it fails."""
    result = subprocess.run(command, capture_output=True, **kwargs)
    if result.returncode != 0:
        stderr = result.stderr if isinstance(result.stderr, str) else \
            result.stderr.decode(errors="replace")
        raise RepairError(f"{command[0]} failed: {stderr.strip()[-2000:]}")
    return result


# --- measurement -----------------------------------------------------------

def load_plan(run_dir):
    """The run's audio timing plan, which ties its audio to source samples."""
    path = Path(run_dir) / "logs" / "audio_timing_plan.json"
    try:
        plan = json.loads(path.read_text())
    except FileNotFoundError:
        raise NeedsReview(
            f"no audio timing plan at {path}, so the audio cannot be aligned "
            f"against its source") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise NeedsReview(f"unreadable audio timing plan {path}: {exc}") from None
    needed = ("input", "trim_samples", "pad_samples", "sample_rate")
    missing = [key for key in needed if key not in plan]
    if missing:
        raise NeedsReview(f"timing plan {path} lacks {', '.join(missing)}")
    if not plan.get("audio_present", True) or plan.get("copy"):
        raise NeedsReview(f"timing plan {path} records no encoded audio")
    if not os.path.isfile(plan["input"]):
        raise NeedsReview(f"the plan's source is not readable: {plan['input']}")
    plan["path"] = str(path)
    return plan


def inspect_media(path, want_video):
    """Stream layout and audio facts; one AAC stream, and one picture if wanted.

    The alignment is measured by decoded sample, which is where a player puts
    each sample only while the audio starts at or before zero: a skipped
    encoder delay, or none. Audio that starts after zero is placed later by
    its timestamps than its samples say, so it is refused rather than misread.
    """
    probe = run(["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type,codec_name,sample_rate,time_base", "-of", "json",
                 str(path)], text=True)
    streams = json.loads(probe.stdout).get("streams", [])
    kinds = [stream.get("codec_type") for stream in streams]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    layout = ", ".join(f"{s.get('codec_type')}:{s.get('codec_name')}" for s in streams)
    if (len(audio) != 1 or audio[0].get("codec_name") != "aac"
            or kinds.count("video") != (1 if want_video else 0)
            or len(streams) != len(audio) + kinds.count("video")):
        expected = "one video and one AAC audio stream" if want_video \
            else "one AAC audio stream"
        raise NeedsReview(f"expected {expected}; found {layout or 'none'}")
    rate = int(audio[0]["sample_rate"])
    first = run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                 "packet=pts", "-read_intervals", "%+#1", "-of", "csv=p=0",
                 str(path)], text=True).stdout.split()
    try:
        start = round(int(first[0]) * Fraction(audio[0]["time_base"]) * rate)
    except (IndexError, ValueError, ZeroDivisionError):
        raise NeedsReview("the audio's first packet has no readable timestamp") from None
    if start > 0:
        raise NeedsReview(f"the audio starts {start} samples after zero; only audio "
                          f"starting at or before zero can be aligned by sample")
    return {"sample_rate": rate, "start": start,
            "priming": vup.audio_priming_samples(str(path))}


def probe_starts(seconds, rate):
    """Probe length and start samples, kept inside the audio."""
    length = int(min(PROBE_SECONDS, seconds / 4) * rate)
    usable = int(seconds * rate) - length - SEARCH_SAMPLES
    if length < rate or usable <= 0:
        raise NeedsReview(f"{seconds:.1f}s of audio is too short to align")
    return length, [int(fraction * usable) for fraction in PROBE_FRACTIONS]


def decode_spans(path, starts, length):
    """Mono float spans of path's audio as a player decodes it, by sample."""
    labels = [f"s{i}" for i in range(len(starts))]
    graph = ("[0:a:0]asetpts=N/SR/TB,"
             "aformat=sample_fmts=flt:channel_layouts=mono,"
             f"asplit={len(starts)}" + "".join(f"[{label}]" for label in labels))
    for label, start in zip(labels, starts):
        graph += (f";[{label}]atrim=start_sample={start}:"
                  f"end_sample={start + length}[o{label}]")
    with tempfile.TemporaryDirectory(prefix="aac-priming-") as tmp:
        command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                   "-filter_complex", graph]
        outputs = [os.path.join(tmp, f"{label}.f32") for label in labels]
        for label, output in zip(labels, outputs):
            command += ["-map", f"[o{label}]", "-f", "f32le", output]
        run(command)
        return [np.fromfile(output, dtype=np.float32).astype(np.float64)
                for output in outputs]


def lag_of(reference, candidate):
    """Samples by which candidate trails reference, and the correlation there."""
    n = min(len(reference), len(candidate))
    ref = reference[:n] - reference[:n].mean()
    cand = candidate[:n] - candidate[:n].mean()
    size = 1 << (2 * n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(cand, size) * np.conj(np.fft.rfft(ref, size)), size)
    lags = np.concatenate([np.arange(0, SEARCH_SAMPLES + 1),
                           np.arange(-SEARCH_SAMPLES, 0)])
    values = np.concatenate([corr[:SEARCH_SAMPLES + 1], corr[-SEARCH_SAMPLES:]])
    best = int(np.argmax(values))
    energy = np.sqrt((ref ** 2).sum() * (cand ** 2).sum())
    return int(lags[best]), float(values[best] / energy) if energy else 0.0


class Alignment:
    """Where a file's decoded audio sits against its source, at each probe.

    The source spans are decoded once and kept, so the repaired copy is
    measured against exactly what the original was.
    """

    def __init__(self, plan, media, seconds, rate):
        self.rate = rate
        self.length, self.starts = probe_starts(seconds, rate)
        offset = int(plan["trim_samples"]) - int(plan["pad_samples"])
        if self.starts[0] + offset < 0:
            raise NeedsReview("the first probe would start before the source audio")
        self.source = decode_spans(plan["input"], [s + offset for s in self.starts],
                                   self.length)
        self.before = self.measure(media)

    def measure(self, media):
        probes = []
        for start, source, ours in zip(self.starts, self.source,
                                       decode_spans(media, self.starts, self.length)):
            complete = len(source) == len(ours) == self.length
            lag, correlation = lag_of(source, ours) if complete else (None, 0.0)
            rms = float(np.sqrt((source ** 2).mean())) if len(source) else 0.0
            probes.append({
                "seconds": round(start / self.rate, 3),
                "lag_samples": lag,
                "correlation": round(correlation, 4),
                "source_rms": round(rms, 5),
                "conclusive": (
                    complete and correlation >= MIN_CORRELATION
                    and rms >= MIN_RMS
                ),
            })
        return probes

    @staticmethod
    def verdict(probes):
        """The one lag every conclusive probe agrees on; NeedsReview otherwise."""
        conclusive = [p for p in probes if p["conclusive"]]
        if len(conclusive) < 2:
            raise NeedsReview(
                f"only {len(conclusive)} of {len(probes)} probes found programme "
                f"audio that matches the source closely enough to align")
        lags = sorted({p["lag_samples"] for p in conclusive})
        if len(lags) != 1:
            raise NeedsReview(f"the probes disagree: lags of {lags} samples")
        return lags[0]

    def describe(self, probes):
        for p in probes:
            lag = p["lag_samples"]
            where = "unmeasured" if lag is None else (
                "aligned" if lag == 0 else
                f"{abs(lag)} samples {'late' if lag > 0 else 'early'} "
                f"({abs(lag) * 1000 / self.rate:.2f} ms)")
            note = "" if p["conclusive"] else "  (inconclusive)"
            print(f"    at {p['seconds']:9.3f}s: {where}, correlation "
                  f"{p['correlation']:.3f}{note}")


def stream_hashes(path, maps):
    """SHA-256 of each mapped stream's packets, in map order."""
    command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path)]
    for stream in maps:
        command += ["-map", stream]
    command += ["-c", "copy", "-f", "streamhash", "-hash", "sha256", "-"]
    lines = run(command, text=True).stdout.split()
    return [line.split(",", 2)[2] for line in lines]


def container_facts(path):
    """Chapters and global tags, minus those the MP4 writer sets itself."""
    probe = run(["ffprobe", "-v", "error", "-show_chapters", "-show_entries",
                 "format_tags", "-of", "json", str(path)], text=True)
    data = json.loads(probe.stdout)
    tags = {key: value for key, value in data.get("format", {}).get("tags", {}).items()
            if key not in ("major_brand", "minor_version", "compatible_brands", "encoder")}
    chapters = [(c.get("start_time"), c.get("end_time"), c.get("tags", {}).get("title"))
                for c in data.get("chapters", [])]
    return {"tags": tags, "chapters": chapters}


def file_identity(path):
    stat = os.stat(path)
    return {"size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()}


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 24), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# --- the repair ------------------------------------------------------------

def restore_delay(original, adts, output, samples, rate, with_video):
    """Remux original with its AAC moved `samples` earlier from raw ADTS."""
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(original),
         "-map", "0:a:0", "-c", "copy", "-f", "adts", str(adts)])
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    if with_video:
        command += ["-i", str(original)]
    command += ["-itsoffset", f"-{samples / rate:.9f}", "-i", str(adts)]
    audio_input = 1 if with_video else 0
    if with_video:
        command += ["-map", "0:v:0"]
    command += ["-map", f"{audio_input}:a:0", "-c", "copy"]
    if with_video:
        command += ["-map_metadata", "0", "-map_metadata:s:a:0", "0:s:a:0",
                    "-map_chapters", "0", "-movflags", "+faststart"]
    command.append(str(output))
    run(command)


def check_restored(original, candidate, alignment, samples, label, with_video):
    """Everything a repaired copy must show before it takes its final name."""
    vup.verify_audio_priming(str(candidate), samples, label)
    maps = ["0:v:0", "0:a:0"] if with_video else ["0:a:0"]
    before, after = stream_hashes(original, maps), stream_hashes(candidate, maps)
    if before != after:
        raise RepairError(f"{label}: packets differ from the original's "
                          f"({before} vs {after})")
    kinds = "picture and audio packets" if with_video else "audio packets"
    print(f"  [packet check] {label}: {kinds} identical to the original's.")
    probes = alignment.measure(candidate)
    alignment.describe(probes)
    if Alignment.verdict(probes) != 0:
        raise RepairError(f"{label}: the repaired audio is still not aligned "
                          f"with its source")
    if ([p["seconds"] for p in probes if p["conclusive"]]
            != [p["seconds"] for p in alignment.before if p["conclusive"]]):
        raise RepairError(f"{label}: the repaired copy is conclusive at different "
                          f"probes from the original")
    print(f"  [alignment check] {label}: aligned with the source at every probe.")
    if not with_video:
        return probes

    timing = vup.probe_video_timing(str(original))
    repaired = vup.probe_video_timing(str(candidate))
    if (timing["frames"], timing["fps"]) != (repaired["frames"], repaired["fps"]) \
            or abs(timing["duration"] - repaired["duration"]) > 1e-6:
        raise RepairError(f"{label}: picture timing changed: {timing} -> {repaired}")
    print(f"  [timing check] {label}: {repaired['frames']} pictures, timing unchanged.")
    sar = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=sample_aspect_ratio,pix_fmt", "-of", "json", str(original)],
              text=True)
    stream = json.loads(sar.stdout)["streams"][0]
    width, height = vup.get_video_dimensions(str(candidate))
    vup.verify_output_sar(str(candidate), stream["sample_aspect_ratio"], width, height)
    vup.check_output_pix_fmt(str(candidate), stream["pix_fmt"], label, fatal=True)
    vup.check_output_color_tags(str(candidate), label, fatal=True)
    vup.check_output_presentation(str(candidate), label)
    vup.verify_final_av_sync(str(candidate), repaired["fps"], encoder_delay=samples)
    if container_facts(original) != container_facts(candidate):
        raise RepairError(f"{label}: chapters or tags differ: "
                          f"{container_facts(original)} vs {container_facts(candidate)}")
    return probes


def assess(path, plan, want_video):
    """Measure a file; return (info, alignment, lag) or raise NeedsReview."""
    info = inspect_media(path, want_video)
    if info["sample_rate"] != int(plan["sample_rate"]):
        raise NeedsReview(f"audio is {info['sample_rate']} Hz; the plan's source "
                          f"is {plan['sample_rate']} Hz")
    seconds = vup.measure_audio_seconds(str(path))
    if not seconds:
        raise NeedsReview("the audio's length cannot be measured")
    print(f"  > Aligning against {plan['input']} ...", flush=True)
    alignment = Alignment(plan, path, seconds, info["sample_rate"])
    alignment.describe(alignment.before)
    lag = Alignment.verdict(alignment.before)
    return info, alignment, lag


def classify(info, lag):
    """None when the file is aligned; NeedsReview unless it is this defect."""
    delay = vup.AAC_ENCODER_DELAY_SAMPLES
    if lag == 0:
        return None
    if lag == delay and info["priming"] == 0:
        return delay
    raise NeedsReview(
        f"{lag} samples late while discarding {info['priming']}; this tool restores "
        f"a lost {delay}-sample encoder delay only")


def existing_repair(mp4, target, report, out_root):
    """Accept a copy already at target only if its report vouches for it.

    A file at the target proves nothing by being there: an interrupted copy, a
    hand-placed file or a later edit would all be taken for a finished repair.
    The report must describe this target, the target must match the size and
    checksum the report recorded, and the original must still be the file the
    repair was made from -- or have been replaced by the repair itself.
    Returns the detail for an accepted repair; raises NeedsReview otherwise.
    """
    relative = str(target.relative_to(out_root))
    try:
        recorded = json.loads(report.read_text())
        repaired, original = recorded["repaired"], recorded["original"]
    except FileNotFoundError:
        raise NeedsReview(f"{target} exists with no {report.name}, so it is not a "
                          f"verified repair; --force replaces it") from None
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise NeedsReview(f"{report} cannot be read ({exc}); --force replaces "
                          f"{target.name}") from None
    if repaired.get("path") != relative:
        raise NeedsReview(f"{report.name} describes {repaired.get('path')}, not "
                          f"{relative}; --force replaces it")
    if (os.path.getsize(target) != repaired.get("size")
            or sha256_of(target) != repaired.get("sha256")):
        raise NeedsReview(f"{target} does not match the size and checksum its report "
                          f"recorded; --force replaces it")
    current = file_identity(mp4)
    if (current["size"], current["mtime"]) == (original.get("size"), original.get("mtime")):
        return f"{target} matches its report and checksum"
    if current["size"] == repaired["size"] and sha256_of(mp4) == repaired["sha256"]:
        return f"{mp4} has already been replaced by its verified repair"
    raise NeedsReview(f"{mp4} has changed since {target.name} was made from it; "
                      f"--force repairs it again")


def repair_deliverable(mp4, out_root, apply, force):
    run_dir = mp4.parent.parent
    target = out_root / run_dir.name / "videos" / mp4.name
    report = target.with_name(target.name + REPORT_SUFFIX)
    if target.exists() and not force:
        return "already repaired", existing_repair(mp4, target, report, out_root)
    plan = load_plan(run_dir)
    info, alignment, lag = assess(mp4, plan, want_video=True)
    samples = classify(info, lag)
    if samples is None:
        return "aligned", f"discards {info['priming']} samples; aligned with the source"
    if not apply:
        return "would repair", f"restore {samples} samples -> {target}"

    original_identity = file_identity(mp4)
    target.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target.parent).free
    if free < original_identity["size"] + SPACE_MARGIN_BYTES:
        raise RepairError(f"{free / 1e9:.1f} GB free at {target.parent}; "
                          f"{original_identity['size'] / 1e9:.1f} GB needed")
    work = Path(tempfile.mkdtemp(prefix=".aac-priming-", dir=target.parent))
    try:
        candidate = work / mp4.name
        print("  > Writing the repaired copy ...", flush=True)
        restore_delay(mp4, work / "audio.aac", candidate, samples,
                      info["sample_rate"], with_video=True)
        after = check_restored(mp4, candidate, alignment, samples,
                               "Repaired copy", with_video=True)
        if file_identity(mp4) != original_identity:
            raise RepairError(f"{mp4} changed while it was being repaired")
        os.chmod(candidate, 0o644)
        os.replace(candidate, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    write_json(report, {
        "tool": f"repair_aac_priming.py {__version__}",
        "repaired_at": datetime.now().astimezone().isoformat(),
        "restored_delay_samples": samples,
        "source": plan["input"],
        "timing_plan": plan["path"],
        "original": {"path": str(mp4), **original_identity,
                     "priming_samples": info["priming"],
                     "alignment": alignment.before},
        "repaired": {"path": str(target.relative_to(out_root)), **file_identity(target),
                     "sha256": sha256_of(target),
                     "priming_samples": vup.audio_priming_samples(str(target)),
                     "alignment": after},
    })
    return "repaired", str(target)


def find_state(run_dir):
    found = sorted(Path(run_dir).glob("work/*/source_measurements.json"))
    if len(found) != 1:
        raise NeedsReview(f"expected one work/*/source_measurements.json in "
                          f"{run_dir}; found {len(found)}")
    return found[0]


def repair_run_audio(run_dir, apply, force):
    """Rebuild a run's saved .mka as an .m4a and record it in the resume state."""
    state_path = find_state(run_dir)
    state = json.loads(state_path.read_text())
    if not state.get("audio_present"):
        return "aligned", "the saved state records no audio"
    if "audio_priming_samples" in state:
        return "aligned", (f"the saved audio already records its encoder delay "
                           f"({state['audio_priming_samples']} samples)")
    if os.path.exists(state["input_video"]):
        raise NeedsReview(f"the master is still present ({state['input_video']}); "
                          f"a resume re-extracts its audio with the delay intact")
    audio = Path(state["audio_file"])
    if audio.suffix != ".mka" or not audio.is_file():
        raise NeedsReview(f"expected the saved audio as an existing .mka: {audio}")
    target = audio.with_suffix(".m4a")
    if target.exists() and not force:
        raise NeedsReview(f"{target} exists but the resume state does not use it, so "
                          f"it is not a finished repair; --force rebuilds it")
    plan = load_plan(run_dir)
    info, alignment, lag = assess(audio, plan, want_video=False)
    samples = classify(info, lag)
    if samples is None:
        raise NeedsReview("the saved audio is aligned but records no encoder delay")
    if not apply:
        return "would repair", (f"restore {samples} samples -> {target}, and "
                                f"record it in {state_path}")

    work = Path(tempfile.mkdtemp(prefix=".aac-priming-", dir=target.parent))
    try:
        candidate = work / target.name
        restore_delay(audio, work / "audio.aac", candidate, samples,
                      info["sample_rate"], with_video=False)
        check_restored(audio, candidate, alignment, samples, "Repaired run audio",
                       with_video=False)
        os.chmod(candidate, 0o644)
        os.replace(candidate, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    backup = state_path.with_name(state_path.name + STATE_BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(state_path, backup)
    state.update({
        "audio_file": str(target),
        "audio_duration": vup.get_video_duration(str(target)),
        "audio_priming_samples": samples,
    })
    vup.record_source_measurements(str(state_path.parent), state)
    return "repaired", (f"{target}; resume state updated ({backup.name} keeps the "
                        f"previous one; {audio.name} is no longer used)")


def find_deliverables(paths, out_root):
    """MP4s under videos/ directories, or named directly; each file once."""
    found = {}
    skip = os.path.realpath(out_root)
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            candidates = [path]
        else:
            candidates = []
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                if os.path.realpath(root).startswith(skip + os.sep):
                    continue
                if Path(root).name == "videos":
                    candidates += [Path(root) / f for f in sorted(files)
                                   if f.endswith(".mp4") and not f.startswith(".")]
        for candidate in candidates:
            found.setdefault(os.path.realpath(candidate), candidate)
    return [found[key] for key in sorted(found)]


def manifest_path(out_root, origin):
    """Where the checksums for repairs going back to origin are listed."""
    return out_root / (str(origin).strip(os.sep).replace(os.sep, "_") + MANIFEST_SUFFIX)


def write_manifests(out_root):
    """One SHA256SUMS per directory of runs the originals came from.

    Paths are relative to that directory -- <run>/videos/<file>, the layout the
    repairs mirror -- so a manifest checks the copies put back there, or on a
    mirror with the same layout, and lists nothing that belongs elsewhere.
    Manifests are derived entirely from the reports and rewritten each time.
    Returns {origin: manifest}.
    """
    groups = {}
    for report in sorted(out_root.glob(f"*/videos/*{REPORT_SUFFIX}")):
        try:
            recorded = json.loads(report.read_text())
            origin = Path(recorded["original"]["path"]).parent.parent.parent
            line = f"{recorded['repaired']['sha256']}  {recorded['repaired']['path']}\n"
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"[WARNING] {report} left out of the checksums: {exc}", file=sys.stderr)
            continue
        groups.setdefault(origin, []).append(line)
    written = {}
    for origin, lines in sorted(groups.items()):
        manifest = manifest_path(out_root, origin)
        tmp = manifest.with_name(manifest.name + ".tmp")
        tmp.write_text("".join(lines))
        os.chmod(tmp, 0o644)
        os.replace(tmp, manifest)
        written[origin] = manifest
    for stale in out_root.glob(f"*{MANIFEST_SUFFIX}"):
        if stale not in written.values():
            stale.unlink()
    return written


def main(argv=None):
    args = parse_args(argv)
    try:
        load_dependencies()
    except RepairError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    out_root = (args.output_dir or default_output_dir()).expanduser()
    if args.run_audio:
        jobs = [(args.run_audio, lambda p: repair_run_audio(p, args.apply, args.force))]
    else:
        jobs = [(p, lambda p: repair_deliverable(p, out_root, args.apply, args.force))
                for p in find_deliverables(args.paths, out_root)]
        if not jobs:
            print("[ERROR] no delivered MP4s found under the given paths", file=sys.stderr)
            return 1
    mode = "Repairing" if args.apply else "Dry run (nothing is written)"
    print(f"[INFO] {mode}: {len(jobs)} item(s).")
    if not args.run_audio:
        print(f"[INFO] Repaired copies: {out_root}")

    results = []
    manifests = {}
    try:
        for path, job in jobs:
            print(f"\n== {path}", flush=True)
            try:
                status, detail = job(path)
            except NeedsReview as exc:
                status, detail = "needs review", str(exc)
            except (RepairError, RuntimeError, OSError, ValueError,
                    subprocess.SubprocessError) as exc:
                status, detail = "failed", str(exc)
            stream = sys.stderr if status in ("needs review", "failed") else sys.stdout
            level = {"needs review": "[WARNING]", "failed": "[ERROR]"}.get(status, "[INFO]")
            print(f"{level} {status}: {detail}", file=stream, flush=True)
            results.append((path, status, detail))
    except KeyboardInterrupt:
        print("\n[ERROR] interrupted; no partial copy was kept", file=sys.stderr)
        return 130
    finally:
        if args.apply and not args.run_audio and out_root.is_dir():
            manifests = write_manifests(out_root)

    print("\n[INFO] Summary:")
    for status in ("repaired", "would repair", "aligned", "already repaired",
                   "needs review", "failed"):
        items = [r for r in results if r[1] == status]
        if items:
            print(f"  {status}: {len(items)}")
    for path, status, detail in results:
        if status in ("needs review", "failed"):
            print(f"  [{status}] {path}\n      {detail}")
    for origin, manifest in manifests.items():
        print(f"[INFO] Checksums for the copies going back to {origin}:\n"
              f"  cd {shlex.quote(str(origin))} && sha256sum -c "
              f"{shlex.quote(str(manifest))}")
    return 1 if any(status == "failed" for _, status, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
