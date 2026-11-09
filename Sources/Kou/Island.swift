import AppKit
import SwiftUI

@MainActor final class IslandModel: ObservableObject {
    enum Mode { case recording, transcribing, message, done }
    @Published var progress: CGFloat = 0
    @Published var contentVisible = false
    @Published var mode: Mode = .message
    @Published var text = ""
    @Published var levels = Array(repeating: CGFloat(0), count: 26)
    @Published var elapsed = "0.0s"
    var doneStart = Date()
    var notchWidth: CGFloat = 160
    var notchHeight: CGFloat = 28
}

struct NotchShape: Shape {
    var progress: CGFloat
    let closedWidth: CGFloat
    let closedHeight: CGFloat
    var animatableData: CGFloat { get { progress } set { progress = newValue } }

    func path(in rect: CGRect) -> Path {
        let p = max(0, (progress - 0.12) / 0.88)
        let width = closedWidth + (304 - closedWidth) * p
        let height = closedHeight + 40 * p
        let top = 6 + 13 * p
        let bottom = 14 + 10 * p
        let left = (rect.width - width) / 2
        let right = (rect.width + width) / 2
        var path = Path()
        path.move(to: CGPoint(x: left, y: 0))
        path.addQuadCurve(to: CGPoint(x: left + top, y: top), control: CGPoint(x: left + top, y: 0))
        path.addLine(to: CGPoint(x: left + top, y: height - bottom))
        path.addQuadCurve(to: CGPoint(x: left + top + bottom, y: height), control: CGPoint(x: left + top, y: height))
        path.addLine(to: CGPoint(x: right - top - bottom, y: height))
        path.addQuadCurve(to: CGPoint(x: right - top, y: height - bottom), control: CGPoint(x: right - top, y: height))
        path.addLine(to: CGPoint(x: right - top, y: top))
        path.addQuadCurve(to: CGPoint(x: right, y: 0), control: CGPoint(x: right - top, y: 0))
        path.closeSubpath()
        return path
    }
}

private let accent = Color(red: 211 / 255, green: 198 / 255, blue: 170 / 255)

private func clamp01(_ value: Double) -> Double { min(1, max(0, value)) }

/// Position of a spring released from 0 toward 1 after `t` seconds.
private func spring(_ t: Double, response: Double, damping: Double) -> Double {
    let w0 = 2 * Double.pi / response
    guard damping < 1 else { return 1 - exp(-w0 * t) * (1 + w0 * t) }
    let wd = w0 * (1 - damping * damping).squareRoot()
    return 1 - exp(-damping * w0 * t) * (cos(wd * t) + damping * w0 / wd * sin(wd * t))
}

