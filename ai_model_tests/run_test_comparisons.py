#!/usr/bin/env python3
"""Run one prepared test clip through matched Real-ESRGAN model choices."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


DEFAULT_MODELS = ("RealESRGAN_x2plus", "realesr-general-x4v3")
PRESERVATION_ROOT = Path(
    os.environ.get("VIDEO_PRESERVATION_ROOT", Path.home() / "VideoPreservation")
)
RUN_ROOT = PRESERVATION_ROOT / "ai"


def check_venv() -> None:
    if sys.prefix == sys.base_prefix and "VIRTUAL_ENV" not in os.environ:
        raise SystemExit(
            "[ERROR] Activate the repository environment first: source venv/bin/activate"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare AI models while holding scale, profile, and encoding constant."
    )
    parser.add_argument("test_clip", type=Path)
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--scale", type=int, choices=(2, 4), default=2)
    parser.add_argument(
        "--profile",
        choices=(
            "balanced", "aggressive", "halo", "dv", "hi8dv", "vhsdv",
            "vhsdv_nosharp", "vhsdv_lightdenoise", "vhsdv_composite",
        ),
        default="balanced",
    )
    parser.add_argument(
        "--run-dir", type=Path, default=None,
        help="Run directory for this comparison (default: a new one under "
             "~/VideoPreservation/ai/). Every model's output, log and working "
             "file goes inside it. See pipeline_docs/storage-layout.md.",
    )
    parser.add_argument(
        "--crf", type=int, default=None, metavar="CRF",
        help="Quality for each model's output. Default: the pipeline's own "
             "default, recorded in the manifest once it is known.",
    )
    parser.add_argument("--force", action="store_true", help="Discard stale pipeline work")
    return parser.parse_args()


def make_run_id(label: str) -> str:
    """<label>_<YYYYMMDD-HHMMSS>, grouping comparisons of the same clip."""
    clean = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9._-]", "_", label)).strip("_")
    return f"{clean or 'run'}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def write_manifest(run_dir: Path, settings: dict, clip: Path,
                   finished: str | None, videos: list[str] | None = None) -> None:
    """Write run.json: what was compared, under what conditions, and what came out.

    The settings held constant across the models are the actual finding of a
    model comparison, so they are recorded rather than left to be recovered
    from the pipeline logs. Written through a temporary file and a rename so an
    interrupted comparison leaves a complete manifest or none.
    """
    path = run_dir / "run.json"
    started = finished
    if path.is_file():
        try:
            started = json.loads(path.read_text()).get("started")
        except (OSError, json.JSONDecodeError):
            started = None
    manifest = {
        "run_id": run_dir.name,
        "class": "model-test",
        "entry_point": "run_test_comparisons.py",
        "preservation_root": str(PRESERVATION_ROOT),
        "started": started or datetime.now().astimezone().isoformat(timespec="seconds"),
        "finished": finished,
        "command": sys.argv,
        "settings": settings,
        "sources": [str(clip.resolve())],
        "videos": videos or [],
        "excluded": [],
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    tmp.replace(path)


def run_dir_conflict(run_dir: Path, settings: dict, clip: Path) -> str | None:
    """Why run_dir cannot hold this comparison, or None when it can.

    write_manifest replaces run.json, so a directory that already holds
    anything must be this same comparison left unfinished: a model-test
    manifest for the same clip and settings. Any other run -- a batch, a tape
    transfer, a finished comparison -- keeps its manifest.
    """
    if not run_dir.exists():
        return None
    if not run_dir.is_dir():
        return "it is not a directory"
    if not any(run_dir.iterdir()):
        return None
    try:
        manifest = json.loads((run_dir / "run.json").read_text())
    except FileNotFoundError:
        return "it is not empty and holds no run.json"
    except (OSError, json.JSONDecodeError) as exc:
        return f"its run.json cannot be read ({exc})"
    if not isinstance(manifest, dict):
        return "its run.json is not a run manifest"
    if (manifest.get("class") != "model-test"
            or manifest.get("entry_point") != "run_test_comparisons.py"):
        return (f"it is a {manifest.get('class') or 'unknown'} run made by "
                f"{manifest.get('entry_point') or 'an unknown tool'}, not a model comparison")
    if manifest.get("finished"):
        return f"that comparison finished at {manifest['finished']}"
    if manifest.get("sources") != [str(clip)]:
        return f"that comparison is of {', '.join(manifest.get('sources') or ['nothing recorded'])}"
    if manifest.get("settings") != settings:
        return (f"that comparison used {json.dumps(manifest.get('settings'), sort_keys=True)}, "
                f"not {json.dumps(settings, sort_keys=True)}")
    return None


def result_snapshot(output_dir: Path, stem: str) -> dict[Path, tuple[int, int]]:
    """Return size/mtime identity for every matching media output."""
    return {
        path: (stat.st_size, stat.st_mtime_ns)
        for path in output_dir.glob(f"{stem}_*")
        if path.suffix.lower() in {".mkv", ".mp4"} and (stat := path.stat())
    }


def current_result(
    output_dir: Path, stem: str, before: dict[Path, tuple[int, int]]
) -> Path | None:
    after = result_snapshot(output_dir, stem)
    changed = [path for path, identity in after.items() if before.get(path) != identity]
    return max(changed, key=lambda path: path.stat().st_mtime_ns, default=None)


def main() -> int:
    # Line-buffered even into a pipe: each model's pipeline writes straight to
    # the inherited descriptor, so lines held here would land after its output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    check_venv()
    if not args.test_clip.is_file():
        print(f"[ERROR] Test clip not found: {args.test_clip}", file=sys.stderr)
        return 2

    # Resolved here, after the check that reports what the user typed and before
    # anything creates a directory or launches a subprocess. The pipeline runs
    # with cwd=repo_root, so a relative path that is valid in the caller's
    # directory points somewhere else by the time the pipeline reads it -- and
    # the failure surfaces as "the prepared master is absent", which describes
    # neither the cause nor the path involved.
    args.test_clip = args.test_clip.resolve()
    if args.run_dir is not None:
        args.run_dir = args.run_dir.resolve()

    repo_root = Path(__file__).resolve().parent.parent
    pipeline = repo_root / "video_upscale_pipeline.py"
    if not pipeline.is_file():
        print(f"[ERROR] Pipeline not found: {pipeline}", file=sys.stderr)
        return 2

    # One run directory for the whole comparison. A model test is a run like
    # any other: its result is the set -- the clip, each model's output, and
    # the settings held constant across them -- and that set is only meaningful
    # together. See pipeline_docs/storage-layout.md.
    run_dir = args.run_dir or (RUN_ROOT / make_run_id(f"model-test_{args.test_clip.stem}"))
    settings = {
        "scale": args.scale,
        "profile": args.profile,
        "models": list(args.models),
        "crf": args.crf,
    }
    conflict = run_dir_conflict(run_dir, settings, args.test_clip)
    if conflict:
        print(f"[ERROR] --run-dir {run_dir} cannot hold this comparison: {conflict}.\n"
              "        Choose a new directory, or omit --run-dir to create one.",
              file=sys.stderr)
        return 2
    videos_dir = run_dir / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(run_dir, settings, args.test_clip, finished=None)
    print(f"Run directory: {run_dir}")

    results: list[tuple[str, Path]] = []
    for model in args.models:
        # Every model writes into the run's own videos/ directory. The model
        # name is part of the pipeline's output filename, so the outputs sit
        # side by side without colliding -- which is what makes them
        # comparable at a glance.
        command = [
            sys.executable, str(pipeline), str(args.test_clip),
            "--scale", str(args.scale), "--profile", args.profile,
            "--model", model,
            "--run-dir", str(run_dir),
            "--log-dir", str(run_dir / "logs" / model.replace("/", "_")),
        ]
        if args.crf is not None:
            command.extend(("--crf", str(args.crf)))
        if args.force:
            command.append("--force")

        print(f"\n=== Model: {model} ===", flush=True)
        before = result_snapshot(videos_dir, args.test_clip.stem)
        try:
            subprocess.run(command, cwd=repo_root, check=True)
        except subprocess.CalledProcessError as exc:
            print(
                f"[ERROR] Pipeline failed for {model} with status {exc.returncode}.",
                file=sys.stderr,
            )
            return 1
        result = current_result(videos_dir, args.test_clip.stem, before)
        if result is None:
            print(
                f"[ERROR] Pipeline completed but produced no new or changed output for {model}.",
                file=sys.stderr,
            )
            return 1
        results.append((model, result))

    write_manifest(
        run_dir, settings, args.test_clip,
        finished=datetime.now().astimezone().isoformat(timespec="seconds"),
        videos=[r.name for _, r in results],
    )
    # The readable summary, from the same manifest and the same renderer every
    # other entry point uses.
    renderer = repo_root / "pipeline_tools" / "write_run_md.py"
    try:
        subprocess.run([sys.executable, str(renderer), str(run_dir)],
                       check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[!] could not write run.md: {exc}", file=sys.stderr)

    print("\nMatched model outputs:")
    for model, result in results:
        print(f"  {model}: {result}")
    if len(results) == 2:
        compare = Path(__file__).resolve().parent / "compare_test_results.sh"
        print("\nCreate a labelled comparison grid with:")
        print(
            f"  {compare} {args.test_clip} {results[0][1]} {results[1][1]} "
            f"--label-a {results[0][0]} --label-b {results[1][0]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
