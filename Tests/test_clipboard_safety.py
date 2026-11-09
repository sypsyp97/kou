"""Run native clipboard code against a named pasteboard and fake UI boundaries."""

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "Sources" / "Kou"

HARNESS = r'''
import AppKit
import CoreGraphics
import Foundation

func L(_ chinese: String, _ english: String) -> String {
    Locale.preferredLanguages.first?.hasPrefix("zh") == true ? chinese : english
}

@MainActor final class HarnessBoard {
    typealias PasteboardType = NSPasteboard.PasteboardType
    static let general = HarnessBoard()
    let real = NSPasteboard(name: NSPasteboard.Name("KouTests." + UUID().uuidString))
    var hijackWrite = false
    var failWrite = false
    var brokenProvider = false
    var unreadableItems = false
    enum AccessBehavior { case alwaysAllow, alwaysDeny }
    var accessBehavior: AccessBehavior = .alwaysAllow
    var types: [PasteboardType]? { real.types }
    var changeCount: Int { real.changeCount }
    var pasteboardItems: [NSPasteboardItem]? {
        if unreadableItems { return nil }
        return brokenProvider ? [BrokenProviderItem()] : real.pasteboardItems
    }
    @discardableResult func clearContents() -> Int { real.clearContents() }
    @discardableResult func writeObjects(_ objects: [NSPasteboardWriting]) -> Bool {
        let failed = failWrite
        failWrite = false
        let result = failed ? false : real.writeObjects(objects)
        if hijackWrite {
            hijackWrite = false
            external("external clipboard")
        }
        return result
    }
    func external(_ value: String) {
        real.clearContents()
        real.setString(value, forType: .string)
    }
    var text: String { real.string(forType: .string) ?? "" }
    func data(forType type: PasteboardType) -> Data? { real.data(forType: type) }
}

final class BrokenProviderItem: NSPasteboardItem {
    override var types: [NSPasteboard.PasteboardType] { [.string] }
    override func data(forType type: NSPasteboard.PasteboardType) -> Data? { nil }
}

@MainActor final class HarnessWorkspace {
    static let shared = HarnessWorkspace()
    struct Application { let processIdentifier: pid_t }
    var frontmostApplication: Application? = Application(processIdentifier: 4242)
}

@MainActor func harnessTrusted() -> Bool { true }

@MainActor final class HarnessRunningApplication {
    let processIdentifier: pid_t
    var isTerminated = false
    init?(processIdentifier: pid_t) { self.processIdentifier = processIdentifier }
}

@MainActor final class HarnessEvent {
    static var events: [[String: Any]] = []
    static var failCreation = false
    var flags: CGEventFlags = []
    let down: Bool
    init?(keyboardEventSource: CGEventSource?, virtualKey: CGKeyCode, keyDown: Bool) {
        if Self.failCreation { return nil }
        down = keyDown
    }
    func post(tap: CGEventTapLocation) {
        Self.events.append(["down": down, "pid": -1])
    }
    func postToPid(_ pid: pid_t) {
        Self.events.append(["down": down, "pid": Int(pid)])
    }
}

@MainActor final class HarnessClock {
    static let shared = HarnessClock()
    private var entries: [(DispatchTime, () -> Void)] = []
    func asyncAfter(deadline: DispatchTime, execute action: @escaping () -> Void) {
        entries.append((deadline, action))
    }
    func runNext() {
        entries.sort { $0.0.uptimeNanoseconds < $1.0.uptimeNanoseconds }
        if !entries.isEmpty { entries.removeFirst().1() }
    }
    func runAll() {
        var budget = 100
        while !entries.isEmpty && budget > 0 { runNext(); budget -= 1 }
    }
}

// CONTROLLER_SEAM

@main struct ClipboardHarness {
    @MainActor static func main() {
        let scenario = CommandLine.arguments[1]
        let board = HarnessBoard.general
        defer { board.real.releaseGlobally() }
        board.external("original clipboard")
        let paster = ClipboardPaster()
        var completions: [[String: Any]] = []
        func paste(_ operation: String) {
            paster.paste("retained transcript " + operation, targetPID: 4242, valid: { true }) { ok, message in
                completions.append(["operation": operation, "success": ok, "message": message])
            }
        }
        var extra: [String: Any] = [:]
        switch scenario {
        case "native-counts":
            extra["before"] = board.real.changeCount
            extra["cleared"] = board.real.clearContents()
            let item = NSPasteboardItem()
            item.setString("native write", forType: .string)
            extra["wrote"] = board.real.writeObjects([item])
            extra["afterWrite"] = board.real.changeCount
            extra["readBack"] = board.real.string(forType: .string) ?? ""
            extra["afterRead"] = board.real.changeCount
        case "ownership-during-write":
            board.hijackWrite = true
            paste("first")
            HarnessClock.shared.runAll()
            paster.restore()
        case "ownership-before-paste":
            paste("first")
            board.external("external clipboard")
            HarnessClock.shared.runAll()
        case "ownership-before-restore":
            paste("first")
            HarnessClock.shared.runNext()
            HarnessClock.shared.runNext()
            board.external("external clipboard")
            paster.restore()
            HarnessClock.shared.runAll()
        case "targeted-events":
            paste("first")
            HarnessClock.shared.runAll()
        case "cancel-before-down":
            paste("first")
            paster.cancel()
            paste("replacement")
            HarnessClock.shared.runAll()
        case "cancel-after-down":
            paste("first")
            HarnessClock.shared.runNext()
            paster.cancel()
            paste("replacement")
            let before = HarnessEvent.events.count
            HarnessClock.shared.runNext()
            extra["staleEvents"] = HarnessEvent.events.count - before
            extra["staleSuccess"] = completions.contains { ($0["operation"] as? String) == "first" && ($0["success"] as? Bool) == true }
            HarnessClock.shared.runAll()
        case "write-failure":
            board.failWrite = true
            paste("first")
            HarnessClock.shared.runAll()
        case "write-failure-with-new-owner":
            board.failWrite = true
            board.hijackWrite = true
            paste("first")
            HarnessClock.shared.runAll()
        case "provider-failure":
            board.brokenProvider = true
            paste("first")
            HarnessClock.shared.runAll()
        case "event-creation-failure":
            HarnessEvent.failCreation = true
            paste("first")
            HarnessClock.shared.runAll()
        case "unreadable-items":
            board.unreadableItems = true
            paste("first")
            HarnessClock.shared.runAll()
        case "access-denied":
            board.accessBehavior = .alwaysDeny
            paste("first")
            HarnessClock.shared.runAll()
        case "retained-write", "retained-provider", "retained-event":
            board.failWrite = scenario == "retained-write"
            board.brokenProvider = scenario == "retained-provider"
            HarnessEvent.failCreation = scenario == "retained-event"
            let controller = HarnessController(paster)
            controller.deliver("manual copy transcript")
            HarnessClock.shared.runAll()
            extra["retainedTranscript"] = controller.lastTranscript
        default:
            fatalError("Unknown test scenario")
        }
        var result: [String: Any] = ["clipboard": board.text, "events": HarnessEvent.events, "completions": completions]
        for (key, value) in extra { result[key] = value }
        let data = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        print(String(data: data, encoding: .utf8)!)
    }
}
'''


