import Foundation
import Darwin

public enum AtomicExport {
    /// Stage on the destination volume, then rename atomically. Cancellation or a copy
    /// failure leaves any existing destination untouched. Never trust response filenames.
    public static func save(source: URL, destination: URL, isCancelled: () -> Bool = { false }) throws {
        let parent = destination.deletingLastPathComponent()
        let staged = parent.appendingPathComponent(".translatesub-\(UUID().uuidString).partial")
        defer { try? FileManager.default.removeItem(at: staged) }
        let input = try FileHandle(forReadingFrom: source)
        defer { try? input.close() }
        let fd = open(staged.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw DesktopError.fileOperation }
        let output = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? output.close() }
        while true {
            if isCancelled() { throw CancellationError() }
            guard let chunk = try input.read(upToCount: 1024 * 1024), !chunk.isEmpty else { break }
            try output.write(contentsOf: chunk)
        }
        try output.synchronize()
        if isCancelled() { throw CancellationError() }
        guard rename(staged.path, destination.path) == 0 else { throw DesktopError.fileOperation }
    }
}
