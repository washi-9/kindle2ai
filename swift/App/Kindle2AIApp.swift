// Kindle2AIApp.swift - a window around the kindle2ai command line.
//
// The app holds no conversion logic of its own: every action runs
// bin/kindle2ai and reads its output, so the CLI stays the single source of
// truth and anything the app does can be reproduced in a terminal.
//
// Built by setup.sh into build/Kindle2AI.app. macOS attributes the screen
// captures and key presses of the CLI it launches to this app, so Screen
// Recording and Accessibility are granted to Kindle2AI rather than a terminal.

import SwiftUI
import AppKit
import ApplicationServices
import UniformTypeIdentifiers

// MARK: - locating the project

enum Project {
    /// build/Kindle2AI.app -> the repository root two levels up.
    static let root: URL = {
        let fromBundle = Bundle.main.bundleURL
            .deletingLastPathComponent().deletingLastPathComponent()
        if FileManager.default.isExecutableFile(atPath: fromBundle.appendingPathComponent("bin/kindle2ai").path) {
            return fromBundle
        }
        if let configured = Bundle.main.object(forInfoDictionaryKey: "Kindle2AIRoot") as? String {
            return URL(fileURLWithPath: configured)
        }
        return fromBundle
    }()
    static var cli: URL { root.appendingPathComponent("bin/kindle2ai") }
    static var outRoot: URL { root.appendingPathComponent("out") }
}

// MARK: - running the CLI

/// One kindle2ai invocation, streaming its output line by line.
final class CLIProcess {
    private let process = Process()
    private var buffer = Data()

    init(_ args: [String], onLine: @escaping (String) -> Void,
         onExit: @escaping (Int32) -> Void) {
        process.executableURL = Project.cli
        process.arguments = args
        process.currentDirectoryURL = Project.root
        // Apps launched from Finder get a bare PATH; Homebrew's python3 and
        // tesseract live outside it.
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        // The handlers hold self strongly on purpose: whoever started the
        // process may drop its reference right away (see `output`), and the
        // output must still arrive. Clearing the handler on exit breaks the cycle.
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData
            guard !chunk.isEmpty else { return }
            self.buffer.append(chunk)
            while let newline = self.buffer.firstIndex(of: 0x0A) {
                let lineData = self.buffer[self.buffer.startIndex..<newline]
                self.buffer.removeSubrange(self.buffer.startIndex...newline)
                let line = String(decoding: lineData, as: UTF8.self)
                DispatchQueue.main.async { onLine(line) }
            }
        }
        process.terminationHandler = { proc in
            pipe.fileHandleForReading.readabilityHandler = nil
            self.buffer.append(pipe.fileHandleForReading.readDataToEndOfFile())
            let tail = String(decoding: self.buffer, as: UTF8.self)
            self.buffer.removeAll()
            DispatchQueue.main.async {
                for line in tail.split(separator: "\n") { onLine(String(line)) }
                onExit(proc.terminationStatus)
            }
        }
    }

    func start() throws { try process.run() }
    /// SIGINT: the CLI saves its state, and `run` goes on to convert the
    /// pages captured so far.
    func interrupt() { process.interrupt() }
    func terminate() { process.terminate() }
    var isRunning: Bool { process.isRunning }

    /// Run to completion and return stdout; for quick JSON queries.
    static func output(_ args: [String]) async -> String {
        await withCheckedContinuation { continuation in
            var lines: [String] = []
            let proc = CLIProcess(args, onLine: { lines.append($0) },
                                  onExit: { _ in continuation.resume(returning: lines.joined(separator: "\n")) })
            do { try proc.start() } catch {
                continuation.resume(returning: "")
            }
        }
    }
}

// MARK: - data

struct Book: Decodable, Identifiable, Hashable {
    let asin: String
    let title: String
    let max_location: Int
    let downloaded: Bool
    var id: String { asin }
}

struct Check: Decodable, Identifiable {
    let key: String
    let status: String
    let label: String
    let detail: String
    let hint: String
    var id: String { key }
}

private struct DoctorReport: Decodable {
    let ok: Bool
    let checks: [Check]
}

enum Phase: Equatable {
    case idle, capturing, recognizing, building, done, failed, cancelled

