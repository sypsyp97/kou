import Foundation

public struct HotkeyGate {
    public private(set) var chordDown = false
    private var lastTrigger = -Double.infinity

    public init() {}

    public mutating func update(fn: Bool, control: Bool, command: Bool,
                                option: Bool, shift: Bool, time: Double) -> Bool {
        let down = fn && control && !command && !option && !shift
        defer { chordDown = down }
        guard down, !chordDown, time - lastTrigger >= 0.4 else { return false }
        lastTrigger = time
        return true
    }

    public mutating func resetChord() { chordDown = false }
}

public struct CaptureSession {
    public enum Phase: Equatable {
        case idle, recording(UUID), finishing(UUID), transcribing(UUID)
    }
    public enum FinishDecision: Equatable {
        case ignore, transcribe, tooShort, failed
    }
    public private(set) var phase: Phase = .idle
    public init() {}

    @discardableResult public mutating func begin() -> UUID {
        let id = UUID()
        phase = .recording(id)
        return id
    }

    public mutating func stop() -> UUID? {
        guard case .recording(let id) = phase else { return nil }
        phase = .finishing(id)
        return id
    }

    public mutating func recorderFinished(id: UUID, succeeded: Bool,
                                          byteCount: Int) -> FinishDecision {
        guard phase == .recording(id) || phase == .finishing(id) else { return .ignore }
        guard succeeded else { phase = .idle; return .failed }
        guard byteCount >= 8000 else { phase = .idle; return .tooShort }
        phase = .transcribing(id)
        return .transcribe
    }

    public mutating func transcriptionFinished(id: UUID) -> Bool {
        guard phase == .transcribing(id) else { return false }
        phase = .idle
        return true
    }

    public mutating func cancel() { phase = .idle }
}
