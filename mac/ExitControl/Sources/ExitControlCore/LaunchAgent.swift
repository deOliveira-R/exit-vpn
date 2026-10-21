import Foundation

/// The background agent: `exitctl <provider> servers --push` every hour, so the exit node
/// runs on a fresh live catalog without the app being open.
public struct LaunchAgent: Sendable {
    public static let label = "org.exit-vpn.catalog-push"
    public var plistURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appending(path: "Library/LaunchAgents/\(Self.label).plist")
    }
    public init() {}

    public var installed: Bool { FileManager.default.fileExists(atPath: plistURL.path) }

    public func plist(exitctl: URL, provider: String, controllerURL: String, interval: Int = 3600) -> [String: Any] {
        let logs = FileManager.default.homeDirectoryForCurrentUser.appending(path: "Library/Logs/exitctl-push.log").path
        return [
            "Label": Self.label,
            "ProgramArguments": [exitctl.path, provider, "servers", "--push", "--json", "--url", controllerURL],
            "StartInterval": interval,
            "RunAtLoad": true,
            "EnvironmentVariables": ["PATH": ProcessRunner.guiPATH],
            "StandardOutPath": logs,
            "StandardErrorPath": logs,
            "ProcessType": "Background",
        ]
    }

    public struct Failure: Error, LocalizedError, Sendable {
        public var message: String
        public var errorDescription: String? { message }
    }

    /// Writes the plist and loads it (it runs once at load, then hourly). Installing again
    /// replaces it (e.g. after the controller URL changed).
    public func install(exitctl: URL, provider: String, controllerURL: String) async throws {
        try? await uninstall()
        let data = try PropertyListSerialization.data(
            fromPropertyList: plist(exitctl: exitctl, provider: provider, controllerURL: controllerURL),
            format: .xml, options: 0)
        try FileManager.default.createDirectory(at: plistURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: plistURL)
        try await launchctl(["bootstrap", "gui/\(getuid())", plistURL.path])
    }

    public func uninstall() async throws {
        if installed {
            try? await launchctl(["bootout", "gui/\(getuid())/\(Self.label)"])
            try FileManager.default.removeItem(at: plistURL)
        }
    }

    func launchctl(_ args: [String]) async throws {
        let r = try await ProcessRunner(timeout: 20).run(URL(fileURLWithPath: "/bin/launchctl"), args)
        if r.code != 0 {
            throw Failure(message: "launchctl \(args[0]) failed (\(r.code))" + (r.lastErrorLine.map { ": \($0)" } ?? ""))
        }
    }
}
