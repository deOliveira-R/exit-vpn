import AppKit
import ExitControlCore
import SwiftUI

/// The menu's update item, drawn by the app (as Tailscale's menu does): two lines, the
/// download's progress ring on the right. SwiftUI's menu-bar menu renders only plain titles
/// and closes on every click, so the item it makes (titled `placeholder`) gets this view
/// once the menu opens. A view-based item keeps the menu open when clicked, so the download
/// stays in view.
enum UpdateMenuItem {
    static let placeholder = "ExitControl Update"
    @MainActor private static weak var openMenu: NSMenu?   // the menu showing the item, while open

    /// Watches the app's menus: whenever one opens or changes, its placeholder item gets the view.
    @MainActor static func install(updater: Updater) {
        let center = NotificationCenter.default
        center.addObserver(forName: NSMenu.didEndTrackingNotification, object: nil, queue: .main) { note in
            nonisolated(unsafe) let menu = note.object as? NSMenu
            MainActor.assumeIsolated { if menu === openMenu { openMenu = nil } }   // not a submenu's end
        }
        for name in [NSMenu.didBeginTrackingNotification, NSMenu.didAddItemNotification,
                     NSMenu.didChangeItemNotification] {
            center.addObserver(forName: name, object: nil, queue: .main) { note in
                nonisolated(unsafe) let menu = note.object as? NSMenu   // posted on the main thread
                MainActor.assumeIsolated { if let menu { attach(to: menu, updater: updater) } }
            }
        }
    }

    /// Closes the menu if it is open: macOS lets an app quit only once its menu has closed,
    /// so the relaunch into the new version would wait for the owner to click away.
    @MainActor static func closeMenu() {
        openMenu?.cancelTracking()
        openMenu = nil
    }

    @MainActor private static func attach(to menu: NSMenu, updater: Updater) {
        guard let item = menu.items.first(where: { $0.title == placeholder }) else { return }
        openMenu = menu
        guard item.view == nil else { return }
        let host = NSHostingView(rootView: UpdateItemView(updater: updater))
        host.frame.size = NSSize(width: max(menu.size.width, 300), height: 44)
        item.view = host
    }
}

private struct UpdateItemView: View {
    let updater: Updater
    @State private var hover = false

    var body: some View {
        let u = updater.progress
        let lit = hover && u.isActionable
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(u.title ?? "").foregroundStyle(lit ? .white : .primary)
                if let s = u.subtitle {
                    Text(s).font(.callout).foregroundStyle(lit ? .white.opacity(0.85) : .secondary)
                }
            }
            Spacer(minLength: 12)
            indicator(u).foregroundStyle(lit ? .white : .accentColor)
        }
        .padding(.leading, 25)    // the other items' titles start past the checkmark column
        .padding(.trailing, 10)
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 6).fill(lit ? Color.accentColor : .clear))
        .padding(.horizontal, 5)
        .contentShape(Rectangle())
        .onHover { hover = $0 }
        .onTapGesture { updater.act() }
    }

    @ViewBuilder private func indicator(_ u: UpdateProgress) -> some View {
        switch u {
        case .available:
            Image(systemName: "arrow.down.circle").font(.title2)
        case .downloading, .extracting:
            if let f = u.fraction {
                ZStack {
                    Circle().stroke(.secondary.opacity(0.3), lineWidth: 2.5)
                    Circle().trim(from: 0, to: max(0.02, f))
                        .stroke(style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                        .animation(.linear(duration: 0.2), value: f)
                    Image(systemName: "arrow.down").font(.system(size: 9, weight: .bold))
                }
                .frame(width: 20, height: 20)
            } else {
                ProgressView().controlSize(.small)   // the size is not known yet
            }
        case .checking, .installing:
            ProgressView().controlSize(.small)
        case .failed:
            Image(systemName: "exclamationmark.triangle").font(.title3)
        case .idle:
            EmptyView()
        }
    }
}