    var label: String {
        switch self {
        case .idle: return "待機中"
        case .capturing: return "ページを撮影中"
        case .recognizing: return "文字を認識中"
        case .building: return "ファイルを作成中"
        case .done: return "完了"
        case .failed: return "エラー"
        case .cancelled: return "中止しました"
        }
    }
}

/// One conversion in progress, Kindle capture or local file.
@MainActor
final class Job: ObservableObject {
    @Published var phase: Phase = .idle
    @Published var capturedPages = 0
    @Published var ocrDone = 0
    @Published var ocrTotal = 0
    @Published var engine = ""
    @Published var summary = ""
    @Published var error = ""
    @Published var log: [String] = []
    @Published var outputDir: URL?
    private var process: CLIProcess?
    private var stopping = false

    var isRunning: Bool { process?.isRunning ?? false }

    func start(_ args: [String], outputDir: URL) {
        self.outputDir = outputDir
        phase = args.first == "local" ? .building : .capturing
        capturedPages = 0; ocrDone = 0; ocrTotal = 0
        engine = ""; summary = ""; error = ""; log = []; stopping = false
        log.append("$ kindle2ai " + args.joined(separator: " "))

        let proc = CLIProcess(args, onLine: { [weak self] in self?.consume($0) },
                              onExit: { [weak self] in self?.finish($0) })
        process = proc
        do { try proc.start() } catch {
            phase = .failed
            self.error = "kindle2ai を起動できませんでした: \(error.localizedDescription)"
        }
    }

    /// Stop capturing and convert the pages taken so far.
    func stopCapture() { process?.interrupt() }

    func cancel() {
        stopping = true
        process?.terminate()
    }

    private func consume(_ line: String) {
        log.append(line)
        if log.count > 2000 { log.removeFirst(log.count - 2000) }
        let trimmed = line.trimmingCharacters(in: .whitespaces)

        if trimmed.hasPrefix("page "), let n = Int(trimmed.split(separator: " ").dropFirst().first ?? "") {
            phase = .capturing
            capturedPages = n
        } else if trimmed.hasPrefix("engine: ") {
            phase = .recognizing
            engine = String(trimmed.dropFirst("engine: ".count))
        } else if trimmed.hasPrefix("ocr "), let frac = trimmed.split(separator: " ").last {
            let parts = frac.split(separator: "/").compactMap { Int($0) }
            if parts.count == 2 { ocrDone = parts[0]; ocrTotal = parts[1] }
        } else if trimmed.hasPrefix("wrote ") && trimmed.hasSuffix("ocr.jsonl") {
            phase = .building
        } else if trimmed.contains("paragraphs,") || trimmed.contains("blocks,") {
            summary = trimmed
        } else if trimmed.hasPrefix("error: ") {
            error = String(trimmed.dropFirst("error: ".count))
        } else if !error.isEmpty && line.hasPrefix("  ") {
            error += "\n" + trimmed  // continuation of a multi-line error
        }
    }

    private func finish(_ status: Int32) {
        process = nil
        if stopping {
            phase = .cancelled
        } else if status == 0 {
            phase = .done
        } else {
            phase = .failed
            if error.isEmpty { error = "終了コード \(status)。ログを確認してください。" }
        }
        NSApp.activate(ignoringOtherApps: true)
        if phase == .done { NSSound(named: "Glass")?.play() }
    }

    /// The Markdown file the CLI wrote, if any.
    var markdown: URL? {
        guard let dir = outputDir,
              let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
        else { return nil }
        return files.first { $0.pathExtension == "md" }
    }
}

// MARK: - app state

enum Defaults {
    private static let store = UserDefaults.standard
    static func int(_ key: String, _ fallback: Int) -> Int {
        store.object(forKey: key) == nil ? fallback : store.integer(forKey: key)
    }
    static func double(_ key: String, _ fallback: Double) -> Double {
        store.object(forKey: key) == nil ? fallback : store.double(forKey: key)
    }
    static func bool(_ key: String, _ fallback: Bool) -> Bool {
        store.object(forKey: key) == nil ? fallback : store.bool(forKey: key)
    }
    static func string(_ key: String, _ fallback: String) -> String {
        store.string(forKey: key) ?? fallback
    }
    static func set(_ value: Any, _ key: String) { store.set(value, forKey: key) }
}

