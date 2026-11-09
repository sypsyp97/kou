"""Hermetic fixtures for the zh-voice suite.

Nothing in here may reach the live system. Every test gets

  * its own $HOME (so ~/.cache/zh-voice/asr.sock, ~/.config/zh-voice/terms.txt
    and ~/.local/bin/zh-voice-daemon all point into a tmp dir),
  * its own $TMPDIR,
  * a $PATH whose first entry is Tests/runtime/stubs/bin, so `nc`, `soxi`, `ffmpeg`,
    `opencc` and `mlx-qwen3-asr` are recording stubs and never the real
    binaries,
  * $STUB_LOG_DIR, where each stub appends one JSON line per invocation.

No real microphone, no real model, no real socket, no network.

Hermeticity is also *policed*: `live_tmp_guard` is autouse, and it watches the
real /tmp/zh-voice-* paths that the live installation owns
(zh-voice-daemon.log, zh-voice-warmup.wav, zh-voice-ptt.wav). If a test clobbers
one, the guard puts the original back and records the violation so the test that
caused it can fail on it. Prevention is not possible from the test side while
the scripts hardcode `/tmp/...`; making those paths honour $TMPDIR is part of
the spec (see the two tmpdir tests).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "Runtime"
SUPPORT = ROOT / "Support"
ZH_VOICE = RUNTIME / "zh-voice"
ZH_VOICE_DAEMON = RUNTIME / "zh-voice-daemon"

STUB_BIN = Path(__file__).resolve().parent / "stubs" / "bin"
PYSTUBS = Path(__file__).resolve().parent / "stubs" / "pystubs"

# Deliberately excludes /opt/homebrew/bin: the scripts must run on nothing but
# the stubs plus base-system utilities (mktemp/sed/tr/awk/grep/tail/nohup).
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# Everything the live installation keeps in the real /tmp. Nothing the suite
# runs may create, truncate or rewrite any of these.
LIVE_TMP_GLOB = "/tmp/zh-voice-*"


def real(path):
    """Resolve /tmp -> /private/tmp so path comparisons actually compare."""
    return os.path.realpath(str(path))


@pytest.fixture
def sandbox_root():
    """A *short* per-test scratch root.

    Not pytest's tmp_path: macOS caps an AF_UNIX path at 104 bytes and
    /var/folders/.../pytest-of-x/pytest-123/test_name0/... blows straight
    through that before we even get to .cache/zh-voice/asr.sock.
    """
    root = Path(tempfile.mkdtemp(prefix="zhv-", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


# ---------------------------------------------------------------------------
# real-/tmp hermeticity guard
# ---------------------------------------------------------------------------

def _snapshot_live_tmp():
    snap = {}
    for p in sorted(glob.glob(LIVE_TMP_GLOB)):
        try:
            if not os.path.isfile(p):
                continue
            with open(p, "rb") as fh:
                snap[real(p)] = fh.read()
        except OSError:
            continue
    return snap


class LiveTmpGuard:
    """Watches the live installation's /tmp/zh-voice-* files across one test.

    An *append* is fine (a real daemon running on this machine logs a line now
    and then). A truncation, a rewrite or a brand-new file is a violation: that
    is the suite reaching outside its sandbox. Violations are repaired, so a red
    test never leaves the user's system damaged, and reported, so the test that
    caused it can assert on them.
    """

    def __init__(self):
        self.before = {}
        self._violations = []

    def start(self):
        self.before = _snapshot_live_tmp()
        return self

    def finish(self):
        after = _snapshot_live_tmp()
        for path, old in self.before.items():
            new = after.get(path)
            if new is None:
                self._violations.append("%s was deleted" % path)
                self._restore(path, old)
            elif new != old and not new.startswith(old):
                self._violations.append(
                    "%s was rewritten (%d B -> %d B)" % (path, len(old), len(new)))
                self._restore(path, old)
        for path in after:
            if path not in self.before:
                self._violations.append("%s was created" % path)
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return self._violations

    @staticmethod
    def _restore(path, content):
        try:
            with open(path, "wb") as fh:
                fh.write(content)
        except OSError:
            pass

    def violations(self):
        """Violations seen so far. Call `poll()` first to sample *now*."""
        return list(self._violations)

    def poll(self):
        """Re-check mid-test (and repair), then return the violations so far."""
        self.finish()
        self.before = _snapshot_live_tmp()
        return self.violations()


@pytest.fixture(autouse=True)
def live_tmp_guard():
    g = LiveTmpGuard().start()
    try:
        yield g
    finally:
        g.finish()


@pytest.fixture(scope="session", autouse=True)
def live_tmp_session_guard():
    """Last line of defence for the *orphan* writer.

    The background relaunch is `( nohup ... & )`: the grandchild is reparented
    and can get around to opening its log after the test that spawned it has
    already been torn down, i.e. after the per-test guard put the original file
    back. One more pass at the end of the session, after a settling pause,
    catches that straggler.
    """
    g = LiveTmpGuard().start()
    try:
        yield g
    finally:
        time.sleep(1.0)
        g.finish()


def write_wav(path, seconds=1.0, rate=16000, channels=1):
    """A real (silent-ish) wav file, so the stub soxi reads real headers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, int(rate * seconds))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * frames * channels)
    return path


