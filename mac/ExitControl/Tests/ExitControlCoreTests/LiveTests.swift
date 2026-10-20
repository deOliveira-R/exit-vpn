import Foundation
import Testing
@testable import ExitControlCore

/// Against the real controller and exitctl, read-only:
/// `EXITCONTROL_LIVE=http://exit-proton:8080 swift test --filter Live`
let liveURL = ProcessInfo.processInfo.environment["EXITCONTROL_LIVE"]

@Suite(.enabled(if: liveURL != nil)) struct Live {
    var client: ControllerClient { ControllerClient(baseURL: URL(string: liveURL!)!) }

    @Test func statusServersKeys() async throws {
        let st = try await client.status()
        #expect(st.server != nil && !st.prefs.regions.isEmpty)
        let sv = try await client.servers()
        #expect(sv.servers.count > 500 && (sv.countries ?? []).count > 50)
        #expect(sv.servers.contains { $0.hasIPv6 } && sv.servers.contains { $0.secureCore && $0.entry?.isEmpty == false })
        let keys = try await client.keys()
        #expect(keys.map(\.slot) == ["a", "b"])
    }

    @Test func eventStreamStartsWithStatus() async throws {
        var got: [String] = []
        for await u in client.updates() {
            switch u {
            case .connected: got.append("connected")
            case .status: got.append("status")
            case .event, .disconnected: break
            }
            if got.count == 2 { break }
        }
        #expect(got == ["connected", "status"])
    }

    @Test func unreachableControllerReportsOffline() async throws {
        let dead = ControllerClient(baseURL: URL(string: "http://127.0.0.1:9")!)
        let t0 = Date()
        for await u in dead.updates() {
            if case .disconnected = u { break }
        }
        #expect(Date().timeIntervalSince(t0) < 5)
        await #expect(throws: APIError.self) { try await dead.status() }
    }

    @Test func badRequestsSurfaceTheDetail() async throws {
        await #expect {
            try await client.select(server: "NO-SUCH#1", ip: nil)
        } throws: { e in
            (e as? APIError).map { $0.status == 400 && $0.detail.contains("no server") } ?? false
        }
    }

    @Test func exitctlReads() async throws {
        let exe = try #require(Exitctl.locate())
        let x = Exitctl(executable: exe, controllerURL: liveURL!)
        let opts = try await x.options("proton")
        #expect(opts.contains { $0.key == "netshield" && $0.confLabel == "NetShield" })
        let who = try await x.whoami("proton")
        if who.isLoggedIn {
            let certs = try await x.certificates("proton")
            #expect(certs.contains { $0.slot == "a" })
        }
    }
}
