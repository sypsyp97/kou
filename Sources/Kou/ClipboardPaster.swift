import AppKit
import CoreGraphics

@MainActor final class ClipboardPaster {
    private struct Snapshot {
        let items: [NSPasteboardItem]
        let changeCount: Int
        let marker: Data?
    }
    private let board: NSPasteboard
    private let markerType = NSPasteboard.PasteboardType("io.github.sypsyp97.Kou.paste-token")
    private var saved: Snapshot?
    private var generation = 0
    private var pendingKeyUp: (event: CGEvent, target: NSRunningApplication)?

    init(board: NSPasteboard = .general) { self.board = board }

    func paste(_ text: String, targetPID: pid_t, valid: @escaping () -> Bool,
               completion: @escaping (Bool, String) -> Void) {
        cancel()
        let token = generation
        guard let target = NSRunningApplication(processIdentifier: targetPID), !target.isTerminated,
              valid(), AXIsProcessTrusted(), NSWorkspace.shared.frontmostApplication?.processIdentifier == targetPID else {
            completion(false, L("焦点已变化，转写已保留", "Focus changed. Text kept.")); return
        }
        let board = self.board
        if #available(macOS 15.4, *), board.accessBehavior == .alwaysDeny {
            completion(false, L("剪贴板访问被拒绝，转写已保留", "Clipboard access denied. Text kept.")); return
        }
        var previous: [NSPasteboardItem] = []
        let before = board.changeCount
        let originals = board.pasteboardItems
        if originals == nil, let types = board.types, !types.isEmpty {
            completion(false, L("无法读取原剪贴板，转写已保留", "Couldn’t read the clipboard. Text kept.")); return
        }
        for item in originals ?? [] {
            let copy = NSPasteboardItem()
            for type in item.types {
                guard let data = item.data(forType: type), copy.setData(data, forType: type) else {
                    completion(false, L("无法保存原剪贴板，转写已保留", "Couldn’t save the clipboard. Text kept.")); return
                }
            }
            previous.append(copy)
        }
        guard board.changeCount == before else { completion(false, L("剪贴板被占用，转写已保留", "Clipboard is busy. Text kept.")); return }
        let item = NSPasteboardItem()
        let marker = Data(UUID().uuidString.utf8)
        guard item.setString(text, forType: .string),
              item.setData(Data(), forType: NSPasteboard.PasteboardType("org.nspasteboard.TransientType")),
              item.setData(marker, forType: markerType) else {
            completion(false, L("无法准备剪贴板，转写已保留", "Couldn’t prepare the clipboard. Text kept.")); return
        }
        let mine = board.clearContents()
        saved = Snapshot(items: previous, changeCount: mine, marker: nil)
        guard board.changeCount == mine else {
            saved = nil
            completion(false, L("剪贴板被占用，转写已保留", "Clipboard is busy. Text kept.")); return
        }
        guard board.writeObjects([item]) else {
            restore()
            completion(false, L("剪贴板写入失败，转写已保留", "Couldn’t write to the clipboard. Text kept.")); return
        }
        let snapshot = Snapshot(items: previous, changeCount: mine, marker: marker)
        saved = snapshot
        guard owns(snapshot) else {
            saved = nil
            completion(false, L("剪贴板被占用，转写已保留", "Clipboard is busy. Text kept.")); return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
            guard let self, self.generation == token else { return }
            guard valid(), AXIsProcessTrusted(), !target.isTerminated, self.owns(snapshot),
                  NSWorkspace.shared.frontmostApplication?.processIdentifier == targetPID else {
                self.restore()
                completion(false, L("粘贴已取消，转写已保留", "Paste canceled. Text kept.")); return
            }
            guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 9, keyDown: true),
                  let up = CGEvent(keyboardEventSource: nil, virtualKey: 9, keyDown: false) else {
                self.restore(); completion(false, L("无法发送粘贴按键", "Couldn’t send the paste keystroke")); return
            }
            down.flags = .maskCommand
            up.flags = .maskCommand
            down.postToPid(targetPID)
            self.pendingKeyUp = (up, target)
            // The interval uses the run loop; it must not block island animation.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.02) { [weak self] in
                guard let self, self.generation == token else { return }
                if !target.isTerminated { up.postToPid(targetPID) }
                self.pendingKeyUp = nil
                if valid() { completion(true, L("✓ 已粘贴", "✓ Pasted")) }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
                guard let self, self.generation == token else { return }
                self.restore()
            }
        }
    }

    func restore() {
        guard let snapshot = saved else { return }
        saved = nil
        guard owns(snapshot) else { return }
        let cleared = board.clearContents()
        guard board.changeCount == cleared else { return }
        if !snapshot.items.isEmpty { board.writeObjects(snapshot.items) }
    }

    private func owns(_ snapshot: Snapshot) -> Bool {
        guard board.changeCount == snapshot.changeCount else { return false }
        guard snapshot.marker == nil || board.data(forType: markerType) == snapshot.marker else { return false }
        return board.changeCount == snapshot.changeCount
    }

    func cancel() {
        generation += 1
        if let pending = pendingKeyUp, !pending.target.isTerminated {
            pending.event.postToPid(pending.target.processIdentifier)
        }
        pendingKeyUp = nil
        restore()
    }
}
