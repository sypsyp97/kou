import AVFoundation
import CoreML
import Foundation

// 这个二进制装出来叫 `zh-voice-audio`，两个子命令：
//   record   AVAudioEngine 原生录音（Apple 的语音处理：回声消除 / AGC / 波束成形）
//   analyze  Silero VAD 判定 + 可选裁剪（不带子命令时也走这条，保持旧调用形式可用）
//
// 录音为什么不继续用 sox：sox 是 Hammerspoon 的子进程，麦克风授权含混地挂在
// Hammerspoon 名下；而且它拿不到 Apple 的 voice processing。嵌进 __TEXT 的
// Info.plist + ad-hoc 签名让这个裸二进制有自己的 bundle id，TCC 授权落在它自己头上。
//
// Exit codes are part of the shell-client contract:
//   0 = speech present, 3 = no speech, 2 = detector/model/audio error.
private let noSpeechExit: Int32 = 3
// arm 模式的退出码契约（init.lua 靠它分辨"怎么退的"）：
//   5 = 待命中被 SIGINT/SIGTERM 解除，一个字节都没录，也没写任何文件
//   6 = 待命中音频设备/路由变了，预备的引擎图已过期，调用方应重新预备
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

/// 每一项都能用环境变量盖掉；默认值是 Silero 官方通用默认 + 两级规则的实测取值。
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
        // 两级规则：Silero 官方自己说 0.5/250ms 只是通用默认，要按数据调。
        // 「好」「对」「嗯」这种单字挂在 250ms 的门槛上，所以给一条"概率够高就放宽
        // 最短时长"的快车道；普通概率仍然要满 250ms，免得键盘声、碰麦克风被放行。
        //
        // **0.75 是在这只麦克风上标定出来的，不是抄来的通用值。**
        // 第一版照抄了 0.85，实测一次单字「好。」是 max_probability=0.833 —— 差一点没够到，
        // 快车道没生效，靠常规门槛的 256ms 过关（只比 250ms 多 6ms，等于走在悬崖边）。
        // 本机四个真实数据点：长句 1.00 / 单字 0.833 / 按了没说话 0.108 /
        // 合成白噪声 0.028。语音侧最低 0.83、噪声侧最高 0.11，中间隔了 7 倍，
        // 所以把线画在 0.75 既能接住单字，离噪声也还很远。
        let strong = try floatSetting("ZH_VOICE_VAD_STRONG_THRESHOLD", default: 0.75)
        let strongMinimum = try intSetting(
            "ZH_VOICE_VAD_STRONG_MIN_SPEECH_MS", default: 128)
        // 段内允许的静音：低于这个长度的停顿不算段结束，免得一句话被切成好几段。
        let minimumSilence = try intSetting("ZH_VOICE_VAD_MIN_SILENCE_MS", default: 100)
        // 裁剪时首尾各留这么多，别把字头字尾削掉。
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

