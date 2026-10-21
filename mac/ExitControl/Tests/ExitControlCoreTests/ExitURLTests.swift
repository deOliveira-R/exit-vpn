import Foundation
import Testing
@testable import ExitControlCore

// Logic: `exitvpn://` URLs, from Shortcuts and the shell, to windows and actions.

private func parse(_ s: String) -> ExitURL? { ExitURL(URL(string: s)!) }

@Test func openURLsNameAWindowAndASettingsTab() {
    #expect(parse("exitvpn://servers") == .open(.servers))
    #expect(parse("exitvpn://settings/account") == .open(.settings(.account)))
    #expect(parse("exitvpn://settings/nonsense") == .open(.settings(.general)))
}

@Test func rotateTakesAScopeAndDefaultsToAny() {
    #expect(parse("exitvpn://rotate?scope=city") == .run(.rotate(.city)))
    #expect(parse("exitvpn://rotate") == .run(.rotate(.any)))
    #expect(parse("exitvpn://rotate?scope=moon") == nil)
}

@Test func connectTakesACountryAndCityOrAServer() {
    #expect(parse("exitvpn://connect?country=United%20States&city=New%20York")
            == .run(.connect(Region(country: "United States", city: "New York"))))
    #expect(parse("exitvpn://connect?country=Switzerland") == .run(.connect(Region(country: "Switzerland"))))
    #expect(parse("exitvpn://connect?server=CH%23821") == .run(.connectServer(name: "CH#821", ip: nil)))
    #expect(parse("exitvpn://connect") == nil)
}

@Test func secureCoreTakesOnOrOff() {
    #expect(parse("exitvpn://securecore?on=1") == .run(.secureCore(true)))
    #expect(parse("exitvpn://securecore?on=off") == .run(.secureCore(false)))
    #expect(parse("exitvpn://securecore") == nil)
}

@Test func refreshIsAnActionAndOtherURLsAreRejected() {
    #expect(parse("exitvpn://refresh") == .refresh)
    #expect(parse("https://rotate") == nil)
    #expect(parse("exitvpn://launch-missiles") == nil)
}
