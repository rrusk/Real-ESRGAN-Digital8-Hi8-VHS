"""The pipeline's terminal copy keeps pace through a pipe and never costs the log.

Run as `... 2>&1 | tee LOG`, the pipeline writes to a pipe. The first tests
start a child process that installs TeeLogger exactly as main() does; the
signal tests run the pipeline itself up to its pre-flight prompt, which comes
before any GPU work. The prompt tests check that only y or n answers a
prompt, typed after it appeared.
"""
import builtins
import io
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]

SETUP = """
import subprocess, sys
import video_upscale_pipeline as p
tee = p.TeeLogger(sys.argv[1])
sys.stdout = sys.stderr = tee
"""


def child(script, log):
    return subprocess.Popen([sys.executable, "-c", SETUP + script, str(log)], cwd=ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)


def test_each_line_reaches_the_pipe_when_printed(tmp_path):
    log = tmp_path / "logs" / "pipeline.log"
    # The child prints one line, then waits for the parent to have read it.
    proc = child("print('first line')\nsys.stdin.readline()\nprint('second line')\n", log)
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 60)
        assert ready, "the first line was held back until exit"
        assert proc.stdout.readline() == b"first line\n"
        proc.stdin.write(b"\n")
        proc.stdin.close()
        assert proc.stdout.read() == b"second line\n"
    finally:
        proc.kill() if proc.poll() is None else None
        proc.wait()
    assert log.read_text() == "first line\nsecond line\n"


def test_reader_gone_leaves_the_log_whole(tmp_path):
    log = tmp_path / "logs" / "pipeline.log"
    # Like Ctrl+C under `| tee`: the reader is gone before the run's last
    # messages. A command started afterwards shares the descriptor.
    proc = child(
        "sys.stdin.readline()\n"
        "print('interrupted')\n"
        "print('resume with --resume')\n"
        "status = subprocess.run(['sh', '-c', 'echo out; echo err >&2']).returncode\n"
        "print(f'child status {status}')\n",
        log)
    proc.stdout.close()
    proc.stdin.write(b"\n")
    proc.stdin.close()
    assert proc.wait(timeout=60) == 0
    lines = log.read_text().splitlines()
    assert lines == ["interrupted",
                     "[INFO] The terminal stopped reading; output continues in this log only.",
                     "resume with --resume",
                     "child status 0"]  # not killed by SIGPIPE


@pytest.fixture
def at_the_prompt(tmp_path):
    """The pipeline on a two-second clip, waiting at "Proceed? (y/n):"."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("FFmpeg tools required")
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=128x96:rate=30000/1001:duration=2",
                    "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)], check=True)
    env = {**os.environ, "VIDEO_PRESERVATION_ROOT": str(tmp_path / "root")}
    env.pop("PIPELINE_RESUME_COMMAND", None)
    proc = subprocess.Popen([sys.executable, str(ROOT / "video_upscale_pipeline.py"), str(clip),
                             "--profile", "balanced"], cwd=ROOT, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    seen, deadline = b"", time.monotonic() + 120
    try:
        while b"Proceed? (y/n):" not in seen:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            chunk = os.read(proc.stdout.fileno(), 65536) if ready else b""
            if not chunk:
                pytest.fail("no pre-flight prompt:\n" + seen.decode(errors="replace")[-3000:])
            seen += chunk
        yield proc, tmp_path / "root"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.mark.parametrize("signum, status, headline", [
    (signal.SIGINT, 130, "INTERRUPTED: Ctrl+C detected."),
    (signal.SIGHUP, 129, "STOPPED: the terminal was closed (SIGHUP)."),
], ids=["ctrl-c", "terminal-closed"])
def test_stop_signal_is_reported_in_the_log(at_the_prompt, signum, status, headline):
    proc, root = at_the_prompt
    # Ctrl+C and a closed window reach the tee as well, so the report is
    # written after the reader has gone.
    proc.stdout.close()
    proc.send_signal(signum)
    assert proc.wait(timeout=60) == status
    log, = {path.resolve() for path in root.glob("ai/*/logs/pipeline.log")}  # current_run links to it
    text = log.read_text()
    assert headline in text
    report = text[text.index(headline):]
    assert "Any chunk currently in progress has been abandoned." in report
    assert "Resume command:" in report and "--resume --run-dir" in report
    assert "Traceback" not in text and "BrokenPipeError" not in text


CAUGHT_HELPER = """
import signal, sys
import video_upscale_pipeline as p
signal.signal(signal.SIGHUP, p._terminal_closed)
try:
    result = p.is_valid_video(sys.argv[2])
