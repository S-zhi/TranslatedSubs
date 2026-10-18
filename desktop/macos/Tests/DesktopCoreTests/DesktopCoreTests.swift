import Testing
import Foundation
import Darwin
@testable import DesktopCore

final class DesktopCoreTests {
    private var directories: [URL] = []
    deinit { directories.forEach { try? FileManager.default.removeItem(at: $0) } }
    private func temporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o700])
        directories.append(directory)
        return directory
    }

    @Test func testBundlePathsAndExplicitDevelopmentOverride() throws {
        let resources = URL(fileURLWithPath: "/Applications/TranslateSub.app/Contents/Resources")
        let paths = try BackendPaths(resources: resources, arguments: [])
        #expect(paths.executable.path == resources.path + "/backend/translatedsubs-backend")
        #expect(paths.ffmpeg.path == resources.path + "/bin/ffmpeg")
        #expect(paths.ffprobe.path == resources.path + "/bin/ffprobe")
        let development = try BackendPaths(resources: resources, arguments: [
            "--backend-executable", "/tmp/backend", "--ffmpeg", "/tmp/ffmpeg", "--ffprobe", "/tmp/ffprobe"
        ])
        #expect(development.executable.path == "/tmp/backend")
        #expect(development.ffprobe.path == "/tmp/ffprobe")
        for args in [["--backend-executable", "python"], ["--python", "/usr/bin/python"],
                     ["--backend-executable"], ["--ffmpeg", "/tmp/ffmpeg"],
                     ["--backend-executable", "/a", "--backend-executable", "/b"]] {
            #expect(throws: (any Error).self) { try BackendPaths(resources: resources, arguments: args) }
        }
    }

    @Test func testSessionDescriptorRejectsMalformedAndOversizedInput() throws {
        let valid = Data(#"{"port":49152,"pid":123,"instance_id":"session-123"}"#.utf8)
        let result = try SessionDescriptor.decode(valid)
        #expect(result.port == 49152)
        #expect(result.pid == 123)
        #expect(result.instanceID == "session-123")
        for value in [#"{"port":0,"pid":123,"instance_id":"a"}"#,
                      #"{"port":65536,"pid":123,"instance_id":"a"}"#,
                      #"{"port":80,"pid":1,"instance_id":"a"}"#,
                      #"{"port":80,"pid":123,"instance_id":""}"#,
                      #"{"port":"80","pid":123,"instance_id":"a"}"#, "{}"] {
            #expect(throws: (any Error).self) { try SessionDescriptor.decode(Data(value.utf8)) }
        }
        #expect(throws: (any Error).self) { try SessionDescriptor.decode(Data(repeating: 65, count: 16_385)) }
    }

    @Test func testDescriptorRequiresPrivateRegularFileAndRejectsSymlink() throws {
        let directory = try temporaryDirectory()
        let path = directory.appendingPathComponent("session.json")
        let data = Data(#"{"port":49152,"pid":123,"instance_id":"a"}"#.utf8)
        #expect(FileManager.default.createFile(atPath: path.path, contents: data,
                                                     attributes: [.posixPermissions: 0o600]))
        #expect(try SessionDescriptor.read(path).port == 49152)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: path.path)
        #expect(throws: (any Error).self) { try SessionDescriptor.read(path) }
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        let link = directory.appendingPathComponent("link.json")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: path)
        #expect(throws: (any Error).self) { try SessionDescriptor.read(link) }
        #expect(throws: (any Error).self) { try SessionDescriptor.read(directory) }
    }

    @Test func testOriginMatchesExactLoopbackHostSchemeAndPort() throws {
        let origin = try LoopbackOrigin(port: 54321)
        #expect(origin.allows(URL(string: "http://127.0.0.1:54321/api/tasks")!))
        for value in ["http://localhost:54321/", "http://127.0.0.1:54322/", "https://127.0.0.1:54321/",
                      "http://127.0.0.1.evil.test:54321/", "file:///tmp/test", "javascript:alert(1)",
                      "http://user@127.0.0.1:54321/", "http://[::1]:54321/"] {
            #expect(!(origin.allows(URL(string: value)!)))
        }
        #expect(throws: (any Error).self) { try LoopbackOrigin(port: 0) }
    }

    @Test func testPrivateSessionCookieAndTokenEntropy() throws {
        let token = try SessionToken.generate()
        #expect(token.count == 64)
        #expect(token != (try SessionToken.generate()))
        let cookie = try LoopbackOrigin(port: 54321).cookie(token: token)
        #expect(cookie.name == "subtrans_desktop_session")
        #expect(cookie.domain == "127.0.0.1")
        #expect(cookie.path == "/")
        #expect(cookie.value == token)
        #expect(cookie.isHTTPOnly)
        #expect(cookie.isSessionOnly)
        #expect(cookie.sameSitePolicy == .sameSiteStrict)
        #expect(!(cookie.isSecure))
    }

    @Test func testExportUsesOnlyFixedExistingArtifactRoutes() throws {
        let origin = try LoopbackOrigin(port: 54321)
        #expect(try BridgeCommand.parse(["action": "export", "taskId": "task_a1b2c3d4", "kind": "video"]) == .export(taskID: "task_a1b2c3d4", kind: .video))
        #expect(try BridgeCommand.artifactURL(origin: origin, taskID: "task_a1b2c3d4", kind: .video).absoluteString == "http://127.0.0.1:54321/api/tasks/task_a1b2c3d4/download")
        #expect(try BridgeCommand.artifactURL(origin: origin, taskID: "task_a1b2c3d4", kind: .subtitle).path == "/api/tasks/task_a1b2c3d4/subtitle")
        for taskID in ["../secret", "/tmp/file", "%2e%2e", "a/b", "a?x=y", "a#b", "a\\b", "", "任务", String(repeating: "a", count: 129)] {
            #expect(throws: (any Error).self) { try BridgeCommand.parse(["action": "export", "taskId": taskID, "kind": "video"]) }
            #expect(throws: (any Error).self) { try BridgeCommand.artifactURL(origin: origin, taskID: taskID, kind: .video) }
        }
    }

    @Test func testBridgeRejectsUnknownFieldsWrongTypesAndOversizedMessages() {
        let invalid: [Any] = ["export", ["action": "export", "taskId": "ok", "kind": "audio"],
                              ["action": "export", "taskId": "ok", "kind": "video", "url": "https://evil.test"],
                              ["action": "export", "taskId": 10, "kind": "video"],
                              ["action": "readFile", "path": "/tmp/a"],
                              ["action": "openExternal", "url": String(repeating: "a", count: 5000)]]
        for value in invalid { #expect(throws: (any Error).self) { try BridgeCommand.parse(value) } }
    }

    @Test func testExternalLinksAllowOnlyHTTPSWithoutCredentials() throws {
        let url = URL(string: "https://example.com/releases/latest")!
        #expect(try BridgeCommand.parse(["action": "openExternal", "url": url.absoluteString]) == .openExternal(url))
        for value in ["http://example.com", "file:///tmp/a", "javascript:alert(1)",
                      "https://user:secret@example.com/a", "https:///", "data:text/html,test"] {
            #expect(throws: (any Error).self) { try BridgeCommand.parse(["action": "openExternal", "url": value]) }
        }
    }

    @Test func testAtomicSaveReplacesCompletedFileAndCleansStage() throws {
        let directory = try temporaryDirectory()
        let source = directory.appendingPathComponent("source")
        let destination = directory.appendingPathComponent("destination")
        let data = Data(repeating: 42, count: 2_100_000)
        try data.write(to: source)
        try Data("old".utf8).write(to: destination)
        try AtomicExport.save(source: source, destination: destination)
        #expect(try Data(contentsOf: destination) == data)
        #expect(try FileManager.default.contentsOfDirectory(atPath: directory.path).sorted() == ["destination", "source"])
    }

    @Test func testCancellationDuringCopyPreservesExistingDestination() throws {
        let directory = try temporaryDirectory()
        let source = directory.appendingPathComponent("source")
        let destination = directory.appendingPathComponent("destination")
        try Data(repeating: 42, count: 2_100_000).write(to: source)
        let previous = Data("previous result".utf8)
        try previous.write(to: destination)
        var checks = 0
        #expect(throws: (any Error).self) { try AtomicExport.save(source: source, destination: destination, isCancelled: {
            checks += 1
            return checks >= 2
        }) }
        #expect(try Data(contentsOf: destination) == previous)
        #expect(try FileManager.default.contentsOfDirectory(atPath: directory.path).sorted() == ["destination", "source"])
    }

    @Test func testFailedExportPreservesDestinationAndCleansStage() throws {
        let directory = try temporaryDirectory()
        let source = directory.appendingPathComponent("missing-source")
        let destination = directory.appendingPathComponent("destination")
        try Data("old".utf8).write(to: destination)
        #expect(throws: (any Error).self) { try AtomicExport.save(source: source, destination: destination) }
        #expect(try Data(contentsOf: destination) == Data("old".utf8))
        #expect(try FileManager.default.contentsOfDirectory(atPath: directory.path) == ["destination"])
    }

    @Test func testGracefulShutdownHasBoundedEscalation() {
        let policy = ShutdownPolicy()
        #expect(policy.signal(elapsed: 0) == nil)
        #expect(policy.signal(elapsed: 7.99) == nil)
        #expect(policy.signal(elapsed: 8) == SIGTERM)
        #expect(policy.signal(elapsed: 9.99) == SIGTERM)
        #expect(policy.signal(elapsed: 10) == SIGKILL)
        #expect(policy.signal(elapsed: 50) == SIGKILL)
    }
}
