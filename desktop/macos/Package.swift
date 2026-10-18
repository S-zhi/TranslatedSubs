// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "TranslateSub",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "TranslateSub", targets: ["TranslateSub"])],
    targets: [
        .target(name: "DesktopCore"),
        .executableTarget(name: "TranslateSub", dependencies: ["DesktopCore"]),
        .testTarget(name: "DesktopCoreTests", dependencies: ["DesktopCore"])
    ],
    swiftLanguageModes: [.v5]
)
