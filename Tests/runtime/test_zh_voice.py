"""Behaviour spec for Runtime/zh-voice (the bash front end).

Every test runs the real script inside the sandbox built by conftest.Harness:
tmp $HOME, tmp $TMPDIR, a $PATH of recording stubs. The mic, the model, the
real socket and the network are unreachable from here by construction.
"""
import os
import pathlib

import pytest

from conftest import real


# --- 1 -------------------------------------------------------------------
def test_daemon_reply_is_printed_and_cli_not_invoked(harness):
    """Live daemon answers -> print its text, exit 0, never spawn the CLI."""
    harness.live_daemon(reply="守护进程转写结果")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "守护进程转写结果"
    assert harness.calls("mlx-qwen3-asr") == [], (
        "the one-shot CLI must not run when the daemon already answered")


# --- 2 -------------------------------------------------------------------
def test_daemon_error_reply_falls_back_to_cli(harness):
    """Daemon says `ERROR: ...` -> fall through to the CLI, still exit 0."""
    harness.live_daemon(reply="ERROR: RuntimeError: model exploded")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={
        "STUB_ASR_STDOUT": "命令行救场",
        # daemon process is alive, only this one request failed
        "STUB_PGREP_FOUND": "1",
    })

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "命令行救场"
    assert len(harness.calls("mlx-qwen3-asr")) == 1, (
        "an ERROR reply must be treated as no answer and hand over to the CLI")


# --- 3 -------------------------------------------------------------------
def test_stale_socket_is_removed_and_daemon_relaunched(harness):
    """Socket file with nobody behind it: unlink it, respawn, use the CLI."""
    sock = harness.stale_socket()
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "兜底文本"})

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "兜底文本"
    launches = harness.daemon_launches()
    assert len(launches) >= 1, (
        "a dead daemon must be relaunched in the background so the next "
        "utterance takes the fast path")
    assert not sock.exists(), (
        "the stale socket file must be deleted, otherwise every later run "
        "pays a failed connect first")


# --- 27 ------------------------------------------------------------------
def test_background_relaunch_log_honours_tmpdir(harness, live_tmp_guard):
    """The background relaunch must log under $TMPDIR, not a fixed /tmp path.

    `>/tmp/zh-voice-daemon.log` is opened with O_TRUNC by whoever runs the
    script, so a second zh-voice - a test run, a second user, a login shell -
    silently wipes the log of the daemon that is currently running, which is
    the only record of why the resident model died. It is also what makes this
    suite non-hermetic: two of the tests above take this branch.
    """
    harness.stale_socket()
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "兜底文本"})

    assert r.returncode == 0, r.stderr
    launches = harness.daemon_launches()
    assert len(launches) >= 1, "precondition: the relaunch branch must be taken"
    log_path = launches[-1].get("logPath")
    assert log_path, (
        "could not read back the relaunch log path from the daemon's fd 1")
    assert log_path.startswith(real(harness.tmpdir) + "/"), (
        "the relaunched daemon's log went to %s instead of somewhere under "
        "$TMPDIR (%s); a hardcoded /tmp path truncates the live daemon's log"
        % (log_path, real(harness.tmpdir)))
    assert live_tmp_guard.poll() == [], (
        "running zh-voice touched the live installation's files in the real "
        "/tmp: %s" % (live_tmp_guard.violations(),))


# --- 4 -------------------------------------------------------------------
def test_failed_socket_read_does_not_abort_script_under_pipefail(harness):
    """REGRESSION: `set -euo pipefail` + a failing socket read killed the
    script before the fallback could run - silent exit 1, empty stdout."""
    harness.stale_socket()
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "兜底仍然有输出"})

    assert r.returncode == 0, (
        "a failed daemon read must never abort the script: rc=%s stderr=%r"
        % (r.returncode, r.stderr))
    assert r.stdout.strip() == "兜底仍然有输出"
    assert len(harness.calls("mlx-qwen3-asr")) == 1, (
        "the CLI fallback must actually be reached after the socket read fails")


# --- 5 -------------------------------------------------------------------
def test_both_paths_empty_exits_nonzero_with_stderr_explanation(harness):
    """Nothing transcribed anywhere -> loud failure, not a silent success."""
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": ""})

    assert r.returncode != 0, "an empty transcript must not exit 0"
    assert r.stdout.strip() == "", (
        "stdout must stay empty rather than emit a blank line that the caller "
        "would paste")
    assert r.stderr.strip() != "", "the failure must be explained on stderr"


# --- 6 -------------------------------------------------------------------
def test_native_16k_mono_wav_skips_ffmpeg(harness):
    """Already 16 kHz mono: no transcode, no extra process spawn."""
    harness.live_daemon(reply="不需要转码")
    wav = harness.wav(seconds=1.0, rate=16000, channels=1)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert harness.calls("ffmpeg") == [], (
        "16 kHz mono input must be used as-is; ffmpeg costs ~25%% of a short "
        "clip's latency")


# --- 7 -------------------------------------------------------------------
def test_offspec_wav_is_resampled_with_ffmpeg(harness):
    """44.1 kHz stereo must be normalised to 16 kHz mono before ASR."""
    harness.live_daemon(reply="转码之后的结果")
    wav = harness.wav(name="stereo.wav", seconds=1.0, rate=44100, channels=2)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    calls = harness.calls("ffmpeg")
    assert len(calls) == 1, "off-spec audio must be resampled, got %r" % (calls,)
    flat = " ".join(calls[0])
    assert "16000" in flat and "-ac 1" in flat, (
        "ffmpeg must be asked for 16 kHz mono, got %r" % (calls[0],))


# --- 8 -------------------------------------------------------------------
def test_audio_shorter_than_min_duration_errors_without_asr_call(harness):
    """A 0.2 s misfire must be rejected up front - no daemon, no CLI."""
    harness.live_daemon(reply="不该被调用")
    wav = harness.wav(name="blip.wav", seconds=0.2)

    r = harness.run([wav])

    assert r.returncode == 1, "too-short audio must exit 1, got %s" % r.returncode
    assert r.stderr.strip() != "", "the user needs to be told why nothing happened"
    assert r.stdout.strip() == ""
    assert harness.calls("mlx-qwen3-asr") == [], "no CLI ASR for a 0.2 s clip"
    assert harness.calls("nc") == [], "no daemon round trip for a 0.2 s clip"


