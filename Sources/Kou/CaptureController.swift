import AppKit
import AVFoundation
import KouCore

private final class RecorderSlot {
    let task = ManagedTask()
    let wav: URL
    let level: URL
    var sessionID: UUID?
    var armed = false
    var stream = ""

    init(paths: KouPaths) {
        let name = UUID().uuidString
        wav = paths.recordings.appendingPathComponent(name + ".wav")
        level = paths.recordings.appendingPathComponent(name + ".level")
    }

    func cleanup() {
        for url in [wav, wav.appendingPathExtension("vadtmp"), level] {
            try? FileManager.default.removeItem(at: url)
        }
    }
}

@MainActor final class CaptureController: ObservableObject {
    @Published var enabled = false { didSet { onEnabledChanged?(enabled) } }
    var onEnabledChanged: ((Bool) -> Void)?
    var onRecordingStarted: (() -> Void)?
    @Published var activity = L("听写已关闭", "Dictation is off")
    @Published var lastTranscript = ""
    @Published var prearm: Bool {
        didSet {
            UserDefaults.standard.set(prearm, forKey: "prearm")
            if prearm { armRecorder() }
            else { let slot = armed; armed = nil; slot?.task.cancel() }
        }
    }
    let runtime: RuntimeManager
    let island: IslandController
    private let paster = ClipboardPaster()
    /// Mirrors `live` in `tla/Capture.tla`. Only `sleeping` returns to `running`; `quitting` is terminal.
    private enum Lifecycle { case running, sleeping, quitting }

    private var session = CaptureSession()
    /// The recorder of the current session while `session.phase` is recording or finishing.
    private var recorder: RecorderSlot?
    private var armed: RecorderSlot?
    private var transcription: ManagedTask?
    private var armFailures = 0
    private var armRetry: DispatchWorkItem?
    private var lifecycle = Lifecycle.running
    private var suspended: Bool { lifecycle != .running }
    private var targetApplication: NSRunningApplication?
    private var pasteGeneration = UUID()

    init(runtime: RuntimeManager, island: IslandController) {
        self.runtime = runtime; self.island = island
        prearm = UserDefaults.standard.bool(forKey: "prearm")
    }