except p.TerminalClosed:
    print("reached the top")
    sys.exit(129)
print(f"swallowed: is_valid_video returned {result}")
"""


def test_hangup_inside_a_catch_all_helper_reaches_the_top(tmp_path):
    # is_valid_video() catches every Exception around its ffprobe. ffprobe
    # blocks opening a FIFO that nothing writes, so the hangup is sure to
    # arrive while the pipeline is inside that handler.
    if not (shutil.which("ffprobe") and shutil.which("pgrep")):
        pytest.skip("ffprobe and pgrep required")
    fifo = tmp_path / "never-written.mp4"
    os.mkfifo(fifo)
    # Its own process group, so cleanup after a failure reaches the blocked
    # ffprobe as well as the Python child.
    proc = subprocess.Popen([sys.executable, "-c", CAUGHT_HELPER, "unused", str(fifo)],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while subprocess.run(["pgrep", "-P", str(proc.pid), "-x", "ffprobe"],
                             capture_output=True).returncode != 0:
            assert proc.poll() is None and time.monotonic() < deadline, "ffprobe never started"
            time.sleep(0.05)
        proc.send_signal(signal.SIGHUP)
        output = proc.communicate(timeout=60)[0].decode()
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # the child and its ffprobe have both exited
        proc.wait()
    assert proc.returncode == 129, output
    assert "reached the top" in output


def test_only_y_or_n_answers_a_prompt(monkeypatch, capsys):
    import video_upscale_pipeline as p
    answers, prompts = iter(["", "yes", " x ", " N "]), []

    def typed(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(builtins, "input", typed)
    assert p.ask_yes_no("  Proceed? (y/n): ") is False
    assert prompts == ["  Proceed? (y/n): "] * 4
    assert capsys.readouterr().out.count("Answer y or n (Ctrl+C to stop).") == 3


def test_closed_input_is_not_an_answer(monkeypatch):
    import video_upscale_pipeline as p

    def closed(prompt):
        raise EOFError

    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(builtins, "input", closed)
    with pytest.raises(SystemExit) as stopped:
        p.ask_yes_no("  Proceed? (y/n): ")
    assert stopped.value.code == 1


TYPED_AHEAD = """
import video_upscale_pipeline as p
print("answered", p.ask_yes_no("  Proceed? (y/n): "))
"""


def test_keys_typed_before_the_prompt_do_not_answer_it():
    # The Enter is queued at the terminal before the child starts, as one
    # typed during preparation's encode is; only the y typed after the prompt
    # appeared may answer it.
    master, slave = pty.openpty()
    try:
        os.write(master, b"\n")
        proc = subprocess.Popen([sys.executable, "-c", TYPED_AHEAD], cwd=ROOT, stdin=slave,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        seen, deadline = b"", time.monotonic() + 60
        while b"Proceed? (y/n):" not in seen:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            chunk = os.read(proc.stdout.fileno(), 65536) if ready else b""
            if not chunk:
                proc.kill()
                pytest.fail("no prompt:\n" + seen.decode(errors="replace"))
            seen += chunk
        os.write(master, b"y\n")
        output = (seen + proc.communicate(timeout=60)[0]).decode()
    finally:
        os.close(master)
        os.close(slave)
    assert proc.returncode == 0, output
    assert "answered True" in output
    assert "Answer y or n" not in output, output


def test_only_y_or_n_answers_the_preflight_prompt(at_the_prompt):
    proc, _ = at_the_prompt
    output = proc.communicate(b"\nyes\nn\n", timeout=60)[0].decode()
    assert proc.returncode == 0, output
    assert output.count("Answer y or n (Ctrl+C to stop).") == 2, output
    assert "Exiting. Add --yes" in output
