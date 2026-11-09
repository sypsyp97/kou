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
    """术语表的**措辞**本身影响很大，不是风格问题。

    一组 184 次转写、跑在同一个模型（mlx-community/Qwen3-ASR-1.7B-8bit）上的实测：空格
    拼成一串是 4 段音频均值 WER 33.8%、术语泄漏 1/4，而 `Technical terms: a, b, c.` 是
    18.2%、泄漏 0/4。最难那段里空格拼串 87.5%，比**完全不给 context**（78.1%）还差——
    也就是说裸列表在难音频上是负收益。

    前缀也不能随便换：同组实验里八个替代前缀全部退回裸列表水平，只有 Technical terms:／
    Vocabulary:／Proper nouns: 三个过关。所以这里钉住默认值。
    """
    harness.write_terms("LIBERO\nsbatch\nmLSTM\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "带术语的转写"})

    assert r.returncode == 0, r.stderr
    calls = harness.calls("mlx-qwen3-asr")
    ctx = calls[0][calls[0].index("--context") + 1]

    assert ctx.startswith("Technical terms: "), (
        "context 没有用实测有效的前缀，裸列表在难音频上比不给 context 还差：%r" % (ctx,))
    assert ctx.endswith("."), "实测的那一版格式是以句号结尾的：%r" % (ctx,)
    body = ctx[len("Technical terms: "):-1]
    assert body.split(", ") == ["LIBERO", "sbatch", "mLSTM"], (
        "术语要用逗号加空格分隔（照实测那一版），实际拆出来是 %r" % (body.split(", "),))


# --- 56 ------------------------------------------------------------------
def test_a_terms_file_with_only_comments_passes_no_context_at_all(harness):
    """加了前缀之后新出现的边界：只剩注释的术语表不能变成一个空壳
    `Technical terms: .` —— 那是给模型一个没有内容的先验，纯噪声。"""
    harness.write_terms("# 全是注释\n#\n\n   \n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "没有术语也能转"})

    assert r.returncode == 0, r.stderr
    calls = harness.calls("mlx-qwen3-asr")
    assert len(calls) == 1
    assert "--context" not in calls[0], (
        "一个只有注释的术语表必须等于没有术语表，不能发一个空前缀出去：%r" % (calls[0],))


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
        "词组没保住或去重没生效，实际是 %r" % (terms,))


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
    """`zh-voice --health` 是 deploy.sh 的上线闸门，也是人自查的入口。"""
    harness.live_daemon(
        reply=lambda req: ("PONG stub/model" if req.startswith("PING")
                           else "不该走到这里"))

    r = harness.run(["--health"])

    assert r.returncode == 0, r.stderr
    assert "healthy" in r.stdout
    assert "stub/model" in r.stdout, (
        "PONG 带回来的模型名要回显出来：它能分辨「daemon 活着」和「daemon 活着而且跑的"
        "是我刚装的那一版」；实际输出 %r" % (r.stdout,))
    assert harness.calls("mlx-qwen3-asr") == [], (
        "健康检查不能顺手起一次真转写：它要测的是服务活不活，不是能不能转写")


# --- 48 ------------------------------------------------------------------
def test_doctor_reports_each_dependency_and_fails_on_a_missing_model(harness):
    """出问题时第一件事该是跑体检，而不是逐项猜。

    模型缓存必须是 FAIL 而不是 warn：daemon 跑离线模式，缓存不在就永远起不来，而症状
    只是「热键没反应」。
    """
    r = harness.run(["--doctor"])

    assert "soxi" in r.stdout and "nc" in r.stdout, (
        "体检要逐项列出依赖和它们的实际路径：%r" % (r.stdout,))
    assert r.returncode != 0, (
        "沙箱里没有模型缓存也没有 daemon，体检必须以非 0 退出")
    assert "FAIL" in r.stdout and "model" in r.stdout, (
        "缺模型缓存要明确报 FAIL：%r" % (r.stdout,))
    assert "zh-voice-audio" in r.stdout and "vad model" in r.stdout, (
        "VAD 也要逐项体检：它坏了听写不会停（fail-open），只会静悄悄退回没有闸门的状态"
        "——正因为无声，才必须能一眼查出来：%r" % (r.stdout,))
    assert harness.calls("mlx-qwen3-asr") == [], "体检不能顺手跑一次转写"


# --- 39 ------------------------------------------------------------------
def test_health_fails_when_no_daemon_is_running(harness):
    """没有 socket = daemon 没在跑。要以非 0 退出，否则 deploy.sh 会把"根本没起来"
    当成上线成功——那正是旧版预热超时还 exit 0 的老毛病。"""
    r = harness.run(["--health"])

    assert r.returncode == 1, (
        "没有 daemon 必须是失败退出，实际 rc=%s stdout=%r" % (r.returncode, r.stdout))
    assert "unhealthy" in r.stderr, r.stderr
    assert harness.calls("mlx-qwen3-asr") == [], "健康检查不该退回一次性 CLI"
    assert harness.daemon_launches(wait=0.5) == [], (
        "--health 是只读探针，不能顺手把 daemon 拉起来：那会让检查本身改变被检查的"
        "东西，deploy.sh 也就再也分不清服务是本来就活着，还是刚被它自己救活的")


# --- 40 ------------------------------------------------------------------
def test_health_fails_on_a_socket_with_nobody_behind_it(harness):
    """正是"进程活着、服务已死"从客户端看到的样子：socket 文件在，连不上/答不出。

    只判断 `[ -S socket ]` 的检查会在这里报健康——旧 deploy.sh 就是这么判的。
    """
    sock = harness.stale_socket()
    assert sock.is_socket()

    r = harness.run(["--health"])

    assert r.returncode == 1, (
        "socket 文件存在但没人应答，必须判成 unhealthy，实际 rc=%s stdout=%r"
        % (r.returncode, r.stdout))
    assert "unhealthy" in r.stderr, r.stderr


# --- 49 ------------------------------------------------------------------
def test_local_vad_rejects_silence_before_any_asr_path(harness):
    """静音绝不能到达带术语表的识别器。

    本机实测过：喂它底噪/风扇声/白噪声，配上线上那份 506 字符的 context，模型会把
    **整份术语表**当成转写结果吐出来，然后被自动粘贴到当前 app。所以闸门要卡在
    两条 ASR 路径之前，而不是事后过滤。

    契约是「退出码 0 + 不输出 + stderr 也空」：init.lua 对这个组合已经有分支，
    显示"没听清，再说一次"，一个字都不粘。
    """
    harness.live_daemon(reply="Technical terms: LIBERO, sbatch, mLSTM.")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\n")
    wav = harness.wav(name="silence.wav", seconds=1.5)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "silence"})

    assert r.returncode == 0, r.stderr
    assert r.stdout == "", "静音必须一个字都不输出，否则 init.lua 会把它粘出去"
    assert r.stderr == "", "stderr 非空会被 init.lua 判成转写失败，那是另一个状态"
    assert len(harness.calls("zh-voice-audio")) == 1
    assert harness.calls("nc") == [], "静音不该占用 daemon"
    assert harness.calls("mlx-qwen3-asr") == [], "静音更不该触发一次性 CLI"


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
    """检测器自己出错时必须照常转写。

    反过来（fail-closed）会让一次探测失败直接吃掉一句真话，而用户看到的只是
    "没听清"——这种静默丢字比偶尔漏过一次噪声难查得多。
    """
    harness.live_daemon(reply="检测器坏了也不能丢语音")
    wav = harness.wav(name="speech.wav", seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "error"})

    assert r.returncode == 0
    assert r.stdout.strip() == "检测器坏了也不能丢语音"
    assert "VAD 检查失败" in r.stderr