    func setEnabled(_ value: Bool) {
        if value {
            guard lifecycle != .quitting else { return }
            guard AXIsProcessTrusted(), AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
                activity = L("需要麦克风和辅助功能权限", "Microphone and Accessibility access required"); return
            }
            let legacy = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/zh-voice/hammerspoon-heartbeat")
            if let values = try? legacy.resourceValues(forKeys: [.contentModificationDateKey]),
               let changed = values.contentModificationDate, Date().timeIntervalSince(changed) < 45 {
                activity = L("另一个听写程序正在使用 fn+control，请先退出它", "Another dictation app uses fn+control. Quit it first."); return
            }
            guard runtime.ready else { activity = L("模型尚未就绪", "Model is not ready"); return }
            enabled = true; activity = L("按 fn+control 开始听写", "Press fn+control to dictate")
            UserDefaults.standard.set(true, forKey: "enabled")
            armRecorder()
        } else {
            enabled = false
            UserDefaults.standard.set(false, forKey: "enabled")
            cancelAll()
            activity = L("听写已关闭", "Dictation is off")
        }
    }

    func toggle() {
        guard enabled, !suspended else { return }
        if case .recording = session.phase { stopRecording() }
        else { startRecording() }
    }

    private func startRecording() {
        guard runtime.ready, let config = runtime.config else { island.message(L("本地模型未就绪", "Model is not ready")); return }
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            activity = L("麦克风权限已关闭", "Microphone access is off"); island.message(activity); return
        }
        // Invalidate callbacks before cancelling processes or touching their files.
        pasteGeneration = UUID(); paster.cancel()
        transcription?.cancel(); transcription = nil
        let old = recorder; recorder = nil; old?.task.cancel()
        do { try runtime.paths.ensureDirectories() } catch { runtime.paths.recordLog("Recording folder unavailable: \(error.localizedDescription)") }
        let id = session.begin()
        let slot: RecorderSlot
        if let prepared = armed, prepared.armed, prepared.task.isRunning {
            slot = prepared; armed = nil
            slot.sessionID = id
            recorder = slot
            slot.task.beginCapture()
        } else {
            let pending = armed; armed = nil; pending?.task.cancel()
            slot = RecorderSlot(paths: runtime.paths)
            slot.sessionID = id; recorder = slot
            launchRecorder(slot, mode: "record", config: config)
        }
        guard recorder === slot else { return }
        onRecordingStarted?()
        island.recording(level: slot.level)
        activity = L("正在听写 · 再按 fn+control 结束", "Listening · Press fn+control to stop")
        runtime.paths.recordLog("Recording started")
        DispatchQueue.main.asyncAfter(deadline: .now() + 125) { [weak self, weak slot] in
            guard let self, let slot, self.session.phase == .recording(id) else { return }
            self.session.cancel(); self.recorder = nil; slot.task.cancel()
            self.activity = L("录音超时", "Recording timed out"); self.island.message(self.activity)
            self.armRecorder()
        }
    }

    private func stopRecording() {
        guard let slot = recorder, let id = session.stop() else { return }
        targetApplication = NSWorkspace.shared.frontmostApplication
        island.transcribing(); activity = L("正在结束录音…", "Finishing recording…")
        slot.task.cancel()
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self, weak slot] in
            guard let self, let slot, self.session.phase == .finishing(id) else { return }
            self.recorder = nil; self.session.cancel(); slot.task.cancel()
            self.activity = L("录音收尾超时", "Recording didn’t finish in time"); self.island.message(self.activity)
            self.armRecorder()
        }
    }

    private func launchRecorder(_ slot: RecorderSlot, mode: String, config: RuntimeConfig) {
        do {
            try slot.task.start(paths: runtime.paths, executable: runtime.paths.resource("zh-voice-audio"),
                                arguments: [mode, slot.wav.path, "--level", slot.level.path, "--max-seconds", "120"],
                                environment: runtime.paths.environment(config),
                                stream: { [weak self, weak slot] text in
                guard let self, let slot, self.armed === slot else { return }
                slot.stream = String((slot.stream + text).suffix(100))
                if slot.stream.contains("ARMED") { slot.armed = true; self.armFailures = 0 }
            }) { [weak self, slot] result in
                guard let self else { slot.cleanup(); return }
                if self.armed === slot {
                    self.armed = nil; slot.cleanup()
                    if result.code != 5 { self.retryArm() }
                    return
                }
                guard self.recorder === slot, let id = slot.sessionID else { slot.cleanup(); return }
                self.recorder = nil
                // The recorder exited before stopRecording recorded the target app.
                if self.session.phase == .recording(id) { self.targetApplication = NSWorkspace.shared.frontmostApplication }
                let bytes = ((try? FileManager.default.attributesOfItem(atPath: slot.wav.path)[.size]) as? Int) ?? 0
                let decision = self.session.recorderFinished(id: id, succeeded: result.code == 0, byteCount: bytes)
                switch decision {
                case .transcribe: self.transcribe(slot, id: id)
                case .tooShort: slot.cleanup(); self.activity = L("按得太短了", "Too short"); self.island.message(self.activity)
                case .failed:
                    slot.cleanup(); self.activity = L("录音失败", "Recording failed"); self.island.message(self.activity)
                    self.runtime.paths.recordLog("Recorder failed: \(result.code); \(result.error.prefix(500))")
                case .ignore: slot.cleanup()
                }
                self.armRecorder()
            }
        } catch {
            if recorder === slot { recorder = nil; session.cancel(); activity = L("录音器启动失败", "Recorder didn’t start"); island.message(activity) }
            if armed === slot { armed = nil; retryArm() }
            slot.cleanup(); runtime.paths.recordLog("Recorder launch failed: \(error.localizedDescription)")
        }
    }

    private func transcribe(_ slot: RecorderSlot, id: UUID, retry: Bool = false, deadline: Date? = nil) {
        guard let config = runtime.config else { slot.cleanup(); session.cancel(); return }
        let end = deadline ?? Date().addingTimeInterval(240)
        island.transcribing(); activity = L("转写中…", "Transcribing…")
        let task = ManagedTask()
        transcription = task
        let target = targetApplication
        let pasteToken = pasteGeneration
        do {
            try task.start(paths: runtime.paths, executable: URL(fileURLWithPath: "/bin/bash"),
                           arguments: [runtime.paths.resource("zh-voice").path, "--from-recorder", slot.wav.path],
                           environment: runtime.paths.environment(config)) { [weak self, weak task] result in
                guard let self, let task, self.transcription === task,
                      self.session.phase == .transcribing(id) else { slot.cleanup(); return }
                self.transcription = nil
                if result.code == 75, !retry, Date() < end {
                    self.activity = L("服务恢复中，录音已保留…", "Restarting the service. Your recording is kept…")
                    self.runtime.recover()
                    self.waitForRecovery(slot, id: id, deadline: end)
                    return
                }
                slot.cleanup()
                guard self.session.transcriptionFinished(id: id) else { return }
                let text = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
                guard result.code == 0, !text.isEmpty else {
                    self.activity = result.code == 0 ? L("没听清，再说一次", "Didn’t catch that. Try again.") : L("转写失败", "Transcription failed")
                    self.island.message(self.activity)
                    self.runtime.paths.recordLog("Transcription ended: code=\(result.code), empty=\(text.isEmpty)")
                    return
                }
                self.lastTranscript = text
                self.paster.paste(text, targetPID: target?.processIdentifier ?? 0, valid: { [weak self] in
                    target?.isTerminated == false && self?.pasteGeneration == pasteToken
                        && self?.enabled == true && self?.suspended == false
                }) { [weak self] _, message in
                    guard let self, self.pasteGeneration == pasteToken else { return }
                    self.activity = message; self.island.message(message)
                }
            }
        } catch {
            transcription = nil; session.cancel(); slot.cleanup()
            activity = L("无法启动转写", "Couldn’t start transcription"); island.message(activity)
        }
        if !retry {
            DispatchQueue.main.asyncAfter(deadline: .now() + max(0, end.timeIntervalSinceNow)) { [weak self] in
                guard let self, self.session.phase == .transcribing(id) else { return }
                self.session.cancel(); self.transcription?.cancel(); self.transcription = nil
                slot.cleanup()
                self.activity = L("转写超时", "Transcription timed out"); self.island.message(self.activity)
            }
        }
    }

    private func waitForRecovery(_ slot: RecorderSlot, id: UUID, deadline: Date) {
        guard session.phase == .transcribing(id), enabled, !suspended else { slot.cleanup(); return }
        guard Date() < deadline else { slot.cleanup(); session.cancel(); activity = L("转写超时", "Transcription timed out"); island.message(activity); return }
        if runtime.ready { transcribe(slot, id: id, retry: true, deadline: deadline); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            guard let self else { slot.cleanup(); return }
            self.waitForRecovery(slot, id: id, deadline: deadline)
        }
    }

    func armRecorder() {
        guard enabled, !suspended, prearm, recorder == nil, armed == nil,
              AVCaptureDevice.authorizationStatus(for: .audio) == .authorized,
              let config = runtime.config else { return }
        armRetry?.cancel(); armRetry = nil
        let slot = RecorderSlot(paths: runtime.paths)
        armed = slot
        launchRecorder(slot, mode: "arm", config: config)
    }

    private func retryArm() {
        guard enabled, !suspended, prearm else { return }
        armFailures += 1
        let delay = armFailures == 1 ? 0 : min(8, 0.5 * pow(2, Double(min(armFailures - 2, 5))))
        let work = DispatchWorkItem { [weak self] in self?.armRecorder() }
        armRetry = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    func sleep() {
        guard lifecycle == .running else { return }
        lifecycle = .sleeping; cancelAll(); activity = L("Mac 睡眠中", "Mac is asleep")
    }

    /// Both didWake and screensDidWake call this; a display wake without system sleep changes nothing.
    func wake() {
        guard lifecycle == .sleeping else { return }
        lifecycle = .running
        if enabled { activity = L("按 fn+control 开始听写", "Press fn+control to dictate"); armRecorder() }
    }

    func shutdown() { lifecycle = .quitting; enabled = false; cancelAll() }

    func cancelAll() {
        session.cancel(); pasteGeneration = UUID(); paster.cancel()
        armRetry?.cancel(); armRetry = nil
        let slots = [recorder, armed]
        recorder = nil; armed = nil
        for slot in slots { slot?.task.cancel() }
        transcription?.cancel(); transcription = nil
        island.hide(immediately: true)
    }
}
