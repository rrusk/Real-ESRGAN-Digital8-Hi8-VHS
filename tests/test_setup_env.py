"""dev_tools/setup_env.sh deletes a directory only when it is a replaceable venv.

--force removes the --venv directory recursively, so its guard and the order of
its checks are tested here. The refusals run under --dry-run, which validates
and exits before anything is removed. Interpreter-refusal tests use failing
stubs; build-order tests record package commands with a stand-in interpreter.
Neither kind of test downloads or installs packages.
"""
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "dev_tools" / "setup_env.sh"
OLD_PYTHON = '#!/bin/sh\n[ "$1" = --version ] && { echo "Python 3.8.10"; exit 0; }\nexit 1\n'


def setup(*args, env=None):
    return subprocess.run(["bash", str(SETUP), *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def venv(path, python=None):
    (path / "bin").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\n")
    if python is not None:
        stub(path / "bin", "python", python)
    return path


def stub(directory, name, script):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(script)
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path):
    """PATH whose python3 is 3.8 and whose required tools are present."""
    bin_dir = tmp_path / "host-bin"
    for name in ("ffmpeg", "ffprobe", "curl", "jq", "MP4Box", "mkvmerge", "mediainfo"):
        stub(bin_dir, name, "#!/bin/sh\nexit 0\n")
    stub(bin_dir, "nvidia-smi", "#!/bin/sh\necho 'Test GPU, 0'\n")
    stub(bin_dir, "python3", OLD_PYTHON)
    return {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"}


@pytest.mark.parametrize("target", ["/", str(ROOT)], ids=["root", "repository"])
def test_force_refuses_root_and_repository(target):
    result = setup("--dry-run", "--force", "--venv", target)
    assert result.returncode == 2
    assert "--force will not remove" in result.stderr


def test_force_refuses_a_directory_that_is_not_a_venv(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "keep.txt").write_text("mine")
    result = setup("--dry-run", "--force", "--venv", str(tmp_path / "data"))
    assert result.returncode == 2
    assert "holds no pyvenv.cfg" in result.stderr


def test_force_refuses_a_venv_that_contains_home(tmp_path):
    outer = venv(tmp_path / "outer")
    (outer / "home").mkdir()
    result = setup("--dry-run", "--force", "--venv", str(outer), env={"HOME": str(outer / "home")})
    assert result.returncode == 2
    assert "is or contains" in result.stderr


def test_force_accepts_a_venv(tmp_path):
    result = setup("--dry-run", "--force", "--venv", str(venv(tmp_path / "venv")))
    assert result.returncode == 0, result.stderr
    assert "(removing the existing one)" in result.stdout


def test_old_python3_leaves_the_forced_venv_in_place(tmp_path, host):
    existing = venv(tmp_path / "venv")
    result = setup("--force", "--skip-models", "--venv", str(existing), env=host)
    assert result.returncode == 1
    assert "Python 3.10 or 3.11 is required" in result.stderr
    assert (existing / "pyvenv.cfg").is_file()
    assert "Removing existing" not in result.stdout


def test_old_venv_interpreter_is_refused_before_pip(tmp_path, host):
    existing = venv(tmp_path / "venv", python=OLD_PYTHON)
    result = setup("--skip-models", "--venv", str(existing), env=host)
    assert result.returncode == 1
    assert f"{existing}/bin/python is Python 3.8.10" in result.stderr
    assert "Installing Python packages" not in result.stdout


@pytest.mark.parametrize("version", [(3, 9), (3, 12), (3, 14)])
def test_unsupported_python_leaves_forced_venv_in_place(tmp_path, host, version):
    """Execute the real version predicate against unsupported interpreter versions."""
    import shlex
    import sys

    host_bin = Path(host["PATH"].split(os.pathsep)[0])
    program = f"import sys; sys.version_info = {version!r}; exec(sys.argv[1])"
    stub(host_bin, "selected-python", (
        "#!/bin/sh\n"
        f"[ \"$1\" = --version ] && {{ echo 'Python {version[0]}.{version[1]}'; exit 0; }}\n"
        f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)} \"$2\"\n"
    ))
    existing = venv(tmp_path / "venv")
    result = setup("--force", "--python", "selected-python", "--venv", str(existing), env=host)
    assert result.returncode == 1
    assert "Python 3.10 or 3.11 is required" in result.stderr
    assert (existing / "pyvenv.cfg").is_file()
    assert "Removing existing" not in result.stdout


@pytest.mark.parametrize("build_fails", [False, True])
def test_basicsr_uses_installed_stack_without_an_isolated_build(tmp_path, host, build_fails):
    """Bootstrap before building, and never retry a failed build with isolation."""
    interpreter = f"#!{sys.executable}\n" + '''import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("Python 3.10.12")
elif args[0] == "-c":
    exec(args[1])
elif args[:2] == ["-m", "pip"]:
    with open(os.environ["SETUP_PIP_LOG"], "a") as log:
        log.write(json.dumps(args[2:]) + "\\n")
    if "--no-build-isolation" in args and os.environ["SETUP_FAIL_BASICSR"] == "1":
        sys.exit(1)
elif args[0] not in ("dev_tools/patch_basicsr.py", "verify_env.py"):
    raise SystemExit("Unexpected interpreter command: " + repr(args))
'''
    existing = venv(tmp_path / "venv", python=interpreter)
    log = tmp_path / "pip.jsonl"
    result = setup(
        "--skip-models", "--skip-smoke", "--venv", str(existing),
        env={**host, "SETUP_PIP_LOG": str(log), "SETUP_FAIL_BASICSR": str(int(build_fails))},
    )
    assert result.returncode == int(build_fails), result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    bootstrap = next(i for i, args in enumerate(calls) if "torch" in args)
    build = next(i for i, args in enumerate(calls) if "basicsr" in args)
    assert bootstrap < build
    for requirement in ("numpy", "cython", "torchvision", "torchaudio"):
        assert requirement in calls[bootstrap]
    for index in (bootstrap, build):
        assert calls[index][calls[index].index("-c") + 1] == "requirements.txt"
    assert "--no-build-isolation" in calls[build]
    assert "--no-deps" in calls[build]
    if build_fails:
        assert calls[build + 1:] == []
        assert "could not build BasicSR against the installed stack" in result.stderr
    else:
        runtime = next(i for i, args in enumerate(calls) if "-r" in args)
        assert build < runtime
        assert calls[runtime][calls[runtime].index("-r") + 1] == "requirements.txt"
