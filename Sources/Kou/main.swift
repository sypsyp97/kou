import AppKit
import Foundation

let usage = L("""
用法：kou [命令]

  （无命令）          打开 Kou
  status             显示听写、模型和识别服务的状态
  terms              用文本编辑器打开词汇表
  transcribe <音频>   转写一个音频文件并输出文字
  log                显示最近的日志
  corpus on|off      是否在本机保留录音和转写文本

""", """
usage: kou [command]

  (no command)        Open Kou
  status              Show dictation, model, and service status
  terms               Open the vocabulary list in a text editor
  transcribe <audio>  Transcribe an audio file and print the text
  log                 Show recent log entries
  corpus on|off       Keep recordings and transcripts on this Mac

""")

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data("kou: \(message)\n".utf8)); exit(code)
}

func loadConfig(_ paths: KouPaths) -> RuntimeConfig? {
    (try? Data(contentsOf: paths.runtime)).flatMap { try? JSONDecoder().decode(RuntimeConfig.self, from: $0) }
}

/// Runs a bundled script with the app's environment, forwards its output, and exits with its status.
@MainActor func runScript(_ paths: KouPaths, _ config: RuntimeConfig?, _ arguments: [String],
                          extra: [String: String] = [:], prefix: String = "") -> Never {
    var env = paths.environment(config)
    env.merge(extra) { $1 }
    do {
        try ManagedTask().start(paths: paths, executable: URL(fileURLWithPath: "/bin/bash"),
                                arguments: [paths.resource("zh-voice").path] + arguments, environment: env) { result in
            let output = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            let error = result.error.trimmingCharacters(in: .whitespacesAndNewlines)
            if !output.isEmpty { print(prefix + output) }
            if !error.isEmpty { FileHandle.standardError.write(Data(((output.isEmpty ? prefix : "") + error + "\n").utf8)) }
            exit(result.code)
        }
    } catch { fail(error.localizedDescription) }
    RunLoop.main.run()
    exit(1)
}

MainActor.assumeIsolated {

let args = Array(CommandLine.arguments.dropFirst())
let identifier = appBundle.bundleIdentifier ?? "io.github.sypsyp97.Kou"

// LaunchServices and debuggers may pass flags such as -psn_… or -NSDocumentRevisionsDebugMode.
let command = args.first.flatMap { $0.hasPrefix("-") && !["--transcribe", "-h", "--help"].contains($0) ? nil : $0 }

switch command {
case nil:
    let others = NSRunningApplication.runningApplications(withBundleIdentifier: identifier)
        .filter { $0.processIdentifier != getpid() }
    if !others.isEmpty {
        DistributedNotificationCenter.default().postNotificationName(NSNotification.Name("KouShowSettings"), object: nil)
        exit(0)
    }
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()

case "transcribe", "--transcribe":
    guard args.count == 2 else { fail(L("用法：kou transcribe <音频文件>", "usage: kou transcribe <audio-file>"), code: 64) }
    do {
        let paths = try KouPaths()
        guard let config = loadConfig(paths) else { fail(L("尚未下载语音识别模型，请先打开 Kou 完成设置。", "Speech model not downloaded. Open Kou to finish setup.")) }
        runScript(paths, config, [args[1]])
    } catch { fail(error.localizedDescription) }

case "status":
    do {
        let paths = try KouPaths()
        let config = loadConfig(paths)
        let running = !NSRunningApplication.runningApplications(withBundleIdentifier: identifier)
            .filter { $0.processIdentifier != getpid() }.isEmpty
        let terms = (try? String(contentsOf: paths.terms, encoding: .utf8)).map(countTerms) ?? 0
        print(L("Kou     ", "Kou        ") + (running ? L("运行中", "running") : L("未运行", "not running")))
        print(L("听写    ", "Dictation  ") + ((appBundle === Bundle.main ? UserDefaults.standard : UserDefaults(suiteName: identifier))?.bool(forKey: "enabled") == true ? L("已启用", "on") : L("已关闭", "off")))
        print(L("模型    ", "Model      ") + (config.flatMap { ModelOption(rawValue: $0.profile)?.name } ?? L("尚未下载", "not downloaded")))
        print(L("词汇    \(terms) 个词", "Vocabulary \(terms) terms"))
        guard config != nil, running else { exit(0) }
        runScript(paths, config, ["--health"], extra: ["ZH_VOICE_HEALTH_TIMEOUT": "2"], prefix: L("服务    ", "Service    "))
    } catch { fail(error.localizedDescription) }

case "terms":
    do {
        let paths = try KouPaths()
        let open = Process()
        open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        open.arguments = ["-t", paths.terms.path]
        try open.run(); open.waitUntilExit()
        print(paths.terms.path)
    } catch { fail(error.localizedDescription) }

case "log":
    do {
        let paths = try KouPaths()
        let text = (try? String(contentsOf: paths.log, encoding: .utf8)) ?? ""
        print(text.split(separator: "\n", omittingEmptySubsequences: false).suffix(40).joined(separator: "\n"))
    } catch { fail(error.localizedDescription) }

case "corpus":
    guard args.count == 2, ["on", "off"].contains(args[1]) else { fail(L("用法：kou corpus on|off", "usage: kou corpus on|off"), code: 64) }
    do {
        let paths = try KouPaths()
        let flag = paths.support.appendingPathComponent("corpus")
        if args[1] == "on" {
            try Data().write(to: flag)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: flag.path)
            let folder = paths.support.appendingPathComponent("corpus-data").path
            print(L("已开启：录音和转写文本保存在 \(folder)", "On. Kou saves recordings and transcripts in \(folder)"))
        } else {
            if FileManager.default.fileExists(atPath: flag.path) { try FileManager.default.removeItem(at: flag) }
            print(L("已关闭：新录音在转写后删除。已保存的内容不会自动删除。", "Off. New recordings are deleted after transcription. Kou keeps saved data until you delete it."))
        }
    } catch { fail(error.localizedDescription) }

case "help", "-h", "--help":
    print(usage, terminator: "")

default:
    FileHandle.standardError.write(Data(usage.utf8)); exit(64)
}
}
