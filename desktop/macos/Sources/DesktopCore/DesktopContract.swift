import Foundation
import Darwin
import Security

public enum DesktopError: Error, LocalizedError {
    case invalidConfiguration, invalidDescriptor, invalidMessage, invalidResponse
    case missingExecutable, startupTimeout, backendExited, fileOperation

    public var errorDescription: String? {
        switch self {
        case .invalidConfiguration: return "桌面运行配置无效。请重新安装应用或检查显式开发参数。"
        case .invalidDescriptor: return "后端会话描述无效。"
        case .invalidMessage: return "已拒绝无效的桌面操作。"
        case .invalidResponse: return "本地服务返回异常，请稍后重试。"
        case .missingExecutable: return "应用缺少后端或 FFmpeg 组件，请重新安装完整应用。"
        case .startupTimeout: return "本地服务启动超时，请退出并重新打开应用。"
        case .backendExited: return "本地服务已停止。请退出并重新打开应用；任务记录会保留。"
        case .fileOperation: return "无法保存文件。请检查目标位置的权限和可用空间。"
        }
    }
}

public struct BackendPaths {
    public let executable: URL
    public let ffmpeg: URL
    public let ffprobe: URL

    /// Development is opt-in, and still uses an executable, never a Python lookup.
    public init(resources: URL, arguments: [String]) throws {
        var overrides: [String: URL] = [:]
        var index = 0
        while index < arguments.count {
            let key = arguments[index]
            guard ["--backend-executable", "--ffmpeg", "--ffprobe"].contains(key),
                  index + 1 < arguments.count, overrides[key] == nil,
                  arguments[index + 1].hasPrefix("/") else {
                throw DesktopError.invalidConfiguration
            }
            overrides[key] = URL(fileURLWithPath: arguments[index + 1])
            index += 2
        }
        if overrides["--backend-executable"] == nil && !overrides.isEmpty {
            throw DesktopError.invalidConfiguration
        }
        executable = overrides["--backend-executable"] ?? resources.appendingPathComponent("backend/translatedsubs-backend")
        ffmpeg = overrides["--ffmpeg"] ?? resources.appendingPathComponent("bin/ffmpeg")
        ffprobe = overrides["--ffprobe"] ?? resources.appendingPathComponent("bin/ffprobe")
    }

    public func validate() throws {
        for url in [executable, ffmpeg, ffprobe] {
            let values = try url.resourceValues(forKeys: [.isRegularFileKey])
            guard values.isRegularFile == true,
                  FileManager.default.isExecutableFile(atPath: url.path) else {
                throw DesktopError.missingExecutable
            }
        }
    }
}

public struct SessionDescriptor: Decodable, Equatable {
    public let port: Int
    public let pid: Int32
    public let instanceID: String

    enum CodingKeys: String, CodingKey { case port, pid; case instanceID = "instance_id" }

    public static func decode(_ data: Data) throws -> SessionDescriptor {
        guard !data.isEmpty, data.count <= 16_384 else { throw DesktopError.invalidDescriptor }
        let descriptor = try JSONDecoder().decode(Self.self, from: data)
        guard (1...65535).contains(descriptor.port), descriptor.pid > 1,
              !descriptor.instanceID.isEmpty, descriptor.instanceID.utf8.count <= 256 else {
            throw DesktopError.invalidDescriptor
        }
        return descriptor
    }

    public static func read(_ url: URL) throws -> SessionDescriptor {
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        guard fd >= 0 else { throw DesktopError.invalidDescriptor }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(),
              info.st_mode & S_IFMT == S_IFREG, info.st_mode & 0o777 == 0o600,
              info.st_size > 0, info.st_size <= 16_384 else { throw DesktopError.invalidDescriptor }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
        return try decode(handle.read(upToCount: 16_385) ?? Data())
    }
}

public struct LoopbackOrigin: Equatable {
    public let port: Int
    public var url: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    public init(port: Int) throws {
        guard (1...65535).contains(port) else { throw DesktopError.invalidDescriptor }
        self.port = port
    }

    public func allows(_ candidate: URL) -> Bool {
        candidate.scheme == "http" && candidate.host == "127.0.0.1" &&
            (candidate.port ?? 80) == port && candidate.user == nil && candidate.password == nil
    }

    public func endpoint(_ path: String) -> URL { url.appendingPathComponent(path) }

    public func cookie(token: String) throws -> HTTPCookie {
        guard !token.isEmpty, let cookie = HTTPCookie(properties: [
            .name: "subtrans_desktop_session", .value: token, .domain: "127.0.0.1",
            .path: "/", HTTPCookiePropertyKey("HttpOnly"): "TRUE",
            .sameSitePolicy: HTTPCookieStringPolicy.sameSiteStrict, .discard: "TRUE"
        ]) else { throw DesktopError.invalidConfiguration }
        // Secure is deliberately absent: the authenticated service uses HTTP loopback.
        return cookie
    }
}

public enum ExportKind: String { case video, subtitle
    public var fileExtension: String { self == .video ? "mp4" : "srt" }
}

public enum BridgeCommand: Equatable {
    case export(taskID: String, kind: ExportKind)
    case openExternal(URL)

    public static func parse(_ body: Any) throws -> BridgeCommand {
        guard let object = body as? [String: Any], object.count <= 3,
              JSONSerialization.isValidJSONObject(object),
              let data = try? JSONSerialization.data(withJSONObject: object), data.count <= 4096,
              let action = object["action"] as? String else { throw DesktopError.invalidMessage }
        switch action {
        case "export":
            guard Set(object.keys) == ["action", "taskId", "kind"],
                  let taskID = object["taskId"] as? String, validTaskID(taskID),
                  let rawKind = object["kind"] as? String, let kind = ExportKind(rawValue: rawKind) else {
                throw DesktopError.invalidMessage
            }
            return .export(taskID: taskID, kind: kind)
        case "openExternal":
            guard Set(object.keys) == ["action", "url"], let value = object["url"] as? String,
                  let url = URL(string: value), isExternalURL(url) else { throw DesktopError.invalidMessage }
            return .openExternal(url)
        default: throw DesktopError.invalidMessage
        }
    }

    public static func validTaskID(_ id: String) -> Bool {
        !id.isEmpty && id.utf8.count <= 128 && id.utf8.allSatisfy {
            (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95
        }
    }

    public static func isExternalURL(_ url: URL) -> Bool {
        url.scheme == "https" && !(url.host ?? "").isEmpty && url.user == nil && url.password == nil &&
            url.absoluteString.utf8.count <= 2048
    }

    public static func artifactURL(origin: LoopbackOrigin, taskID: String, kind: ExportKind) throws -> URL {
        guard validTaskID(taskID) else { throw DesktopError.invalidMessage }
        return origin.endpoint("api/tasks/\(taskID)/\(kind == .video ? "download" : "subtitle")")
    }
}

public enum SessionToken {
    public static func generate() throws -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw DesktopError.invalidConfiguration
        }
        return bytes.map { String(format: "%02x", $0) }.joined()
    }
}
