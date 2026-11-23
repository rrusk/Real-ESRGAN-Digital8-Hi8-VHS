#!/usr/bin/env python3
"""Repair the installed BasicSR's imports against newer dependencies.

WHY THIS EXISTS

basicsr 1.4.2 is the last release (2022) and is effectively unmaintained, so
its imports name modules that its dependencies have since moved or removed.
Each one breaks `import basicsr` outright once the removal lands, and with it
every Real-ESRGAN entry point.

torchvision: `basicsr/data/degradations.py` opens with

    from torchvision.transforms.functional_tensor import rgb_to_grayscale

`torchvision.transforms.functional_tensor` was a private module that torchvision
removed in 0.17. The function itself still exists, in the public
`torchvision.transforms.functional`, so the fix is a one-line import change.

This is not optional here. `basicsr/data/__init__.py` scans its directory and
imports every `*_dataset.py` it finds, and those reach `degradations`, so the
failure is not confined to code that wants degradations: plain `import basicsr`
raises ModuleNotFoundError, and with it every Real-ESRGAN entry point.

The validated environment uses torchvision 0.26. This patch keeps BasicSR's
imports compatible with that stack without downgrading its dependencies.

scipy: `basicsr/metrics/niqe.py` imports `convolve` from
`scipy.ndimage.filters`, a namespace deprecated since SciPy 1.8 and scheduled
for removal in SciPy 2.0. `scipy.ndimage.convolve` IS the same function object
-- verified with `is` -- so this is a namespace change and nothing else. Left
alone it becomes the torchvision failure again when SciPy 2.0 arrives, because
`basicsr/metrics/__init__.py` imports niqe, so `import basicsr` reaches it.

Run it after `pip install -r requirements.txt` and before anything else. It is
idempotent: running it twice is harmless, and it refuses to guess if a file
does not look like the version it knows how to fix.

USAGE
    python dev_tools/patch_basicsr.py            apply the patch if needed
    python dev_tools/patch_basicsr.py --check    report status, change nothing

EXIT STATUS
    0  already patched, or patched successfully
    1  basicsr not found, or its source is not what this script expects
    2  command-line error
"""
import argparse
import sys
import sysconfig
from pathlib import Path

# Each entry: the file inside the basicsr package, the import as shipped, the
# import to write, and what breaks if it is left alone. Adding a fix means
# adding a row -- the apply/check/refuse-to-guess behaviour is shared.
PATCHES = (
    {
        "path": ("data", "degradations.py"),
        "stock": "from torchvision.transforms.functional_tensor import rgb_to_grayscale",
        "fixed": "from torchvision.transforms.functional import rgb_to_grayscale",
        "why": "torchvision >= 0.17 removed transforms.functional_tensor",
    },
    {
        "path": ("metrics", "niqe.py"),
        "stock": "from scipy.ndimage.filters import convolve",
        "fixed": "from scipy.ndimage import convolve",
        "why": "scipy.ndimage.filters is deprecated and goes away in SciPy 2.0",
    },
)


def find_in_basicsr(*parts):
    """Locate a file inside the installed basicsr WITHOUT importing basicsr.

    Importing it is what fails when a patch is missing, so the whole point of
    this script would be lost if finding the file needed the import to work.
    """
    roots = []
    for name in ("purelib", "platlib"):
        path = sysconfig.get_paths().get(name)
        if path:
            roots.append(Path(path))
    roots.extend(Path(p) for p in sys.path if p)
    seen = set()
    for root in roots:
        candidate = root.joinpath("basicsr", *parts)
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def main():
    parser = argparse.ArgumentParser(
        prog="patch_basicsr.py",
        description="Repair BasicSR's imports against newer torchvision and SciPy.")
    parser.add_argument("--check", action="store_true",
                        help="report whether the patch is needed; change nothing")
    args = parser.parse_args()

    status = 0
    for patch in PATCHES:
        target = find_in_basicsr(*patch["path"])
        if target is None:
            print("[ERROR] basicsr is not installed in this interpreter.", file=sys.stderr)
            print("        Activate the venv and run: pip install -r requirements.txt",
                  file=sys.stderr)
            return 1

        source = target.read_text(encoding="utf-8")
        if patch["fixed"] in source:
            print(f"[OK] Already patched: {target}")
            continue
        if patch["stock"] not in source:
            # Neither form present: a different basicsr than this script knows.
            # Rewriting it blindly could break an installation that was fine.
            print(f"[ERROR] Unexpected basicsr source: {target}", file=sys.stderr)
            print("        Neither the stock import nor the patched one was found, so", file=sys.stderr)
            print("        this script will not modify it. Check the basicsr version;", file=sys.stderr)
            print("        requirements.txt pins basicsr==1.4.2.", file=sys.stderr)
            status = 1
            continue

        if args.check:
            print(f"[NEEDS PATCH] {target}  ({patch['why']})")
            print(f"    {patch['stock']}")
            print(f" -> {patch['fixed']}")
            continue

        target.write_text(source.replace(patch["stock"], patch["fixed"]),
                          encoding="utf-8")
        print(f"[PATCHED] {target}  ({patch['why']})")
        print(f"    {patch['stock']}")
        print(f" -> {patch['fixed']}")
    return status


if __name__ == "__main__":
    sys.exit(main())
