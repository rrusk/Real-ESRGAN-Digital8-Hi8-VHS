#!/usr/bin/env python3
"""Verify one of the GPU/Python environments validated for this repository.

Version reporting alone is not verification. `pip install -r requirements.txt`
installs a basicsr whose torchvision import was removed in torchvision 0.17, so
`metadata.version("basicsr")` answers "1.4.2" for an installation that cannot be
imported at all. This script therefore IMPORTS what it reports on: a green
result means the pipeline can actually load, not merely that the right version
strings are on disk. See dev_tools/patch_basicsr.py.
"""

import argparse
import importlib
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path


def running_in_venv():
    """True when this interpreter is a virtual environment's own.

    The same test video_upscale_pipeline.py's check_venv() uses, so the two
    tools agree about what counts as the project environment.
    """
    return sys.prefix != sys.base_prefix or "VIRTUAL_ENV" in os.environ


def report_wrong_interpreter():
    """Report the interpreter before inspecting packages.

    User-site packages can differ from the project's virtual environment;
    inspecting them would give misleading repair advice."""
    print("--- Real-ESRGAN Environment Verification ---")
    print("[FAIL] Not running inside a virtual environment.")
    print(f"       Interpreter: {sys.executable}")
    user_site = os.path.expanduser("~/.local/lib")
    shadowing = [p for p in sys.path if p.startswith(user_site)]
    if shadowing:
        print(f"       {len(shadowing)} user-site path(s) under {user_site} are on")
        print("       sys.path and will shadow the project's packages.")
    print("       Nothing below this line would describe the project environment,")
    print("       so no further checks were run.")
    print()
    print("       Run it with the environment's own interpreter:")
    print("         venv/bin/python verify_env.py")
    print("       or activate first:")
    print("         source venv/bin/activate && ./verify_env.py")
    return 1


COMMON_PACKAGES = {
    "numpy": "1.25.0",
    "basicsr": "1.4.2",
    "opencv-python": "4.9.0.80",
}

VALIDATED_STACKS = (
    {
        "name": "current RTX 4060 Ti / CUDA 12.8",
        "packages": {
            "torch": "2.11.0",
            "torchvision": "0.26.0",
            "torchaudio": "2.11.0",
        },
        "cuda": "12.8",
    },
    {
        "name": "legacy GTX 1060 / CUDA 11.7",
        "packages": {
            "torch": "2.0.1",
            "torchvision": "0.15.2",
            "torchaudio": "2.0.2",
        },
        "cuda": "11.7",
    },
)


def public_version(version):
    """Strip a wheel's local suffix, e.g. 2.11.0+cu128 -> 2.11.0."""
    return version.split("+", 1)[0]


def installed_version(package):
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def read_nvidia_smi():
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


# Imported, not merely version-checked. Each entry is (module, what a failure
# means, what to do about it).
IMPORT_CHECKS = (
    ("basicsr",
     "BasicSR cannot be imported",
     "python dev_tools/patch_basicsr.py     "
     "(basicsr 1.4.2 imports a torchvision module removed in 0.17)"),
    ("realesrgan",
     "the Real-ESRGAN package itself cannot be imported",
     "python -m pip install -e . --no-deps  (from the repository root)"),
)

# The pipeline resolves models from weights/ by filename. Absent weights are not
# an installation fault, so they are reported without failing the run.
EXPECTED_WEIGHTS = (
    "RealESRGAN_x2plus.pth",
    "RealESRGAN_x4plus.pth",
    "realesr-general-x4v3.pth",
    "realesr-general-wdn-x4v3.pth",
)


def verify_imports():
    """True when every required module imports. Prints an action on failure."""
    ok = True
    for module, meaning, remedy in IMPORT_CHECKS:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - any failure is a failure
            print(f"[FAIL] import {module}: {type(exc).__name__}: {exc}")
            print(f"       {meaning}. Fix with:")
            print(f"       {remedy}")
            ok = False
        else:
            print(f"[OK]   import {module}")
    return ok


