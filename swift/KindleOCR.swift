// KindleOCR.swift - macOS native helper for kindle2ai
//
// Subcommands:
//   windows [--bundle ID]        list on-screen windows as JSON
//   ocr IMG [--langs ja-JP,en-US] [--min-conf 0.0] [--fast]
//   livetext IMG [--langs ja-JP]  Live Text transcript (reads vertical text)
//   dhash IMG                    64-bit perceptual hash (hex)
//   compare A B [--size 256]     how different two captures are, as JSON
//   ink IMG --rect x,y,w,h       share of dark pixels in a region
//   prep IN OUT [options]        crop / scale / contrast / binarize for OCR
//   pdftext PDF                  extract embedded text layer as JSON
//   pdfrender PDF OUTDIR [--dpi 200]
//   langs                        list OCR languages supported by this OS
//   perms [--request]            report (or request) Screen Recording / Accessibility
//
// Build: swiftc -O KindleOCR.swift -o kindle-ocr

import Foundation
import AppKit
import Vision
import CoreGraphics
import PDFKit
import VisionKit

// MARK: - helpers

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(("kindle-ocr: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

func emit(_ obj: Any) {
    guard let data = try? JSONSerialization.data(
        withJSONObject: obj,
        options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    ) else { die("failed to serialize JSON") }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func loadCGImage(_ path: String) -> CGImage {
    let url = URL(fileURLWithPath: path)
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
        die("cannot read image: \(path)")
    }
    return img
}

/// Pull a named option out of an argument list ("--key value").
func option(_ args: [String], _ name: String) -> String? {
    guard let i = args.firstIndex(of: "--" + name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

func flag(_ args: [String], _ name: String) -> Bool {
    args.contains("--" + name)
}

// MARK: - windows

func cmdWindows(_ args: [String]) {
    let wantBundle = option(args, "bundle")
    guard let raw = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID
    ) as? [[String: Any]] else {
        die("CGWindowListCopyWindowInfo failed (grant Screen Recording permission)")
    }

    // pid -> bundle identifier, so callers can filter by app rather than name.
    var bundleFor: [Int: String] = [:]
    for app in NSWorkspace.shared.runningApplications {
        if let b = app.bundleIdentifier { bundleFor[Int(app.processIdentifier)] = b }
    }

    var out: [[String: Any]] = []
    for w in raw {
        guard let pid = w[kCGWindowOwnerPID as String] as? Int,
              let wid = w[kCGWindowNumber as String] as? Int,
              let bounds = w[kCGWindowBounds as String] as? [String: Any] else { continue }
        let bundle = bundleFor[pid] ?? ""
        if let want = wantBundle, bundle != want { continue }
        let layer = w[kCGWindowLayer as String] as? Int ?? 0
        let width = bounds["Width"] as? Double ?? 0
        let height = bounds["Height"] as? Double ?? 0
        // Skip menubar items and other chrome; real document windows are layer 0.
        if layer != 0 || width < 200 || height < 200 { continue }
        out.append([
            "id": wid,
            "pid": pid,
            "bundle": bundle,
            "app": w[kCGWindowOwnerName as String] as? String ?? "",
            "title": w[kCGWindowName as String] as? String ?? "",
            "x": bounds["X"] as? Double ?? 0,
            "y": bounds["Y"] as? Double ?? 0,
            "width": width,
            "height": height,
        ])
    }
    out.sort { (($0["width"] as! Double) * ($0["height"] as! Double))
             > (($1["width"] as! Double) * ($1["height"] as! Double)) }
    emit(out)
}

// MARK: - ocr

func recognize(_ cg: CGImage, langs: [String], minConf: Float, fast: Bool) -> [[String: Any]] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = fast ? .fast : .accurate
    request.recognitionLanguages = langs
    request.usesLanguageCorrection = true
    // Kindle body text is small relative to the page; the default 0 lets Vision decide,
    // but a small floor keeps it from chasing single stray glyphs.
    request.minimumTextHeight = 0.0

    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([request]) } catch { die("OCR failed: \(error)") }

    let W = Double(cg.width), H = Double(cg.height)
    var lines: [[String: Any]] = []
    for obs in (request.results ?? []) {
        guard let top = obs.topCandidates(1).first else { continue }
        if top.confidence < minConf { continue }
        let b = obs.boundingBox  // normalized, origin bottom-left
        lines.append([
            "text": top.string,
            "conf": Double(top.confidence),
            // pixel coordinates, origin top-left
            "x": b.origin.x * W,
            "y": (1.0 - b.origin.y - b.size.height) * H,
            "w": b.size.width * W,
            "h": b.size.height * H,
        ])
    }
    return lines
}

func cmdOCR(_ args: [String]) {
    guard let path = args.first, !path.hasPrefix("--") else { die("usage: ocr IMG [--langs ...]") }
    let langs = (option(args, "langs") ?? "ja-JP,en-US")
        .split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }
    let minConf = Float(option(args, "min-conf") ?? "0.0") ?? 0.0
    let cg = loadCGImage(path)
    let lines = recognize(cg, langs: langs, minConf: minConf, fast: flag(args, "fast"))
    emit(["width": cg.width, "height": cg.height, "lines": lines])
}

