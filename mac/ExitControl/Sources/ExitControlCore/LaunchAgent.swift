import Foundation

/// The background agent: `exitctl <provider> servers --push` every hour, so the exit node
/// runs on a fresh live catalog without the app being open (PLAN Phase 5).
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
            "EnvironmentVariables": ["PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"],
            "StandardOutPath": logs,
            "StandardErrorPath": logs,
            "ProcessType": "Background",
        ]
    }

    public func install(exitctl: URL, provider: String, controllerURL: String) throws {
        try? uninstall()
        let data = try PropertyListSerialization.data(
            fromPropertyList: plist(exitctl: exitctl, provider: provider, controllerURL: controllerURL),
            format: .xml, options: 0)
        try FileManager.default.createDirectory(at: plistURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: plistURL)
        try launchctl(["bootstrap", "gui/\(getuid())", plistURL.path])
    }

    public func uninstall() throws {
        if installed {
            try? launchctl(["bootout", "gui/\(getuid())/\(Self.label)"])
            try FileManager.default.removeItem(at: plistURL)
        }
    }

    func launchctl(_ args: [String]) throws {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = args
        p.standardOutput = Pipe(); p.standardError = Pipe()
        try p.run()
        p.waitUntilExit()
        if p.terminationStatus != 0 {
            throw ExitctlError.failed(code: p.terminationStatus, message: "launchctl \(args[0]) failed (\(p.terminationStatus))")
        }
    }
}
