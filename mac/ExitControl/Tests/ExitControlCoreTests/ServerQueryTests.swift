import Foundation
import Testing
@testable import ExitControlCore

// Logic: filtering and ordering the server list (nearest first, then load).

private let rows = [server("CH#2", "Switzerland", "Zurich", load: 10), server("US#3", "United States", "Miami", load: 5),
                    server("CH#1", "Switzerland", "Geneva", load: 90, p2p: true), server("CH#3", "Switzerland", "Zurich", load: 10)]

@Test func serversSortByCountryThenCityThenLoadThenName() {
    #expect(rows.sorted(by: ServerQuery.nearestFirst).map(\.name) == ["CH#1", "CH#2", "CH#3", "US#3"])
}

@Test func theP2PFilterKeepsOnlyPortForwardingServers() {
    var q = ServerQuery()
    q.p2p = true
    #expect(rows.filter(q.matches).map(\.name) == ["CH#1"])
}

@Test func searchMatchesPartOfACity() {
    var q = ServerQuery()
    q.search = "zur"
    #expect(rows.filter(q.matches).map(\.name) == ["CH#2", "CH#3"])
}
