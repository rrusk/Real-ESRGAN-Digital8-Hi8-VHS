"""A model comparison claims only a run directory that is free or its own."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "run_test_comparisons", ROOT / "ai_model_tests" / "run_test_comparisons.py")
comparisons = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparisons)

SETTINGS = {"scale": 2, "profile": "balanced", "models": ["RealESRGAN_x2plus"], "crf": None}


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"")
    return path


def manifest(run_dir, clip, **overrides):
    run_dir.mkdir(parents=True, exist_ok=True)
    record = {"class": "model-test", "entry_point": "run_test_comparisons.py",
              "finished": None, "sources": [str(clip)], "settings": SETTINGS}
    record.update(overrides)
    (run_dir / "run.json").write_text(json.dumps(record))


def test_new_or_empty_directory_is_free(tmp_path, clip):
    assert comparisons.run_dir_conflict(tmp_path / "new", SETTINGS, clip) is None
    (tmp_path / "empty").mkdir()
    assert comparisons.run_dir_conflict(tmp_path / "empty", SETTINGS, clip) is None


def test_unfinished_matching_comparison_resumes(tmp_path, clip):
    manifest(tmp_path / "run", clip)
    assert comparisons.run_dir_conflict(tmp_path / "run", SETTINGS, clip) is None


@pytest.mark.parametrize("overrides, reason", [
    ({"class": "digital8", "entry_point": "upscale_dv_batch.sh"}, "not a model comparison"),
    ({"finished": "2026-10-11T12:00:00-07:00"}, "finished"),
    ({"sources": ["/elsewhere/other.mp4"]}, "other.mp4"),
    ({"settings": dict(SETTINGS, scale=4)}, '"scale": 4'),
])
def test_other_runs_are_refused(tmp_path, clip, overrides, reason):
    manifest(tmp_path / "run", clip, **overrides)
    assert reason in comparisons.run_dir_conflict(tmp_path / "run", SETTINGS, clip)


def test_nonempty_directory_without_manifest_is_refused(tmp_path, clip):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "notes.txt").write_text("mine")
    assert "no run.json" in comparisons.run_dir_conflict(tmp_path / "run", SETTINGS, clip)


def test_batch_manifest_survives_a_refused_comparison(tmp_path, clip, monkeypatch):
    run_dir = tmp_path / "batch"
    manifest(run_dir, clip, **{"class": "digital8", "entry_point": "upscale_dv_batch.sh"})
    before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(comparisons, "check_venv", lambda: None)
    monkeypatch.setattr("sys.argv", ["run_test_comparisons.py", str(clip),
                                     "--models", "RealESRGAN_x2plus", "--run-dir", str(run_dir)])
    assert comparisons.main() == 2
    assert (run_dir / "run.json").read_bytes() == before
    assert not (run_dir / "videos").exists()
