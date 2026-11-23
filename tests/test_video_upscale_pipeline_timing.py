import pytest

from video_upscale_pipeline import (
    load_source_measurements,
    record_source_measurements,
    validate_timing_measurements,
)


def timing(duration, fps, frames):
    return {"duration": duration, "fps": fps, "frames": frames}


def test_matching_no_rife_timing_passes():
    result = validate_timing_measurements(
        timing(100.0, 60000 / 1001, 5994),
        timing(100.0, 60000 / 1001, 5994),
        fps_multiplier=1,
        frame_tolerance=2,
    )
    assert result["problems"] == []


def test_no_rife_rejects_two_duplicated_leading_frames():
    result = validate_timing_measurements(
        timing(100.0, 60000 / 1001, 5994),
        timing(100.033, 60000 / 1001, 5996),
        fps_multiplier=1,
        frame_tolerance=0,
    )
    assert any("difference +2, tolerance ±0" in problem
               for problem in result["problems"])


def test_truncated_output_fails_duration_and_frame_count():
    result = validate_timing_measurements(
        timing(6450.177, 60000 / 1001, 386624),
        timing(6235.093, 60000 / 1001, 373731),
        fps_multiplier=1,
        frame_tolerance=2,
    )
    assert any("frame count" in problem for problem in result["problems"])
    assert any("duration" in problem for problem in result["problems"])


def test_wrong_output_fps_fails():
    result = validate_timing_measurements(
        timing(10.0, 30000 / 1001, 300),
        timing(10.0, 30.0, 300),
        fps_multiplier=1,
        frame_tolerance=2,
    )
    assert any("frame rate" in problem for problem in result["problems"])


def test_rife_allows_configured_endpoint_frame_variance():
    result = validate_timing_measurements(
        timing(100.0, 30000 / 1001, 2997),
        timing(99.95, 60000 / 1001, 5990),
        fps_multiplier=2,
        frame_tolerance=4,
    )
    assert result["problems"] == []


def test_invalid_tolerance_is_rejected():
    with pytest.raises(ValueError):
        validate_timing_measurements(
            timing(1.0, 30.0, 30),
            timing(1.0, 30.0, 30),
            fps_multiplier=1,
            frame_tolerance=-1,
        )


def complete_source_state(path):
    return {
        "duration": 10.0,
        "fps": 60.0,
        "frames": 600,
        "span": 10.0,
        "container_duration": 10.005,
        "input_video": str(path),
        "input_extension": ".mp4",
        "fps_text": "60.000",
        "width": 720,
        "height": 480,
        "sar": "8:9",
        "use_chunking": True,
        "audio_present": True,
        "audio_file": "/run/work/processing_chunks/source_original.m4a",
        "audio_duration": 10.005,
        "audio_priming_samples": 1024,
    }


def test_source_state_round_trip_is_resume_ready(tmp_path):
    master = tmp_path / "work" / "masters" / "tape_master_from_prepare_video.mp4"
    processing = tmp_path / "work" / "processing_chunks"
    expected = complete_source_state(master)

    record_source_measurements(str(processing), expected)
    actual = load_source_measurements(
        str(processing), expected_input=str(master), require_resume_state=True)

    assert actual == expected
    assert not (processing / "source_measurements.json.tmp").exists()


def test_source_free_resume_rejects_another_master(tmp_path):
    processing = tmp_path / "processing"
    master = tmp_path / "first_master_from_prepare_video.mp4"
    record_source_measurements(str(processing), complete_source_state(master))

    with pytest.raises(RuntimeError, match="different master"):
        load_source_measurements(
            str(processing),
            expected_input=str(tmp_path / "second_master_from_prepare_video.mp4"),
            require_resume_state=True,
        )


def test_incomplete_measurements_do_not_authorize_missing_master(tmp_path):
    processing = tmp_path / "processing"
    record_source_measurements(
        str(processing), timing(duration=10.0, fps=60.0, frames=600))

    with pytest.raises(RuntimeError, match="is incomplete"):
        load_source_measurements(
            str(processing), expected_input="/missing/master.mp4",
            require_resume_state=True,
        )


def test_concat_avoids_accumulated_movie_duration_rounding(tmp_path):
    import shutil
    import subprocess
    from video_upscale_pipeline import probe_video_timing, write_concat_list

    if not all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe')):
        pytest.skip('requires ffmpeg and ffprobe')
    clip = tmp_path / "chunk's video.mp4"
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=64x48:rate=59.94', '-frames:v', '7',
        '-c:v', 'libx264', '-preset', 'fast', str(clip),
    ], check=True)
    source = timing(700 / 59.94, 59.94, 700)
    for precise in (False, True):
        listing = tmp_path / f'{precise}.txt'
        if precise:
            write_concat_list(listing, [clip] * 100)
        else:
            escaped = str(clip).replace("'", "'\\''")
            listing.write_text(f"file '{escaped}'\n" * 100)
        output = tmp_path / f'{precise}.mp4'
        subprocess.run([
            'ffmpeg', '-v', 'error', '-f', 'concat', '-safe', '0',
            '-i', str(listing), '-c', 'copy', str(output),
        ], check=True)
        measured = probe_video_timing(str(output))
        assert measured['frames'] == 700
        result = validate_timing_measurements(source, measured, 1, 0)
        if precise:
            assert result['problems'] == []
            assert abs(measured['span'] - source['duration']) < 0.001
        else:
            assert result['problems']


@pytest.fixture
def capture(tmp_path):
    """A one-second generated capture in the given codec, interlaced."""
    import shutil
    import subprocess
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("FFmpeg tools required")

    def make(codec):
        size, extra, name = ("720x480", ["-pix_fmt", "yuv411p"], "capture.dv") if codec == "dvvideo" \
            else ("128x96", ["-flags", "+ilme+ildct", "-top", "1"], "capture.mpg")
        path = tmp_path / name
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        f"testsrc2=size={size}:rate=30000/1001:duration=1",
                        "-c:v", codec, *extra, str(path)], check=True)
        return str(path)
    return make


def test_unprepared_mpeg2_is_refused_even_with_the_override(capture, capsys):
    from video_upscale_pipeline import check_input_is_prepared
    with pytest.raises(SystemExit):
        check_input_is_prepared(capture("mpeg2video"), allow_unprepared=True)
    assert "does not cover MPEG-2" in capsys.readouterr().out


def test_override_still_admits_an_unprepared_dv_capture(capture, capsys):
    from video_upscale_pipeline import check_input_is_prepared
    check_input_is_prepared(capture("dvvideo"), allow_unprepared=True)
    assert "continuing anyway" in capsys.readouterr().out
