import AVFoundation
import CoreML
import Foundation

// The installed binary is named `zh-voice-audio`. Subcommands:
//   record   native recording with AVAudioEngine (Apple voice processing: echo
//            cancellation, AGC, beamforming)
//   analyze  Silero VAD decision plus optional trim (also the default when no
//            subcommand is given)
//   arm      prepare the recorder and wait for SIGUSR1 before capture
//
// The binary records natively and not through sox: sox cannot use Apple voice
// processing, and the microphone permission would belong to the parent process.
// An Info.plist embedded in __TEXT plus an ad-hoc signature gives this bare
// binary its own bundle id, so the TCC grant belongs to the binary itself.
//
// Exit codes are part of the shell-client contract:
//   0 = speech present, 3 = no speech, 2 = detector/model/audio error.
private let noSpeechExit: Int32 = 3
// Exit codes of arm mode (the caller uses them to tell how the process ended):
//   5 = disarmed by SIGINT/SIGTERM while waiting; no audio recorded, no file written
//   6 = the audio device or route changed while waiting; the prepared engine
//       graph is stale and the caller must arm again
private let disarmedExit: Int32 = 5
private let staleArmExit: Int32 = 6

private struct WavAudio {
    let samples: [Float]
    let sampleRate: Int
}

private enum VADError: LocalizedError {
    case usage
    case invalidWav(String)
    case missingModel(String)
    case invalidSetting(String)
    case recording(String)
    case conversion(String)
    case output(String)

    var errorDescription: String? {
        switch self {
        case .usage:
            return """
                usage: zh-voice-audio record <out.wav> [--level PATH] [--max-seconds N] \
                [--auto-stop-silence SECONDS] [--voice-processing]
                       zh-voice-audio arm <out.wav> [同上参数]   # 预备待命，SIGUSR1 才开始采集
                       zh-voice-audio [analyze] [--json] [--trim] <16k-mono-pcm16.wav> <silero_vad.mlmodelc>
                """
        case .invalidWav(let detail):
            return "invalid WAV: \(detail)"
        case .missingModel(let path):
            return "Core ML VAD model is missing: \(path)"
        case .invalidSetting(let detail):
            return "invalid VAD setting: \(detail)"
        case .recording(let detail):
            return "recording failed: \(detail)"
        case .conversion(let detail):
            return "resampling failed: \(detail)"
        case .output(let detail):
            return detail
        }
    }
}

private extension Data {
    func littleUInt16(at offset: Int) throws -> UInt16 {
        guard offset >= 0, offset + 2 <= count else {
            throw VADError.invalidWav("truncated UInt16 at byte \(offset)")
        }
        return UInt16(self[offset]) | (UInt16(self[offset + 1]) << 8)
    }

    func littleUInt32(at offset: Int) throws -> UInt32 {
        guard offset >= 0, offset + 4 <= count else {
            throw VADError.invalidWav("truncated UInt32 at byte \(offset)")
        }
        return UInt32(self[offset])
            | (UInt32(self[offset + 1]) << 8)
            | (UInt32(self[offset + 2]) << 16)
            | (UInt32(self[offset + 3]) << 24)
    }

    func ascii(at offset: Int, count length: Int) -> String? {
        guard offset >= 0, offset + length <= count else { return nil }
        return String(data: self[offset ..< offset + length], encoding: .ascii)
    }
}

