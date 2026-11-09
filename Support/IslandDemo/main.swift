// Records the notch overlay animation for the README.
// The window sits off screen, so the recording never shows the overlay on the desktop.
// Usage: island-demo <output-folder> light|dark
import AppKit
import CoreMedia
import ScreenCaptureKit
import SwiftUI

func L(_ chinese: String, _ english: String) -> String { english }

final class FrameSink: NSObject, SCStreamOutput {
    var frames: [CGImage] = []
    var times: [Double] = []
    private let context = CIContext()

    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, let pixels = buffer.imageBuffer else { return }
        let image = CIImage(cvPixelBuffer: pixels)
        guard let frame = context.createCGImage(image, from: image.extent) else { return }
        frames.append(frame)
        times.append(buffer.presentationTimeStamp.seconds)
    }
}

/// The top of a MacBook lid: aluminium rim, rounded lid corners, black bezel, display
/// with rounded top corners, wallpaper, and menu bar. The overlay hangs from the bezel.
/// The display is 1512 points wide, the default resolution of a 14-inch MacBook Pro,
/// so the notch and the overlay have their real size relative to the screen.
struct MacTop: View {
    @ObservedObject var model: IslandModel
    let dark: Bool
    static let margin: CGFloat = 14
    static let bezel: CGFloat = 16
    static let display: CGFloat = 1512
    static let lidWidth: CGFloat = display + bezel * 2
    static let width: CGFloat = lidWidth + margin * 2
    static func height(_ model: IslandModel) -> CGFloat { margin + bezel + model.notchHeight + 54 + 34 }

    var body: some View {
        let ink = dark ? Color.white : Color.black
        let page = dark ? Color(red: 0.05, green: 0.07, blue: 0.09) : Color.white
        let lid = UnevenRoundedRectangle(topLeadingRadius: 34, topTrailingRadius: 34, style: .continuous)
        ZStack(alignment: .top) {
            page
            ZStack(alignment: .top) {
                lid.fill(Color(white: 0.05))
                lid.strokeBorder(dark ? Color(white: 0.30) : Color(red: 0.78, green: 0.79, blue: 0.80), lineWidth: 2)
                ZStack(alignment: .top) {
                    LinearGradient(colors: dark
                        ? [Color(red: 0.12, green: 0.13, blue: 0.19), Color(red: 0.21, green: 0.16, blue: 0.25)]
                        : [Color(red: 0.84, green: 0.88, blue: 0.95), Color(red: 0.96, green: 0.92, blue: 0.94)],
                        startPoint: .topLeading, endPoint: .bottomTrailing)
                    HStack(spacing: 16) {
                        Image(systemName: "apple.logo").font(.system(size: 15))
                        Text("Finder").fontWeight(.bold)
                        ForEach(["File", "Edit", "View", "Go", "Window", "Help"], id: \.self) { Text($0) }
                        Spacer()
                        RoundedRectangle(cornerRadius: 3.5, style: .continuous).stroke(ink, lineWidth: 1.5).frame(width: 13, height: 13)
                        Image(systemName: "wifi")
                        Image(systemName: "battery.75percent")
                        Text("Mon 9:41")
                    }
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(ink.opacity(0.85))
                    .padding(.horizontal, 18)
                    .frame(height: model.notchHeight)
                    .background(dark ? Color.black.opacity(0.18) : Color.white.opacity(0.32))
                }
                .clipShape(UnevenRoundedRectangle(topLeadingRadius: 18, topTrailingRadius: 18, style: .continuous))
                .padding([.top, .horizontal], Self.bezel)
                IslandView(model: model).padding(.top, Self.bezel)
            }
            .frame(width: Self.lidWidth)
            .padding(.top, Self.margin)
            // The lid continues below the frame; fade it into the page.
            LinearGradient(colors: [page.opacity(0), page], startPoint: .top, endPoint: .bottom)
                .frame(height: 30)
                .frame(maxHeight: .infinity, alignment: .bottom)
        }
        .frame(width: Self.width, height: Self.height(model))
    }
}

