"""Behaviour spec for Runtime/zh-voice-daemon (the resident ASR server).

The heavy imports (`mlx_qwen3_asr`, `mlx.core`) are replaced by
Tests/runtime/stubs/pystubs on PYTHONPATH, so the daemon boots in milliseconds with no
model, no Metal and no network. $HOME is a tmp dir, so the socket it binds is
never the live one.
"""
import os
import re
import signal
import socket
import subprocess
import time

import pytest

from conftest import ZH_VOICE_DAEMON, real, write_wav


def wait_gone(proc, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


# --- 13 ------------------------------------------------------------------
def test_sighup_is_ignored_so_nohup_survives_shell_exit(daemon_factory):
    """REGRESSION: installing a SIGHUP handler cancels nohup's inherited
    SIG_IGN, so the daemon died the moment its launching shell exited."""
    d = daemon_factory()
    wav = write_wav(d.tmp / "clip.wav", 0.5)

    os.kill(d.proc.pid, signal.SIGHUP)
    time.sleep(0.5)

    assert d.alive(), (
        "SIGHUP killed the daemon (rc=%s); it must be ignored outright\n%s"
        % (d.proc.returncode, d.output()))
    reply = d.transcribe(str(wav))
    assert reply.strip() != "" and not reply.startswith("ERROR:"), (
        "daemon stopped serving after SIGHUP: %r" % (reply,))


# --- 14 ------------------------------------------------------------------
def test_sigterm_exits_cleanly_and_removes_socket(daemon_factory):
    """A supervised shutdown must not leave a socket file for the next run to
    trip over."""
    d = daemon_factory()
    assert d.sock.exists()

    os.kill(d.proc.pid, signal.SIGTERM)

    assert wait_gone(d.proc, 5), "daemon ignored SIGTERM\n%s" % d.output()
    assert d.proc.returncode == 0, (
        "SIGTERM must be a clean exit, got rc=%s\n%s"
        % (d.proc.returncode, d.output()))
    assert not d.sock.exists(), (
        "the socket file must be unlinked on shutdown, else the client sees a "
        "phantom daemon")


# --- 15 ------------------------------------------------------------------
def test_stale_socket_file_is_removed_and_bind_succeeds(daemon_factory):
    """Starting on top of a crashed predecessor's socket must just work."""
    d = daemon_factory(start=False)
    stale = d.stale_socket()
    stale_inode = stale.stat().st_ino

    d.start()

    assert d.alive(), "daemon failed to start over a stale socket\n%s" % d.output()
    assert d.sock.is_socket()
    assert d.sock.stat().st_ino != stale_inode, (
        "the daemon bound to (or reused) the stale socket file instead of "
        "replacing it")
    wav = write_wav(d.tmp / "clip.wav", 0.5)
    assert not d.transcribe(str(wav)).startswith("ERROR:")


# --- 16 ------------------------------------------------------------------
def test_malformed_request_gets_error_line_and_server_stays_alive(daemon_factory):
    """Protocol: 2 lines in; `META asr_ms=<n>` then exactly one transcript line out.

    The transcript MUST be the last line and the only transcript line: zh-voice
    extracts it with `${_raw##*\\n}`. An ERROR reply carries no META line: the client
    checks the last line for the ERROR: prefix, and an extra header line would
    hide it.

    Three ways a client can misbehave, none of which may take the resident
    model down with it:
      a) it hangs up before reading the reply (the write then EPIPEs),
      b) it sends half a request and just sits there (the read must be
         bounded, or one stuck client wedges the single-threaded accept loop),
      c) it comes back afterwards with a good request, which must still work.
    """
    d = daemon_factory()
    wav = write_wav(d.tmp / "clip.wav", 0.5)

    good = d.transcribe(str(wav))
    assert good.endswith("\n") and good.count("\n") == 2, (
        "transcribe reply must be one META line plus one transcript line: %r" % (good,))
    head, transcript, _ = good.split("\n")
    assert re.match(r"^META asr_ms=\d+$", head), "first line must be server ASR time: %r" % (head,)
    assert transcript and not transcript.startswith("ERROR:")

    # (a) client vanishes before the reply is written
    gone = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    gone.settimeout(5)
    gone.connect(str(d.sock))
    gone.sendall(b"\n\n")
    gone.close()
    time.sleep(0.3)
    assert d.alive(), (
        "a client that hung up before reading its reply killed the daemon\n%s"
        % d.output())

    # (b) half a request, connection deliberately left open
    stuck = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stuck.settimeout(5)
    stuck.connect(str(d.sock))
    try:
        stuck.sendall(b"only-one-line-and-no-context")
        try:
            reply = stuck.recv(65536).decode("utf-8", "replace")
        except socket.timeout:
            pytest.fail(
                "no reply to a truncated request within 5 s: the read must be "
                "bounded so one stuck client cannot block every other one")
        assert reply.startswith("ERROR:"), (
            "a malformed request must be answered with an ERROR: line, got %r"
            % (reply,))

        # (c) back to business
        assert d.alive(), "the server died on a malformed request\n%s" % d.output()
        try:
            again = d.transcribe(str(wav), timeout=5)
        except socket.timeout:
            pytest.fail("server stopped serving after a malformed request")
        assert not again.startswith("ERROR:"), (
            "the next good request must succeed, got %r" % (again,))
    finally:
        stuck.close()


# --- 17 ------------------------------------------------------------------
def test_transcription_exception_replies_error_and_server_survives(daemon_factory):
    """One bad clip must cost one request, not the whole resident model."""
    d = daemon_factory(env_extra={"STUB_ASR_RAISE_ON": "poison"})
    bad_wav = write_wav(d.tmp / "poison.wav", 0.5)
    good_wav = write_wav(d.tmp / "clip.wav", 0.5)

    reply = d.transcribe(str(bad_wav))
    assert reply.startswith("ERROR:"), (
        "a transcription exception must come back as ERROR:, got %r" % (reply,))
    assert reply.count("\n") == 1, "still exactly one line: %r" % (reply,)

    assert d.alive(), "the server died on a failed transcription\n%s" % d.output()
    again = d.transcribe(str(good_wav))
    assert not again.startswith("ERROR:") and again.strip() != "", (
        "the server must keep serving after a failure, got %r" % (again,))


# --- 18 ------------------------------------------------------------------
def test_idle_timeout_makes_daemon_exit_on_its_own(daemon_factory):
    """It holds ~3.4 GB resident; the idle timeout must actually release it,
    which means the idle check has to run on a much finer grain than the
    timeout itself."""
    d = daemon_factory(env_extra={"ZH_VOICE_IDLE": "1"})

    assert wait_gone(d.proc, 6), (
        "ZH_VOICE_IDLE=1 but the daemon was still alive 6 s later: the idle "
        "poll interval must be small relative to the configured timeout\n%s"
        % d.output())
    assert not d.sock.exists(), (
        "an idle-timeout exit must clean up its socket file too")


# --- 28 ------------------------------------------------------------------
def test_warmup_scratch_wav_honours_tmpdir(daemon_factory, live_tmp_guard):
    """The warm-up scratch wav must be written under $TMPDIR, not a fixed path.

    `/tmp/zh-voice-warmup.wav` is rewritten on every start, so two daemons
    starting at once race on the same file - and it makes this suite trample the
    live installation, since every daemon test here goes through warm_up().
    """
    d = daemon_factory()

    calls = d.asr_calls()
    assert calls, (
        "the daemon must warm the model up before serving; MLX materialises "
        "weights lazily and the first real request would otherwise pay for it")
    warm = calls[0]["path"]
    assert warm.startswith(real(d.tmpdir) + "/"), (
        "the warm-up wav was written to %s instead of somewhere under $TMPDIR "
        "(%s)" % (warm, real(d.tmpdir)))
    assert live_tmp_guard.poll() == [], (
        "starting the daemon touched the live installation's files in the real "
        "/tmp: %s" % (live_tmp_guard.violations(),))


# --- 19 ------------------------------------------------------------------
def test_daemon_detaches_into_its_own_session(daemon_factory):
    """setsid: the launching terminal's group signals must not reach it."""
    d = daemon_factory()

    sid = os.getsid(d.proc.pid)
    assert sid == d.proc.pid, (
        "daemon is still in the launcher's session (sid=%s, pid=%s, our sid=%s);"
        " it must call setsid() so closing the terminal cannot take it down"
        % (sid, d.proc.pid, os.getsid(0)))
    assert d.alive()


# --- 34 ------------------------------------------------------------------
def test_idle_timeout_off_keeps_the_model_resident(daemon_factory):
    """ZH_VOICE_IDLE<=0 is the launchd-managed mode: never release the model.

    The old watchdog took `poll = max(0.05, IDLE_SEC/20)` and compared against
    `> IDLE_SEC`, so a non-positive timeout meant "exit 50 ms from now" - the
    exact opposite of what the plist asks for, and it would have looped against
    KeepAlive at ~20 restarts a second.
    """
    d = daemon_factory(env_extra={"ZH_VOICE_IDLE": "0"})

    time.sleep(2)

    assert d.alive(), (
        "ZH_VOICE_IDLE=0 must disable the idle timeout, but the daemon exited "
        "(rc=%s)\n%s" % (d.proc.returncode, d.output()))
    assert d.sock.exists(), "a live daemon must keep its socket bound"

    wav = write_wav(d.tmp / "clip.wav", 0.5)
    reply = d.transcribe(str(wav))
    assert reply.strip() != "" and not reply.startswith("ERROR:"), (
        "resident daemon stopped serving: %r" % (reply,))


# --- 36 ------------------------------------------------------------------
def test_wedged_transcription_makes_the_daemon_exit_so_launchd_replaces_it(
        daemon_factory):
    """A process that is alive while its service is dead must have a way out.

    When MLX/Metal hangs, the process and the socket still exist. launchd
    KeepAlive only checks that the process exists, so it sees nothing wrong. The
    accept loop is single-threaded, so every key press first waits for the 90 s nc
    timeout of the client before it falls back to the one-shot CLI. The only
    self-healing step is that the daemon admits the failure and exits, so that
    launchd starts a new one.

    This test also pins that the watchdog must not depend on ZH_VOICE_IDLE. The
    production plist uses IDLE=0. If the watchdog depended on it, the only
    self-healing step would fail in the one configuration that needs it.
    """
    d = daemon_factory(env_extra={
        "STUB_ASR_HANG_ON": "wedge",
        "ZH_VOICE_IDLE": "0",           # the production mode
        "ZH_VOICE_HANG_FLOOR": "1",
        "ZH_VOICE_HANG_CEIL": "1",
    })
    wav = write_wav(d.tmp / "wedge.wav", 0.5)

    # The client must receive an ERROR first and only then see the daemon exit.
    # This order matters to the user: zh-voice falls back to the one-shot CLI at
    # once on ERROR, and the utterance is still transcribed. If the daemon just
    # died, the client would wait for the full 90 s nc timeout.
    reply = d.transcribe(str(wav), timeout=20)

    assert reply.startswith("ERROR:"), (
        "a hang must reply ERROR at once, not leave the client waiting for timeout or EOF; got %r\n%s"
        % (reply, d.output()))
    assert "转写卡死" in reply, "ERROR must say it is a hang, not another failure: %r" % (reply,)

    assert wait_gone(d.proc, 20), (
        "daemon must exit by itself after replying ERROR: KeepAlive cannot tell, "
        "and the socket stays, so nothing outside shows the service is dead\n%s" % d.output())
    assert d.proc.returncode == 70, (
        "hang exit must use a non-zero code (70 = EX_SOFTWARE) to differ from a "
        "normal exit in the launchd log; got rc=%s\n%s" % (d.proc.returncode, d.output()))
    assert not d.sock.exists(), (
        "the exit path must unlink the socket, or the client waits 90 s on a dead socket")
    assert "转写卡死" in d.output(), (
        "a hang exit must log its reason, or launchd shows an unexplained restart\n%s"
        % d.output())


# --- 37 ------------------------------------------------------------------
def test_ping_is_answered_without_touching_the_model(daemon_factory):
    """The health probe tests whether the accept loop still runs, so it must not run a transcription itself.

    A daemon that hangs in MLX cannot read this request at all (the transcription
    occupies the only thread). So "no PONG for a PING" is itself proof of death,
    and it is more accurate than pgrep, which also counts zombies.
    """
    d = daemon_factory()
    before = len(d.asr_calls())

    reply = d.request("PING\n\n")

    assert reply.startswith("PONG"), (
        "PING must be answered with PONG in place (health gate); got %r" % (reply,))
    assert "stub/model" in reply, (
        "PONG must include the loaded model so the caller can confirm it is the "
        "intended one, not a leftover daemon; got %r" % (reply,))
    assert reply.count("\n") == 1, "reply must stay one line: %r" % (reply,)
    assert len(d.asr_calls()) == before, (
        "PING ran a real transcription (%d -> %d): a health check must have no side "
        "effects and must not compete with real requests for the model thread" % (before, len(d.asr_calls())))
    assert d.alive()

    wav = write_wav(d.tmp / "clip.wav", 0.5)
    assert not d.transcribe(str(wav)).startswith("ERROR:"), (
        "daemon must keep serving after PING")


# --- 46 ------------------------------------------------------------------
def test_the_model_lives_in_a_separate_process_from_the_socket(daemon_factory):
    """The model must not live in the same process as the socket.

    A Metal call can hang without killing Python. If inference ran in the socket
    process, the only recovery would be a watchdog thread that calls os._exit, and
    whether that thread gets scheduled depends on whether the hung C call releases
    the GIL. Do not rely on that. With MLX in a child process, the parent never
    touches Metal and can always enforce the deadline.

    Check both sides: a child process exists (behavior), and mlx is not imported
    at module level in the parent (so that a later "simplification" is caught;
    no other test would fail on that regression).
    """
    d = daemon_factory()

    children = subprocess.run(["/usr/bin/pgrep", "-P", str(d.proc.pid)],
                              capture_output=True, text=True)
    kids = [line for line in children.stdout.split() if line.strip()]
    assert kids, (
        "daemon has no child process: model and socket share one process, so a hang "
        "depends on the GIL being released\n%s" % d.output())

    source = ZH_VOICE_DAEMON.read_text(encoding="utf-8")
    top_level = [line for line in source.splitlines()
                 if re.match(r"^(import|from)\s+mlx", line)]
    assert not top_level, (
        "mlx must not be imported at module top level: %r. spawn re-imports this "
        "file, so the parent would also pay a 3.4 GB load, and the parent must "
        "never touch Metal"
        % (top_level,))


# --- 47 ------------------------------------------------------------------
def test_empty_transcript_is_retried_without_the_term_bias(daemon_factory):
    """A full term-list line is a strong prior and can suppress the whole output of a short utterance.

    An empty result means the user spoke for nothing, so ask once more without the
    context. This costs one extra run only on a path that would otherwise return
    nothing.
    """
    d = daemon_factory(env_extra={"STUB_ASR_EMPTY_WITH_CONTEXT": "1"})
    wav = write_wav(d.tmp / "clip.wav", 0.5)
    before = len(d.asr_calls())

    reply = d.transcribe(str(wav), context="LIBERO sbatch mLSTM")

    assert reply.strip() and not reply.startswith("ERROR:"), (
        "when the glossary yields no text, retry without it instead of returning an empty result: %r" % (reply,))
    calls = d.asr_calls()[before:]
    assert len(calls) == 2, (
        "expected exactly two transcriptions, one with context and one without; got %d: %r"
        % (len(calls), calls))
    assert calls[0]["context"], "first attempt must still carry the glossary"
    assert not calls[1]["context"], "retry must not carry the glossary"


# --- 75 --- the pinned revision must take effect at run time ---------------
def test_pinned_snapshot_path_is_what_actually_gets_loaded(daemon_factory, tmp_path):
    """Passing only the repo id to load_model does not pin the version.

    In offline mode huggingface_hub reads refs/main, which is the most recently
    fetched snapshot in the cache. After an upstream update it differs from the
    pinned ASR_REVISION, and the install pre-check (which only checks that the
    directory exists) still passes. The only symptom is worse transcription, which
    is hard to diagnose. So pass the PATH of the snapshot, and echo the real
    source of the loaded model in PONG so that the install check can compare it.
    """
    snap = tmp_path / "snapshots" / ("b" * 40)
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")

    d = daemon_factory(env_extra={"ZH_VOICE_MODEL_PATH": str(snap)})
    reply = d.request("PING\n\n")

    assert "rev=" + "b" * 40 in reply, (
        "PONG must echo the loaded revision so the pin can be verified: %r" % (reply,))
    loaded = [c for c in d.asr_calls()]
    assert loaded, "worker did not even run warm-up"


def test_an_unusable_pin_says_unpinned_out_loud_instead_of_pretending(
        daemon_factory, tmp_path):
    """A pinned path is set but unusable (for example the snapshot was emptied by delete-cache). The behavior is a fallback to the repo id.

    The daemon still transcribes, but with other weights. This silent drift is
    what the pin must prevent, so the daemon reports it in two places: a warning
    line in the log, and rev=unpinned in PONG (it does not pretend to be pinned).
    The install check uses the PONG value as its deadline.
    """
    empty = tmp_path / "snapshots" / ("c" * 40)
    empty.mkdir(parents=True)                      # the directory exists, config.json does not

    d = daemon_factory(env_extra={"ZH_VOICE_MODEL_PATH": str(empty)})
    reply = d.request("PING\n\n")

    assert "rev=unpinned" in reply, (
        "an unusable pin must report unpinned, or drift passes verification: %r" % (reply,))
    log = d.stdout_path.read_text(encoding="utf-8", errors="replace")
    assert "ZH_VOICE_MODEL_PATH" in log, (
        "the fallback to the repo id must be logged: %r" % (log[:400],))


# --- 77 --- the tokenizer must also come from the pinned snapshot -------------
def test_tokenizer_is_pinned_to_the_snapshot_we_loaded(daemon_factory, tmp_path):
    """Without a pin, the tokenizer comes from the upstream DEFAULT_MODEL_ID repo and ignores our revision.

    Measured with HF_HOME pointing to an empty directory and HF_HUB_OFFLINE=1:
    without the pin, no transcription runs (LocalEntryNotFoundError). With the pin,
    the same environment transcribes normally. So with the pin, the cache of the
    upstream repo (1.7 GB locally, 99% of it 0.6B weights that are never loaded)
    is not needed.
    """
    snap = tmp_path / "snapshots" / ("e" * 40)
    snap.mkdir(parents=True)
    for name in ("config.json", "vocab.json", "merges.txt", "tokenizer_config.json"):
        (snap / name).write_text("{}", encoding="utf-8")

    d = daemon_factory(env_extra={"ZH_VOICE_MODEL_PATH": str(snap)})
    reply = d.request("PING\n\n")

    assert "tok=pinned" in reply, (
        "PONG must report tok=pinned so the tokenizer pin can be verified: %r" % (reply,))
    calls = d.asr_calls()
    assert calls, "worker did not transcribe even during warm-up"
    assert calls[0]["tokenizer_from"] == str(snap), (
        "the warm-up transcription used tokenizer %r: the pin must apply before any "
        "transcribe call, warm_up included" % (calls[0]["tokenizer_from"],))


def test_an_unpinnable_target_says_upstream_instead_of_pretending(
        daemon_factory, tmp_path):
    """If the pin is not possible (the pinned snapshot has no tokenizer files, or upstream changed its structure), report upstream.

    The install check then requires the upstream repo to exist with a tokenizer
    that is byte-identical to the pinned one. If the daemon pretended to be
    pinned, that fallback check would be skipped while the daemon uses the
    tokenizer of another repo."""
    snap = tmp_path / "snapshots" / ("f" * 40)
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")   # weights only, no tokenizer files

    d = daemon_factory(env_extra={"ZH_VOICE_MODEL_PATH": str(snap)})
    reply = d.request("PING\n\n")

    assert "tok=upstream" in reply, (
        "a snapshot without tokenizer files cannot be pinned; PONG must say upstream: %r" % (reply,))
    log = d.stdout_path.read_text(encoding="utf-8", errors="replace")
    assert "词表" in log, "the failed pin must be logged: %r" % (log[:400],)


def test_the_pin_uses_the_module_from_sys_modules_not_the_reexported_function():
    """`import mlx_qwen3_asr.transcribe as T` returns the FUNCTION (the package __init__ re-exports a function of the same name).

    Setting an attribute on it adds a field that nobody reads, and fails
    silently. The only reliable handle is sys.modules["mlx_qwen3_asr.transcribe"].
    """
    code = ZH_VOICE_DAEMON.read_text(encoding="utf-8")
    body = re.search(r"def pin_tokenizer_dir\(target\):(.*?)\ndef ", code, re.S)
    assert body, "pin_tokenizer_dir not found"
    # Look only at the lines that execute. The docstring deliberately contains the
    # wrong form as a warning, and matching it would make this assertion always fail.
    statements = body.group(1).split('"""')[-1]
    assert "sys.modules" in statements, (
        "the pin must go through sys.modules, not a re-exported function")
    assert not re.search(r"import\s+mlx_qwen3_asr\.transcribe\s+as", statements), (
        "this form imports the function, not the module")