# --- 52 ------------------------------------------------------------------
def test_missing_vad_model_fails_open_without_tripping_set_e(harness):
    """模型缓存没了也要能听写。`set -euo pipefail` 下这条最容易写成静默退出。"""
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
    """VAD 之后的第二道网：闸门是 fail-open 的，泄漏那条路并没被完全堵死。

    模型照着 context 念的时候输出是术语表的前缀（被 max token 截断），而且前面常带
    一个"技术术语："标签、标点是全角。这三种形态都要认得出来。
    """
    harness.live_daemon(
        reply="技术术语：LIBERO，sbatch，mLSTM，checkpoint")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "speech"})

    assert r.returncode == 0
    assert r.stdout == "", "照着术语表念出来的东西不是人说的话，不能粘：%r" % (r.stdout,)
    assert r.stderr == ""


# --- 54 ------------------------------------------------------------------
def test_real_speech_that_merely_mentions_a_term_is_kept(harness):
    """上面那张网不能宽到吃掉正常说话——他本来就一天到晚在说这些词。"""
    harness.live_daemon(reply="LIBERO 那个 checkpoint 我等下再看")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "speech"})

    assert r.returncode == 0
    assert r.stdout.strip() == "LIBERO 那个 checkpoint 我等下再看"


# --- 65 --- 2026-07-29：私密临时文件 / 裁剪 / 指标 / 回声重试 --------------
def test_vad_is_asked_for_json_and_trim(harness):
    """裁掉首尾静音不是锦上添花：长停顿正是 ASR 产生幻觉的温床，而且送进去的
    音频越短，转写越快。所以闸门那一趟顺手把 --trim 带上。"""
    harness.live_daemon(reply="裁过之后的正常语音")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    call = harness.calls("zh-voice-audio")[0]
    assert "--json" in call, "没有 --json 就拿不到指标"
    assert "--trim" in call, "没有 --trim 就不会裁静音"


