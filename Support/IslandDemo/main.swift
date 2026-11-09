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

@MainActor func record(to folder: URL, dark: Bool) async throws {
    let model = IslandModel()
    model.notchWidth = 179
    model.notchHeight = 32
    let height = model.notchHeight + 54
    let window = NSWindow(contentRect: NSRect(x: -4000, y: -4000, width: 344, height: height),
                          styleMask: .borderless, backing: .buffered, defer: false)
    window.backgroundColor = dark ? NSColor(red: 0.11, green: 0.11, blue: 0.12, alpha: 1)
                                  : NSColor(red: 0.96, green: 0.96, blue: 0.97, alpha: 1)
    window.contentView = NSHostingView(rootView: IslandView(model: model))
    window.orderFrontRegardless()
    try await Task.sleep(nanoseconds: 300_000_000)

    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    guard let target = content.windows.first(where: { $0.windowID == CGWindowID(window.windowNumber) }) else {
        throw NSError(domain: "IslandDemo", code: 1, userInfo: [NSLocalizedDescriptionKey: "Window not found. Allow Screen Recording for this terminal."])
    }
    let configuration = SCStreamConfiguration()
    configuration.width = 688
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
