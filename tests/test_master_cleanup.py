"""Ownership and deletion regressions for standalone prepared masters."""
import json
from types import SimpleNamespace

import pytest
import video_upscale_pipeline as pipeline

CAPTURE = "/archive/Hi8/19980503_19990429_trimmed.mpg"


@pytest.fixture
def master(tmp_path, monkeypatch):
    prep = tmp_path / "preparation"
    path = prep / "work/masters/tape_master_from_prepare_video.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"prepared intermediate")
    (prep / "run.json").write_text(json.dumps({
        "run_id": prep.name, "entry_point": "prepare_video.sh",
        "sources": [CAPTURE]}))
    (prep / "logs").mkdir()
    (prep / "logs/prepare.log").write_text("prepare ran\n")
    monkeypatch.setattr(pipeline.subprocess, "run", lambda args, **kw:
                        SimpleNamespace(stdout=("h264\nyuv444p\n" if "stream=codec_name,pix_fmt" in args
                                                else pipeline.MASTER_TAG_FRAGMENT)))
    monkeypatch.setattr(pipeline, "get_embedded_x264_crf", lambda path: pipeline.PREFILTER_CRF)
    run = tmp_path / "upscale"
    (run / "logs").mkdir(parents=True)
    (run / "logs/pipeline.log").write_text("pipeline ran\n")
    (run / "run.json").write_text(json.dumps({
        "entry_point": "video_upscale_pipeline.py",
        "sources": [str(path)], "finished": None}))
    return path, run, prep


def test_separate_preparation_master_is_removed(master):
    path, run, prep = master
    assert pipeline.input_is_disposable_master(str(path), str(run))[0]
    pipeline.free_master_if_disposable(str(path), str(run), False)
    assert not path.exists()
    # The emptied preparation run is folded into the consuming run as
    # provenance, and its orphaned directory removed.
    assert not prep.exists()
    # Matches the single run enhance_tape.sh produces: no master_run.json,
    # and the capture prepare recorded replaces the deleted master as source.
    assert not (run / "master_run.json").exists()
    assert json.loads((run / "run.json").read_text())["sources"] == [CAPTURE]
    # The preparation log is kept, alongside this run's own pipeline log.
    assert (run / "logs/prepare.log").read_text() == "prepare ran\n"
    assert (run / "logs/pipeline.log").read_text() == "pipeline ran\n"
    # Completion may call cleanup a second time; the master is already gone.
    pipeline.free_master_if_disposable(str(path), str(run), False)


def test_keep_input_overrides_cleanup(master):
    path, run, _ = master
    pipeline.free_master_if_disposable(str(path), str(run), True)
    assert path.exists()


@pytest.mark.parametrize("manifest", [None, "invalid JSON", "[]",
                                      '{"entry_point":"capture.sh","run_id":"preparation"}',
                                      '{"entry_point":"prepare_video.sh","run_id":"another-run"}'])
def test_unproven_external_ownership_keeps_input(master, manifest):
    path, run, prep = master
    record = prep / "run.json"
    if manifest is None:
        record.unlink()
    else:
        record.write_text(manifest)
    pipeline.free_master_if_disposable(str(path), str(run), False)
    assert path.exists()


def test_current_run_master_still_qualifies(master):
    path, _, prep = master
    (prep / "run.json").unlink()
    assert pipeline.input_is_disposable_master(str(path), str(prep))[0]


def test_symlink_to_external_capture_does_not_qualify(master, tmp_path):
    path, run, _ = master
    capture = tmp_path / path.name
    path.rename(capture)
    path.symlink_to(capture)
    pipeline.free_master_if_disposable(str(path), str(run), False)
    assert capture.exists()
    assert path.is_symlink()


def test_wrong_crf_keeps_master(master, monkeypatch):
    path, run, _ = master
    monkeypatch.setattr(pipeline, "get_embedded_x264_crf", lambda path: 23)
    pipeline.free_master_if_disposable(str(path), str(run), False)
    assert path.exists()


def test_missing_preparation_tag_keeps_master(master, monkeypatch):
    path, run, _ = master
    monkeypatch.setattr(pipeline.subprocess, "run", lambda args, **kw:
                        SimpleNamespace(stdout="h264\nyuv444p\n"))
    pipeline.free_master_if_disposable(str(path), str(run), False)
    assert path.exists()


@pytest.mark.parametrize("sibling", ["masters/another.mp4", "processing_chunks/chunk.mp4"])
def test_cleanup_preserves_other_work(master, sibling):
    path, run, prep = master
    other = prep / "work" / sibling
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"keep this work")
    pipeline.free_master_if_disposable(str(path), str(run), False)
    assert not path.exists()
    assert other.read_bytes() == b"keep this work"
    assert (prep / "work").is_dir()
    assert (prep / "run.json").exists()  # not folded while work/ is non-empty
    assert not (run / "master_run.json").exists()
    if sibling.startswith("processing_chunks/"):
        assert not path.parent.exists()
