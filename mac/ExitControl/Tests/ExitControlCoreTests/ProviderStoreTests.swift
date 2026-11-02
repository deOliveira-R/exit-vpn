import Foundation
import Testing
@testable import ExitControlCore

// Component: a key slot's levers as the provider store resolves them from exitctl and the node.

@MainActor @Test func whatExitctlRecordedWinsOverTheConfAndTheConfOverDefaults() throws {
    let p = try providerStore(meta: #"{"provider":"proton","options":{"netshield":2}}"#)
    #expect(p.levers(slot: "a") == ["netshield": "2", "accelerator": "off", "moderate_nat": "off"])
}

@MainActor @Test func withoutRecordedOptionsTheConfCommentsApply() throws {
    let p = try providerStore(meta: "{}")
    #expect(p.levers(slot: "a") == ["netshield": "1", "accelerator": "off", "moderate_nat": "off"])
}

@MainActor @Test func aSlotTheNodeDoesNotReportHasNoLevers() throws {
    let p = try providerStore(meta: "{}")
    #expect(p.levers(slot: "b") == [:])
}

// Component: lever changes reach both exit tunnels, so a handover keeps them.

func standby(on interface: String) throws -> Standby {
    try ControllerClient.decoder.decode(Standby.self, from: Data(#"{"state":"ready","interface":"\#(interface)"}"#.utf8))
}

@MainActor @Test func theActiveSlotIsTheOneTheStandbyDoesNotUse() throws {
    #expect(ProviderStore.activeSlot(standby: nil) == "a")
    #expect(ProviderStore.activeSlot(standby: try standby(on: "wg1")) == "a")
    #expect(ProviderStore.activeSlot(standby: try standby(on: "wg0")) == "c")
}

@MainActor @Test func leverChangesGoToTheStandbyFirstThenTheActiveAndOnlyToKeyedSlots() throws {
    let p = try providerStore(meta: "{}")
    #expect(p.exitSlots(standby: try standby(on: "wg1")) == ["a"])   // slot c has no key
    let c = try ControllerClient.decoder.decode(KeySlot.self, from: Data(#"""
    {"slot":"c","role":"standby","source":"uploaded","public_key":"k2","options":{},"meta":{},"expires_in_days":null}
    """#.utf8))
    p.setForTests(options: p.options, keys: p.keys + [c])
    #expect(p.exitSlots(standby: try standby(on: "wg1")) == ["c", "a"])
    #expect(p.exitSlots(standby: try standby(on: "wg0")) == ["a", "c"])
}

@MainActor @Test func aLeverChangeKeepsTheActiveSlotsOtherLevers() throws {
    let p = try providerStore(meta: "{}")
    #expect(p.levers(changing: "netshield", to: "2", standby: nil)
            == ["netshield": "2", "accelerator": "off", "moderate_nat": "off"])
    #expect(ProviderStore().levers(changing: "netshield", to: "2", standby: nil) == nil)
}
