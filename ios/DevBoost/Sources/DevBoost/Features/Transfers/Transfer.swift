import Citadel
import CoreTransferable
import Foundation
import NIO
import Photos
import PhotosUI
import SwiftUI
import UIKit
import UniformTypeIdentifiers

private struct OriginalPhotoFile: Transferable {
    let url: URL

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(importedContentType: .image) { received in
            OriginalPhotoFile(url: try PhotoTransferStaging.copy(received.file))
        }
        FileRepresentation(importedContentType: .movie) { received in
            OriginalPhotoFile(url: try PhotoTransferStaging.copy(received.file))
        }
    }
}

enum PhotoTransferStaging {
    static func destination(named name: String) throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("devboost-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        return directory.appendingPathComponent(name)
    }

    static func copy(_ source: URL) throws -> URL {
        // Isolate each selection in a directory; the filename is user data.
        let destination = try destination(named: source.lastPathComponent)
        do {
            try FileManager.default.copyItem(at: source, to: destination)
            return destination
        } catch {
            try? FileManager.default.removeItem(at: destination.deletingLastPathComponent())
            throw error
        }
    }

    static func discard(_ url: URL) {
        try? FileManager.default.removeItem(at: url.deletingLastPathComponent())
    }
}

enum FileUploadStream {
    static let chunkSize = 128 * 1024

    static func write(
        fileURL: URL,
        chunkSize: Int = chunkSize,
        progress: @escaping @Sendable (Double) -> Void,
        writeChunk: (Data, UInt64) async throws -> Void
    ) async throws -> UInt64 {
        precondition(chunkSize > 0)
        let fileSize = (try fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        let progressSize = max(1, fileSize)
        let handle = try FileHandle(forReadingFrom: fileURL)
        defer { try? handle.close() }
        var offset: UInt64 = 0
        while let data = try handle.read(upToCount: chunkSize), !data.isEmpty {
            try Task.checkCancellation()
            try await writeChunk(data, offset)
            offset += UInt64(data.count)
            progress(min(1, Double(offset) / Double(progressSize)))
        }
        return offset
    }

    static func verify(expectedSize: Int, uploadedSize: UInt64?) throws {
        guard uploadedSize == UInt64(expectedSize) else {
            throw AppError.connectionFailed("Upload verification failed: the server received \(uploadedSize ?? 0) of \(expectedSize) bytes.")
        }
    }
}

struct RemoteFileTransfer: Sendable {
    let keychain: KeychainStore

    func homeDirectory(on host: Host) async throws -> String {
        let value = try await RemoteCommandService(keychain: keychain).execute("pwd", on: host)
        return try RemoteTransferOutput.homeDirectory(from: value)
    }

    func folders(at path: String, on host: Host) async throws -> [String] {
        let clean = RemotePath.normalized(path)
        let command = RemoteTransferCommands.folders(path: clean)
        return try await RemoteCommandService(keychain: keychain).execute(command, on: host).split(whereSeparator: \.isNewline).map(String.init)
    }
    func createDirectory(_ path: String, on host: Host) async throws {
        let clean = RemotePath.normalized(path)
        _ = try await RemoteCommandService(keychain: keychain).execute(RemoteTransferCommands.createDirectory(path: clean), on: host)
    }
    func upload(fileURL: URL, to directory: String, on host: Host, progress: @escaping @Sendable (Double) -> Void) async throws -> String {
        let destination = RemotePath.normalized(directory)
        let name = RemotePath.safeFileName(fileURL.lastPathComponent)
        let remotePath = destination + "/" + name
        try await createDirectory(destination, on: host)
        let fileSize = (try fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        let client = try await SSHConnectionFactory(keychain: keychain).connect(to: host)
        do {
            try await client.withSFTP { sftp in
                try? await sftp.remove(at: remotePath)
                try await sftp.withFile(filePath: remotePath, flags: [.write, .create, .truncate]) { remoteFile in
                    _ = try await FileUploadStream.write(fileURL: fileURL, progress: progress) { data, offset in
                        try await remoteFile.write(ByteBuffer(data: data), at: offset)
                    }
                }
                let uploadedSize = try await sftp.getAttributes(at: remotePath).size
                try FileUploadStream.verify(expectedSize: fileSize, uploadedSize: uploadedSize)
            }
            try? await client.close()
        } catch {
            try? await client.close()
            throw error
        }
        return remotePath
    }
}

enum RemotePath {
    static func normalized(_ path: String) -> String {
        let clean = path.trimmingCharacters(in: .whitespacesAndNewlines)
        return clean.hasPrefix("/") ? clean : "/" + clean
    }

