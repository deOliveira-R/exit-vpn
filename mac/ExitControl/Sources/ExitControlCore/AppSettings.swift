import Foundation
import Observation

/// The app's own settings, in UserDefaults (keys unchanged since v1: `controllerURL`,
/// `exitctlPath`, `showFlag`, `notify`). `onControllerURLChange` lets the app reconnect and
/// re-point the background agent.
@MainActor @Observable
public final class AppSettings {
    public static let defaultControllerURL = "http://exit-proton:8080"

    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored public var onControllerURLChange: @MainActor (String) -> Void = { _ in }

    public var controllerURL: String {
        didSet { defaults.set(controllerURL, forKey: "controllerURL"); onControllerURLChange(controllerURL) }
    }
    public var exitctlPath: String { didSet { defaults.set(exitctlPath, forKey: "exitctlPath") } }
    public var showFlag: Bool { didSet { defaults.set(showFlag, forKey: "showFlag") } }
    public var notify: Bool { didSet { defaults.set(notify, forKey: "notify") } }

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        controllerURL = defaults.string(forKey: "controllerURL") ?? Self.defaultControllerURL
        exitctlPath = defaults.string(forKey: "exitctlPath") ?? ""
        showFlag = defaults.object(forKey: "showFlag") as? Bool ?? true
        notify = defaults.object(forKey: "notify") as? Bool ?? true
    }

    /// The exit node's Tailscale name: the controller URL's first host label.
    public var nodeName: String {
        URL(string: controllerURL)?.host()?.split(separator: ".").first.map(String.init) ?? "exit-proton"
    }

    public var controller: ControllerClient? { URL(string: controllerURL).map { ControllerClient(baseURL: $0) } }

    public var exitctl: Exitctl? {
        Exitctl.locate(override: exitctlPath).map { Exitctl(executable: $0, controllerURL: controllerURL) }
    }
}
