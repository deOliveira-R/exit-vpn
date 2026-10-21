// swift-tools-version: 6.0
// ExitControl: menu-bar app for the exit node (PLAN Phase 5, D21).
// This package holds the testable core (`swift test`). The app itself (Sources/ExitControl,
// with Sparkle and App Intents) is built by Xcode from project.yml: `./build-app.sh`.
import PackageDescription

let package = Package(
    name: "ExitControl",
    platforms: [.macOS(.v14)],
    products: [.library(name: "ExitControlCore", targets: ["ExitControlCore"])],
    targets: [
        // Everything testable without a UI: models, controller client, SSE, exitctl, tailscale
        .target(name: "ExitControlCore"),
        .testTarget(name: "ExitControlCoreTests", dependencies: ["ExitControlCore"]),
    ]
)
