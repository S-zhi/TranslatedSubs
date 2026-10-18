import Testing
import Foundation
import Darwin
@testable import DesktopCore

struct NativeIntegrationTests {
    private func withDirectory(_ operation: (URL) throws -> Void) throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        try operation(directory)
    }

    @Test func appNameUsesPackagingMetadata() {
        #expect(AppIdentity.displayName(info: ["CFBundleDisplayName": "字幕工作台", "CFBundleName": "Internal"]) == "字幕工作台")
        #expect(AppIdentity.displayName(info: ["CFBundleDisplayName": " \n", "CFBundleName": "Packaged Name"]) == "Packaged Name")
        #expect(AppIdentity.displayName(info: [:]) == "TranslateSub")
    }

    @Test func duplicateOwnersAreRejectedAndLockCanBeReacquired() throws {
        try withDirectory { directory in
            var owner: DesktopInstanceLock? = try DesktopInstanceLock(directory: directory)
            _ = withExtendedLifetime(owner) {
                #expect(throws: DesktopInstanceLock.Failure.alreadyRunning) {
                    try DesktopInstanceLock(directory: directory)
                }
            }
            owner = nil
            let next = try DesktopInstanceLock(directory: directory)
            withExtendedLifetime(next) {
                #expect(FileManager.default.fileExists(atPath: directory.appendingPathComponent("desktop.lock").path))
            }
        }
    }

    @Test func lockRejectsSymlinksAndInsecureFiles() throws {
        try withDirectory { directory in
            let target = directory.appendingPathComponent("untouched")
            let lock = directory.appendingPathComponent("desktop.lock")
            try Data("original".utf8).write(to: target)
            try FileManager.default.createSymbolicLink(at: lock, withDestinationURL: target)
            #expect(throws: DesktopInstanceLock.Failure.invalidFile) { try DesktopInstanceLock(directory: directory) }
            #expect(try String(contentsOf: target, encoding: .utf8) == "original")
            try FileManager.default.removeItem(at: lock)
            #expect(FileManager.default.createFile(atPath: lock.path, contents: Data(), attributes: [.posixPermissions: 0o644]))
            #expect(throws: DesktopInstanceLock.Failure.invalidFile) { try DesktopInstanceLock(directory: directory) }
        }
    }

    @Test func exportPreservesDirectoryDestinationAndSpecialParentPath() throws {
        try withDirectory { directory in
            let parent = directory.appendingPathComponent("课程 #1 % 完成", isDirectory: true)
            try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: false)
            let source = directory.appendingPathComponent("source")
            try Data("media".utf8).write(to: source)
            let destination = parent.appendingPathComponent("result.mp4")
            try AtomicExport.save(source: source, destination: destination)
            #expect(try String(contentsOf: destination, encoding: .utf8) == "media")
            #expect(throws: (any Error).self) { try AtomicExport.save(source: source, destination: parent) }
            #expect(try FileManager.default.contentsOfDirectory(atPath: parent.path) == ["result.mp4"])
        }
    }

    @Test @MainActor func ownedProcessRejectsDuplicateStartAndLeavesUnownedProcessAlive() async throws {
        let owned = OwnedBackendProcess()
        let unrelated = Process()
        unrelated.executableURL = URL(fileURLWithPath: "/bin/sleep")
        unrelated.arguments = ["30"]
        try unrelated.run()
        defer { if unrelated.isRunning { unrelated.terminate() }; unrelated.waitUntilExit(); owned.finish() }
        try owned.start(executable: URL(fileURLWithPath: "/bin/sleep"), arguments: ["30"], environment: [:])
        let pid = owned.pid
        #expect(owned.owns(descriptorPID: pid))
        #expect(!owned.owns(descriptorPID: unrelated.processIdentifier))
        #expect(throws: (any Error).self) {
            try owned.start(executable: URL(fileURLWithPath: "/bin/sleep"), arguments: ["30"], environment: [:])
        }
        #expect(owned.pid == pid)
        owned.signalGroup(SIGTERM)
        for _ in 0..<100 {
            if owned.exited { break }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        #expect(owned.exited)
        owned.finish()
        #expect(owned.pid == 0)
        #expect(unrelated.isRunning)
    }

    @Test @MainActor func failedSpawnNeverClaimsOwnership() throws {
        let owned = OwnedBackendProcess()
        #expect(throws: (any Error).self) {
            try owned.start(executable: URL(fileURLWithPath: "/nonexistent/translatesub-backend"),
                            arguments: [], environment: [:])
        }
        #expect(owned.pid == 0)
        #expect(!owned.owns(descriptorPID: getpid()))
        // Cleanup after a failed startup is safe and does not signal our group.
        owned.finish()
        #expect(owned.pid == 0)
    }

    @Test @MainActor func gracefulGroupSignalReachesChildBeforeForcedCleanup() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let ready = directory.appendingPathComponent("ready")
        let stopped = directory.appendingPathComponent("stopped")
        let owned = OwnedBackendProcess()
        defer { owned.finish() }
        // Positional arguments preserve paths; no interpolation into shell program text.
        let script = "trap 'exit 0' TERM; /bin/sh -c 'trap \"printf stopped > \\\"$2\\\"; exit 0\" TERM; printf ready > \"$1\"; while :; do /bin/sleep 1; done' child \"$1\" \"$2\" & wait"
        try owned.start(executable: URL(fileURLWithPath: "/bin/sh"),
                        arguments: ["-c", script, "leader", ready.path, stopped.path], environment: [:])
        for _ in 0..<100 {
            if FileManager.default.fileExists(atPath: ready.path) { break }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        try #require(FileManager.default.fileExists(atPath: ready.path))
        owned.signalGroup(SIGTERM)
        for _ in 0..<100 {
            if FileManager.default.fileExists(atPath: stopped.path) { break }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        #expect(FileManager.default.fileExists(atPath: stopped.path))
    }
}
