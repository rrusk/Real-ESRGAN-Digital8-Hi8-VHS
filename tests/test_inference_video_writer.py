"""The video writer reports an ffmpeg that fails after the last frame."""
import subprocess
import sys

import pytest

from inference_realesrgan_video import Writer


def process(status):
    return subprocess.Popen(
        [sys.executable, "-c", f"import sys; sys.stdin.buffer.read(); sys.exit({status})"],
        stdin=subprocess.PIPE)


def writer(video_status, lossless_status=None):
    instance = Writer.__new__(Writer)  # the ffmpeg processes stand in for __init__'s
    instance.stream_writer = process(video_status)
    instance.lossless_writer = None if lossless_status is None else process(lossless_status)
    return instance


def test_clean_exits_close_quietly():
    writer(0, 0).close()


def test_failed_lossless_writer_is_raised_after_both_close():
    instance = writer(0, 3)
    with pytest.raises(RuntimeError, match="lossless diagnostic writer .* status 3"):
        instance.close()
    assert instance.stream_writer.returncode == 0


def test_failed_video_writer_is_raised_without_a_diagnostic():
    with pytest.raises(RuntimeError, match="video writer .* status 1"):
        writer(1).close()
