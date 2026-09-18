// Asha — macOS launcher for the bundled app.
// Runs the embedded Python runtime (Resources/runtime) with the app code
// (Resources/app) and shows the local UI in a WebKit window. Everything a
// developer needs is bundled: voice models are pointed at Resources/models, so
// nothing downloads. OmniRoute is NOT bundled (optional user-installed add-on);
// Asha reaches a user's own OmniRoute through the app's OMNIROUTE_BASE_URL.

import AppKit
import LocalAuthentication
import WebKit

// Touch ID / password lock on startup. LOCK_ON_START = true makes the app show the
// lock surface (Touch ID, with Mac-password fallback and retry) before the UI loads.
// LOCK_ON_START = false (current) skips straight to the normal UI — the lock is never
// constructed or shown. All lock code (showLockSurface/attemptUnlock/unlockApp) is
// intentionally left intact, so re-enabling is this one line: LOCK_ON_START = true.
let LOCK_ON_START = false

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate,
                         WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var backend: Process?
    let uiURL = URL(string: "http://127.0.0.1:8000")!

    // Touch ID gate state.
    var lockView: NSView?
    var lockStatusLabel: NSTextField!
    var unlockButton: NSButton!
    var authInFlight = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenu()
        setDockIcon()
        startBackend()
        WKWebsiteDataStore.default().removeData(
            ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(),
            modifiedSince: Date.distantPast) { [weak self] in
            if LOCK_ON_START {
                self?.showLockSurface()
            } else {
                self?.buildWindow()
            }
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) {
        backend?.terminate()
    }

    func startBackend() {
        guard let res = Bundle.main.resourceURL else { return }
        let runtime = res.appendingPathComponent("runtime")
        let appDir = res.appendingPathComponent("app")
        let models = res.appendingPathComponent("models")
        let py = runtime.appendingPathComponent("bin/python3")
        guard FileManager.default.isExecutableFile(atPath: py.path) else {
            NSLog("Asha: embedded python missing at \(py.path)")
            return
        }
        var env = ProcessInfo.processInfo.environment
        env["KOKORO_MODEL_PATH"] = models.appendingPathComponent("kokoro/kokoro-v1.0.onnx").path
        env["KOKORO_VOICES_PATH"] = models.appendingPathComponent("kokoro/voices-v1.0.bin").path
        env["MOONSHINE_VOICE_CACHE"] = models.appendingPathComponent("moonshine").path
        // Keep runtime data out of the bundle, and default the working folder
        // to ~/Jarvis (never the app bundle).
        let support = FileManager.default.urls(for: .applicationSupportDirectory,
                                               in: .userDomainMask).first?
            .appendingPathComponent("Jarvis")
        if let support { env["JARVIS_DATA_DIR"] = support.path }
        env["JARVIS_DEFAULT_PROJECT"] =
            URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Jarvis").path
        env["PATH"] = runtime.appendingPathComponent("bin").path + ":" + (env["PATH"] ?? "")
        env["PYTHONUNBUFFERED"] = "1"

        backend = Process()
        backend?.executableURL = py
        backend?.arguments = [appDir.appendingPathComponent("ui/launch.py").path, "--no-browser"]
        backend?.currentDirectoryURL = appDir.appendingPathComponent("ui")
        backend?.environment = env
        // Redirect the server's stdout/stderr to a per-boot log file. If the log
        // cannot be opened we leave the streams unset (inherit) so the app still
        // starts — logging must never stop the server.
        let attestation = configAttestation()
        NSLog("Asha: %@", attestation)
        if let log = openServerLog() {
            backend?.standardOutput = log
            backend?.standardError = log
            // Put the keyless-config attestation at the top of the app log so a
            // support call can answer "is this build keyless?" without reading
            // files. Values never include a credential.
            if let data = (attestation + "\n").data(using: .utf8) { log.write(data) }
        }
        do { try backend?.run() } catch { NSLog("Asha backend start failed: \(error)") }
    }

    // MARK: - Server log
    // The server's stdout/stderr go to <data>/logs/server-<stamp>.log, with
    // logs/latest.log symlinked to the current boot. The newest 10 boot logs are
    // kept; nothing else is ever touched. Logging is best-effort: any failure
    // returns nil and the caller leaves the streams unset.
    private func openServerLog() -> FileHandle? {
        let fm = FileManager.default
        let base: URL
        if let dir = ProcessInfo.processInfo.environment["JARVIS_DATA_DIR"], !dir.isEmpty {
            base = URL(fileURLWithPath: dir)
        } else if let support = fm.urls(for: .applicationSupportDirectory,
                                        in: .userDomainMask).first {
            base = support.appendingPathComponent("Jarvis")
        } else {
            return nil
        }
        let logs = base.appendingPathComponent("logs")
        do {
            try fm.createDirectory(at: logs, withIntermediateDirectories: true)
        } catch {
            NSLog("Asha: server log dir unavailable: \(error)")
            return nil
        }
        let logURL = logs.appendingPathComponent("server-\(serverLogStamp()).log")
        if !fm.fileExists(atPath: logURL.path) {
            fm.createFile(atPath: logURL.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: logURL) else {
            NSLog("Asha: server log file unavailable at \(logURL.path)")
            return nil
        }
        let latest = logs.appendingPathComponent("latest.log")
        try? fm.removeItem(at: latest)
        try? fm.createSymbolicLink(atPath: latest.path,
                                   withDestinationPath: logURL.lastPathComponent)
        pruneServerLogs(in: logs)
        return handle
    }

    // Keep only the newest 10 server-*.log files. Only regular files matching
    // that prefix/suffix are candidates; latest.log and everything else survive.
    private func pruneServerLogs(in dir: URL) {
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: dir.path) else { return }
        var boot: [String] = []
        for name in names where name.hasPrefix("server-") && name.hasSuffix(".log") {
            var isDir: ObjCBool = false
            let path = dir.appendingPathComponent(name).path
            if fm.fileExists(atPath: path, isDirectory: &isDir), !isDir.boolValue {
                boot.append(name)
            }
        }
        guard boot.count > 10 else { return }
        for name in boot.sorted().prefix(boot.count - 10) {
            try? fm.removeItem(atPath: dir.appendingPathComponent(name).path)
        }
    }

    private func serverLogStamp() -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd-HHmmss"
        return f.string(from: Date())
    }

    // MARK: - Keyless-config attestation
    // Reads the shipped app/.env (the exact file the server loads) and states
    // which non-secret settings it carries, plus whether any local provider key
    // is present. Only allow-listed non-secret values are echoed; any
    // credential-shaped value is reported by NAME only, never its value. This
    // is what lets support answer "is this build keyless?" from the log alone.
    private static let nonSecretSettings: Set<String> = [
        "JARVIS_BRAIN_MODEL", "JARVIS_BRAIN_TRANSPORT", "OMNIROUTE_BASE_URL",
        "JARVIS_GATEWAY_URL", "JARVIS_DEMO_SIGNUP",
    ]
    private static let keyShape: NSRegularExpression? = try? NSRegularExpression(
        pattern: #"sk-[A-Za-z0-9_-]{16,}|sk-or-v1-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{30,}|GOCSPX-[A-Za-z0-9_-]{16,}|figd_[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9_-]{20,}|xai-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"#)

    func configAttestation() -> String {
        guard let res = Bundle.main.resourceURL else {
            return "[config] keyless build: no provider API keys bundled"
        }
        let envURL = res.appendingPathComponent("app/.env")
        guard let text = try? String(contentsOf: envURL, encoding: .utf8) else {
            return "[config] keyless build: shipped config absent; defaults only; no provider API keys bundled"
        }
        var settings: [String] = []
        var credentials: [String] = []
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            guard let eq = line.firstIndex(of: "=") else { continue }
            let key = String(line[..<eq]).trimmingCharacters(in: .whitespaces)
            let value = String(line[line.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            guard !key.isEmpty else { continue }
            let shaped = value.isEmpty ? false : (Self.keyShape?.firstMatch(
                in: value, range: NSRange(value.startIndex..., in: value)) != nil)
            if shaped || key.hasSuffix("API_KEY") || key.hasSuffix("SECRET") || key.hasSuffix("_TOKEN") {
                credentials.append(key)
            } else if Self.nonSecretSettings.contains(key) {
                settings.append("\(key)=\(value)")
            } else {
                settings.append(key)
            }
        }
        let summary = settings.isEmpty ? "(none)" : settings.joined(separator: ", ")
        if credentials.isEmpty {
            return "[config] keyless build: no provider API keys in app/.env; loaded: \(summary)"
        }
        return "[config] shipped app/.env has user-supplied credential(s) [\(credentials.joined(separator: ", "))] (values never logged); loaded: \(summary)"
    }

    func buildMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem(); mainMenu.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "Quit Asha",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let editItem = NSMenuItem(); mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit"); editItem.submenu = editMenu
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        NSApp.mainMenu = mainMenu
    }

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "mediaDevicesEnabled")
        config.preferences.setValue(true, forKey: "mediaStreamEnabled")
        config.userContentController.add(self, name: "openFilePicker")
        // Inside the native shell → inset the web UI below the traffic lights.
        config.userContentController.addUserScript(WKUserScript(
            source: "document.documentElement.classList.add('macapp');",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true))

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsMagnification = true

        makeWindow()
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        webView.load(URLRequest(url: uiURL))
    }

    // The shared window chrome. Kept byte-for-byte identical to the original
    // construction; idempotent so both the locked-start path and buildWindow()
    // can call it without creating a second window.
    private func makeWindow() {
        if window != nil { return }
        // Start windowed at a comfortable, centred size, inset from the visible
        // frame so the window edges are grabbable by the cursor. Never launch
        // filling the whole screen.
        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)
        let margin: CGFloat = 40
        let width = min(1280, visible.width - margin * 2)
        let height = min(800, visible.height - margin * 2)
        let frame = NSRect(x: visible.midX - width / 2,
                           y: visible.midY - height / 2,
                           width: width, height: height)
        // NOTE: do NOT put `.fullScreen` (NSWindowStyleMaskFullScreen) in the
        // initial style mask. That bit makes AppKit/Accessibility report the
        // window as permanently fullscreen (AXFullScreen = true), which blocks
        // resizing and makes the green button a no-op. The window is resizable,
        // so the green button can still enter/leave fullscreen normally.
        window = NSWindow(contentRect: frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable,
                                      .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Asha"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        if #available(macOS 11.0, *) { window.titlebarSeparatorStyle = .none }
        window.isMovableByWindowBackground = true
        // A sane floor so the window cannot be collapsed to nothing, while
        // still allowing the responsive narrow/stacked layouts (<= 820px).
        window.contentMinSize = NSSize(width: 640, height: 480)
    }

    // The Dock icon comes from Info.plist, but setting it explicitly keeps it
    // correct when the binary is launched directly or the icon cache is stale.
    func setDockIcon() {
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
           let image = NSImage(contentsOf: url) {
            NSApp.applicationIconImage = image
        }
    }

    // MARK: - Touch ID gate (LocalAuthentication)
    // LOCKED START: show a lock surface and auto-trigger Touch ID. The WKWebView
    // is not created and the UI URL is not loaded until authentication succeeds.
    // Policy per attempt (Apple: evaluatePolicy(_:localizedReason:reply:)):
    //   primary  = .deviceOwnerAuthenticationWithBiometrics (Touch ID)
    //   fallback = .deviceOwnerAuthentication (biometrics with Mac password)
    func showLockSurface() {
        makeWindow()

        let container = NSView(frame: window.contentLayoutRect)
        container.wantsLayer = true
        container.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false

        let lockImage = NSImageView()
        lockImage.image = NSImage(systemSymbolName: "lock.fill", accessibilityDescription: "Locked")
        if #available(macOS 11.0, *) {
            lockImage.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 44, weight: .regular)
        }
        lockImage.contentTintColor = .secondaryLabelColor
        stack.addArrangedSubview(lockImage)

        let title = NSTextField(labelWithString: "Locked — unlock with Touch ID")
        title.font = NSFont.boldSystemFont(ofSize: 20)
        title.alignment = .center
        stack.addArrangedSubview(title)

        let status = NSTextField(labelWithString: "Authenticate to continue.")
        status.font = NSFont.systemFont(ofSize: 13)
        status.textColor = .secondaryLabelColor
        status.alignment = .center
        status.maximumNumberOfLines = 2
        status.lineBreakMode = .byWordWrapping
        stack.addArrangedSubview(status)
        lockStatusLabel = status

        let button = NSButton(title: "Unlock with Touch ID", target: self, action: #selector(attemptUnlock))
        button.bezelStyle = .rounded
        button.keyEquivalent = "\r"
        stack.addArrangedSubview(button)
        unlockButton = button

        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: container.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: container.trailingAnchor, constant: -24),
        ])

        window.contentView = container
        lockView = container
        window.makeKeyAndOrderFront(nil)
        attemptUnlock()
    }

    // One Touch ID / device-password attempt. Conservative default: any failure
    // or cancellation keeps the lock surface and never unlocks.
    @objc func attemptUnlock() {
        guard !authInFlight else { return }
        authInFlight = true
        unlockButton.isEnabled = false

        // Fresh context per attempt; kept strongly referenced by the reply
        // closure until it fires, then invalidated.
        let context = LAContext()
        context.localizedCancelTitle = "Cancel"

        // canEvaluatePolicy must not be called from the reply closure (deadlock).
        var biometricsError: NSError?
        let canUseBiometrics = context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics,
                                                         error: &biometricsError)
        let policy: LAPolicy
        if canUseBiometrics {
            policy = .deviceOwnerAuthenticationWithBiometrics
            lockStatusLabel.stringValue = "Waiting for Touch ID…"
        } else {
            var passwordError: NSError?
            guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &passwordError) else {
                authInFlight = false
                unlockButton.isEnabled = true
                lockStatusLabel.stringValue =
                    "No authentication available on this Mac. Touch ID or a Mac password is required."
                return
            }
            policy = .deviceOwnerAuthentication
            lockStatusLabel.stringValue = "Touch ID unavailable — unlock with your Mac password."
        }

        // localizedReason must not contain the app name (Apple docs).
        context.evaluatePolicy(policy, localizedReason: "Unlock to open the app") { [weak self] success, _ in
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                context.invalidate()
                self.authInFlight = false
                self.unlockButton.isEnabled = true
                if success {
                    self.unlockApp()
                } else if policy == .deviceOwnerAuthenticationWithBiometrics {
                    self.lockStatusLabel.stringValue = "Touch ID could not verify you. Try again."
                } else {
                    self.lockStatusLabel.stringValue = "Touch ID unavailable — unlock with your Mac password."
                }
            }
        }
    }

    // Authentication succeeded: build the WebView for the first time and load UI.
    func unlockApp() {
        lockStatusLabel.stringValue = "Unlocked"
        buildWindow()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation?,
                 withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            self?.webView.load(URLRequest(url: self!.uiURL))
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        window.makeFirstResponder(webView)
    }

    func webView(_ webView: WKWebView,
                 requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(type == .microphone ? .grant : .prompt)
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 from origin: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.begin { result in completionHandler(result == .OK ? panel.urls : nil) }
    }

    func userContentController(_ userContentController: WKUserContentController,
                              didReceive message: WKScriptMessage) {
        guard message.name == "openFilePicker", let body = message.body as? [String: Any] else { return }
        let multi = body["multiple"] as? Bool ?? false
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = multi
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.begin { [weak self] result in
            guard result == .OK, !panel.urls.isEmpty else {
                self?.evalJS("window._onFilePickerResult && window._onFilePickerResult([])")
                return
            }
            var items: [[String: Any]] = []
            for url in panel.urls {
                guard let data = try? Data(contentsOf: url) else { continue }
                let mime = url.pathExtension.lowercased() == "pdf" ? "application/pdf" : "text/plain"
                items.append(["dataUrl": "data:\(mime);base64,\(data.base64EncodedString())",
                              "name": url.lastPathComponent, "w": 0, "h": 0, "mime": mime])
            }
            if let d = try? JSONSerialization.data(withJSONObject: items),
               let json = String(data: d, encoding: .utf8) {
                self?.evalJS("window._onFilePickerResult && window._onFilePickerResult(\(json))")
            }
        }
    }

    private func evalJS(_ code: String) {
        DispatchQueue.main.async { [weak self] in self?.webView.evaluateJavaScript(code) }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()

@main
struct AshaApp {
    static func main() {
        app.delegate = delegate
        app.run()
    }
}
