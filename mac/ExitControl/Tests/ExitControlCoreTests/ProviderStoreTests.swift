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