/// Live Text (VisionKit's ImageAnalyzer) reads vertical Japanese, which
/// VNRecognizeTextRequest cannot: it returns nothing for a tategaki page. It
/// only exposes a transcript -- no boxes or confidences -- but measured against
/// Aozora Bunko it was about three times as accurate as Tesseract's jpn_vert
/// (CER 0.67% vs 1.96%), mostly on kanji, and it skips ruby on its own.
func cmdLiveText(_ args: [String]) {
    guard let path = args.first, !path.hasPrefix("--") else { die("usage: livetext IMG [--langs ...]") }
    guard #available(macOS 13.0, *), ImageAnalyzer.isSupported else {
        die("Live Text needs macOS 13 or later on supported hardware")
    }
    let langs = (option(args, "langs") ?? "ja-JP")
        .split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }
    let cg = loadCGImage(path)
    var config = ImageAnalyzer.Configuration([.text])
    config.locales = langs

    // The analyzer is async-only; keep the main run loop turning until it answers.
    var transcript: String?
    var failure: Error?
    Task {
        do {
            let analysis = try await ImageAnalyzer().analyze(cg, orientation: .up,
                                                             configuration: config)
            transcript = analysis.transcript
        } catch {
            failure = error
        }
    }
    while transcript == nil && failure == nil {
        RunLoop.main.run(until: Date().addingTimeInterval(0.02))
    }
    if let failure { die("Live Text failed: \(failure)") }
    emit(["width": cg.width, "height": cg.height, "text": transcript ?? ""])
}

func cmdLangs() {
    let r = VNRecognizeTextRequest()
    r.recognitionLevel = .accurate
    let supported = (try? r.supportedRecognitionLanguages()) ?? []
    emit(supported)
}

// MARK: - dhash

/// Difference hash: 9x8 grayscale, compare each pixel to its right neighbour.
func cmdDHash(_ args: [String]) {
    guard let path = args.first else { die("usage: dhash IMG") }
    let cg = loadCGImage(path)
    let w = 9, h = 8
    var buf = [UInt8](repeating: 0, count: w * h)
    guard let ctx = CGContext(data: &buf, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: w, space: CGColorSpaceCreateDeviceGray(),
                              bitmapInfo: CGImageAlphaInfo.none.rawValue) else {
        die("cannot create bitmap context")
    }
    ctx.interpolationQuality = .medium
    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))

    var bits: UInt64 = 0
    var n = 0
    for row in 0..<h {
        for col in 0..<(w - 1) {
            if buf[row * w + col] > buf[row * w + col + 1] { bits |= (1 << UInt64(n)) }
            n += 1
        }
    }
    print(String(format: "%016llx", bits))
}

// MARK: - compare

/// Downscale to a grayscale grid and report how different two captures are.
///
/// A 64-bit perceptual hash is useless here: every page of a book is "grey text
/// block on white", so distinct pages collapse to nearly the same hash. Keeping
/// a few thousand samples preserves the paragraph shape that actually differs
/// from page to page.
func grayGrid(_ cg: CGImage, _ size: Int) -> [UInt8] {
    var buf = [UInt8](repeating: 0, count: size * size)
    guard let ctx = CGContext(data: &buf, width: size, height: size,
                              bitsPerComponent: 8, bytesPerRow: size,
                              space: CGColorSpaceCreateDeviceGray(),
                              bitmapInfo: CGImageAlphaInfo.none.rawValue) else {
        die("cannot create bitmap context")
    }
    ctx.interpolationQuality = .medium
    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: size, height: size))
    return buf
}

