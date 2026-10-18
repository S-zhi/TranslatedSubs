import Foundation
import DesktopCore

@MainActor
final class DesktopRuntime {
    let process = OwnedBackendProcess()
    private(set) var client: BackendClient?
    private(set) var token = ""
    private var directory: URL?
    private var sessionFile: URL?
    private var timer: Timer?
    private var healthTask: Task<Void, Never>?
    private var startedAt = Date()
    private var checking = false
    private var ready = false
    private var stopping = false
    private var failed = false
    private var started = false
    private var instanceLock: DesktopInstanceLock?
    var onReady: ((BackendClient, String) -> Void)?
    var onError: ((Error) -> Void)?

    func start() throws {
        guard !stopping, !failed else { throw DesktopError.invalidConfiguration }
        guard !started else { return }
        started = true
        do { try launchBackend() }
        catch { fail(error); throw error }
    }

    private func launchBackend() throws {
        guard let resources = Bundle.main.resourceURL else { throw DesktopError.invalidConfiguration }
        let paths = try BackendPaths(resources: resources, arguments: Array(CommandLine.arguments.dropFirst()))
        try paths.validate()
        token = try SessionToken.generate()
        let support = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                  appropriateFor: nil, create: true)
        let home = support.appendingPathComponent("TranslateSub", isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        instanceLock = try DesktopInstanceLock(directory: home)
        let sessionDirectory = home.appendingPathComponent("session-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: sessionDirectory, withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o700])
        directory = sessionDirectory
        let descriptor = sessionDirectory.appendingPathComponent("session.json")
        guard FileManager.default.createFile(atPath: descriptor.path, contents: Data(),
                                             attributes: [.posixPermissions: 0o600]) else {
            cleanup(); throw DesktopError.invalidConfiguration
        }
        sessionFile = descriptor
        var environment = ProcessInfo.processInfo.environment
        // Ignore host Python injection even when launched from a developer terminal.
        for key in ["PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"] { environment.removeValue(forKey: key) }
        environment["SUBTRANS_DESKTOP"] = "1"
        environment["SUBTRANS_DESKTOP_HOME"] = home.path
        environment["SUBTRANS_DESKTOP_SESSION_TOKEN"] = token
        environment["SUBTRANS_DESKTOP_VERSION"] = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.1.0"
        environment["SUBTRANS_DESKTOP_FFMPEG"] = paths.ffmpeg.path
        environment["SUBTRANS_DESKTOP_FFPROBE"] = paths.ffprobe.path
        // App metadata is set by packaging; optional explicit environment overrides
        // support development without writing user configuration.
        for key in ["SUBTRANS_DESKTOP_UPDATE_URL", "SUBTRANS_DESKTOP_MODEL_ENDPOINT"] {
            if let value = Bundle.main.object(forInfoDictionaryKey: key) as? String, !value.isEmpty {
                environment[key] = value
            }
        }
        environment["PATH"] = paths.ffmpeg.deletingLastPathComponent().path + ":/usr/bin:/bin:/usr/sbin:/sbin"
        do {
            try process.start(executable: paths.executable, arguments: ["--session-file", descriptor.path], environment: environment)
        } catch { cleanup(); throw error }
        startedAt = Date()
        let timer = Timer(timeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.poll() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func poll() {
        guard !stopping, !failed else { return }
        if process.exited { fail(DesktopError.backendExited); return }
        guard !ready else { return }
        if Date().timeIntervalSince(startedAt) > 60 { fail(DesktopError.startupTimeout); return }
        guard !checking, let sessionFile,
              let descriptor = try? SessionDescriptor.read(sessionFile), process.owns(descriptorPID: descriptor.pid),
              let origin = try? LoopbackOrigin(port: descriptor.port) else { return }
        checking = true
        let candidate = BackendClient(origin: origin, token: token)
        healthTask = Task { [weak self] in
            guard let self else { candidate.invalidate(); return }
            defer { self.checking = false }
            do {
                let health = try await candidate.json("api/health")
                guard health["ok"] as? Bool == true else { throw DesktopError.invalidResponse }
                guard !Task.isCancelled, !self.stopping, !self.failed, !self.process.exited else {
                    candidate.invalidate(); return
                }
                self.client = candidate
                self.ready = true
                self.onReady?(candidate, self.token)
            } catch { candidate.invalidate() } // Descriptor may precede full readiness; retry until deadline.
        }
    }

    private func fail(_ error: Error) {
        // A cancelled health request may already have completed its network work.
        // Mark stopped before cancellation so it cannot publish a stale ready client.
        failed = true
        timer?.invalidate(); timer = nil
        healthTask?.cancel()
        client?.invalidate()
        process.finish()
        cleanup()
        onError?(error)
    }

    func shutdown(completion: @escaping () -> Void) {
        guard !stopping else { return }
        stopping = true
        timer?.invalidate(); timer = nil
        healthTask?.cancel()
        let client = self.client
        Task {
            if let client { _ = try? await client.json("api/desktop/shutdown", method: "POST") }
        }
        // Absolute deadline starts with the request, not after an unresponsive API.
        let began = ProcessInfo.processInfo.systemUptime
        let policy = ShutdownPolicy()
        var termSent = false
        let timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] timer in
            Task { @MainActor in
                guard let self else { timer.invalidate(); completion(); return }
                let elapsed = ProcessInfo.processInfo.systemUptime - began
                if self.process.pid == 0 || policy.signal(elapsed: elapsed) == SIGKILL {
                    timer.invalidate()
                    self.process.finish()
                    client?.invalidate()
                    self.cleanup()
                    completion()
                } else if !termSent && policy.signal(elapsed: elapsed) == SIGTERM {
                    termSent = true
                    self.process.signalGroup(SIGTERM)
                }
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func cleanup() {
        if let directory { try? FileManager.default.removeItem(at: directory) }
        directory = nil
        sessionFile = nil
        token = ""
        client = nil
        ready = false
        instanceLock = nil
    }
}
