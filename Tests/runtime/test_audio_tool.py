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

    assert re.search(r"disarmedExit\s*:\s*Int32\s*=\s*5", code), "disarmedExit must be 5"
    assert re.search(r"staleArmExit\s*:\s*Int32\s*=\s*6", code), "staleArmExit must be 6"
    for use in ("return disarmedExit", "return staleArmExit"):
        assert use in code, "%s is never used: constant defined but not wired into the contract" % use


def test_sigusr1_is_ignored_at_program_entry_not_inside_the_arm_branch():
    """The default disposition of SIGUSR1 kills the process. The signal can arrive
    before argument parsing. A SIG_IGN inside the arm branch would leave a door
    where an early GO silently kills the armed process. Pin it at program entry."""
    code = SWIFT.read_text(encoding="utf-8")

    ign = code.find("signal(SIGUSR1, SIG_IGN)")
    assert ign != -1, "missing SIG_IGN for SIGUSR1"
    run_call = code.find("exit(try run())")
    assert run_call != -1, "program entry not found"
    assert ign < run_call, "SIG_IGN must be installed before run(), or an early signal kills the process"
    # It must not be inside a function body: the entry statement is top level (indent 0).
    line = code[:ign].rsplit("\n", 1)[-1]
    assert line == "", "signal(SIGUSR1, SIG_IGN) must be at top level (indented: %r)" % line


def test_armed_handshake_prints_armed_before_waiting_for_go():
    """The caller sends GO only after it sees ARMED. This handshake is the real
    guarantee against early signals (SIG_IGN only covers the unexpected). The
    print needs fflush: stdout is a pipe, and without a flush the handshake never
    completes, every key press falls back to a cold start, and arming is wasted."""
    code = SWIFT.read_text(encoding="utf-8")

    m = re.search(r'print\("ARMED"\)\s*\n\s*fflush\(stdout\)', code)
    assert m, "ARMED must be followed immediately by fflush(stdout)"
    wait = code.find("armSem.wait()")
    assert wait != -1 and m.start() < wait, "ARMED must be printed before waiting for GO"


def test_level_file_only_written_while_capturing():
    """The level timer must start only in startCapture. The armed phase must write
    no byte to disk. A moving level bar must mean real capture, because the user
    reads it as the signal that speaking has started."""
    code = SWIFT.read_text(encoding="utf-8")

    configure = re.search(r"func configure\(.*?\n    \}", code, re.S)
    capture = re.search(r"func startCapture\(.*?\n    \}", code, re.S)
    assert configure and capture, "configure/startCapture split is missing"
    assert "startLevelTimer" not in configure.group(0), (
        "configure starts the level timer: the armed phase would write levels to disk")
    assert "startLevelTimer" in capture.group(0), "startCapture does not start the level timer"
    assert capture.group(0).find("engine.start()") < capture.group(0).find("startLevelTimer"), (
        "level timer starts before engine.start(): the level bar would move before the engine runs")


def test_voice_processing_is_off_unless_asked_for():
    """Apple voice processing is off by default. Its AGC raises the noise floor by
    46 dB in a silent room (RMS 0.031-0.061 vs 0.00013) and rewrites the gain
    heavily. The only way to turn it on is `--voice-processing`. A default that
    drifts back to true is silent and audible only."""
    code = SWIFT.read_text(encoding="utf-8")

    m = re.search(r"var voiceProcessing = (true|false)", code)
    assert m, "voiceProcessing default not found in recordCommand"
    assert m.group(1) == "false", "Apple voice processing must be off by default"
    assert 'case "--voice-processing": voiceProcessing = true' in code, (
        "missing --voice-processing flag to turn VP back on without recompiling")


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
    assert m, "ducking config must be (enableAdvancedDucking: false, duckingLevel: .min)"
    enabled = code.find("setVoiceProcessingEnabled(true)")
    assert enabled != -1 and enabled < m.start(), (
        "ducking config must be set after enabling VP, when the VPIO unit exists")
    assert 'degraded.append("ducking_not_min")' in code, (
        "missing read-back check: a silent system change to the property goes unnoticed")


def test_config_change_while_armed_requests_a_rearm():
    """If the default input changes or headphones are plugged in while armed, the
    prepared engine graph is stale. Without an exit and re-arm, start() throws at
    GO and the whole utterance is lost. Unsubscribe after GO: behavior during
    recording must be identical to a cold start."""
    code = SWIFT.read_text(encoding="utf-8")

    assert "AVAudioEngineConfigurationChange" in code, "AVAudioEngineConfigurationChange is not observed"
    sub = code.find("AVAudioEngineConfigurationChange")
    unsub = code.find("removeObserver")
    assert unsub != -1 and sub < unsub, "observer must be removed after GO so recording behaves like a cold start"
