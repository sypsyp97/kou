"""Structural contract of Support/zh-voice-vad.swift. AVAudioEngine behavior on
real hardware is not covered here.

These tests pin the safety-critical parts of arm mode: exit codes, signal order
and "armed mode does not touch the disk". The compiler does not protect them.
A refactor can drop them silently, and the symptom (the start of speech is lost
again) is audible only.
"""
import re

from conftest import SUPPORT

SWIFT = SUPPORT / "zh-voice-vad.swift"


def test_arm_mode_exit_codes_are_the_documented_contract():
    """5 = disarmed (no file written), 6 = device changed (arm again). The caller
    uses these two numbers to tell how the process ended. If a number drifts, a
    normal disarm is logged as a crash, or the reverse."""
    code = SWIFT.read_text(encoding="utf-8")

    assert re.search(r"disarmedExit\s*:\s*Int32\s*=\s*5", code), "解除待命必须是 5"
    assert re.search(r"staleArmExit\s*:\s*Int32\s*=\s*6", code), "设备变化必须是 6"
    for use in ("return disarmedExit", "return staleArmExit"):
        assert use in code, "%s 没被用到：常量定义了但契约没接上" % use


def test_sigusr1_is_ignored_at_program_entry_not_inside_the_arm_branch():
    """The default disposition of SIGUSR1 kills the process. The signal can arrive
    before argument parsing. A SIG_IGN inside the arm branch would leave a door
    where an early GO silently kills the armed process. Pin it at program entry."""
    code = SWIFT.read_text(encoding="utf-8")

    ign = code.find("signal(SIGUSR1, SIG_IGN)")
    assert ign != -1, "没有 SIGUSR1 的 SIG_IGN"
    run_call = code.find("exit(try run())")
    assert run_call != -1, "找不到程序入口"
    assert ign < run_call, "SIG_IGN 在 run() 之后才装，早到的信号还是能杀进程"
    # It must not be inside a function body: the entry statement is top level (indent 0).
    line = code[:ign].rsplit("\n", 1)[-1]
    assert line == "", "signal(SIGUSR1, SIG_IGN) 不在顶层（前面有缩进：%r）" % line


def test_armed_handshake_prints_armed_before_waiting_for_go():
    """The caller sends GO only after it sees ARMED. This handshake is the real
    guarantee against early signals (SIG_IGN only covers the unexpected). The
    print needs fflush: stdout is a pipe, and without a flush the handshake never
    completes, every key press falls back to a cold start, and arming is wasted."""
    code = SWIFT.read_text(encoding="utf-8")

    m = re.search(r'print\("ARMED"\)\s*\n\s*fflush\(stdout\)', code)
    assert m, "ARMED 打印后没有立刻 fflush(stdout)"
    wait = code.find("armSem.wait()")
    assert wait != -1 and m.start() < wait, "ARMED 必须在等 GO 之前打印"


def test_level_file_only_written_while_capturing():
    """The level timer must start only in startCapture. The armed phase must write
    no byte to disk. A moving level bar must mean real capture, because the user
    reads it as the signal that speaking has started."""
    code = SWIFT.read_text(encoding="utf-8")

    configure = re.search(r"func configure\(.*?\n    \}", code, re.S)
    capture = re.search(r"func startCapture\(.*?\n    \}", code, re.S)
    assert configure and capture, "configure/startCapture 的拆分没了？"
    assert "startLevelTimer" not in configure.group(0), (
        "configure 里起了电平计时器：待命阶段会往磁盘写电平")
    assert "startLevelTimer" in capture.group(0), "startCapture 里没有起电平计时器"
    assert capture.group(0).find("engine.start()") < capture.group(0).find("startLevelTimer"), (
        "电平计时器起在 engine.start() 之前：引擎没起来条子就在动")


def test_voice_processing_is_off_unless_asked_for():
    """Apple voice processing is off by default. Its AGC raises the noise floor by
    46 dB in a silent room (RMS 0.031-0.061 vs 0.00013) and rewrites the gain
    heavily. The only way to turn it on is `--voice-processing`. A default that
    drifts back to true is silent and audible only."""
    code = SWIFT.read_text(encoding="utf-8")

    m = re.search(r"var voiceProcessing = (true|false)", code)
    assert m, "recordCommand 里找不到 voiceProcessing 的默认值"
    assert m.group(1) == "false", "Apple 语音处理又变成默认开了"
    assert 'case "--voice-processing": voiceProcessing = true' in code, (
        "没有把 VP 开回去的旗标：出问题时只能重新编译才能 A/B")


def test_other_audio_ducking_is_pinned_to_min_without_advanced():
    """Enabling VP ducks other audio to -15 dB (scalar 0.178), and it ducks even
    while armed without capture. This is the root cause of muffled video playback.
    .min is -4 dB (0.631). enableAdvancedDucking = true ignores .min and falls
    back to -15 dB, so the false value is as important as .min."""
    code = SWIFT.read_text(encoding="utf-8")

    m = re.search(
        r"voiceProcessingOtherAudioDuckingConfiguration\s*=\s*\n?\s*"
        r"AVAudioVoiceProcessingOtherAudioDuckingConfiguration\(\s*\n?\s*"
        r"enableAdvancedDucking:\s*false,\s*duckingLevel:\s*\.min\)",
        code,
    )
    assert m, "ducking 配置不是 (enableAdvancedDucking: false, duckingLevel: .min)"
    enabled = code.find("setVoiceProcessingEnabled(true)")
    assert enabled != -1 and enabled < m.start(), (
        "ducking 配置在开 VP 之前设：那时还没有 VPIO 单元，设了也白设")
    assert 'degraded.append("ducking_not_min")' in code, (
        "没有回读校验：属性被系统悄悄改掉时无人知晓")


def test_config_change_while_armed_requests_a_rearm():
    """If the default input changes or headphones are plugged in while armed, the
    prepared engine graph is stale. Without an exit and re-arm, start() throws at
    GO and the whole utterance is lost. Unsubscribe after GO: behavior during
    recording must be identical to a cold start."""
    code = SWIFT.read_text(encoding="utf-8")

    assert "AVAudioEngineConfigurationChange" in code, "没订阅设备变化通知"
    sub = code.find("AVAudioEngineConfigurationChange")
    unsub = code.find("removeObserver")
    assert unsub != -1 and sub < unsub, "GO 之后没有退订：录音中设备变化的行为会和冷启动不同"
