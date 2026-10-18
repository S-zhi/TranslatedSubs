import Foundation
import DesktopCore

/// No cookies, proxy or redirects on native requests: credentials stay on the
/// exact authenticated loopback origin, including artifact downloads.
final class BackendClient: NSObject, URLSessionTaskDelegate {
    let origin: LoopbackOrigin
    private let token: String
    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.connectionProxyDictionary = [:]
        configuration.timeoutIntervalForRequest = 4
        configuration.timeoutIntervalForResource = 30
        return URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }()

    init(origin: LoopbackOrigin, token: String) { self.origin = origin; self.token = token }

    func request(_ path: String, method: String = "GET") -> URLRequest {
        var request = URLRequest(url: origin.endpoint(path))
        request.httpMethod = method
        request.setValue(token, forHTTPHeaderField: "X-API-Token")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        return request
    }

    func json(_ path: String, method: String = "GET") async throws -> [String: Any] {
        let (data, response) = try await session.data(for: request(path, method: method))
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              let url = http.url, origin.allows(url), data.count <= 1024 * 1024,
              let body = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw DesktopError.invalidResponse
        }
        return body
    }

    func download(taskID: String, kind: ExportKind) async throws -> URL {
        let url = try BridgeCommand.artifactURL(origin: origin, taskID: taskID, kind: kind)
        var request = request(url.path)
        request.timeoutInterval = 60
        // A separate download session permits large local exports without holding
        // media in RAM; the task remains cancellable through Swift concurrency.
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.connectionProxyDictionary = [:]
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 3600
        let downloadSession = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        defer { downloadSession.invalidateAndCancel() }
        let (temporary, response) = try await downloadSession.download(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              let finalURL = http.url, finalURL == url else {
            try? FileManager.default.removeItem(at: temporary)
            throw DesktopError.invalidResponse
        }
        let retained = FileManager.default.temporaryDirectory.appendingPathComponent("translatesub-export-\(UUID().uuidString)")
        try FileManager.default.moveItem(at: temporary, to: retained)
        do {
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: retained.path)
        } catch {
            try? FileManager.default.removeItem(at: retained)
            throw error
        }
        return retained
    }

    func invalidate() { session.invalidateAndCancel() }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
