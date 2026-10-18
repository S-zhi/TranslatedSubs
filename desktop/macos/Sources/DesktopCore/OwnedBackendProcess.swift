import Foundation
import Darwin

public struct ShutdownPolicy: Sendable {
    public let grace: TimeInterval
    public let terminateGrace: TimeInterval
    public init(grace: TimeInterval = 8, terminateGrace: TimeInterval = 2) {
        self.grace = max(0, grace)
        self.terminateGrace = max(0, terminateGrace)
    }
    public func signal(elapsed: TimeInterval) -> Int32? {
        if elapsed >= grace + terminateGrace { return SIGKILL }
        if elapsed >= grace { return SIGTERM }
        return nil
    }
}

/// All methods run on the main thread. The leader remains unreaped until final
/// group cleanup, reserving its PID so a later signal cannot hit a reused PGID.
public final class OwnedBackendProcess {
    public private(set) var pid: pid_t = 0
    public private(set) var exited = false
    private var exitSource: DispatchSourceProcess?

    public init() {}

    public func start(executable: URL, arguments: [String], environment: [String: String]) throws {
        precondition(Thread.isMainThread)
        guard pid == 0 else { throw DesktopError.invalidConfiguration }
        var attributes: posix_spawnattr_t?
        var actions: posix_spawn_file_actions_t?
        guard posix_spawnattr_init(&attributes) == 0 else { throw DesktopError.invalidConfiguration }
        defer { posix_spawnattr_destroy(&attributes) }
        guard posix_spawn_file_actions_init(&actions) == 0 else { throw DesktopError.invalidConfiguration }
        defer { posix_spawn_file_actions_destroy(&actions) }
        // Child logging cannot accidentally print its inherited authentication token.
        for descriptor: Int32 in [STDIN_FILENO, STDOUT_FILENO, STDERR_FILENO] {
            guard posix_spawn_file_actions_addopen(&actions, descriptor, "/dev/null", O_RDWR, 0) == 0 else {
                throw DesktopError.invalidConfiguration
            }
        }
        guard posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT)) == 0,
              posix_spawnattr_setpgroup(&attributes, 0) == 0 else { throw DesktopError.invalidConfiguration }
        let argv = ([executable.path] + arguments).map { strdup($0) } + [nil]
        let envp = environment.sorted { $0.key < $1.key }.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { argv.forEach { free($0) }; envp.forEach { free($0) } }
        var child: pid_t = 0
        let result = argv.withUnsafeBufferPointer { a in
            envp.withUnsafeBufferPointer { e in
                posix_spawn(&child, executable.path, &actions, &attributes, a.baseAddress!, e.baseAddress!)
            }
        }
        guard result == 0 else { throw DesktopError.missingExecutable }
        pid = child
        let source = DispatchSource.makeProcessSource(identifier: child, eventMask: .exit, queue: .main)
        source.setEventHandler { [weak self] in self?.exited = true }
        source.resume()
        exitSource = source
    }

    public func owns(descriptorPID: pid_t) -> Bool {
        pid > 1 && descriptorPID > 1 && getpgid(descriptorPID) == pid
    }

    public func signalGroup(_ signal: Int32) {
        precondition(Thread.isMainThread)
        guard pid > 1 else { return }
        // Only a group created by this object's successful posix_spawn is addressed.
        _ = kill(-pid, signal)
    }

    public func finish() {
        precondition(Thread.isMainThread)
        guard pid > 1 else { return }
        signalGroup(SIGKILL)
        exitSource?.cancel()
        exitSource = nil
        let child = pid
        pid = 0
        // Reaping never blocks the UI; there are no further signals after this point.
        DispatchQueue.global(qos: .utility).async {
            var status: Int32 = 0
            while waitpid(child, &status, 0) == -1 && errno == EINTR {}
        }
    }
}
