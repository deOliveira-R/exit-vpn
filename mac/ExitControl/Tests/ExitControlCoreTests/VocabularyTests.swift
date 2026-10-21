import Foundation
import Testing
@testable import ExitControlCore

// Logic: the app's vocabulary (health, features, scopes, find outcomes, badges) against the wire.

@Test func healthReadsKnownStatesAndKeepsUnknownOnes() {
    #expect(Health(wire: "failing_over") == .failingOver)
    #expect(Health(wire: "new_state") == .unknown("new_state"))
}

@Test func featuresAndRotationScopesUseTheWireNames() {
    #expect(Feature.allCases.map(\.rawValue) == ["port_forward", "stream", "ipv6"])
    #expect(RotationScope.allCases.map(\.rawValue) == ["city", "country", "any"])
}

@Test func anUnknownFindOutcomeShowsAQuestionMark() {
    #expect(FindOutcome(wire: "challenge") == .challenge)
    #expect(FindOutcome(wire: "x").icon == "❔")
}

@Test func serverBadgesShowTheSecureCoreEntryStreamingAndIPv6() throws {
    let json = """
    {"name":"CH-US#3","country":"United States","city":"New York","hostname":"node-us-244.protonvpn.net",
     "ip":"1.2.3.4","pubkey":"k","secure_core":true,"tor":false,"free":false,"stream":true,"port_forward":false,
     "ipv6":true,"load":40,"enabled":true,"entry":"CH"}
    """
    let s = try ControllerClient.decoder.decode(Server.self, from: Data(json.utf8))
    #expect(s.badges == ["SC CH", "Streaming", "IPv6"])
}