private func loadWav(_ url: URL) throws -> WavAudio {
    let data = try Data(contentsOf: url, options: .mappedIfSafe)
    guard data.count >= 12,
          data.ascii(at: 0, count: 4) == "RIFF",
          data.ascii(at: 8, count: 4) == "WAVE" else {
        throw VADError.invalidWav("expected a RIFF/WAVE file")
    }

    var audioFormat: UInt16?
    var channels: UInt16?
    var sampleRate: UInt32?
    var bitsPerSample: UInt16?
    var pcmRange: Range<Int>?
    var offset = 12

    while offset + 8 <= data.count {
        guard let chunkName = data.ascii(at: offset, count: 4) else { break }
        let rawSize = Int(try data.littleUInt32(at: offset + 4))
        let bodyStart = offset + 8
        let bodyEnd = bodyStart + rawSize
        guard rawSize >= 0, bodyEnd <= data.count else {
            throw VADError.invalidWav("truncated \(chunkName) chunk")
        }

        if chunkName == "fmt " {
            guard rawSize >= 16 else {
                throw VADError.invalidWav("fmt chunk is shorter than 16 bytes")
            }
            audioFormat = try data.littleUInt16(at: bodyStart)
            channels = try data.littleUInt16(at: bodyStart + 2)
            sampleRate = try data.littleUInt32(at: bodyStart + 4)
            bitsPerSample = try data.littleUInt16(at: bodyStart + 14)
        } else if chunkName == "data" {
            pcmRange = bodyStart ..< bodyEnd
        }

        // RIFF chunks are word-aligned.
        offset = bodyEnd + (rawSize & 1)
    }

    guard audioFormat == 1 else {
        throw VADError.invalidWav("only integer PCM is supported")
    }
    guard channels == 1 else {
        throw VADError.invalidWav("expected mono audio")
    }
    guard sampleRate == 16_000 else {
        throw VADError.invalidWav("expected 16000 Hz audio")
    }
    guard bitsPerSample == 16 else {
        throw VADError.invalidWav("expected 16-bit PCM audio")
    }
    guard let range = pcmRange else {
        throw VADError.invalidWav("missing data chunk")
    }
    guard range.count.isMultiple(of: 2) else {
        throw VADError.invalidWav("odd-sized PCM payload")
    }

    var samples = [Float]()
    samples.reserveCapacity(range.count / 2)
    var sampleOffset = range.lowerBound
    while sampleOffset < range.upperBound {
        let raw = try data.littleUInt16(at: sampleOffset)
        samples.append(Float(Int16(bitPattern: raw)) / 32_768.0)
        sampleOffset += 2
    }
    return WavAudio(samples: samples, sampleRate: Int(sampleRate!))
}

private final class SileroVAD {
    static let chunkSize = 512
    static let contextSize = 64

    private let model: MLModel
    private var hidden: MLMultiArray
    private var cell: MLMultiArray
    private var context = [Float](repeating: 0, count: contextSize)

    init(modelURL: URL) throws {
        let configuration = MLModelConfiguration()
        // Keep the ASR GPU free; this tiny recurrent model is fastest and
        // lowest-overhead on Core ML's CPU/Neural Engine path.
        configuration.computeUnits = .cpuAndNeuralEngine
        model = try MLModel(contentsOf: modelURL, configuration: configuration)
        hidden = try MLMultiArray(shape: [1, 1, 128], dataType: .float16)
        cell = try MLMultiArray(shape: [1, 1, 128], dataType: .float16)
        reset()
    }

    func reset() {
        context = [Float](repeating: 0, count: Self.contextSize)
        zero(hidden)
        zero(cell)
    }

    private func zero(_ array: MLMultiArray) {
        let values = array.dataPointer.assumingMemoryBound(to: Float16.self)
        for index in 0 ..< array.count { values[index] = 0 }
    }

    func process(_ chunk: ArraySlice<Float>) throws -> Float {
        precondition(chunk.count == Self.chunkSize)
        let audio = try MLMultiArray(shape: [1, 1, 576], dataType: .float16)
        let audioValues = audio.dataPointer.assumingMemoryBound(to: Float16.self)
        for index in 0 ..< Self.contextSize {
            audioValues[index] = Float16(context[index])
        }
        for (index, sample) in chunk.enumerated() {
            audioValues[Self.contextSize + index] = Float16(sample)
        }
        context = Array(chunk.suffix(Self.contextSize))

        let input = try MLDictionaryFeatureProvider(dictionary: [
            "audio": MLFeatureValue(multiArray: audio),
            "h": MLFeatureValue(multiArray: hidden),
            "c": MLFeatureValue(multiArray: cell),
        ])
        let prediction = try model.prediction(from: input)
        guard let probability = prediction.featureValue(for: "probability")?.multiArrayValue,
              let newHidden = prediction.featureValue(for: "h_out")?.multiArrayValue,
              let newCell = prediction.featureValue(for: "c_out")?.multiArrayValue else {
            throw VADError.invalidSetting("model outputs do not match Silero v6.2.1")
        }
        hidden = newHidden
        cell = newCell
        return Float(probability.dataPointer.assumingMemoryBound(to: Float16.self)[0])
    }
}

private struct SpeechSegment: Encodable {
    let startMs: Int
    let endMs: Int

    enum CodingKeys: String, CodingKey {
        case startMs = "start_ms"
        case endMs = "end_ms"
    }
}

private struct DetectionResult: Encodable {
    let speech: Bool
    let maxProbability: Float
    let speechDurationMs: Int
    let threshold: Float
    let usedStrongRule: Bool
    let inputDurationMs: Int
    let outputDurationMs: Int
    let trimStartMs: Int
    let trimEndMs: Int
    let trimmed: Bool
    let segments: [SpeechSegment]

