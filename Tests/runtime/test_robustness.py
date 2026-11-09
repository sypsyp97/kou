"""Reply framing and health checks while the inference worker is occupied."""
import socket
import time

from conftest import write_wav


def test_empty_metadata_reply_does_not_paste_or_load_another_model(harness):
    harness.live_daemon(reply="META asr_ms=17\n")
    result = harness.run([harness.wav(seconds=1)])
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert harness.calls("mlx-qwen3-asr") == []


def test_metadata_reply_keeps_transcript_and_timing(harness):
    harness.live_daemon(reply="META asr_ms=17\nhello")
    result = harness.run([harness.wav(seconds=1)])
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert "asr_ms=17" in (harness.home / ".cache/zh-voice/metrics.log").read_text()


def test_health_remains_responsive_until_inference_deadline(daemon_factory):
    d = daemon_factory(env_extra={"STUB_ASR_HANG_ON": "blocked.wav",
                                  "ZH_VOICE_HANG_FLOOR": "2",
                                  "ZH_VOICE_HANG_CEIL": "2"})
    wav = write_wav(d.tmp / "blocked.wav", 1)
    with socket.socket(socket.AF_UNIX) as pending:
        pending.settimeout(5)
        pending.connect(str(d.sock))
        pending.sendall((str(wav) + "\n\n").encode())
        time.sleep(0.1)
        with socket.socket(socket.AF_UNIX) as health:
            health.settimeout(0.5)
            health.connect(str(d.sock) + ".health")
            health.sendall(b"PING\n\n")
            reply = health.recv(4096)
        assert reply.startswith(b"PONG ")
        assert b"state=busy" in reply
        assert pending.recv(4096).startswith(b"ERROR:")
    d.proc.wait(timeout=8)
    assert d.proc.returncode == 70
    assert not d.sock.exists()
    assert not d.sock.with_name(d.sock.name + ".health").exists()


def test_health_listener_rejects_transcription_requests(daemon_factory):
    d = daemon_factory()
    with socket.socket(socket.AF_UNIX) as health:
        health.settimeout(1)
        health.connect(str(d.sock) + ".health")
        health.sendall(b"/invalid.wav\n\n")
        assert b"accepts PING only" in health.recv(4096)
    assert d.alive()
