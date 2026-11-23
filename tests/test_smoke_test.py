"""Smoke-test subprocesses cannot replace a production active-run pointer."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fails", [False, True])
def test_smoke_preserves_production_pointer(tmp_path, fails):
    # Exercise the shell harness with a pipeline stand-in, without GPU access.
    repository = tmp_path / "checkout"
    tools = repository / "dev_tools"
    tools.mkdir(parents=True)
    smoke = tools / "smoke_test.sh"
    smoke.write_bytes((ROOT / "dev_tools" / "smoke_test.sh").read_bytes())
    (repository / "weights").mkdir()
    (repository / "weights" / "realesr-general-x4v3.pth").write_text("fixture")
    environment = repository / "venv" / "bin"
    environment.mkdir(parents=True)
    (environment / "python").symlink_to(sys.executable)
    pipeline = repository / "video_upscale_pipeline.py"
    pipeline.write_text('''import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ["VIDEO_PRESERVATION_ROOT"])
run = pathlib.Path(sys.argv[sys.argv.index("--run-dir") + 1])
(root / "ai").mkdir(parents=True, exist_ok=True)
link = root / "ai" / "current_run"
link.unlink(missing_ok=True)
link.symlink_to(run)
pathlib.Path(os.environ["SMOKE_OBSERVATION"]).write_text(json.dumps({"root": str(root), "run": str(run)}))
if os.environ["SMOKE_FAIL"] == "1":
    sys.exit(1)
(run / "videos").mkdir(parents=True)
subprocess.run(["ffmpeg", "-v", "error", "-i", sys.argv[1], "-vf", "scale=128:96",
                "-c:v", "libx264", "-c:a", "copy", str(run / "videos" / "out.mp4")], check=True)
''')
    production = tmp_path / "production"
    active = production / "ai" / "existing-run"
    active.mkdir(parents=True)
    pointer = active.parent / "current_run"
    pointer.symlink_to(active)
    observation = tmp_path / "observation.json"
    result = subprocess.run(
        ["bash", str(smoke)], capture_output=True, text=True,
        env={**os.environ, "VIDEO_PRESERVATION_ROOT": str(production),
             "SMOKE_OBSERVATION": str(observation), "SMOKE_FAIL": str(int(fails))},
    )
    assert result.returncode == int(fails), result.stdout + result.stderr
    paths = json.loads(observation.read_text())
    assert Path(paths["root"]).parent == Path(paths["run"]).parent
    assert Path(paths["root"]) != production
    assert pointer.resolve() == active
    assert not Path(paths["root"]).exists()  # reclaimed on success and failure
