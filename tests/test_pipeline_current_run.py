import json
from pathlib import Path
import pytest
import video_upscale_pipeline as p


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'RUN_ROOT', str(tmp_path / 'ai'))
    directory = tmp_path / 'ai' / 'run-one'
    directory.mkdir(parents=True)
    (directory / 'run.json').write_text(json.dumps({'entry_point': 'video_upscale_pipeline.py', 'finished': None}))
    args = p.parse_arguments([str(tmp_path / 'missing_master.mp4'), '--profile', 'balanced',
                             '--crf', '19', '--chunk-duration', '42', '--keep-input'])
    p.save_resume_arguments(args, str(directory))
    p.set_current_run(str(directory))
    return directory


def test_resume_restores_recipe_without_master(run):
    args = p.parse_arguments(['--resume'])
    assert args.crf == 19 and args.chunk_duration == 42 and args.keep_input
    assert args.run_dir == str(run)
    assert not Path(args.input_video).exists()


def test_explicit_run_resume(run):
    p.clear_current_run(str(run))
    assert p.parse_arguments(['--resume', '--run-dir', str(run)]).crf == 19


def test_clear_only_matching_pointer(run):
    other = run.parent / 'run-two'
    other.mkdir()
    p.set_current_run(str(other))
    p.clear_current_run(str(run))
    assert (run.parent / 'current_run').resolve() == other
    p.clear_current_run(str(other))
    assert not (run.parent / 'current_run').is_symlink()


def test_refuse_real_current_directory(run):
    p.clear_current_run(str(run))
    (run.parent / 'current_run').mkdir()
    with pytest.raises(RuntimeError):
        p.set_current_run(str(run))


@pytest.mark.parametrize('change', ['finished', 'foreign', 'missing', 'malformed'])
def test_bad_resume_state_rejected(run, change):
    if change in ('finished', 'foreign'):
        (run / 'run.json').write_text(json.dumps({
            'entry_point': 'video_upscale_pipeline.py' if change == 'finished' else 'enhance_tape.sh',
            'finished': 'today' if change == 'finished' else None,
        }))
    elif change == 'missing':
        (run / 'state/pipeline_resume.json').unlink()
    else:
        (run / 'state/pipeline_resume.json').write_text('{}')
    with pytest.raises(SystemExit) as exc:
        p.parse_arguments(['--resume'])
    assert exc.value.code == 2


def test_resume_rejects_recipe_override(run):
    with pytest.raises(SystemExit):
        p.parse_arguments(['--resume', '--crf', '23'])


def test_no_current_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'RUN_ROOT', str(tmp_path))
    with pytest.raises(SystemExit):
        p.parse_arguments(['--resume'])


def test_success_clears_pointer(run, monkeypatch):
    monkeypatch.setattr(p, 'RUN_DIR', str(run))
    for name in ('free_master_if_disposable', 'prune_empty_work', 'finish_run_manifest', 'write_run_md'):
        monkeypatch.setattr(p, name, lambda *a: None)
    p.finish_run(True, 'final.mp4')
    assert not (run.parent / 'current_run').is_symlink()


@pytest.mark.parametrize('extra', [[], ['--verify-full-decode']])
def test_resume_requires_recorded_verification_choice(run, extra):
    path = run / 'state/pipeline_resume.json'
    state = json.loads(path.read_text())
    del state['arguments']['verify_full_decode']
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    with pytest.raises(SystemExit) as exc:
        p.parse_arguments(['--resume', *extra])
    assert exc.value.code == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize('value', [None, 'false', 0, 1, [], {}])
def test_resume_rejects_invalid_verification_choice(run, value):
    path = run / 'state/pipeline_resume.json'
    state = json.loads(path.read_text())
    state['arguments']['verify_full_decode'] = value
    path.write_text(json.dumps(state))
    with pytest.raises(SystemExit) as exc:
        p.parse_arguments(['--resume'])
    assert exc.value.code == 2


def test_thorough_verification_is_saved_and_restored(run):
    args = p.parse_arguments(['--resume', '--verify-full-decode'])
    p.save_resume_arguments(args, str(run))
    assert p.parse_arguments(['--resume']).verify_full_decode


@pytest.mark.parametrize('record', ['[]', 'null', '"text"', '1', 'invalid JSON'])
def test_bad_previous_manifest_does_not_block_pointer_change(run, record):
    (run / 'run.json').write_text(record)
    other = run.parent / 'run-two'
    other.mkdir()
    p.set_current_run(str(other))
    assert (run.parent / 'current_run').resolve() == other


def test_displaced_run_hint_names_previous_run(run, capsys):
    other = run.parent / 'run-two'
    other.mkdir()
    p.set_current_run(str(other))
    output = capsys.readouterr().out
    assert f'--resume --run-dir {run}' in output
    assert (run.parent / 'current_run').resolve() == other
