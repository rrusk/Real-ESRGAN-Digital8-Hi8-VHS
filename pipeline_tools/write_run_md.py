#!/usr/bin/env python3
"""Render a run's run.json into run.md, the copy a person can read.

Every run directory carries its settings twice: run.json for tools and run.md
for whoever opens the drive years from now with no access to this repository.
Both come from here, from the same source, so they cannot drift apart and say
different things about the same run -- and no entry point has to carry its own
copy of the wording.

A machine-readable file on its own is not a record. Someone finding a directory
of video and a JSON blob has no way to tell what the videos are or whether the
directory can be deleted, and will eventually decide it is junk.

Usage:
  write_run_md.py RUN_DIR...     write RUN_DIR/run.md from RUN_DIR/run.json
  write_run_md.py --stdout DIR   print it instead of writing
"""

import json
import os
import sys

CLASS_DESCRIPTION = {
    "digital8": "Digital8 tape captures, upscaled with Real-ESRGAN. Each video\n"
                "is one recording day: every capture from that day joined into a\n"
                "single file whose chapter marks carry each clip's recording time.",
    "hi8-dv": (
        "A Hi8 analog tape, transferred to DV and upscaled with\n"
        "Real-ESRGAN."
    ),
    "other": "A video upscaled with Real-ESRGAN.",
    "model-test": "A model comparison: one test clip through several Real-ESRGAN\n"
                  "models with every other setting held constant. The videos are\n"
                  "evidence for a decision, not deliverables -- they are only\n"
                  "meaningful side by side.",
}

MODEL_NAMES = {
    "x2plus": "RealESRGAN_x2plus",
    "x4plus": "RealESRGAN_x4plus",
    "gen-x4v3": "realesr-general-x4v3",
}

SETTING_LABELS = {
    "scale": "Scale", "mode": "Output mode", "crf": "Video CRF",
    "profile": "Pre-filter profile", "rife": "Frame interpolation",
    "model": "ESRGAN model", "models": "ESRGAN models",
    "input_root": "Source directory",
    "time_source": "Recording time source",
}


def _human_size(num):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024 or unit == "TiB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


def _setting_value(key, value):
    if key == "rife":
        return "RIFE" if value in (True, "true") else "none"
    if key == "scale":
        return f"{value}x"
    if key == "model":
        return MODEL_NAMES.get(value, value)
    if key == "models":
        return ", ".join(MODEL_NAMES.get(m, m) for m in value)
    if key == "crf" and value in ("", None):
        return "(pipeline default)"
    return str(value)


def render(run_dir):
    with open(os.path.join(run_dir, "run.json")) as handle:
        run = json.load(handle)

    out = []
    add = out.append
    run_id = run.get("run_id", os.path.basename(run_dir.rstrip("/")))
    add(f"# {run_id}\n")
    add(CLASS_DESCRIPTION.get(run.get("class"), CLASS_DESCRIPTION["other"]) + "\n")

    started, finished = run.get("started"), run.get("finished")
    if finished:
        add(f"Run started {started} and finished {finished}.\n")
    else:
        add(f"Run started {started}. **It has not finished** -- either it is still\n"
            f"running, or it stopped part-way through and can be resumed by\n"
            f"re-running the same command.\n")

    videos_dir = os.path.join(run_dir, "videos")
    add("## Videos\n")
    videos = run.get("videos") or []
    if videos:
        add("| File | Size |")
        add("|---|---|")
        for name in videos:
            path = os.path.join(videos_dir, name)
            size = _human_size(os.path.getsize(path)) if os.path.isfile(path) else "missing"
            add(f"| `{name}` | {size} |")
        add("")
    else:
        add("This run produced no video.\n")

    settings = run.get("settings") or {}
    if settings:
        add("## Settings\n")
        add("Every video above was produced with these settings.\n")
        add("| Setting | Value |")
        add("|---|---|")
        for key, value in settings.items():
            add(f"| {SETTING_LABELS.get(key, key)} | {_setting_value(key, value)} |")
        add("")

    sources = run.get("sources") or []
    if sources:
        add("## Sources\n")
        add(f"{len(sources)} capture(s) went into this run:\n")
        for path in sources[:50]:
            add(f"- `{path}`")
        if len(sources) > 50:
            add(f"- ...and {len(sources) - 50} more, listed in `run.json`.")
        add("")

    excluded = run.get("excluded") or []
    if excluded:
        add("## Captures left out\n")
        add("| Capture | Reason |")
        add("|---|---|")
        for item in excluded:
            add(f"| `{os.path.basename(item.get('source', '?'))}` | {item.get('reason', '?')} |")
        add("")

    conflicts = run.get("metadata_conflicts") or []
    if conflicts:
        add("## Captures whose recording time could not be confirmed\n")
        add("The tape's own metadata sources disagreed. The run manifest records "
            "whether each capture was excluded or resolved from other evidence.\n")
        for item in conflicts:
            add(f"- `{os.path.basename(item.get('source', '?'))}` -- {item.get('detail', '')}")
        add("")
    if run.get("subcode_cross_check"):
        add(f"Recording times were taken from filenames alone: "
            f"{run['subcode_cross_check']}.\n")

    # Only what is actually present is described. A run has whichever of these
    # its entry point produced -- a single-tape run has no driver log, a run
    # with nothing to resume has no state/ -- and listing a path that is not
    # there sends the reader looking for a file that was never written.
    add("## What else is in this directory\n")
    add("| Path | Holds |")
    add("|---|---|")
    present = [
        ("videos/", "The videos listed above."),
        ("logs/", None),  # description depends on the shape; filled in below
        ("run.json", "The same facts as this file, for tools to read."),
        ("run.log", "The driver's log, start to finish, across any resumptions."),
        ("state/", "Resume ledger. Keep it as long as the run is kept."),
        ("work/", "Working files. Its presence means the run stopped part-way."),
    ]
    logs = os.path.join(run_dir, "logs")
    per_source = os.path.isdir(logs) and any(
        os.path.isdir(os.path.join(logs, e)) for e in os.listdir(logs))
    for name, description in present:
        if not os.path.exists(os.path.join(run_dir, name.rstrip("/"))):
            continue
        if name == "logs/":
            description = ("One directory per source, with everything recorded "
                           "about processing it.") if per_source else \
                          ("Everything recorded about processing this run's "
                           "source.")
        add(f"| `{name}` | {description} |")
    add("")
    add("This directory is the whole run. Copying it copies everything: nothing\n"
        "the run produced was written anywhere else.\n")
    return "\n".join(out) + "\n"


def write(run_dir):
    path = os.path.join(run_dir, "run.md")
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(render(run_dir))
    os.replace(tmp, path)
    return path


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    to_stdout = argv[0] == "--stdout"
    if to_stdout:
        argv = argv[1:]
    if not argv:
        print("[ERROR] a run directory is required", file=sys.stderr)
        return 2
    for run_dir in argv:
        if not os.path.isfile(os.path.join(run_dir, "run.json")):
            print(f"[ERROR] no run.json in {run_dir}", file=sys.stderr)
            return 1
        if to_stdout:
            sys.stdout.write(render(run_dir))
        else:
            print(write(run_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