/// 扫完整段音频，给出语音区间和裁剪点。
///
/// 和原来那版的区别：原来一旦累计够 minimumFrames 就立刻 return true（当闸门够用，
/// 也快）。现在要输出区间来裁静音，就必须走完——Silero 是有状态的 RNN，不能倒着扫，
/// 想知道最后一段语音在哪结束只有正向扫到底这一条路。
private func analyze(samples: [Float], modelURL: URL,
                     settings: VADSettings) throws -> Analysis {
    let detector = try SileroVAD(modelURL: modelURL)
    let normalFrames = max(1, Int(ceil(Double(settings.minimumSpeechMs) / 32.0)))
    let strongFrames = max(1, Int(ceil(Double(settings.strongMinimumSpeechMs) / 32.0)))
    let silenceFrames = max(1, Int(ceil(Double(settings.minimumSilenceMs) / 32.0)))

    var maxProbability: Float = 0
    var candidateStart: Int?          // 越过 onset、但还没满最短时长的一段
    var candidateLength = 0
    var candidatePeak: Float = 0
    var confirmedStart: Int?          // 已经确认是语音的一段
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

        // 候选转正：段内出现过 >= strong 的帧就走快车道，否则按常规最短时长。
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

    if let start = confirmedStart {       // 说到最后一刻都没停
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

/// 写一个 16k/mono/16-bit 的 WAV。只用来落裁剪结果，所以格式写死。
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

    // 先写临时文件再原子替换：中途崩了也不会留下半截 WAV 顶替原音频。
    // 录音可能跨过缓存清理（目录被整个删掉），落盘前补建父目录。
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
        // 第一次跑会弹系统对话框。授权落在这个二进制自己的 bundle id 上
        // （local.zh-voice.audio），不再挂在 Hammerspoon 名下。
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

    /// 配置引擎但**不开始采集**。实测这一半（inputNode + setVoiceProcessingEnabled +
    /// prepare）要 ~590ms，占了整个启动损失的八成——arm 模式把它提前到按键之前付掉。
    /// 配置完等多久都没有一个 buffer（实测 400ms 零回调），所以待命不占麦克风。
    func configure(requestVoiceProcessing: Bool) throws {
        let input = engine.inputNode
        if requestVoiceProcessing {
            // 回声消除、自动增益、波束成形。**默认关掉**（2026-08-24）：AGC 在静音房间里
            // 把底噪抬了 46dB（同一间屋子 3s 实测 RMS 0.031-0.061 vs 关掉后 0.00013），
            // 也就是说它一直在大幅改写增益，而开会时别人听到的麦克风是小的。
            // 关掉后回到 sox 时代的单位增益，电平标定（0.0008/0.020）本来就是那时候量的；
            // VAD 和 ASR 实测不受影响（合成语音压到 RMS 0.003 仍 max_probability=1.0、转写逐字正确）。
            // 代价：外放声音会被录进来（没有 AEC 了）。
            // 拿不到也不该让录音失败——降级记下来，照录。
            do {
                try input.setVoiceProcessingEnabled(true)
                voiceProcessingActive = input.isVoiceProcessingEnabled
                if !voiceProcessingActive { degraded.append("voice_processing_unavailable") }
                // 开 VP 的那一刻就注册成 ducking 客户端，其它音频被压到 −15dB，而且
                // **跟引擎有没有在跑无关**——预备式待命着不采集也照压（本机实测 scalar
                // 0.178，视频/外放一直是闷的）。.min 把它降到 −4dB（0.631）。
                // ⚠️ enableAdvancedDucking 必须是 false：设 true 会把 .min 吃掉、退回 −15dB。
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
        // 格式必须在设完 voice processing **之后**再问：开启它会换掉输入格式。
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

    /// 真正开始采集。GO→第一个 buffer 实测恒定 ~170ms，冷热一样——这是压不掉的下限。
    func startCapture() throws {
        do {
            try engine.start()
        } catch {
            inputNode?.removeTap(onBus: 0)
            inputNode = nil
            throw VADError.recording(error.localizedDescription)
        }
        // 电平从这里才开始写：待命阶段一个字节都不落盘，条子动了 = 真的在采集。
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
            // 只取 0 号声道。VoiceProcessingIO 在 macOS 上会多出辅助声道，
            // 把它们平均进来等于把回声参考信号又折回录音里。
            let value = channels[0][frame]
            mono[frame] = value
            squareSum += Double(value * value)
        }
        let rms = Float((squareSum / Double(frames)).squareRoot())
        // 和原来 island.lua 读 wav 尾部时用的是同一组标定值，所以岛的观感不变。
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

    /// 只给 `zh-voice` 独立录音那条路用（原来是 sox 的 `silence` 效果）。
    /// 热键路径不用它——什么时候停由你再按一下决定。
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
    var voiceProcessing = false   // 见 AudioRecorder.configure：AGC 抬底噪 46dB，默认不开

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
        case "--no-voice-processing": voiceProcessing = false   // 已是默认，留着不破旧调用
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
    // 忽略默认处置，改用 DispatchSource 接管：这样收到 SIGINT 时能把 wav 写完再退，
    // 和调用方对 sox 的期待一致（init.lua 就是靠 task:interrupt() 停录音的）。
    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)
    let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: signalQueue)
    let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: signalQueue)

    if armed {
        // ── 待命段：配好引擎图（贵的那 ~590ms），然后等 GO ───────────────
        // 三种唤醒相互竞争，谁先到听谁的；wake 只在 signalQueue 上写，读也走它。
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

        // 待命期间音频路由变了（换默认输入、插拔耳机），预备的图就过期了：
        // 退出让调用方重新预备，好过 GO 之后才发现 start() 抛错。通知到达线程不定，
        // 挪到 signalQueue 上再碰 wake。GO 之后立刻退订，录音中的行为和冷启动完全一致。
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
            return disarmedExit          // 一个字节都没录，没有任何文件要收
        case .stale:
            return staleArmExit
        case .go:
            break
        }
        // GO 之后 SIGINT/SIGTERM 变回"停止录音"：换回录音段的处理器。
        interrupt.setEventHandler { stop.signal() }
        terminate.setEventHandler { stop.signal() }
        goSource.cancel()
        try recorder.startCapture()      // 抛错 → exit 2，调用方退回冷启动
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
        case "--trim": wantsTrim = true          // 原地裁掉首尾静音
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
    // 只有确实有语音、且确实裁掉了东西，才回写；没语音时原样留着交给调用方处置。
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
    // 不带子命令 = analyze。旧的 `zh-voice-vad [--json] wav model` 调用形式保持可用。
    if arguments.first == "analyze" { arguments.removeFirst() }
    return try analyzeCommand(arguments)
}

_ = umask(0o077)          // 这个进程写出去的东西一律 0600，不指望每个调用点都记得
// SIGUSR1 的默认处置是杀进程，而它是 arm 模式的 GO 信号。SIG_IGN 放在程序入口而不是
// arm 分支里：实测信号能赶在参数解析之前到，那时进程就被无声杀掉了。真正的保证是
// 握手（调用方见到 ARMED 才发 GO），这里只是把意外的早到信号变成无害。
signal(SIGUSR1, SIG_IGN)
do {
    exit(try run())
} catch {
    FileHandle.standardError.write(Data("zh-voice-audio: \(error.localizedDescription)\n".utf8))
    exit(2)
}
