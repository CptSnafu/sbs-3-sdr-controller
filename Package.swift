// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "SBS3Control",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "SBS3ControlCore", targets: ["SBS3ControlCore"]),
        .executable(name: "SBS3ControlMac", targets: ["SBS3ControlMac"]),
    ],
    targets: [
        .target(name: "SBS3ControlCore"),
        .executableTarget(name: "SBS3ControlMac", dependencies: ["SBS3ControlCore"]),
    ]
)
