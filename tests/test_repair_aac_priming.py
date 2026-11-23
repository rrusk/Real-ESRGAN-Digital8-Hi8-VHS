"""repair_aac_priming.py restores a lost AAC encoder delay, and only that."""
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import video_upscale_pipeline as vup

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "repair_aac_priming", ROOT / "pipeline_tools/repair_aac_priming.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

pytestmark = pytest.mark.skipif(
    not all(shutil.which(x) for x in ("ffmpeg", "ffprobe")),
    reason="requires ffmpeg and ffprobe")

RATE = 48000
TRIM = 1500         # the plan's lead-in: source sample TRIM is master sample 0
SECONDS = 12
COLOUR = ["-colorspace", "smpte170m", "-color_trc", "smpte170m",
          "-color_primaries", "smpte170m"]


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, args)],
                   check=True)


def stream_hashes(path):
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0",
         "-map", "0:a:0", "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-"],
        check=True, capture_output=True, text=True).stdout
    return out.split()


def make_run(root, name, audio, master, plan):
    """A run directory holding one deliverable, muxed as the pipeline muxes."""
    run = root / name
    (run / "videos").mkdir(parents=True)
    if plan is not None:
        (run / "logs").mkdir()
        (run / "logs" / "audio_timing_plan.json").write_text(json.dumps(plan))
    ffmpeg("-i", master, *audio, "-map", "0:v", "-map", "1:a", "-c", "copy",
           *COLOUR, "-movflags", "+faststart", run / "videos" / "tape_x2.mp4")
    return run


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    source = d / "source.wav"           # stands in for the DVD
    ffmpeg("-f", "lavfi", "-i",
           f"anoisesrc=sample_rate={RATE}:duration={SECONDS + 2}:amplitude=0.3:color=pink",
           "-ac", "2", source)
    master = d / "master.mp4"           # trimmed and encoded as prepare_video.sh does
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=64x48:rate=60000/1001:duration={SECONDS}",
           "-i", source, "-filter_complex",
           f"[0:v]setsar=8/9[v];[1:a]asetpts=N/SR/TB,atrim=start_sample={TRIM}:"
           f"end_sample={TRIM + SECONDS * RATE},asetpts=N/SR/TB[a]",
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", master)
    adts = d / "audio.aac"
    ffmpeg("-i", master, "-map", "0:a:0", "-c", "copy", "-f", "adts", adts)
    # Content cut one block earlier than the plan says: one block late on top
    # of the lost delay, which is a different defect from the one repaired.
    late = d / "late.aac"
    ffmpeg("-i", source, "-af", f"asetpts=N/SR/TB,atrim=start_sample={TRIM - 1024}:"
           f"end_sample={TRIM - 1024 + SECONDS * RATE}", "-c:a", "aac", "-b:a", "192k",
           "-f", "adts", late)
    plan = {"input": str(source), "trim_samples": TRIM, "pad_samples": 0,
            "sample_rate": RATE, "copy": False, "audio_present": True}
    runs = d / "runs"
    make_run(runs, "lost", ["-i", adts], master, plan)
    make_run(runs, "good", ["-i", master], master, plan)
    make_run(runs, "shifted", ["-i", late], master, plan)
    make_run(runs, "latestart", ["-itsoffset", f"{1024 / RATE:.9f}", "-i", adts],
             master, plan)
    make_run(runs, "noplan", ["-i", adts], master, None)
    return {"dir": d, "runs": runs, "master": master, "adts": adts,
            "source": source, "plan": plan}


def summary(out):
    return out.split("[INFO] Summary:")[1]


def test_dry_run_sorts_the_runs_and_writes_nothing(media, tmp_path, capsys):
    repairs = tmp_path / "repairs"
    assert tool.main([str(media["runs"]), "--output-dir", str(repairs)]) == 0
    captured = capsys.readouterr()
    assert not repairs.exists()
    tail = summary(captured.out)
    assert "would repair: 1" in tail and "aligned: 1" in tail
    assert "needs review: 3" in tail
    assert "noplan" in tail and "no audio timing plan" in tail
    assert "shifted" in tail and "2048 samples late while discarding 0" in tail
    assert "latestart" in tail and "samples after zero" in tail


