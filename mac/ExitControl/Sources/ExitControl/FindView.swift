import ExitControlCore
import SwiftUI

/// "Find a server this URL accepts" (PLAN Phase 4): the prober tries candidates through
/// its own tunnel, never disturbing the exit; any server that passed can be applied.
struct FindView: View {
    let model: AppModel
    @State private var url = ""
    @State private var country = ""     // "" = current country, then pinned regions
    @State private var mode = "auto"
    @State private var js = false
    @State private var job: FindJob?
    @State private var error: String?
    @State private var poll: Task<Void, Never>?

    var body: some View {
        Form {
            TextField("URL", text: $url, prompt: Text("https://www.example.com/"))
                .onSubmit(start)
            Picker("Countries", selection: $country) {
                Text("Current, then pinned regions").tag("")
                ForEach(model.countries, id: \.self) { Text("\(flag(model.codes[$0])) \($0)").tag($0) }
            }
            Picker("Probe", selection: $mode) {
                Text("Auto (HTTP, browser when unsure)").tag("auto")
                Text("HTTP only (fast)").tag("http")
                Text("Browser").tag("browser")
            }
            Toggle("The site reveals VPN blocks only with JavaScript (e.g. video players)", isOn: $js)
            HStack {
                Button(job?.running == true ? "Probing…" : "Find") { start() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(url.isEmpty || job?.running == true)
                if let job {
                    ProgressView(value: Double(job.progress.done), total: Double(max(job.progress.total, 1)))
                    Text("\(job.progress.done)/\(job.progress.total)").monospacedDigit()
                }
            }
            if let error { Text(error).foregroundStyle(.red) }
        }
        .formStyle(.grouped)
        .frame(maxHeight: 280)
        List(job?.results ?? []) { r in
            HStack {
                Text(icon(r.outcome))
                VStack(alignment: .leading) {
                    Text("\(r.server)  ·  \(r.city ?? ""), \(r.country ?? "")")
                    Text("\(r.outcome)" + ((r.signals ?? []).isEmpty ? "" : ": " + (r.signals ?? []).prefix(3).joined(separator: ", ")))
                        .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer()
                if r.outcome == "ok", let job {
                    Button("Switch") {
                        model.perform("Switch to \(r.server)") { try await $0.applyFind(job: job.jobId, server: r.server, ip: r.ip) }
                    }
                    .disabled(model.busy != nil)
                }
            }
        }
        .overlay {
            if (job?.results ?? []).isEmpty {
                ContentUnavailableView(job?.running == true ? "Probing…" : "No results yet", systemImage: "magnifyingglass",
                    description: Text("The prober tries servers through its own tunnel; the exit is not disturbed."))
            }
        }
        .onDisappear { poll?.cancel() }
    }

    func icon(_ outcome: String) -> String {
        ["ok": "✅", "blocked": "⛔️", "challenge": "🧩", "redirected": "↪️", "suspicious": "🤨", "inconclusive": "❔", "error": "⚠️"][outcome] ?? "❔"
    }

    func start() {
        guard let client = model.client, !url.isEmpty else { return }
        error = nil
        poll?.cancel()
        poll = Task {
            do {
                var j = try await client.find(url: url, countries: country.isEmpty ? [] : [country], mode: mode, js: js)
                job = j
                while j.running && !Task.isCancelled {
                    try await Task.sleep(for: .seconds(1.5))
                    j = try await client.findJob(j.jobId)
                    job = j
                }
                if let e = j.error { error = e }
            } catch is CancellationError {
            } catch { self.error = error.localizedDescription }
        }
    }
}
