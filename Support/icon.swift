import AppKit
import Foundation

let output = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let unit = CGFloat(pixels) / 1024
        let bounds = NSRect(x: 64 * unit, y: 64 * unit, width: 896 * unit, height: 896 * unit)
        NSColor(red: 24 / 255, green: 43 / 255, blue: 41 / 255, alpha: 1).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 196 * unit, yRadius: 196 * unit).fill()
        let mouth = NSBezierPath(roundedRect: NSRect(x: 304 * unit, y: 296 * unit,
                                                   width: 416 * unit, height: 432 * unit),
                                xRadius: 18 * unit, yRadius: 18 * unit)
        mouth.lineWidth = 70 * unit
        NSColor(red: 211 / 255, green: 198 / 255, blue: 170 / 255, alpha: 1).setStroke()
        mouth.stroke()
        image.unlockFocus()
        guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
              let png = bitmap.representation(using: .png, properties: [:]) else { fatalError("Icon rendering failed") }
        let suffix = scale == 2 ? "@2x" : ""
        try png.write(to: output.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