/// Share of dark pixels inside a rectangle given as fractions of the image
/// (x,y,w,h, origin top-left). Used to tell whether Kindle's toolbar is up:
/// its back-arrow and contents icons sit in the top-left corner, which is
/// blank page margin when the toolbar is hidden.
func cmdInk(_ args: [String]) {
    guard let path = args.first, let rect = option(args, "rect") else {
        die("usage: ink IMG --rect x,y,w,h")
    }
    let f = rect.split(separator: ",").compactMap { Double($0) }
    guard f.count == 4 else { die("--rect needs four fractions: x,y,w,h") }
    let cg = loadCGImage(path)
    let W = cg.width, H = cg.height
    var buf = [UInt8](repeating: 255, count: W * H)
    guard let ctx = CGContext(data: &buf, width: W, height: H, bitsPerComponent: 8,
                              bytesPerRow: W, space: CGColorSpaceCreateDeviceGray(),
                              bitmapInfo: CGImageAlphaInfo.none.rawValue) else {
        die("cannot create bitmap context")
    }
    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: W, height: H))
    let x0 = Int(f[0] * Double(W)), y0 = Int(f[1] * Double(H))
    let x1 = min(W, x0 + Int(f[2] * Double(W))), y1 = min(H, y0 + Int(f[3] * Double(H)))
    var dark = 0, total = 0
    for y in max(0, y0)..<max(y0, y1) {
        for x in max(0, x0)..<max(x0, x1) {
            total += 1
            if buf[y * W + x] < 128 { dark += 1 }
        }
    }
    emit(["ink": total > 0 ? Double(dark) / Double(total) : 0.0])
}

func cmdCompare(_ args: [String]) {
    guard args.count >= 2 else { die("usage: compare A B [--size N]") }
    let size = Int(option(args, "size") ?? "256") ?? 256
    let a = grayGrid(loadCGImage(args[0]), size)
    let b = grayGrid(loadCGImage(args[1]), size)

    var total = 0.0
    var changed = 0
    for i in 0..<a.count {
        let d = abs(Int(a[i]) - Int(b[i]))
        total += Double(d)
        if d > 16 { changed += 1 }
    }
    let n = Double(a.count)
    emit([
        "mean_abs": total / n / 255.0,
        "changed_frac": Double(changed) / n,
        "size": size,
    ])
}

// MARK: - preprocessing

/// Prepare a screenshot for OCR.
///
/// Kindle renders body text in a muted brown on cream at roughly 24px per
/// character. Tesseract's models expect crisp black on white around 300dpi, so
/// stretching the contrast and scaling up recovers accuracy that the screenshot
/// never had to lose.
func cmdPrep(_ args: [String]) {
    guard args.count >= 2 else { die("usage: prep IN OUT [--scale N] [--crop-* f] [--contrast] [--binarize]") }
    let src = loadCGImage(args[0])
    let scale = Double(option(args, "scale") ?? "1") ?? 1
    let cropTop = Double(option(args, "crop-top") ?? "0") ?? 0
    let cropBottom = Double(option(args, "crop-bottom") ?? "0") ?? 0
    let cropLeft = Double(option(args, "crop-left") ?? "0") ?? 0
    let cropRight = Double(option(args, "crop-right") ?? "0") ?? 0

    let fullW = Double(src.width), fullH = Double(src.height)
    let rect = CGRect(x: fullW * cropLeft,
                      y: fullH * cropTop,
                      width: fullW * (1 - cropLeft - cropRight),
                      height: fullH * (1 - cropTop - cropBottom))
    guard rect.width > 1, rect.height > 1,
          let cropped = src.cropping(to: rect) else { die("invalid crop") }

    let outW = max(1, Int(Double(cropped.width) * scale))
    let outH = max(1, Int(Double(cropped.height) * scale))
    var buf = [UInt8](repeating: 0, count: outW * outH)
    guard let ctx = CGContext(data: &buf, width: outW, height: outH,
                              bitsPerComponent: 8, bytesPerRow: outW,
                              space: CGColorSpaceCreateDeviceGray(),
                              bitmapInfo: CGImageAlphaInfo.none.rawValue) else {
        die("cannot create bitmap context")
    }
    ctx.interpolationQuality = .high
    ctx.draw(cropped, in: CGRect(x: 0, y: 0, width: outW, height: outH))

    if flag(args, "contrast") || flag(args, "binarize") {
        stretchContrast(&buf)
    }
    var rubyBands = 0
    if flag(args, "strip-ruby") {
        rubyBands = stripVerticalRuby(&buf, width: outW, height: outH)
    }
    if flag(args, "binarize") {
        let threshold = otsuThreshold(buf)
        for i in 0..<buf.count { buf[i] = buf[i] > threshold ? 255 : 0 }
    }

    guard let outCtx = CGContext(data: &buf, width: outW, height: outH,
                                 bitsPerComponent: 8, bytesPerRow: outW,
                                 space: CGColorSpaceCreateDeviceGray(),
                                 bitmapInfo: CGImageAlphaInfo.none.rawValue),
          let image = outCtx.makeImage(),
          let dest = CGImageDestinationCreateWithURL(
              URL(fileURLWithPath: args[1]) as CFURL, "public.png" as CFString, 1, nil)
    else { die("cannot write \(args[1])") }
    CGImageDestinationAddImage(dest, image, nil)
    CGImageDestinationFinalize(dest)
    emit(["width": outW, "height": outH, "path": args[1], "ruby_bands": rubyBands])
}

