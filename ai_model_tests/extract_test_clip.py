#!/usr/bin/env python3
"""Extract a short, high-quality clip for matched AI model comparisons."""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PRESERVATION_ROOT = Path(
    os.environ.get("VIDEO_PRESERVATION_ROOT", Path.home() / "VideoPreservation")
)
DEFAULT_OUTPUT_DIR = PRESERVATION_ROOT / "ai"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract a short clip from a prepared progressive master. The clip "
            "is encoded losslessly so every model receives the same input."
        ),
        epilog="""Examples:
  %(prog)s master.mp4 00:05:00
  %(prog)s master.mkv 754.5 --duration 20 --output-dir /path/to/tests
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="Prepared progressive master")
    parser.add_argument("start", help="Start timestamp (seconds or HH:MM:SS[.mmm])")
    parser.add_argument("-d", "--duration", type=float, default=10.0)
    parser.add_argument("-o", "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("-f", "--force", action="store_true", help="replace a colliding output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        print(f"[ERROR] Input file not found: {args.input}", file=sys.stderr)
        return 2
    if args.duration <= 0:
        print("[ERROR] --duration must be greater than zero.", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    safe_start = args.start.replace(":", "-").replace(".", "-")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output_dir / (
        f"{args.input.stem}_start{safe_start}_dur{args.duration:g}s_{timestamp}.mkv"
    )
    if output.exists() and not args.force:
        print(f"[ERROR] Output already exists: {output}", file=sys.stderr)
        return 1

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
        "-ss", args.start, "-i", str(args.input), "-t", str(args.duration),
        "-map", "0:v:0", "-map", "0:a?", "-c:v", "ffv1", "-level", "3",
        "-coder", "1", "-context", "1", "-g", "1", "-c:a", "copy", str(output),
    ]
    command.insert(1, "-y" if args.force else "-n")

    print("Extracting matched model-test input:")
    print(f"  Source:   {args.input}")
    print(f"  Start:    {args.start}")
    print(f"  Duration: {args.duration:g}s")
    print(f"  Output:   {output}")
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError:
        print("[ERROR] ffmpeg was not found.", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] ffmpeg failed with status {exc.returncode}.", file=sys.stderr)
        return 1

    print(f"Created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
