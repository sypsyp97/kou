"""Compile the native quit callback against isolated lifecycle boundaries."""

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from test_clipboard_safety import SOURCE_ROOT, matching_delimiter


HARNESS = r'''
import Foundation

@MainActor final class HarnessApplication {
    enum TerminateReply { case terminateNow, terminateCancel, terminateLater }
    var replies = 0
    func reply(toApplicationShouldTerminate answer: Bool) { if answer { replies += 1 } }
}
@MainActor enum ManagedTask { static var activeCount = 1 }
@MainActor final class HarnessTimer {
    let action: (HarnessTimer) -> Void
    var valid = true
    init(timeInterval: TimeInterval, repeats: Bool, block: @escaping (HarnessTimer) -> Void) { action = block }
    func invalidate() { valid = false }
    func fire() { if valid { action(self) } }
}
@MainActor final class HarnessRunLoop {
    enum Mode { case common, modalPanel }
    static let main = HarnessRunLoop()
    var timers: [HarnessTimer] = []
    func add(_ timer: HarnessTimer, forMode: Mode) {
        if !timers.contains(where: { $0 === timer }) { timers.append(timer) }
    }
    func tick() { for timer in timers { timer.fire() } }
}
@MainActor final class HarnessPaths {
    var sweeps = 0
    func sweepRecordings() { sweeps += 1 }
}
@MainActor final class HarnessRuntime { let paths = HarnessPaths() }
@MainActor final class HarnessModel {
    let runtime = HarnessRuntime()
    var shutdowns = 0
    func shutdown() { shutdowns += 1 }
}
@MainActor final class TerminationSubject {
    var terminationPending = false
    var model: HarnessModel? = HarnessModel()
    // TERMINATION_CALLBACK
}
@main struct QuitHarness {
    @MainActor static func main() {
        let subject = TerminationSubject()
        let application = HarnessApplication()
        var allDeferred = true
        for _ in 0..<3 {
            if case .terminateLater = subject.applicationShouldTerminate(application) {} else { allDeferred = false }
        }
        HarnessRunLoop.main.tick()
        let repliesWhileBusy = application.replies
        ManagedTask.activeCount = 0
        HarnessRunLoop.main.tick()
        HarnessRunLoop.main.tick()
        _ = subject.applicationShouldTerminate(application)
        HarnessRunLoop.main.tick()
        let result: [String: Any] = [
            "allDeferred": allDeferred,
            "busyReplies": repliesWhileBusy,
            "finalReplies": application.replies,
            "shutdowns": subject.model!.shutdowns,
            "sweeps": subject.model!.runtime.paths.sweeps
        ]
        print(String(data: try! JSONSerialization.data(withJSONObject: result), encoding: .utf8)!)
    }
}
'''


def test_repeated_quit_requests_reply_only_once_after_drain(tmp_path):
    source = (SOURCE_ROOT / "App.swift").read_text()
    start = source.index("func applicationShouldTerminate(")
    opening = source.index("{", start)
    end = matching_delimiter(source, opening, "{", "}")
    callback = source[start:end + 1]
    for original, replacement in [
        ("NSApplication", "HarnessApplication"),
        ("Timer", "HarnessTimer"),
        ("RunLoop", "HarnessRunLoop"),
    ]:
        callback = re.sub(r"\b" + original + r"\b", replacement, callback)
    harness = tmp_path / "QuitHarness.swift"
    harness.write_text(HARNESS.replace("// TERMINATION_CALLBACK", callback))
    executable = tmp_path / "quit-tests"
    compiled = subprocess.run(
        ["swiftc", "-swift-version", "5", "-parse-as-library", str(harness), "-o", str(executable)],
        capture_output=True, text=True, timeout=60,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed["allDeferred"] is True
    assert observed["busyReplies"] == 0
    assert observed["finalReplies"] == 1
    assert observed["shutdowns"] == 1
    assert observed["sweeps"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