# --- 9 -------------------------------------------------------------------
def test_missing_terms_file_still_succeeds_without_context(harness):
    """No term-bias file is normal, not an error, and passes no --context."""
    harness.remove_terms()
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "没有术语表也能转"})

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "没有术语表也能转"
    calls = harness.calls("mlx-qwen3-asr")
    assert len(calls) == 1
    assert "--context" not in calls[0], (
        "an absent terms file must not turn into an empty --context")


# --- 10 ------------------------------------------------------------------
def test_terms_file_comments_are_stripped_from_context(harness):
    """`#` comments are for humans; only the terms reach the model."""
    harness.write_terms(
        "# 这是整行注释，绝不能进 context\n"
        "\n"
        "LIBERO\nsbatch\n"
        "mLSTM   # 行尾注释也要去掉\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "带术语的转写"})

    assert r.returncode == 0, r.stderr
    calls = harness.calls("mlx-qwen3-asr")
    assert len(calls) == 1
    assert "--context" in calls[0], "terms must be forwarded as --context"
    ctx = calls[0][calls[0].index("--context") + 1]
    for term in ("LIBERO", "sbatch", "mLSTM"):
        assert term in ctx, "term %r missing from context %r" % (term, ctx)
    assert "#" not in ctx and "注释" not in ctx, (
        "comments leaked into the ASR context: %r" % (ctx,))


# --- 55 ------------------------------------------------------------------
def test_context_uses_the_measured_wrapper_not_a_bare_space_joined_list(harness):
    """The wording of the term list has a large effect.

    Measured: 184 transcriptions with the same model
    (mlx-community/Qwen3-ASR-1.7B-8bit). Terms joined by spaces give a mean WER of
    33.8% over 4 clips and a term leak of 1/4. `Technical terms: a, b, c.` gives
    18.2% and a leak of 0/4. On the hardest clip, space-joined gives 87.5%, worse
    than no context (78.1%). So a bare list hurts on hard audio.

    Do not change the prefix. In the same experiment eight other prefixes fell
    back to the bare-list level. Only Technical terms:, Vocabulary: and Proper
    nouns: passed. This test pins the default.
    """
    harness.write_terms("LIBERO\nsbatch\nmLSTM\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "带术语的转写"})

    assert r.returncode == 0, r.stderr
    calls = harness.calls("mlx-qwen3-asr")
    ctx = calls[0][calls[0].index("--context") + 1]

    assert ctx.startswith("Technical terms: "), (
        "context must start with the \"Technical terms: \" prefix; a bare list is worse than no context on hard audio: %r" % (ctx,))
    assert ctx.endswith("."), "context must end with a period: %r" % (ctx,)
    body = ctx[len("Technical terms: "):-1]
    assert body.split(", ") == ["LIBERO", "sbatch", "mLSTM"], (
        "terms must be separated by comma and space; got %r" % (body.split(", "),))


# --- 56 ------------------------------------------------------------------
def test_a_terms_file_with_only_comments_passes_no_context_at_all(harness):
    """Edge case of the prefix: a terms file with only comments must not become an
    empty shell `Technical terms: .`, which gives the model a prior without content."""
    harness.write_terms("# 全是注释\n#\n\n   \n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "没有术语也能转"})

    assert r.returncode == 0, r.stderr
    calls = harness.calls("mlx-qwen3-asr")
    assert len(calls) == 1
    assert "--context" not in calls[0], (
        "a terms file with only comments must behave like no terms file, with no empty prefix sent: %r" % (calls[0],))


# --- 57 ------------------------------------------------------------------
def test_multi_word_terms_are_kept_intact_and_duplicates_are_dropped(harness):
    """Keep each multi-word term intact and retain its first occurrence."""
    harness.write_terms(
        "SLURM\nsbatch\nsqueue\n"
        "Claude Code\nspeculative decoding\n"
        "sbatch\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "转写"})

    assert r.returncode == 0, r.stderr
    calls = harness.calls("mlx-qwen3-asr")
    ctx = calls[0][calls[0].index("--context") + 1]
    terms = ctx[len("Technical terms: "):-1].split(", ")

    assert terms == ["SLURM", "sbatch", "squeue", "Claude Code",
                     "speculative decoding"], (
        "phrases must be kept and duplicates removed; got %r" % (terms,))


def test_terms_use_one_trimmed_line_per_term(harness):
    """Strip comments and blanks; preserve spaces, commas, and first occurrence order."""
    harness.write_terms(
        " \tClaude Code \t# inline comment, ignored term\n"
        "\n \t\n"
        "  # whole-line comment, ignored term\n"
        " alpha,beta \t\n"
        "\tflow  matching\t\n"
        "Claude Code\n"
        "alpha,beta# duplicate\n"
        "mLSTM\n"
        " flow  matching # duplicate\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "转写"})

    assert r.returncode == 0, r.stderr
    calls = harness.calls("mlx-qwen3-asr")
    assert len(calls) == 1, calls
    ctx = calls[0][calls[0].index("--context") + 1]
    assert ctx == "Technical terms: Claude Code, alpha,beta, flow  matching, mLSTM."


# --- 11 ------------------------------------------------------------------
def test_traditional_output_is_converted_to_simplified(harness):
    """ASR sometimes drifts Traditional; the pasted text must be Simplified."""
    harness.live_daemon(reply="測試轉寫結果")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "测试转写结果", (
        "Traditional characters reached stdout: %r" % (r.stdout,))


# --- 12 ------------------------------------------------------------------
def test_output_is_a_single_trimmed_line(harness):
    """The caller pastes stdout verbatim: exactly one line, no stray space."""
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={
        "STUB_ASR_STDOUT": "Fetching 5 files: 100%\\n   你好  世界   ",
    })

    assert r.returncode == 0, r.stderr
    lines = r.stdout.split("\n")
    assert lines[-1] == "", "output must end with exactly one newline: %r" % r.stdout
    body = lines[:-1]
    assert len(body) == 1, "expected one line of output, got %r" % (body,)
    assert body[0] == body[0].strip() != "", (
        "output must be trimmed and non-empty: %r" % (body[0],))
    assert "你好" in body[0] and "世界" in body[0]
    assert "Fetching" not in body[0], "progress noise must not reach stdout"


# --- 38 ------------------------------------------------------------------
def test_health_says_healthy_when_the_daemon_answers_ping(harness):
    """`zh-voice --health` is the go-live gate of the installer and the manual self-check."""
    harness.live_daemon(
        reply=lambda req: ("PONG stub/model" if req.startswith("PING")
                           else "不该走到这里"))

    r = harness.run(["--health"])

    assert r.returncode == 0, r.stderr
    assert "healthy" in r.stdout
    assert "stub/model" in r.stdout, (
        "the model name from PONG must be echoed, so a live daemon can be told apart "
        "from a live daemon running the installed model; got %r" % (r.stdout,))
    assert harness.calls("mlx-qwen3-asr") == [], (
        "a health check must not run a real transcription")


# --- 48 ------------------------------------------------------------------
def test_doctor_reports_each_dependency_and_fails_on_a_missing_model(harness):
    """When something fails, run the health check first instead of guessing item by item.

    A missing model cache must be FAIL, not warn: the daemon runs in offline mode
    and never starts without the cache, and the only symptom is "the hotkey does
    nothing".
    """
    r = harness.run(["--doctor"])

    assert "soxi" in r.stdout and "nc" in r.stdout, (
        "doctor must list each dependency with its actual path: %r" % (r.stdout,))
    assert r.returncode != 0, (
        "doctor must exit non-zero when there is no model cache and no daemon")
    assert "FAIL" in r.stdout and "model" in r.stdout, (
        "a missing model cache must be reported as FAIL: %r" % (r.stdout,))
    assert "zh-voice-audio" in r.stdout and "vad model" in r.stdout, (
        "doctor must check VAD too: it fails open silently, so a broken VAD must be "
        "visible here: %r" % (r.stdout,))
    assert harness.calls("mlx-qwen3-asr") == [], "doctor must not run a transcription"


# --- 39 ------------------------------------------------------------------
def test_health_fails_when_no_daemon_is_running(harness):
    """No socket means no daemon is running. Exit non-zero. Otherwise the installer
    would treat "never started" as a successful go-live."""
    r = harness.run(["--health"])

    assert r.returncode == 1, (
        "no daemon must exit with failure; got rc=%s stdout=%r" % (r.returncode, r.stdout))
    assert "unhealthy" in r.stderr, r.stderr
    assert harness.calls("mlx-qwen3-asr") == [], "a health check must not fall back to the one-shot CLI"
    assert harness.daemon_launches(wait=0.5) == [], (
        "--health is a read-only probe and must not start the daemon, or the check "
        "would change what it checks")


# --- 40 ------------------------------------------------------------------
def test_health_fails_on_a_socket_with_nobody_behind_it(harness):
    """This is how "process alive, service dead" looks from the client: the socket file exists, but connecting fails or nothing answers.

    A check that only tests `[ -S socket ]` would report healthy here.
    """
    sock = harness.stale_socket()
    assert sock.is_socket()

    r = harness.run(["--health"])

    assert r.returncode == 1, (
        "a socket file with no listener must be unhealthy; got rc=%s stdout=%r"
        % (r.returncode, r.stdout))
    assert "unhealthy" in r.stderr, r.stderr


# --- 49 ------------------------------------------------------------------
def test_local_vad_rejects_silence_before_any_asr_path(harness):
    """Silence must never reach the recognizer that has a term list.

    Measured: with noise, fan sound or white noise and the production 506-character
    context, the model outputs the WHOLE term list as the transcript, and the caller
    pastes it into the active app. So the gate must sit before both ASR paths and
    not filter afterwards.

    The contract is "exit code 0, no output, empty stderr". The caller treats this
    combination as "not heard" and pastes nothing.
    """
    harness.live_daemon(reply="Technical terms: LIBERO, sbatch, mLSTM.")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\n")
    wav = harness.wav(name="silence.wav", seconds=1.5)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "silence"})

    assert r.returncode == 0, r.stderr
    assert r.stdout == "", "silence must produce no stdout"
    assert r.stderr == "", "silence must produce no stderr, since stderr output means a transcription failure"
    assert len(harness.calls("zh-voice-audio")) == 1
    assert harness.calls("nc") == [], "silence must not use the daemon"
    assert harness.calls("mlx-qwen3-asr") == [], "silence must not trigger the one-shot CLI"


# --- 50 ------------------------------------------------------------------
def test_local_vad_lets_speech_through(harness):
    harness.live_daemon(reply="正常语音")
    wav = harness.wav(name="speech.wav", seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "speech"})

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "正常语音"
    assert len(harness.calls("zh-voice-audio")) == 1


# --- 51 ------------------------------------------------------------------
def test_vad_runtime_error_fails_open(harness):
    """If the detector itself fails, transcription must continue.

    The opposite (fail-closed) would let one failed probe swallow a real
    utterance, and the user would only see "not heard". Silent loss of speech is
    much harder to diagnose than an occasional noise that passes.
    """
    harness.live_daemon(reply="检测器坏了也不能丢语音")
    wav = harness.wav(name="speech.wav", seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "error"})

    assert r.returncode == 0
    assert r.stdout.strip() == "检测器坏了也不能丢语音"
    assert "VAD 检查失败" in r.stderr