    enum CodingKeys: String, CodingKey {
        case speech
        case maxProbability = "max_probability"
        case speechDurationMs = "speech_duration_ms"
        case threshold
        case usedStrongRule = "used_strong_rule"
        case inputDurationMs = "input_duration_ms"
        case outputDurationMs = "output_duration_ms"
        case trimStartMs = "trim_start_ms"
        case trimEndMs = "trim_end_ms"
        case trimmed
        case segments
    }
}

/// Each value can be overridden by an environment variable. The defaults are the
/// Silero general defaults plus the measured values of the two-tier rule.
private struct VADSettings {
    let onset: Float
    let offset: Float
    let minimumSpeechMs: Int
    let strongThreshold: Float
    let strongMinimumSpeechMs: Int
    let minimumSilenceMs: Int
    let speechPadMs: Int

    static func fromEnvironment() throws -> VADSettings {
        let onset = try floatSetting("ZH_VOICE_VAD_THRESHOLD", default: 0.5)
        let offset = try floatSetting("ZH_VOICE_VAD_OFFSET", default: 0.35)
        let minimum = try intSetting("ZH_VOICE_VAD_MIN_SPEECH_MS", default: 250)
        // Two-tier rule. Silero says that 0.5/250 ms is only a general default
        // that needs tuning on data. A single-syllable word such as "好", "对" or
        // "嗯" sits at the 250 ms threshold. So a fast lane relaxes the minimum
        // duration when the probability is high enough. A normal probability
        // still needs the full 250 ms, so that key clicks and microphone bumps do
        // not pass.
        //
        // 0.75 is calibrated on the target microphone. It is not a generic value.
        // Measured data points: long sentence 1.00, single word "好。" 0.833, key
        // press without speech 0.108, synthetic white noise 0.028. The lowest
        // speech value (0.83) is about 7 times the highest noise value (0.11).
        // A threshold of 0.75 accepts single words and stays far from noise.
        let strong = try floatSetting("ZH_VOICE_VAD_STRONG_THRESHOLD", default: 0.75)
        let strongMinimum = try intSetting(
            "ZH_VOICE_VAD_STRONG_MIN_SPEECH_MS", default: 128)
        // Silence allowed inside a segment: a pause shorter than this does not end the segment, so one sentence is not split into several.
        let minimumSilence = try intSetting("ZH_VOICE_VAD_MIN_SILENCE_MS", default: 100)
        // Padding kept at each end when trimming, so that the start and end of a word are not cut.
        let pad = try intSetting("ZH_VOICE_VAD_SPEECH_PAD_MS", default: 160)

        guard onset > 0, onset <= 1, offset >= 0, offset < onset,
              strong >= onset, strong <= 1,
              minimum >= 0, strongMinimum >= 0, minimumSilence >= 0, pad >= 0 else {
            throw VADError.invalidSetting(
                "expected 0 <= offset < threshold <= strong <= 1 and non-negative durations")
        }
        return VADSettings(
            onset: onset, offset: offset, minimumSpeechMs: minimum,
            strongThreshold: strong, strongMinimumSpeechMs: strongMinimum,
            minimumSilenceMs: minimumSilence, speechPadMs: pad)
    }
}

private func floatSetting(_ name: String, default fallback: Float) throws -> Float {
    guard let raw = ProcessInfo.processInfo.environment[name], !raw.isEmpty else {
        return fallback
    }
    guard let value = Float(raw), value.isFinite else {
        throw VADError.invalidSetting("\(name)=\(raw)")
    }
    return value
}

private func intSetting(_ name: String, default fallback: Int) throws -> Int {
    guard let raw = ProcessInfo.processInfo.environment[name], !raw.isEmpty else {
        return fallback
    }
    guard let value = Int(raw) else {
        throw VADError.invalidSetting("\(name)=\(raw)")
    }
    return value
}

private struct Analysis {
    let result: DetectionResult
    let trimmedSamples: [Float]
}

