import Foundation
import XCTest
import KouCore

final class CaptureSessionTests: XCTestCase {
    func testInitialSessionIsIdleAndIgnoresUnsolicitedCallbacks() {
        var session = CaptureSession()
        assertPhase(session, .idle)
        XCTAssertNil(session.stop())
        let unknown = UUID()
        if case .ignore = session.recorderFinished(id: unknown, succeeded: true, byteCount: 8000) {} else {
            XCTFail("An idle session must ignore recorder callbacks")
        }
        XCTAssertFalse(session.transcriptionFinished(id: unknown))
        assertPhase(session, .idle)
    }

    func testStopOnlyTransitionsRecordingOnce() {
        var session = CaptureSession()
        let id = session.begin()
        assertPhase(session, .recording(id))
        XCTAssertEqual(session.stop(), id)
        assertPhase(session, .finishing(id))
        XCTAssertNil(session.stop())
        assertPhase(session, .finishing(id))
        if case .transcribe = session.recorderFinished(id: id, succeeded: true, byteCount: 8000) {} else {
            XCTFail("A completed recording must transcribe")
        }
        assertPhase(session, .transcribing(id))
        XCTAssertNil(session.stop())
        assertPhase(session, .transcribing(id))
        XCTAssertTrue(session.transcriptionFinished(id: id))
        assertPhase(session, .idle)
        XCTAssertNil(session.stop())
    }

    func testRecorderCanFinishWithoutExplicitStop() {
        var session = CaptureSession()
        let id = session.begin()
        if case .transcribe = session.recorderFinished(id: id, succeeded: true, byteCount: 8000) {} else {
            XCTFail("A recorder may finish while recording")
        }
        assertPhase(session, .transcribing(id))
        XCTAssertTrue(session.transcriptionFinished(id: id))
        assertPhase(session, .idle)
    }

    func testSuccessfulRecordingSizeBoundaryInRecordingAndFinishingPhases() {
        for stopFirst in [false, true] {
            for bytes in [0, 7999, 8000, 8001] {
                var session = CaptureSession()
                let id = session.begin()
                if stopFirst { XCTAssertEqual(session.stop(), id) }
                let decision = session.recorderFinished(id: id, succeeded: true, byteCount: bytes)
                if bytes < 8000 {
                    if case .tooShort = decision {} else {
                        XCTFail("A successful recording of \(bytes) bytes must be too short")
                    }
                    assertPhase(session, .idle)
                    XCTAssertFalse(session.transcriptionFinished(id: id))
                } else {
                    if case .transcribe = decision {} else {
                        XCTFail("A successful recording of \(bytes) bytes must transcribe")
                    }
                    assertPhase(session, .transcribing(id))
                }
            }
        }
    }

    func testRecorderFailureWinsOverByteCountAndReturnsIdle() {
        for stopFirst in [false, true] {
            for bytes in [0, 7999, 8000, 16000] {
                var session = CaptureSession()
                let id = session.begin()
                if stopFirst { XCTAssertEqual(session.stop(), id) }
                if case .failed = session.recorderFinished(id: id, succeeded: false, byteCount: bytes) {} else {
                    XCTFail("A failed recorder must report failure at \(bytes) bytes")
                }
                assertPhase(session, .idle)
                XCTAssertFalse(session.transcriptionFinished(id: id))
                if case .ignore = session.recorderFinished(id: id, succeeded: true, byteCount: 8000) {} else {
                    XCTFail("A failed round cannot be revived by a late recorder callback")
                }
            }
        }
    }

    func testWrongIDsNeverChangeCurrentPhase() {
        var session = CaptureSession()
        let id = session.begin()
        let wrongID = UUID()
        for stage in 0..<3 {
            let expected: ExpectedPhase
            if stage == 0 {
                expected = .recording(id)
            } else if stage == 1 {
                XCTAssertEqual(session.stop(), id)
                expected = .finishing(id)
            } else {
                if case .transcribe = session.recorderFinished(id: id, succeeded: true, byteCount: 8000) {} else {
                    XCTFail("Current recorder completion must transcribe")
                }
                expected = .transcribing(id)
            }
            for succeeded in [false, true] {
                if case .ignore = session.recorderFinished(id: wrongID, succeeded: succeeded, byteCount: 8000) {} else {
                    XCTFail("A foreign recorder callback must be ignored")
                }
                assertPhase(session, expected)
            }
            XCTAssertFalse(session.transcriptionFinished(id: wrongID))
            assertPhase(session, expected)
        }
    }

    func testTranscriptionCannotFinishBeforeRecorderCompletion() {
        var session = CaptureSession()
        let id = session.begin()
        XCTAssertFalse(session.transcriptionFinished(id: id))
        assertPhase(session, .recording(id))
        XCTAssertEqual(session.stop(), id)
        XCTAssertFalse(session.transcriptionFinished(id: id))
        assertPhase(session, .finishing(id))
    }