/// Blank out ruby in vertical text. Tesseract's jpn_vert treats the small
/// reading glyphs between columns as part of a column, so they corrupt the
/// kanji they annotate (邪智暴虐 -> 邪智殿閥) and leak into the next line as
/// stray characters (和, 人, 上). Against Aozora Bunko, ruby-bearing kanji had a
/// 27-30% error rate versus 3-5% for the rest.
///
/// On a Kindle page each body column is one character wide (~30px) and the
/// ruby beside it is a strip about half that width, starting a few pixels to
/// its right. A narrow strip in the inter-column gap that hugs a full-width
/// column is ruby; a short body line made of narrow glyphs would instead sit a
/// full line gap away, on the column grid. Returns the number of strips blanked.
func stripVerticalRuby(_ buf: inout [UInt8], width w: Int, height h: Int) -> Int {
    let threshold = otsuThreshold(buf)
    // Skip the top and bottom margins so a running head or page number cannot
    // bridge separate columns into one band.
    let y0 = h / 20, y1 = h - h / 20
    var ink = [Int](repeating: 0, count: w)
    for y in y0..<y1 {
        let row = y * w
        for x in 0..<w where buf[row + x] < threshold { ink[x] += 1 }
    }

    var bands: [(start: Int, end: Int)] = []
    var x = 0
    while x < w {
        if ink[x] == 0 { x += 1; continue }
        let start = x
        while x < w && ink[x] > 0 { x += 1 }
        bands.append((start, x))
    }
    // Ignore specks when estimating the column width.
    let widths = bands.map { $0.end - $0.start }.filter { $0 >= 8 }.sorted()
    guard widths.count >= 3 else { return 0 }
    let column = widths[widths.count / 2]
    let narrow = Int(Double(column) * 0.6)
    let hug = max(2, column / 4)

    var lastWideEnd = Int.min
    var removed = 0
    for band in bands {
        let bw = band.end - band.start
        if bw > narrow {
            lastWideEnd = band.end
            continue
        }
        guard band.start - lastWideEnd <= hug else { continue }
        for y in 0..<h {
            let row = y * w
            for x in band.start..<band.end { buf[row + x] = 255 }
        }
        removed += 1
    }
    return removed
}

/// Rescale the 1st..99th percentile to full black..white, ignoring outliers so
/// a single dark pixel cannot flatten the page.
func stretchContrast(_ buf: inout [UInt8]) {
    var histogram = [Int](repeating: 0, count: 256)
    for v in buf { histogram[Int(v)] += 1 }
    let total = buf.count
    let lowCut = total / 100, highCut = total / 100

    var low = 0, running = 0
    for v in 0..<256 { running += histogram[v]; if running > lowCut { low = v; break } }
    var high = 255
    running = 0
    for v in stride(from: 255, through: 0, by: -1) {
        running += histogram[v]
        if running > highCut { high = v; break }
    }
    guard high > low else { return }
    let span = Double(high - low)
    for i in 0..<buf.count {
        let scaled = (Double(buf[i]) - Double(low)) / span * 255.0
        buf[i] = UInt8(max(0, min(255, scaled)))
    }
}

/// Otsu's method: the threshold that maximizes between-class variance.
func otsuThreshold(_ buf: [UInt8]) -> UInt8 {
    var histogram = [Int](repeating: 0, count: 256)
    for v in buf { histogram[Int(v)] += 1 }
    let total = Double(buf.count)
    var sum = 0.0
    for v in 0..<256 { sum += Double(v) * Double(histogram[v]) }

    var sumB = 0.0, weightB = 0.0, best = 0.0
    var threshold: UInt8 = 128
    for v in 0..<256 {
        weightB += Double(histogram[v])
        if weightB == 0 { continue }
        let weightF = total - weightB
        if weightF == 0 { break }
        sumB += Double(v) * Double(histogram[v])
        let meanB = sumB / weightB
        let meanF = (sum - sumB) / weightF
        let between = weightB * weightF * (meanB - meanF) * (meanB - meanF)
        if between > best { best = between; threshold = UInt8(v) }
    }
    return threshold
}

