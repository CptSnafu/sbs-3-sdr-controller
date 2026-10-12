import Foundation

public struct SBS3Frame: Equatable, Sendable {
    public let payload: Data
    public let integrity: Data

    public init(payload: Data, integrity: Data? = nil) {
        self.payload = payload
        self.integrity = integrity ?? Self.xmodemCRC(payload)
    }

    public var wireData: Data {
        var result = Data([0x10, 0x02])
        for byte in payload {
            result.append(byte)
            if byte == 0x10 { result.append(0x10) }
        }
        result.append(contentsOf: [0x10, 0x03])
        result.append(integrity)
        return result
    }

    /// Kinetic's TCP API uses the same DLE frame, but also DLE-stuffs CRC bytes.
    public var networkWireData: Data {
        var result = Data([0x10, 0x02])
        for byte in payload {
            result.append(byte)
            if byte == 0x10 { result.append(0x10) }
        }
        result.append(contentsOf: [0x10, 0x03])
        for byte in integrity {
            result.append(byte)
            if byte == 0x10 { result.append(0x10) }
        }
        return result
    }

    public static func tunerConfiguration(
        tuner: Tuner,
        frequencyHz: UInt32,
        enabled: Bool,
        sequence: UInt8
    ) -> SBS3Frame {
        var payload = Data([
            0x54, sequence, 0x01, 0x00, 0x10, 0x00, tuner.rawValue, 0x00,
            0x03, 0x00, 0x0A, 0x00,
        ])
        withUnsafeBytes(of: frequencyHz.littleEndian) { payload.append(contentsOf: $0) }
        payload.append(contentsOf: [0x00, 0x12, 0x7A, 0x00, enabled ? 0xFF : 0x00, 0x01])
        return SBS3Frame(payload: payload)
    }

    public static func sdrConfiguration(
        receiver: UInt8,
        frequencyHz: UInt32,
        am: Bool,
        source: Tuner,
        squelch: UInt8,
        muted: Bool,
        sequence: UInt8
    ) -> SBS3Frame {
        precondition(receiver < 6, "SBS-3 has six SDR receivers")
        var payload = Data([
            0x54, sequence, 0x01, 0x00, 0x00, 0x00, receiver, 0x00,
            0x04, 0x00, 0x08, 0x00,
        ])
        withUnsafeBytes(of: frequencyHz.littleEndian) { payload.append(contentsOf: $0) }
        payload.append(contentsOf: [
            source.rawValue,
            am ? 0x01 : 0x00,
            squelch,
            muted ? 0x80 : 0x00,
        ])
        return SBS3Frame(payload: payload)
    }

    public static func sdrSquelchUpdate(receiver: UInt8, squelch: UInt8, sequence: UInt8) -> SBS3Frame {
        precondition(receiver < 6, "SBS-3 has six SDR receivers")
        return SBS3Frame(payload: Data([
            0x54, sequence, 0x01, 0x00, 0x06, 0x00, receiver, 0x00,
            0x04, 0x00, 0x01, 0x00, squelch,
        ]))
    }

    public static func mixerConfiguration(volumes: [UInt8], output: UInt8 = 0, sequence: UInt8) -> SBS3Frame {
        precondition(volumes.count == 6, "SBS-3 mixer needs six volumes")
        precondition(output < 2, "SBS-3 mixer output must be analog (0) or digital (1)")
        var payload = Data([
            0x54, sequence, 0x01, 0x00, 0x00, 0x00, output, 0x00,
            0x05, 0x00, 0x40, 0x00, 0x06, 0x00, 0x00, 0x03,
        ])
        func appendStereo(_ level: UInt8) {
            payload.append(contentsOf: [level, 0x00, level, 0x00])
        }
        appendStereo(100)
        payload.append(Data(repeating: 0, count: 12))
        appendStereo(volumes[0])
        for (index, level) in volumes.dropFirst().enumerated() {
            let receiver = UInt32(index + 1).littleEndian
            withUnsafeBytes(of: receiver) { payload.append(contentsOf: $0) }
            appendStereo(level)
        }
        return SBS3Frame(payload: payload)
    }

    /// Routes exactly one SDR to the SBS-3 digital output for an explicit
    /// diagnostic.  It leaves the analogue mixer and all tuner/SDR settings
    /// untouched.  The documented digital mode is stereo, 16-bit, 16 kHz with
    /// the output-enable bit set; all non-selected inputs are muted.
    public static func digitalAudioTestMixer(receiver: UInt8, sequence: UInt8) -> SBS3Frame {
        digitalAudioMixer(receiver: receiver, enabled: true, sequence: sequence)
    }

    /// Configures the single shared SBS-3 digital mixer. A selected SDR is
    /// routed to both digital output channels; disabling leaves every input
    /// muted and turns the output-enable bit off. The analogue mixer is never
    /// included in this frame.
    public static func digitalAudioMixer(receiver: UInt8?, enabled: Bool, sequence: UInt8) -> SBS3Frame {
        if let receiver { precondition(receiver < 6, "SBS-3 has six SDR receivers") }
        let selected = enabled ? receiver : nil
        let selectedIndex = selected.map { Int($0) }
        var payload = Data([
            0x54, sequence, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00,
            0x05, 0x00, 0x40, 0x00, 0x06, 0x00, enabled ? 0x83 : 0x03, 0x03,
        ])
        func appendStereo(_ level: UInt8, muted: Bool) {
            let flag: UInt8 = muted ? 0x80 : 0x00
            payload.append(contentsOf: [level, flag, level, flag])
        }
        appendStereo(100, muted: false) // Digital master L/R.
        payload.append(Data(repeating: 0, count: 12))
        for index in 0..<6 {
            let input = UInt32(index).littleEndian
            withUnsafeBytes(of: input) { payload.append(contentsOf: $0) }
            appendStereo(index == selectedIndex ? 100 : 0, muted: index != selectedIndex)
        }
        return SBS3Frame(payload: payload)
    }

    public static func xmodemCRC(_ input: Data) -> Data {
        var crc: UInt16 = 0
        for byte in input {
            crc ^= UInt16(byte) << 8
            for _ in 0..<8 {
                crc = (crc & 0x8000) != 0 ? (crc << 1) ^ 0x1021 : crc << 1
            }
        }
        return Data([UInt8(crc >> 8), UInt8(crc & 0xFF)])
    }
}

public enum Tuner: UInt8, CaseIterable, Identifiable, Sendable {
    case a = 0
    case b = 1

    public var id: UInt8 { rawValue }
    public var title: String { self == .a ? "Tuner A" : "Tuner B" }
}

public extension Data {
    var sbs3Hex: String { map { String(format: "%02X", $0) }.joined(separator: " ") }
}