def report_weights():
    """Report model availability. Never fails the run: weights are a download."""
    weights = Path(__file__).resolve().parent / "weights"
    present = [name for name in EXPECTED_WEIGHTS if (weights / name).is_file()]
    missing = [name for name in EXPECTED_WEIGHTS if name not in present]
    for name in present:
        print(f"[OK]   weights/{name}")
    if missing:
        print(f"[NOTE] {len(missing)} model file(s) not downloaded: "
              + ", ".join(missing))
        print("       Fetch them with: dev_tools/fetch_models.sh")


def verify_environment():
    # Before anything else: every check below reports on THIS interpreter, so
    # running under the wrong one produces a confident, detailed and entirely
    # misleading report.
    if not running_in_venv():
        return report_wrong_interpreter()

    print("--- Real-ESRGAN Environment Verification ---")
    ok = True

    installed = {}
    package_names = set(COMMON_PACKAGES)
    for stack in VALIDATED_STACKS:
        package_names.update(stack["packages"])

    for package in sorted(package_names):
        version = installed_version(package)
        installed[package] = version
        if version is None:
            print(f"[MISS] {package}: not installed")
            ok = False
        else:
            print(f"[INFO] {package}: {version}")

    for package, expected in COMMON_PACKAGES.items():
        actual = installed.get(package)
        if actual is not None and public_version(actual) != expected:
            print(f"[FAIL] {package}: expected {expected}, found {actual}")
            ok = False

    matched_stack = None
    for stack in VALIDATED_STACKS:
        if all(
            installed.get(package) is not None
            and public_version(installed[package]) == expected
            for package, expected in stack["packages"].items()
        ):
            matched_stack = stack
            break

    if matched_stack is None:
        expected = " or ".join(stack["name"] for stack in VALIDATED_STACKS)
        print(f"[FAIL] PyTorch packages do not match a validated stack: {expected}")
        ok = False
    else:
        print(f"[OK]   Validated stack: {matched_stack['name']}")

    try:
        import torch
    except ImportError:
        torch = None

    if torch is not None:
        runtime = torch.version.cuda
        if matched_stack is not None and runtime != matched_stack["cuda"]:
            print(
                f"[FAIL] PyTorch CUDA runtime: expected {matched_stack['cuda']}, "
                f"found {runtime or 'CPU-only build'}"
            )
            ok = False
        elif runtime:
            print(f"[OK]   PyTorch CUDA runtime: {runtime}")

        if not torch.cuda.is_available():
            print("[FAIL] CUDA is not available to PyTorch")
            ok = False
        else:
            print(f"[OK]   CUDA device: {torch.cuda.get_device_name(0)}")

    if not verify_imports():
        ok = False

    report_weights()

    gpu_status = read_nvidia_smi()
    if gpu_status is None:
        print("[FAIL] nvidia-smi could not query the NVIDIA driver")
        ok = False
    else:
        for line in gpu_status.splitlines():
            print(f"[OK]   NVIDIA GPU/driver: {line}")

    print("--------------------------------------------")
    if not ok:
        print("Environment does not match a validated GPU configuration.")
        print("Recreate the environment using the installation steps in README.md.")
        return 1

    print("Environment matches a validated GPU configuration.")
    return 0


VERSION = "1.0.0"


def parse_arguments(argv=None):
    """Parse help and options before importing or checking the environment."""
    parser = argparse.ArgumentParser(
        prog="verify_env.py",
        description="Verify one of the GPU/Python environments validated for "
                    "this repository. Imports what it reports on, so a green "
                    "result means the pipeline can actually load.",
        epilog="Exit status: 0 the environment matches a validated stack, "
               "1 it does not.",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"verify_env.py {VERSION}")
    return parser.parse_args(argv)


if __name__ == "__main__":
    parse_arguments()
    raise SystemExit(verify_environment())
