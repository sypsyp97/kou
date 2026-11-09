import XCTest
import KouCore

final class HotkeyGateTests: XCTestCase {
    func testOnlyExactFnControlCombinationTriggers() {
        for modifiers in 0..<32 {
            var gate = HotkeyGate()
            XCTAssertFalse(gate.chordDown)
            let triggered = gate.update(
                fn: modifiers & 1 != 0,
                control: modifiers & 2 != 0,
                command: modifiers & 4 != 0,
                option: modifiers & 8 != 0,
                shift: modifiers & 16 != 0,
                time: 0
            )
            XCTAssertEqual(triggered, modifiers == 3, "Modifier mask: \(modifiers)")
            XCTAssertEqual(gate.chordDown, modifiers == 3, "Modifier mask: \(modifiers)")
        }
    }

    func testHeldChordNeverRepeatsAndReleaseNeverTriggers() {
        var gate = HotkeyGate()
        XCTAssertTrue(update(&gate, time: 0))
        for time in [0.1, 0.4, 1.0, 100.0] {
            XCTAssertFalse(update(&gate, time: time))
            XCTAssertTrue(gate.chordDown)
        }
        XCTAssertFalse(update(&gate, fn: false, time: 101))
        XCTAssertFalse(gate.chordDown)
        XCTAssertFalse(update(&gate, fn: false, control: false, time: 102))
        XCTAssertTrue(update(&gate, time: 103))
    }

    func testEitherRequiredModifierCanReleaseChord() {
        for releaseFn in [true, false] {
            var gate = HotkeyGate()
            XCTAssertTrue(update(&gate, time: 0))
            XCTAssertFalse(update(&gate, fn: !releaseFn, control: releaseFn, time: 0.1))
            XCTAssertFalse(gate.chordDown)
            XCTAssertTrue(update(&gate, time: 0.4))
        }
    }

    func testDebounceRejectsBeforeBoundaryAndAcceptsAtBoundary() {
        var gate = HotkeyGate()
        XCTAssertTrue(update(&gate, time: 0))
        XCTAssertFalse(update(&gate, fn: false, time: 0.1))
        XCTAssertFalse(update(&gate, time: 0.399999))
        XCTAssertTrue(gate.chordDown)
        XCTAssertFalse(update(&gate, fn: false, time: 0.3999995))
        XCTAssertTrue(update(&gate, time: 0.4))
    }

    func testRejectedBounceStillTracksChordAndDoesNotMoveAcceptedTimestamp() {
        var gate = HotkeyGate()
        XCTAssertTrue(update(&gate, time: 0))
        XCTAssertFalse(update(&gate, control: false, time: 0.1))
        XCTAssertFalse(update(&gate, time: 0.2))
        XCTAssertTrue(gate.chordDown)
        XCTAssertFalse(update(&gate, time: 0.5))
        XCTAssertFalse(update(&gate, control: false, time: 0.6))
        XCTAssertTrue(update(&gate, time: 0.61))
    }

    func testProhibitedModifierReleasesExactChord() {
        for prohibited in 0..<3 {
            var gate = HotkeyGate()
            XCTAssertTrue(update(&gate, time: 0))
            XCTAssertFalse(gate.update(
                fn: true, control: true,
                command: prohibited == 0,
                option: prohibited == 1,
                shift: prohibited == 2,
                time: 0.5
            ))
            XCTAssertFalse(gate.chordDown)
            XCTAssertTrue(update(&gate, time: 0.6))
        }
    }

    func testResetReleasesChordWithoutBypassingDebounce() {
        var gate = HotkeyGate()
        XCTAssertTrue(update(&gate, time: 0))
        gate.resetChord()
        XCTAssertFalse(gate.chordDown)
        XCTAssertFalse(update(&gate, time: 0.2))
        XCTAssertTrue(gate.chordDown)
        XCTAssertFalse(update(&gate, time: 0.5))
        gate.resetChord()
        XCTAssertFalse(gate.chordDown)
        XCTAssertTrue(update(&gate, time: 0.6))
    }

    func testResetBeforeFirstPressKeepsFirstPressEligible() {
        var gate = HotkeyGate()
        gate.resetChord()
        gate.resetChord()
        XCTAssertFalse(gate.chordDown)
        XCTAssertTrue(update(&gate, time: 0))
    }

    private func update(
        _ gate: inout HotkeyGate,
        fn: Bool = true,
        control: Bool = true,
        time: TimeInterval
    ) -> Bool {
        gate.update(fn: fn, control: control, command: false, option: false, shift: false, time: time)
    }
}