    static func safeFileName(_ name: String) -> String {
        name.replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "\0", with: "_")
    }
}

enum RemoteTransferCommands {
    static func folders(path: String) -> String {
        "find \(shellQuote(path)) -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | LC_ALL=C sort"
    }

    static func createDirectory(path: String) -> String {
        "mkdir -p -- \(shellQuote(path))"
    }
}

enum RemoteTransferOutput {
    static func homeDirectory(from output: String) throws -> String {
        guard let home = output.split(whereSeparator: \.isNewline).first.map(String.init), home.hasPrefix("/") else {
            throw AppError.connectionFailed("The server did not return a valid home directory.")
        }
        return home
    }
}

struct TransferView: View {
    @EnvironmentObject private var store: AppStore
    @State private var hostID: UUID?
    @State private var destination = ""
    @State private var folders: [String] = []
    @State private var selectedURLs: [URL] = []
    @State private var importerPresented = false
    @State private var photoPickerPresented = false
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var photoPermissionDenied = false
    @State private var isLoadingFolders = false
    @State private var isUploading = false
    @State private var progress = 0.0
    @State private var message = "Choose a host and a destination folder."
    @State private var newFolderName = ""
    private var host: Host? { store.hosts.first { $0.id == (hostID ?? store.hosts.first?.id) } }

    var body: some View {
        List {
            Section("Destination") {
                Picker("SSH host", selection: $hostID) { ForEach(store.hosts) { Text($0.name).tag(Optional($0.id)) } }
                TextField("Absolute remote folder", text: $destination).textInputAutocapitalization(.never).autocorrectionDisabled()
                HStack {
                    Button("Show folders", systemImage: "folder") { loadFolders() }.disabled(host == nil || isLoadingFolders)
                    Button("Create folder", systemImage: "folder.badge.plus") { createFolder() }.disabled(host == nil || newFolderName.isEmpty)
                }
                TextField("New folder name", text: $newFolderName).textInputAutocapitalization(.never).autocorrectionDisabled()
                ForEach(store.destinations(for: host ?? Host())) { item in Button(item.path) { destination = item.path; loadFolders() } }
            }
            if !folders.isEmpty { Section("Existing folders") { ForEach(folders, id: \.self) { folder in Button(folder) { destination = folder; loadFolders() } } } }
            Section("Files") {
                Button("Choose files", systemImage: "doc.badge.plus") { importerPresented = true }
                Button("Choose photos or videos", systemImage: "photo.badge.plus") { choosePhotos() }
                if photoPermissionDenied {
                    Button("Allow Photo Library access in Settings", systemImage: "gear") {
                        UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!)
                    }
                }
                ForEach(selectedURLs, id: \.self) { Text($0.lastPathComponent) }
                if isUploading { ProgressView(value: progress) }
                Button("Upload to server", systemImage: "arrow.up.circle.fill") { upload() }.disabled(host == nil || destination.isEmpty || selectedURLs.isEmpty || isUploading)
            }
            Section { Text(message).font(.footnote).foregroundStyle(.secondary) }
        }
        .overlay { if store.hosts.isEmpty { ContentUnavailableView("No SSH hosts", systemImage: "arrow.up.doc", description: Text("Add a host before transferring files.")) } }
        .navigationTitle("File Transfer")
        .onAppear { if hostID == nil { hostID = store.hosts.first?.id }; if destination.isEmpty, let host, let latest = store.destinations(for: host).first { destination = latest.path }; if destination.isEmpty { loadFolders() } }
        .onChange(of: hostID) { _, _ in
            destination = host.flatMap { store.destinations(for: $0).first?.path } ?? ""
            folders = []
            loadFolders()
        }
        .fileImporter(isPresented: $importerPresented, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in if case .success(let urls) = result { selectedURLs = urls; message = "\(urls.count) file\(urls.count == 1 ? "" : "s") selected." } }
        .photosPicker(isPresented: $photoPickerPresented, selection: $photoItems, maxSelectionCount: 20, matching: .any(of: [.images, .videos]), preferredItemEncoding: .current)
        .onChange(of: photoItems) { _, items in importPhotos(items) }
    }

    private func choosePhotos() {
        let status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        switch status {
        case .authorized, .limited:
            photoPermissionDenied = false
            photoPickerPresented = true
        case .notDetermined:
            Task {
                let result = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
                if result == .authorized || result == .limited {
                    photoPermissionDenied = false
                    photoPickerPresented = true
                } else {
                    photoPermissionDenied = true
                    message = "Allow Photo Library access to transfer photos in their original format."
                }
            }
        case .denied, .restricted:
            photoPermissionDenied = true
            message = "Allow Photo Library access to transfer photos in their original format."
        @unknown default:
            photoPermissionDenied = true
            message = "Photo Library access is unavailable."
        }
    }

