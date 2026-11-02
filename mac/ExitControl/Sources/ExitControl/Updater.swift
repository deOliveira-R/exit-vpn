import AppKit
import ExitControlCore
import Foundation
import Observation
import Sparkle

/// In-app updates: Sparkle reads the appcast of the latest GitHub Release and installs an
/// update only if its EdDSA signature matches SUPublicEDKey in Info.plist. Sparkle's own
/// windows are replaced by the menu (as Tailscale's menu does it): a found update becomes
/// "Version X available", a click downloads it ("Downloading…" with its progress), and it
/// installs and relaunches at once. The owner asked for it, so there is no second prompt.
/// Checks: at launch and once a day (SUScheduledCheckInterval), unless turned off in
/// Settings → General, which also has "Check Now".
@MainActor @Observable
final class Updater: NSObject, SPUUserDriver {
    @ObservationIgnored private var updater: SPUUpdater?
    @ObservationIgnored private var observation: NSKeyValueObservation?
    @ObservationIgnored private var choose: ((SPUUserUpdateChoice) -> Void)?
    @ObservationIgnored private var cancelDownload: (() -> Void)?
    @ObservationIgnored private var userInitiated = false
    @ObservationIgnored private var installFound = false   // exitvpn://update: no click needed
    private(set) var canCheck = false
    private(set) var automaticChecks = true
    private(set) var progress: UpdateProgress = .idle
    private(set) var releaseNotes: URL?

    func start() {
        guard updater == nil else { return }
        let u = SPUUpdater(hostBundle: .main, applicationBundle: .main, userDriver: self, delegate: nil)
        do { try u.start() } catch { Log.error("updater: \(error.localizedDescription)"); return }
        updater = u
        automaticChecks = u.automaticallyChecksForUpdates
        if automaticChecks { u.checkForUpdatesInBackground() }   // at launch; then daily
        observation = u.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] _, change in
            let can = change.newValue ?? false
            Task { @MainActor in self?.canCheck = can }
        }
    }

    func setAutomaticChecks(_ on: Bool) {
        updater?.automaticallyChecksForUpdates = on
        automaticChecks = on
    }

    var shortVersion: String { Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?" }

    var version: String {
        let i = Bundle.main.infoDictionary
        return "\(i?["CFBundleShortVersionString"] as? String ?? "?") (\(i?["CFBundleVersion"] as? String ?? "?"))"
    }

    /// Settings → "Check Now": a found update shows in the menu (and next to the button);
    /// "up to date" and errors in an alert.
    func check(install: Bool = false) {
        if install, case .available = progress { act(); return }
        userInitiated = !install   // a script gets no alerts; the log and the menu show it
        installFound = install
        updater?.checkForUpdates()
    }

    /// The update item's click: download and install a found update, or dismiss a failure.
    func act() {
        switch progress {
        case .available:
            Log.info("update: download \(progress.version ?? "?")")
            choose?(.install)
            choose = nil
        case .failed:
            progress = .idle
        default: break
        }
    }

    // SPUUserDriver ------------------------------------------------------------------------
    func show(_ request: SPUUpdatePermissionRequest,
              reply: @escaping (SUUpdatePermissionResponse) -> Void) {
        reply(SUUpdatePermissionResponse(automaticUpdateChecks: true, sendSystemProfile: false))
    }

    func showUserInitiatedUpdateCheck(cancellation: @escaping () -> Void) {
        progress = .checking
    }

    func showUpdateFound(with appcastItem: SUAppcastItem, state: SPUUserUpdateState,
                         reply: @escaping (SPUUserUpdateChoice) -> Void) {
        let v = appcastItem.displayVersionString
        Log.info("update: \(v) found (\(state.stage.rawValue))")
        releaseNotes = appcastItem.fullReleaseNotesURL ?? appcastItem.releaseNotesURL
            ?? UpdateProgress.releaseNotesURL(feed: updater?.feedURL, version: v)
        userInitiated = false
        if state.stage == .notDownloaded {
            progress = .available(version: v)
            choose = reply
            if installFound { installFound = false; act() }
        } else {   // downloaded earlier (or installing): finish it
            progress = .installing(version: v)
            reply(.install)
        }
    }

    func showUpdateReleaseNotes(with downloadData: SPUDownloadData) {}
    func showUpdateReleaseNotesFailedToDownloadWithError(_ error: any Error) {}

    func showUpdateNotFoundWithError(_ error: any Error, acknowledgement: @escaping () -> Void) {
        progress = .idle
        if userInitiated { alert("ExitControl is up to date", error.localizedDescription) }
        Log.info("update: none (\(error.localizedDescription))")
        userInitiated = false
        installFound = false
        acknowledgement()
    }

    func showUpdaterError(_ error: any Error, acknowledgement: @escaping () -> Void) {
        Log.error("update: \(error.localizedDescription)")
        progress = .failed(message: String(error.localizedDescription.prefix(80)))
        if userInitiated { alert("Updating ExitControl failed", error.localizedDescription) }
        userInitiated = false
        installFound = false
        choose = nil
        acknowledgement()
    }

    func showDownloadInitiated(cancellation: @escaping () -> Void) {
        cancelDownload = cancellation
        progress = progress.receivedExpectedLength(0)
    }

    func showDownloadDidReceiveExpectedContentLength(_ expectedContentLength: UInt64) {
        progress = progress.receivedExpectedLength(expectedContentLength)
    }

    func showDownloadDidReceiveData(ofLength length: UInt64) {
        progress = progress.received(length)
    }

    func showDownloadDidStartExtractingUpdate() {
        cancelDownload = nil
        if let v = progress.version { progress = .extracting(version: v, fraction: 0) }
    }

    func showExtractionReceivedProgress(_ fraction: Double) {
        if let v = progress.version { progress = .extracting(version: v, fraction: fraction) }
    }

    func showReady(toInstallAndRelaunch reply: @escaping (SPUUserUpdateChoice) -> Void) {
        if let v = progress.version { progress = .installing(version: v) }
        Log.info("update: installing and relaunching")
        // Seen in the menu for a moment, then the menu closes: an open menu holds off the quit
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            UpdateMenuItem.closeMenu()
            reply(.install)
        }
    }

    func showInstallingUpdate(withApplicationTerminated applicationTerminated: Bool,
                              retryTerminatingApplication: @escaping () -> Void) {
        if !applicationTerminated { retryTerminatingApplication() }
    }

    func showUpdateInstalledAndRelaunched(_ relaunched: Bool, acknowledgement: @escaping () -> Void) {
        progress = .idle
        acknowledgement()
    }

    func showUpdateInFocus() {}

    func dismissUpdateInstallation() {
        if case .failed = progress { return }   // stays until clicked away
        if case .available = progress, choose != nil { return }
        progress = .idle
        choose = nil
        cancelDownload = nil
    }

    private func alert(_ title: String, _ text: String) {
        bringToFront()
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.runModal()
    }
}
