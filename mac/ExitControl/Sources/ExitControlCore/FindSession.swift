import Foundation
import Observation

/// "Find a server for this URL": starts a job on the controller and follows it
/// until it is done. Closing the window stops following; the job itself carries on.
@MainActor @Observable
public final class FindSession {
    public private(set) var job: FindJob?
    public private(set) var error: String?
    public var url = ""
    public var country = ""          // "" = the current country, then the pinned regions
    public var mode: FindMode = .auto
    public var js = false

    private let api: @MainActor () -> (any ControllerAPI)?
    private var follow: Task<Void, Never>?
    private let pollEvery: Duration

    public init(api: @escaping @MainActor () -> (any ControllerAPI)?, pollEvery: Duration = .seconds(1.5)) {
        self.api = api
        self.pollEvery = pollEvery
    }

    public var running: Bool { job?.running == true }
    public var results: [FindResult] { job?.results ?? [] }

    public func start() {
        guard let api = api(), !url.isEmpty, !running else { return }
        error = nil
        follow?.cancel()
        follow = Task { [pollEvery, url, country, mode, js] in
            do {
                var j = try await api.find(url: url, countries: country.isEmpty ? [] : [country],
                                           max: 10, mode: mode, js: js)
                self.job = j
                while j.running && !Task.isCancelled {
                    try await Task.sleep(for: pollEvery)
                    j = try await api.findJob(j.jobId)
                    self.job = j
                }
                if let e = j.error, !e.isEmpty { self.error = e }
            } catch is CancellationError {
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    public func stopFollowing() { follow?.cancel() }
}