/// Scan the whole audio and return the speech segments and the trim points.
///
/// The scan cannot stop early. Silero is a stateful RNN and cannot scan backward,
/// so the only way to find where the last speech segment ends is to scan forward
/// to the end.
private func analyze(samples: [Float], modelURL: URL,
                     settings: VADSettings) throws -> Analysis {
    let detector = try SileroVAD(modelURL: modelURL)
    let normalFrames = max(1, Int(ceil(Double(settings.minimumSpeechMs) / 32.0)))
    let strongFrames = max(1, Int(ceil(Double(settings.strongMinimumSpeechMs) / 32.0)))
    let silenceFrames = max(1, Int(ceil(Double(settings.minimumSilenceMs) / 32.0)))

    var maxProbability: Float = 0
    var candidateStart: Int?          // a span above onset that has not yet reached the minimum duration
    var candidateLength = 0
    var candidatePeak: Float = 0
    var confirmedStart: Int?          // a span already confirmed as speech
    var pendingSilence = 0
    var segmentsInSamples = [(start: Int, end: Int)]()
    var usedStrongRule = false
    var sampleIndex = 0
    var frameIndex = 0

    while sampleIndex < samples.count {
        let copied = min(samples.count - sampleIndex, SileroVAD.chunkSize)
        var frame = [Float](repeating: 0, count: SileroVAD.chunkSize)
        frame.replaceSubrange(0 ..< copied,
                              with: samples[sampleIndex ..< sampleIndex + copied])
        let probability = try detector.process(frame[...])
        maxProbability = max(maxProbability, probability)

        if let start = confirmedStart {
            pendingSilence = probability < settings.offset ? pendingSilence + 1 : 0
            if pendingSilence >= silenceFrames {
                let endFrame = max(start + 1, frameIndex - pendingSilence + 1)
                segmentsInSamples.append((
                    start: start * SileroVAD.chunkSize,
                    end: min(samples.count, endFrame * SileroVAD.chunkSize)))
                confirmedStart = nil
                pendingSilence = 0
            }
        } else if candidateStart == nil {
            if probability >= settings.onset {
                candidateStart = frameIndex
                candidateLength = 1
                candidatePeak = probability
            }
        } else if probability < settings.offset {
            candidateStart = nil
            candidateLength = 0
            candidatePeak = 0
        } else {
            candidateLength += 1
            candidatePeak = max(candidatePeak, probability)
        }

        // Promote the candidate: if any frame in the span reached the strong threshold, use the fast lane. Otherwise use the normal minimum duration.
        if confirmedStart == nil, let start = candidateStart {
            let strong = candidatePeak >= settings.strongThreshold
            if candidateLength >= (strong ? strongFrames : normalFrames) {
                confirmedStart = start
                usedStrongRule = usedStrongRule || strong
                candidateStart = nil
                candidateLength = 0
                candidatePeak = 0
            }
        }

        sampleIndex += copied
        frameIndex += 1
    }

    if let start = confirmedStart {       // speech continues to the very end
        segmentsInSamples.append((start: start * SileroVAD.chunkSize,
                                  end: samples.count))
    }

    let pad = settings.speechPadMs * 16                  // 16 samples per ms @16k
    let speech = !segmentsInSamples.isEmpty
    let trimStart = speech ? max(0, segmentsInSamples[0].start - pad) : 0
    let trimEnd = speech
        ? min(samples.count, segmentsInSamples[segmentsInSamples.count - 1].end + pad)
        : samples.count
    let trimmedSamples = speech ? Array(samples[trimStart ..< trimEnd]) : samples
    let speechSamples = segmentsInSamples.reduce(0) { $0 + max(0, $1.end - $1.start) }

    let result = DetectionResult(
        speech: speech,
        maxProbability: maxProbability,
        speechDurationMs: speechSamples / 16,
        threshold: settings.onset,
        usedStrongRule: usedStrongRule,
        inputDurationMs: samples.count / 16,
        outputDurationMs: trimmedSamples.count / 16,
        trimStartMs: trimStart / 16,
        trimEndMs: trimEnd / 16,
        trimmed: speech && (trimStart > 0 || trimEnd < samples.count),
        segments: segmentsInSamples.map {
            SpeechSegment(startMs: $0.start / 16, endMs: $0.end / 16)
        })
    return Analysis(result: result, trimmedSamples: trimmedSamples)
}

