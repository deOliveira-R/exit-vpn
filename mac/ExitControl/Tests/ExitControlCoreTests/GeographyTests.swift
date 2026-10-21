import Foundation
import Testing
@testable import ExitControlCore

// Logic: flags, country codes and continents for the menus and the server list.

@Test func aHostnameGivesItsCountryCode() {
    #expect(countryCode(hostname: "node-us-244.protonvpn.net") == "US")
    #expect(countryCode(hostname: "ch-03.x") == "CH")
}

@Test func aSecureCoreServersCountryIsItsExitNotItsEntry() {
    var s = server("CH-US#3", "United States", "New York", load: 40)
    s.hostname = "node-us-244.protonvpn.net"; s.secureCore = true; s.entry = "CH"
    #expect(countryCode(s) == "US")
}

@Test func flagsCoverTheUKAndFallBackToAGlobe() {
    #expect(flag("ch") == "🇨🇭")
    #expect(flag("UK") == "🇬🇧")
    #expect(flag(nil) == "🌐")
}

@Test func continentsGroupCountriesWithOtherForUnknownCodes() {
    #expect(continent("UK") == "Europe")
    #expect(continent("br") == "South America")
    #expect(continent("ZZ") == "Other")
}
