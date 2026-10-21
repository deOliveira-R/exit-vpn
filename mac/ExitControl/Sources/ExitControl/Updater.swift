import Foundation
import Observation
import Sparkle

/// In-app updates (PLAN Phase 5c): Sparkle reads the appcast of the latest GitHub Release and
/// installs an update only if its EdDSA signature matches SUPublicEDKey in Info.plist.
@MainActor @Observable
final class Updater {
    static let shared = Updater()

    private let controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: nil,
                                                          userDriverDelegate: nil)
    private(set) var canCheck = false
    private var observation: NSKeyValueObservation?

    private init() {
        observation = controller.updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] _, change in
            let can = change.newValue ?? false
            Task { @MainActor in self?.canCheck = can }
        }
    }

    var automaticChecks: Bool {
        get { controller.updater.automaticallyChecksForUpdates }
        set { controller.updater.automaticallyChecksForUpdates = newValue }
    }

    var version: String {
        let i = Bundle.main.infoDictionary
        return "\(i?["CFBundleShortVersionString"] as? String ?? "?") (\(i?["CFBundleVersion"] as? String ?? "?"))"
    }

    func check() {
        bringToFront()   // a menu-bar app: the update window must come forward
        controller.checkForUpdates(nil)
    }
}
