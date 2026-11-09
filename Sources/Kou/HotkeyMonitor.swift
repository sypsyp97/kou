import AppKit
import CoreGraphics
import KouCore

final class HotkeyMonitor {
    private let lock = NSLock()
    private var loop: CFRunLoop?
    private var tap: CFMachPort?
    private var thread: Thread?
    private var stopped = false
    private var active = false
    private var alternateDown = false
    private var gate = HotkeyGate()
    private var lastAlternate = -Double.infinity
    var onTrigger: (() -> Void)?
    var onAvailability: ((Bool) -> Void)?

    func start() {
        guard thread == nil else { return }
        let worker = Thread { [self] in run() }
        worker.name = "Kou hotkey monitor"
        worker.qualityOfService = .userInteractive
        thread = worker
        worker.start()
    }

    func stop() {
        lock.lock()
        stopped = true
        let activeLoop = loop
        lock.unlock()
        if let activeLoop { CFRunLoopStop(activeLoop) }
    }

    func setActive(_ value: Bool) { lock.lock(); active = value; lock.unlock() }

    private func run() {
        let current = CFRunLoopGetCurrent()!
        lock.lock()
        if stopped { lock.unlock(); return }
        loop = current
        lock.unlock()
        let mask = (CGEventMask(1) << CGEventType.flagsChanged.rawValue)
            | (CGEventMask(1) << CGEventType.keyDown.rawValue)
            | (CGEventMask(1) << CGEventType.keyUp.rawValue)
        let context = Unmanaged.passUnretained(self).toOpaque()
        func createTap() -> CFMachPort? {
            CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap,
                              options: .defaultTap, eventsOfInterest: mask,
                              callback: { _, type, event, context in
                guard let context else { return Unmanaged.passUnretained(event) }
                let monitor = Unmanaged<HotkeyMonitor>.fromOpaque(context).takeUnretainedValue()
                if monitor.receive(type: type, event: event) { return nil }
                return Unmanaged.passUnretained(event)
            }, userInfo: context)
        }
        var source: CFRunLoopSource?
        func rebuild() {
            if let old = tap { CFMachPortInvalidate(old) }
            if let source { CFRunLoopRemoveSource(current, source, .commonModes) }
            tap = createTap()
            source = tap.flatMap { CFMachPortCreateRunLoopSource(kCFAllocatorDefault, $0, 0) }
            if let source { CFRunLoopAddSource(current, source, .commonModes) }
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            forgetHeldKeys()
            publishAvailability(tap != nil)
        }
        rebuild()
        let timer = CFRunLoopTimerCreateWithHandler(kCFAllocatorDefault,
                                                    CFAbsoluteTimeGetCurrent() + 1, 1, 0, 0) { [self] _ in
            if let tap, CGEvent.tapIsEnabled(tap: tap) {
                let flags = CGEventSource.flagsState(.combinedSessionState)
                if !flags.contains(.maskSecondaryFn) || !flags.contains(.maskControl) { gate.resetChord() }
            } else {
                if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
                if tap == nil || !CGEvent.tapIsEnabled(tap: tap!) { rebuild() }
                else { forgetHeldKeys(); publishAvailability(true) }
            }
        }
        CFRunLoopAddTimer(current, timer, .commonModes)
        CFRunLoopRun()
        CFRunLoopTimerInvalidate(timer)
        if let tap { CFMachPortInvalidate(tap) }
        if let source { CFRunLoopRemoveSource(current, source, .commonModes) }
        lock.lock(); loop = nil; lock.unlock()
    }

    private func receive(type: CGEventType, event: CGEvent) -> Bool {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            forgetHeldKeys()
            return false
        }
        lock.lock(); let enabled = active; lock.unlock()
        let flags = event.flags
        let now = ProcessInfo.processInfo.systemUptime
        if type == .keyUp, event.getIntegerValueField(.keyboardEventKeycode) == 49, alternateDown {
            alternateDown = false
            return true
        }
        if type == .flagsChanged {
            if gate.update(fn: flags.contains(.maskSecondaryFn), control: flags.contains(.maskControl),
                           command: flags.contains(.maskCommand), option: flags.contains(.maskAlternate),
                           shift: flags.contains(.maskShift), time: now), enabled { trigger() }
        } else if type == .keyDown, event.getIntegerValueField(.keyboardEventKeycode) == 49,
                  enabled,
                  flags.contains(.maskAlternate), flags.intersection([.maskControl, .maskCommand, .maskShift, .maskSecondaryFn]).isEmpty {
            alternateDown = true
            if event.getIntegerValueField(.keyboardEventAutorepeat) == 0, now - lastAlternate >= 0.4 {
                lastAlternate = now
                trigger()
            }
            return true
        }
        return false
    }

    /// A disabled tap misses key-up events, so release both hotkeys.
    private func forgetHeldKeys() { gate.resetChord(); alternateDown = false }

    private func trigger() { DispatchQueue.main.async { [weak self] in self?.onTrigger?() } }
    private func publishAvailability(_ available: Bool) {
        DispatchQueue.main.async { [weak self] in self?.onAvailability?(available) }
    }
}