# --- 52 ------------------------------------------------------------------
def test_missing_vad_model_fails_open_without_tripping_set_e(harness):
    """Dictation must still work without the model cache. Under `set -euo pipefail` this path easily turns into a silent exit."""
    harness.live_daemon(reply="缓存没了也不能丢语音")
    wav = harness.wav(name="speech.wav", seconds=1.0)

    r = harness.run([wav], extra_env={
        "ZH_VOICE_VAD_MODEL_PATH": harness.tmp / "missing.mlmodelc"})

    assert r.returncode == 0
    assert r.stdout.strip() == "缓存没了也不能丢语音"
    assert "VAD 不可用" in r.stderr
    assert harness.calls("zh-voice-audio") == []


# --- 53 ------------------------------------------------------------------
def test_term_list_echo_is_swallowed_not_pasted(harness):
    """Second net after the VAD: the gate fails open, so the leak path is not fully closed.

    When the model recites the context, the output is a prefix of the term list
    (cut by the max-token limit), often with a leading label such as "技术术语：" and
    full-width punctuation. All three forms must be recognized.
    """
    harness.live_daemon(
        reply="技术术语：LIBERO，sbatch，mLSTM，checkpoint")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "speech"})

    assert r.returncode == 0
    assert r.stdout == "", "a reply that only recites the glossary is not speech and must not be output: %r" % (r.stdout,)
    assert r.stderr == ""