    private func loadFolders() {
        guard let host else { return }
        isLoadingFolders = true; message = "Loading folders…"
        Task {
            do {
                if destination.isEmpty { destination = try await RemoteFileTransfer(keychain: store.keychain).homeDirectory(on: host) }
                folders = try await RemoteFileTransfer(keychain: store.keychain).folders(at: destination, on: host)
                store.remember(destination: destination, for: host)
                message = folders.isEmpty ? "No subfolders found." : "Choose a folder or upload here."
            } catch { folders = []; message = error.localizedDescription }
            isLoadingFolders = false
        }
    }
    private func createFolder() {
        guard let host, !destination.isEmpty else { return }
        let path = destination.hasSuffix("/") ? destination + newFolderName : destination + "/" + newFolderName
        Task { do { try await RemoteFileTransfer(keychain: store.keychain).createDirectory(path, on: host); destination = path; newFolderName = ""; store.remember(destination: path, for: host); loadFolders(); message = "Folder created." } catch { message = error.localizedDescription } }
    }
    private func upload() {
        guard let host else { return }
        isUploading = true; progress = 0; message = "Uploading…"
        let urls = selectedURLs
        Task {
            var completed = 0
            for url in urls {
                let completedBeforeCurrentFile = completed
                let scoped = url.startAccessingSecurityScopedResource()
                defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                defer {
                    if url.deletingLastPathComponent().lastPathComponent.hasPrefix("devboost-") {
                        PhotoTransferStaging.discard(url)
                    }
                }
                do {
                    let remote = try await RemoteFileTransfer(keychain: store.keychain).upload(fileURL: url, to: destination, on: host) { part in Task { @MainActor in progress = (Double(completedBeforeCurrentFile) + part) / Double(urls.count) } }
                    store.record(TransferRecord(id: UUID(), hostID: host.id, fileName: url.lastPathComponent, remotePath: remote, createdAt: .now, succeeded: true, message: "Uploaded"))
                    completed += 1
                } catch {
                    store.record(TransferRecord(id: UUID(), hostID: host.id, fileName: url.lastPathComponent, remotePath: destination, createdAt: .now, succeeded: false, message: error.localizedDescription))
                    message = "Upload failed: \(error.localizedDescription)"; isUploading = false; return
                }
            }
            store.remember(destination: destination, for: host); message = "Uploaded \(completed) file\(completed == 1 ? "" : "s")."; isUploading = false; selectedURLs = []
        }
    }
    private func importPhotos(_ items: [PhotosPickerItem]) {
        guard !items.isEmpty else { return }
        Task {
            var urls: [URL] = []
            var failures = 0
            for item in items {
                do {
                    if let url = try await originalAssetURL(for: item) {
                        urls.append(url)
                        continue
                    }
                    guard let file = try await item.loadTransferable(type: OriginalPhotoFile.self) else {
                        failures += 1
                        continue
                    }
                    urls.append(file.url)
                } catch {
                    failures += 1
                }
            }
            selectedURLs.append(contentsOf: urls)
            photoItems = []
            if failures == 0 {
                message = "\(urls.count) photo or video item\(urls.count == 1 ? "" : "s") selected."
            } else if urls.isEmpty {
                message = "The selected photo or video could not be read in its original format."
            } else {
                message = "\(urls.count) selected; \(failures) could not be read in its original format."
            }
        }
    }

    private func originalAssetURL(for item: PhotosPickerItem) async throws -> URL? {
        guard let identifier = item.itemIdentifier,
              let asset = PHAsset.fetchAssets(withLocalIdentifiers: [identifier], options: nil).firstObject else {
            return nil
        }
        let resources = PHAssetResource.assetResources(for: asset)
        let resource: PHAssetResource?
        if asset.mediaType == .video {
            resource = resources.first { $0.type == .video } ?? resources.first { $0.type == .fullSizeVideo }
        } else {
            resource = resources.first { $0.type == .photo } ?? resources.first { $0.type == .fullSizePhoto }
        }
        guard let resource, !resource.originalFilename.isEmpty else { return nil }
        let destination = try PhotoTransferStaging.destination(named: resource.originalFilename)
        let options = PHAssetResourceRequestOptions()
        options.isNetworkAccessAllowed = true
        do {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                PHAssetResourceManager.default().writeData(for: resource, toFile: destination, options: options) { error in
                    if let error { continuation.resume(throwing: error) }
                    else { continuation.resume() }
                }
            }
            return destination
        } catch {
            PhotoTransferStaging.discard(destination)
            throw error
        }
    }
}