/// The thin arc that rotates while Kou transcribes.
private struct SpinRing: View {
    var body: some View {
        TimelineView(.animation) { context in
            let degrees = (context.date.timeIntervalSinceReferenceDate * 300).truncatingRemainder(dividingBy: 360)
            ZStack {
                Circle().stroke(accent.opacity(0.28), lineWidth: 2)
                Circle().trim(from: 0, to: 95 / 360)
                    .stroke(accent, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .rotationEffect(.degrees(degrees))
            }
            .frame(width: 11.2, height: 11.2)
        }
    }
}

/// Completion: the arc slows and closes into a ring, the ring blooms into three
/// staggered halos, the check mark springs in, and the label fades in after it.
private struct DoneMark: View {
    let start: Date
    let label: String

    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSince(start)
            let close = 0.26
            let out = 1 - clamp01((t - 0.80) / 0.12)
            HStack(spacing: 8) {
                ZStack {
                    if t < close {
                        let p = t / close
                        let angle = start.timeIntervalSinceReferenceDate * 300 + 300 * (t - 0.4 * t * t / close)
                        Circle().stroke(accent.opacity(0.28 * (1 - p)), lineWidth: 2)
                        Circle().trim(from: 0, to: (95 + 265 * (1 - (1 - p) * (1 - p))) / 360)
                            .stroke(accent, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                            .rotationEffect(.degrees(angle.truncatingRemainder(dividingBy: 360)))
                    } else {
                        Circle().stroke(accent.opacity(1 - clamp01((t - close) / 0.14)), lineWidth: 2)
                    }
                    ForEach(0..<3, id: \.self) { index in
                        let elapsed = t - [0.26, 0.30, 0.34][index]
                        if elapsed >= 0 {
                            let v = spring(elapsed, response: 0.42, damping: 0.80)
                            let radius = 5.6 + ([10.0, 15.0, 21.0][index] - 5.6) * v
                            let decay = 1 - clamp01((t - 0.54) / 0.22)
                            Circle().fill(accent.opacity([0.30, 0.16, 0.07][index] * clamp01(v) * decay * out))
                                .frame(width: radius * 2, height: radius * 2)
                        }
                    }
                    if t >= 0.28 {
                        let elapsed = t - 0.28
                        Text("✓")
                            .foregroundStyle(accent.opacity(0.38 + 0.62 * clamp01(elapsed / 0.26)))
                            .scaleEffect(0.8 + 0.2 * spring(elapsed, response: 0.30, damping: 0.50))
                            .opacity(clamp01(elapsed / 0.12) * out)
                    }
                }
                .frame(width: 11.2, height: 11.2)
                Text(label).lineLimit(1)
                    .opacity((1 - pow(1 - clamp01((t - 0.34) / 0.20), 3)) * out)
            }
        }
    }
}

struct IslandView: View {
    @ObservedObject var model: IslandModel
    var body: some View {
        ZStack(alignment: .top) {
            NotchShape(progress: model.progress, closedWidth: model.notchWidth,
                       closedHeight: model.notchHeight).fill(.black)
            Group {
                switch model.mode {
                case .recording:
                    HStack(spacing: 12) {
                        HStack(alignment: .center, spacing: 4) {
                            ForEach(0..<26, id: \.self) { index in
                                RoundedRectangle(cornerRadius: 1.5).fill(accent.opacity(0.3 + 0.7 * model.levels[index]))
                                    .frame(width: 3, height: 3 + model.levels[index] * 20)
                            }
                        }
                        Text(model.elapsed).monospacedDigit()
                    }
                case .transcribing:
                    HStack(spacing: 8) { SpinRing(); Text(L("转写中", "Transcribing")) }
                case .done:
                    DoneMark(start: model.doneStart, label: model.text)
                case .message:
                    HStack(spacing: 8) {
                        if model.text.hasPrefix("✓") {
                            Text("✓")
                            Text(model.text.dropFirst().trimmingCharacters(in: .whitespaces)).lineLimit(1)
                        } else {
                            Circle().fill(accent).frame(width: 9, height: 9)
                            Text(model.text).lineLimit(1)
                        }
                    }
                }
            }
            .id(model.mode)
            .transition(.opacity)
            .font(.system(size: 13, weight: .regular, design: .monospaced))
            .foregroundStyle(accent)
            .frame(height: 40)
            .padding(.top, model.notchHeight)
            .opacity(model.contentVisible ? 1 : 0)
            .scaleEffect(model.contentVisible ? 1 : 0.9, anchor: .top)
        }
        .frame(width: 344, height: model.notchHeight + 54, alignment: .top)
        .accessibilityLabel(model.mode == .recording ? L("录音中 \(model.elapsed)", "Recording \(model.elapsed)") : model.text)
    }
}

final class IslandPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

@MainActor final class IslandController {
    let model = IslandModel()
    private var panel: IslandPanel?
    private var tick: Timer?
    private var dismiss: DispatchWorkItem?
    private var reveal: DispatchWorkItem?
    private var sequence = 0