# --- 54 ------------------------------------------------------------------
def test_real_speech_that_merely_mentions_a_term_is_kept(harness):
    """The net above must not be so wide that it swallows normal speech: the speaker uses these words all day."""
    harness.live_daemon(reply="LIBERO 那个 checkpoint 我等下再看")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "speech"})

    assert r.returncode == 0
    assert r.stdout.strip() == "LIBERO 那个 checkpoint 我等下再看"


# --- 65 --- private temp files / trimming / metrics / echo retry ------------
def test_vad_is_asked_for_json_and_trim(harness):
    """Trimming leading and trailing silence is not optional. Long pauses cause ASR
    hallucination, and shorter audio transcribes faster. So the gate pass also
    passes --trim."""
    harness.live_daemon(reply="裁过之后的正常语音")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    call = harness.calls("zh-voice-audio")[0]
    assert "--json" in call, "--json is required to get metrics"
    assert "--trim" in call, "--trim is required to trim silence"


def test_vad_trim_can_be_turned_off(harness):
    harness.live_daemon(reply="没裁的正常语音")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_VAD_TRIM": "0"})

    assert r.returncode == 0, r.stderr
    assert "--trim" not in harness.calls("zh-voice-audio")[0]


def test_metrics_record_numbers_but_never_the_transcript(harness, tmp_path):
    """Metrics tell apart which stage swallowed an utterance. They must NEVER
    contain the transcript text or the audio path."""
    secret = "这句话绝对不能出现在指标里"
    harness.live_daemon(reply=secret)
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0, r.stderr
    line = metrics.read_text()
    assert secret not in line, "metrics must not contain transcript text: %r" % (line,)
    assert ".wav" not in line, "metrics must not contain the audio path: %r" % (line,)
    for key in ("ok=1", "path=daemon", "asr_ms=", "vad_p=0.97", "audio_ms=1500",
                "trimmed_ms=980", "retry=0"):
        assert key in line, "metrics missing %s: %r" % (key, line)


def test_metrics_distinguish_a_vad_gated_utterance(harness, tmp_path):
    harness.live_daemon(reply="不该被叫到")
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "silence",
                                      "ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0 and r.stdout == ""
    line = metrics.read_text()
    assert "gated=1" in line, line
    assert "vad_p=0.013" in line, "a gated clip must still record its probability"


def test_reordered_term_echo_is_retried_without_context_and_recovered(harness):
    """Criterion 2: the text is not a prefix but mostly consists of terms from the list. The model recites them in another order.

    Do not drop it at once. Transcribe once more WITHOUT context: the gate fails
    open, so real speech may exist and the term bias may have covered it.
    """
    def by_context(request):
        # The protocol has two lines: wav path and context. With the bias the stub recites the term list. Without it the stub returns plain speech.
        lines = request.split("\n")
        context = lines[1] if len(lines) > 1 else ""
        if context.strip():
            return "mLSTM，checkpoint，sbatch，benchmark"
        return "其实我说的是这句"

    harness.live_daemon(reply=by_context)
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "其实我说的是这句", \
        "a clean retry result must be used instead of dropping the echo: %r" % (r.stdout,)


def test_term_echo_still_dropped_when_retry_is_also_an_echo(harness):
    """If the retry also recites the term list, there is no speech. Follow the same contract as silence."""
    harness.live_daemon(reply="mLSTM，checkpoint，sbatch，benchmark")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0
    assert r.stdout == "", "when both attempts are echo, nothing must be output: %r" % (r.stdout,)
    assert r.stderr == ""


def test_real_speech_mentioning_three_terms_is_not_mistaken_for_an_echo(harness):
    """The coverage criterion must stay tight: the speaker uses these words all day."""
    harness.live_daemon(reply="LIBERO 的 checkpoint 和 benchmark 我等下一起看")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "LIBERO 的 checkpoint 和 benchmark 我等下一起看"


