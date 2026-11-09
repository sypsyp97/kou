"""Support/zh-voice-vad.swift 的结构契约（真机上的 AVAudioEngine 行为归 deploy 冒烟管）。

这里钉的是 arm 模式的安全关键面：退出码、信号次序、"待命不碰磁盘"。它们没有编译期
保护，一次重构就能悄悄掉，而症状全都是"开头又开始漏字"这种要用耳朵才发现的。
"""
import re

from conftest import SUPPORT

SWIFT = SUPPORT / "zh-voice-vad.swift"


def test_arm_mode_exit_codes_are_the_documented_contract():
    """5 = 解除待命（零文件），6 = 设备变化（要重臂）。init.lua 靠这两个数分辨
    "怎么退的"，漂一个数就是把"正常收掉"当成"意外崩溃"记日志、或反过来。"""
    code = SWIFT.read_text(encoding="utf-8")

    assert re.search(r"disarmedExit\s*:\s*Int32\s*=\s*5", code), "解除待命必须是 5"
    assert re.search(r"staleArmExit\s*:\s*Int32\s*=\s*6", code), "设备变化必须是 6"
    for use in ("return disarmedExit", "return staleArmExit"):
        assert use in code, "%s 没被用到：常量定义了但契约没接上" % use


def test_sigusr1_is_ignored_at_program_entry_not_inside_the_arm_branch():
    """SIGUSR1 默认处置是杀进程。实测它能赶在参数解析之前到——SIG_IGN 写在 arm 分支
    里等于给"早到的 GO"留了一扇能无声杀掉待命进程的门。必须钉在程序入口。"""
    code = SWIFT.read_text(encoding="utf-8")

    ign = code.find("signal(SIGUSR1, SIG_IGN)")
    assert ign != -1, "没有 SIGUSR1 的 SIG_IGN"
    run_call = code.find("exit(try run())")
    assert run_call != -1, "找不到程序入口"
    assert ign < run_call, "SIG_IGN 在 run() 之后才装，早到的信号还是能杀进程"
    # 且不在某个函数体里：入口那份是顶层语句（缩进为 0）
    line = code[:ign].rsplit("\n", 1)[-1]
    assert line == "", "signal(SIGUSR1, SIG_IGN) 不在顶层（前面有缩进：%r）" % line


def test_armed_handshake_prints_armed_before_waiting_for_go():
    """调用方只在见到 ARMED 后才发 GO——这是早到信号问题的真正保证（SIG_IGN 只是兜
    意外）。打印必须带 fflush：stdout 接的是管道，不 flush 的话 Lua 永远握不到手，
    每次按键都退回冷启动，预备等于白做。"""
    code = SWIFT.read_text(encoding="utf-8")

    m = re.search(r'print\("ARMED"\)\s*\n\s*fflush\(stdout\)', code)
    assert m, "ARMED 打印后没有立刻 fflush(stdout)"
    wait = code.find("armSem.wait()")
    assert wait != -1 and m.start() < wait, "ARMED 必须在等 GO 之前打印"


def test_level_file_only_written_while_capturing():
    """电平计时器必须从 startCapture 才开始：待命阶段一个字节都不该出现在磁盘上，
    而且条子动了就该意味着真的在采集（这是用户判断"开始说话了没"的信号）。"""
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
    """Apple 语音处理默认关（2026-08-24）：它的 AGC 在静音房间里把底噪抬 46dB
    （实测 RMS 0.031-0.061 vs 0.00013），一直在大幅改写增益。开回去只有一条路
    `--voice-processing`；默认值漂回 true 是静默的，只有耳朵能发现。"""
    code = SWIFT.read_text(encoding="utf-8")

    m = re.search(r"var voiceProcessing = (true|false)", code)
    assert m, "recordCommand 里找不到 voiceProcessing 的默认值"
    assert m.group(1) == "false", "Apple 语音处理又变成默认开了"
    assert 'case "--voice-processing": voiceProcessing = true' in code, (
        "没有把 VP 开回去的旗标：出问题时只能重新编译才能 A/B")


def test_other_audio_ducking_is_pinned_to_min_without_advanced():
    """VP 一开就把其它音频压到 −15dB（scalar 0.178），而且待命不采集时也照压——
    这是"看视频一直闷着"的根因。.min 是 −4dB（0.631）。⚠️ enableAdvancedDucking
    设 true 会把 .min 吃掉、退回 −15dB，所以这个 false 和 .min 同等重要。"""
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
    """待命中换默认输入/插拔耳机，预备的引擎图就过期了。不自杀重臂的话，GO 那一刻
    start() 抛错，用户那一句话整个丢掉。GO 之后必须退订——录音中的行为要和冷启动
    完全一致。"""
    code = SWIFT.read_text(encoding="utf-8")

    assert "AVAudioEngineConfigurationChange" in code, "没订阅设备变化通知"
    sub = code.find("AVAudioEngineConfigurationChange")
    unsub = code.find("removeObserver")
    assert unsub != -1 and sub < unsub, "GO 之后没有退订：录音中设备变化的行为会和冷启动不同"
