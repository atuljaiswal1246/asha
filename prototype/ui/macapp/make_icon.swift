import AppKit
import CoreGraphics

let size = CGSize(width: 1024, height: 1024)
let image = NSImage(size: size)
image.lockFocus()

let rect = NSRect(x: 0, y: 0, width: size.width, height: size.height)
// Rounded gradient background
let bg = NSBezierPath(roundedRect: rect.insetBy(dx: 48, dy: 48), xRadius: 220, yRadius: 220)
let grad = NSGradient(colors: [NSColor(calibratedRed: 0.16, green: 0.32, blue: 0.85, alpha: 1),
                               NSColor(calibratedRed: 0.09, green: 0.13, blue: 0.28, alpha: 1)])
grad?.draw(in: bg, angle: -70)

// Mic glyph (rounded rect capsule + stand)
NSColor.white.setFill()
let capsule = NSBezierPath(roundedRect: NSRect(x: 392, y: 420, width: 240, height: 340), xRadius: 120, yRadius: 120)
capsule.fill()

// wave bars
for i in 0..<5 {
    let bh = CGFloat(360 - i * 60)
    let bw = CGFloat(56)
    let bx = 512 + CGFloat(i * 0) - bw / 2 - (2 - CGFloat(i)) * 0
    let by = 512 - bh / 2
    NSColor.black.withAlphaComponent(0.8).setFill()
    let bar = NSBezierPath(roundedRect: NSRect(x: bx + CGFloat((i - 2) * 0), y: 512 - bh / 2, width: bw, height: bh), xRadius: 28, yRadius: 28)
    _ = by
    bar.fill()
}

image.unlockFocus()

guard let tiff = image.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
try! png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
