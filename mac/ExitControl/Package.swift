// swift-tools-version: 6.0
// ExitControl: menu-bar app for the exit node (PLAN Phase 5, D21).
// `swift build` / `swift test`, or open this folder in Xcode; `./build-app.sh` makes the .app.
import PackageDescription

let package = Package(
    name: "ExitControl",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "ExitControl", targets: ["ExitControl"])],
    targets: [
        // Everything testable without a UI: models, controller client, SSE, exitctl, tailscale
        .target(name: "ExitControlCore"),
        .executableTarget(name: "ExitControl", dependencies: ["ExitControlCore"]),
        .testTarget(name: "ExitControlCoreTests", dependencies: ["ExitControlCore"]),
    ]
)
