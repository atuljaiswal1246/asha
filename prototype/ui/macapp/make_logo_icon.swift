// Build a macOS app icon (.iconset) from the Jarvis logo.
// Draws the logo into the standard macOS rounded-square canvas (824/1024 with
// ~185pt corners) plus a soft shadow, so the Dock icon looks native instead of
// a full-bleed square.
//
//   swift make_logo_icon.swift <logo.png> <out.iconset>
//   iconutil -c icns <out.iconset> -o AppIcon.icns

import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: make_logo_icon <logo.png> <out.iconset>\n".data(using: .utf8)!)
    exit(1)
}
let srcPath = args[1]
let outDir = args[2]

guard let src = NSImage(contentsOfFile: srcPath) else {
    FileHandle.standardError.write("cannot read \(srcPath)\n".data(using: .utf8)!)
    exit(2)
}

let fm = FileManager.default
try? fm.removeItem(atPath: outDir)
try! fm.createDirectory(atPath: outDir, withIntermediateDirectories: true)

func render(_ size: Int) -> NSBitmapImageRep {
    let s = CGFloat(size)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                               isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = NSSize(width: s, height: s)

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let ctx = NSGraphicsContext.current!.cgContext
    ctx.clear(CGRect(x: 0, y: 0, width: s, height: s))

    let inset = s * (100.0 / 1024.0)
    let rect = CGRect(x: inset, y: inset, width: s - inset * 2, height: s - inset * 2)
    let radius = rect.width * (185.0 / 824.0)
    let path = CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)

    // contact shadow, like Apple's own icons
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -s * 0.010),
                  blur: s * 0.028,
                  color: NSColor.black.withAlphaComponent(0.32).cgColor)
    ctx.addPath(path)
    ctx.setFillColor(NSColor.black.cgColor)
    ctx.fillPath()
    ctx.restoreGState()

    // the logo, clipped to the rounded square
    ctx.saveGState()
    ctx.addPath(path)
    ctx.clip()
    src.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1.0)
    ctx.restoreGState()

    // hairline rim so it reads as a surface
    ctx.saveGState()
    ctx.addPath(path)
    ctx.setStrokeColor(NSColor.white.withAlphaComponent(0.16).cgColor)
    ctx.setLineWidth(max(1, s * 0.0045))
    ctx.strokePath()
    ctx.restoreGState()

    NSGraphicsContext.restoreGraphicsState()
    return rep
}

let variants: [(Int, String)] = [
    (16, "icon_16x16"), (32, "icon_16x16@2x"),
    (32, "icon_32x32"), (64, "icon_32x32@2x"),
    (128, "icon_128x128"), (256, "icon_128x128@2x"),
    (256, "icon_256x256"), (512, "icon_256x256@2x"),
    (512, "icon_512x512"), (1024, "icon_512x512@2x"),
]

for (size, name) in variants {
    let rep = render(size)
    guard let data = rep.representation(using: .png, properties: [:]) else { continue }
    try! data.write(to: URL(fileURLWithPath: outDir + "/" + name + ".png"))
}

print("wrote \(variants.count) images -> \(outDir)")
