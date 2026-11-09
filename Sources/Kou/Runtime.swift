import AppKit
import Foundation

/// Chinese when the user's first preferred language is Chinese, English otherwise.
let prefersChinese = Locale.preferredLanguages.first?.hasPrefix("zh") == true
func L(_ chinese: String, _ english: String) -> String { prefersChinese ? chinese : english }

/// The app bundle, also when the binary runs through the `kou` symlink that the cask installs.
let appBundle: Bundle = {
    guard Bundle.main.bundleIdentifier == nil, let executable = Bundle.main.executableURL?.resolvingSymlinksInPath() else { return .main }
    return Bundle(url: executable.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()) ?? .main
}()

struct KouPaths {
    let support: URL
    let cache: URL
    let resources: URL
    var recordings: URL { cache.appendingPathComponent("recordings") }
    var socket: URL { cache.appendingPathComponent("asr.sock") }
    var terms: URL { support.appendingPathComponent("terms.txt") }
    var runtime: URL { support.appendingPathComponent("runtime.json") }
    var venv: URL { support.appendingPathComponent("python-0.4.4") }
    var python: URL { venv.appendingPathComponent("bin/python3") }
    var log: URL { support.appendingPathComponent("kou.log") }

    init() throws {
        let fm = FileManager.default
        support = try fm.url(for: .applicationSupportDirectory, in: .userDomainMask,
                             appropriateFor: nil, create: true).appendingPathComponent("Kou")
        cache = try fm.url(for: .cachesDirectory, in: .userDomainMask,
                           appropriateFor: nil, create: true).appendingPathComponent("Kou")
        guard let root = appBundle.resourceURL else { throw KouError.message("App resources are missing") }
        resources = root
        try ensureDirectories()
        if !fm.fileExists(atPath: terms.path) {
            try Data("# One term per line. Terms can contain spaces.\n".utf8).write(to: terms)
            try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: terms.path)
        }
    }

    /// Cache cleaners can delete ~/Library/Caches while Kou runs, so capture calls this again.
    func ensureDirectories() throws {
        let fm = FileManager.default
        for url in [support, cache, recordings] {
            try fm.createDirectory(at: url, withIntermediateDirectories: true,
                                   attributes: [.posixPermissions: 0o700])
            try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
        }
    }

    func resource(_ name: String) -> URL { resources.appendingPathComponent(name) }

    func environment(_ config: RuntimeConfig?) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "\(venv.path)/bin:\(resources.path):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONUNBUFFERED"] = "1"
        env["HF_HUB_OFFLINE"] = "1"
        env["ZH_VOICE_SOCKET"] = socket.path
        env["ZH_VOICE_MANAGED_DAEMON"] = "1"
        env["ZH_VOICE_IDLE"] = "0"
        env["ZH_VOICE_TERMS"] = terms.path
        env["ZH_VOICE_TERMS_CACHE"] = cache.appendingPathComponent("terms.cache").path
        env["ZH_VOICE_METRICS"] = cache.appendingPathComponent("metrics.log").path
        env["ZH_VOICE_VAD_BIN"] = resource("zh-voice-audio").path
        env["ZH_VOICE_CORPUS_FLAG"] = support.appendingPathComponent("corpus").path
        env["ZH_VOICE_CORPUS_DIR"] = support.appendingPathComponent("corpus-data").path
        env["TMPDIR"] = recordings.path + "/"
        if let config {
            env["ZH_VOICE_MODEL"] = config.model
            env["ZH_VOICE_MODEL_PATH"] = config.modelPath
            env["ZH_VOICE_ASR_REVISION"] = config.revision
            env["ZH_VOICE_VAD_MODEL_PATH"] = config.vadPath
            env["ZH_VOICE_HANG_CEIL"] = config.profile == "small" ? "150" : "180"
            env["ZH_VOICE_HANG_FACTOR"] = config.profile == "small" ? "2" : "4"
            env["ZH_VOICE_DAEMON_WAIT"] = config.profile == "small" ? "165" : "195"
        }
        return env
    }

    func recordLog(_ message: String) {
        let fm = FileManager.default
        if let size = try? fm.attributesOfItem(atPath: log.path)[.size] as? Int, size > 524288 {
            try? fm.removeItem(at: log.appendingPathExtension("old"))
            try? fm.moveItem(at: log, to: log.appendingPathExtension("old"))
        }
        if !fm.fileExists(atPath: log.path) { fm.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
        guard let handle = try? FileHandle(forWritingTo: log) else { return }
        defer { try? handle.close() }
        do {
            try handle.seekToEnd()
            try handle.write(contentsOf: Data("\(ISO8601DateFormatter().string(from: Date())) \(message)\n".utf8))
        } catch { NSLog("Kou log write failed: %@", error.localizedDescription) }
    }

    func sweepRecordings() {
        guard let urls = try? FileManager.default.contentsOfDirectory(at: recordings, includingPropertiesForKeys: nil) else { return }
        for url in urls { try? FileManager.default.removeItem(at: url) }
    }
}