@MainActor func record(to folder: URL, dark: Bool) async throws {
    let model = IslandModel()
    model.notchWidth = 179
    model.notchHeight = 32
    let height = MacTop.height(model)
    let window = NSWindow(contentRect: NSRect(x: -4000, y: -4000, width: MacTop.width, height: height),
                          styleMask: .borderless, backing: .buffered, defer: false)
    window.backgroundColor = dark ? NSColor(red: 0.05, green: 0.07, blue: 0.09, alpha: 1) : .white
    window.contentView = NSHostingView(rootView: MacTop(model: model, dark: dark))
    window.orderFrontRegardless()
    try await Task.sleep(nanoseconds: 300_000_000)

    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    guard let target = content.windows.first(where: { $0.windowID == CGWindowID(window.windowNumber) }) else {
        throw NSError(domain: "IslandDemo", code: 1, userInfo: [NSLocalizedDescriptionKey: "Window not found. Allow Screen Recording for this terminal."])
    }
    let configuration = SCStreamConfiguration()
    configuration.width = Int(MacTop.width * 2)
    configuration.height = Int(height * 2)
    configuration.minimumFrameInterval = CMTime(value: 1, timescale: 60)
    configuration.showsCursor = false
    let sink = FrameSink()
    let stream = SCStream(filter: SCContentFilter(desktopIndependentWindow: target), configuration: configuration, delegate: nil)
    try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: .main)
    try await stream.startCapture()

    func wait(_ seconds: Double) async { try? await Task.sleep(nanoseconds: UInt64(seconds * 1e9)) }
    // The same calls as IslandController.show and hide.
    func show(_ mode: IslandModel.Mode, _ text: String = "") {
        if !model.contentVisible { model.mode = mode; model.text = text }
        else { withAnimation(.easeOut(duration: 0.22)) { model.mode = mode; model.text = text } }
        withAnimation(.spring(response: 0.42, dampingFraction: 0.80)) { model.progress = 1 }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
            withAnimation(.easeOut(duration: 0.30)) { model.contentVisible = true }
        }
    }
    func hide() {
        withAnimation(.easeOut(duration: 0.12)) { model.contentVisible = false }
        withAnimation(.spring(response: 0.38, dampingFraction: 1)) { model.progress = 0 }
    }

    await wait(0.3)
    let start = Date()
    show(.recording)
    // A synthetic speech envelope at the 20 Hz rate of the real level file.
    let tick = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
        MainActor.assumeIsolated {
            let t = Date().timeIntervalSince(start)
            let level = CGFloat(max(0, min(1, 0.45 + 0.4 * sin(t * 9) * sin(t * 2.3))))
            let previous = model.levels.last ?? 0
            model.levels.removeFirst()
            model.levels.append(previous + (level - previous) * 0.65)
            model.elapsed = String(format: "%.1fs", t)
        }
    }
    await wait(2.6)
    tick.invalidate()
    show(.transcribing)
    await wait(1.3)
    model.doneStart = Date()
    show(.done, "Pasted")
    await wait(0.92)
    hide()
    await wait(0.9)
    try await stream.stopCapture()

    try? FileManager.default.removeItem(at: folder)
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    for (index, frame) in sink.frames.enumerated() {
        let data = NSBitmapImageRep(cgImage: frame).representation(using: .png, properties: [:])!
        try data.write(to: folder.appendingPathComponent(String(format: "%04d.png", index)))
    }
    try sink.times.map { String($0) }.joined(separator: "\n")
        .write(to: folder.appendingPathComponent("times.txt"), atomically: true, encoding: .utf8)
}

MainActor.assumeIsolated {
    let arguments = CommandLine.arguments
    guard arguments.count == 3, ["light", "dark"].contains(arguments[2]) else {
        FileHandle.standardError.write(Data("usage: island-demo <output-folder> light|dark\n".utf8)); exit(64)
    }
    NSApplication.shared.setActivationPolicy(.prohibited)
    Task { @MainActor in
        do { try await record(to: URL(fileURLWithPath: arguments[1]), dark: arguments[2] == "dark"); exit(0) }
        catch { FileHandle.standardError.write(Data("island-demo: \(error.localizedDescription)\n".utf8)); exit(1) }
    }
    NSApplication.shared.run()
}