@MainActor
final class AppModel: ObservableObject {
    @Published var books: [Book] = []
    @Published var query = ""
    @Published var selected: Book?
    @Published var checks: [Check] = []
    @Published var checking = false
    @Published var loadingBooks = false

    // @AppStorage does not publish from inside an ObservableObject, so the
    // settings are @Published and written through to UserDefaults.
    @Published var maxPages = Defaults.int("maxPages", 2000) { didSet { Defaults.set(maxPages, "maxPages") } }
    @Published var engine = Defaults.string("engine", "auto") { didSet { Defaults.set(engine, "engine") } }
    @Published var delay = Defaults.double("delay", 1.0) { didSet { Defaults.set(delay, "delay") } }
    @Published var formatMD = Defaults.bool("formatMD", true) { didSet { Defaults.set(formatMD, "formatMD") } }
    @Published var formatJSONL = Defaults.bool("formatJSONL", true) { didSet { Defaults.set(formatJSONL, "formatJSONL") } }
    @Published var formatTXT = Defaults.bool("formatTXT", true) { didSet { Defaults.set(formatTXT, "formatTXT") } }

    let kindleJob = Job()
    let fileJob = Job()

    var filteredBooks: [Book] {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return books }
        return books.filter { $0.title.localizedCaseInsensitiveContains(q) || $0.asin.contains(q) }
    }

    var formats: String {
        [formatMD ? "md" : nil, formatJSONL ? "jsonl" : nil, formatTXT ? "txt" : nil]
            .compactMap { $0 }.joined(separator: ",")
    }

    var allChecksPass: Bool { !checks.isEmpty && checks.allSatisfy { $0.status != "fail" } }

    func refreshDoctor(request: Bool = false) async {
        checking = true
        defer { checking = false }
        let json = await CLIProcess.output(["doctor", "--json"] + (request ? ["--request"] : []))
        if let data = json.data(using: .utf8),
           let report = try? JSONDecoder().decode(DoctorReport.self, from: data) {
            checks = report.checks
        }
    }

    func loadBooks() async {
        loadingBooks = true
        defer { loadingBooks = false }
        let json = await CLIProcess.output(["books", "--json"])
        if let data = json.data(using: .utf8),
           let list = try? JSONDecoder().decode([Book].self, from: data) {
            books = list
            if let sel = selected, !list.contains(sel) { selected = nil }
        }
    }

    func outputDir(for book: Book) -> URL {
        let safe = book.title
            .replacingOccurrences(of: "/", with: "／")
            .replacingOccurrences(of: ":", with: "：")
            .trimmingCharacters(in: .whitespaces)
        return Project.outRoot.appendingPathComponent(safe.isEmpty ? book.asin : safe)
    }

    func hasCapturedPages(_ dir: URL) -> Bool {
        let pages = dir.appendingPathComponent("pages")
        let items = (try? FileManager.default.contentsOfDirectory(atPath: pages.path)) ?? []
        return items.contains { $0.hasSuffix(".png") }
    }

    func startKindle(book: Book, resume: Bool) {
        let dir = outputDir(for: book)
        var args = ["run", "--out", dir.path, "--pages", String(maxPages),
                    "--book", book.asin, "--title", book.title,
                    "--engine", engine, "--delay", String(delay),
                    "--formats", formats.isEmpty ? "md" : formats]
        if resume { args.append("--resume") }
        kindleJob.start(args, outputDir: dir)
    }

    func startFresh(book: Book) {
        let dir = outputDir(for: book)
        // Move the previous capture to the Trash rather than deleting it.
        if FileManager.default.fileExists(atPath: dir.path) {
            NSWorkspace.shared.recycle([dir]) { _, _ in
                Task { @MainActor in self.startKindle(book: book, resume: false) }
            }
        } else {
            startKindle(book: book, resume: false)
        }
    }

    func convertFile(_ url: URL) {
        let stem = url.deletingPathExtension().lastPathComponent
        let dir = Project.outRoot.appendingPathComponent(stem)
        fileJob.start(["local", url.path, "--out", dir.path,
                       "--formats", formats.isEmpty ? "md" : formats], outputDir: dir)
    }
}

// MARK: - views

@main
struct Kindle2AIApp: App {
    @StateObject private var model = AppModel()