enum KouError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
}

struct RuntimeConfig: Codable {
    let profile: String
    let model: String
    let revision: String
    let modelPath: String
    let vadPath: String
}

struct CommandResult {
    let code: Int32
    let output: String
    let error: String
}

// The C supervisor forwards cancellation to the complete child process group.
final class ManagedTask {
    private static let registryLock = NSLock()
    private static var running: [UUID: ManagedTask] = [:]
    private let id = UUID()
    static var activeCount: Int { registryLock.lock(); defer { registryLock.unlock() }; return running.count }
    let process = Process()
    private let output = Pipe()
    private let error = Pipe()
    private let lock = NSLock()
    private var stdout = Data()
    private var stderr = Data()
    var isRunning: Bool { process.isRunning }

    func start(paths: KouPaths, executable: URL, arguments: [String],
               environment: [String: String], stream: ((String) -> Void)? = nil,
               completion: @escaping (CommandResult) -> Void) throws {
        process.executableURL = paths.resource("kou-run")
        process.arguments = [executable.path] + arguments
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = error
        let readers = DispatchGroup()
        for (pipe, isOutput) in [(output, true), (error, false)] {
            readers.enter()
            DispatchQueue.global(qos: .userInitiated).async { [self] in
                defer { readers.leave() }
                while let data = try? pipe.fileHandleForReading.read(upToCount: 4096), !data.isEmpty {
                    lock.lock()
                    if isOutput { stdout.append(data); if stdout.count > 1048576 { stdout.removeFirst(stdout.count - 1048576) } }
                    else { stderr.append(data); if stderr.count > 65536 { stderr.removeFirst(stderr.count - 65536) } }
                    lock.unlock()
                    if let stream { DispatchQueue.main.async { stream(String(decoding: data, as: UTF8.self)) } }
                }
            }
        }
        do { try process.run() }
        catch let launchError {
            try? output.fileHandleForWriting.close()
            try? error.fileHandleForWriting.close()
            throw launchError
        }
        Self.registryLock.lock(); Self.running[id] = self; Self.registryLock.unlock()
        try? output.fileHandleForWriting.close()
        try? error.fileHandleForWriting.close()
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            process.waitUntilExit()
            readers.wait()
            lock.lock()
            let result = CommandResult(code: process.terminationStatus,
                                       output: String(decoding: stdout, as: UTF8.self),
                                       error: String(decoding: stderr, as: UTF8.self))
            lock.unlock()
            Self.registryLock.lock(); Self.running[self.id] = nil; Self.registryLock.unlock()
            DispatchQueue.main.async { completion(result) }
        }
    }

    func cancel() { if process.isRunning { process.terminate() } }
    func beginCapture() { if process.isRunning { kill(process.processIdentifier, SIGUSR1) } }
}

