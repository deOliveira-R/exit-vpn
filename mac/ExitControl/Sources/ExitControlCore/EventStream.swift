import Foundation

/// Server-sent events (text/event-stream), as `/v1/events` sends them.
public struct SSEMessage: Sendable, Equatable {
    public var event: String
    public var data: String
}

/// Incremental parser: feed lines, get messages at each blank line. Comments
/// (": keepalive") are ignored.
public struct SSEParser: Sendable {
    var event = "message"
    var data: [String] = []
    public init() {}

    public mutating func feed(_ line: String) -> SSEMessage? {
        if line.isEmpty {
            defer { event = "message"; data = [] }
            return data.isEmpty ? nil : SSEMessage(event: event, data: data.joined(separator: "\n"))
        }
        if line.hasPrefix(":") { return nil }
        let (field, value): (Substring, Substring)
        if let i = line.firstIndex(of: ":") {
            field = line[..<i]
            var v = line[line.index(after: i)...]
            if v.first == " " { v = v.dropFirst() }
            value = v
        } else {
            field = Substring(line); value = ""
        }
        switch field {
        case "event": event = String(value)
        case "data": data.append(String(value))
        default: break
        }
        return nil
    }
}

public enum StreamUpdate: Sendable {
    case connected
    case status(Status)
    case event(Event)
    case disconnected(String)
}

extension ControllerClient {
    /// Live updates, reconnecting with backoff forever (until the task is cancelled).
    /// The first message after each connect is a full status.
    public func updates() -> AsyncStream<StreamUpdate> {
        AsyncStream { cont in
            let task = Task {
                var delay: UInt64 = 1
                while !Task.isCancelled {
                    do {
                        var req = URLRequest(url: baseURL.appending(path: "v1/events"))
                        req.timeoutInterval = 20   // keepalives arrive every 15 s: silence means offline
                        let (bytes, resp) = try await session.bytes(for: req)
                        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
                            throw APIError(status: (resp as? HTTPURLResponse)?.statusCode ?? 0, detail: "events refused")
                        }
                        cont.yield(.connected)
                        delay = 1
                        var parser = SSEParser()
                        // `lines` drops empty lines, so message boundaries come from the
                        // raw bytes: split on "\n" ourselves.
                        var buf = Data()
                        for try await byte in bytes {
                            if byte != UInt8(ascii: "\n") { buf.append(byte); continue }
                            var line = String(decoding: buf, as: UTF8.self)
                            buf.removeAll(keepingCapacity: true)
                            if line.hasSuffix("\r") { line.removeLast() }
                            guard let msg = parser.feed(line) else { continue }
                            let data = Data(msg.data.utf8)
                            if msg.event == "status", let st = try? Self.decoder.decode(Status.self, from: data) {
                                cont.yield(.status(st))
                            } else if let ev = try? Self.decoder.decode(Event.self, from: data) {
                                cont.yield(.event(ev))
                            }
                        }
                        cont.yield(.disconnected("stream ended"))
                    } catch {
                        if Task.isCancelled { break }
                        cont.yield(.disconnected(error.localizedDescription))
                    }
                    try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                    delay = min(delay * 2, 30)
                }
                cont.finish()
            }
            cont.onTermination = { _ in task.cancel() }
        }
    }
}