    init() {
        // `Kindle2AI --selftest` runs the same CLI queries the window makes
        // and prints what came back, so a broken setup can be diagnosed from
        // a terminal without looking at the UI.
        if CommandLine.arguments.contains("--selftest") {
            Task { @MainActor in
                print("project: \(Project.root.path)")
                print("app process: accessibility=\(AXIsProcessTrusted()) "
                      + "screen=\(CGPreflightScreenCaptureAccess())")
                let model = AppModel()
                await model.refreshDoctor()
                print("doctor: \(model.checks.count) checks")
                for c in model.checks { print("  [\(c.status)] \(c.label): \(c.detail)") }
                await model.loadBooks()
                print("books: \(model.books.count)")
                exit(model.checks.isEmpty ? 1 : 0)
            }
        }
    }

    var body: some Scene {
        WindowGroup("Kindle2AI") {
            ContentView()
                .environmentObject(model)
                .frame(minWidth: 900, minHeight: 620)
        }
        .windowResizability(.contentMinSize)
    }
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        TabView {
            KindleTab().tabItem { Label("Kindle の本", systemImage: "book") }
            FileTab().tabItem { Label("ファイルを変換", systemImage: "doc.text") }
        }
        .padding()
        .task {
            await model.refreshDoctor()
            await model.loadBooks()
        }
    }
}

// MARK: Kindle tab

struct KindleTab: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            StatusPanel()
            HSplitView {
                BookList().frame(minWidth: 320)
                ConvertPanel(job: model.kindleJob).frame(minWidth: 420)
            }
        }
    }
}

struct StatusPanel: View {
    @EnvironmentObject var model: AppModel

    private var problems: [Check] { model.checks.filter { $0.status != "ok" } }

    var body: some View {
        GroupBox {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    if model.checks.isEmpty {
                        Text(model.checking ? "環境を確認しています…" : "環境を確認できませんでした")
                            .foregroundStyle(.secondary)
                    } else if problems.isEmpty {
                        Label("準備完了", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                    }
                    ForEach(problems) { check in
                        VStack(alignment: .leading, spacing: 2) {
                            Label(Self.title(check), systemImage: check.status == "fail"
                                  ? "xmark.octagon.fill" : "exclamationmark.triangle.fill")
                                .foregroundStyle(check.status == "fail" ? .red : .orange)
                            Text(Self.advice(check)).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    if let vertical = model.checks.first(where: { $0.key == "vertical" }), vertical.status == "ok" {
                        Text("縦書き: \(vertical.detail)").font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 6) {
                    Button {
                        Task { await model.refreshDoctor(); await model.loadBooks() }
                    } label: { Label("再確認", systemImage: "arrow.clockwise") }
                    .disabled(model.checking)
                    if problems.contains(where: { $0.key == "screen_recording" || $0.key == "accessibility" }) {
                        Button("権限をリクエスト") { Task { await model.refreshDoctor(request: true) } }
                    }
                    if problems.contains(where: { $0.key == "accessibility" }) {
                        Button("アクセシビリティ設定を開く") { Self.openPrivacySettings("Privacy_Accessibility") }
                    }
                    if problems.contains(where: { $0.key == "screen_recording" }) {
                        Button("画面収録設定を開く") { Self.openPrivacySettings("Privacy_ScreenCapture") }
                    }
                    if problems.contains(where: { $0.key == "kindle" }) {
                        Button("Kindle を起動") { Self.launchKindle() }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    static func title(_ c: Check) -> String {
        switch c.key {
        case "screen_recording": return "画面収録の権限がありません"
        case "accessibility": return "アクセシビリティの権限がありません"
        case "kindle": return "Kindle のウィンドウが見つかりません"
        case "helper": return "ネイティブヘルパーがありません"
        case "vertical": return "縦書きの OCR エンジンがありません"
        case "library": return "Kindle の蔵書データを読めません"
        default: return "\(c.label): \(c.detail)"
        }
    }

    static func advice(_ c: Check) -> String {
        switch c.key {
        case "screen_recording", "accessibility":
            return "システム設定 > プライバシーとセキュリティ で Kindle2AI を許可し、このアプリを再起動してください。"
                + "\nすでにオンなのに認識されない場合は、一覧の Kindle2AI を「−」で削除し、「＋」で build/Kindle2AI.app を追加し直してください（アプリを作り直すと以前の許可が無効になります）。"
        case "kindle": return "Kindle アプリを起動して本を開いてください。"
        case "helper": return "ターミナルで ./setup.sh を実行してください。"
        default: return c.hint.isEmpty ? c.detail : c.hint
        }
    }

    static func openPrivacySettings(_ pane: String) {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") {
            NSWorkspace.shared.open(url)
        }
    }

    static func launchKindle() {
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.amazon.Lassen") {
            NSWorkspace.shared.openApplication(at: url, configuration: .init())
        }
    }
}

struct BookList: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("タイトル・ASIN で検索", text: $model.query).textFieldStyle(.roundedBorder)
            }
            List(model.filteredBooks, selection: $model.selected) { book in
                VStack(alignment: .leading, spacing: 2) {
                    Text(book.title).lineLimit(2)
                    HStack(spacing: 8) {
                        Text(book.asin)
                        if book.downloaded { Text("ダウンロード済み") } else { Text("クラウド") }
                        if book.max_location > 0 { Text("位置 \(book.max_location)") }
                    }
                    .font(.caption).foregroundStyle(.secondary)
                }
                .tag(book)
                .padding(.vertical, 2)
            }
            .overlay {
                if model.books.isEmpty {
                    Text(model.loadingBooks ? "読み込み中…" : "蔵書が見つかりません")
                        .foregroundStyle(.secondary)
                }
            }
            Text("一覧は最後に開いた順です").font(.caption).foregroundStyle(.secondary)
        }
        .padding(.trailing, 8)
    }
}

struct ConvertPanel: View {
    @EnvironmentObject var model: AppModel
    @ObservedObject var job: Job
    @State private var confirming = false
    @State private var askResume = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let book = model.selected {
                Text(book.title).font(.title2).bold().lineLimit(2)
                Text("保存先: \(model.outputDir(for: book).path)")
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            } else {
                Text("左の一覧から本を選んでください").foregroundStyle(.secondary)
            }

