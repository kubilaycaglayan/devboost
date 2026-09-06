import Foundation
@preconcurrency import SwiftTerm
import SwiftUI
import UIKit

struct TerminalPickerView: View {
    @EnvironmentObject private var store: AppStore
    var body: some View {
        List(store.hosts) { host in
            NavigationLink(host.name) { TerminalScreen(host: host) }
        }
        .overlay { if store.hosts.isEmpty { ContentUnavailableView("No SSH hosts", systemImage: "terminal", description: Text("Add a host first.")) } }
        .navigationTitle("Terminal")
    }
}

@MainActor final class TerminalModel: ObservableObject {
    @Published private(set) var state: ConnectionState = .disconnected
    let manager: SSHManager
    let host: Host
    private var terminal: TerminalView?

    init(host: Host, keychain: KeychainStore) { self.host = host; manager = SSHManager(keys: KeyManager(keychain: keychain)) }
    func attach(_ terminal: TerminalView) { self.terminal = terminal }
    func connect() async {
        do {
            let size = terminal.map { ($0.getTerminal().cols, $0.getTerminal().rows) }
            try await manager.connect(host, terminalSize: size, onData: { [weak self] data in Task { @MainActor in self?.terminal?.feed(byteArray: Array(data)[...]) } }, onState: { [weak self] state in Task { @MainActor in self?.state = state } })
        } catch { state = .failed(error.localizedDescription) }
    }
    func send(_ data: Data) { manager.send(data) }
    func resize(cols: Int, rows: Int) { manager.resize(cols: cols, rows: rows) }
    func disconnect() { manager.disconnect(); state = .disconnected }
    func dismissKeyboard() { _ = terminal?.resignFirstResponder() }
}

struct TerminalScreen: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    let host: Host
    @StateObject private var model: TerminalModel
    init(host: Host) { self.host = host; _model = StateObject(wrappedValue: TerminalModel(host: host, keychain: KeychainStore())) }
    var body: some View {
        TerminalRepresentable(model: model).background(.black)
            .navigationTitle(host.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) { Text(model.state.title).font(.caption).foregroundStyle(model.state == .connected ? .green : .secondary) }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { model.dismissKeyboard() } label: { Image(systemName: "keyboard.chevron.compact.down") }
                    Button("Done") { model.disconnect(); dismiss() }
                }
            }
            .task { await model.connect() }
            .onDisappear { model.disconnect() }
    }
}

struct TerminalRepresentable: UIViewRepresentable {
    @ObservedObject var model: TerminalModel
    func makeUIView(context: Context) -> RemoteTerminalView { let view = RemoteTerminalView(frame: .zero); view.model = model; model.attach(view); return view }
    func updateUIView(_ uiView: RemoteTerminalView, context: Context) { uiView.model = model }
}

final class RemoteTerminalView: TerminalView, @preconcurrency TerminalViewDelegate {
    weak var model: TerminalModel?
    override init(frame: CGRect) { super.init(frame: frame); terminalDelegate = self; font = .monospacedSystemFont(ofSize: 14, weight: .regular); backgroundColor = .black }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func send(source: TerminalView, data: ArraySlice<UInt8>) { model?.send(Data(data)) }
    func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) { model?.resize(cols: newCols, rows: newRows) }
    func setTerminalTitle(source: TerminalView, title: String) {}
    func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
    func scrolled(source: TerminalView, position: Double) {}
    func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {}
    func bell(source: TerminalView) { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
    func clipboardCopy(source: TerminalView, content: Data) { UIPasteboard.general.string = String(decoding: content, as: UTF8.self) }
    func clipboardRead(source: TerminalView) -> Data? { UIPasteboard.general.string.map { Data($0.utf8) } }
    func iTermContent(source: TerminalView, content: ArraySlice<UInt8>) {}
    func rangeChanged(source: TerminalView, startY: Int, endY: Int) {}
}