def test_speech_with_full_term_list_appended_is_retried_and_recovered(harness):
    """Criterion 3: real speech followed by the whole term list.

    Criterion 1 misses it (not a prefix), and criterion 2 misses it (the real
    speech dilutes coverage below 75%). Example: 10.8 s of speech produced 582
    characters, and the whole text was pasted into the focused app."""
    def by_context(request):
        lines = request.split("\n")
        context = lines[1] if len(lines) > 1 else ""
        if context.strip():
            return ("我们今天先把这个实验跑起来，然后看结果。"
                    "技术术语:LIBERO-Pro，RoboCasa，MIKASA，checkpoint，"
                    "benchmark，speculative decoding，embodied intelligence")
        return "我们今天先把这个实验跑起来，然后看结果。"

    harness.live_daemon(reply=by_context)
    harness.write_terms("LIBERO-Pro\nRoboCasa\nMIKASA\ncheckpoint\nbenchmark\n"
                        "speculative decoding\nembodied intelligence\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "我们今天先把这个实验跑起来，然后看结果。", \
        "a trailing echo must trigger a retry that recovers the real speech: %r" % (r.stdout,)


def test_real_speech_ending_with_a_short_term_list_is_not_an_echo(harness):
    """Criterion 3 must stay tight: naming three terms in a row at the end of a sentence (under 60 characters) is normal speech."""
    said = "我一会儿把下面三个环境统统跑一遍再汇报，LIBERO-Pro，RoboCasa，MIKASA"
    harness.live_daemon(reply=said)
    harness.write_terms("LIBERO-Pro\nRoboCasa\nMIKASA\ncheckpoint\nbenchmark\n"
                        "speculative decoding\nembodied intelligence\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == said


SPACE_ECHO_TERMS = (
    "Claude Code\nspeculative decoding\nembodied intelligence\n"
    "checkpoint\nbenchmark\n"
)
SPACE_ECHO_WORDS = (
    "Claude", "Code", "speculative", "decoding", "embodied",
    "intelligence", "checkpoint", "benchmark",
)


def record_space_echo_contexts(harness, first_output, retry_output):
    """Record ASR contexts and return the transcript for each context state."""
    contexts = []

    def by_context(request):
        lines = request.split("\n")
        context = lines[1] if len(lines) > 1 else ""
        contexts.append(context)
        return first_output if context.strip() else retry_output

    harness.live_daemon(reply=by_context)
    return contexts


@pytest.mark.parametrize("separator", [" ", "   "], ids=["single", "multiple"])
@pytest.mark.parametrize("position", ["suffix", "prefix"])
def test_space_echo_edge_run_retries_without_context_and_recovers(
        harness, separator, position):
    """A long term run at either speech edge requires one unbiased retry."""
    clean = "我们今天先检查实验结果，然后决定下一步。"
    echo = separator.join(SPACE_ECHO_WORDS)
    assert len("".join(SPACE_ECHO_WORDS)) >= 60
    first = clean + separator + echo if position == "suffix" else echo + separator + clean
    contexts = record_space_echo_contexts(harness, first, clean)
    harness.write_terms(SPACE_ECHO_TERMS)
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout == clean + "\n", (
        "expected clean retry, got stdout=%r contexts=%r" % (r.stdout, contexts))
    assert len(contexts) == 2, contexts
    assert contexts[0].strip(), contexts
    assert not contexts[1].strip(), contexts
    assert harness.calls("mlx-qwen3-asr") == []


@pytest.mark.parametrize("position", ["suffix", "prefix"])
def test_space_echo_retry_echo_is_dropped(harness, position):
    """A second long space-separated echo must produce no pasted text."""
    speech = "我们今天先检查实验结果。"
    echo = "  ".join(SPACE_ECHO_WORDS)
    first = speech + " " + echo if position == "suffix" else echo + " " + speech
    retry = echo + " " + speech if position == "suffix" else speech + " " + echo
    contexts = record_space_echo_contexts(harness, first, retry)
    harness.write_terms(SPACE_ECHO_TERMS)
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout == "", (
        "retry echo reached stdout=%r contexts=%r" % (r.stdout, contexts))
    assert len(contexts) == 2, contexts
    assert contexts[0].strip(), contexts
    assert not contexts[1].strip(), contexts
    assert harness.calls("mlx-qwen3-asr") == []


@pytest.mark.parametrize("separator", [" ", "   "], ids=["single", "multiple"])
def test_space_echo_pure_term_dump_retries_and_recovers(harness, separator):
    """A pure term dump requires a retry even without accompanying speech."""
    clean = "这是重新识别得到的正常语音。"
    contexts = record_space_echo_contexts(
        harness, separator.join(SPACE_ECHO_WORDS), clean)
    harness.write_terms(SPACE_ECHO_TERMS)
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout == clean + "\n", (
        "term dump reached stdout=%r contexts=%r" % (r.stdout, contexts))
    assert len(contexts) == 2, contexts
    assert contexts[0].strip(), contexts
    assert not contexts[1].strip(), contexts
    assert harness.calls("mlx-qwen3-asr") == []


def test_space_echo_three_items_at_sixty_characters_retries(harness):
    """Three consecutive items totalling exactly 60 characters meet the limit."""
    words = ("abcdefghijklmnopqrst", "uvwxyzabcdefghijklmn", "opqrstuvwxyzabcdefgh")
    assert len(words) == 3 and len("".join(words)) == 60
    clean = "这个实验今天先暂停，等明天确认配置后再继续。"
    contexts = record_space_echo_contexts(harness, clean + " " + " ".join(words), clean)
    harness.write_terms("\n".join(words) + "\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout == clean + "\n", (
        "60-character echo reached stdout=%r contexts=%r" % (r.stdout, contexts))
    assert len(contexts) == 2, contexts
    assert contexts[0].strip(), contexts
    assert not contexts[1].strip(), contexts
    assert harness.calls("mlx-qwen3-asr") == []


@pytest.mark.parametrize("said", [
    "我先用 sbatch 提交作业，再看 MIKASA 的结果，最后跑 LIBERO 做确认。",
    "我一会儿把下面三个环境统统跑一遍再汇报 sbatch   MIKASA LIBERO",
], ids=["scattered", "short-suffix"])
def test_space_echo_real_speech_is_kept_without_retry(harness, said):
    """Scattered term words and a short trailing run remain valid speech."""
    assert len("sbatchMIKASALIBERO") < 60
    contexts = record_space_echo_contexts(harness, said, "不该使用的重试结果")
    harness.write_terms("sbatch\nMIKASA\nLIBERO\n" + SPACE_ECHO_TERMS)
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout == said + "\n", (
        "valid speech changed: stdout=%r contexts=%r" % (r.stdout, contexts))
    assert len(contexts) == 1, contexts
    assert contexts[0].strip(), contexts
    assert harness.calls("mlx-qwen3-asr") == []


@pytest.mark.parametrize("loop", [
    "甲乙丙丁戊己庚辛壬癸" * 3,
    "甲乙丙丁" * 8,
    "甲乙丙丁戊己庚辛壬，甲 乙丙丁戊己庚辛壬、甲乙丙丁戊己庚辛壬,",
    "先检查今天的模型配置。" + (
        "DeepStack，用的那个 AdaLN-Zero，cross-attention，flow，matching，" * 15),
], ids=["exactly-30-characters", "minimum-unit", "normalized", "production-suffix"])
def test_decoding_loop_retries_without_context_and_recovers(harness, tmp_path, loop):
    """A repeated stretch requires one retry with term context removed."""
    clean = "我们今天先检查模型配置，再决定下一步。"
    contexts = record_space_echo_contexts(harness, loop, clean)
    # Unrelated terms isolate loop detection from term echo detection.
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0, r.stderr
    assert r.stdout == clean + "\n", (r.stdout, contexts)
    assert r.stderr == ""
    assert len(contexts) == 2, contexts
    assert contexts[0].strip(), contexts
    assert contexts[1] == "", contexts
    assert harness.calls("mlx-qwen3-asr") == []
    lines = metrics.read_text().splitlines()
    assert len(lines) == 1, lines
    assert "retry=1" in lines[0].split(), lines


@pytest.mark.parametrize("retry", [
    "子丑寅卯辰巳午未申酉" * 3,
    "mLSTM，checkpoint，sbatch，benchmark",
], ids=["loop", "term-echo"])
def test_decoding_loop_bad_retry_is_dropped(harness, tmp_path, retry):
    """A loop or term echo on the sole retry must not reach stdout."""
    contexts = record_space_echo_contexts(
        harness, "甲乙丙丁戊己庚辛壬癸" * 3, retry)
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0, r.stderr
    assert r.stdout == "", (r.stdout, contexts)
    assert r.stderr == ""
    assert len(contexts) == 2, contexts
    assert contexts[0].strip(), contexts
    assert contexts[1] == "", contexts
    assert harness.calls("mlx-qwen3-asr") == []
    lines = metrics.read_text().splitlines()
    assert len(lines) == 1, lines
    assert "retry=1" in lines[0].split(), lines
    assert "echo_dropped=1" in lines[0].split(), lines


@pytest.mark.parametrize("said", [
    "只有两百个，只有两百个，只有两百个人可以抢",
    "对对对对对对，你说得对",
    "今天用 DeepStack 和 AdaLN-Zero 检查 cross-attention，再看 flow matching 的结果。",
    "甲乙丙丁" * 7,
    "甲乙丙丁戊己庚辛壬" * 3,
    "甲乙丙丁戊己庚" * 4 + "甲",
    "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳" * 2,
    "甲乙丙丁戊己庚辛壬癸，中间换一句话，甲乙丙丁戊己庚辛壬癸，最后再说，甲乙丙丁戊己庚辛壬癸",
], ids=["human-phrase", "short-unit", "mixed-zh-en", "28-characters",
        "27-chinese-characters", "29-with-partial-unit", "exactly-two", "not-consecutive"])
def test_decoding_loop_valid_speech_is_kept_without_retry(harness, tmp_path, said):
    """Short, interrupted, or twice-repeated stretches remain valid speech."""
    contexts = record_space_echo_contexts(harness, said, "不该使用的重试结果")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0, r.stderr
    assert r.stdout == said + "\n", (r.stdout, contexts)
    assert r.stderr == ""
    assert len(contexts) == 1, contexts
    assert contexts[0].strip(), contexts
    assert harness.calls("mlx-qwen3-asr") == []
    lines = metrics.read_text().splitlines()
    assert len(lines) == 1, lines
    assert "retry=0" in lines[0].split(), lines
    assert "echo_dropped=1" not in lines[0].split(), lines


def test_decoding_loop_cli_retry_is_dropped_without_context(harness, tmp_path):
    """The CLI path also retries once, then suppresses a persistent loop."""
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={
        "STUB_ASR_STDOUT": "甲乙丙丁戊己庚辛壬癸" * 3,
        "ZH_VOICE_METRICS": str(metrics),
    })

    assert r.returncode == 0, r.stderr
    assert r.stdout == "", r.stdout
    assert r.stderr == ""
    calls = harness.calls("mlx-qwen3-asr")
    assert len(calls) == 2, calls
    assert "--context" in calls[0], calls
    assert calls[0][calls[0].index("--context") + 1].strip(), calls
    assert "--context" not in calls[1], calls
    lines = metrics.read_text().splitlines()
    assert len(lines) == 1, lines
    assert "retry=1" in lines[0].split(), lines
    assert "echo_dropped=1" in lines[0].split(), lines


def test_cli_fallback_loads_the_pinned_snapshot_by_path(harness):
    """`--model <repo id>` makes huggingface_hub read refs/main, which does not pin the
    version. If the pinned snapshot is in the cache, pass ITS PATH to the CLI (the
    load_model parameter is path_or_hf_repo)."""
    rev = "d" * 40
    snap = (harness.home / ".cache" / "huggingface" / "hub"
            / "models--stub--model" / "snapshots" / rev)
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_ASR_REVISION": rev,
                                      "STUB_ASR_STDOUT": "钉住版本的转写"})

    assert r.returncode == 0, r.stderr
    calls = harness.calls("mlx-qwen3-asr")
    assert len(calls) == 1, calls
    got = calls[0][calls[0].index("--model") + 1]
    assert got == str(snap), "--model must be the pinned snapshot path; got %r" % (got,)