class FakeDaemonSocket:
    """A unix-socket server speaking the daemon's plain-text protocol.

    Used to stand in for a *live* daemon. `reply` may be a string or a
    callable(request_text) -> string.
    """

    def __init__(self, path, reply="来自守护进程的结果"):
        self.path = str(path)
        self.reply = reply
        self.requests = []
        self._srv = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(self.path):
            os.unlink(self.path)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.settimeout(0.2)
        self._srv.bind(self.path)
        self._srv.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(3)
                buf = b""
                while buf.count(b"\n") < 2:
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buf += chunk
                req = buf.decode("utf-8", "replace")
                self.requests.append(req)
                reply = self.reply(req) if callable(self.reply) else self.reply
                conn.sendall((reply + "\n").encode("utf-8"))
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Harness:
    """Everything a test needs to drive Runtime/zh-voice in a sealed sandbox."""

    def __init__(self, tmp_path):
        self.tmp = Path(tmp_path)
        self.home = self.tmp / "home"
        self.logs = self.tmp / "stublogs"
        self.tmpdir = self.tmp / "tmp"
        for d in (self.home, self.logs, self.tmpdir,
                  self.home / ".local" / "bin",
                  self.home / ".cache" / "zh-voice",
                  self.home / ".config" / "zh-voice"):
            d.mkdir(parents=True, exist_ok=True)

        # The client launches the daemon by absolute path; give it the stub.
        shutil.copy2(STUB_BIN / "zh-voice-daemon",
                     self.home / ".local" / "bin" / "zh-voice-daemon")

        self.sock = self.home / ".cache" / "zh-voice" / "asr.sock"
        self.terms = self.home / ".config" / "zh-voice" / "terms.txt"
        # A real .mlmodelc is a directory; the stub detector never opens it, but
        # the client checks `-d` before deciding the VAD is usable at all.
        self.vad_model = self.home / ".cache" / "zh-voice" / "silero_vad.mlmodelc"
        self.vad_model.mkdir(parents=True, exist_ok=True)
        self._servers = []

        self.env = {
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "PATH": os.pathsep.join(
                [str(STUB_BIN), str(self.home / ".local" / "bin"), SYSTEM_PATH]),
            "STUB_LOG_DIR": str(self.logs),
            # the client launches the daemon detached; without this it would
            # write a bytecode cache into the sandbox *after* teardown
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "en_US.UTF-8",
            "LANG": "en_US.UTF-8",
            "ZH_VOICE_MODEL": "stub/model",
            "ZH_VOICE_VAD_MODEL_PATH": str(self.vad_model),
        }

    # ---- inputs -------------------------------------------------------
    def wav(self, name="in.wav", seconds=1.0, rate=16000, channels=1):
        return write_wav(self.tmp / name, seconds, rate, channels)

    def write_terms(self, text, path=None):
        p = Path(path) if path else self.terms
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def remove_terms(self):
        if self.terms.exists():
            self.terms.unlink()

    def live_daemon(self, reply="来自守护进程的结果"):
        srv = FakeDaemonSocket(self.sock, reply).start()
        self._servers.append(srv)
        return srv

    def stale_socket(self):
        """A socket file on disk with nobody listening: bind, then drop it."""
        self.sock.parent.mkdir(parents=True, exist_ok=True)
        if self.sock.exists():
            self.sock.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.sock))
        s.close()
        assert self.sock.is_socket()
        return self.sock

    # ---- running ------------------------------------------------------
    def run(self, args=(), extra_env=None, timeout=30):
        env = dict(self.env)
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items()})
        return subprocess.run(
            [str(ZH_VOICE)] + [str(a) for a in args],
            env=env, capture_output=True, text=True, timeout=timeout,
            cwd=str(self.tmp))

    # ---- observations -------------------------------------------------
    def calls(self, stub):
        """Every recorded invocation of a stub, as a list of argv lists."""
        p = self.logs / ("%s.log" % stub)
        if not p.exists():
            return []
        return [json.loads(line) for line in
                p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def daemon_launches(self, wait=3.0):
        """Poll for the background daemon marker (it is launched detached).

        Each record carries `logPath`: the file the launcher pointed the
        daemon's stdout at, read back from fd 1 with F_GETPATH. That is how a
        test can tell a $TMPDIR-scoped log from a clobbered real /tmp one.
        """
        p = self.logs / "daemon-launched.log"
        deadline = time.time() + wait
        while time.time() < deadline:
            if p.exists() and p.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.05)
        if not p.exists():
            return []
        return [json.loads(line) for line in
                p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def close(self):
        # The relaunch is detached; give it a moment to actually exec before
        # the sandbox (and the daemon stub inside it) is deleted out from under
        # it. Only costs anything when the relaunch branch was taken at all.
        if self.calls("pgrep") and not self.daemon_launches(wait=2.0):
            time.sleep(0.5)
        for srv in self._servers:
            srv.stop()


@pytest.fixture
def harness(sandbox_root):
    h = Harness(sandbox_root)
    try:
        yield h
    finally:
        h.close()


# ---------------------------------------------------------------------------
# daemon fixtures
# ---------------------------------------------------------------------------

class DaemonProc:
    """A `Runtime/zh-voice-daemon` running against stubbed mlx imports."""

    def __init__(self, tmp_path, env_extra=None):
        self.tmp = Path(tmp_path)
        self.home = self.tmp / "dhome"
        self.logs = self.tmp / "dlogs"
        self.tmpdir = self.tmp / "dtmp"
        for d in (self.home, self.logs, self.home / ".cache" / "zh-voice"):
            d.mkdir(parents=True, exist_ok=True)
        self.sock = self.home / ".cache" / "zh-voice" / "asr.sock"
        # inherit the interpreter's env, minus anything the user may have set
        # for the live daemon (ZH_VOICE_IDLE / _MODEL would skew the tests)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("ZH_VOICE_") and not k.startswith("STUB_")}
        self.env.update({
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "PYTHONPATH": str(PYSTUBS),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "STUB_LOG_DIR": str(self.logs),
            "ZH_VOICE_MODEL": "stub/model",
        })
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        if env_extra:
            self.env.update({k: str(v) for k, v in env_extra.items()})
        self.proc = None

    def stale_socket(self):
        """Leave a socket file behind with nobody listening, as a crashed
        daemon would have."""
        self.sock.parent.mkdir(parents=True, exist_ok=True)
        if self.sock.exists():
            self.sock.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.sock))
        s.close()
        assert self.sock.is_socket()
        return self.sock

    def start(self, wait_ready=True, timeout=20):
        self.stdout_path = self.tmp / "daemon-stdout.txt"
        self._out = open(str(self.stdout_path), "wb")
        self.proc = subprocess.Popen(
            [sys.executable, str(ZH_VOICE_DAEMON)],
            env=self.env, stdout=self._out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, cwd=str(self.tmp))
        if wait_ready:
            self.wait_ready(timeout)
        return self

    def wait_ready(self, timeout=20):
        """Block until the daemon answers.

        The probe sends a *complete* request and drains the reply on purpose:
        a probe that hangs up early would itself be a protocol edge case, and
        that belongs in the test for it, not in every fixture.

        A drained reply is not by itself proof of life: if the daemon dies
        mid-probe the socket just EOFs and the drain loop exits cleanly. So the
        liveness check is repeated *after* the probe, with a short grace so a
        daemon that is in the middle of falling over is not mistaken for a
        healthy one - otherwise the fixture reports ready, the test's own
        `s.connect` blows up with a raw FileNotFoundError from inside conftest,
        and the real assertion never gets to run.
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(
                    "daemon exited early (rc=%s):\n%s"
                    % (self.proc.returncode, self.output()))
            if self.sock.exists():
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(2)
                    s.connect(str(self.sock))
                    try:
                        s.sendall(b"\n\n")
                        s.shutdown(socket.SHUT_WR)
                        while s.recv(65536):
                            pass
                    finally:
                        s.close()
                    try:
                        self.proc.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        return self          # still up: genuinely ready
                    raise AssertionError(
                        "daemon answered the readiness probe and then died "
                        "(rc=%s):\n%s" % (self.proc.returncode, self.output()))
                except OSError as exc:
                    last = exc
            time.sleep(0.05)
        raise AssertionError(
            "daemon never became reachable on %s (last error %r)\n%s"
            % (self.sock, last, self.output()))

    def asr_calls(self):
        """Every `mlx_qwen3_asr.transcribe()` the daemon made, in order.

        The first one is the warm-up: that is how a test can see which scratch
        wav the daemon burned the lazy-weight cost on.
        """
        p = self.logs / "asr-transcribe.log"
        if not p.exists():
            return []
        return [json.loads(line) for line in
                p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def request(self, payload, timeout=5, half_close=True):
        """Send raw bytes, return the decoded reply (or raise on timeout)."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(self.sock))
        try:
            s.sendall(payload)
            if half_close:
                s.shutdown(socket.SHUT_WR)
            out = b""
            while b"\n" not in out:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
            return out.decode("utf-8", "replace")
        finally:
            s.close()

    def transcribe(self, wav_path, context="", timeout=5, half_close=True):
        return self.request("%s\n%s\n" % (wav_path, context), timeout=timeout,
                            half_close=half_close)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def output(self):
        try:
            self._out.flush()
        except Exception:
            pass
        try:
            return Path(self.stdout_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return "<no output>"

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            self._out.close()
        except Exception:
            pass


@pytest.fixture
def daemon_factory(sandbox_root):
    made = []

    def make(env_extra=None, start=True, wait_ready=True):
        d = DaemonProc(sandbox_root / ("d%d" % len(made)), env_extra)
        made.append(d)
        if start:
            d.start(wait_ready=wait_ready)
        return d

    try:
        yield make
    finally:
        for d in made:
            d.stop()
