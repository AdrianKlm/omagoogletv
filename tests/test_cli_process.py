"""Running the backend as a real process.

Covers what unit tests cannot see: handling the various kinds of stdin, and
whether the process is able to exit at all.
"""

import subprocess
import sys

import pytest


def run_backend(stdin_bytes, tmp_path, *, stdin_from_file=None, timeout=25):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        # Private XDG: the test must touch neither a real device nor credentials.
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    command = [sys.executable, "-m", "google_tv_remote.cli"]
    if stdin_from_file is not None:
        with open(stdin_from_file, "rb") as fh:
            return subprocess.run(command, stdin=fh, capture_output=True,
                                  env=env, timeout=timeout)
    return subprocess.run(command, input=stdin_bytes, capture_output=True,
                          env=env, timeout=timeout)


def test_exits_when_stdin_is_a_pipe(tmp_path):
    result = run_backend(b'{"id":1,"action":"status"}\n', tmp_path)
    assert result.returncode == 0
    assert b'"id":1' in result.stdout


def test_exits_when_stdin_is_dev_null(tmp_path):
    # Previously: PermissionError from epoll and a process hanging forever.
    result = run_backend(None, tmp_path, stdin_from_file="/dev/null")
    assert result.returncode == 0, result.stderr.decode()


def test_works_when_stdin_is_a_regular_file(tmp_path):
    # Previously: ValueError("Pipe transport is for pipes/sockets only.")
    path = tmp_path / "input.jsonl"
    path.write_bytes(b'{"id":7,"action":"status"}\n')
    result = run_backend(None, tmp_path, stdin_from_file=str(path))
    assert result.returncode == 0, result.stderr.decode()
    assert b'"id":7' in result.stdout


def test_stdout_carries_json_only(tmp_path):
    result = run_backend(
        b'{"id":1,"action":"status"}\nnot json at all\n{"id":2,"action":"status"}\n',
        tmp_path)
    import json
    for line in result.stdout.decode().splitlines():
        if line.strip():
            json.loads(line)  # raises if anything else reaches stdout


def test_a_malformed_line_does_not_end_the_process(tmp_path):
    result = run_backend(b'not-json\n{"id":2,"action":"status"}\n', tmp_path)
    assert result.returncode == 0
    assert b'"id":2' in result.stdout


def test_sigterm_ends_the_process_cleanly(tmp_path):
    # Quickshell closes the helper with a signal, not by closing stdin. Without
    # handling it the session was never closed and the loop died with pending
    # tasks.
    import signal
    import time

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "google_tv_remote.cli"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    # Signalling before the loop starts would not prove anything useful.
    time.sleep(1.0)
    process.send_signal(signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        pytest.fail("the process did not exit after SIGTERM")

    assert process.returncode == 0, stderr.decode()
    # shutdown() must have run: without it there is no disconnect event.
    assert b'"state":"disconnected"' in stdout
    assert b"Task was destroyed" not in stderr