def matching_delimiter(source, start, opening, closing):
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("Cannot isolate native source boundary")


def controller_handoff_source():
    source = (SOURCE_ROOT / "CaptureController.swift").read_text()
    start = source.index("self.lastTranscript = text")
    call = source.index("self.paster.paste(", start)
    call_open = source.index("(", call)
    call_end = matching_delimiter(source, call_open, "(", ")")
    completion_open = source.index("{", call_end)
    completion_end = matching_delimiter(source, completion_open, "{", "}")
    handoff = source[start:completion_end + 1]
    target = ("let target = HarnessRunningApplication(processIdentifier: 4242)"
              if "target?." in handoff else "let target: pid_t = 4242")
    return r'''
@MainActor final class HarnessIsland {
    func message(_ value: String) {}
}
@MainActor final class HarnessController {
    let paster: ClipboardPaster
    let island = HarnessIsland()
    var lastTranscript = ""
    var pasteGeneration = 1
    var enabled = true
    var suspended = false
    var activity = ""
    init(_ paster: ClipboardPaster) { self.paster = paster }
    func deliver(_ text: String) {
''' + target + "\n        let pasteToken = 1\n" + handoff + "\n    }\n}\n"


@pytest.fixture(scope="session")
def clipboard_harness(tmp_path_factory):
    root = tmp_path_factory.mktemp("clipboard-native-tests")
    source = (SOURCE_ROOT / "ClipboardPaster.swift").read_text()
    # Replace external boundaries only. The production control flow is compiled.
    source = re.sub(r"\bNSPasteboard\b", "HarnessBoard", source)
    source = source.replace("NSWorkspace.", "HarnessWorkspace.")
    source = source.replace("AXIsProcessTrusted()", "harnessTrusted()")
    source = source.replace("DispatchQueue.main", "HarnessClock.shared")
    source = re.sub(r"\bCGEvent\b", "HarnessEvent", source)
    source = re.sub(r"\bNSRunningApplication\b", "HarnessRunningApplication", source)
    instrumented = root / "ClipboardPaster.swift"
    instrumented.write_text(source)
    harness = root / "Harness.swift"
    harness.write_text(HARNESS.replace("// CONTROLLER_SEAM", controller_handoff_source()))
    executable = root / "clipboard-tests"
    result = subprocess.run(
        ["swiftc", "-swift-version", "5", str(instrumented), str(harness), "-o", str(executable)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def run_scenario(executable, scenario):
    result = subprocess.run([str(executable), scenario], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_native_named_pasteboard_ownership_counts(clipboard_harness):
    result = run_scenario(clipboard_harness, "native-counts")
    print("Named pasteboard counts:", json.dumps(result, sort_keys=True))
    assert result["wrote"] is True
    assert result["readBack"] == "native write"
    assert result["cleared"] == result["afterWrite"] == result["afterRead"]


@pytest.mark.parametrize("scenario", ["ownership-during-write", "ownership-before-paste", "write-failure-with-new-owner"])
def test_new_clipboard_owner_is_never_adopted_or_overwritten(clipboard_harness, scenario):
    result = run_scenario(clipboard_harness, scenario)
    assert result["clipboard"] == "external clipboard", "Paster restored over another owner's clipboard"
    assert result["events"] == [], "Paster sent a paste after losing clipboard ownership"
    assert result["completions"] and not any(item["success"] for item in result["completions"])


def test_ownership_is_checked_again_before_restore(clipboard_harness):
    result = run_scenario(clipboard_harness, "ownership-before-restore")
    assert result["clipboard"] == "external clipboard"


def test_both_paste_events_target_captured_pid(clipboard_harness):
    result = run_scenario(clipboard_harness, "targeted-events")
    assert result["events"] == [{"down": True, "pid": 4242}, {"down": False, "pid": 4242}], "Paste used a global or wrong destination"


def test_cancel_before_keydown_does_not_complete_old_operation(clipboard_harness):
    result = run_scenario(clipboard_harness, "cancel-before-down")
    assert len(result["events"]) == 2, "Cancelled operation sent delayed key events"
    successful = [item["operation"] for item in result["completions"] if item["success"]]
    assert successful == ["replacement"]


def test_cancel_after_keydown_invalidates_old_keyup_and_success(clipboard_harness):
    result = run_scenario(clipboard_harness, "cancel-after-down")
    assert result["staleEvents"] == 0, "Cancelled key-up callback sent a stale event"
    assert result["staleSuccess"] is False, "Cancelled operation reported success during replacement"
    assert [item["operation"] for item in result["completions"] if item["success"]] == ["replacement"]


@pytest.mark.parametrize("scenario", ["write-failure", "provider-failure", "event-creation-failure", "unreadable-items", "access-denied"])
def test_clipboard_errors_return_failure_without_pasting(clipboard_harness, scenario):
    result = run_scenario(clipboard_harness, scenario)
    assert result["events"] == []
    assert result["clipboard"] == "original clipboard"
    assert result["completions"] and all(not item["success"] for item in result["completions"])


@pytest.mark.parametrize("scenario", ["retained-write", "retained-provider", "retained-event"])
def test_controller_preserves_manual_copy_transcript_after_paste_errors(clipboard_harness, scenario):
    result = run_scenario(clipboard_harness, scenario)
    assert result["retainedTranscript"] == "manual copy transcript"
    assert result["events"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
