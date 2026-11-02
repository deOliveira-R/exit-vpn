import Foundation
import Testing
@testable import ExitControlCore

// Component: the menu's update item, from Sparkle's steps.

@Test func anAvailableUpdateIsOneClickableItem() {
    let u = UpdateProgress.available(version: "0.7.0")
    #expect(u.title == "Version 0.7.0 available")
    #expect(u.subtitle == "Click to download and install")
    #expect(u.isActionable && u.isPending)
    #expect(UpdateProgress.idle.title == nil && !UpdateProgress.idle.isPending)
}

@Test func theDownloadCountsItsBytes() {
    var u = UpdateProgress.available(version: "0.7.0").receivedExpectedLength(4_000_000)
    u = u.received(1_000_000).received(1_000_000)
    #expect(u == .downloading(version: "0.7.0", received: 2_000_000, expected: 4_000_000))
    #expect(u.fraction == 0.5)
    #expect(u.title == "Downloading…")
    #expect(u.subtitle == "Version 0.7.0: 50% of 4.0 MB")
    #expect(!u.isActionable)
}

@Test func anUnknownLengthShowsNoPercentage() {
    let u = UpdateProgress.available(version: "1.0").receivedExpectedLength(0).received(10)
    #expect(u.fraction == nil)
    #expect(u.subtitle == "Version 1.0")
}

@Test func theReleaseNotesAreTheVersionsGitHubRelease() {
    let feed = URL(string: "https://github.com/o/r/releases/latest/download/appcast.xml")
    #expect(UpdateProgress.releaseNotesURL(feed: feed, version: "0.7.0")?.absoluteString
            == "https://github.com/o/r/releases/tag/v0.7.0")
    #expect(UpdateProgress.releaseNotesURL(feed: URL(string: "https://e.com/a.xml"), version: "1") == nil)
}
