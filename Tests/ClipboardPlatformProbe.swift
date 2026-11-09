import AppKit
import CoreGraphics
import Foundation

func L(_ chinese: String, _ english: String) -> String {
    Locale.preferredLanguages.first?.hasPrefix("zh") == true ? chinese : english
}

@MainActor final class NamedPasteView: NSTextView {
    let isolatedBoard: NSPasteboard
    private let probeStorage: NSTextStorage
    var pasteCalls = 0
    var pasteDiagnostic: [String: Any] = [:]

    init(board: NSPasteboard) {
        isolatedBoard = board
        let storage = NSTextStorage()
        let manager = NSLayoutManager()
        let container = NSTextContainer(size: NSSize(width: 520, height: CGFloat.greatestFiniteMagnitude))
        storage.addLayoutManager(manager)
        manager.addTextContainer(container)
        probeStorage = storage
        super.init(frame: NSRect(x: 20, y: 20, width: 520, height: 160), textContainer: container)
        isRichText = false
        isEditable = true
        isSelectable = true
        isAutomaticQuoteSubstitutionEnabled = false
        isAutomaticDashSubstitutionEnabled = false
        font = .systemFont(ofSize: 18)
        setSelectedRange(NSRange(location: 0, length: 0))
    }

    required init?(coder: NSCoder) { fatalError("Not used by the probe") }

    override func paste(_ sender: Any?) {
        pasteCalls += 1
        let range = selectedRange()
        pasteDiagnostic = [
            "boardChangeCount": isolatedBoard.changeCount,
            "hasTextStorage": textStorage != nil, "hasLayoutManager": layoutManager != nil,
            "hasTextContainer": textContainer != nil,
            "selectedLocation": range.location, "selectedLength": range.length,
            "beforeInsertion": string
        ]
        guard let text = isolatedBoard.string(forType: .string) else {
            pasteDiagnostic["boardStringAvailable"] = false
            return
        }
        pasteDiagnostic["boardStringAvailable"] = true
        pasteDiagnostic["boardString"] = text
        insertText(text, replacementRange: selectedRange())
        pasteDiagnostic["afterInsertion"] = string
    }

    override func validateUserInterfaceItem(_ item: NSValidatedUserInterfaceItem) -> Bool {
        item.action == #selector(NSText.paste(_:))
    }
}

@MainActor final class ClipboardPlatformProbe: NSObject, NSApplicationDelegate {
    private let transcript = "声音输入验证 — cafe\u{301} 👩🏽‍💻\n第二行：π = 3.14159"
    private let board = NSPasteboard(name: NSPasteboard.Name("KouPlatformProbe." + UUID().uuidString))
    private let resultURL: URL
    private var paster: ClipboardPaster?
    private var window: NSWindow?
    private var textView: NamedPasteView?
    private var eventMonitor: Any?
    private var original: [[String: Data]] = []
    private var active = true
    private var finished = false
    private var keyDownEvents = 0
    private var keyUpEvents = 0

    init(resultURL: URL) { self.resultURL = resultURL }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard AXIsProcessTrusted() else {
            finish(status: "skipped", reason: "accessibility-permission-unavailable", code: 77)
            return
        }
        do {
            let first = NSPasteboardItem()
            let second = NSPasteboardItem()
            guard first.setString("Original — 备份", forType: .string),
                  first.setData(Data([0, 255, 17, 10]), forType: NSPasteboard.PasteboardType("io.kou.probe.binary")),
                  second.setString("Second original item", forType: .string),
                  second.setData(Data("{\\rtf1 original}".utf8), forType: .rtf) else {
                finish(status: "failed", reason: "cannot-prepare-isolated-board", code: 1); return
            }
            board.clearContents()
            guard board.writeObjects([first, second]) else {
                finish(status: "failed", reason: "cannot-seed-isolated-board", code: 1); return
            }
            original = try snapshot()
            guard original.count == 2,
                  original[0]["io.kou.probe.binary"] == Data([0, 255, 17, 10]),
                  original[1][NSPasteboard.PasteboardType.rtf.rawValue] != nil else {
                finish(status: "failed", reason: "isolated-board-seed-not-multitype", code: 1); return
            }
        } catch {
            finish(status: "failed", reason: "cannot-read-isolated-board", code: 1); return
        }