def test_cli_fallback_still_transcribes_when_the_pin_is_missing(harness):
    """If the pinned snapshot is missing, transcribe with an unpinned version rather
    than not at all: a revision problem must not stop dictation. --doctor reports
    the drift."""
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "没钉住也照样出字"})

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "没钉住也照样出字"
    calls = harness.calls("mlx-qwen3-asr")
    assert calls[0][calls[0].index("--model") + 1] == "stub/model", (
        "without the pinned snapshot, --model must fall back to the repo id: %r" % (calls[0],))


def test_doctor_reports_the_tokenizer_as_pinned_not_as_a_missing_upstream_cache(harness):
    """With the pin, the cache of the upstream repo (1.7 GB locally, 99% of it 0.6B weights that are never loaded) can be deleted.

    If the health check still reported FAIL for a missing upstream cache, it would
    be a false alarm, and false alarms teach people to ignore the check."""
    rev = "a" * 40
    snap = (harness.home / ".cache" / "huggingface" / "hub"
            / "models--stub--model" / "snapshots" / rev)
    snap.mkdir(parents=True)
    for name in ("config.json", "vocab.json", "merges.txt", "tokenizer_config.json"):
        (snap / name).write_text("{}", encoding="utf-8")

    r = harness.run(["--doctor"], extra_env={"ZH_VOICE_ASR_REVISION": rev})

    line = [ln for ln in r.stdout.splitlines() if "tokenizer" in ln]
    assert line, "doctor output must have a tokenizer line: %r" % (r.stdout,)
    assert line[0].startswith("ok"), (
        "a pinnable tokenizer must be reported ok without an upstream cache: %r" % (line[0],))