    private func build() {
        guard panel == nil, let screen = NSScreen.screens.first else { return }
        if let left = screen.auxiliaryTopLeftArea, let right = screen.auxiliaryTopRightArea,
           screen.safeAreaInsets.top > 0 {
            model.notchWidth = max(1, right.minX - left.maxX - 6)
            model.notchHeight = screen.safeAreaInsets.top
        } else {
            model.notchWidth = 160
            model.notchHeight = max(24, screen.frame.maxY - screen.visibleFrame.maxY)
        }
        let height = model.notchHeight + 54
        let rect = NSRect(x: screen.frame.midX - 172, y: screen.frame.maxY - height, width: 344, height: height)
        let window = IslandPanel(contentRect: rect, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.ignoresMouseEvents = true
        window.hidesOnDeactivate = false
        window.canHide = false
        window.isExcludedFromWindowsMenu = true
        window.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.screenSaverWindow)))
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        window.contentView = NSHostingView(rootView: IslandView(model: model))
        window.isReleasedWhenClosed = false
        panel = window
    }

    func show(_ mode: IslandModel.Mode, text: String = "") {
        sequence += 1
        dismiss?.cancel(); reveal?.cancel()
        build()
        // Cross-fade between states; the completion animation continues the arc in place.
        if mode == .done || !model.contentVisible { model.mode = mode; model.text = text }
        else { withAnimation(.easeOut(duration: 0.18)) { model.mode = mode; model.text = text } }
        panel?.orderFrontRegardless()
        withAnimation(.spring(response: 0.42, dampingFraction: 0.80)) { model.progress = 1 }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            withAnimation(.easeOut(duration: 0.30)) { self.model.contentVisible = true }
        }
        reveal = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12, execute: work)
    }

    func recording(level: URL) {
        tick?.invalidate()
        model.levels = Array(repeating: 0, count: 26)
        let start = ProcessInfo.processInfo.systemUptime
        show(.recording)
        tick = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                let value = (try? String(contentsOf: level, encoding: .utf8)).flatMap { Double($0.trimmingCharacters(in: .whitespacesAndNewlines)) } ?? 0
                // Smooth toward the new reading so the bars follow syllables without jitter.
                let previous = self.model.levels.last ?? 0
                let current = CGFloat(value.isFinite ? min(1, max(0, value)) : 0)
                self.model.levels.removeFirst()
                self.model.levels.append(previous + (current - previous) * 0.65)
                self.model.elapsed = String(format: "%.1fs", ProcessInfo.processInfo.systemUptime - start)
            }
        }
    }

    func transcribing() { tick?.invalidate(); tick = nil; show(.transcribing) }

    func message(_ text: String, duration: TimeInterval = 2.0) {
        tick?.invalidate(); tick = nil
        if text.hasPrefix("✓"), panel?.isVisible == true, model.progress == 1, model.contentVisible {
            model.doneStart = Date()
            show(.done, text: text.dropFirst().trimmingCharacters(in: .whitespaces))
            let work = DispatchWorkItem { [weak self] in self?.hide() }
            dismiss = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.92, execute: work)
            return
        }
        show(.message, text: text)
        let work = DispatchWorkItem { [weak self] in self?.hide() }
        dismiss = work
        DispatchQueue.main.asyncAfter(deadline: .now() + duration, execute: work)
    }

    func hide(immediately: Bool = false) {
        sequence += 1
        let token = sequence
        tick?.invalidate(); tick = nil
        dismiss?.cancel(); reveal?.cancel()
        if immediately {
            model.contentVisible = false; model.progress = 0; panel?.orderOut(nil)
            return
        }
        withAnimation(.easeOut(duration: 0.12)) { model.contentVisible = false }
        withAnimation(.spring(response: 0.38, dampingFraction: 1)) { model.progress = 0 }
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.sequence == token else { return }
            self.panel?.orderOut(nil)
        }
        dismiss = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.30, execute: work)
    }

    func rebuild() {
        let visible = panel?.isVisible == true
        panel?.orderOut(nil); panel?.close(); panel = nil
        build()
        if visible { panel?.orderFrontRegardless() }
    }
}