def test_apply_restores_the_delay_and_nothing_else(media, tmp_path, capsys):
    repairs = tmp_path / "repairs"
    lost = media["runs"] / "lost" / "videos" / "tape_x2.mp4"
    before = lost.read_bytes()
    assert tool.main(["--apply", "--output-dir", str(repairs), str(lost)]) == 0
    repaired = repairs / "lost" / "videos" / "tape_x2.mp4"

    assert lost.read_bytes() == before
    assert vup.audio_priming_samples(str(repaired)) == vup.AAC_ENCODER_DELAY_SAMPLES
    assert stream_hashes(repaired) == stream_hashes(lost)
    # What a player hears is now exactly what the master held.
    assert vup.decoded_audio_head(str(repaired)) == vup.decoded_audio_head(str(media["master"]))
    assert vup.get_video_sar(str(repaired)) == "8:9"

    report = json.loads((repaired.parent / "tape_x2.mp4.repair.json").read_text())
    assert report["restored_delay_samples"] == 1024
    assert {p["lag_samples"] for p in report["original"]["alignment"]} == {1024}
    assert {p["lag_samples"] for p in report["repaired"]["alignment"]} == {0}
    digest = hashlib.sha256(repaired.read_bytes()).hexdigest()
    manifest = tool.manifest_path(repairs, media["runs"])
    assert manifest.read_text() == f"{digest}  lost/videos/tape_x2.mp4\n"
    assert not list(repaired.parent.glob(".aac-priming-*"))
    # The manifest checks the copy where it goes back, by its archive path.
    restored = tmp_path / "archive" / "lost" / "videos"
    restored.mkdir(parents=True)
    shutil.copy2(repaired, restored)
    subprocess.run(["sha256sum", "--quiet", "-c", str(manifest)],
                   cwd=tmp_path / "archive", check=True)
    capsys.readouterr()

    assert tool.main(["--apply", "--output-dir", str(repairs), str(lost)]) == 0
    out = capsys.readouterr().out
    assert "already repaired: 1" in summary(out)
    assert "matches its report and checksum" in out


def copy_run(media, name, parent):
    """A private copy of one fixture run, for tests that change the original."""
    shutil.copytree(media["runs"] / name, parent / name)
    return parent / name / "videos" / "tape_x2.mp4"


def test_an_existing_copy_counts_only_with_its_report(media, tmp_path, capsys):
    repairs = tmp_path / "repairs"
    lost = media["runs"] / "lost" / "videos" / "tape_x2.mp4"
    placed = repairs / "lost" / "videos" / "tape_x2.mp4"
    placed.parent.mkdir(parents=True)
    placed.write_bytes(b"not an mp4")

    assert tool.main(["--apply", "--output-dir", str(repairs), str(lost)]) == 0
    tail = summary(capsys.readouterr().out)
    assert "needs review: 1" in tail and "not a verified repair" in tail
    assert placed.read_bytes() == b"not an mp4"

    assert tool.main(["--apply", "--force", "--output-dir", str(repairs), str(lost)]) == 0
    assert "repaired: 1" in summary(capsys.readouterr().out)
    assert vup.audio_priming_samples(str(placed)) == vup.AAC_ENCODER_DELAY_SAMPLES


def test_an_existing_copy_must_match_its_checksum(media, tmp_path, capsys):
    repairs = tmp_path / "repairs"
    lost = media["runs"] / "lost" / "videos" / "tape_x2.mp4"
    assert tool.main(["--apply", "--output-dir", str(repairs), str(lost)]) == 0
    repaired = repairs / "lost" / "videos" / "tape_x2.mp4"
    data = bytearray(repaired.read_bytes())
    data[-1] ^= 0xFF
    repaired.write_bytes(data)
    capsys.readouterr()

    assert tool.main(["--output-dir", str(repairs), str(lost)]) == 0
    tail = summary(capsys.readouterr().out)
    assert "needs review: 1" in tail and "does not match the size and checksum" in tail