@MainActor final class RuntimeManager: ObservableObject {
    static let notDownloaded = L("尚未下载语音识别模型", "Speech model not downloaded")
    @Published var status = notDownloaded
    @Published var ready = false
    /// True while a setup step runs. `setupTask` is the only source; publish real changes only.
    @Published private(set) var installing = false
    @Published var detail = ""
    let paths: KouPaths
    private(set) var config: RuntimeConfig?
    private var daemon: ManagedTask?
    /// Kou asked the current daemon to stop. Its health replies and exit are not service failures.
    private var daemonRetiring = false
    private var setupTask: ManagedTask? {
        didSet { if installing != (setupTask != nil) { installing = setupTask != nil } }
    }
    private var healthTask: ManagedTask?
    private var timer: Timer?
    private var stopped = false
    private var startedAt = Date.distantPast
    private var failures = 0
    private var restartAt = Date.distantPast

    init(paths: KouPaths) { self.paths = paths }

    static var recommendedProfile: String {
        var size = 0
        sysctlbyname("machdep.cpu.brand_string", nil, &size, nil, 0)
        var bytes = [CChar](repeating: 0, count: size)
        sysctlbyname("machdep.cpu.brand_string", &bytes, &size, nil, 0)
        return String(cString: bytes).hasPrefix("Apple M1") ? "small" : "large"
    }

