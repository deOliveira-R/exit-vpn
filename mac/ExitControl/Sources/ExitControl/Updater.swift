import Foundation
import Observation
import Sparkle

/// In-app updates: Sparkle reads the appcast of the latest GitHub Release and
/// installs an update only if its EdDSA signature matches SUPublicEDKey in Info.plist.
@MainActor @Observable
final class Updater {
    @ObservationIgnored private var controller: SPUStandardUpdaterController?
    @ObservationIgnored private var observation: NSKeyValueObservation?
    private(set) var canCheck = false
    private(set) var automaticChecks = true

    func start() {
        guard controller == nil else { return }
        let c = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil)
        controller = c
        automaticChecks = c.updater.automaticallyChecksForUpdates
        observation = c.updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] _, change in
            let can = change.newValue ?? false
            Task { @MainActor in self?.canCheck = can }
        }
    }

    func setAutomaticChecks(_ on: Bool) {
        controller?.updater.automaticallyChecksForUpdates = on
        automaticChecks = on
    }

    var version: String {
        let i = Bundle.main.infoDictionary
        return "\(i?["CFBundleShortVersionString"] as? String ?? "?") (\(i?["CFBundleVersion"] as? String ?? "?"))"
    }

    func check() {
        bringToFront()   // a menu-bar app: the update window must come forward
        controller?.checkForUpdates(nil)
    }
}