/// Write a 16 kHz mono 16-bit WAV. It stores only trim results, so the format is fixed.
private func writeWav(samples: [Float], to url: URL) throws {
    let byteCount = samples.count * 2
    var data = Data(capacity: 44 + byteCount)
    func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
    func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }

    data.append(contentsOf: Array("RIFF".utf8)); u32(UInt32(36 + byteCount))
    data.append(contentsOf: Array("WAVE".utf8))
    data.append(contentsOf: Array("fmt ".utf8)); u32(16)
    u16(1); u16(1); u32(16_000); u32(16_000 * 2); u16(2); u16(16)
    data.append(contentsOf: Array("data".utf8)); u32(UInt32(byteCount))
    for s in samples {
        let clamped = max(-1.0, min(1.0, s))
        u16(UInt16(bitPattern: Int16(clamped * 32_767.0)))
    }

    // Write a temporary file, then replace atomically: a crash in between never
    // leaves a partial WAV in place of the original audio.
    // A cache cleanup can delete the whole directory during a recording, so create
    // the parent directory before writing.
    let tmp = url.appendingPathExtension("vadtmp")
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                            withIntermediateDirectories: true,
                                            attributes: [.posixPermissions: 0o700])
    try data.write(to: tmp, options: .atomic)
    try FileManager.default.setAttributes([.posixPermissions: 0o600],
                                          ofItemAtPath: tmp.path)
    _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
}

// ───────────────────────────── record ─────────────────────────────

private func ensureMicrophonePermission() throws {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized:
        return
    case .denied, .restricted:
        throw VADError.recording(
            "麦克风权限被拒。系统设置 → 隐私与安全性 → 麦克风，打开「本地语音输入」")
    case .notDetermined:
        // The first run shows a system dialog. The grant belongs to the bundle id
        // of this binary (local.zh-voice.audio).
        let decision = DispatchSemaphore(value: 0)
        var granted = false
        AVCaptureDevice.requestAccess(for: .audio) { allowed in
            granted = allowed
            decision.signal()
        }
        guard decision.wait(timeout: .now() + 30) == .success, granted else {
            throw VADError.recording("麦克风权限没被授予")
        }
    @unknown default:
        throw VADError.recording("麦克风权限状态未知")
    }
}

private final class AudioRecorder {
    private let engine = AVAudioEngine()
    private let levelURL: URL?
    private let lock = NSLock()
    private var captured = [Float]()
    private var latestLevel: Float = 0
    private var heardSound = false
    private var lastSoundUptime: TimeInterval = 0
    private var levelTimer: DispatchSourceTimer?
    private var inputNode: AVAudioInputNode?

    private(set) var sampleRate: Double = 0
    private(set) var voiceProcessingActive = false
    private(set) var degraded = [String]()

    init(levelURL: URL?) {
        self.levelURL = levelURL
    }

