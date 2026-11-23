"""Scan type and field order detection on sources without reliable metadata."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('probe_video', ROOT / 'pipeline_tools/probe_video.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which('ffmpeg') and shutil.which('ffprobe')), reason='FFmpeg tools required')


def encode(path, codec, scan):
    """Two seconds of 720x480 video: interlaced from 59.94p, or progressive."""
    if scan == 'progressive':
        source, vf = 'testsrc2=size=720x480:rate=30000/1001:duration=2', 'noise=alls=12:allf=t'
    else:
        source, vf = 'testsrc2=size=720x480:rate=60000/1001:duration=2', f'interlace=scan={scan}'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', source,
                    '-vf', vf, '-c:v', codec, str(path)], check=True)
    return path


def still_then_moving(path, still, moving=0, field_order=None):
    """Bottom-field-first HuffYUV opening on a still grey picture, as a tape
    leader does, then `moving` seconds of motion."""
    inputs = ['-f', 'lavfi', '-i', f'color=c=gray:size=720x480:rate=60000/1001:duration={still}']
    graph = '[0]interlace=scan=bff'
    if moving:
        inputs += ['-f', 'lavfi', '-i', f'testsrc2=size=720x480:rate=60000/1001:duration={moving}']
        graph = '[0][1]concat=n=2:v=1,interlace=scan=bff'
    tag = ['-field_order', field_order] if field_order else []
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', *inputs, '-filter_complex', graph,
                    '-c:v', 'huffyuv', *tag, str(path)], check=True)
    return path


def detect(path, frames=500):
    field_order = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
         'stream=field_order,codec_name,duration,r_frame_rate', '-of', 'default=nw=1', str(path)],
        capture_output=True, text=True, check=True).stdout
    meta = dict(line.split('=', 1) for line in field_order.split())
    return probe.detect_interlace(str(path), meta, frames)


@needs_ffmpeg
@pytest.mark.parametrize('scan, parity', [('tff', '0'), ('bff', '1')])
def test_flagless_lossless_capture_is_found_interlaced_by_its_pixels(tmp_path, scan, parity):
    # HuffYUV carries no interlace flag: every frame decodes as not interlaced.
    scan_type, interlaced, found = detect(encode(tmp_path / 'capture.avi', 'huffyuv', scan))
    assert interlaced and found == parity, scan_type
    assert 'idet' in scan_type


@needs_ffmpeg
def test_flagless_progressive_capture_stays_progressive(tmp_path):
    scan_type, interlaced, parity = detect(encode(tmp_path / 'capture.avi', 'huffyuv', 'progressive'))
    assert not interlaced and parity == '-1'
    assert scan_type.startswith('Progressive'), scan_type


@needs_ffmpeg
@pytest.mark.parametrize('scan, swapped, parity', [('tff', 'tb', '0'), ('bff', 'bt', '1')])
def test_swapped_stream_order_is_settled_by_frame_flags(tmp_path, scan, swapped, parity):
    # FFmpeg writes tb for top-field-first FFV1 and bt for bottom-field-first,
    # the reverse of its own definitions; the frame flags state the order.
    path = encode(tmp_path / 'capture.mkv', 'ffv1', scan)
    scan_type, interlaced, found = detect(path)
    assert interlaced and found == parity, scan_type
    assert 'frame-flag' in scan_type


@needs_ffmpeg
def test_idet_reads_windows_across_the_recording(tmp_path):
    # 50 frames from the start see only the still opening; windows spread
    # over the six seconds reach the motion.
    path = still_then_moving(tmp_path / 'capture.avi', still=4, moving=2)
    opening_only = probe.idet_verdict(probe.idet_counts(str(path), {}, 50))
    assert not opening_only[1], opening_only
    scan_type, interlaced, parity = detect(path, frames=50)
    assert interlaced and parity == '1', scan_type


@needs_ffmpeg
def test_tagged_interlaced_source_stays_interlaced_when_no_order_is_seen(tmp_path):
    # bt says interlaced; a still picture shows no combing of either order.
    scan_type, interlaced, parity = detect(
        still_then_moving(tmp_path / 'capture.mkv', still=2, field_order='bt'))
    assert interlaced and parity == '-1', scan_type
    assert scan_type.startswith('Interlaced (order not established'), scan_type


def fake_run(idet_counts):
    """subprocess.run stand-in: unflagged frames, then the given idet verdict."""
    tff, bff, prog = idet_counts

    def run(cmd, **kwargs):
        if cmd[0] == 'ffprobe':
            return SimpleNamespace(stdout='interlaced_frame=0\ntop_field_first=0\n' * 50, stderr='')
        return SimpleNamespace(stdout='', stderr=(
            f'[Parsed_idet_0 @ 0x0] Multi frame detection: TFF: {tff} BFF: {bff} '
            f'Progressive: {prog} Undetermined: 0\n'))
    return run


@pytest.mark.parametrize('stream_order, counts, expected', [
    ('', (47, 44, 29), ('Undetermined', False, '-1')),   # both orders: fine detail, not interlace
    ('', (900, 30, 70), ('Interlaced (TFF', True, '0')),  # one order dominates
    ('', (20, 400, 80), ('Interlaced (BFF', True, '1')),
    ('', (5, 3, 492), ('Progressive', False, '-1')),
    # A stream tagged interlaced stays interlaced; idet can only add the order.
    ('bt', (20, 400, 80), ('Interlaced (BFF', True, '1')),
    ('tb', (5, 3, 492), ('Interlaced (order not established', True, '-1')),
    ('bt', (47, 44, 29), ('Interlaced (order not established', True, '-1')),
])
def test_idet_verdict_needs_one_dominant_order(monkeypatch, stream_order, counts, expected):
    monkeypatch.setattr(probe.subprocess, 'run', fake_run(counts))
    monkeypatch.setattr(probe, 'require_tools', lambda *tools: None)
    scan_type, interlaced, parity = probe.detect_interlace(
        'capture.avi', {'codec_name': 'huffyuv', 'field_order': stream_order})
    assert scan_type.startswith(expected[0]) and (interlaced, parity) == expected[1:], scan_type