def test_vad_trim_can_be_turned_off(harness):
    harness.live_daemon(reply="没裁的正常语音")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_VAD_TRIM": "0"})

    assert r.returncode == 0, r.stderr
    assert "--trim" not in harness.calls("zh-voice-audio")[0]


def test_metrics_record_numbers_but_never_the_transcript(harness, tmp_path):
    """指标是为了分清"没听清"到底是谁吃掉的。但**绝不能**把转写文本抄进去——
    这一轮做的事就是把音频从磁盘上清掉，没道理换个地方又把内容写下来。"""
    secret = "这句话绝对不能出现在指标里"
    harness.live_daemon(reply=secret)
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0, r.stderr
    line = metrics.read_text()
    assert secret not in line, "指标里出现了转写文本：%r" % (line,)
    assert ".wav" not in line, "指标里出现了音频路径：%r" % (line,)
    for key in ("ok=1", "path=daemon", "asr_ms=", "vad_p=0.97", "audio_ms=1500",
                "trimmed_ms=980", "retry=0"):
        assert key in line, "指标缺 %s：%r" % (key, line)


def test_metrics_distinguish_a_vad_gated_utterance(harness, tmp_path):
    harness.live_daemon(reply="不该被叫到")
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_VAD_RESULT": "silence",
                                      "ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0 and r.stdout == ""
    line = metrics.read_text()
    assert "gated=1" in line, line
    assert "vad_p=0.013" in line, "被拦时也要留下概率，否则没法判断是不是拦错了"


def test_reordered_term_echo_is_retried_without_context_and_recovered(harness):
    """判据二：不是前缀、但内容大部分直接来自术语表 —— 模型在乱序照念。

    这种情况下不再直接丢掉，而是**不带 context 重转一次**：闸门是 fail-open 的，
    底下真有可能有话，只是被术语偏置盖过去了。
    """
    def by_context(request):
        # 协议是两行：wav 路径 + context。带偏置时照念术语表，不带时说人话。
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
        "重试拿到了干净结果就该用它，而不是把回声丢了了事：%r" % (r.stdout,)


def test_term_echo_still_dropped_when_retry_is_also_an_echo(harness):
    """重试回来还是照念术语表 —— 那就真的没有话，走和静音一样的契约。"""
    harness.live_daemon(reply="mLSTM，checkpoint，sbatch，benchmark")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0
    assert r.stdout == "", "两次都是回声还往外粘：%r" % (r.stdout,)
    assert r.stderr == ""


