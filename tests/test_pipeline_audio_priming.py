"""The AAC encoder delay must survive audio extraction and the final mux."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import video_upscale_pipeline as vup

ROOT = Path(__file__).resolve().parents[1]

needs_ffmpeg = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")),
    reason="requires ffmpeg and ffprobe")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, args)],
                   check=True)


@pytest.fixture
def master(tmp_path):
    """A prepare_video.sh-style master: picture plus natively encoded AAC."""
    path = tmp_path / "master.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x48:rate=60000/1001:duration=4",
           "-f", "lavfi", "-i", "anoisesrc=sample_rate=48000:duration=4:amplitude=0.3",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", path)
    return path


def copy_audio(source, out, fmt=None):
    ffmpeg("-i", source, "-map", "0:a:0", "-c", "copy",
           *(["-f", fmt] if fmt else []), out)
    return out


def mux(video_source, audio, out):
    ffmpeg("-i", video_source, "-i", audio, "-map", "0:v", "-map", "1:a",
           "-c", "copy", out)
    return out


def lost_delay(master, tmp_path):
    """The defect, built without Matroska: raw ADTS carries no delay either."""
    adts = copy_audio(master, tmp_path / "audio.aac", fmt="adts")
    return mux(master, adts, tmp_path / "lost.mp4")


def test_every_ffmpeg_command_ignores_the_terminal():
    commands = re.findall(r'"ffmpeg",[^\n]*',
                          (ROOT / "video_upscale_pipeline.py").read_text())
    assert commands
    missing = [c for c in commands if not c.startswith('"ffmpeg", "-nostdin"')]
    assert not missing, missing


def test_prepare_video_ffmpeg_ignores_the_terminal():
    commands = re.findall(r"^\s*ffmpeg\s.*$",
                          (ROOT / "prepare_video.sh").read_text(), re.MULTILINE)
    assert commands
    missing = [c for c in commands if not c.split()[1] == "-nostdin"]
    assert not missing, missing


@needs_ffmpeg
def test_m4a_copy_keeps_the_masters_delay(master, tmp_path):
    m4a = copy_audio(master, tmp_path / "audio.m4a")
    assert vup.audio_priming_samples(str(master)) == vup.AAC_ENCODER_DELAY_SAMPLES
    assert vup.audio_priming_samples(str(m4a)) == vup.AAC_ENCODER_DELAY_SAMPLES
    assert vup.audio_priming_samples(str(lost_delay(master, tmp_path))) == 0


@needs_ffmpeg
def test_no_audio_is_not_a_delay_of_zero(tmp_path):
    silent = tmp_path / "silent.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x48:rate=30:duration=1",
           "-c:v", "libx264", "-preset", "ultrafast", silent)
    assert vup.audio_priming_samples(str(silent)) is None


@needs_ffmpeg
def test_final_check_passes_a_preserved_delay(master, tmp_path, capsys):
    m4a = copy_audio(master, tmp_path / "audio.m4a")
    final = mux(master, m4a, tmp_path / "final.mp4")
    assert vup.verify_audio_priming(str(final), 1024, "Final", reference=str(m4a))
    assert "decodes identically" in capsys.readouterr().out


@needs_ffmpeg
def test_final_check_refuses_a_lost_delay(master, tmp_path):
    with pytest.raises(RuntimeError, match="discards 0 leading samples"):
        vup.verify_audio_priming(str(lost_delay(master, tmp_path)), 1024, "Final")


@needs_ffmpeg
def test_decoded_comparison_catches_a_shift_the_metadata_hides(master, tmp_path):
    # Expecting no delay makes the metadata agree; the decode still differs.
    m4a = copy_audio(master, tmp_path / "audio.m4a")
    with pytest.raises(RuntimeError, match="decode differently"):
        vup.verify_audio_priming(str(lost_delay(master, tmp_path)), 0, "Final",
                                 reference=str(m4a))


@needs_ffmpeg
def test_an_unrecorded_delay_fails_rather_than_passing(master):
    with pytest.raises(RuntimeError, match="never recorded"):
        vup.verify_audio_priming(str(master), None, "Final")


@needs_ffmpeg
def test_force_reports_and_does_not_claim_success(master, tmp_path, capsys):
    assert vup.verify_audio_priming(
        str(lost_delay(master, tmp_path)), 1024, "Final", force=True) is False
    out = capsys.readouterr().out
    assert "WARNING" in out and "[priming check]" not in out


@needs_ffmpeg
def test_extraction_copies_aac_with_the_masters_delay(master, tmp_path):
    audio = tmp_path / "source_original.m4a"
    seconds, priming = vup.extract_source_audio(str(master), str(audio), force=True)
    assert priming == vup.AAC_ENCODER_DELAY_SAMPLES
    assert vup.audio_priming_samples(str(audio)) == priming
    assert seconds == pytest.approx(4.0, abs=0.05)


@needs_ffmpeg
def test_extraction_encodes_other_audio_with_the_encoders_delay(tmp_path):
    source = tmp_path / "pcm.mkv"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x48:rate=30:duration=2",
           "-f", "lavfi", "-i", "anoisesrc=sample_rate=48000:duration=2:amplitude=0.3",
           "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", source)
    audio = tmp_path / "source_original.m4a"
    _, priming = vup.extract_source_audio(str(source), str(audio), force=True)
    assert priming == vup.AAC_ENCODER_DELAY_SAMPLES
    assert vup.audio_priming_samples(str(audio)) == priming


def test_unrecorded_saved_audio_names_its_repair():
    error = vup.missing_audio_priming_error(
        "/runs/tape run/work/processing_chunks/tape_original.mka", "/runs/tape run")
    assert "repair_aac_priming.py --apply --run-audio '/runs/tape run'" in str(error)


def av_sync(path, encoder_delay=vup.AAC_ENCODER_DELAY_SAMPLES):
    fps = vup.probe_video_timing(str(path))["fps"]
    vup.verify_final_av_sync(str(path), fps, encoder_delay=encoder_delay)


def discarding(master, tmp_path, extra_samples):
    """The master's audio with extra_samples more than its delay discarded."""
    m4a = copy_audio(master, tmp_path / "audio.m4a")
    out = tmp_path / f"early_{extra_samples}.mp4"
    ffmpeg("-i", master, "-itsoffset", f"-{extra_samples / 48000:.9f}", "-i", m4a,
           "-map", "0:v", "-map", "1:a", "-c", "copy", out)
    return out


@needs_ffmpeg
def test_position_places_audio_at_its_first_programme_sample(master, tmp_path, capsys):
    m4a = copy_audio(master, tmp_path / "audio.m4a")
    av_sync(mux(master, m4a, tmp_path / "final.mp4"))
    out = capsys.readouterr().out
    assert "audio starts +0.0000s (first packet -0.0213s" in out, out
    assert "Alignment OK" in out


@needs_ffmpeg
def test_position_sees_an_undiscarded_delay_as_late_audio(master, tmp_path):
    with pytest.raises(RuntimeError, match=r"\+21\.3ms from the picture \(audio behind"):
        av_sync(lost_delay(master, tmp_path))


@needs_ffmpeg
def test_position_holds_early_audio_to_one_frame(master, tmp_path, capsys):
    av_sync(discarding(master, tmp_path, 480))          # 10ms early: within a frame
    assert "Alignment OK" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="AUDIO AHEAD OF PICTURE, limit 16.7ms"):
        av_sync(discarding(master, tmp_path, 1024))     # one block early


@needs_ffmpeg
def test_position_needs_the_encoder_delay(master):
    with pytest.raises(RuntimeError, match="encoder delay of the audio .* is not known"):
        av_sync(master, encoder_delay=None)
