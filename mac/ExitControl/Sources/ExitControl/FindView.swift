import ExitControlCore
import SwiftUI

/// "Find a server this URL accepts" (PLAN Phase 4): the prober tries candidates through its
/// own tunnel, never disturbing the exit; any server that passed can be applied.
struct FindView: View {
    let app: AppContainer
    @State private var session = FindSession(api: { AppContainer.shared.api })

    var body: some View {
        @Bindable var s = session
        Form {
            TextField("URL", text: $s.url, prompt: Text("https://www.example.com/"))
                .onSubmit { session.start() }
            Picker("Countries", selection: $s.country) {
                Text("Current, then pinned regions").tag("")
                ForEach(app.catalog.countries, id: \.self) { Text("\(flag(app.catalog.codes[$0])) \($0)").tag($0) }
            }
            Picker("Probe", selection: $s.mode) {
                ForEach(FindMode.allCases, id: \.self) { Text($0.label).tag($0) }
            }
            Toggle("The site reveals VPN blocks only with JavaScript (e.g. video players)", isOn: $s.js)
            HStack {
                Button(session.running ? "Probing…" : "Find") { session.start() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(session.url.isEmpty || session.running)
                if let job = session.job {
                    ProgressView(value: Double(job.progress.done), total: Double(max(job.progress.total, 1)))
                    Text("\(job.progress.done)/\(job.progress.total)").monospacedDigit()
                }
            }
            if let error = session.error { Text(error).foregroundStyle(.red) }
        }
        .formStyle(.grouped)
        .frame(maxHeight: 280)
        List(session.results) { r in
            HStack {
                Text(r.verdict.icon)
                VStack(alignment: .leading) {
                    Text("\(r.server)  ·  \(r.city ?? ""), \(r.country ?? "")")
                    Text(r.outcome + ((r.signals ?? []).isEmpty ? "" : ": " + (r.signals ?? []).prefix(3).joined(separator: ", ")))
                        .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer()
                if r.verdict == .ok, let job = session.job {
                    Button("Switch") { app.run(.applyFind(job: job.jobId, server: r.server, ip: r.ip)) }
                        .disabled(!app.ready)
                }
            }
        }
        .overlay {
            if session.results.isEmpty {
                ContentUnavailableView(session.running ? "Probing…" : "No results yet", systemImage: "magnifyingglass",
                    description: Text("The prober tries servers through its own tunnel; the exit is not disturbed."))
            }
        }
        .onDisappear { session.stopFollowing() }
    }
}