def test_real_speech_mentioning_three_terms_is_not_mistaken_for_an_echo(harness):
    """覆盖率那条判据必须收得住：他本来就一天到晚在说这些词。"""
    harness.live_daemon(reply="LIBERO 的 checkpoint 和 benchmark 我等下一起看")
    harness.write_terms("LIBERO\nsbatch\nmLSTM\ncheckpoint\nbenchmark\n")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "LIBERO 的 checkpoint 和 benchmark 我等下一起看"


def test_speech_with_full_term_list_appended_is_retried_and_recovered(harness):
    """判据三：真话说完，术语表整段贴在后面。判据一拦不住（不是前缀），
    判据二也拦不住（真话把覆盖率稀释到 75% 以下）——2026-08-14 实测泄漏过一次：
    10.8s 语音出 582 字，整段被粘进当时聚焦的 app。"""
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
        "贴尾回声该触发重试并救回真话：%r" % (r.stdout,)


def test_real_speech_ending_with_a_short_term_list_is_not_an_echo(harness):
    """判据三必须收得住：句尾真的连报三个术语（不到 60 字）是日常说法。"""
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
    """`--model <repo id>` 让 huggingface_hub 去读 refs/main，钉不住版本。缓存里有钉死的
    那个 snapshot 时，要把**它的路径**交给 CLI（load_model 的形参就叫 path_or_hf_repo）。"""
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
    assert got == str(snap), "--model 收到的是 %r，不是钉死的 snapshot 路径" % (got,)


def test_cli_fallback_still_transcribes_when_the_pin_is_missing(harness):
    """钉死的 snapshot 不在时宁可版本没钉住也要出字：一次听写不该因为 revision 的事
    整条链路停摆。该拦住漂移的是 deploy 的预检（那里是 die）和 --doctor。"""
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "没钉住也照样出字"})

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "没钉住也照样出字"
    calls = harness.calls("mlx-qwen3-asr")
    assert calls[0][calls[0].index("--model") + 1] == "stub/model", (
        "找不到钉死的 snapshot 时该退回 repo id：%r" % (calls[0],))


def test_doctor_reports_the_tokenizer_as_pinned_not_as_a_missing_upstream_cache(harness):
    """钉住之后，上游那个仓库的缓存（本机 1.7 GB，99% 是我们从不加载的 0.6B 权重）就该能删。
    体检要是还按"上游缓存在不在"报 FAIL，那就是一条**假警报**，而假警报会训练人忽略体检。"""
    rev = "a" * 40
    snap = (harness.home / ".cache" / "huggingface" / "hub"
            / "models--stub--model" / "snapshots" / rev)
    snap.mkdir(parents=True)
    for name in ("config.json", "vocab.json", "merges.txt", "tokenizer_config.json"):
        (snap / name).write_text("{}", encoding="utf-8")

    r = harness.run(["--doctor"], extra_env={"ZH_VOICE_ASR_REVISION": rev})

    line = [ln for ln in r.stdout.splitlines() if "tokenizer" in ln]
    assert line, "体检没有 tokenizer 这一行：%r" % (r.stdout,)
    assert line[0].startswith("ok"), (
        "钉得住却没报 ok（沙箱里根本没有上游缓存，这正是删掉之后的样子）：%r" % (line[0],))


