import AppIntents
import ExitControlCore
import Foundation

// Shortcuts actions (PLAN Phase 5b). They run inside the app (launched in the background
// if needed) and go through the same queue as exitvpn:// URLs.

enum RotateScope: String, AppEnum {
    case city, country, any
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Scope"
    static let caseDisplayRepresentations: [RotateScope: DisplayRepresentation] = [
        .city: "Same City", .country: "Same Country", .any: "Pinned Regions",
    ]
}

struct RotateExitIntent: AppIntent {
    static let title: LocalizedStringResource = "Rotate Exit Server"
    static let description = IntentDescription("Moves the exit node to another VPN server.")
    @Parameter(title: "Scope", default: .city) var scope: RotateScope

    static var parameterSummary: some ParameterSummary { Summary("Rotate the exit server within \(\.$scope)") }

    @MainActor func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        try await AppModel.shared.run("Rotate (\(scope.rawValue))") { try await $0.rotate(scope.rawValue) }
        let line = AppModel.shared.status.map(statusLine) ?? "Rotated"
        return .result(value: line, dialog: "\(line)")
    }
}

struct CountryOptions: DynamicOptionsProvider {
    @MainActor func results() async throws -> [String] {
        let m = AppModel.shared
        if m.countries.isEmpty { await m.loadServers() }
        return m.countries
    }
}

struct ConnectExitIntent: AppIntent {
    static let title: LocalizedStringResource = "Connect Exit to Region"
    static let description = IntentDescription("Moves the exit node to the least loaded working server in a country, or a city in it.")
    @Parameter(title: "Country", optionsProvider: CountryOptions()) var country: String
    @Parameter(title: "City") var city: String?

    static var parameterSummary: some ParameterSummary { Summary("Connect the exit to \(\.$country), city \(\.$city)") }

    @MainActor func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        let r = Region(country: country, city: city?.isEmpty == true ? nil : city)
        try await AppModel.shared.run("Connect to \(r.label)") { try await $0.select(region: r) }
        let line = AppModel.shared.status.map(statusLine) ?? "Connected"
        return .result(value: line, dialog: "\(line)")
    }
}

struct ExitStatusIntent: AppIntent {
    static let title: LocalizedStringResource = "Exit Status"
    static let description = IntentDescription("The exit node's server, location, exit IP and health.")

    @MainActor func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        guard let client = AppModel.shared.client else { throw AppError("No valid controller URL") }
        let line = statusLine(try await client.status())
        return .result(value: line, dialog: "\(line)")
    }
}

func statusLine(_ st: Status) -> String {
    let place = [st.city, st.country].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: ", ")
    var parts = [st.server ?? "no server", place, "exit IP \(st.exitIp ?? "unknown")", st.health_.label]
    if let e = st.entry, !e.isEmpty { parts.insert("Secure Core via \(e)", at: 1) }
    return parts.filter { !$0.isEmpty }.joined(separator: " · ")
}

struct ExitShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: RotateExitIntent(), phrases: ["Rotate the exit server in \(.applicationName)",
                                                          "New exit server with \(.applicationName)"],
                    shortTitle: "Rotate Exit", systemImageName: "arrow.triangle.2.circlepath")
        AppShortcut(intent: ConnectExitIntent(), phrases: ["Connect the exit in \(.applicationName)"],
                    shortTitle: "Connect Exit", systemImageName: "globe")
        AppShortcut(intent: ExitStatusIntent(), phrases: ["Exit status in \(.applicationName)",
                                                          "Where is my \(.applicationName) exit"],
                    shortTitle: "Exit Status", systemImageName: "shield.lefthalf.filled")
    }
}
