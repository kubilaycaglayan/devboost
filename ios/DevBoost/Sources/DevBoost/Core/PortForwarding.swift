@preconcurrency import Citadel
import Foundation
import NIO
import NIOSSH

enum PortForwardError: LocalizedError, Equatable {
    case invalidConfiguration
    case localPortUnavailable

    var errorDescription: String? {
        switch self {
        case .invalidConfiguration: "Choose valid local and remote ports and a remote host."
        case .localPortUnavailable: "The selected local port is already in use."
        }
    }
}

/// Bridges one local TCP connection to one SSH direct-tcpip channel.
private final class LocalForwardHandler: ChannelInboundHandler, @unchecked Sendable {
    typealias InboundIn = ByteBuffer
    typealias OutboundOut = ByteBuffer

    private let client: SSHClient
    private let forward: PortForward
    private weak var localChannel: Channel?
    private var remoteChannel: Channel?
    private var pending = [ByteBuffer]()

    init(client: SSHClient, forward: PortForward, localChannel: Channel) {
        self.client = client
        self.forward = forward
        self.localChannel = localChannel
    }

    func channelActive(context: ChannelHandlerContext) {
        localChannel = context.channel
        do {
            let originator = try SocketAddress(ipAddress: "127.0.0.1", port: forward.localPort)
            let localChannel = context.channel
            Task { [weak self] in
                guard let self else { return }
                do {
                    let remote = try await client.createDirectTCPIPChannel(
                        using: SSHChannelType.DirectTCPIP(targetHost: forward.remoteHost, targetPort: forward.remotePort, originatorAddress: originator)
                    ) { channel in
                        return channel.pipeline.addHandler(RemoteForwardHandler(localChannel: localChannel))
                    }
                    localChannel.eventLoop.execute { [weak self] in
                        guard let self else { return }
                        self.remoteChannel = remote
                        for buffer in self.pending {
                            remote.writeAndFlush(buffer, promise: nil)
                        }
                        self.pending.removeAll()
                    }
                } catch {
                    localChannel.close(promise: nil)
                }
            }
        } catch {
            context.close(promise: nil)
        }
    }

    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        let buffer = self.unwrapInboundIn(data)
        if let remoteChannel {
            remoteChannel.writeAndFlush(buffer, promise: nil)
        } else {
            pending.append(buffer)
        }
    }

    func channelInactive(context: ChannelHandlerContext) {
        remoteChannel?.close(promise: nil)
        remoteChannel = nil
    }
}

private final class RemoteForwardHandler: ChannelInboundHandler, @unchecked Sendable {
    typealias InboundIn = ByteBuffer
    typealias OutboundOut = ByteBuffer

    private weak var localChannel: Channel?
    init(localChannel: Channel) { self.localChannel = localChannel }

    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        guard let localChannel else {
            context.close(promise: nil)
            return
        }
        localChannel.writeAndFlush(self.unwrapInboundIn(data), promise: nil)
    }

    func channelInactive(context: ChannelHandlerContext) {
        localChannel?.close(promise: nil)
    }
}

private final class PortForwardRuntime: @unchecked Sendable {
    let listener: Channel
    let client: SSHClient

    init(listener: Channel, client: SSHClient) {
        self.listener = listener
        self.client = client
    }

    func stop() async {
        try? await listener.close()
        try? await client.close()
    }
}

/// Owns the foreground tunnels. Definitions live in AppStore; this object only
/// owns sockets and SSH sessions while the app is active.
actor PortForwardManager {
    private var runtimes: [UUID: PortForwardRuntime] = [:]

    func isRunning(_ forward: PortForward) -> Bool { runtimes[forward.id] != nil }

    func start(_ forward: PortForward, on host: Host, keychain: KeychainStore) async throws {
        guard forward.isValid, host.isConfigured else { throw PortForwardError.invalidConfiguration }
        if runtimes[forward.id] != nil { return }

        let client = try await SSHConnectionFactory(keychain: keychain).connect(to: host)
        do {
            let listener = try await ServerBootstrap(group: client.eventLoop)
                .serverChannelOption(ChannelOptions.backlog, value: 16)
                .serverChannelOption(ChannelOptions.socket(SocketOptionLevel(SOL_SOCKET), SO_REUSEADDR), value: 1)
                .childChannelOption(ChannelOptions.allowRemoteHalfClosure, value: true)
                .childChannelInitializer { channel in
                    channel.pipeline.addHandler(LocalForwardHandler(client: client, forward: forward, localChannel: channel))
                }
                .bind(host: "127.0.0.1", port: forward.localPort)
                .get()
            runtimes[forward.id] = PortForwardRuntime(listener: listener, client: client)
        } catch {
            try? await client.close()
            throw PortForwardError.localPortUnavailable
        }
    }

    func stop(_ forward: PortForward) async {
        guard let runtime = runtimes.removeValue(forKey: forward.id) else { return }
        await runtime.stop()
    }

    func stopAll() async {
        let active = Array(runtimes.values)
        runtimes.removeAll()
        for runtime in active { await runtime.stop() }
    }
}