        let menu = NSMenu()
        menu.autoenablesItems = false
        let edit = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "Edit")
        editMenu.autoenablesItems = false
        let paste = NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        paste.keyEquivalentModifierMask = .command
        editMenu.addItem(paste)
        edit.submenu = editMenu
        menu.addItem(edit)
        NSApp.mainMenu = menu

        let view = NamedPasteView(board: board)
        guard view.textStorage != nil, view.layoutManager != nil, view.textContainer != nil else {
            finish(status: "failed", reason: "fixture-missing-text-system", code: 1); return
        }
        let ownWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 210),
                                 styleMask: [.titled], backing: .buffered, defer: false)
        ownWindow.title = "Kou isolated paste acceptance probe"
        ownWindow.isReleasedWhenClosed = false
        ownWindow.contentView?.addSubview(view)
        ownWindow.center()
        window = ownWindow
        textView = view
        eventMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .keyUp]) { [weak self] event in
            if event.keyCode == 9 && event.modifierFlags.contains(.command) {
                if event.type == .keyDown { self?.keyDownEvents += 1 }
                if event.type == .keyUp { self?.keyUpEvents += 1 }
            }
            return event
        }
        ownWindow.makeKeyAndOrderFront(nil)
        ownWindow.makeFirstResponder(view)
        NSApp.activate(ignoringOtherApps: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { [weak self] in
            self?.finish(status: "failed", reason: "probe-timeout", code: 1)
        }
        waitForOwnFocus(until: Date().addingTimeInterval(2))
    }

    private func waitForOwnFocus(until deadline: Date) {
        guard active else { return }
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == getpid(),
              window?.isKeyWindow == true, window?.firstResponder === textView else {
            if Date() >= deadline {
                finish(status: "failed", reason: "own-window-did-not-acquire-focus", code: 1)
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.waitForOwnFocus(until: deadline) }
            }
            return
        }
        let worker = ClipboardPaster(board: board)
        paster = worker
        worker.paste(transcript, targetPID: getpid(), valid: { [weak self] in
            self?.active == true && self?.window?.isKeyWindow == true
        }) { [weak self] success, message in
            guard let self else { return }
            guard success else {
                self.finish(status: "failed", reason: "production-paster-rejected: " + message, code: 1); return
            }
            // The production restorer runs 1.2 seconds after key-down.
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.45) { [weak self] in self?.verify() }
        }
    }

    private func snapshot() throws -> [[String: Data]] {
        var result: [[String: Data]] = []
        for item in board.pasteboardItems ?? [] {
            var values: [String: Data] = [:]
            for type in item.types {
                guard let data = item.data(forType: type) else {
                    throw NSError(domain: "KouPlatformProbe", code: 1)
                }
                values[type.rawValue] = data
            }
            result.append(values)
        }
        return result
    }

    private func verify() {
        let matched = Array((textView?.string ?? "").utf8) == Array(transcript.utf8)
        let restored = (try? snapshot()) == original
        let onePaste = textView?.pasteCalls == 1
        let keyPair = keyDownEvents == 1 && keyUpEvents == 1
        let accepted = matched && restored && onePaste && keyPair
        finish(status: accepted ? "passed" : "failed", reason: accepted ? "accepted" : "paste-or-restore-mismatch",
               code: accepted ? 0 : 1, details: [
                "transcriptMatched": matched, "clipboardRestored": restored,
                "pasteCalls": textView?.pasteCalls ?? 0,
                "keyDownEvents": keyDownEvents, "keyUpEvents": keyUpEvents,
                "received": textView?.string ?? "",
                "pasteDiagnostic": textView?.pasteDiagnostic ?? [:]
               ])
    }

    private func finish(status: String, reason: String, code: Int32, details: [String: Any] = [:]) {
        guard !finished else { return }
        finished = true
        active = false
        paster?.cancel()
        if let monitor = eventMonitor { NSEvent.removeMonitor(monitor) }
        window?.close()
        board.releaseGlobally()
        var result = details
        result["status"] = status
        result["reason"] = reason
        result["targetPID"] = Int(getpid())
        result["namedPasteboardOnly"] = true
        do {
            let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
            try data.write(to: resultURL, options: .atomic)
            FileHandle.standardOutput.write(data + Data([10]))
            exit(code)
        } catch {
            FileHandle.standardError.write(Data(("Cannot write probe result: \(error)\n").utf8))
            exit(74)
        }
    }
}

@main struct ClipboardPlatformProbeMain {
    @MainActor static func main() {
        let args = CommandLine.arguments
        guard args.count == 3, args[1] == "--result" else {
            FileHandle.standardError.write(Data("Usage: Kou --result /absolute/result.json\n".utf8)); exit(64)
        }
        let resultURL = URL(fileURLWithPath: args[2])
        guard !FileManager.default.fileExists(atPath: resultURL.path) else {
            FileHandle.standardError.write(Data("Refusing to replace an existing probe result\n".utf8)); exit(73)
        }
        let application = NSApplication.shared
        let delegate = ClipboardPlatformProbe(resultURL: resultURL)
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { application.run() }
    }
}