# ── 热路径省下来的那些 spawn（2026-07-30）───────────────────────────────────
def test_from_recorder_trusts_the_caller_and_skips_the_soxi_probes(harness):
    """`--from-recorder` = 调用方（init.lua）保证这份 wav 是我们录音器写的 16k/mono。

    三次 soxi（-r/-c 探格式、-D 查时长）问的都是已经知道的事，实测每次 6.4ms。时长下限
    也归 init.lua 的 MIN_BYTES 管，不必在这里重判一遍。
    """
    harness.live_daemon(reply="不必再问格式")
    wav = harness.wav(seconds=1.0, rate=16000, channels=1)

    r = harness.run(["--from-recorder", wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "不必再问格式"
    assert harness.calls("soxi") == [], (
        "带 --from-recorder 还去问 soxi：热路径每轮白花三次 spawn")
    assert harness.calls("ffmpeg") == [], "不该转码"


def test_without_the_flag_the_format_is_still_probed(harness):
    """独立入口（人手给一个任意 wav）没有那个保证，格式探测必须留着。

    反向断言：省 spawn 不能顺手把这条路也省成"假设 16k/mono"，那样一份 44.1k 立体声
    会被当 16k 送进 ASR，出来是快放的胡言乱语。
    """
    harness.live_daemon(reply="探过格式")
    wav = harness.wav(seconds=1.0, rate=16000, channels=1)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert harness.calls("soxi") != [], "没带旗标时仍然必须探格式"


def test_server_reported_asr_ms_is_what_lands_in_metrics(harness, tmp_path):
    """耗时改由 daemon 自报（回复首行 META）。客户端原来那两次 perl（各 5ms）取消了。"""
    harness.live_daemon(reply="META asr_ms=137\n服务端计时的转写")
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "服务端计时的转写", "META 那一行不能被当成转写内容"
    assert "asr_ms=137" in metrics.read_text(), (
        "服务端报的耗时没进指标：%r" % (metrics.read_text(),))


def test_a_reply_without_meta_still_yields_the_transcript(harness, tmp_path):
    """没有 META 的回复（PING、ERROR、或者哪天换个 daemon）整串就是转写，行为不变。"""
    harness.live_daemon(reply="没有头信息的回复")
    metrics = tmp_path / "metrics.log"
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav], extra_env={"ZH_VOICE_METRICS": str(metrics)})

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "没有头信息的回复"
    assert "asr_ms=?" in metrics.read_text(), (
        "拿不到服务端耗时就该记 ?，不能编一个数出来：%r" % (metrics.read_text(),))


def test_a_single_line_reply_that_looks_like_meta_is_not_eaten(harness):
    """判 META 要求它后面真有换行。否则一句正好以 "META asr_ms=" 开头的转写会被
    整行当成头信息丢掉——用户那句话就凭空消失了。"""
    harness.live_daemon(reply="META asr_ms=999")
    wav = harness.wav(seconds=1.0)

    r = harness.run([wav])

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "META asr_ms=999", (
        "单行回复必须原样当转写输出，实得 %r" % (r.stdout,))


def test_terms_cache_is_read_back_and_an_edit_invalidates_it(harness):
    """术语串和它的归一化形式只随 terms.txt 变，缓存起来省掉每轮 ~15ms。

    新鲜度靠缓存第一行那个 key（terms.txt 的 mtime+大小）。这里先把缓存内容换成哨兵、
    **保留原 key**，证明它真的在读；再改 terms.txt 证明 key 一变就重算——只测第二半的话，
    一个永远不读缓存的实现也能通过。
    """
    harness.write_terms("LIBERO\nsbatch\n")
    wav = harness.wav(seconds=1.0)
    cache = harness.home / ".cache" / "zh-voice" / "terms.cache"

    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "第一次"})
    assert r.returncode == 0, r.stderr
    assert cache.exists(), "第一轮没写出缓存"

    key = cache.read_text(encoding="utf-8").splitlines()[0]
    cache.write_text("%s\n哨兵术语\n哨兵术语\n" % key, encoding="utf-8")
    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "第二次"})
    ctx = harness.calls("mlx-qwen3-asr")[-1]
    ctx = ctx[ctx.index("--context") + 1]
    assert "哨兵术语" in ctx, "缓存没被读：context 里应该是哨兵，实得 %r" % (ctx,)

    harness.write_terms("mLSTM\nGR00T\n")       # 改动让 terms.txt 变新
    r = harness.run([wav], extra_env={"STUB_ASR_STDOUT": "第三次"})
    ctx = harness.calls("mlx-qwen3-asr")[-1]
    ctx = ctx[ctx.index("--context") + 1]
    assert "mLSTM" in ctx and "哨兵术语" not in ctx, (
        "terms.txt 改了却还在用旧缓存：%r" % (ctx,))


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
