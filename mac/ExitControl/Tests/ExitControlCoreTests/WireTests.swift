import Foundation
import Testing
@testable import ExitControlCore

// Logic: the wire as the app writes and reads it (request bodies, the event stream, derived fields).

@Test func aRegionWithoutACitySendsItAsNull() throws {
    let e = JSONEncoder(); e.outputFormatting = .sortedKeys
    #expect(String(decoding: try e.encode(Region(country: "Germany")), as: UTF8.self) == #"{"city":null,"country":"Germany"}"#)
}

@Test func aPrefsPatchSendsOnlyTheFieldsItSets() throws {
    let data = try ControllerClient.encoder.encode(ControllerClient.PrefsPatch(secureCore: true))
    #expect(String(decoding: data, as: UTF8.self) == #"{"secure_core":true}"#)
}

@Test func theEventStreamParserJoinsDataLinesAndSkipsComments() {
    var p = SSEParser()
    let lines = ["event: status", "data: {\"a\":1}", "", ": keepalive", "", "data: x", "data: y", ""]
    let msgs = lines.compactMap { p.feed($0) }
    #expect(msgs == [SSEMessage(event: "status", data: "{\"a\":1}"), SSEMessage(event: "message", data: "x\ny")])
}

@Test func statusGivesTheEndpointIPWithoutThePort() throws {
    let st = try sample("controller/status", as: Status.self)
    #expect(st.endpointIP == "149.40.49.30")
}

@Test func statusSaysWhetherIPv6GoesThroughTheExitAndNothingForOlderNodes() throws {
    var st = try sample("controller/status", as: Status.self)
    #expect(StatusText.ipv6(st) == "IPv6 off on this server")
    st.ipv6 = true; st.exitIp6 = "2001:db8::1"
    #expect(StatusText.ipv6(st) == "IPv6 2001:db8::1")
    st.ipv6 = nil
    #expect(StatusText.ipv6(st) == nil)
}