def test_the_original_must_be_what_was_repaired(media, tmp_path, capsys):
    repairs, archive = tmp_path / "repairs", tmp_path / "archive"
    archive.mkdir()
    original = copy_run(media, "lost", archive)
    assert tool.main(["--apply", "--output-dir", str(repairs), str(original)]) == 0
    repaired = repairs / "lost" / "videos" / "tape_x2.mp4"
    capsys.readouterr()

    # Copied back over the original: recognised, not repaired again.
    shutil.copyfile(repaired, original)
    assert tool.main(["--output-dir", str(repairs), str(original)]) == 0
    out = capsys.readouterr().out
    assert "has already been replaced by its verified repair" in out

    # Anything else in the original's place is not the file the repair is of.
    shutil.copyfile(media["runs"] / "good" / "videos" / "tape_x2.mp4", original)
    assert tool.main(["--output-dir", str(repairs), str(original)]) == 0
    tail = summary(capsys.readouterr().out)
    assert "needs review: 1" in tail and "has changed since" in tail


def test_each_destination_gets_its_own_manifest(media, tmp_path, capsys):
    repairs = tmp_path / "repairs"
    first, second = tmp_path / "drive_a" / "ai", tmp_path / "home" / "ai"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    copy_run(media, "lost", first)
    shutil.copytree(media["runs"] / "lost", second / "tonight")
    assert tool.main(["--apply", "--output-dir", str(repairs), str(first), str(second)]) == 0
    out = capsys.readouterr().out

    a, b = tool.manifest_path(repairs, first), tool.manifest_path(repairs, second)
    assert a.read_text().endswith("  lost/videos/tape_x2.mp4\n")
    assert b.read_text().endswith("  tonight/videos/tape_x2.mp4\n")
    assert len(a.read_text().splitlines()) == len(b.read_text().splitlines()) == 1
    assert sorted(p.name for p in repairs.glob("*.SHA256SUMS")) == sorted([a.name, b.name])
    assert f"cd {first} && sha256sum -c {a}" in out


def test_run_audio_rebuilds_the_saved_extraction(media, tmp_path, capsys):
    run = tmp_path / "run"
    processing = run / "work" / "processing_chunks"
    processing.mkdir(parents=True)
    (run / "logs").mkdir()
    (run / "logs" / "audio_timing_plan.json").write_text(json.dumps(media["plan"]))
    saved = processing / "tape_original.mka"
    ffmpeg("-i", media["adts"], "-c", "copy", saved)
    state = {"input_video": str(tmp_path / "released_master.mp4"),
             "audio_present": True, "audio_file": str(saved),
             "audio_duration": vup.get_video_duration(str(saved))}
    state_path = processing / "source_measurements.json"
    state_path.write_text(json.dumps(state))

    assert tool.main(["--run-audio", str(run)]) == 0
    assert json.loads(state_path.read_text()) == state
    assert "would repair: 1" in summary(capsys.readouterr().out)

    # An .m4a the state does not use is not a finished repair.
    rebuilt = processing / "tape_original.m4a"
    rebuilt.write_bytes(b"left by an interrupted repair")
    assert tool.main(["--apply", "--run-audio", str(run)]) == 0
    assert "does not use it" in summary(capsys.readouterr().out)
    assert json.loads(state_path.read_text()) == state
    rebuilt.unlink()

    assert tool.main(["--apply", "--run-audio", str(run)]) == 0
    updated = json.loads(state_path.read_text())
    rebuilt = processing / "tape_original.m4a"
    assert updated["audio_file"] == str(rebuilt)
    assert updated["audio_priming_samples"] == vup.AAC_ENCODER_DELAY_SAMPLES
    assert json.loads((processing / "source_measurements.json.before-aac-priming")
                      .read_text()) == state
    # The resume's own check: the delay is there and decodes as the master's did.
    master_audio = tmp_path / "master_audio.m4a"
    ffmpeg("-i", media["master"], "-map", "0:a:0", "-c", "copy", master_audio)
    assert vup.verify_audio_priming(str(rebuilt), updated["audio_priming_samples"],
                                    "Saved source audio", reference=str(master_audio))


@pytest.mark.parametrize("argv", [
    [],
    ["--run-audio", "."] + ["somewhere"],
    ["--no-such-option", "."],
])
def test_command_line_errors_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        tool.main(argv)
    assert exc.value.code == 2
