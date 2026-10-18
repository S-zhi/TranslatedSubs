import Foundation
import Darwin

/// One native owner per data directory, including launches outside LaunchServices.
/// Keep the inode on disk: unlinking a locked file permits two independent locks.
/// The descriptor is never inherited by the backend and the OS releases it on exit.
public final class DesktopInstanceLock {
    private let descriptor: Int32

    public enum Failure: Error, LocalizedError {
        case alreadyRunning, invalidFile

        public var errorDescription: String? {
            switch self {
            case .alreadyRunning: return "应用已经在运行。请从菜单栏打开现有窗口。"
            case .invalidFile: return "无法锁定应用数据目录。请检查目录权限后重新打开应用。"
            }
        }
    }

    public init(directory: URL) throws {
        let path = directory.appendingPathComponent("desktop.lock")
        let fd = open(path.path, O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw Failure.invalidFile }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(),
              info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1,
              info.st_mode & 0o777 == 0o600 else {
            close(fd)
            throw Failure.invalidFile
        }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            let busy = errno == EWOULDBLOCK
            close(fd)
            throw busy ? Failure.alreadyRunning : Failure.invalidFile
        }
        descriptor = fd
    }

    deinit { close(descriptor) }
}
