// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "DevBoost",
    platforms: [.iOS(.v17)],
    products: [.library(name: "DevBoost", targets: ["DevBoost"])],
    dependencies: [
        .package(url: "https://github.com/orlandos-nl/Citadel.git", exact: "0.9.2"),
        .package(url: "https://github.com/migueldeicaza/SwiftTerm.git", exact: "1.20.0"),
        .package(url: "https://github.com/Joannis/swift-nio-ssh.git", exact: "0.3.4"),
        .package(url: "https://github.com/apple/swift-crypto.git", exact: "2.0.0"),
        .package(url: "https://github.com/apple/swift-nio.git", exact: "2.81.0")
    ],
    targets: [
        .target(name: "DevBoost", dependencies: [
            .product(name: "Citadel", package: "Citadel"),
            .product(name: "SwiftTerm", package: "SwiftTerm"),
            .product(name: "NIOSSH", package: "swift-nio-ssh"),
            .product(name: "Crypto", package: "swift-crypto"),
            .product(name: "NIO", package: "swift-nio")
        ], path: "Sources", sources: ["DevBoost", "Shared"]),
        .testTarget(name: "DevBoostTests", dependencies: ["DevBoost"], path: "Tests/DevBoostTests")
    ]
)
