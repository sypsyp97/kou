// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Kou",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "Kou", targets: ["Kou"])],
    targets: [
        .target(name: "KouCore"),
        .executableTarget(name: "Kou", dependencies: ["KouCore"]),
        .testTarget(name: "KouCoreTests", dependencies: ["KouCore"])
    ]
)
