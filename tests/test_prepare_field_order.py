"""prepare_video.sh scan-type detection and its --field-order override."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (shutil.which('ffmpeg') and shutil.which('ffprobe')), reason='FFmpeg tools required')


@pytest.fixture
def interlaced_bff(tmp_path):
    """A flagless lossless capture holding 60 bottom-field-first frames."""
    path = tmp_path / 'capture.avi'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=720x480:rate=60000/1001:duration=2',
                    '-vf', 'interlace=scan=bff', '-c:v', 'huffyuv', str(path)], check=True)
    return path


def prepare(tmp_path, source, *opts):
    result = subprocess.run(
        ['bash', str(ROOT / 'prepare_video.sh'), '--yes',
         '--output-dir', str(tmp_path / 'masters'), '--log-dir', str(tmp_path / 'logs'),
         *opts, str(source)],
        capture_output=True, text=True,
        env={**os.environ, 'VIDEO_PRESERVATION_ROOT': str(tmp_path / 'preservation')})
    return result, sorted((tmp_path / 'masters').glob('*'))


def frames_and_comment(master):
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets',
         '-show_entries', 'stream=nb_read_packets:format_tags=comment',
         '-of', 'default=nw=1:nk=1', str(master)],
        capture_output=True, text=True, check=True).stdout.split('\n', 1)
    return int(out[0]), out[1]


def plan(result):
    return next(line for line in result.stdout.splitlines() if line.startswith('Video Plan:'))


def test_detected_order_deinterlaces_a_flagless_capture(tmp_path, interlaced_bff):
    result, masters = prepare(tmp_path, interlaced_bff)
    assert result.returncode == 0, (result.stdout + result.stderr)[-5000:]
    assert 'Field Order: BFF' in plan(result)
    frames, comment = frames_and_comment(masters[0])
    assert frames == 120                       # field rate: two pictures per frame
    assert 'field order' not in comment


def test_field_order_overrides_detection_and_is_recorded(tmp_path, interlaced_bff):
    result, masters = prepare(tmp_path, interlaced_bff, '--field-order', 'tff')
    assert result.returncode == 0, (result.stdout + result.stderr)[-5000:]
    assert 'Field Order: TFF (Top Field First, set by --field-order)' in plan(result)
    frames, comment = frames_and_comment(masters[0])
    assert frames == 120
    assert 'prepare_video.sh master' in comment
    assert 'field order: tff set by --field-order' in comment


def test_field_order_progressive_skips_deinterlacing(tmp_path, interlaced_bff):
    result, masters = prepare(tmp_path, interlaced_bff, '--field-order', 'progressive')
    assert result.returncode == 0, (result.stdout + result.stderr)[-5000:]
    assert 'Progressive (set by --field-order)' in plan(result)
    assert frames_and_comment(masters[0])[0] == 60


def test_tagged_interlaced_source_without_an_order_needs_field_order(tmp_path):
    # Tagged bt, so interlaced; a still picture shows neither order.
    still = tmp_path / 'still.mkv'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=gray:size=720x480:rate=60000/1001:duration=2',
                    '-vf', 'interlace=scan=bff', '-c:v', 'huffyuv', '-field_order', 'bt',
                    str(still)], check=True)
    result, masters = prepare(tmp_path, still)
    assert result.returncode == 1
    assert 'field order was not established' in result.stdout + result.stderr
    assert '--field-order tff' in result.stdout + result.stderr
    assert masters == []

    result, masters = prepare(tmp_path, still, '--field-order', 'bff')
    assert result.returncode == 0, (result.stdout + result.stderr)[-5000:]
    assert frames_and_comment(masters[0])[0] == 120


@pytest.mark.parametrize('colour, refused', [('bt709', True), ('bt470bg', True), ('smpte170m', False)])
def test_source_tagged_with_other_colour_is_refused(tmp_path, colour, refused):
    source = tmp_path / 'capture.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=720x480:rate=30000/1001:duration=1',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-colorspace', colour,
                    '-color_primaries', colour, '-color_trc', 'smpte170m', str(source)], check=True)
    # Standalone, so a refusal can be seen to leave no run directory behind.
    result = subprocess.run(['bash', str(ROOT / 'prepare_video.sh'), '--yes', str(source)],
                            capture_output=True, text=True,
                            env={**os.environ, 'VIDEO_PRESERVATION_ROOT': str(tmp_path / 'preservation')})
    if refused:
        assert result.returncode == 1
        assert f'color_space={colour}' in result.stderr and 'NTSC' in result.stderr
        assert not (tmp_path / 'preservation').exists()
    else:
        assert result.returncode == 0, (result.stdout + result.stderr)[-5000:]
        assert 'colour this pipeline does not support' not in result.stdout + result.stderr


@pytest.mark.parametrize('opts, message', [
    (('--field-order', 'sideways'), 'must be tff, bff or progressive, got: sideways'),
    (('--field-order',), 'requires tff, bff or progressive'),
])
def test_invalid_field_order_is_a_usage_error(tmp_path, interlaced_bff, opts, message):
    # The option goes last, so a missing value is not filled by the source path.
    result = subprocess.run(['bash', str(ROOT / 'prepare_video.sh'), '--yes',
                             '--output-dir', str(tmp_path / 'masters'), str(interlaced_bff), *opts],
                            capture_output=True, text=True,
                            env={**os.environ, 'VIDEO_PRESERVATION_ROOT': str(tmp_path / 'preservation')})
    assert result.returncode == 2
    assert message in result.stderr and '--help' in result.stderr
    assert not (tmp_path / 'masters').exists() or not any((tmp_path / 'masters').iterdir())