# ── Process spawns saved on the hot path ─────────────────────────────────────
def test_from_recorder_trusts_the_caller_and_skips_the_soxi_probes(harness):
    """`--from-recorder` means the caller guarantees that the wav comes from our recorder (16 kHz mono).

    The three soxi calls (-r and -c probe the format, -D reads the duration) ask
    for facts that are already known, and each costs 6.4 ms. The caller also
    enforces the minimum duration, so it is not checked again here.
    """
    harness.live_daemon(reply="不必再问格式")
    wav = harness.wav(seconds=1.0, rate=16000, channels=1)

    r = harness.run(["--from-recorder", wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "不必再问格式"
    assert harness.calls("soxi") == [], (
        "--from-recorder must skip the soxi format probe")
    assert harness.calls("ffmpeg") == [], "must not transcode"


def test_without_the_flag_the_format_is_still_probed(harness):
    """The standalone entry (any wav from the user) has no such guarantee, so the format probe must stay.

    Reverse assertion: saving spawns must not turn this path into "assume 16 kHz
    mono". A 44.1 kHz stereo file would reach the ASR as 16 kHz and produce
    fast-forward nonsense.
    """
    harness.live_daemon(reply="探过格式")
    wav = harness.wav(seconds=1.0, rate=16000, channels=1)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert harness.calls("soxi") != [], "without the flag the format must still be probed"


def test_server_reported_asr_ms_is_what_lands_in_metrics(harness, tmp_path):
    """The daemon reports the transcription time itself (META in the first reply line). The client does not time it."""
    harness.live_daemon(reply="META asr_ms=137\n服务端计时的转写")
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "服务端计时的转写", "the META line must not be treated as transcript"
    assert "asr_ms=137" in metrics.read_text(), (
        "server ASR time must reach the metrics: %r" % (metrics.read_text(),))


def test_a_reply_without_meta_still_yields_the_transcript(harness, tmp_path):
    """A reply without META (PING, ERROR, or another daemon) is the transcript as a whole. The behavior is unchanged."""
    harness.live_daemon(reply="没有头信息的回复")
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "没有头信息的回复"
    assert "asr_ms=?" in metrics.read_text(), (
        "missing server ASR time must be recorded as ?, never invented: %r" % (metrics.read_text(),))


def test_a_single_line_reply_that_looks_like_meta_is_not_eaten(harness):
    """The META check requires a real newline after it. Otherwise a transcript that
    starts with "META asr_ms=" would be dropped as a header, and the utterance
    would vanish."""
    harness.live_daemon(reply="META asr_ms=999")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "META asr_ms=999", (
        "a single-line reply must be output unchanged as the transcript; got %r" % (r.stdout,))


def test_terms_cache_is_read_back_and_an_edit_invalidates_it(harness):
    """The term string and its normalized form change only with terms.txt. The cache saves ~15 ms per turn.

    Freshness depends on the key in the first cache line (mtime and size of
    terms.txt). The test first replaces the cache content with a sentinel and
    KEEPS the key, which proves that the cache is read. Then it edits terms.txt,
    which proves that a changed key forces a recompute. With only the second half,
    an implementation that never reads the cache would pass.
    """
    harness.write_terms("LIBERO\nsbatch\n")
    wav = harness.wav(seconds=1.0)
    cache = harness.home / ".cache" / "zh-voice" / "terms.cache"

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "第一次"})
    assert r.returncode == 0, r.stderr
    assert cache.exists(), "first run must write the cache"

    key = cache.read_text(encoding="utf-8").splitlines()[0]
    cache.write_text("%s\n哨兵术语\n哨兵术语\n" % key, encoding="utf-8")
    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "第二次"})
    ctx = harness.calls("mlx-qwen3-asr")[-1]
    ctx = ctx[ctx.index("--context") + 1]
    assert "哨兵术语" in ctx, "the cache must be read: context must hold the sentinel; got %r" % (ctx,)

    harness.write_terms("mLSTM\nGR00T\n")       # the edit makes terms.txt newer
    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "第三次"})
    ctx = harness.calls("mlx-qwen3-asr")[-1]
    ctx = ctx[ctx.index("--context") + 1]
    assert "mLSTM" in ctx and "哨兵术语" not in ctx, (
        "a changed terms.txt must invalidate the cache: %r" % (ctx,))


def assert_corpus_record(archive, audio, transcript, context):
    """One successful transcription produces one complete archive record."""
    wavs = list(archive.glob("*.wav"))
    assert len(wavs) == 1, list(archive.iterdir())
    record_id = wavs[0].stem
    assert {p.name for p in archive.iterdir()} == {
        record_id + ".wav", record_id + ".txt", record_id + ".ctx", "index.tsv"}
    assert wavs[0].read_bytes() == audio
    assert transcript.endswith("\n") and len(transcript.splitlines()) == 1
    assert (archive / (record_id + ".txt")).read_text(encoding="utf-8") == transcript
    assert (archive / (record_id + ".ctx")).read_text(encoding="utf-8") == context + "\n"
    rows = (archive / "index.tsv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1, rows
    assert rows[0].startswith(record_id + "\t"), rows


@pytest.mark.parametrize("backend", ["daemon", "cli"])
def test_corpus_absent_flag_writes_nothing(harness, backend):
    """A successful transcription must not archive without the flag file."""
    flag = harness.tmp / "corpus.enabled"
    archive = harness.tmp / "corpus"
    if backend == "daemon":
        harness.live_daemon(reply="正常转写结果")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={
        "ZH_VOICE_CORPUS_FLAG": flag,
        "ZH_VOICE_CORPUS_DIR": archive,
        "STUB_ASR_STDOUT": "正常转写结果",
    })

    assert r.returncode == 0, r.stderr
    assert r.stdout == "正常转写结果\n"
    assert not flag.exists()
    assert not archive.exists() or list(archive.iterdir()) == []


