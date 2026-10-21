import Foundation

/// Runs a command line tool (exitctl, tailscale, launchctl) without blocking the caller:
/// stdout and stderr are read concurrently (a full stderr pipe cannot stall it), a timeout
/// ends a hung process, and cancelling the calling task terminates it.
public struct ProcessRunner: Sendable {
    public struct Result: Sendable {
        public var code: Int32
        public var stdout: Data
        public var stderr: String
        /// The last stderr line, for an error message.
        public var lastErrorLine: String? { stderr.split(separator: "\n").last.map(String.init) }
    }

    public struct Failure: Error, LocalizedError, Sendable {
        public var message: String
        public var errorDescription: String? { message }
    }

    /// GUI apps get a minimal PATH; tools installed by Homebrew (gpg for proton-core) need more.
    public static let guiPATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

    public var timeout: TimeInterval
    public init(timeout: TimeInterval = 300) { self.timeout = timeout }

    public func run(_ executable: URL, _ args: [String], stdin: Data? = nil,
                    environment: [String: String]? = nil) async throws -> Result {
        let p = Process()
        p.executableURL = executable
        p.arguments = args
        var env = environment ?? ProcessInfo.processInfo.environment
        env["PATH"] = Self.guiPATH + ":" + (env["PATH"] ?? "")
        p.environment = env
        let out = Pipe(), err = Pipe(), inp = Pipe()
        p.standardOutput = out; p.standardError = err; p.standardInput = inp
        let exited = AsyncStream<Int32> { cont in
            p.terminationHandler = { cont.yield($0.terminationStatus); cont.finish() }
        }
        do { try p.run() } catch {
            throw Failure(message: "\(executable.lastPathComponent): \(error.localizedDescription)")
        }
        return try await withTaskCancellationHandler {
            async let stdoutData = Self.readAll(out.fileHandleForReading)
            async let stderrData = Self.readAll(err.fileHandleForReading)
            if let stdin { inp.fileHandleForWriting.write(stdin) }
            try? inp.fileHandleForWriting.close()
            let timedOut = Flag()
            let watchdog = Task { [timeout] in
                try? await Task.sleep(for: .seconds(timeout))
                if !Task.isCancelled, p.isRunning { timedOut.set(); p.terminate() }
            }
            defer { watchdog.cancel() }
            var code: Int32 = -1
            for await c in exited { code = c }
            let (o, e) = await (stdoutData, stderrData)
            try Task.checkCancellation()
            if timedOut.isSet {
                throw Failure(message: "\(executable.lastPathComponent) took longer than \(Int(timeout)) s")
            }
            return Result(code: code, stdout: o, stderr: String(decoding: e, as: UTF8.self))
        } onCancel: {
            if p.isRunning { p.terminate() }
        }
    }

    private static func readAll(_ handle: FileHandle) async -> Data {
        await withCheckedContinuation { cont in
            DispatchQueue.global().async { cont.resume(returning: handle.readDataToEndOfFile()) }
        }
    }
}

/// A thread-safe one-way switch (the watchdog sets it, the runner reads it).
private final class Flag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func set() { lock.withLock { value = true } }
    var isSet: Bool { lock.withLock { value } }
}
