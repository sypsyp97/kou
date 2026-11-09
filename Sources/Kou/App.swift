import AppKit
import AVFoundation
import Combine
import ServiceManagement
import SwiftUI

@MainActor final class AppModel: ObservableObject {
    let runtime: RuntimeManager
    let capture: CaptureController
    let island: IslandController
    let hotkey = HotkeyMonitor()
    @Published var accessibility = AXIsProcessTrusted()
    @Published var microphone = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
    @Published var hotkeyAvailable = false
    @Published var loginEnabled = SMAppService.mainApp.status == .enabled
    @Published var loginMessage = ""
    @Published var showMenu = UserDefaults.standard.bool(forKey: "showMenu")
    @Published var profile = RuntimeManager.recommendedProfile
    @Published var termsText = "" { didSet { if termsText != oldValue { termsMessage = "" } } }
    @Published var savedTerms = ""
    @Published var termsMessage = ""
    @Published var terminating = false
    private var resumeAfterInstall = false
    private var permissionTimer: Timer?
    var menuChanged: (() -> Void)?

    init(paths: KouPaths) {
        runtime = RuntimeManager(paths: paths)
        island = IslandController()
        capture = CaptureController(runtime: runtime, island: island)
        termsText = (try? String(contentsOf: paths.terms, encoding: .utf8)) ?? ""
        savedTerms = termsText
        hotkey.onTrigger = { [weak self] in self?.capture.toggle() }
        hotkey.onAvailability = { [weak self] available in self?.hotkeyAvailable = available }
        capture.onEnabledChanged = { [weak self] active in self?.hotkey.setActive(active) }
    }

    func start() {
        runtime.paths.sweepRecordings()
        runtime.load()
        profile = runtime.config?.profile ?? RuntimeManager.recommendedProfile
        hotkey.start()
        permissionTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                // Publish only real changes: every publish makes open settings panes lay out again.
                let accessibility = AXIsProcessTrusted()
                let microphone = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
                let loginEnabled = SMAppService.mainApp.status == .enabled
                if self.accessibility != accessibility { self.accessibility = accessibility }
                if self.microphone != microphone { self.microphone = microphone }
                if self.loginEnabled != loginEnabled { self.loginEnabled = loginEnabled }
                if self.capture.enabled && (!self.accessibility || !self.microphone) { self.capture.setEnabled(false) }
                if self.resumeAfterInstall, !self.runtime.installing, self.runtime.ready {
                    self.resumeAfterInstall = false
                    UserDefaults.standard.set(true, forKey: "enabled")
                }
                if UserDefaults.standard.bool(forKey: "enabled"), !self.capture.enabled, self.runtime.ready {
                    self.capture.setEnabled(true)
                }
            }
        }
    }

    var needsSetup: Bool { runtime.config == nil || !microphone || !accessibility }

    func requestMicrophone() {
        NSApp.activate(ignoringOtherApps: true)
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            DispatchQueue.main.async { self?.microphone = granted }
        }
        if AVCaptureDevice.authorizationStatus(for: .audio) == .denied { openPrivacy("Privacy_Microphone") }
    }

    func requestAccessibility() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        accessibility = AXIsProcessTrustedWithOptions(options)
        if !accessibility { openPrivacy("Privacy_Accessibility") }
    }

    func openPrivacy(_ anchor: String) {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(anchor)") { NSWorkspace.shared.open(url) }
    }

    /// Downloads the selected model. Dictation pauses during the download and
    /// resumes once the new model passes its health check.
    func installModel() {
        resumeAfterInstall = capture.enabled || UserDefaults.standard.bool(forKey: "enabled")
        capture.setEnabled(false)
        runtime.install(profile: profile)
    }

    /// Stops a download and goes back to the installed model, if there is one.
    func cancelInstall() {
        runtime.shutdown()
        runtime.load()
        if let installed = runtime.config?.profile { profile = installed }
    }

    func setLogin(_ value: Bool) {
        do {
            if value { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
            loginEnabled = SMAppService.mainApp.status == .enabled
            loginMessage = SMAppService.mainApp.status == .requiresApproval ? L("请在“系统设置” > “通用” > “登录项”中允许 Kou。", "Allow Kou in System Settings > General > Login Items.") : ""
        } catch { loginMessage = error.localizedDescription; loginEnabled = SMAppService.mainApp.status == .enabled }
    }

    /// "system", or the language Kou uses instead of the system language.
    /// Stored as the app's AppleLanguages, the same key that the per-app language
    /// setting in System Settings writes.
    var language: String {
        let domain = UserDefaults.standard.persistentDomain(forName: Bundle.main.bundleIdentifier ?? "")
        return (domain?["AppleLanguages"] as? [String])?.first ?? "system"
    }

    func setLanguage(_ value: String) {
        guard value != language else { return }
        if value == "system" { UserDefaults.standard.removeObject(forKey: "AppleLanguages") }
        else { UserDefaults.standard.set([value], forKey: "AppleLanguages") }
        UserDefaults.standard.set(true, forKey: "openSettingsAtLaunch")
        // Reopen after this process has exited, so the new instance is not
        // mistaken for a second copy of Kou.
        let relaunch = Process()
        relaunch.executableURL = URL(fileURLWithPath: "/bin/sh")
        relaunch.arguments = ["-c", "while kill -0 \(getpid()) 2>/dev/null; do sleep 0.2; done; open \"$0\"", Bundle.main.bundlePath]
        try? relaunch.run()
        NSApp.terminate(nil)
    }

    func setMenu(_ value: Bool) { showMenu = value; UserDefaults.standard.set(value, forKey: "showMenu"); menuChanged?() }

    var termsDirty: Bool { termsText != savedTerms }

    var termCount: Int { countTerms(termsText) }

    func saveTerms() {
        do {
            try Data(termsText.utf8).write(to: runtime.paths.terms, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: runtime.paths.terms.path)
            try? FileManager.default.removeItem(at: runtime.paths.cache.appendingPathComponent("terms.cache"))
            savedTerms = termsText
            termsMessage = L("已保存，下次听写时生效。", "Saved. Changes apply to your next dictation.")
        } catch { termsMessage = L("无法保存：\(error.localizedDescription)", "Couldn’t save: \(error.localizedDescription)") }
    }

    func copyLastTranscript() {
        guard !capture.lastTranscript.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(capture.lastTranscript, forType: .string)
    }

    func shutdown() {
        terminating = true
        permissionTimer?.invalidate(); capture.shutdown(); hotkey.stop(); runtime.shutdown()
    }
}