    func load() {
        stopped = false
        do {
            let data = try Data(contentsOf: paths.runtime)
            let loaded = try JSONDecoder().decode(RuntimeConfig.self, from: data)
            guard ["small", "large"].contains(loaded.profile),
                  FileManager.default.isExecutableFile(atPath: paths.python.path),
                  FileManager.default.fileExists(atPath: loaded.modelPath + "/config.json"),
                  FileManager.default.fileExists(atPath: loaded.vadPath + "/model.mil") else {
                throw KouError.message(L("运行环境不完整，请重新下载模型", "Setup is incomplete. Download the model again."))
            }
            config = loaded
            startDaemon()
        } catch { config = nil; ready = false; status = Self.notDownloaded }
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.checkHealth() }
            }
        }
    }

    func install(profile: String) {
        guard !installing else { return }
        guard let uv = ["/opt/homebrew/bin/uv", "/usr/local/bin/uv"].first(where: FileManager.default.isExecutableFile) else {
            status = L("缺少组件 uv，请在终端运行：brew install uv opencc", "Missing uv. In Terminal, run: brew install uv opencc"); return
        }
        guard ["/opt/homebrew/bin/opencc", "/usr/local/bin/opencc"].contains(where: FileManager.default.isExecutableFile) else {
            status = L("缺少组件 OpenCC，请在终端运行：brew install opencc", "Missing OpenCC. In Terminal, run: brew install opencc"); return
        }
        shutdown()
        stopped = false
        status = L("正在准备运行环境（1/3）…", "Preparing the runtime (1/3)…")
        detail = L("首次使用需要联网下载运行环境和模型。之后听写完全离线。", "The first setup downloads the runtime and model. After that, dictation works offline.")
        var env = paths.environment(nil)
        env["HF_HUB_OFFLINE"] = "0"
        runSetup(URL(fileURLWithPath: uv), ["venv", "--python", "3.13", "--managed-python", "--allow-existing", paths.venv.path], env: env) { [weak self] in
            guard let self else { return }
            self.status = L("正在安装识别组件（2/3）…", "Installing components (2/3)…")
            self.runSetup(URL(fileURLWithPath: uv), ["pip", "sync", "--python", self.paths.python.path, self.paths.resource("requirements.txt").path], env: env) {
                self.status = L("正在下载语音识别模型（3/3）…", "Downloading the speech model (3/3)…")
                self.runSetup(self.paths.python, [self.paths.resource("provision.py").path,
                                                 self.paths.resource("models.json").path, profile,
                                                 self.paths.runtime.path], env: env) {
                    self.setupTask = nil
                    self.load()
                }
            }
        }
    }

    private func runSetup(_ executable: URL, _ args: [String], env: [String: String], next: @escaping () -> Void) {
        let task = ManagedTask()
        setupTask = task
        do {
            try task.start(paths: paths, executable: executable, arguments: args, environment: env,
                           stream: { [weak self] text in
                guard let self else { return }
                self.detail = String((self.detail + text).suffix(6000))
            }) { [weak self, weak task] result in
                guard let self, let task, self.setupTask === task else { return }
                // On success the next step replaces setupTask, so `installing` stays true between steps.
                if result.code == 0 { next() }
                else {
                    self.setupTask = nil
                    self.status = L("下载失败，请检查网络后重试", "Download failed. Check your connection and try again.")
                    self.detail = String((result.error + result.output).suffix(6000))
                    self.paths.recordLog("Setup failed: \(result.code)")
                }
            }
        } catch { setupTask = nil; status = L("下载失败：\(error.localizedDescription)", "Download failed: \(error.localizedDescription)") }
    }

    private func startDaemon() {
        guard !stopped, !installing, let config, daemon == nil, Date() >= restartAt else { return }
        ready = false
        status = L("正在加载模型…", "Loading the model…")
        startedAt = Date()
        let task = ManagedTask()
        daemon = task
        daemonRetiring = false
        do {
            try task.start(paths: paths, executable: paths.python,
                           arguments: [paths.resource("zh-voice-daemon").path], environment: paths.environment(config)) { [weak self, weak task] result in
                guard let self, let task, self.daemon === task else { return }
                self.daemon = nil
                self.ready = false
                // A probe that this instance answered must not mark its successor ready.
                self.healthTask?.cancel(); self.healthTask = nil
                let requested = self.daemonRetiring
                self.daemonRetiring = false
                guard !self.stopped else { return }
                if requested { self.restartAt = .distantPast; self.startDaemon(); return }
                self.failures += 1
                self.restartAt = Date().addingTimeInterval(min(30, pow(2, Double(min(self.failures, 5)))))
                self.status = L("识别服务已停止，正在重启…", "Recognition service stopped. Restarting…")
                self.detail = String(result.error.suffix(2000))
                self.paths.recordLog("Daemon exited: \(result.code); \(String(result.error.suffix(500)))")
            }
        } catch {
            daemon = nil; status = L("无法启动识别服务：\(error.localizedDescription)", "Couldn’t start the recognition service: \(error.localizedDescription)")
            restartAt = Date().addingTimeInterval(10)
        }
    }

    func checkHealth() {
        guard !stopped, !installing, let config else { return }
        guard daemon != nil else { startDaemon(); return }
        guard healthTask == nil, !daemonRetiring else { return }
        let task = ManagedTask()
        healthTask = task
        var env = paths.environment(config)
        env["ZH_VOICE_HEALTH_TIMEOUT"] = "2"
        do {
            try task.start(paths: paths, executable: URL(fileURLWithPath: "/bin/bash"),
                           arguments: [paths.resource("zh-voice").path, "--health"], environment: env) { [weak self, weak task] result in
                guard let self, let task, self.healthTask === task else { return }
                self.healthTask = nil
                let valid = result.code == 0 && result.output.contains("rev=\(config.revision)")
                    && result.output.contains("tok=pinned") && result.output.contains(config.model)
                if self.ready != valid { self.ready = valid }
                if valid {
                    let status = L("已就绪 · Qwen3-ASR \(config.profile == "small" ? "0.6B" : "1.7B")", "Ready · Qwen3-ASR \(config.profile == "small" ? "0.6B" : "1.7B")")
                    if self.status != status { self.status = status }
                    self.failures = 0
                }
                else if Date().timeIntervalSince(self.startedAt) > 190 {
                    self.status = L("识别服务无响应，正在重启…", "Recognition service isn’t responding. Restarting…")
                    self.daemon?.cancel()  // An unresponsive service is a failure; keep the backoff.
                }
            }
        } catch { healthTask = nil; ready = false; status = L("无法检查识别服务：\(error.localizedDescription)", "Couldn’t check the recognition service: \(error.localizedDescription)") }
    }

    func shutdown() {
        stopped = true; ready = false
        setupTask?.cancel(); setupTask = nil
        healthTask?.cancel(); healthTask = nil
        stopDaemon()
    }

    private func stopDaemon() {
        guard let daemon else { return }
        daemonRetiring = true
        daemon.cancel()
    }

    func recover() {
        ready = false
        status = L("正在重启识别服务…", "Restarting the recognition service…")
        healthTask?.cancel(); healthTask = nil
        if daemon != nil { stopDaemon() }
        else { restartAt = .distantPast; startDaemon() }
    }
}
