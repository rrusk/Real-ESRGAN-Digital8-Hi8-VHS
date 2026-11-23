"""Real GPAC append regression: audio padding must not hide trailing pictures."""
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    'repair_edit', Path(__file__).resolve().parents[1] / 'pipeline_tools/repair_mp4_video_edit.py')
repair_edit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair_edit)


def run(*args):
    return subprocess.check_output([str(a) for a in args], stderr=subprocess.PIPE)


def hashes(path, ignore=False):
    opts = ['-ignore_editlist', '1'] if ignore else []
    data = run('ffmpeg', '-v', 'error', *opts, '-i', path, '-map', '0:v:0',
               '-vsync', '0', '-f', 'framemd5', '-')
    return [line.split(b',')[-1].strip() for line in data.splitlines()
            if line and not line.startswith(b'#')]


@pytest.mark.skipif(not all(shutil.which(x) for x in ('ffmpeg', 'ffprobe', 'MP4Box')),
                    reason='requires ffmpeg, ffprobe and MP4Box')
def test_audio_driven_append_exposes_every_picture(tmp_path):
    clips = []
    expected = []
    for i, frames in enumerate((556, 812, 4152)):
        video, audio, clip = [tmp_path / f'{i}.{suffix}' for suffix in ('video.mp4', 'mka', 'mp4')]
        seconds = str(frames * 1001 / 60000)
        run('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=64x48:rate=60000/1001', '-frames:v', frames,
            '-c:v', 'libx264', '-preset', 'fast', '-bf', '3', video)
        run('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            f'sine=sample_rate=32000:duration={seconds}', '-ac', '2',
            '-c:a', 'aac', '-b:a', '192k', audio)
        run('ffmpeg', '-v', 'error', '-i', video, '-i', audio,
            '-map', '0:v', '-map', '1:a', '-c', 'copy', clip)
        expected.extend(hashes(clip))
        clips.append(clip)
    merged = tmp_path / 'joined.mp4'
    args = ['MP4Box', '-quiet']
    for clip in clips:
        args.extend(['-cat', clip])
    run(*args, '-new', merged)
    full = hashes(merged, ignore=True)
    assert full == expected
    before = merged.read_bytes()
    # Older GPAC suppresses a frame here. New versions may already fix it;
    # in either case repair must preserve all decoded content and be idempotent.
    repair_edit.repair(merged)
    assert hashes(merged) == expected
    repaired = merged.read_bytes()
    assert len(repaired) == len(before)
    # Duration-field patching must leave mdat, sample tables and audio intact.
    with merged.open('rb') as f:
        top = list(repair_edit.boxes(f, 0, len(repaired)))
    for kind, start, end in top:
        if kind == b'mdat':
            assert repaired[start:end] == before[start:end]
    assert not repair_edit.repair(merged)
    assert merged.read_bytes() == repaired


def test_invalid_file_is_unchanged(tmp_path):
    path = tmp_path / 'bad.mp4'
    original = b'not an mp4'
    path.write_bytes(original)
    with pytest.raises(ValueError):
        repair_edit.repair(path)
    assert path.read_bytes() == original


def test_presentation_end_uses_composition_order_and_signed_offsets():
    import io
    import struct

    def box(kind, body):
        return struct.pack('>I4s', len(body) + 8, kind) + body

    stts = box(b'stts', struct.pack('>IIII', 0, 1, 3, 10))
    # Decode order is not presentation order: middle sample is presented last.
    for version, offsets, expected in ((0, (20, 30, 0), 50),
                                       (1, (-10, 20, -10), 40)):
        entries = b''.join(struct.pack('>Ii', 1, offset) for offset in offsets)
        ctts = box(b'ctts', struct.pack('>II', version << 24, 3) + entries)
        data = stts + ctts
        f = io.BytesIO(data)
        assert repair_edit.presentation_end(f, list(repair_edit.boxes(f, 0, len(data)))) == expected