/// Matches the parser in zh-voice: one term per line; `#` starts a comment; duplicates count once.
func countTerms(_ text: String) -> Int {
    Set(text.split(whereSeparator: \.isNewline).map { line in
        (line.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false).first ?? "")
            .trimmingCharacters(in: .whitespaces)
    }.filter { !$0.isEmpty }).count
}

enum ModelOption: String, CaseIterable, Identifiable {
    case large, small
    var id: Self { self }
    var name: String { self == .large ? "Qwen3-ASR 1.7B" : "Qwen3-ASR 0.6B" }
    var size: String { self == .large ? L("约 2.5 GB", "about 2.5 GB") : L("约 1.0 GB", "about 1.0 GB") }
    var summary: String { self == .large ? L("识别更准确，适合大多数 Mac。约 2.5 GB。", "More accurate. Best for most Macs. About 2.5 GB.") : L("速度更快，适合 M1 芯片的 Mac。约 1.0 GB。", "Faster. Best for Macs with M1. About 1.0 GB.") }
}

struct KeyCaps: View {
    let keys: [String]
    var body: some View {
        HStack(spacing: 4) {
            ForEach(keys, id: \.self) { key in
                Text(key)
                    .font(.system(size: 12, weight: .medium))
                    .padding(.horizontal, 7)
                    .frame(minWidth: 24, minHeight: 22)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 5, style: .continuous))
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// A form row with a title, an optional secondary description, and trailing content.
struct DetailRow<Trailing: View>: View {
    let title: String
    var detail: String = ""
    @ViewBuilder var trailing: Trailing
    var body: some View {
        LabeledContent {
            trailing
        } label: {
            Text(title)
            if !detail.isEmpty { Text(detail) }
        }
    }
}

// MARK: - Settings panes

struct GeneralPane: View {
    @ObservedObject var model: AppModel
    @ObservedObject var runtime: RuntimeManager
    @ObservedObject var capture: CaptureController

    init(model: AppModel) { self.model = model; runtime = model.runtime; capture = model.capture }

    private var installed: String? { runtime.config?.profile }

    private var enableDetail: String {
        if runtime.config == nil { return L("请先下载下方的语音识别模型。", "Download a speech model below first.") }
        if !model.microphone || !model.accessibility { return L("请先允许下方列出的权限。", "Allow the permissions below first.") }
        // When dictation is off, activity holds the reason it could not turn on.
        return capture.enabled || capture.activity == L("听写已关闭", "Dictation is off") ? "" : capture.activity
    }

    var body: some View {
        Form {
            Section {
                Toggle(isOn: Binding(get: { capture.enabled }, set: capture.setEnabled)) {
                    Text(L("启用听写", "Enable Dictation"))
                    if !enableDetail.isEmpty { Text(enableDetail) }
                }
                .disabled(!capture.enabled && model.needsSetup)
                DetailRow(title: L("开始或结束听写", "Start or Stop Dictation"), detail: L("也可以按 ⌥ Space。单次最长 2 分钟。", "You can also press ⌥ Space. Each dictation can last up to 2 minutes.")) {
                    KeyCaps(keys: ["fn", "control"])
                }
            }
            if !model.microphone || !model.accessibility {
                Section(L("需要权限", "Permissions Required")) {
                    if !model.microphone {
                        DetailRow(title: L("麦克风", "Microphone"), detail: L("用于录下你说的话。", "Kou records what you say.")) {
                            Button(L("允许…", "Allow…")) { model.requestMicrophone() }
                        }
                    }
                    if !model.accessibility {
                        DetailRow(title: L("辅助功能", "Accessibility"), detail: L("用于响应快捷键，并把文字输入到当前 App。", "Kou responds to the shortcut and types text into the current app.")) {
                            Button(L("打开系统设置…", "Open System Settings…")) { model.requestAccessibility() }
                        }
                    }
                }
            }
            Section(L("语音识别模型", "Speech Model")) {
                Picker(L("模型", "Model"), selection: $model.profile) {
                    ForEach(ModelOption.allCases) { option in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(option.name + (option.rawValue == RuntimeManager.recommendedProfile ? L("（推荐）", " (Recommended)") : ""))
                            Text(option.summary).font(.callout).foregroundStyle(.secondary)
                        }
                        .tag(option.rawValue)
                    }
                }
                .pickerStyle(.radioGroup)
                .labelsHidden()
                .disabled(runtime.installing)
                DetailRow(title: runtime.status, detail: L("模型保存在这台 Mac 上，听写时不联网。", "The model stays on this Mac. Dictation works offline.")) {
                    if runtime.installing {
                        HStack(spacing: 10) {
                            ProgressView().controlSize(.small)
                            Button(L("取消", "Cancel")) { model.cancelInstall() }
                        }
                    } else if installed != model.profile {
                        Button(installed == nil ? L("下载", "Download") : L("下载并切换", "Download and Switch")) { model.installModel() }
                    } else if !runtime.ready {
                        Button(L("重新检查", "Check Again")) { runtime.checkHealth() }
                    }
                }
            }
            Section {
                Picker(selection: Binding(get: { model.language }, set: model.setLanguage)) {
                    Text(L("跟随系统", "System")).tag("system")
                    Text("简体中文").tag("zh-Hans")
                    Text("English").tag("en")
                } label: {
                    Text(L("语言", "Language"))
                    Text(L("更改后 Kou 会重新打开。", "Kou reopens after you change it."))
                }
                Toggle(isOn: Binding(get: { model.loginEnabled }, set: model.setLogin)) {
                    Text(L("登录时打开 Kou", "Open Kou at Login"))
                    if !model.loginMessage.isEmpty { Text(model.loginMessage) }
                }
                Toggle(isOn: Binding(get: { model.showMenu }, set: model.setMenu)) {
                    Text(L("在菜单栏中显示 Kou", "Show Kou in Menu Bar"))
                    Text(L("隐藏后，再次打开 Kou 即可显示此窗口。", "When hidden, open Kou again to show this window."))
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 520, height: 560)
        .disabled(model.terminating)
    }
}

struct VocabularyPane: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Form {
            Section {
                Text(L("添加人名、产品名或专业术语，Kou 会优先识别这些写法。", "Add names, product names, or technical terms. Kou prefers these spellings."))
                    .foregroundStyle(.secondary)
                TextEditor(text: $model.termsText)
                    .font(.system(.body, design: .monospaced))
                    .scrollContentBackground(.hidden)
                    .frame(height: 300)
                Text(L("每行一个词，词里可以有空格（如 Visual Studio Code）。# 之后的内容会被忽略。", "Enter one term per line. A term can contain spaces, such as Visual Studio Code. Kou ignores text after #."))
                    .font(.callout).foregroundStyle(.secondary)
            } header: {
                HStack {
                    Text(L("词汇表", "Vocabulary"))
                    Spacer()
                    Text(L("\(model.termCount) 个词", "\(model.termCount) terms")).monospacedDigit()
                }
            }
            Section {
                LabeledContent {
                    Button(L("保存", "Save")) { model.saveTerms() }
                        .keyboardShortcut("s", modifiers: .command)
                        .disabled(!model.termsDirty)
                } label: {
                    Text(!model.termsMessage.isEmpty ? model.termsMessage : (model.termsDirty ? L("有未保存的更改", "Unsaved changes") : L("所有更改均已保存", "All changes saved")))
                        .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 520, height: 560)
        .disabled(model.terminating)
    }
}

// MARK: - Settings window

enum SettingsPane: Int, CaseIterable, Identifiable {
    case general, vocabulary
    var id: Int { rawValue }
    var title: String { self == .general ? L("通用", "General") : L("词汇", "Vocabulary") }
    var symbol: String { self == .general ? "gearshape" : "character.book.closed" }
}

/// One capsule per pane. The selected capsule is Liquid Glass and slides to the new pane.
struct PaneSwitcher: View {
    let selection: SettingsPane
    let select: (SettingsPane) -> Void
    @Namespace private var thumb

    var body: some View {
        HStack(spacing: 2) {
            ForEach(SettingsPane.allCases) { pane in
                let selected = pane == selection
                Button { select(pane) } label: {
                    Label(pane.title, systemImage: pane.symbol)
                        .font(.system(size: 13, weight: selected ? .semibold : .regular))
                        .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                        .padding(.horizontal, 16)
                        .frame(minWidth: 112, minHeight: 30)
                        .contentShape(Capsule())
                        .background {
                            if selected { Thumb().matchedGeometryEffect(id: "thumb", in: thumb) }
                        }
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(selected ? .isSelected : [])
            }
        }
        .padding(3)
        .background(.quaternary.opacity(0.6), in: Capsule())
    }

    private struct Thumb: View {
        var body: some View {
            if #available(macOS 26, *) {
                Capsule().fill(.clear).glassEffect(.regular.interactive(), in: Capsule())
            } else {
                Capsule().fill(.background).shadow(color: .black.opacity(0.12), radius: 2, y: 1)
            }
        }
    }
}

/// The selected settings pane. `forward` changes one update before `pane`, because
/// the outgoing pane keeps the transition from its last update.
@MainActor final class SettingsState: ObservableObject {
    @Published private(set) var pane: SettingsPane
    @Published private(set) var forward = true

    init() { pane = SettingsPane(rawValue: UserDefaults.standard.integer(forKey: "settingsPane")) ?? .general }

    func select(_ next: SettingsPane) {
        guard next != pane else { return }
        forward = next.rawValue > pane.rawValue
        UserDefaults.standard.set(next.rawValue, forKey: "settingsPane")
        DispatchQueue.main.async {
            withAnimation(.spring(response: 0.4, dampingFraction: 0.82)) { self.pane = next }
        }
    }
}

/// Settings: the pane switcher on top, and the selected pane below. A pane slides in
/// from the side of its capsule while the old pane slides out and blurs.
struct SettingsView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var state: SettingsState

    var body: some View {
        VStack(spacing: 0) {
            PaneSwitcher(selection: state.pane, select: state.select)
            // Below the title bar, which would take clicks as window drags.
            .padding(.top, 30)
            .padding(.bottom, 2)
            ZStack {
                switch state.pane {
                case .general: GeneralPane(model: model).transition(slide)
                case .vocabulary: VocabularyPane(model: model).transition(slide)
                }
            }
            .frame(width: 520, height: 560)
            .clipped()
        }
    }

    private var slide: AnyTransition {
        let shift: CGFloat = state.forward ? 36 : -36
        return .asymmetric(
            insertion: .modifier(active: PaneShift(x: shift, amount: 1), identity: PaneShift(x: 0, amount: 0)),
            removal: .modifier(active: PaneShift(x: -shift, amount: 1), identity: PaneShift(x: 0, amount: 0)))
    }
}

private struct PaneShift: ViewModifier {
    let x: CGFloat
    let amount: CGFloat
    func body(content: Content) -> some View {
        content.offset(x: x).opacity(1 - amount).blur(radius: 4 * amount)
    }
}

// MARK: - Onboarding

struct OnboardingView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var runtime: RuntimeManager
    let finish: () -> Void

    init(model: AppModel, finish: @escaping () -> Void) {
        self.model = model; runtime = model.runtime; self.finish = finish
    }

    private var option: ModelOption { ModelOption(rawValue: model.profile) ?? .large }
    private var modelDone: Bool { runtime.config != nil }
    private var allDone: Bool { modelDone && model.microphone && model.accessibility }

    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: 8) {
                Image(nsImage: NSApp.applicationIconImage)
                    .resizable().frame(width: 88, height: 88)
                Text(L("欢迎使用 Kou", "Welcome to Kou")).font(.largeTitle.weight(.semibold))
                Text(L("按 fn + control 说话，再按一次，文字就会出现在光标处。识别全程在本机完成。", "Press fn + control and speak. Press again, and the text appears at the cursor. Recognition runs entirely on this Mac."))
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: 400)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.top, 28)
            Form {
                Section {
                    step(1, done: modelDone, title: L("下载语音识别模型", "Download the Speech Model"),
                         detail: runtime.installing || runtime.status != RuntimeManager.notDownloaded
                             ? runtime.status : L("\(option.name)，\(option.size)。只需下载一次。", "\(option.name), \(option.size). You download it only once.")) {
                        if runtime.installing { ProgressView().controlSize(.small) }
                        else { Button(L("下载", "Download")) { model.installModel() } }
                    }
                    step(2, done: model.microphone, title: L("允许使用麦克风", "Allow Microphone Access"), detail: L("用于录下你说的话。", "Kou records what you say.")) {
                        Button(L("允许…", "Allow…")) { model.requestMicrophone() }
                    }
                    step(3, done: model.accessibility, title: L("允许辅助功能", "Allow Accessibility Access"),
                         detail: L("用于响应快捷键，并把文字输入到当前 App。在列表中打开 Kou 即可。", "Kou responds to the shortcut and types text into the current app. Turn on Kou in the list.")) {
                        Button(L("打开系统设置…", "Open System Settings…")) { model.requestAccessibility() }
                    }
                }
            }
            .formStyle(.grouped)
            .scrollDisabled(true)
            HStack {
                Spacer()
                let start = Button(L("开始使用", "Get Started")) { finish() }
                    .controlSize(.large)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!allDone)
                if #available(macOS 26, *) { start.buttonStyle(.glassProminent) } else { start }
            }
            .padding(.horizontal, 20).padding(.bottom, 20)
        }
        .frame(width: 540, height: 500)
        .disabled(model.terminating)
    }

    private func step<Action: View>(_ number: Int, done: Bool, title: String, detail: String,
                                    @ViewBuilder action: () -> Action) -> some View {
        HStack(spacing: 12) {
            Image(systemName: done ? "checkmark.circle.fill" : "\(number).circle")
                .font(.title2)
                .foregroundStyle(done ? Color.green : Color.secondary)
                .contentTransition(.symbolEffect(.replace))
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                Text(detail).font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            if !done { action() }
        }
        .padding(.vertical, 4)
        .animation(.smooth, value: done)
    }
}