    /// Configure the engine but do NOT start capture. This half (inputNode +
    /// setVoiceProcessingEnabled + prepare) takes ~590 ms, about 80% of the start
    /// delay. Arm mode pays it before the key press. After configuration no
    /// buffer arrives (400 ms measured with zero callbacks), so waiting does not
    /// use the microphone.
    func configure(requestVoiceProcessing: Bool) throws {
        let input = engine.inputNode
        if requestVoiceProcessing {
            // Echo cancellation, AGC, beamforming. OFF by default: in a silent room
            // the AGC raises the noise floor by 46 dB (3 s in the same room:
            // RMS 0.031-0.061 with it, 0.00013 without), so it rewrites the gain
            // heavily. Without it the gain is unity, and the level calibration
            // (0.0008/0.020) assumes unity gain. VAD and ASR are not affected:
            // synthetic speech at RMS 0.003 still gives max_probability=1.0 and an
            // exact transcript.
            // Cost: speaker output is recorded (no AEC).
            // If voice processing is unavailable, recording must not fail. Record
            // the degradation and keep recording.
            do {
                try input.setVoiceProcessingEnabled(true)
                voiceProcessingActive = input.isVoiceProcessingEnabled
                if !voiceProcessingActive { degraded.append("voice_processing_unavailable") }
                // Enabling VP registers the process as a ducking client at once.
                // Other audio drops to -15 dB, whether or not the engine runs: an
                // armed recorder that does not capture still ducks (scalar 0.178
                // measured). .min reduces this to -4 dB (0.631).
                // enableAdvancedDucking must be false: true ignores .min and falls
                // back to -15 dB.
                input.voiceProcessingOtherAudioDuckingConfiguration =
                    AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
                        enableAdvancedDucking: false, duckingLevel: .min)
                if input.voiceProcessingOtherAudioDuckingConfiguration.duckingLevel != .min {
                    degraded.append("ducking_not_min")
                }
            } catch {
                degraded.append("voice_processing_error")
            }
        }
        // Query the format AFTER voice processing is set: enabling it changes the input format.
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw VADError.recording("默认输入设备没有可用声道（先看麦克风权限）")
        }
        guard format.commonFormat == .pcmFormatFloat32, !format.isInterleaved else {
            throw VADError.recording("默认输入不是非交错 Float32：\(format)")
        }
        sampleRate = format.sampleRate

        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buf, _ in
            self?.consume(buf)
        }
        inputNode = input
        engine.prepare()
    }

    /// Start capture. The delay from GO to the first buffer is a constant ~170 ms, cold or warm. This is the lower limit.
    func startCapture() throws {
        do {
            try engine.start()
        } catch {
            inputNode?.removeTap(onBus: 0)
            inputNode = nil
            throw VADError.recording(error.localizedDescription)
        }
        // Write the level only from here. The armed phase writes no byte, so a moving level bar means real capture.
        startLevelTimer()
    }

    func start(requestVoiceProcessing: Bool) throws {
        try configure(requestVoiceProcessing: requestVoiceProcessing)
        try startCapture()
    }

    private func consume(_ buffer: AVAudioPCMBuffer) {
        guard let channels = buffer.floatChannelData else { return }
        let frames = Int(buffer.frameLength)
        guard frames > 0, buffer.format.channelCount > 0 else { return }
        var mono = [Float](repeating: 0, count: frames)
        var squareSum: Double = 0
        for frame in 0 ..< frames {
            // Use channel 0 only. VoiceProcessingIO on macOS adds auxiliary
            // channels, and averaging them in would fold the echo reference
            // signal back into the recording.
            let value = channels[0][frame]
            mono[frame] = value
            squareSum += Double(value * value)
        }
        let rms = Float((squareSum / Double(frames)).squareRoot())
        // Level calibration: RMS 0.0008 maps to 0 and RMS 0.020 maps to 1.
        let normalized = max(0, min(1, (rms - 0.0008) / (0.020 - 0.0008)))
        lock.lock()
        captured.append(contentsOf: mono)
        latestLevel = pow(normalized, 0.6)
        if rms >= 0.0025 {
            heardSound = true
            lastSoundUptime = ProcessInfo.processInfo.systemUptime
        }
        lock.unlock()
    }

    private func startLevelTimer() {
        guard levelURL != nil else { return }
        let timer = DispatchSource.makeTimerSource(
            queue: DispatchQueue(label: "local.zh-voice.audio-level"))
        timer.schedule(deadline: .now(), repeating: .milliseconds(50))
        timer.setEventHandler { [weak self] in self?.flushLevel() }
        levelTimer = timer
        timer.resume()
    }

    private func stopLevelTimer() {
        levelTimer?.cancel()
        levelTimer = nil
        if let levelURL { try? writePrivate(Data("0\n".utf8), to: levelURL) }
    }

    private func flushLevel() {
        guard let levelURL else { return }
        lock.lock(); let level = latestLevel; lock.unlock()
        try? writePrivate(Data(String(format: "%.5f\n", level).utf8), to: levelURL)
    }

    func stop() -> [Float] {
        if let inputNode { inputNode.removeTap(onBus: 0) }
        inputNode = nil
        engine.stop()
        stopLevelTimer()
        lock.lock(); let result = captured; lock.unlock()
        return result
    }

    /// Used only by the standalone recording path of `zh-voice`. The hotkey path
    /// does not use it: the next key press decides when to stop.
    func reachedSilenceEndpoint(after seconds: Double) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return heardSound
            && ProcessInfo.processInfo.systemUptime - lastSoundUptime >= seconds
    }
}

private func writePrivate(_ data: Data, to url: URL) throws {
    try data.write(to: url, options: .atomic)
    guard chmod(url.path, 0o600) == 0 else {
        throw VADError.output("cannot chmod 0600 \(url.path)")
    }
}

