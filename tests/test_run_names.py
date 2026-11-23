"""Source-first names must not change which run a resume or audit selects."""
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

import video_upscale_pipeline as pipeline
from pipeline_tools import run_names as names

ROOT = Path(__file__).resolve().parents[1]


def shell_function(function, *args):
    return subprocess.run(
        ["bash", "-c", 'source "$1"; shift; "$@"', "test",
         str(ROOT / "pipeline_tools/run_lib.sh"), function, *map(str, args)],
        capture_output=True, text=True)


def run(root, name, started, **fields):
    directory = root / name
    directory.mkdir(parents=True)
    record = {"run_id": name, "started": started, "finished": None,
              "class": "hi8-dv", "settings": {"crf": 23}, **fields}
    (directory / "run.json").write_text(json.dumps(record))
    return directory


def test_all_id_builders_put_the_source_first():
    spec = importlib.util.spec_from_file_location(
        "comparison_names", ROOT / "ai_model_tests/run_test_comparisons.py")
    comparison = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(comparison)
    for make in (pipeline.make_run_id, comparison.make_run_id,
                 lambda label: shell_function("run_make_id", label).stdout.strip()):
        assert re.fullmatch(r"19980503_trimmed_\d{8}-\d{6}", make("19980503 trimmed"))
        assert re.fullmatch(r"run_\d{8}-\d{6}", make("***"))


@pytest.mark.parametrize("suffix", [
    "_mask8_60fps_20260912_151048_master_from_prepare_video",
    "_mask0_30fps_TEST_master_from_prepare_video",
    "_master_from_prepare_video",
])
def test_master_label_recovers_capture_stem_without_records(tmp_path, suffix):
    assert names.source_stem(tmp_path / f"19980503_19990429_trimmed{suffix}.mp4") \
        == "19980503_19990429_trimmed"
    assert names.source_stem(tmp_path / "capture_mask8_60fps.mp4") == "capture_mask8_60fps"


def test_master_label_prefers_recorded_source_over_its_filename(tmp_path):
    master = tmp_path / "run/work/masters/wrong_master_from_prepare_video.mp4"
    master.parent.mkdir(parents=True)
    record = tmp_path / "run/run.json"
    record.write_text(json.dumps({"sources": ["/dvd/19980503_trimmed.mpg"]}))
    assert names.source_stem(master) == "19980503_trimmed"
    record.unlink()
    (tmp_path / "run/logs").mkdir()
    (tmp_path / "run/logs/audio_timing_plan.json").write_text(
        json.dumps({"input": "/dvd/other_trimmed.mpg"}))
    assert names.source_stem(master) == "other_trimmed"


def test_latest_selection_uses_instants_and_filters_class_and_recipe(tmp_path):
    old = run(tmp_path, "zzz_20261015-130000", "2026-10-15T13:00:00+02:00")
    newest = run(tmp_path, "aaa_20261015-100000", "2026-10-15T10:00:00-07:00")
    run(tmp_path, "foreign_20261015-230000", "2026-10-16T01:00:00Z", **{"class": "digital8"})
    run(tmp_path, "done_20261015-230000", "2026-10-16T02:00:00Z", finished="done")
    wrong = run(tmp_path, "wrong_20261015-230000", "2026-10-16T03:00:00Z", settings={"crf": 12})
    invalid = run(tmp_path, "invalid_20261015-230000", "yesterday")
    (tmp_path / "current_run").symlink_to(old)
    assert names.runs_newest_first(tmp_path) == [wrong, tmp_path / "done_20261015-230000",
                                                 tmp_path / "foreign_20261015-230000", newest, old]
    result = shell_function("run_find_adoptable", tmp_path, "hi8-dv", '{"crf":23}')
    assert result.returncode == 0 and result.stdout.strip() == str(newest)
    result = shell_function("run_find_latest_unfinished", tmp_path, "hi8-dv")
    assert result.returncode == 0 and result.stdout.strip() == str(wrong)
    assert str(invalid) in result.stderr


def test_audit_defaults_to_newest_digital8_manifest(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    root = tmp_path / "pres/ai"
    run(root, "zzz_20261015-230000", "2026-10-15T01:00:00Z", **{"class": "digital8"})
    newest = run(root, "aaa_20261015-010000", "2026-10-15T23:00:00Z", **{"class": "digital8"})
    run(root, "hi8_20261016-010000", "2026-10-16T01:00:00Z")
    (newest / "videos").mkdir()
    import os
    result = subprocess.run(
        ["bash", str(ROOT / "pipeline_tools/check_daily_ai_distribution.sh"),
         "--input-root", str(sources)], capture_output=True, text=True,
        env={**os.environ, "VIDEO_PRESERVATION_ROOT": str(tmp_path / "pres")})
    assert f"Run:          {newest}" in result.stdout, result.stdout + result.stderr


def test_resuming_preserves_started_and_provenance(tmp_path):
    directory = run(tmp_path, "tape_20261001-010000", "2026-10-01T01:00:00-07:00",
                    sources=["/dvd/tape.mpg"])
    pipeline.write_run_manifest(str(directory), "hi8-dv", {}, ["/master.mp4"])
    record = json.loads((directory / "run.json").read_text())
    assert record["started"] == "2026-10-01T01:00:00-07:00"
    assert record["sources"] == ["/dvd/tape.mpg"]