@pytest.mark.parametrize("with_terms", [False, True], ids=["no-terms", "terms"])
@pytest.mark.parametrize("trim", [False, True], ids=["vad-disabled", "vad-trimmed"])
def test_corpus_daemon_archives_exact_asr_audio_text_and_context(harness, with_terms, trim):
    """Archive the bytes and context received by ASR, plus the printed text."""
    from conftest import STUB_BIN

    flag = harness.tmp / "corpus.enabled"
    flag.touch()
    archive = harness.tmp / "corpus"
    wav = harness.wav(seconds=1.0)
    original = wav.read_bytes()
    if with_terms:
        harness.write_terms("LIBERO\nsbatch\nmLSTM\n")
    else:
        harness.remove_terms()
    received = []

    def transcribe(request):
        audio_path, context = request.split("\n")[:2]
        received.append((pathlib.Path(audio_path).read_bytes(), context))
        return "測試轉寫結果"

    harness.live_daemon(reply=transcribe)
    env = {
        "ZH_VOICE_CORPUS_FLAG": flag,
        "ZH_VOICE_CORPUS_DIR": archive,
        "ZH_VOICE_VAD_ENABLED": "1" if trim else "0",
    }
    if trim:
        trimmed = harness.wav(name="trimmed.wav", seconds=0.5)
        detector = harness.tmp / "trim-vad"
        detector.write_text(
            "#!/usr/bin/env python3\n"
            "import runpy, shutil, sys\n"
            "assert '--trim' in sys.argv\n"
            "shutil.copyfile(%r, sys.argv[-2])\n"
            "runpy.run_path(%r, run_name='__main__')\n"
            % (str(trimmed), str(STUB_BIN / "zh-voice-audio")), encoding="utf-8")
        detector.chmod(0o755)
        env["ZH_VOICE_VAD_BIN"] = detector

    r = harness.run([wav], extra_env=env)

    assert r.returncode == 0, r.stderr
    assert r.stdout == "测试转写结果\n"
    assert len(received) == 1, received
    audio, context = received[0]
    if trim:
        assert audio == trimmed.read_bytes()
        assert audio != original, "the VAD stub must actually change the ASR input"
    else:
        assert audio == original
        assert harness.calls("zh-voice-audio") == []
    assert bool(context) == with_terms, context
    assert_corpus_record(archive, audio, r.stdout, context)


@pytest.mark.parametrize("with_terms", [False, True], ids=["no-terms", "terms"])
def test_corpus_cli_archives_input_text_and_context(harness, with_terms):
    """The CLI fallback must produce the same complete archive record."""
    flag = harness.tmp / "corpus.enabled"
    flag.touch()
    archive = harness.tmp / "corpus"
    wav = harness.wav(seconds=1.0)
    original = wav.read_bytes()
    if with_terms:
        harness.write_terms("LIBERO\nsbatch\nmLSTM\n")
    else:
        harness.remove_terms()

    r = harness.run([wav], extra_env={
        "ZH_VOICE_CORPUS_FLAG": flag,
        "ZH_VOICE_CORPUS_DIR": archive,
        "ZH_VOICE_VAD_ENABLED": "0",
        "STUB_ASR_STDOUT": "測試轉寫結果",
    })

    assert r.returncode == 0, r.stderr
    assert r.stdout == "测试转写结果\n"
    calls = harness.calls("mlx-qwen3-asr")
    assert len(calls) == 1, calls
    if with_terms:
        assert "--context" in calls[0], calls
        context = calls[0][calls[0].index("--context") + 1]
        assert context
    else:
        assert "--context" not in calls[0], calls
        context = ""
    assert_corpus_record(archive, original, r.stdout, context)


@pytest.mark.parametrize("backend", ["daemon", "cli"])
def test_corpus_flag_does_not_change_stdout(harness, backend):
    """Opting into archiving must leave the printed transcript unchanged."""
    flag = harness.tmp / "corpus.enabled"
    archive = harness.tmp / "corpus"
    wav = harness.wav(seconds=1.0)
    if backend == "daemon":
        harness.live_daemon(reply="測試轉寫結果")
    env = {
        "ZH_VOICE_CORPUS_FLAG": flag,
        "ZH_VOICE_CORPUS_DIR": archive,
        "STUB_ASR_STDOUT": "測試轉寫結果",
    }

    without_flag = harness.run([wav], extra_env=env)
    flag.touch()
    with_flag = harness.run([wav], extra_env=env)

    assert without_flag.returncode == 0, without_flag.stderr
    assert with_flag.returncode == 0, with_flag.stderr
    assert without_flag.stdout == with_flag.stdout == "测试转写结果\n"


@pytest.mark.parametrize("backend", ["daemon", "cli"])
def test_corpus_unwritable_directory_does_not_fail_transcription(harness, backend):
    """Archive errors must not prevent a successful transcript from printing."""
    flag = harness.tmp / "corpus.enabled"
    flag.touch()
    archive = harness.tmp / "corpus"
    archive.write_text("regular file blocks directory creation", encoding="utf-8")
    if backend == "daemon":
        harness.live_daemon(reply="归档失败也要输出")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={
        "ZH_VOICE_CORPUS_FLAG": flag,
        "ZH_VOICE_CORPUS_DIR": archive,
        "STUB_ASR_STDOUT": "归档失败也要输出",
    })

    assert r.returncode == 0, r.stderr
    assert r.stdout == "归档失败也要输出\n"
    assert archive.read_text(encoding="utf-8") == "regular file blocks directory creation"