private func resampleTo16k(_ samples: [Float], from inputRate: Double) throws -> [Float] {
    guard !samples.isEmpty else { return [] }
    if abs(inputRate - 16_000) < 0.1 { return samples }
    guard let inFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                       sampleRate: inputRate, channels: 1, interleaved: false),
          let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                        sampleRate: 16_000, channels: 1, interleaved: false),
          let converter = AVAudioConverter(from: inFormat, to: outFormat) else {
        throw VADError.conversion("建不出 AVAudioConverter")
    }
    guard samples.count <= Int(UInt32.max) else { throw VADError.conversion("录音太长") }
    guard let inBuffer = AVAudioPCMBuffer(pcmFormat: inFormat,
                                          frameCapacity: AVAudioFrameCount(samples.count)) else {
        throw VADError.conversion("建不出输入缓冲")
    }
    inBuffer.frameLength = AVAudioFrameCount(samples.count)
    samples.withUnsafeBufferPointer {
        inBuffer.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count)
    }
    let capacity = Int(ceil(Double(samples.count) * 16_000.0 / inputRate)) + 1024
    guard let outBuffer = AVAudioPCMBuffer(pcmFormat: outFormat,
                                           frameCapacity: AVAudioFrameCount(capacity)) else {
        throw VADError.conversion("建不出输出缓冲")
    }
    var supplied = false
    var conversionError: NSError?
    converter.convert(to: outBuffer, error: &conversionError) { _, status in
        if supplied {
            status.pointee = .endOfStream
            return nil
        }
        supplied = true
        status.pointee = .haveData
        return inBuffer
    }
    if let conversionError { throw VADError.conversion(conversionError.localizedDescription) }
    let count = Int(outBuffer.frameLength)
    guard count > 0, let data = outBuffer.floatChannelData else { return [] }
    return Array(UnsafeBufferPointer(start: data[0], count: count))
}

private func recordCommand(_ rawArguments: [String], armed: Bool = false) throws -> Int32 {
    var arguments = rawArguments
    var outputPath: String?
    var levelPath: String?
    var maximumSeconds = 120.0
    var autoStopSilence: Double?
    var voiceProcessing = false   // see AudioRecorder.configure: AGC raises the noise floor by 46 dB, so it is off by default

    func next(_ flag: String) throws -> String {
        guard !arguments.isEmpty else { throw VADError.invalidSetting("\(flag) 缺参数") }
        return arguments.removeFirst()
    }
    while !arguments.isEmpty {
        let argument = arguments.removeFirst()
        switch argument {
        case "--level": levelPath = try next(argument)
        case "--max-seconds":
            guard let v = Double(try next(argument)), v > 0 else {
                throw VADError.invalidSetting("--max-seconds 要正数")
            }
            maximumSeconds = v
        case "--auto-stop-silence":
            guard let v = Double(try next(argument)), v > 0 else {
                throw VADError.invalidSetting("--auto-stop-silence 要正数")
            }
            autoStopSilence = v
        case "--voice-processing": voiceProcessing = true
        case "--no-voice-processing": voiceProcessing = false   // already the default; kept so existing callers still work
        default:
            guard outputPath == nil, !argument.hasPrefix("--") else { throw VADError.usage }
            outputPath = argument
        }
    }
    guard let outputPath else { throw VADError.usage }

    let outputURL = URL(fileURLWithPath: outputPath)
    let levelURL = levelPath.map { URL(fileURLWithPath: $0) }
    try ensureMicrophonePermission()
    try? FileManager.default.removeItem(at: outputURL)

    let recorder = AudioRecorder(levelURL: levelURL)
    let stop = DispatchSemaphore(value: 0)
    let signalQueue = DispatchQueue(label: "local.zh-voice.audio-signals")
    // Ignore the default disposition and handle the signals with DispatchSource.
    // On SIGINT the process then finishes writing the wav before it exits, as a
    // caller expects from a recorder that is stopped by interrupt.
    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)
    let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: signalQueue)
    let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: signalQueue)

    if armed {
        // ── Armed phase: configure the engine graph (the expensive ~590 ms), then wait for GO ──
        // Three wake reasons compete and the first one wins. wake is written only on signalQueue, and read through it.
        enum Wake { case go, disarm, stale }
        var wake = Wake.disarm
        let armSem = DispatchSemaphore(value: 0)
        interrupt.setEventHandler { wake = .disarm; armSem.signal() }
        terminate.setEventHandler { wake = .disarm; armSem.signal() }
        interrupt.resume()
        terminate.resume()
        let goSource = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: signalQueue)
        goSource.setEventHandler { wake = .go; armSem.signal() }
        goSource.resume()

        try recorder.configure(requestVoiceProcessing: voiceProcessing)

        // If the audio route changes while armed (new default input, headphones
        // plugged in or out), the prepared graph is stale. Exit so that the caller
        // arms again; this is better than a start() error after GO. The
        // notification arrives on any thread, so move to signalQueue before
        // touching wake. Unsubscribe right after GO, so that behavior during
        // recording is identical to a cold start.
        let staleObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: nil, queue: nil) { _ in
            signalQueue.async { wake = .stale; armSem.signal() }
        }

        print("ARMED")
        fflush(stdout)

        armSem.wait()
        let reason = signalQueue.sync { wake }
        NotificationCenter.default.removeObserver(staleObserver)
        switch reason {
        case .disarm:
            return disarmedExit          // nothing recorded, no file to collect
        case .stale:
            return staleArmExit
        case .go:
            break
        }
        // After GO, SIGINT/SIGTERM mean "stop recording" again: switch to the recording handlers.
        interrupt.setEventHandler { stop.signal() }
        terminate.setEventHandler { stop.signal() }
        goSource.cancel()
        try recorder.startCapture()      // on error: exit 2, the caller falls back to a cold start
    } else {
        interrupt.setEventHandler { stop.signal() }
        terminate.setEventHandler { stop.signal() }
        interrupt.resume()
        terminate.resume()
        try recorder.start(requestVoiceProcessing: voiceProcessing)
    }

    var endpointTimer: DispatchSourceTimer?
    if let autoStopSilence {
        let timer = DispatchSource.makeTimerSource(queue: signalQueue)
        timer.schedule(deadline: .now() + .milliseconds(100), repeating: .milliseconds(100))
        timer.setEventHandler {
            if recorder.reachedSilenceEndpoint(after: autoStopSilence) { stop.signal() }
        }
        endpointTimer = timer
        timer.resume()
    }
    DispatchQueue.global(qos: .userInitiated)
        .asyncAfter(deadline: .now() + maximumSeconds) { stop.signal() }

    stop.wait()
    endpointTimer?.cancel()
    interrupt.cancel()
    terminate.cancel()

    let samples = try resampleTo16k(recorder.stop(), from: recorder.sampleRate)
    try writeWav(samples: samples, to: outputURL)
    if !recorder.degraded.isEmpty {
        FileHandle.standardError.write(
            Data("zh-voice-audio: 降级 \(recorder.degraded.joined(separator: ","))\n".utf8))
    }
    return 0
}

