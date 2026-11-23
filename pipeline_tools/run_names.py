#!/usr/bin/env python3
"""Source labels and manifest-time ordering shared by AI run entry points."""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path


def clean_label(label):
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9._-]", "_", label)).strip("_") or "run"


def source_stem(source, seen=()):
    """Prefer a prepared master's recorded capture, then its generated suffix."""
    source = Path(source)
    stem = source.stem
    marker = "_master_from_prepare_video"
    if not stem.endswith(marker) or str(source) in seen:
        return stem
    run = (source.parent.parent.parent
           if source.parent.name == "masters" and source.parent.parent.name == "work"
           else source.parent)
    for record, key in ((run / "run.json", "sources"),
                        (run / "logs/audio_timing_plan.json", "input")):
        try:
            value = json.loads(record.read_text()).get(key)
            capture = value[0] if isinstance(value, list) and len(value) == 1 else value
            if isinstance(capture, str) and capture != str(source):
                return source_stem(capture, (*seen, str(source)))
        except (OSError, ValueError, AttributeError):
            pass
    return re.sub(r"_mask\d+_(?:30fps|60fps)(?:_(?:\d{8}_\d{6}|TEST))?$",
                  "", stem[:-len(marker)])


def runs_newest_first(root):
    """Order physical run directories by started as an instant, never by label.

    Invalid/missing timestamps cannot establish recency and are skipped with
    a warning. An explicit --run-dir can still inspect such a run.
    """
    rows = []
    root = Path(root)
    if not root.is_dir():
        return []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir() or not (path / "run.json").is_file():
            continue
        try:
            record = json.loads((path / "run.json").read_text())
            started = datetime.fromisoformat(record["started"].replace("Z", "+00:00"))
            if started.tzinfo is None:
                raise ValueError("started must include its time zone")
            rows.append((started.timestamp(), path.name, path))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            print(f"[WARNING] Cannot order {path}: {exc}; use --run-dir explicitly",
                  file=sys.stderr)
    return [path for _, _, path in sorted(rows, reverse=True)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    for path in runs_newest_first(args.root):
        print(path)


if __name__ == "__main__":
    main()
