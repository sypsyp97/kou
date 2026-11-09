"""Exercise the managed daemon contract through the real client process."""

from contextlib import contextmanager
import socket
import sys

import pytest

from conftest import FakeDaemonSocket


@contextmanager
def replying_socket(path, reply):
    server = FakeDaemonSocket(path, reply).start()
    try:
        yield server
    finally:
        server.stop()


def managed_environment(path=None):
    env = {"ZH_VOICE_MANAGED_DAEMON": "1", "STUB_ASR_STDOUT": "CLI MUST NOT RUN"}
    if path is not None:
        env["ZH_VOICE_SOCKET"] = str(path)
    return env


def assert_no_recovery_processes(harness):
    assert harness.calls("mlx-qwen3-asr") == [], "Managed client started CLI ASR"
    assert harness.daemon_launches(wait=0.2) == [], "Managed client started a daemon"


@pytest.mark.parametrize("stale", [False, True], ids=["missing-socket", "stale-socket"])
def test_managed_unavailable_server_exits_75_without_recovery(harness, stale):
    path = harness.tmp / "managed.sock"
    if stale:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
    result = harness.run([harness.wav()], extra_env=managed_environment(path))
    assert result.returncode == 75, result.stderr
    assert result.stdout == ""
    assert_no_recovery_processes(harness)


@pytest.mark.parametrize("reply", [
    "ERROR: RuntimeError: server failed",
    "META asr_ms=12\nERROR: RuntimeError: server failed",
], ids=["plain-error", "metadata-error"])
def test_managed_error_reply_exits_75_without_recovery(harness, reply):
    path = harness.tmp / "managed.sock"
    with replying_socket(path, reply) as server:
        result = harness.run([harness.wav()], extra_env=managed_environment(path))
        assert server.requests, "The test server was not contacted"
    assert result.returncode == 75, result.stderr
    assert result.stdout == ""
    assert_no_recovery_processes(harness)


@pytest.mark.parametrize("reply", [
    "unframed transcript",
    "META asr_ms=no-number\ntext must not escape",
    "META asr_ms=\ntext must not escape",
    "META asr_ms=-1\ntext must not escape",
    "META asr_ms=1.5\ntext must not escape",
    "META wrong_key=12\ntext must not escape",
], ids=["missing", "nonnumeric", "empty-number", "negative", "fractional", "wrong-field"])
def test_managed_malformed_metadata_exits_75_without_recovery(harness, reply):
    path = harness.tmp / "managed.sock"
    with replying_socket(path, reply) as server:
        result = harness.run([harness.wav()], extra_env=managed_environment(path))
        assert server.requests, "The test server was not contacted"
    assert result.returncode == 75, result.stderr
    assert result.stdout == "", "Invalid metadata leaked transcript text"
    assert_no_recovery_processes(harness)


@pytest.mark.parametrize("transcript", ["管理模式转写", ""], ids=["text", "empty"])
def test_managed_valid_metadata_retains_normal_success(harness, transcript):
    path = harness.tmp / "managed.sock"
    with replying_socket(path, "META asr_ms=0\n" + transcript) as server:
        result = harness.run([harness.wav()], extra_env=managed_environment(path))
        assert server.requests, "The test server was not contacted"
    assert result.returncode == 0, result.stderr
    assert result.stdout == (transcript + "\n" if transcript else "")
    assert_no_recovery_processes(harness)


def test_client_socket_override_chooses_requested_server(harness):
    default_server = harness.live_daemon(reply="META asr_ms=3\nwrong server")
    path = harness.tmp / "managed.sock"
    with replying_socket(path, "META asr_ms=7\nchosen server") as chosen:
        result = harness.run([harness.wav()], extra_env=managed_environment(path))
        assert chosen.requests, "Client ignored ZH_VOICE_SOCKET"
    assert result.returncode == 0, result.stderr
    assert result.stdout == "chosen server\n"
    assert default_server.requests == [], "Client contacted the default socket despite override"
    assert_no_recovery_processes(harness)


def test_client_without_socket_override_keeps_default_path(harness):
    server = harness.live_daemon(reply="META asr_ms=7\ndefault server")
    result = harness.run([harness.wav()], extra_env=managed_environment())
    assert result.returncode == 0, result.stderr
    assert result.stdout == "default server\n"
    assert server.requests, "Client changed its default socket path"
    assert_no_recovery_processes(harness)


def test_daemon_and_managed_client_share_socket_override(harness, daemon_factory):
    path = harness.tmp / "shared.sock"
    daemon = daemon_factory(start=False, env_extra={"ZH_VOICE_SOCKET": str(path)})
    old_default = daemon.sock
    # The fixture's probes must use the public override too.
    daemon.sock = path
    daemon.start()
    assert path.is_socket()
    assert not old_default.exists(), "Daemon bound its default socket instead of override"
    assert daemon.request("PING\n\n").startswith("PONG")
    before = len(daemon.asr_calls())
    result = harness.run([harness.wav()], extra_env=managed_environment(path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "Stub daemon should return its normal transcript"
    assert not result.stdout.startswith("META ")
    assert len(daemon.asr_calls()) > before, "The resident daemon did not process the clip"
    assert_no_recovery_processes(harness)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
