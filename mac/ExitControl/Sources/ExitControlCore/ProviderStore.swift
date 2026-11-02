import Foundation
import Observation

/// The VPN provider account as exitctl sees it, and the exit node's key slots.
@MainActor @Observable
public final class ProviderStore {
    public private(set) var account: Account?
    public private(set) var options: [ProviderOption] = []
    public private(set) var keys: [KeySlot] = []
    public private(set) var certificates: [Certificate] = []
    public private(set) var error: String?
    public var provider = "proton"

    private var cli: (any ProviderCLI)?
    private var api: (any ControllerAPI)?

    public init() {}

    public var isLoggedIn: Bool { account?.isLoggedIn == true }

    public func connect(cli: (any ProviderCLI)?, api: (any ControllerAPI)?) {
        self.cli = cli
        self.api = api
    }

    public func load() async {
        guard let cli else { error = ExitctlError.notInstalled.localizedDescription; return }
        var problems: [String] = []
        do { account = try await cli.whoami(provider) } catch { problems.append("account: \(error.localizedDescription)") }
        if options.isEmpty {
            do { options = try await cli.options(provider) } catch { problems.append("options: \(error.localizedDescription)") }
        }
        if let api {
            do { keys = try await api.keys() } catch { problems.append("keys: \(error.localizedDescription)") }
        }
        if isLoggedIn {
            do { certificates = try await cli.certificates(provider) } catch {
                problems.append("certificates: \(error.localizedDescription)")
            }
        }
        error = problems.isEmpty ? nil : problems.joined(separator: "; ")
    }

    /// Runs a provider command (through the action runner by the caller), then reloads.
    public func perform(_ command: ProviderCommand) async throws {
        guard let cli else { throw ExitctlError.notInstalled }
        defer { Task { await self.load() } }
        try await cli.perform(provider, command)
    }

    public func login(_ step: LoginStep) async throws -> Account {
        guard let cli else { throw ExitctlError.notInstalled }
        return try await cli.login(provider, step)
    }

    /// A slot's current lever values in flag form ("on"/"off"/"2"): what exitctl recorded
    /// if it made the key, else what the key's .conf comments say, else the defaults
    /// (as exitctl's `resolve_levers`).
    public func levers(slot: String) -> [String: String] {
        guard let k = keys.first(where: { $0.slot == slot }) else { return [:] }
        var out: [String: String] = [:]
        for opt in options {
            if let v = opt.default.string { out[opt.key] = flagForm(v, opt) }
            if let label = opt.confLabel, let v = k.options[label] { out[opt.key] = flagForm(v, opt) }
        }
        if k.provider == provider, case .object(let recorded)? = k.meta["options"] {
            for (key, v) in recorded { if let s = v.string { out[key] = s } }
        }
        return out
    }

    /// The exit's key slots, by interface: wg0 runs slot a's key, wg1 slot c's (keys stay
    /// with interfaces; the active and standby roles move between them at a handover).
    public static let slotOfInterface = ["wg0": "a", "wg1": "c"]

    /// The slot whose key carries the exit's traffic now: the one the standby does not use.
    public static func activeSlot(standby: Standby?) -> String {
        standby?.interface.flatMap { slotOfInterface[$0] } == "a" ? "c" : "a"
    }

    /// The exit slots that hold a key, standby first: lever changes go to both tunnels (else
    /// a handover would drop them), the standby's first since its new key never touches
    /// forwarded traffic.
    public func exitSlots(standby: Standby?) -> [String] {
        let active = Self.activeSlot(standby: standby)
        let other = active == "a" ? "c" : "a"
        return [other, active].filter { s in keys.contains { $0.slot == s && $0.publicKey != nil } }
    }

    /// The exit's lever values with one changed, in flag form; nil when the levers are not
    /// known yet (exitctl or the node not loaded).
    public func levers(changing key: String, to value: String, standby: Standby?) -> [String: String]? {
        var values = levers(slot: Self.activeSlot(standby: standby))
        guard !values.isEmpty else { return nil }
        values[key] = value
        return values
    }

    func setForTests(options: [ProviderOption], keys: [KeySlot]) {
        self.options = options; self.keys = keys
    }

    private func flagForm(_ v: String, _ opt: ProviderOption) -> String {
        guard opt.isToggle else { return v }
        return ["on", "true", "yes", "1"].contains(v.lowercased()) ? "on" : "off"
    }
}
