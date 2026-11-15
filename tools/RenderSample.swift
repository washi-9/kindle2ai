// RenderSample.swift - synthesise a Kindle-looking vertical Japanese page.
//
// Measuring OCR accuracy needs text whose correct transcription is known, and
// rendering it under the reader's own conditions (sepia background, mincho
// face, the same characters-per-column) keeps the measurement honest without
// transcribing anyone's book.
//
// Usage: render-sample TEXTFILE OUT.png [--size 24] [--gap 2.08] [--line-pitch 1.02]
//                       [--width 2940] [--height 1836]
// Build: swiftc -O tools/RenderSample.swift -o build/render-sample

import AppKit

func opt(_ name: String, _ fallback: Double) -> Double {
    let args = CommandLine.arguments
    guard let i = args.firstIndex(of: "--" + name), i + 1 < args.count,
          let v = Double(args[i + 1]) else { return fallback }
    return v
}

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: render-sample TEXT OUT.png [--size N]\n".data(using: .utf8)!)
    exit(1)
}
var text = (try! String(contentsOfFile: args[1], encoding: .utf8))
    .replacingOccurrences(of: "\n", with: "")
let outPath = args[2]

let size = opt("size", 24)
let W = Int(opt("width", 2940)), H = Int(opt("height", 1836))

// Sampled from a real Kindle for Mac page: a cream background and muted ink,
// only ~110 levels of luminance apart.
let background = NSColor(calibratedRed: 251/255, green: 241/255, blue: 218/255, alpha: 1)
let ink = NSColor(calibratedRed: 135/255, green: 128/255, blue: 118/255, alpha: 1)

let marginX = Double(W) * 0.07
let marginY = Double(H) * 0.10
let usableH = Double(H) - marginY * 2
// Measured on a real Kindle for Mac page: glyphs advance by their own size
// vertically, while columns sit a little over two character widths apart.
let pitch = size * opt("line-pitch", 1.02)
let columnGap = size * opt("gap", 2.08)
let perColumn = max(1, Int(usableH / pitch))
let columns = max(1, Int((Double(W) - marginX * 2) / columnGap))

// A real page holds far more text than the sample, and Tesseract's layout
// analysis behaves differently on a half-empty page, so repeat the sample
// until the page is full. The transcript records exactly what was drawn.
let needed = columns * perColumn
if text.count < needed {
    let original = text
    while text.count < needed { text += original }
}

let image = NSImage(size: NSSize(width: W, height: H))
image.lockFocus()
background.setFill()
NSRect(x: 0, y: 0, width: W, height: H).fill()

let font = NSFont(name: "Hiragino Mincho ProN W3", size: size)
    ?? NSFont.systemFont(ofSize: size)
let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: ink]

var used: [String] = []
var index = text.startIndex
outer: for col in 0..<columns {
    // Tategaki reads right to left.
    let x = Double(W) - marginX - Double(col) * columnGap
    var column = ""
    for row in 0..<perColumn {
        if index == text.endIndex { used.append(column); break outer }
        let ch = text[index]
        index = text.index(after: index)
        column.append(ch)
        let y = Double(H) - marginY - Double(row) * pitch - pitch
        (String(ch) as NSString).draw(at: NSPoint(x: x, y: y), withAttributes: attrs)
    }
    used.append(column)
}
image.unlockFocus()

let rep = NSBitmapImageRep(data: image.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!
    .write(to: URL(fileURLWithPath: outPath))

// The transcript of what was actually drawn, one line per column, in reading
// order - this is the ground truth the OCR output is scored against.
try! used.joined(separator: "\n").write(toFile: outPath + ".truth.txt",
                                        atomically: true, encoding: .utf8)
print("columns=\(used.count) chars=\(used.joined().count) size=\(Int(size))")