            SettingsForm()
                .disabled(job.isRunning)

            HStack {
                if job.isRunning {
                    if job.phase == .capturing {
                        Button("ここで撮影を終えて変換") { job.stopCapture() }
                    }
                    Button("中止", role: .destructive) { job.cancel() }
                } else {
                    Button {
                        guard let book = model.selected else { return }
                        if model.hasCapturedPages(model.outputDir(for: book)) { askResume = true }
                        else { confirming = true }
                    } label: {
                        Label("変換を開始", systemImage: "play.fill").frame(minWidth: 120)
                    }
                    .keyboardShortcut(.defaultAction)
                    .controlSize(.large)
                    .disabled(model.selected == nil)
                }
            }

            ProgressPanel(job: job, maxPages: model.maxPages)
        }
        .padding(.leading, 8)
        .alert("Kindle で本を開いてください", isPresented: $confirming) {
            Button("開始") { if let b = model.selected { model.startFresh(book: b) } }
            Button("キャンセル", role: .cancel) {}
        } message: {
            Text("Kindle アプリで『\(model.selected?.title ?? "")』を開き、変換を始めたいページ（本文の1ページ目など）を表示してから「開始」を押してください。\n\n撮影中は完了するまでキーボードとマウスに触れないでください。")
        }
        .confirmationDialog("前回の撮影が残っています", isPresented: $askResume) {
            Button("続きから再開") { if let b = model.selected { model.startKindle(book: b, resume: true) } }
            Button("最初からやり直す（前回分はゴミ箱へ）") { confirming = true }
            Button("キャンセル", role: .cancel) {}
        } message: {
            Text("続きから再開する場合は、Kindle で前回止まったページを表示してください。")
        }
    }
}