    func testDuplicateRecorderAndTranscriptionCallbacksAreIgnored() {
        var session = CaptureSession()
        let id = session.begin()
        if case .transcribe = session.recorderFinished(id: id, succeeded: true, byteCount: 8000) {} else {
            XCTFail("First completion must transcribe")
        }
        for succeeded in [false, true] {
            if case .ignore = session.recorderFinished(id: id, succeeded: succeeded, byteCount: 8000) {} else {
                XCTFail("A duplicate recorder callback must be ignored")
            }
            assertPhase(session, .transcribing(id))
        }
        XCTAssertTrue(session.transcriptionFinished(id: id))
        XCTAssertFalse(session.transcriptionFinished(id: id))
        if case .ignore = session.recorderFinished(id: id, succeeded: true, byteCount: 8000) {} else {
            XCTFail("A completed round must remain complete")
        }
        assertPhase(session, .idle)
    }

    func testBeginSupersedesEveryActivePhaseAndRejectsOldCallbacks() {
        for stage in 0..<3 {
            var session = CaptureSession()
            let oldID = session.begin()
            if stage >= 1 { XCTAssertEqual(session.stop(), oldID) }
            if stage == 2 {
                if case .transcribe = session.recorderFinished(id: oldID, succeeded: true, byteCount: 8000) {} else {
                    XCTFail("Old round must enter transcription")
                }
            }
            let newID = session.begin()
            XCTAssertNotEqual(newID, oldID)
            assertPhase(session, .recording(newID))
            XCTAssertFalse(session.transcriptionFinished(id: oldID))
            for succeeded in [false, true] {
                if case .ignore = session.recorderFinished(id: oldID, succeeded: succeeded, byteCount: 8000) {} else {
                    XCTFail("A superseded recorder callback must be ignored")
                }
                assertPhase(session, .recording(newID))
            }
            XCTAssertEqual(session.stop(), newID)
            if case .transcribe = session.recorderFinished(id: newID, succeeded: true, byteCount: 8000) {} else {
                XCTFail("New round must remain usable")
            }
            XCTAssertFalse(session.transcriptionFinished(id: oldID))
            assertPhase(session, .transcribing(newID))
            XCTAssertTrue(session.transcriptionFinished(id: newID))
            assertPhase(session, .idle)
        }
    }

    func testCancelInvalidatesCallbacksInEveryPhaseAndAllowsFreshRound() {
        for stage in 0..<3 {
            var session = CaptureSession()
            let oldID = session.begin()
            if stage >= 1 { XCTAssertEqual(session.stop(), oldID) }
            if stage == 2 {
                if case .transcribe = session.recorderFinished(id: oldID, succeeded: true, byteCount: 8000) {} else {
                    XCTFail("Round must enter transcription before cancellation")
                }
            }
            session.cancel()
            session.cancel()
            assertPhase(session, .idle)
            XCTAssertNil(session.stop())
            XCTAssertFalse(session.transcriptionFinished(id: oldID))
            if case .ignore = session.recorderFinished(id: oldID, succeeded: true, byteCount: 8000) {} else {
                XCTFail("Cancellation must invalidate recorder callbacks")
            }
            let newID = session.begin()
            XCTAssertNotEqual(oldID, newID)
            if case .ignore = session.recorderFinished(id: oldID, succeeded: false, byteCount: 0) {} else {
                XCTFail("A cancelled callback cannot affect a fresh round")
            }
            assertPhase(session, .recording(newID))
            if case .transcribe = session.recorderFinished(id: newID, succeeded: true, byteCount: 8000) {} else {
                XCTFail("A fresh round must complete after cancellation")
            }
            XCTAssertTrue(session.transcriptionFinished(id: newID))
            assertPhase(session, .idle)
        }
    }

    func testShortRecordingCannotBeRevivedAndNextRoundWorks() {
        var session = CaptureSession()
        let oldID = session.begin()
        if case .tooShort = session.recorderFinished(id: oldID, succeeded: true, byteCount: 7999) {} else {
            XCTFail("The short round must be rejected")
        }
        let newID = session.begin()
        XCTAssertNotEqual(oldID, newID)
        if case .ignore = session.recorderFinished(id: oldID, succeeded: true, byteCount: 8000) {} else {
            XCTFail("A rejected round cannot consume the new round")
        }
        XCTAssertFalse(session.transcriptionFinished(id: oldID))
        assertPhase(session, .recording(newID))
        if case .transcribe = session.recorderFinished(id: newID, succeeded: true, byteCount: 8000) {} else {
            XCTFail("New round must transcribe")
        }
        XCTAssertTrue(session.transcriptionFinished(id: newID))
    }

    private enum ExpectedPhase {
        case idle
        case recording(UUID)
        case finishing(UUID)
        case transcribing(UUID)
    }

    private func assertPhase(
        _ session: CaptureSession,
        _ expected: ExpectedPhase,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        switch (session.phase, expected) {
        case (.idle, .idle):
            break
        case let (.recording(actual), .recording(wanted)),
             let (.finishing(actual), .finishing(wanted)),
             let (.transcribing(actual), .transcribing(wanted)):
            XCTAssertEqual(actual, wanted, file: file, line: line)
        default:
            XCTFail("Unexpected capture phase: \(session.phase)", file: file, line: line)
        }
    }
}
