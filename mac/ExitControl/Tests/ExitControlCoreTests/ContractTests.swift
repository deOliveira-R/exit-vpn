import Foundation
import Testing
@testable import ExitControlCore

// The app's side of the contracts (PLAN R1): every sample in contracts/ that the app reads must
// decode into the types it uses. The producers' own tests (pytest) check that what the
// controller and exitctl emit matches the same samples.

private let contracts = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent().appending(path: "contracts")

private func sample<T: Decodable>(_ name: String, as type: T.Type = T.self) throws -> T {
    let data = try Data(contentsOf: contracts.appending(path: "\(name).json"))
    return try ControllerClient.decoder.decode(T.self, from: data)
}

@Test func controllerSamplesDecode() throws {
    let st = try sample("controller/status", as: Status.self)
    #expect(st.server != nil && st.health_ != .unknown)
    let servers = try sample("controller/servers", as: ServersResponse.self)
    #expect(!servers.servers.isEmpty)
    let prefs = try sample("controller/prefs", as: Prefs.self)
    #expect(!prefs.regions.isEmpty && prefs.favorites != nil)
    #expect(try sample("controller/keys", as: KeysResponse.self).slots.first?.deviceName != nil)
    _ = try sample("controller/key_slot", as: KeySlot.self)
    let job = try sample("controller/find_job", as: FindJob.self)
    #expect(job.results.first?.outcome == "ok")
}

@Test func everyEventKindDecodesAndHasASummary() throws {
    let events = try sample("controller/events", as: [String: Event].self)
    #expect(events.count >= 18)
    for (kind, ev) in events {
        #expect(ev.kind == kind)
        // a generic summary is the capitalised kind: every kind should have its own line
        let generic = kind.replacingOccurrences(of: "_", with: " ").capitalized
        #expect(ev.summary != generic, "no summary for event kind \(kind)")
    }
}

@Test func exitctlSamplesDecode() throws {
    #expect(try !sample("exitctl/providers", as: [ProviderInfo].self).isEmpty)
    let options = try sample("exitctl/options", as: [ProviderOption].self)
    #expect(!options.isEmpty)
    #expect(try sample("exitctl/login", as: Account.self).isLoggedIn)
    #expect(try sample("exitctl/whoami", as: Account.self).isLoggedIn)
    #expect(try !sample("exitctl/logged_out", as: Account.self).isLoggedIn)
    #expect(try sample("exitctl/list", as: [Certificate].self).first?.slot != nil)
    _ = try sample("exitctl/keys", as: KeysResponse.self)
    for name in ["rotate", "renew"] {
        let out = try sample("exitctl/\(name)", as: [String: JSONValue].self)
        #expect(out["key"] != nil, "exitctl/\(name)")
    }
}

@Test func exitctlErrorsCarryWhatTheAppReads() throws {
    let err = try sample("exitctl/error", as: [String: JSONValue].self)
    #expect(err["error"]?.string?.isEmpty == false)
    let totp = try sample("exitctl/need_totp", as: [String: JSONValue].self)
    #expect(totp["need"]?.string == "totp")
    let hv = try sample("exitctl/need_human_verification", as: [String: JSONValue].self)
    #expect(hv["need"]?.string == "human_verification" && hv["url"]?.string != nil && hv["token"]?.string != nil)
}