struct SettingsForm: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        GroupBox("設定") {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    Text("最大ページ数")
                    HStack {
                        TextField("", value: $model.maxPages, format: .number)
                            .frame(width: 80).textFieldStyle(.roundedBorder)
                        Stepper("", value: $model.maxPages, in: 1...20000, step: 50).labelsHidden()
                        Text("本の終わりで自動停止します").font(.caption).foregroundStyle(.secondary)
                    }
                }
                GridRow {
                    Text("OCR エンジン")
                    Picker("", selection: $model.engine) {
                        Text("自動（おすすめ）").tag("auto")
                        Text("NDLOCR-Lite（縦書き・最高精度）").tag("ndlocr")
                        Text("Live Text（縦書き）").tag("livetext")
                        Text("Vision（横書き）").tag("vision")
                        Text("Tesseract").tag("tesseract")
                    }
                    .labelsHidden().frame(width: 260)
                }
                GridRow {
                    Text("出力形式")
                    HStack {
                        Toggle("Markdown", isOn: $model.formatMD)
                        Toggle("JSONL（RAG 用）", isOn: $model.formatJSONL)
                        Toggle("テキスト", isOn: $model.formatTXT)
                    }
                }
                GridRow {
                    Text("ページ送り待ち")
                    HStack {
                        Slider(value: $model.delay, in: 0.5...3.0, step: 0.25).frame(width: 160)
                        Text(String(format: "%.2f 秒", model.delay)).monospacedDigit()
                        Text("途中で止まるなら長く").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(4)
        }
    }
}

struct ProgressPanel: View {
    @ObservedObject var job: Job
    var maxPages: Int = 0
    @State private var showLog = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if job.phase != .idle {
                HStack {
                    Text(job.phase.label).font(.headline)
                    if !job.engine.isEmpty && job.phase == .recognizing {
                        Text(job.engine).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if job.phase == .capturing { Text("\(job.capturedPages) ページ").monospacedDigit() }
                    if job.phase == .recognizing && job.ocrTotal > 0 {
                        Text("\(job.ocrDone) / \(job.ocrTotal)").monospacedDigit()
                    }
                }
                switch job.phase {
                case .capturing:
                    ProgressView()
                        .progressViewStyle(.linear)
                    Text("Kindle が自動でページをめくっています。キーボードとマウスに触れないでください。")
                        .font(.caption).foregroundStyle(.secondary)
                case .recognizing:
                    if job.ocrTotal > 0 {
                        ProgressView(value: Double(job.ocrDone), total: Double(job.ocrTotal))
                    } else {
                        ProgressView().progressViewStyle(.linear)
                    }
                case .building:
                    ProgressView().progressViewStyle(.linear)
                case .done:
                    if !job.summary.isEmpty { Text(job.summary).foregroundStyle(.secondary) }
                    HStack {
                        if let dir = job.outputDir {
                            Button("Finder で表示") { NSWorkspace.shared.activateFileViewerSelecting([dir]) }
                        }
                        if let md = job.markdown {
                            Button("Markdown を開く") { NSWorkspace.shared.open(md) }
                        }
                    }
                case .failed:
                    Text(job.error).foregroundStyle(.red).textSelection(.enabled)
                case .cancelled, .idle:
                    EmptyView()
                }

                DisclosureGroup("ログ", isExpanded: $showLog) {
                    ScrollViewReader { proxy in
                        ScrollView {
                            Text(job.log.joined(separator: "\n"))
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Color.clear.frame(height: 1).id("end")
                        }
                        .frame(minHeight: 120, maxHeight: 220)
                        .onChange(of: job.log.count) { _ in proxy.scrollTo("end") }
                    }
                }
            }
            Spacer()
        }
    }
}

// MARK: File tab

struct FileTab: View {
    @EnvironmentObject var model: AppModel
    @State private var targeted = false

    private static let types: [UTType] = [.epub, .pdf, .plainText, .html]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("DRM のない EPUB / PDF / TXT / HTML を直接変換します（OCR は不要です）")
                .foregroundStyle(.secondary)
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(style: StrokeStyle(lineWidth: 2, dash: [8]))
                .foregroundStyle(targeted ? Color.accentColor : .secondary)
                .frame(height: 160)
                .overlay {
                    VStack(spacing: 8) {
                        Image(systemName: "arrow.down.doc").font(.largeTitle)
                        Text("ここにファイルをドロップ")
                        Button("ファイルを選択…") { choose() }
                    }
                }
                .onDrop(of: [.fileURL], isTargeted: $targeted) { providers in
                    guard let provider = providers.first else { return false }
                    _ = provider.loadObject(ofClass: URL.self) { url, _ in
                        if let url { Task { @MainActor in model.convertFile(url) } }
                    }
                    return true
                }
                .disabled(model.fileJob.isRunning)
            ProgressPanel(job: model.fileJob)
        }
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = Self.types
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url { model.convertFile(url) }
    }
}