@pytest.fixture
def short_mp4(tmp_path):
    if not all(shutil.which(x) for x in ('ffmpeg', 'ffprobe')):
        pytest.skip('requires ffmpeg and ffprobe')
    path = tmp_path / 'short video.mp4'
    run('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=64x48:rate=30:duration=2', '-f', 'lavfi', '-i',
        'sine=sample_rate=48000:duration=2', '-c:v', 'libx264',
        '-preset', 'fast', '-bf', '3', '-c:a', 'aac', path)
    return path


def video_edit(path):
    with path.open('rb') as f:
        top = list(repair_edit.boxes(f, 0, path.stat().st_size))
        movie = list(repair_edit.boxes(f, *repair_edit.one(top, b'moov')))
        for kind, start, end in movie:
            if kind != b'trak':
                continue
            track = list(repair_edit.boxes(f, start, end))
            media = list(repair_edit.boxes(f, *repair_edit.one(track, b'mdia')))
            if repair_edit.read(f, repair_edit.one(media, b'hdlr'))[8:12] == b'vide':
                edits = list(repair_edit.boxes(f, *repair_edit.one(track, b'edts')))
                return repair_edit.one(edits, b'elst')[0]
    raise AssertionError('no video edit')


def hide_final_100ms(path):
    import struct
    edit = video_edit(path)
    with path.open('r+b') as f:
        f.seek(edit + 8)
        f.write(struct.pack('>I', 1900))


def test_default_check_rejects_hidden_tail_without_decoding_or_writing(short_mp4, monkeypatch):
    import video_upscale_pipeline as pipeline

    def no_decode(*args, **kwargs):
        pytest.fail('default verification must not decode the whole video')
    monkeypatch.setattr(pipeline, 'check_output_presents_every_picture', no_decode)

    intact = short_mp4.read_bytes()
    pipeline.check_output_presentation(str(short_mp4), 'test')
    assert short_mp4.read_bytes() == intact

    hide_final_100ms(short_mp4)
    before = short_mp4.read_bytes()
    with pytest.raises(RuntimeError, match='ends before its last stored pictures'):
        pipeline.check_output_presentation(str(short_mp4), 'test')
    # The delivery check reports the fault; it never repairs the file.
    assert short_mp4.read_bytes() == before


def test_manual_repair_exposes_hidden_tail_changing_only_durations(short_mp4):
    original_hashes = hashes(short_mp4)
    hide_final_100ms(short_mp4)
    assert len(hashes(short_mp4)) < len(original_hashes)
    before = short_mp4.read_bytes()
    planned = repair_edit.repair_plan(short_mp4)
    assert planned

    assert repair_edit.repair(short_mp4)
    assert hashes(short_mp4) == original_hashes
    expected = bytearray(before)
    for pos, data in planned:
        expected[pos:pos + len(data)] = data
    # Everything except planned duration fields, including audio, is identical.
    assert short_mp4.read_bytes() == expected
    assert not repair_edit.repair_plan(short_mp4)
    assert not repair_edit.repair(short_mp4)
    assert short_mp4.read_bytes() == expected


def test_thorough_check_decodes_all_frames(short_mp4, capsys):
    import video_upscale_pipeline as pipeline
    pipeline.check_output_presentation(str(short_mp4), 'test', full_decode=True)
    output = capsys.readouterr().out
    assert 'full decode verification' in output
    assert 'decodes all 60 stored pictures' in output


def test_leading_trim_is_rejected_without_modification(short_mp4):
    import struct
    edit = video_edit(short_mp4)
    with short_mp4.open('r+b') as f:
        f.seek(edit + 12)
        media_time = struct.unpack('>i', f.read(4))[0]
        f.seek(edit + 12)
        f.write(struct.pack('>i', media_time + 512))
    before = short_mp4.read_bytes()
    with pytest.raises(ValueError, match='hides leading samples'):
        repair_edit.repair(short_mp4)
    assert short_mp4.read_bytes() == before


def test_unsupported_edit_layout_is_rejected_without_modification(short_mp4):
    import struct
    edit = video_edit(short_mp4)
    with short_mp4.open('r+b') as f:
        f.seek(edit + 4)
        f.write(struct.pack('>I', 2))
    before = short_mp4.read_bytes()
    with pytest.raises(ValueError, match='single normal video edit'):
        repair_edit.repair(short_mp4)
    assert short_mp4.read_bytes() == before
