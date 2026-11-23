"""Incomplete resume records must not guess settings or relax coverage checks."""
import json

import pytest

import video_upscale_pipeline as pipeline


@pytest.mark.parametrize('duration', [None, 0, -1, True, '60', float('nan'), float('inf')])
def test_resume_requires_a_positive_recorded_chunk_duration(duration):
    with pytest.raises(RuntimeError, match='no valid chunk_duration'):
        pipeline.resume_chunk_duration({'chunk_duration': duration})


def test_resume_uses_its_recorded_chunk_duration():
    assert pipeline.resume_chunk_duration({'chunk_duration': 42}) == 42
    assert pipeline.resume_chunk_duration({'chunk_duration': 1.5}) == 1.5
    with pytest.raises(RuntimeError, match='no valid chunk_duration'):
        pipeline.resume_chunk_duration({})


@pytest.mark.parametrize('record', [
    None, {}, {'duration': 10},
    {'no_chunk_prefilter_duration': 10},
    {'no_chunk_prefilter_duration': -1, 'no_chunk_prefilter_validated': True},
    {'no_chunk_prefilter_duration': float('nan'), 'no_chunk_prefilter_validated': True},
    {'no_chunk_prefilter_duration': True, 'no_chunk_prefilter_validated': True},
    {'no_chunk_prefilter_duration': 10, 'no_chunk_prefilter_validated': 'true'},
])
def test_no_chunk_resume_requires_a_complete_checkpoint(record):
    with pytest.raises(RuntimeError, match='valid pre-filter checkpoint'):
        pipeline.no_chunk_checkpoint_duration(record)


@pytest.mark.parametrize('validated', [True, False])
def test_no_chunk_checkpoint_retains_its_measured_duration(validated):
    # A false validation flag is still recorded data; the caller's ordinary
    # validation and --force policy decide whether processing may continue.
    record = {'no_chunk_prefilter_duration': 10.5,
              'no_chunk_prefilter_validated': validated}
    assert pipeline.no_chunk_checkpoint_duration(record) == 10.5


@pytest.mark.parametrize('contents', [
    'invalid JSON', '[]', 'null',
    '{"chunk_000": 0}', '{"chunk_000": -1}', '{"chunk_000": true}',
    '{"chunk_000": 30.0}', '{"chunk_000": "30"}',
])
def test_invalid_frame_counts_are_not_replaced(tmp_path, contents):
    record = tmp_path / pipeline.CHUNK_FRAMES_FILE
    record.write_text(contents)
    with pytest.raises(RuntimeError, match='source-frame counts'):
        pipeline.record_chunk_source_frames(str(tmp_path), [], [])
    assert record.read_text() == contents
    assert sorted(p.name for p in tmp_path.iterdir()) == [pipeline.CHUNK_FRAMES_FILE]


@pytest.mark.parametrize('saved', [None, {'chunk_001': 30}])
def test_completed_chunk_without_source_count_is_refused(tmp_path, monkeypatch, saved):
    record = tmp_path / pipeline.CHUNK_FRAMES_FILE
    if saved is not None:
        record.write_text(json.dumps(saved))
    output = tmp_path / 'chunk_000_final.mp4'
    output.write_bytes(b'completed inference output')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    # Inferring from this output would be wrong for interpolated chunks.
    monkeypatch.setattr(pipeline, 'probe_video_timing', lambda path:
                        pytest.fail('completed output was used to infer a source count'))
    with pytest.raises(RuntimeError, match='Missing: chunk_000_final.mp4'):
        pipeline.chunk_source_frame_total(str(tmp_path), [], [str(output)])
    with pytest.raises(RuntimeError, match='Missing: chunk_000_final.mp4'):
        pipeline.record_chunk_source_frames(str(tmp_path), [], [str(output)])
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_mixed_processed_and_input_chunks_use_the_source_frame_domain(tmp_path, monkeypatch):
    record = tmp_path / pipeline.CHUNK_FRAMES_FILE
    record.write_text(json.dumps({'chunk_000': 30}))
    original = str(tmp_path / 'chunk_001.mp4')
    finished = str(tmp_path / 'chunk_000_final.mp4')
    seen = []

    def probe(path):
        seen.append(path)
        assert path == original
        return {'frames': 20}

    monkeypatch.setattr(pipeline, 'probe_video_timing', probe)
    assert pipeline.record_chunk_source_frames(str(tmp_path), [original], [finished]) == {
        'chunk_000': 30, 'chunk_001': 20}
    assert pipeline.chunk_source_frame_total(str(tmp_path), [original], [finished]) == 50
    assert seen == [original, original]
    assert pipeline.read_chunk_source_frames(str(tmp_path)) == {'chunk_000': 30, 'chunk_001': 20}
