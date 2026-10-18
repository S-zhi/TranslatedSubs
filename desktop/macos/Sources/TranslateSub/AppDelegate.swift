import AppKit
import WebKit
import DesktopCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private let appName = AppIdentity.displayName(info: Bundle.main.infoDictionary ?? [:])
    private let runtime = DesktopRuntime()
    private let exporter = ExportController()
    private var client: BackendClient?
    private var quitPending = false
    private var terminating = false
    private var statusLabel: NSTextField!

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMenus()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = appName
        window.minSize = NSSize(width: 900, height: 620)
        window.isReleasedWhenClosed = false
        window.center()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.userContentController.add(self, name: "desktop")
        webView = WKWebView(frame: window.contentView!.bounds, configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window.contentView?.addSubview(webView)
        statusLabel = NSTextField(wrappingLabelWithString: "正在启动本地服务…")
        statusLabel.alignment = .center
        statusLabel.frame = NSRect(x: 80, y: 350, width: 1040, height: 80)
        statusLabel.autoresizingMask = [.width, .minYMargin, .maxYMargin]
        window.contentView?.addSubview(statusLabel)
        reopen()
        runtime.onReady = { [weak self] client, token in self?.load(client: client, token: token) }
        runtime.onError = { [weak self] error in
            self?.client = nil
            self?.webView.stopLoading()
            self?.webView.isHidden = true
            self?.statusLabel.stringValue = error.localizedDescription
            self?.statusLabel.isHidden = false
        }
        do { try runtime.start() } catch { statusLabel.stringValue = error.localizedDescription }
    }

    private func load(client: BackendClient, token: String) {
        do {
            let cookie = try client.origin.cookie(token: token)
            webView.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) { [weak self] in
                guard let self, !self.terminating, !self.runtime.process.exited else { return }
                self.client = client
                self.statusLabel.isHidden = true
                self.webView.load(URLRequest(url: client.origin.url))
            }
        } catch { statusLabel.stringValue = error.localizedDescription }
    }

    private func installMenus() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "显示 \(appName)", action: #selector(reopen), keyEquivalent: "0").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 \(appName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        let edit = NSMenu(title: "编辑")
        for (title, selector, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"),
                                      ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(selector), keyEquivalent: key)
        }
        editItem.submenu = edit
        main.addItem(editItem)
        NSApp.mainMenu = main
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "captions.bubble", accessibilityDescription: appName)
        statusItem.button?.toolTip = appName
        let statusMenu = NSMenu()
        statusMenu.addItem(withTitle: "打开 \(appName)", action: #selector(reopen), keyEquivalent: "").target = self
        statusMenu.addItem(.separator())
        statusMenu.addItem(withTitle: "退出 \(appName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        statusItem.menu = statusMenu
    }

    @objc private func reopen() { window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { reopen(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminating || quitPending { return .terminateLater }
        quitPending = true
        Task {
            var count: Int?
            if let client, let body = try? await client.json("api/desktop/tasks/active"),
               let number = body["count"] as? NSNumber,
               CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue >= 0,
               number.doubleValue.rounded() == number.doubleValue, number.doubleValue < Double(Int.max) {
                count = number.intValue
            }
            if (count ?? 1) > 0 && runtime.process.pid > 0 {
                reopen()
                let alert = NSAlert()
                alert.messageText = "退出 \(appName)？"
                alert.informativeText = count.map { "还有 \($0) 个任务正在运行。退出会停止处理，已完成结果和任务记录会保留。" }
                    ?? "暂时无法确认任务状态。退出会停止本地服务及正在进行的处理，任务记录会保留。"
                alert.addButton(withTitle: "继续运行")
                alert.addButton(withTitle: "停止并退出")
                guard alert.runModal() == .alertSecondButtonReturn else {
                    quitPending = false
                    sender.reply(toApplicationShouldTerminate: false)
                    return
                }
            }
            terminating = true
            exporter.cancelExport()
            webView.stopLoading()
            runtime.shutdown { sender.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    private func trusted(_ frame: WKFrameInfo) -> Bool {
        guard frame.isMainFrame, let origin = client?.origin else { return false }
        let security = frame.securityOrigin
        return security.protocol == "http" && security.host == "127.0.0.1" && security.port == origin.port
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard !terminating, message.name == "desktop", message.webView === webView,
              trusted(message.frameInfo), let client,
              let command = try? BridgeCommand.parse(message.body) else { return }
        switch command {
        case let .export(taskID, kind): exporter.export(taskID: taskID, kind: kind, client: client, window: window)
        case let .openExternal(url): NSWorkspace.shared.open(url)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard !terminating, let url = navigationAction.request.url, let client else { decisionHandler(.cancel); return }
        if client.origin.allows(url) && navigationAction.targetFrame != nil { decisionHandler(.allow); return }
        if navigationAction.navigationType == .linkActivated, trusted(navigationAction.sourceFrame), BridgeCommand.isExternalURL(url) {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard let url = navigationResponse.response.url, client?.origin.allows(url) == true,
              navigationResponse.canShowMIMEType else { decisionHandler(.cancel); return }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        guard !terminating, trusted(frame) else { completionHandler(nil); return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { response in completionHandler(response == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        guard trusted(frame) else { completionHandler(); return }
        let alert = NSAlert()
        alert.messageText = appName
        alert.informativeText = String(message.prefix(4096))
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        guard trusted(frame) else { completionHandler(false); return }
        let alert = NSAlert()
        alert.messageText = appName
        alert.informativeText = String(message.prefix(4096))
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }
}