// MARK: - App delegate

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, NSWindowDelegate {
    var model: AppModel?
    private var settingsWindow: NSWindow?
    private var onboardingWindow: NSWindow?
    private var statusItem: NSStatusItem?
    private var observations: [NSObjectProtocol] = []
    private var cancellables: Set<AnyCancellable> = []
    private var terminationPending = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        UserDefaults.standard.register(defaults: ["showMenu": true])
        do {
            let paths = try KouPaths()
            model = AppModel(paths: paths)
            guard let model else { return }
            installMainMenu()
            model.menuChanged = { [weak self] in self?.updateMenu() }
            model.capture.onRecordingStarted = { [weak self] in
                self?.settingsWindow?.close(); self?.onboardingWindow?.close()
            }
            model.capture.$enabled.sink { [weak self] enabled in self?.updateStatusIcon(enabled: enabled) }.store(in: &cancellables)
            model.start()
            updateMenu()
            let center = NSWorkspace.shared.notificationCenter
            observations.append(center.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak model] _ in
                Task { @MainActor in model?.capture.sleep() }
            })
            for name in [NSWorkspace.didWakeNotification, NSWorkspace.screensDidWakeNotification] {
                observations.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak model] _ in
                    Task { @MainActor in
                        model?.island.rebuild(); model?.capture.wake(); model?.runtime.checkHealth()
                    }
                })
            }
            observations.append(NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak model] _ in
                Task { @MainActor in model?.island.rebuild() }
            })
            DistributedNotificationCenter.default().addObserver(self, selector: #selector(showMainWindow), name: NSNotification.Name("KouShowSettings"), object: nil)
            if UserDefaults.standard.bool(forKey: "openSettingsAtLaunch") {
                UserDefaults.standard.removeObject(forKey: "openSettingsAtLaunch")
                showMainWindow()
            } else if model.needsSetup || !UserDefaults.standard.bool(forKey: "enabled") { showMainWindow() }
        } catch {
            let alert = NSAlert(); alert.messageText = L("Kou 无法启动", "Kou Can’t Start"); alert.informativeText = error.localizedDescription
            alert.runModal(); NSApp.terminate(nil)
        }
    }

    // An accessory app has no visible menu bar, but key equivalents still route
    // through the main menu; without it ⌘C/⌘V/⌘W do nothing in Kou's windows.
    private func installMainMenu() {
        let main = NSMenu()
        let app = NSMenu()
        app.addItem(withTitle: L("设置…", "Settings…"), action: #selector(openSettings), keyEquivalent: ",").target = self
        app.addItem(withTitle: L("退出 Kou", "Quit Kou"), action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let edit = NSMenu(title: L("编辑", "Edit"))
        edit.addItem(withTitle: L("撤销", "Undo"), action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: L("重做", "Redo"), action: Selector(("redo:")), keyEquivalent: "z").keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: L("剪切", "Cut"), action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: L("拷贝", "Copy"), action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: L("粘贴", "Paste"), action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: L("全选", "Select All"), action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let windowMenu = NSMenu(title: L("窗口", "Window"))
        windowMenu.addItem(withTitle: L("关闭", "Close"), action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowMenu.addItem(withTitle: L("最小化", "Minimize"), action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        for submenu in [app, edit, windowMenu] {
            let item = NSMenuItem(); item.submenu = submenu; main.addItem(item)
        }
        NSApp.mainMenu = main
    }

    @objc func showMainWindow() {
        guard let model else { return }
        if model.needsSetup { openOnboarding() } else { openSettings() }
    }

    private func openOnboarding() {
        guard let model, !model.terminating else { return }
        if onboardingWindow == nil {
            let view = OnboardingView(model: model) { [weak self] in
                UserDefaults.standard.set(true, forKey: "enabled")
                self?.onboardingWindow?.close()
            }
            let window = NSWindow(contentViewController: NSHostingController(rootView: view))
            window.styleMask = [.titled, .closable, .fullSizeContentView]
            window.titlebarAppearsTransparent = true
            window.titleVisibility = .hidden
            window.isMovableByWindowBackground = true
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.center()
            onboardingWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        onboardingWindow?.makeKeyAndOrderFront(nil)
    }

    @objc func openSettings() {
        guard let model, !model.terminating else { return }
        if settingsWindow == nil {
            let view = SettingsView(model: model, state: SettingsState())
            let window = NSWindow(contentViewController: NSHostingController(rootView: view))
            window.styleMask = [.titled, .closable, .fullSizeContentView]
            window.titlebarAppearsTransparent = true
            window.titleVisibility = .hidden
            window.title = L("Kou 设置", "Kou Settings")
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.center()
            settingsWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    // A hidden window keeps its SwiftUI panes subscribed to the models, and AppKit
    // keeps observation records for every layout they cause. Release closed windows.
    func windowWillClose(_ notification: Notification) {
        guard let window = notification.object as? NSWindow else { return }
        if window === settingsWindow { settingsWindow = nil }
        if window === onboardingWindow { onboardingWindow = nil }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showMainWindow(); return true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !terminationPending else { return .terminateLater }
        terminationPending = true
        model?.shutdown()
        let timer = Timer(timeInterval: 0.05, repeats: true) { [weak self] timer in
            guard ManagedTask.activeCount == 0 else { return }
            timer.invalidate()
            MainActor.assumeIsolated {
                self?.model?.runtime.paths.sweepRecordings()
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        RunLoop.main.add(timer, forMode: .modalPanel)
        return .terminateLater
    }
    func applicationWillTerminate(_ notification: Notification) { model?.shutdown() }

    private func updateMenu() {
        guard let model else { return }
        if !model.showMenu {
            if let statusItem { NSStatusBar.system.removeStatusItem(statusItem) }
            statusItem = nil; return
        }
        if statusItem == nil {
            statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
            let menu = NSMenu()
            menu.delegate = self
            statusItem?.menu = menu
            menuNeedsUpdate(menu)
        }
        updateStatusIcon(enabled: model.capture.enabled)
    }

    private func updateStatusIcon(enabled: Bool) {
        statusItem?.button?.image = Self.menuBarIcon
        statusItem?.button?.appearsDisabled = !enabled
        statusItem?.button?.setAccessibilityLabel(enabled ? L("Kou 听写已启用", "Kou dictation is on") : L("Kou 听写已关闭", "Kou dictation is off"))
    }

    /// The rounded square from the app icon (口), drawn as a template image.
    private static let menuBarIcon: NSImage = {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { rect in
            let path = NSBezierPath(roundedRect: rect.insetBy(dx: 3.5, dy: 3.5), xRadius: 3, yRadius: 3)
            path.lineWidth = 1.8
            NSColor.black.setStroke()
            path.stroke()
            return true
        }
        image.isTemplate = true
        return image
    }()

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        guard let model else { return }
        if model.needsSetup { menu.addItem(withTitle: L("需要完成设置", "Setup required"), action: nil, keyEquivalent: "").isEnabled = false }
        menu.addItem(withTitle: L("设置…", "Settings…"), action: #selector(showMainWindow), keyEquivalent: ",").target = self
        menu.addItem(.separator())
        if !model.needsSetup {
            let toggle = menu.addItem(withTitle: L("启用听写", "Enable Dictation"), action: #selector(toggleEnabled), keyEquivalent: "")
            toggle.target = self
            toggle.state = model.capture.enabled ? .on : .off
        }
        let copy = menu.addItem(withTitle: L("拷贝上一次听写", "Copy Last Dictation"), action: model.capture.lastTranscript.isEmpty ? nil : #selector(copyLast), keyEquivalent: "")
        copy.target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: L("关于 Kou", "About Kou"), action: #selector(about), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: L("退出 Kou", "Quit Kou"), action: #selector(quit), keyEquivalent: "q").target = self
    }

    @objc private func toggleEnabled() { guard let model, !model.terminating else { return }; model.capture.setEnabled(!model.capture.enabled) }
    @objc private func copyLast() { model?.copyLastTranscript() }
    @objc private func about() { NSApp.activate(ignoringOtherApps: true); NSApp.orderFrontStandardAboutPanel(nil) }
    @objc private func quit() { NSApp.terminate(nil) }
}
