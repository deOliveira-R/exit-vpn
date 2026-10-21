import Foundation
import Observation

/// Runs the owner's actions one at a time (the controller switches one server at a time, and
/// a key change must not overlap another). Two ways in:
/// - `.rejectIfBusy` (menu clicks): refused while something runs; the menu greys out anyway.
/// - `.enqueue` (exitvpn:// URLs, Shortcuts): waits its turn, in the order sent, so a script
///   can send several in a row. Gives up after `queueTimeout`.
/// `busy` names what runs; `lastError` keeps the last failure for the menu.
@MainActor @Observable
public final class ActionRunner {
    public enum Policy: Sendable { case rejectIfBusy, enqueue }

    public struct Busy: Error, LocalizedError {
        public var running: String
        public var errorDescription: String? { "Still busy with \(running)" }
    }

    public private(set) var busy: String?
    public private(set) var lastError: String?
    /// After every success (the connection refreshes the status).
    public var afterSuccess: @MainActor () async -> Void = {}
    /// After every failure (notifications), with the label and the message.
    public var onFailure: @MainActor (String, String) -> Void = { _, _ in }
    public var queueTimeout: Duration = .seconds(180)

    private var tail: Task<Void, Never>?

    public init() {}

    public var isIdle: Bool { busy == nil && tail == nil }

    /// Runs `work` under `label`; returns false if it was refused (`.rejectIfBusy` while busy).
    @discardableResult
    public func submit(_ label: String, policy: Policy = .rejectIfBusy,
                       _ work: @escaping @MainActor () async throws -> Void) async throws -> Bool {
        switch policy {
        case .rejectIfBusy:
            guard isIdle else { return false }
            try await execute(label, work)
            return true
        case .enqueue:
            let previous = tail
            let deadline = ContinuousClock.now + queueTimeout
            let mine = Task { @MainActor in
                await previous?.value
                while self.busy != nil {   // a menu action started meanwhile
                    if ContinuousClock.now > deadline { throw Busy(running: self.busy ?? "another action") }
                    try await Task.sleep(for: .milliseconds(200))
                }
                try await self.execute(label, work)
            }
            let link = Task { @MainActor in _ = try? await mine.value }
            tail = link
            defer { if tail == link { tail = nil } }
            try await mine.value
            return true
        }
    }

    private func execute(_ label: String, _ work: @MainActor () async throws -> Void) async throws {
        busy = label
        lastError = nil
        defer { busy = nil }
        do {
            try await work()
        } catch {
            lastError = "\(label): \(error.localizedDescription)"
            onFailure(label, error.localizedDescription)
            throw error
        }
        await afterSuccess()
    }

    public func clearError() { lastError = nil }
}
