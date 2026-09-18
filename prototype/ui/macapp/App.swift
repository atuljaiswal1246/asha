import AppKit
import LocalAuthentication
import WebKit

// Touch ID / password lock on startup. LOCK_ON_START = true makes the app show the
// lock surface (Touch ID, with Mac-password fallback and retry) before the UI loads.
// LOCK_ON_START = false (current) skips straight to the normal UI — the lock is never
// constructed or shown. All lock code (showLockSurface/attemptUnlock/unlockApp) is
// intentionally left intact, so re-enabling is this one line: LOCK_ON_START = true.
let LOCK_ON_START = false

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
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
        ensureBackend()
        // Clear the WebView cache/data store so the app ALWAYS loads the current
        // code. WKWebView otherwise persists a stale/broken page across relaunches
        // (this caused a white screen + 404 in the work iframe after a backend fix).
        WKWebsiteDataStore.default().removeData(
            ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(),
            modifiedSince: Date.distantPast
        ) { [weak self] in
            if LOCK_ON_START {
                self?.showLockSurface()
            } else {
                self?.buildWindow()
            }
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func ensureBackend() {
        // Is the WS bot already up? If not, start the local engine (llama server + bot + static page).
        let probe = "http://127.0.0.1:8000"
        var request = URLRequest(url: URL(string: probe)!)
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { _, _, _ in
            // fallthrough: even if 8000 is up, ensure the bot WS is up
            let wsProbe = "http://127.0.0.1:7860"
            var r2 = URLRequest(url: URL(string: wsProbe)!)
            r2.timeoutInterval = 2
            URLSession.shared.dataTask(with: r2) { _, _, _ in
                self.startBackendIfNeeded(stillProbing: true)
            }.resume()
        }.resume()
    }

    func startBackendIfNeeded(stillProbing: Bool) {
        let home = ProcessInfo.processInfo.environment["VOICE_BACKEND_HOME"]
            ?? NSHomeDirectory() + "/Desktop/Jarvis/prototype/ui"
        let runScript = home + "/run_ui.sh"
        guard FileManager.default.fileExists(atPath: runScript) else {
            NSLog("UI backend: run_ui.sh not found at \(runScript)")
            return
        }
        backend = Process()
        backend?.executableURL = URL(fileURLWithPath: "/bin/bash")
        backend?.arguments = [runScript]
        backend?.currentDirectoryURL = URL(fileURLWithPath: home)
        // Redirect the server's stdout/stderr to a per-boot log file. If the log
        // cannot be opened we leave the streams unset (inherit) so the app still
        // starts — logging must never stop the server.
        if let log = openServerLog() {
            backend?.standardOutput = log
            backend?.standardError = log
        }
        do { try backend?.run() } catch { NSLog("UI backend start failed: \(error)") }
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
            NSLog("Jarvis: server log dir unavailable: \(error)")
            return nil
        }
        let logURL = logs.appendingPathComponent("server-\(serverLogStamp()).log")
        if !fm.fileExists(atPath: logURL.path) {
            fm.createFile(atPath: logURL.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: logURL) else {
            NSLog("Jarvis: server log file unavailable at \(logURL.path)")
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

    func buildMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "Quit Asha",
                        action: #selector(NSApplication.terminate(_:)),
                        keyEquivalent: "q")

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editItem.submenu = editMenu
        editMenu.addItem(withTitle: "Undo",
                         action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo",
                         action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut",
                         action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy",
                         action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste",
                         action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All",
                         action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        NSApp.mainMenu = mainMenu
    }

    func buildWindow() {
        let config = WKWebViewConfiguration()
        // Enable getUserMedia (microphone) inside WKWebView.
        config.preferences.setValue(true, forKey: "mediaDevicesEnabled")
        config.preferences.setValue(true, forKey: "mediaStreamEnabled")

        // JS→Swift bridge for native file picker (WKWebView swallows <input type="file"> clicks).
        config.userContentController.add(self, name: "openFilePicker")

        // Tell the web UI it is inside the native shell. The window is
        // .fullSizeContentView under a transparent title bar, so the UI has to
        // inset itself below the traffic lights; `html.macapp` does that.
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
        let startFrame = NSRect(x: visible.midX - width / 2,
                                y: visible.midY - height / 2,
                                width: width, height: height)
        // NOTE: do NOT put `.fullScreen` (NSWindowStyleMaskFullScreen) in the
        // initial style mask. That bit makes AppKit/Accessibility report the
        // window as permanently fullscreen (AXFullScreen = true), which blocks
        // resizing and makes the green button a no-op. The window is resizable,
        // so the green button can still enter/leave fullscreen normally.
        window = NSWindow(
            contentRect: startFrame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false)
        window.title = "Asha"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        if #available(macOS 11.0, *) { window.titlebarSeparatorStyle = .none }
        // Drag the window from any empty surface (WKWebView has no
        // -webkit-app-region, so AppKit has to do it).
        window.isMovableByWindowBackground = true
        // A sane floor so the window cannot be collapsed to nothing, while
        // still allowing the responsive narrow/stacked layouts (<= 820px).
        window.contentMinSize = NSSize(width: 640, height: 480)
    }

    // The Dock icon comes from Info.plist, but setting it explicitly keeps it
    // correct when the binary is launched directly or the cache is stale.
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

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation?, withError error: Error) {
        // Backend not up yet — retry shortly.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            self?.webView.load(URLRequest(url: self!.uiURL))
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        window.makeFirstResponder(webView)
    }

    func webView(_ webView: WKWebView,
                 requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo,
                 type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        switch type {
        case .microphone:
            decisionHandler(.grant)
        default:
            decisionHandler(.prompt)
        }
    }

    // Answer opencode web's basic-auth challenge (username/password from .env)
    // so the same-window toggle into opencode actually loads.
    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.host == "127.0.0.1",
              challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodHTTPBasic else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        let env = ProcessInfo.processInfo.environment
        let user = env["OPENCODE_SERVER_USERNAME"] ?? env["OPENCODE_SERVER_USER"] ?? "opencode"
        let pass = env["OPENCODE_SERVER_PASSWORD"] ?? ""
        if !pass.isEmpty {
            completionHandler(.useCredential, URLCredential(user: user, password: pass, persistence: .forSession))
        } else {
            completionHandler(.performDefaultHandling, nil)
        }
    }

    // Required for WKWebView to open a native file picker when an <input type="file">
    // is tapped. Without this, WebKit silently swallows every file-input interaction.
    func webView(_ webView: WKWebView,
                 runOpenPanelWith parameters: WKOpenPanelParameters,
                 from origin: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.begin { result in
            NSLog("[APP] openPanel result=%d urls=%d", result == .OK ? 1 : 0, panel.urls.count)
            completionHandler(result == .OK ? panel.urls : nil)
        }
    }

    // JS→Swift bridge: open native file picker, return results to JS.
    // Bypasses WKWebView's broken <input type="file"> handling entirely.
    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "openFilePicker",
              let body = message.body as? [String: Any] else { return }
        let multi = body["multiple"] as? Bool ?? false

        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = multi
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        // No content type filter — let the user pick any file.
        // The backend validates and handles what it can (images, PDFs, text/code).
        panel.begin { [weak self] result in
            guard result == .OK, !panel.urls.isEmpty else {
                self?.evalJS("window._onFilePickerResult && window._onFilePickerResult([])")
                return
            }
            var items: [[String: Any]] = []
            for url in panel.urls {
                guard let data = try? Data(contentsOf: url) else { continue }
                let b64 = data.base64EncodedString()
                let ext = url.pathExtension.lowercased()
                let isImage = ["jpg","jpeg","png","webp","gif","bmp","tiff","tif","heic","heif"].contains(ext)
                let mime: String
                if isImage {
                    mime = ext == "png" ? "image/png"
                         : ext == "webp" ? "image/webp"
                         : ext == "gif" ? "image/gif"
                         : ext == "bmp" ? "image/bmp"
                         : ext == "tiff" || ext == "tif" ? "image/tiff"
                         : ext == "heic" || ext == "heif" ? "image/heic"
                         : "image/jpeg"
                } else if ext == "pdf" {
                    mime = "application/pdf"
                } else {
                    // text file — detect MIME from extension
                    mime = ext == "csv" ? "text/csv"
                         : ext == "json" ? "application/json"
                         : ext == "xml" ? "application/xml"
                         : ext == "py" ? "text/x-python"
                         : ext == "js" ? "text/javascript"
                         : ext == "ts" ? "text/typescript"
                         : ext == "sh" ? "text/x-shellscript"
                         : ext == "md" ? "text/markdown"
                         : ext == "html" || ext == "htm" ? "text/html"
                         : ext == "css" ? "text/css"
                         : ext == "yaml" || ext == "yml" ? "text/yaml"
                         : "text/plain"
                }
                let dataUrl = "data:\(mime);base64,\(b64)"
                var w = 0, h = 0
                if isImage, let img = NSImage(data: data) {
                    w = Int(img.size.width)
                    h = Int(img.size.height)
                }
                items.append([
                    "dataUrl": dataUrl,
                    "name": url.lastPathComponent,
                    "w": w, "h": h,
                    "mime": mime
                ])
            }
            if let jsonData = try? JSONSerialization.data(withJSONObject: items),
               let json = String(data: jsonData, encoding: .utf8) {
                self?.evalJS("window._onFilePickerResult && window._onFilePickerResult(\(json))")
            }
        }
    }

    private func evalJS(_ code: String) {
        DispatchQueue.main.async { [weak self] in
            self?.webView.evaluateJavaScript(code)
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()

@main
struct VoiceAssistantApp {
    static func main() {
        app.delegate = delegate
        app.run()
    }
}