// ───────────────────────────── analyze ─────────────────────────────

private func analyzeCommand(_ rawArguments: [String]) throws -> Int32 {
    var arguments = rawArguments
    var wantsJSON = false
    var wantsTrim = false
    while let first = arguments.first, first.hasPrefix("--") {
        switch first {
        case "--json": wantsJSON = true
        case "--trim": wantsTrim = true          // trim leading and trailing silence in place
        default: throw VADError.usage
        }
        arguments.removeFirst()
    }
    guard arguments.count == 2 else { throw VADError.usage }

    let wavURL = URL(fileURLWithPath: arguments[0])
    let modelURL = URL(fileURLWithPath: arguments[1], isDirectory: true)
    guard FileManager.default.fileExists(atPath: modelURL.path) else {
        throw VADError.missingModel(modelURL.path)
    }

    let audio = try loadWav(wavURL)
    guard audio.sampleRate == 16_000 else {
        throw VADError.invalidWav("expected 16000 Hz audio")
    }
    let analysis = try analyze(samples: audio.samples, modelURL: modelURL,
                               settings: try VADSettings.fromEnvironment())
    // Write back only if speech exists and something was trimmed. With no speech, leave the file unchanged for the caller.
    if wantsTrim, analysis.result.speech, analysis.result.trimmed {
        try writeWav(samples: analysis.trimmedSamples, to: wavURL)
    }
    if wantsJSON {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        FileHandle.standardOutput.write(try encoder.encode(analysis.result))
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
    return analysis.result.speech ? 0 : noSpeechExit
}

private func run() throws -> Int32 {
    var arguments = Array(CommandLine.arguments.dropFirst())
    if arguments.first == "record" {
        arguments.removeFirst()
        return try recordCommand(arguments)
    }
    if arguments.first == "arm" {
        arguments.removeFirst()
        return try recordCommand(arguments, armed: true)
    }
    // No subcommand means analyze, so `zh-voice-vad [--json] wav model` still works.
    if arguments.first == "analyze" { arguments.removeFirst() }
    return try analyzeCommand(arguments)
}

_ = umask(0o077)          // everything this process writes is mode 0600, without relying on each call site
// The default disposition of SIGUSR1 kills the process, and SIGUSR1 is the GO
// signal of arm mode. Set SIG_IGN at program entry, not in the arm branch: the
// signal can arrive before argument parsing, and the process would die silently.
// The real guarantee is the handshake (the caller sends GO only after it sees
// ARMED). This line only makes an unexpected early signal harmless.
signal(SIGUSR1, SIG_IGN)
do {
    exit(try run())
} catch {
    FileHandle.standardError.write(Data("zh-voice-audio: \(error.localizedDescription)\n".utf8))
    exit(2)
}
