import AppKit
import UniformTypeIdentifiers
import DesktopCore

@MainActor
final class ExportController {
    private var task: Task<Void, Never>?
    private var savePanel: NSSavePanel?
    private var progress: NSPanel?
    private var busy = false

    func export(taskID: String, kind: ExportKind, client: BackendClient, window: NSWindow) {
        guard !busy else { return }
        busy = true
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 320, height: 110),
                            styleMask: [.titled], backing: .buffered, defer: false)
        panel.title = "导出文件"
        let label = NSTextField(labelWithString: "正在准备导出文件…")
        label.frame = NSRect(x: 20, y: 65, width: 280, height: 22)
        let button = NSButton(title: "取消", target: self, action: #selector(cancelExport))
        button.frame = NSRect(x: 220, y: 15, width: 80, height: 30)
        panel.contentView?.addSubview(label)
        panel.contentView?.addSubview(button)
        window.beginSheet(panel)
        progress = panel
        task = Task {
            defer { self.busy = false; self.task = nil; self.endProgress(window: window) }
            var downloaded: URL?
            defer { if let downloaded { try? FileManager.default.removeItem(at: downloaded) } }
            do {
                downloaded = try await client.download(taskID: taskID, kind: kind)
                try Task.checkCancellation()
                self.endProgress(window: window)
                let save = NSSavePanel()
                save.title = "导出\(kind == .video ? "视频" : "字幕")"
                save.nameFieldStringValue = "\(taskID).\(kind.fileExtension)"
                save.allowedContentTypes = [kind == .video ? .mpeg4Movie : (UTType(filenameExtension: "srt") ?? .plainText)]
                save.canCreateDirectories = true
                self.savePanel = save
                let response = await withCheckedContinuation { continuation in
                    save.beginSheetModal(for: window) { continuation.resume(returning: $0) }
                }
                self.savePanel = nil
                guard response == .OK, let destination = save.url, let downloaded else { return }
                try Task.checkCancellation()
                // Copy/rename on a worker, with cancellation propagated during large copies.
                let copy = Task.detached(priority: .userInitiated) {
                    try AtomicExport.save(source: downloaded, destination: destination,
                                          isCancelled: { Task.isCancelled })
                }
                try await withTaskCancellationHandler(operation: { try await copy.value }, onCancel: { copy.cancel() })
            } catch {
                if !Task.isCancelled && !(error is CancellationError) {
                    self.endProgress(window: window)
                    let alert = NSAlert()
                    alert.messageText = "导出失败"
                    alert.informativeText = "无法下载或保存文件。请检查任务产物、目标目录权限和可用空间后重试。"
                    alert.beginSheetModal(for: window, completionHandler: nil)
                }
            }
        }
    }

    @objc func cancelExport() { task?.cancel(); savePanel?.cancel(nil) }

    private func endProgress(window: NSWindow) {
        if let progress { window.endSheet(progress); progress.orderOut(nil); self.progress = nil }
    }
}