// MARK: - pdf

func cmdPDFText(_ args: [String]) {
    guard let path = args.first else { die("usage: pdftext PDF") }
    guard let doc = PDFDocument(url: URL(fileURLWithPath: path)) else { die("cannot open PDF") }
    var pages: [[String: Any]] = []
    for i in 0..<doc.pageCount {
        let s = doc.page(at: i)?.string ?? ""
        pages.append(["index": i + 1, "text": s])
    }
    let attrs = doc.documentAttributes ?? [:]
    emit([
        "title": attrs[PDFDocumentAttribute.titleAttribute] as? String ?? "",
        "author": attrs[PDFDocumentAttribute.authorAttribute] as? String ?? "",
        "page_count": doc.pageCount,
        "pages": pages,
    ])
}

func cmdPDFRender(_ args: [String]) {
    guard args.count >= 2 else { die("usage: pdfrender PDF OUTDIR [--dpi N]") }
    let dpi = Double(option(args, "dpi") ?? "200") ?? 200
    guard let doc = PDFDocument(url: URL(fileURLWithPath: args[0])) else { die("cannot open PDF") }
    let outDir = URL(fileURLWithPath: args[1])
    try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

    var written: [String] = []
    let scale = dpi / 72.0
    for i in 0..<doc.pageCount {
        guard let page = doc.page(at: i) else { continue }
        let box = page.bounds(for: .mediaBox)
        let pxW = Int(box.width * scale), pxH = Int(box.height * scale)
        guard pxW > 0, pxH > 0,
              let ctx = CGContext(data: nil, width: pxW, height: pxH, bitsPerComponent: 8,
                                  bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { continue }
        ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: pxW, height: pxH))
        ctx.scaleBy(x: scale, y: scale)
        ctx.translateBy(x: -box.origin.x, y: -box.origin.y)
        page.draw(with: .mediaBox, to: ctx)

        guard let img = ctx.makeImage() else { continue }
        let dest = outDir.appendingPathComponent(String(format: "page_%05d.png", i + 1))
        guard let out = CGImageDestinationCreateWithURL(dest as CFURL, "public.png" as CFString, 1, nil)
        else { continue }
        CGImageDestinationAddImage(out, img, nil)
        CGImageDestinationFinalize(out)
        written.append(dest.path)
    }
    emit(["page_count": doc.pageCount, "files": written])
}

// MARK: - permissions

/// screencapture silently returns a desktop-only image when Screen Recording is
/// denied, and key events are swallowed when Accessibility is denied, so the CLI
/// checks both up front rather than producing 400 blank pages.
// macOS grants Screen Recording and Accessibility to the "responsible"
// process -- the app that launched this chain (a terminal, or Kindle2AI) --
// not to this helper. Reporting it makes "granted but still refused" cases
// diagnosable: the permission has to be on for exactly this app.
@_silgen_name("responsibility_get_pid_responsible_for_pid")
func responsibilityGetPidResponsibleForPid(_ pid: pid_t) -> pid_t

func executablePath(_ pid: pid_t) -> String {
    var buf = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    let n = proc_pidpath(pid, &buf, UInt32(buf.count))
    return n > 0 ? String(cString: buf) : ""
}

func cmdPerms(_ args: [String]) {
    if flag(args, "request") {
        if !CGPreflightScreenCaptureAccess() { CGRequestScreenCaptureAccess() }
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
        _ = AXIsProcessTrustedWithOptions(opts as CFDictionary)
    }
    let responsible = responsibilityGetPidResponsibleForPid(getpid())
    emit([
        "screen_recording": CGPreflightScreenCaptureAccess(),
        "accessibility": AXIsProcessTrusted(),
        "responsible": executablePath(responsible),
    ])
}

// MARK: - main

let argv = Array(CommandLine.arguments.dropFirst())
guard let cmd = argv.first else {
    die("usage: kindle-ocr <windows|ocr|livetext|dhash|compare|prep|pdftext|pdfrender|langs|perms> [args]")
}
let rest = Array(argv.dropFirst())

switch cmd {
case "windows":   cmdWindows(rest)
case "ocr":       cmdOCR(rest)
case "livetext":  cmdLiveText(rest)
case "dhash":     cmdDHash(rest)
case "compare":   cmdCompare(rest)
case "ink":       cmdInk(rest)
case "prep":      cmdPrep(rest)
case "pdftext":   cmdPDFText(rest)
case "pdfrender": cmdPDFRender(rest)
case "langs":     cmdLangs()
case "perms":     cmdPerms(rest)
default:          die("unknown command: \(cmd)")
}
