import AppKit
import AVFoundation
import Darwin
import Foundation
import SBS3ControlCore
import SwiftUI

private enum SBS3NetworkError: Error, LocalizedError, Sendable {
    case message(String)

    var errorDescription: String? {
        if case let .message(value) = self { return value }
        return nil
    }
}

private final class SBS3NetworkClient {
    static func send(
        frames: [SBS3Frame], sequences: [UInt8], host: String, port: UInt16,
        completion: @MainActor @escaping @Sendable (Result<String, SBS3NetworkError>) -> Void
    ) {
        DispatchQueue.global(qos: .userInitiated).async {
            let result: Result<String, SBS3NetworkError>
            do {
                result = .success(try exchange(frames: frames, sequences: sequences, host: host, port: port))
            } catch {
                result = .failure(.message(error.localizedDescription))
            }
            Task { @MainActor in completion(result) }
        }
    }

    private static func exchange(frames: [SBS3Frame], sequences: [UInt8], host: String, port: UInt16) throws -> String {
        guard frames.count == sequences.count else { throw SBS3NetworkError.message("Internal sequence mismatch.") }
        let socketFD = try openSocket(host: host, port: port)
        defer { Darwin.close(socketFD) }
        var receivedBytes = 0
        for (frame, sequence) in zip(frames, sequences) {
            try writeAll(socketFD, data: frame.networkWireData)
            receivedBytes += try receiveConfirmation(socketFD, sequence: sequence)
        }
        return "SBS-3 confirmed \(frames.count) command\(frames.count == 1 ? "" : "s") (\(receivedBytes) received byte\(receivedBytes == 1 ? "" : "s"))."
    }

    fileprivate static func openSocket(host: String, port: UInt16) throws -> Int32 {
        var hints = addrinfo(ai_flags: 0, ai_family: AF_UNSPEC, ai_socktype: SOCK_STREAM, ai_protocol: IPPROTO_TCP, ai_addrlen: 0, ai_canonname: nil, ai_addr: nil, ai_next: nil)
        var addresses: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host, String(port), &hints, &addresses) == 0, let first = addresses else {
            throw SBS3NetworkError.message("Could not resolve SBS-3 address \(host).")
        }
        defer { freeaddrinfo(first) }
        var candidate: UnsafeMutablePointer<addrinfo>? = first
        while let current = candidate {
            let item = current.pointee
            let fd = Darwin.socket(item.ai_family, item.ai_socktype, item.ai_protocol)
            if fd >= 0 {
                let flags = Darwin.fcntl(fd, F_GETFL)
                guard flags >= 0 else { Darwin.close(fd); candidate = item.ai_next; continue }
                _ = Darwin.fcntl(fd, F_SETFL, flags | O_NONBLOCK)
                var connected = Darwin.connect(fd, item.ai_addr, item.ai_addrlen) == 0
                if !connected, errno == EINPROGRESS {
                    var ready = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
                    if Darwin.poll(&ready, 1, 1_500) > 0 {
                        var socketError: Int32 = 0
                        var errorLength = socklen_t(MemoryLayout<Int32>.size)
                        connected = getsockopt(fd, SOL_SOCKET, SO_ERROR, &socketError, &errorLength) == 0 && socketError == 0
                    }
                }
                _ = Darwin.fcntl(fd, F_SETFL, flags)
                if connected {
                    // The SBS-3 can interleave periodic status frames before a
                    // command response.  Keep the socket open long enough to
                    // receive the matching response instead of treating the
                    // first quiet second as a failed command.
                    var timeout = timeval(tv_sec: 3, tv_usec: 0)
                    _ = withUnsafePointer(to: &timeout) {
                        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, $0, socklen_t(MemoryLayout<timeval>.size))
                    }
                    return fd
                }
                Darwin.close(fd)
            }
            candidate = item.ai_next
        }
        throw SBS3NetworkError.message("Could not connect to SBS-3 at \(host):\(port).")
    }

    fileprivate static func writeAll(_ socketFD: Int32, data: Data) throws {
        let bytes = [UInt8](data)
        var offset = 0
        while offset < bytes.count {
            let written = bytes.withUnsafeBytes { pointer in
                Darwin.send(socketFD, pointer.baseAddress!.advanced(by: offset), bytes.count - offset, 0)
            }
            guard written > 0 else { throw SBS3NetworkError.message("SBS-3 network write failed.") }
            offset += written
        }
    }

    private static func receiveConfirmation(_ socketFD: Int32, sequence: UInt8) throws -> Int {
        let deadline = Date().addingTimeInterval(3.5)
        var response = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while Date() < deadline {
            let read = Darwin.recv(socketFD, &buffer, buffer.count, 0)
            if read > 0 {
                response.append(contentsOf: buffer.prefix(Int(read)))
                if let result = confirmationResult(response, sequence: sequence) {
                    guard result == 0 else {
                        throw SBS3NetworkError.message(String(format: "SBS-3 rejected sequence 0x%02X (result 0x%02X).", sequence, result))
                    }
                    return response.count
                }
                continue
            }
            if read == 0 || errno == EAGAIN || errno == EWOULDBLOCK { break }
            throw SBS3NetworkError.message("SBS-3 network read failed.")
        }
        throw SBS3NetworkError.message(String(format: "SBS-3 did not confirm sequence 0x%02X.", sequence))
    }

    private static func confirmationResult(_ data: Data, sequence: UInt8) -> UInt8? {
        let bytes = [UInt8](data)
        var index = 0
        while index + 1 < bytes.count {
            guard bytes[index] == 0x10, bytes[index + 1] == 0x02 else { index += 1; continue }
            index += 2
            var payload: [UInt8] = []
            var complete = false
            while index + 1 < bytes.count {
                if bytes[index] == 0x10, bytes[index + 1] == 0x10 {
                    payload.append(0x10); index += 2
                } else if bytes[index] == 0x10, bytes[index + 1] == 0x03 {
                    index += 2; complete = true; break
                } else if bytes[index] == 0x10 {
                    break
                } else {
                    payload.append(bytes[index]); index += 1
                }
            }
            guard complete else { return nil }
            var crcBytes = 0
            while index < bytes.count, crcBytes < 2 {
                if bytes[index] == 0x10, index + 1 < bytes.count, bytes[index + 1] == 0x10 { index += 2 } else { index += 1 }
                crcBytes += 1
            }
            if payload.count >= 3, payload[0] == 0x20, payload[1] == sequence {
                return payload[2]
            }
        }
        return nil
    }
}

private struct SBS3NetworkFrame: Sendable {
    let payload: Data
    let integrity: Data
}

private struct SBS3NetworkFrameDecoder {
    private var buffer: [UInt8] = []

    mutating func append(_ data: Data) -> [SBS3NetworkFrame] {
        buffer.append(contentsOf: data)
        var frames: [SBS3NetworkFrame] = []
        while buffer.count >= 2 {
            guard buffer[0] == 0x10, buffer[1] == 0x02 else {
                buffer.removeFirst()
                continue
            }
            var index = 2
            var payload: [UInt8] = []
            var terminated = false
            while index < buffer.count {
                if buffer[index] != 0x10 {
                    payload.append(buffer[index]); index += 1; continue
                }
                guard index + 1 < buffer.count else { return frames }
                switch buffer[index + 1] {
                case 0x10: payload.append(0x10); index += 2
                case 0x03: index += 2; terminated = true
                default:
                    buffer.removeFirst()
                    index = 0
                }
                if terminated || index == 0 { break }
            }
            guard terminated else { break }
            var integrity: [UInt8] = []
            while integrity.count < 2 {
                guard index < buffer.count else { return frames }
                if buffer[index] == 0x10 {
                    guard index + 1 < buffer.count else { return frames }
                    guard buffer[index + 1] == 0x10 else { buffer.removeFirst(); integrity.removeAll(); break }
                    integrity.append(0x10); index += 2
                } else {
                    integrity.append(buffer[index]); index += 1
                }
            }
            guard integrity.count == 2 else { continue }
            frames.append(SBS3NetworkFrame(payload: Data(payload), integrity: Data(integrity)))
            buffer.removeFirst(index)
        }
        return frames
    }
}

private final class SBS3AcarsMonitor: @unchecked Sendable {
    private let lock = NSLock()
    private var socketFD: Int32 = -1
    private var stopped = false

    func start(
        host: String,
        port: UInt16,
        startupFrames: [SBS3Frame] = [SBS3Frame(payload: Data([0x17, 0x00]))],
        onFrame: @escaping @MainActor @Sendable (SBS3NetworkFrame) -> Void,
        onStopped: @escaping @MainActor @Sendable (String?) -> Void
    ) {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            var failure: String?
            do {
                let fd = try SBS3NetworkClient.openSocket(host: host, port: port)
                self.lock.lock()
                self.socketFD = fd
                let shouldStop = self.stopped
                self.lock.unlock()
                guard !shouldStop else { Darwin.close(fd); return }
                // The documented API login makes the SBS-3 expose its capability
                // packets and keeps a streaming session explicit.
                for (index, frame) in startupFrames.enumerated() {
                    try SBS3NetworkClient.writeAll(fd, data: frame.networkWireData)
                    // The SBS-3 needs time to process login and a mixer write
                    // before it accepts the following request on the same
                    // one-and-only TCP session.
                    if index < startupFrames.count - 1 {
                        Thread.sleep(forTimeInterval: index == 0 ? 0.7 : 0.25)
                    }
                }
                var decoder = SBS3NetworkFrameDecoder()
                var bytes = [UInt8](repeating: 0, count: 4096)
                while !self.isStopped {
                    let count = Darwin.recv(fd, &bytes, bytes.count, 0)
                    if count > 0 {
                        let frames = decoder.append(Data(bytes.prefix(Int(count))))
                        Task { @MainActor in frames.forEach(onFrame) }
                    } else if count == 0 {
                        throw SBS3NetworkError.message("SBS-3 ACARS connection was closed.")
                    } else if errno != EAGAIN && errno != EWOULDBLOCK {
                        throw SBS3NetworkError.message("SBS-3 ACARS network read failed.")
                    }
                }
            } catch {
                if !self.isStopped { failure = error.localizedDescription }
            }
            self.closeSocket()
            Task { @MainActor in onStopped(failure) }
        }
    }

    func stop() {
        lock.lock()
        stopped = true
        let fd = socketFD
        socketFD = -1
        lock.unlock()
        if fd >= 0 { Darwin.close(fd) }
    }

    func send(_ frame: SBS3Frame) throws {
        lock.lock()
        let fd = socketFD
        let isStopped = stopped
        lock.unlock()
        guard !isStopped, fd >= 0 else {
            throw SBS3NetworkError.message("SBS-3 audio session is not active.")
        }
        try SBS3NetworkClient.writeAll(fd, data: frame.networkWireData)
    }

    private var isStopped: Bool {
        lock.lock(); defer { lock.unlock() }
        return stopped
    }

    private func closeSocket() {
        lock.lock()
        let fd = socketFD
        socketFD = -1
        lock.unlock()
        if fd >= 0 { Darwin.close(fd) }
    }
}

@MainActor
private final class SBS3PCMPlayer {
    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private let channels: AVAudioChannelCount
    private let inputBitDepth: Int
    private let duplicateLeftChannel: Bool
    private let format: AVAudioFormat
    private var pendingAudio = Data()
    private var queuedChunks = 0
    private var playbackStarted = false

    init(stereo: Bool, sampleRate: Double, bitDepth: Int, duplicateLeftChannel: Bool = false) {
        channels = stereo ? 2 : 1
        inputBitDepth = bitDepth
        self.duplicateLeftChannel = duplicateLeftChannel
        format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: channels, interleaved: true)!
        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: format)
    }

    func start() throws {
        if !engine.isRunning { try engine.start() }
    }

    func enqueue(_ bytes: Data) {
        let bytesPerFrame = Int(channels) * MemoryLayout<Int16>.size
        let pcm: Data
        if inputBitDepth == 8 {
            var widened = Data(capacity: bytes.count * 2)
            for value in bytes {
                var sample = Int16(Int8(bitPattern: value)) << 8
                withUnsafeBytes(of: &sample) { widened.append(contentsOf: $0) }
            }
            pcm = widened
        } else {
            // SBS-3 network data fields use most-significant byte first;
            // AVAudioEngine's Int16 PCM format is native-endian on macOS.
            // Convert each signed 16-bit network sample to native storage.
            guard bytes.count.isMultiple(of: 2) else { return }
            var nativePCM = Data(capacity: bytes.count)
            let source = [UInt8](bytes)
            if duplicateLeftChannel, channels == 2 {
                // SBS-3 digital output carries the selected monophonic SDR in
                // its left PCM lane while the right lane is silent. Duplicate
                // it locally so both Mac speakers carry the same receiver.
                guard source.count.isMultiple(of: 4) else { return }
                for offset in stride(from: 0, to: source.count, by: 4) {
                    var sample = UInt16(source[offset]) << 8 | UInt16(source[offset + 1])
                    withUnsafeBytes(of: &sample) { nativePCM.append(contentsOf: $0) }
                    withUnsafeBytes(of: &sample) { nativePCM.append(contentsOf: $0) }
                }
            } else {
                for offset in stride(from: 0, to: source.count, by: 2) {
                    var sample = UInt16(source[offset]) << 8 | UInt16(source[offset + 1])
                    withUnsafeBytes(of: &sample) { nativePCM.append(contentsOf: $0) }
                }
            }
            pcm = nativePCM
        }
        guard pcm.count.isMultiple(of: bytesPerFrame) else { return }
        pendingAudio.append(pcm)
        // Network packets are only a few milliseconds long. Scheduling those
        // individually makes the player node under-run between arrivals, so
        // retain a short packet-aligned pre-roll before starting playback.
        let chunkBytes = max(2_048, bytesPerFrame * 64)
        while pendingAudio.count >= chunkBytes {
            let chunk = Data(pendingAudio.prefix(chunkBytes))
            pendingAudio.removeFirst(chunkBytes)
            schedule(chunk)
            queuedChunks += 1
        }
        if !playbackStarted, queuedChunks >= 3 {
            node.play()
            playbackStarted = true
        }
    }

    private func schedule(_ pcm: Data) {
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(pcm.count / (Int(channels) * MemoryLayout<Int16>.size))) else { return }
        buffer.frameLength = buffer.frameCapacity
        _ = pcm.withUnsafeBytes { source in
            memcpy(buffer.audioBufferList.pointee.mBuffers.mData!, source.baseAddress!, pcm.count)
        }
        node.scheduleBuffer(buffer)
    }

    func stop() {
        node.stop()
        engine.stop()
        pendingAudio.removeAll(keepingCapacity: false)
        queuedChunks = 0
        playbackStarted = false
    }
}

private struct SBS3DigitalAudioConfig {
    let stereo: Bool
    let bitDepth: Int
    let sampleRate: Double
    let outputEnabled: Bool

    var description: String {
        "\(stereo ? "stereo" : "mono"), \(bitDepth)-bit, \(Int(sampleRate)) Hz"
    }

    static func fromRegisterResponse(_ frame: SBS3NetworkFrame, tag: UInt8) -> SBS3DigitalAudioConfig? {
        let bytes = [UInt8](frame.payload)
        guard bytes.count >= 14, bytes[0] == 0x45, bytes[1] == tag,
              bytes[4] == 0x02, bytes[6] == 0x01, bytes[8] == 0x05 else { return nil }
        let mode = bytes[12]
        let rate: Double
        switch bytes[13] {
        case 1: rate = 48_000
        case 2: rate = 24_000
        case 3: rate = 16_000
        case 6: rate = 8_000
        default: return nil
        }
        return SBS3DigitalAudioConfig(stereo: mode & 0x01 != 0, bitDepth: mode & 0x02 != 0 ? 16 : 8, sampleRate: rate, outputEnabled: mode & 0x80 != 0)
    }
}

@MainActor
final class SBS3Controller: NSObject {
    private let hostField = NSTextField(string: "192.168.1.170")
    private let portField = NSTextField(string: "10001")
    private let statusLabel = NSTextField(labelWithString: "Enter the SBS-3 address, then send a tuner or SDR setting.")
    private var sequence: UInt8 = 0x4A
    private var tunerFrequencyFields: [NSTextField] = []
    private var tunerButtons: [NSButton] = []
    private var tunerEnabled = [true, false]
    private var sdrControls: [SDRControls] = []
    private var sending = false

    func makeContentView() -> NSView {
        loadState()
        let container = MacControlContentView()
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: container.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.edgeInsets = NSEdgeInsets(top: 16, left: 18, bottom: 18, right: 18)
        scroll.documentView = root
        root.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor).isActive = true

        let header = NSStackView(views: [titleLabel("SBS-3", size: 22), titleLabel("SDR CONTROL", size: 11)])
        header.orientation = .horizontal
        header.spacing = 10
        let network = NSStackView(views: [NSTextField(labelWithString: "SBS-3 Network"), hostField, NSTextField(labelWithString: "Port"), portField])
        network.orientation = .horizontal
        network.spacing = 6
        hostField.widthAnchor.constraint(equalToConstant: 150).isActive = true
        portField.widthAnchor.constraint(equalToConstant: 65).isActive = true
        let top = NSStackView(views: [header, NSView(), network])
        top.orientation = .horizontal
        top.alignment = .centerY
        top.widthAnchor.constraint(equalToConstant: 894).isActive = true
        root.addArrangedSubview(top)

        statusLabel.textColor = .secondaryLabelColor
        statusLabel.font = .systemFont(ofSize: 12)
        statusLabel.widthAnchor.constraint(equalToConstant: 894).isActive = true
        root.addArrangedSubview(statusLabel)

        let tunerRow = NSStackView(views: [makeTunerCard(index: 0, title: "Tuner A"), makeTunerCard(index: 1, title: "Tuner B")])
        tunerRow.orientation = .horizontal
        tunerRow.distribution = .fillEqually
        tunerRow.spacing = 12
        tunerRow.widthAnchor.constraint(equalToConstant: 894).isActive = true
        root.addArrangedSubview(tunerRow)
        root.addArrangedSubview(sectionLabel("RECEIVERS"))
        for receiver in 0..<6 { root.addArrangedSubview(makeSDRCard(receiver: receiver)) }
        return container
    }

    private func makeTunerCard(index: Int, title: String) -> NSView {
        let card = NSBox()
        card.boxType = .custom
        card.cornerRadius = 8
        card.borderColor = .separatorColor
        card.fillColor = .controlBackgroundColor
        let field = NSTextField(string: index == 0 ? "125.000" : "123.800")
        field.alignment = .right
        field.font = .monospacedDigitSystemFont(ofSize: 20, weight: .semibold)
        field.widthAnchor.constraint(equalToConstant: 170).isActive = true
        field.tag = index
        field.target = self
        field.action = #selector(tunerFieldSubmitted(_:))
        tunerFrequencyFields.append(field)
        let state = NSButton(title: index == 0 ? "ON" : "OFF", target: self, action: #selector(tunerButtonPressed(_:)))
        state.bezelStyle = .rounded
        state.tag = index
        state.widthAnchor.constraint(equalToConstant: 70).isActive = true
        tunerButtons.append(state)
        let frequency = horizontal([field, NSTextField(labelWithString: "MHz"), state])
        frequency.alignment = .centerY
        frequency.spacing = 8
        let stack = NSStackView(views: [titleLabel(title, size: 14), frequency])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 12, left: 14, bottom: 12, right: 14)
        card.contentView = stack
        return card
    }

    private func makeSDRCard(receiver: Int) -> NSView {
        let card = NSBox()
        card.boxType = .custom
        card.cornerRadius = 7
        card.borderColor = .separatorColor
        card.fillColor = .controlBackgroundColor
        let controls = SDRControls(receiver: receiver)
        sdrControls.append(controls)
        controls.frequency.font = .monospacedDigitSystemFont(ofSize: 17, weight: .semibold)
        controls.frequency.alignment = .right
        controls.frequency.widthAnchor.constraint(equalToConstant: 135).isActive = true
        controls.frequency.target = self
        controls.frequency.action = #selector(sdrFieldSubmitted(_:))
        controls.frequency.tag = receiver
        controls.source.addItems(withTitles: ["Tuner A", "Tuner B"])
        controls.mode.addItems(withTitles: ["AM", "FM"])
        for control in [controls.source, controls.mode, controls.squelch, controls.volume] {
            control.target = self
            control.action = #selector(sdrControlChanged(_:))
            control.tag = receiver
        }
        controls.mute.target = self
        controls.mute.action = #selector(mutePressed(_:))
        controls.mute.tag = receiver
        controls.send.target = self
        controls.send.action = #selector(sendPressed(_:))
        controls.send.tag = receiver
        controls.squelchLabel.stringValue = "SQ \(Int(controls.squelch.integerValue))%"
        controls.volumeLabel.stringValue = "Vol \(Int(controls.volume.integerValue))%"
        let receiverLabel = titleLabel("SDR \(receiver + 1)", size: 13)
        receiverLabel.widthAnchor.constraint(equalToConstant: 52).isActive = true
        let source = vertical([smallLabel("TUNER"), controls.source])
        let mode = vertical([smallLabel("MODE"), controls.mode])
        let frequency = vertical([smallLabel("FREQUENCY"), horizontal([controls.frequency, smallLabel("MHz")])])
        let squelch = vertical([controls.squelchLabel, controls.squelch])
        let volume = vertical([controls.volumeLabel, controls.volume])
        let actions = vertical([controls.mute, controls.send])
        actions.spacing = 5
        let row = horizontal([receiverLabel, source, mode, frequency, squelch, volume, actions])
        row.alignment = .centerY
        row.spacing = 12
        row.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 8, right: 12)
        card.contentView = row
        card.widthAnchor.constraint(equalToConstant: 894).isActive = true
        return card
    }

    @objc private func tunerButtonPressed(_ sender: NSButton) {
        let index = sender.tag
        tunerEnabled[index].toggle()
        sender.title = tunerEnabled[index] ? "ON" : "OFF"
        sendTuner(index)
    }

    @objc private func tunerFieldSubmitted(_ sender: NSTextField) { sendTuner(sender.tag) }
    @objc private func sendPressed(_ sender: NSButton) { sendSDR(sender.tag) }
    @objc private func mutePressed(_ sender: NSButton) {
        let controls = sdrControls[sender.tag]
        controls.muted.toggle()
        sender.title = controls.muted ? "MUTE" : "SIGNAL"
        sendSDR(sender.tag)
    }

    @objc private func sdrFieldSubmitted(_ sender: NSTextField) { sendSDR(sender.tag) }
    @objc private func sdrControlChanged(_ sender: NSControl) {
        let controls = sdrControls[sender.tag]
        controls.squelchLabel.stringValue = "SQ \(Int(controls.squelch.integerValue))%"
        controls.volumeLabel.stringValue = "Vol \(Int(controls.volume.integerValue))%"
        sendSDR(sender.tag)
    }

    private func sendTuner(_ index: Int) {
        guard let hertz = hertz(from: tunerFrequencyFields[index].stringValue) else { status("Enter a valid tuner frequency.", failure: true); return }
        let frame = SBS3Frame.tunerConfiguration(tuner: index == 0 ? .a : .b, frequencyHz: hertz, enabled: tunerEnabled[index], sequence: sequence)
        send(frames: [frame], count: 1)
    }

    private func sendSDR(_ receiver: Int) {
        guard let hertz = hertz(from: sdrControls[receiver].frequency.stringValue) else { status("Enter a valid SDR frequency.", failure: true); return }
        let controls = sdrControls[receiver]
        let source: Tuner = controls.source.indexOfSelectedItem == 0 ? .a : .b
        let frame = SBS3Frame.sdrConfiguration(receiver: UInt8(receiver), frequencyHz: hertz, am: controls.mode.indexOfSelectedItem == 0, source: source, squelch: UInt8(controls.squelch.integerValue), muted: controls.muted, sequence: sequence)
        let squelch = SBS3Frame.sdrSquelchUpdate(receiver: UInt8(receiver), squelch: UInt8(controls.squelch.integerValue), sequence: sequence &+ 1)
        let mixer = SBS3Frame.mixerConfiguration(volumes: sdrControls.map { UInt8($0.volume.integerValue) }, sequence: sequence &+ 2)
        send(frames: [frame, squelch, mixer], count: 3)
    }

    private func send(frames: [SBS3Frame], count: UInt8) {
        guard !sending else { status("A command is already being sent. Please wait.", failure: true); return }
        let host = hostField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty, let port = UInt16(portField.stringValue) else { status("Enter a valid SBS-3 address and TCP port.", failure: true); return }
        sending = true
        status("Sending to \(host):\(port)…", failure: false)
        let sequences = (0..<Int(count)).map { sequence &+ UInt8($0) }
        SBS3NetworkClient.send(frames: frames, sequences: sequences, host: host, port: port) { [weak self] result in
            guard let self else { return }
            self.sending = false
            switch result {
            case let .success(message):
                self.sequence &+= count
                self.saveState()
                self.status(message, failure: false)
            case let .failure(error): self.status(error.localizedDescription, failure: true)
            }
        }
    }

    private func hertz(from value: String) -> UInt32? {
        let normalized = value.replacingOccurrences(of: ",", with: ".")
        guard let mhz = Double(normalized), mhz >= 0, mhz <= 4_294.967295 else { return nil }
        return UInt32((mhz * 1_000_000).rounded())
    }

    private func status(_ text: String, failure: Bool) {
        statusLabel.stringValue = text
        statusLabel.textColor = failure ? .systemRed : .secondaryLabelColor
    }

    private func loadState() {
        let defaults = UserDefaults.standard
        hostField.stringValue = defaults.string(forKey: "sbs3.host") ?? hostField.stringValue
        portField.stringValue = defaults.string(forKey: "sbs3.port") ?? portField.stringValue
        if let stored = defaults.object(forKey: "sbs3.sequence") as? Int { sequence = UInt8(truncatingIfNeeded: stored) }
    }

    private func saveState() {
        let defaults = UserDefaults.standard
        defaults.set(hostField.stringValue, forKey: "sbs3.host")
        defaults.set(portField.stringValue, forKey: "sbs3.port")
        defaults.set(Int(sequence), forKey: "sbs3.sequence")
    }

    private func titleLabel(_ text: String, size: CGFloat) -> NSTextField {
        let value = NSTextField(labelWithString: text)
        value.font = .systemFont(ofSize: size, weight: .bold)
        value.textColor = size > 15 ? .labelColor : .systemBlue
        return value
    }

    private func sectionLabel(_ text: String) -> NSTextField {
        let value = NSTextField(labelWithString: text)
        value.font = .systemFont(ofSize: 11, weight: .bold)
        value.textColor = .secondaryLabelColor
        return value
    }

    private func smallLabel(_ text: String) -> NSTextField {
        let value = NSTextField(labelWithString: text)
        value.font = .systemFont(ofSize: 9, weight: .semibold)
        value.textColor = .secondaryLabelColor
        return value
    }

    private func horizontal(_ views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.spacing = 5
        return stack
    }

    private func vertical(_ views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 3
        return stack
    }
}

@main
struct SBS3ControlMacApp: App {
    @StateObject private var model = MacControlModel()

    var body: some Scene {
        WindowGroup("SBS-3 SDR Control") {
            NativeControlView()
                .environmentObject(model)
                .frame(minWidth: 930, minHeight: 810)
        }
        .defaultSize(width: 930, height: 810)
        .windowResizability(.contentMinSize)

        Window("SBS-3 Device Log", id: "device-log") {
            DeviceLogView()
                .environmentObject(model)
                .onDisappear { model.setPanelOpen("device-log", open: false) }
        }
        .defaultSize(width: 760, height: 460)

        Window("SBS-3 Admin", id: "admin") {
            PresetAdminView()
                .environmentObject(model)
                .onDisappear { model.setPanelOpen("admin", open: false) }
        }
        .defaultSize(width: 560, height: 480)

        Window("SBS-3 ACARS Messages", id: "acars") {
            AcarsView()
                .environmentObject(model)
                .onDisappear { model.setPanelOpen("acars", open: false) }
        }
        .defaultSize(width: 980, height: 480)

    }
}

private struct NativeControlView: View {
    @EnvironmentObject private var model: MacControlModel
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismissWindow) private var dismissWindow

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 10) {
                    Text("SBS-3").font(.system(size: 24, weight: .bold))
                    Text("SDR CONTROL").font(.system(size: 12, weight: .semibold)).foregroundStyle(.blue)
                    Spacer()
                    panelButton("✈ ACARS", id: "acars")
                    panelButton("▤ Device Log", id: "device-log")
                    panelButton("⚙ Admin", id: "admin")
                }

                Text(model.status)
                    .font(.system(size: 12))
                    .foregroundStyle(model.failure ? .red : .secondary)

                HStack(spacing: 8) {
                    Text("SBS-3 Network").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
                    TextField("Address", text: $model.host).frame(width: 165)
                    Text("Port").font(.system(size: 12)).foregroundStyle(.secondary)
                    TextField("Port", text: $model.port).frame(width: 62)
                    Button("NW Check") { model.checkNetwork() }
                        .disabled(model.networkChecking)
                    Spacer(minLength: 0)
                }

                HStack(spacing: 14) {
                    tunerCard(index: 0, name: "Tuner A")
                    tunerCard(index: 1, name: "Tuner B")
                }

                Text("RECEIVERS").font(.system(size: 11, weight: .bold)).foregroundStyle(.secondary)
                ForEach($model.receivers) { $receiver in
                    ReceiverCard(
                        receiver: $receiver,
                        presets: model.presets,
                        onSend: { model.sendSDR(receiver.id) },
                        onMute: { model.toggleMute(receiver.id) },
                        onDigitalOutput: { model.toggleDigitalOutput(receiver.id) },
                        digitalOutputTitle: model.digitalOutputTitle(for: receiver.id),
                        digitalOutputAvailable: model.digitalOutputAvailable(for: receiver.id),
                        onLevelChanged: { model.sendSDR(receiver.id) },
                        onPreset: { model.selectReceiverPreset(receiver.id, presetID: $0) },
                        onValuesChanged: { model.controlValuesChanged() }
                    )
                }
            }
            .padding(18)
            .frame(minWidth: 894, alignment: .leading)
        }
        .frame(minWidth: 930, minHeight: 810)
    }

    private func panelButton(_ title: String, id: String) -> some View {
        Button(title) {
            if model.isPanelOpen(id) {
                dismissWindow(id: id)
                model.setPanelOpen(id, open: false)
            } else {
                openWindow(id: id)
                model.setPanelOpen(id, open: true)
            }
        }
    }

    @ViewBuilder
    private func tunerCard(index: Int, name: String) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    TextField("Frequency", text: model.tunerFrequencyBinding(index))
                        .font(.system(size: 20, weight: .semibold, design: .monospaced))
                        .foregroundStyle(model.tunerEnabled[index] ? .green : .secondary)
                        .multilineTextAlignment(.trailing)
                        .frame(width: 170)
                        .onSubmit { model.sendTuner(index) }
                    Text("MHz")
                    Button(model.tunerEnabled[index] ? "ON" : "OFF") { model.toggleTuner(index) }
                        .frame(width: 68)
                }
                Picker("Preset", selection: model.tunerPresetBinding(index)) {
                    Text("Frequency preset").tag(UUID?.none)
                    ForEach(model.presets) { preset in
                        Text(preset.label).tag(Optional(preset.id))
                    }
                }
                .labelsHidden()
                .frame(width: 245)
            }
        } label: {
            Text(name)
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(model.tunerEnabled[index] ? .green : .secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ReceiverCard: View {
    @Binding var receiver: MacReceiver
    let presets: [FrequencyPreset]
    let onSend: () -> Void
    let onMute: () -> Void
    let onDigitalOutput: () -> Void
    let digitalOutputTitle: String
    let digitalOutputAvailable: Bool
    let onLevelChanged: () -> Void
    let onPreset: (UUID) -> Void
    let onValuesChanged: () -> Void

    var body: some View {
        GroupBox {
            HStack(alignment: .center, spacing: 14) {
                Text("SDR \(receiver.id + 1)")
                    .font(.system(size: 13, weight: .bold))
                    .frame(width: 48, alignment: .leading)
                VStack(alignment: .leading, spacing: 3) {
                    Text("TUNER").font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                    Picker("Tuner", selection: $receiver.source) {
                        Text("A").tag(0)
                        Text("B").tag(1)
                    }
                    .labelsHidden().pickerStyle(.segmented).frame(width: 75)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("MODE").font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                    Picker("Mode", selection: $receiver.mode) {
                        Text("AM").tag(0)
                        Text("FM").tag(1)
                    }
                    .labelsHidden().pickerStyle(.segmented).frame(width: 84)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("FREQUENCY").font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                    HStack(spacing: 5) {
                        TextField("Frequency", text: $receiver.frequency)
                            .font(.system(size: 17, weight: .semibold, design: .monospaced))
                            .multilineTextAlignment(.trailing)
                            .frame(width: 135)
                            .onSubmit { onSend() }
                        Text("MHz").font(.system(size: 11))
                    }
                    Picker("Preset", selection: Binding(
                        get: { receiver.presetID },
                        set: { selected in
                            receiver.presetID = selected
                            if let selected { onPreset(selected) }
                        }
                    )) {
                        Text("Preset").tag(UUID?.none)
                        ForEach(presets) { preset in
                            Text(preset.label).tag(Optional(preset.id))
                        }
                    }
                    .labelsHidden()
                    .frame(width: 180)
                }
                .frame(width: 180, alignment: .leading)
                VStack(alignment: .leading, spacing: 3) {
                    Text("SQ \(receiver.squelch)%").font(.system(size: 11, weight: .semibold))
                    Slider(value: percentageBinding(\.squelch), in: 0...100, step: 1, onEditingChanged: { if !$0 { onLevelChanged() } })
                        .frame(width: 105)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("Vol \(receiver.volume)%").font(.system(size: 11, weight: .semibold))
                    Slider(value: percentageBinding(\.volume), in: 0...100, step: 1, onEditingChanged: { if !$0 { onLevelChanged() } })
                        .frame(width: 105)
                }
                VStack(spacing: 5) {
                    HStack(spacing: 5) {
                        Button(receiver.muted ? "MUTE" : "SIGNAL", action: onMute)
                            .frame(width: 72)
                            .foregroundStyle(receiver.muted ? .red : .green)
                        Button(digitalOutputTitle, action: onDigitalOutput)
                            .frame(width: 72)
                            .foregroundStyle(digitalOutputTitle == "STOP" ? .blue : .secondary)
                            .disabled(!digitalOutputAvailable)
                    }
                    Button("Send", action: onSend)
                }
            }
        }
        .onChange(of: receiver) { _, _ in onValuesChanged() }
    }

    private func percentageBinding(_ keyPath: WritableKeyPath<MacReceiver, Int>) -> Binding<Double> {
        Binding(
            get: { Double(receiver[keyPath: keyPath]) },
            set: { receiver[keyPath: keyPath] = Int($0.rounded()) }
        )
    }
}

private struct MacReceiver: Identifiable, Codable, Equatable {
    let id: Int
    var source = 0
    var mode = 0
    var frequency: String
    var squelch = 75
    var volume = 75
    var muted = false
    var presetID: UUID?
}

private struct FrequencyPreset: Identifiable, Codable, Equatable {
    let id: UUID
    var name: String
    var frequency: String

    init(id: UUID = UUID(), name: String, frequency: String) {
        self.id = id
        self.name = name
        self.frequency = frequency
    }

    var label: String { "\(name) — \(frequency) MHz" }
}

private struct MacAcarsRecord: Identifiable {
    let id = UUID()
    let timestamp: Date
    let receiver: Int
    let mode: String
    let registration: String
    let sequence: String
    let flight: String
    let message: String
}

private struct AcarsView: View {
    @EnvironmentObject private var model: MacControlModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("ACARS MESSAGES").font(.headline).foregroundStyle(.blue)
                Text(model.acarsStatus).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button(model.acarsListening ? "Stop listening" : "Start listening") {
                    model.toggleAcarsMonitoring()
                }
                Button("Clear") { model.clearAcars() }
            }
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 6) {
                GridRow {
                    header("Time", width: 62); header("Source", width: 130); header("Mode", width: 44)
                    header("Registration", width: 98); header("Sequence", width: 76); header("Flight", width: 82)
                    header("Message text", width: 250)
                }
                Divider().gridCellColumns(7)
                ForEach(model.acarsRecords) { record in
                    GridRow {
                        cell(timeString(record.timestamp), width: 62); cell("SDR \(record.receiver + 1)", width: 130)
                        cell(record.mode, width: 44); cell(record.registration, width: 98); cell(record.sequence, width: 76)
                        cell(record.flight, width: 82); cell(record.message, width: 250)
                    }
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(Color.black.opacity(0.78))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .padding(18)
        .frame(minWidth: 900, minHeight: 390)
    }

    private func header(_ text: String, width: CGFloat) -> some View {
        Text(text).font(.system(size: 10, weight: .bold)).foregroundStyle(.yellow).frame(width: width, alignment: .leading)
    }

    private func cell(_ text: String, width: CGFloat) -> some View {
        Text(text).font(.system(size: 11, design: .monospaced)).foregroundStyle(.green).lineLimit(1).frame(width: width, alignment: .leading)
    }

    private func timeString(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: date)
    }
}

private struct DeviceLogView: View {
    @EnvironmentObject private var model: MacControlModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("DEVICE LOG").font(.headline).foregroundStyle(.blue)
                Spacer()
                Button("Clear") { model.clearLog() }
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 5) {
                    ForEach(model.logEntries) { entry in
                        Text(entry.text)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(entry.isError ? .red : .green)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                    }
                }
                .padding(12)
            }
            .background(Color.black.opacity(0.78))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .padding(18)
        .frame(minWidth: 620, minHeight: 340)
    }
}

private struct PresetAdminView: View {
    @EnvironmentObject private var model: MacControlModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Frequency presets").font(.title3.weight(.bold))
            Text("Presets are available at both tuners and all six SDR receivers.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                TextField("Name", text: $model.presetName)
                TextField("MHz", text: $model.presetFrequency).frame(width: 100)
                Button("Add / update") {
                    model.addEnteredPreset()
                }
            }
            List {
                ForEach(model.presets) { preset in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(preset.name)
                            Text("\(preset.frequency) MHz").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button(role: .destructive) { model.deletePreset(preset.id) } label: {
                            Image(systemName: "trash")
                        }
                        .buttonStyle(.borderless)
                    }
                }
            }
            Text(model.presetMessage)
                .font(.caption)
                .foregroundStyle(model.presetError ? .red : .secondary)
            Divider()
            VStack(alignment: .leading, spacing: 6) {
                Text("SBS-3 audio recovery").font(.headline)
                Text("Restores the known BaseStation analog and digital mixer setup. It does not change tuner or SDR settings.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Restore SBS-3 Audio Mixer") {
                    model.restoreAudioMixer()
                }
            }
        }
        .padding(18)
        .frame(minWidth: 500, minHeight: 420)
    }
}

private final class MacControlContentView: NSView {
    override var intrinsicContentSize: NSSize {
        NSSize(width: 930, height: 760)
    }
}

private struct DeviceLogEntry: Identifiable {
    let id = UUID()
    let text: String
    let isError: Bool
}

@MainActor
private final class MacControlModel: ObservableObject {
    @Published var host = UserDefaults.standard.string(forKey: "sbs3.host") ?? "192.168.1.170"
    @Published var port = UserDefaults.standard.string(forKey: "sbs3.port") ?? "10001"
    @Published var tunerFrequencies = ["125.000", "123.800"]
    // The SBS-3 status frames do not yet provide a confirmed tuner on/off flag.
    // Start conservatively: the UI must never claim that a tuner is active until
    // this app has received a successful confirmation for its own ON command.
    @Published var tunerEnabled = [false, false]
    @Published var receivers = [
        MacReceiver(id: 0, frequency: "119.400"), MacReceiver(id: 1, frequency: "123.800"),
        MacReceiver(id: 2, frequency: "121.600"), MacReceiver(id: 3, frequency: "121.775"),
        MacReceiver(id: 4, frequency: "134.675"), MacReceiver(id: 5, frequency: "125.175"),
    ]
    @Published var status = "Enter the SBS-3 address, then send a tuner or SDR setting."
    @Published var failure = false
    @Published var presets: [FrequencyPreset] = []
    @Published var tunerPresetIDs: [UUID?] = [nil, nil]
    @Published var logEntries: [DeviceLogEntry] = []
    @Published var presetMessage = "Add a name and a valid MHz frequency."
    @Published var presetError = false
    @Published var presetName = ""
    @Published var presetFrequency = ""
    @Published var acarsRecords: [MacAcarsRecord] = []
    @Published var acarsListening = false
    @Published var acarsStatus = "Not listening"
    @Published var networkChecking = false
    @Published var directAudioPlaying = false
    @Published var directAudioRouteEnabled = false
    @Published var directAudioReceiver = 0
    @Published var directAudioStatus = "Stopped"
    @Published var directAudioError = false
    private var sequence: UInt8 = UInt8(truncatingIfNeeded: UserDefaults.standard.integer(forKey: "sbs3.sequence"))
    private var sending = false
    private var acarsMonitor: SBS3AcarsMonitor?
    private var audioMonitor: SBS3AcarsMonitor?
    private var pcmPlayer: SBS3PCMPlayer?
    private var lastDirectAudioStatusUpdate = Date.distantPast
    private var pendingDirectAudioDisableSequence: UInt8?
    private var openPanelIDs = Set<String>()

    init() {
        if sequence == 0 { sequence = 0x4A }
        presets = Self.loadPresets()
        let stored = Self.loadControlState()
        tunerFrequencies = stored.tunerFrequencies
        tunerEnabled = stored.tunerEnabled
        receivers = stored.receivers
        appendLog("Ready. Network target \(host):\(port).", error: false)
    }

    func tunerFrequencyBinding(_ index: Int) -> Binding<String> {
        Binding(get: { self.tunerFrequencies[index] }, set: {
            self.tunerFrequencies[index] = $0
            self.saveControlState()
        })
    }

    /// Opens and immediately closes a TCP connection.  This deliberately
    /// sends no SBS-3 protocol data, so it is safe for firewall testing.
    func checkNetwork() {
        guard !networkChecking else { return }
        let address = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !address.isEmpty, let networkPort = UInt16(port) else {
            setStatus("Enter a valid SBS-3 address and TCP port.", failure: true)
            return
        }
        networkChecking = true
        setStatus("Checking TCP connection to \(address):\(networkPort)…", failure: false)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result: Result<Void, Error>
            do {
                let socketFD = try SBS3NetworkClient.openSocket(host: address, port: networkPort)
                Darwin.close(socketFD)
                result = .success(())
            } catch {
                result = .failure(error)
            }
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.networkChecking = false
                switch result {
                case .success:
                    let message = "Network reachable: \(address):\(networkPort). No SBS-3 data was sent."
                    self.setStatus(message, failure: false)
                    self.appendLog(message, error: false)
                case let .failure(error):
                    self.setStatus(error.localizedDescription, failure: true)
                    self.appendLog("NW Check error: \(error.localizedDescription)", error: true)
                }
            }
        }
    }

    func tunerPresetBinding(_ index: Int) -> Binding<UUID?> {
        Binding(
            get: { self.tunerPresetIDs[index] },
            set: { selected in
                self.tunerPresetIDs[index] = selected
                guard let selected, let preset = self.presets.first(where: { $0.id == selected }) else { return }
                self.tunerFrequencies[index] = preset.frequency
                self.saveControlState()
                self.sendTuner(index)
                self.tunerPresetIDs[index] = nil
            }
        )
    }

    func selectReceiverPreset(_ index: Int, presetID: UUID) {
        guard let preset = presets.first(where: { $0.id == presetID }) else { return }
        receivers[index].frequency = preset.frequency
        saveControlState()
        sendSDR(index)
    }

    func addPreset(name: String, frequency: String) -> Bool {
        let normalized = frequency.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: ",", with: ".")
        guard !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let value = Double(normalized), value >= 0, value <= 4_294.967_295 else {
            presetMessage = "Enter a name and a valid MHz frequency."
            presetError = true
            return false
        }
        let cleanName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let displayFrequency = String(format: "%.3f", value)
        presets.removeAll { $0.name.caseInsensitiveCompare(cleanName) == .orderedSame }
        presets.append(FrequencyPreset(name: cleanName, frequency: displayFrequency))
        savePresets()
        presetMessage = "Saved \(cleanName) at \(displayFrequency) MHz."
        presetError = false
        appendLog("Preset saved: \(cleanName) — \(displayFrequency) MHz.", error: false)
        return true
    }

    func addEnteredPreset() {
        if addPreset(name: presetName, frequency: presetFrequency) {
            presetName = ""
            presetFrequency = ""
        }
    }

    func deletePreset(_ id: UUID) {
        guard let preset = presets.first(where: { $0.id == id }) else { return }
        presets.removeAll { $0.id == id }
        for index in tunerPresetIDs.indices where tunerPresetIDs[index] == id { tunerPresetIDs[index] = nil }
        for index in receivers.indices where receivers[index].presetID == id { receivers[index].presetID = nil }
        savePresets()
        presetMessage = "Deleted \(preset.name)."
        presetError = false
        appendLog("Preset deleted: \(preset.name).", error: false)
    }

    func clearLog() { logEntries.removeAll() }

    func clearAcars() { acarsRecords.removeAll() }

    func isPanelOpen(_ id: String) -> Bool { openPanelIDs.contains(id) }

    func setPanelOpen(_ id: String, open: Bool) {
        if open { openPanelIDs.insert(id) } else { openPanelIDs.remove(id) }
    }

    func controlValuesChanged() { saveControlState() }

    func digitalOutputTitle(for receiver: Int) -> String {
        directAudioPlaying && directAudioReceiver == receiver ? "STOP" : "DIGITAL"
    }

    func digitalOutputAvailable(for receiver: Int) -> Bool {
        !directAudioPlaying || directAudioReceiver == receiver
    }

    func toggleDigitalOutput(_ receiver: Int) {
        if directAudioPlaying {
            guard directAudioReceiver == receiver else {
                setStatus("Digital audio is active for SDR \(directAudioReceiver + 1). Stop it before selecting another SDR.", failure: true)
                return
            }
            stopAndDisableDirectAudio()
            return
        }
        directAudioReceiver = receiver
        enableAndListenDirectAudio()
    }

    func enableAndListenDirectAudio() {
        guard !directAudioPlaying else { return }
        guard !acarsListening else {
            directAudioStatus = "Stop ACARS first; the SBS-3 supports one network session."
            directAudioError = true
            return
        }
        startDirectAudioListener(route: UInt8(directAudioReceiver))
    }

    func stopAndDisableDirectAudio() {
        guard let monitor = audioMonitor, directAudioPlaying else {
            directAudioStatus = "No active digital-audio session to disable."
            directAudioError = true
            return
        }
        guard pendingDirectAudioDisableSequence == nil else { return }
        let disableSequence = sequence
        let frame = SBS3Frame.digitalAudioMixer(receiver: nil, enabled: false, sequence: disableSequence)
        do {
            try monitor.send(frame)
            pendingDirectAudioDisableSequence = disableSequence
            directAudioStatus = "Disabling digital audio…"
            directAudioError = false
            appendLog(String(format: "TX 0x%02X  %@", disableSequence, frame.networkWireData.sbs3Hex), error: false)
        } catch {
            directAudioStatus = error.localizedDescription
            directAudioError = true
            setStatus(error.localizedDescription, failure: true)
            appendLog("ERROR \(error.localizedDescription)", error: true)
        }
    }

    func stopDirectAudioListening() {
        stopDirectAudioListener()
        directAudioStatus = "Stopped"
        directAudioError = false
        setStatus("Digital audio stopped.", failure: false)
    }

    private func stopDirectAudioListener() {
        audioMonitor?.stop(); audioMonitor = nil
        pcmPlayer?.stop(); pcmPlayer = nil
        directAudioPlaying = false
        pendingDirectAudioDisableSequence = nil
    }

    /// Opens the SBS-3's only TCP session once.  If a source is supplied, the
    /// digital-mixer write, format read and continuous PCM stream all share
    /// this same connection; opening a second connection causes the device to
    /// reject it.
    private func startDirectAudioListener(route: UInt8?) {
        guard !sending else {
            directAudioStatus = "Another SBS-3 command is still being sent."
            directAudioError = true
            return
        }
        let address = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let networkPort = UInt16(port), !address.isEmpty else { directAudioStatus = "Enter a valid SBS-3 address and port."; directAudioError = true; return }
        let monitor = SBS3AcarsMonitor()
        audioMonitor = monitor; directAudioPlaying = true; directAudioError = false
        directAudioStatus = route == nil
            ? "Listening at \(address):\(networkPort)…"
            : "Routing SDR \(Int(route!) + 1) to digital audio…"
        setStatus(directAudioStatus, failure: false)
        let audioFormatTag: UInt8 = 0xD3
        let audioFormatRead = SBS3Frame(payload: Data([0x54, audioFormatTag, 0x04, 0x00, 0x02, 0x00, 0x01, 0x00, 0x05, 0x00, 0x02, 0x00]))
        let routeSequence = route == nil ? nil : sequence
        var startupFrames = [SBS3Frame(payload: Data([0x17, 0x00]))]
        if let route, let routeSequence {
            let mixer = SBS3Frame.digitalAudioMixer(receiver: route, enabled: true, sequence: routeSequence)
            startupFrames.append(mixer)
            appendLog(String(format: "TX 0x%02X  %@", routeSequence, mixer.networkWireData.sbs3Hex), error: false)
        }
        startupFrames.append(audioFormatRead)
        monitor.start(host: address, port: networkPort, startupFrames: startupFrames, onFrame: { [weak self] frame in
            guard let self else { return }
            if let routeSequence,
               frame.payload.count >= 3,
               frame.payload[0] == 0x20,
               frame.payload[1] == routeSequence {
                guard frame.payload[2] == 0 else {
                    self.directAudioStatus = "SBS-3 rejected the digital-audio route."
                    self.directAudioError = true
                    self.setStatus(self.directAudioStatus, failure: true)
                    self.appendLog(String(format: "ERROR SBS-3 rejected digital-audio route sequence 0x%02X.", routeSequence), error: true)
                    self.audioMonitor?.stop()
                    return
                }
                self.sequence &+= 1
                UserDefaults.standard.set(Int(self.sequence), forKey: "sbs3.sequence")
                self.directAudioRouteEnabled = true
                self.setStatus("Digital audio routed from SDR \(self.directAudioReceiver + 1).", failure: false)
                self.appendLog(String(format: "RX SBS-3 confirmed digital-audio route sequence 0x%02X.", routeSequence), error: false)
                return
            }
            if let disableSequence = self.pendingDirectAudioDisableSequence,
               frame.payload.count >= 3,
               frame.payload[0] == 0x20,
               frame.payload[1] == disableSequence {
                guard frame.payload[2] == 0 else {
                    self.pendingDirectAudioDisableSequence = nil
                    self.directAudioStatus = "SBS-3 did not disable digital audio."
                    self.directAudioError = true
                    self.setStatus(self.directAudioStatus, failure: true)
                    self.appendLog(String(format: "ERROR SBS-3 rejected digital-audio disable sequence 0x%02X.", disableSequence), error: true)
                    return
                }
                self.sequence &+= 1
                UserDefaults.standard.set(Int(self.sequence), forKey: "sbs3.sequence")
                self.pendingDirectAudioDisableSequence = nil
                self.directAudioRouteEnabled = false
                self.appendLog(String(format: "RX SBS-3 confirmed digital-audio disable sequence 0x%02X.", disableSequence), error: false)
                self.stopDirectAudioListener()
                self.directAudioStatus = "Digital audio disabled."
                self.directAudioError = false
                self.setStatus(self.directAudioStatus, failure: false)
                return
            }
            if let config = SBS3DigitalAudioConfig.fromRegisterResponse(frame, tag: audioFormatTag) {
                self.pcmPlayer?.stop()
                let player = SBS3PCMPlayer(stereo: config.stereo, sampleRate: config.sampleRate, bitDepth: config.bitDepth, duplicateLeftChannel: config.stereo)
                do {
                    try player.start()
                    self.pcmPlayer = player
                    self.directAudioRouteEnabled = config.outputEnabled
                    self.directAudioStatus = "Receiving digital mixer audio…"
                    self.directAudioError = false
                    self.setStatus("Digital audio streaming from SDR \(self.directAudioReceiver + 1).", failure: false)
                } catch {
                    self.directAudioStatus = error.localizedDescription
                    self.directAudioError = true
                    self.setStatus(error.localizedDescription, failure: true)
                }
                return
            }
            // Observed SBS-3 firmware streams 0x3B followed directly by a
            // 128-byte PCM block; there is no tag byte in these frames.
            guard frame.payload.first == 0x3B, frame.payload.count == 129,
                  SBS3Frame.xmodemCRC(frame.payload) == frame.integrity else { return }
            let pcm = Data(frame.payload.dropFirst())
            guard self.pcmPlayer != nil else { return }
            self.pcmPlayer?.enqueue(pcm)
            let peak = pcm.withUnsafeBytes { raw -> Int in
                guard let samples = raw.bindMemory(to: Int16.self).baseAddress else { return 0 }
                return (0..<(pcm.count / 2)).reduce(0) { max($0, abs(Int(samples[$1].littleEndian))) }
            }
            if Date().timeIntervalSince(self.lastDirectAudioStatusUpdate) >= 0.25 {
                self.lastDirectAudioStatusUpdate = Date()
                self.directAudioStatus = "Receiving digital mixer audio · peak \(peak)"
            }
        }, onStopped: { [weak self] failure in
            guard let self else { return }
            self.pcmPlayer?.stop(); self.pcmPlayer = nil; self.audioMonitor = nil; self.directAudioPlaying = false
            self.pendingDirectAudioDisableSequence = nil
            self.directAudioStatus = failure ?? "Stopped"; self.directAudioError = failure != nil
            self.setStatus(self.directAudioStatus, failure: failure != nil)
        })
    }

    func toggleAcarsMonitoring() {
        if acarsListening {
            acarsMonitor?.stop()
            acarsMonitor = nil
            acarsListening = false
            acarsStatus = "Stopped"
            appendLog("ACARS listener stopped.", error: false)
            return
        }
        guard !directAudioPlaying else {
            acarsStatus = "Stop direct audio first; the SBS-3 supports one network session."
            appendLog("ACARS listener was not started: direct audio owns the SBS-3 network session.", error: true)
            return
        }
        let address = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !address.isEmpty, let networkPort = UInt16(port) else {
            acarsStatus = "Enter a valid SBS-3 address and port."
            appendLog("ACARS listener was not started: invalid network target.", error: true)
            return
        }
        let monitor = SBS3AcarsMonitor()
        acarsMonitor = monitor
        acarsListening = true
        acarsStatus = "Listening at \(address):\(networkPort)"
        appendLog("ACARS listener started at \(address):\(networkPort).", error: false)
        monitor.start(host: address, port: networkPort, onFrame: { [weak self] frame in
            self?.receiveAcarsFrame(frame)
        }, onStopped: { [weak self] failure in
            guard let self else { return }
            self.acarsListening = false
            self.acarsMonitor = nil
            if let failure {
                self.acarsStatus = failure
                self.appendLog("ACARS listener error: \(failure)", error: true)
            } else if self.acarsStatus != "Stopped" {
                self.acarsStatus = "Stopped"
            }
        })
    }

    func toggleTuner(_ index: Int) {
        let requestedState = !tunerEnabled[index]
        sendTuner(index, enabled: requestedState) { [weak self] in
            guard let self else { return }
            self.tunerEnabled[index] = requestedState
            self.saveControlState()
        }
    }

    func toggleMute(_ index: Int) {
        receivers[index].muted.toggle()
        saveControlState()
        sendMuteState(index)
    }

    func sendTuner(_ index: Int, enabled requestedState: Bool? = nil, onSuccess: @escaping () -> Void = {}) {
        saveControlState()
        guard let frequencyHz = hertz(from: tunerFrequencies[index]) else { setStatus("Enter a valid tuner frequency.", failure: true); return }
        let enabled = requestedState ?? tunerEnabled[index]
        let frame = SBS3Frame.tunerConfiguration(tuner: index == 0 ? .a : .b, frequencyHz: frequencyHz, enabled: enabled, sequence: sequence)
        // BaseStation re-applies every SDR assigned to the changed tuner
        // immediately after the tuner frame.  Sending just the tuner frame
        // leaves the SBS-3's receiver path in an inconsistent state on some
        // units (audible full-scale noise from Tuner A).  Do not alter mixer
        // settings here; BaseStation's observed tuner workflow only sends the
        // dependent SDR configuration frames.
        var frames = [frame]
        for (receiverIndex, receiver) in receivers.enumerated() where receiver.source == index {
            guard let receiverFrequencyHz = hertz(from: receiver.frequency) else {
                setStatus("Enter a valid SDR \(receiverIndex + 1) frequency.", failure: true)
                return
            }
            let receiverFrame = SBS3Frame.sdrConfiguration(
                receiver: UInt8(receiverIndex),
                frequencyHz: receiverFrequencyHz,
                am: receiver.mode == 0,
                source: index == 0 ? .a : .b,
                squelch: UInt8(receiver.squelch),
                muted: receiver.muted,
                sequence: sequence &+ UInt8(frames.count)
            )
            frames.append(receiverFrame)
        }
        send(frames: frames, count: UInt8(frames.count), onSuccess: onSuccess)
    }

    func restoreAudioMixer() {
        let baseStationLevels = [UInt8](repeating: 100, count: 6)
        let analog = SBS3Frame.mixerConfiguration(
            volumes: baseStationLevels,
            output: 0,
            sequence: sequence
        )
        let digital = SBS3Frame.mixerConfiguration(
            volumes: baseStationLevels,
            output: 1,
            sequence: sequence &+ 1
        )
        send(frames: [analog, digital], count: 2)
    }

    func sendSDR(_ index: Int) {
        saveControlState()
        let receiver = receivers[index]
        guard let hertz = hertz(from: receiver.frequency) else { setStatus("Enter a valid SDR frequency.", failure: true); return }
        let source: Tuner = receiver.source == 0 ? .a : .b
        let frame = SBS3Frame.sdrConfiguration(receiver: UInt8(index), frequencyHz: hertz, am: receiver.mode == 0, source: source, squelch: UInt8(receiver.squelch), muted: receiver.muted, sequence: sequence)
        let squelch = SBS3Frame.sdrSquelchUpdate(receiver: UInt8(index), squelch: UInt8(receiver.squelch), sequence: sequence &+ 1)
        let mixer = SBS3Frame.mixerConfiguration(volumes: receivers.map { UInt8($0.volume) }, sequence: sequence &+ 2)
        send(frames: [frame, squelch, mixer], count: 3)
    }

    /// A mute button must affect only its own SDR.  In particular, do not
    /// rewrite the shared analogue mixer here: doing so on every mute/unmute
    /// can disturb the active Tuner A audio path.
    private func sendMuteState(_ index: Int) {
        let receiver = receivers[index]
        guard let hertz = hertz(from: receiver.frequency) else {
            setStatus("Enter a valid SDR frequency.", failure: true)
            return
        }
        let source: Tuner = receiver.source == 0 ? .a : .b
        let frame = SBS3Frame.sdrConfiguration(
            receiver: UInt8(index),
            frequencyHz: hertz,
            am: receiver.mode == 0,
            source: source,
            squelch: UInt8(receiver.squelch),
            muted: receiver.muted,
            sequence: sequence
        )
        send(frames: [frame], count: 1)
    }

    private func send(frames: [SBS3Frame], count: UInt8, onSuccess: @escaping () -> Void = {}) {
        guard !sending else { setStatus("A command is already being sent. Please wait.", failure: true); return }
        if directAudioPlaying {
            setStatus("Stop direct audio first; the SBS-3 supports one network session.", failure: true)
            return
        }
        if acarsListening {
            setStatus("Stop ACARS first; the SBS-3 supports one network session.", failure: true)
            return
        }
        let address = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !address.isEmpty, let networkPort = UInt16(port) else { setStatus("Enter a valid SBS-3 address and TCP port.", failure: true); return }
        sending = true
        setStatus("Sending to \(address):\(networkPort)…", failure: false)
        let sequences = (0..<Int(count)).map { sequence &+ UInt8($0) }
        for (frame, frameSequence) in zip(frames, sequences) {
            appendLog(String(format: "TX 0x%02X  %@", frameSequence, frame.networkWireData.sbs3Hex), error: false)
        }
        SBS3NetworkClient.send(frames: frames, sequences: sequences, host: address, port: networkPort) { [weak self] result in
            guard let self else { return }
            self.sending = false
            switch result {
            case let .success(message):
                self.sequence &+= count
                UserDefaults.standard.set(self.host, forKey: "sbs3.host")
                UserDefaults.standard.set(self.port, forKey: "sbs3.port")
                UserDefaults.standard.set(Int(self.sequence), forKey: "sbs3.sequence")
                self.setStatus(message, failure: false)
                self.appendLog("RX \(message)", error: false)
                onSuccess()
            case let .failure(error):
                self.setStatus(error.localizedDescription, failure: true)
                self.appendLog("ERROR \(error.localizedDescription)", error: true)
            }
        }
    }

    private func hertz(from value: String) -> UInt32? {
        let normalized = value.replacingOccurrences(of: ",", with: ".")
        guard let mhz = Double(normalized), mhz >= 0, mhz <= 4_294.967_295 else { return nil }
        return UInt32((mhz * 1_000_000).rounded())
    }

    private func setStatus(_ message: String, failure: Bool) {
        status = message
        self.failure = failure
    }

    private static func loadPresets() -> [FrequencyPreset] {
        guard let data = UserDefaults.standard.data(forKey: "sbs3.presets"),
              let saved = try? JSONDecoder().decode([FrequencyPreset].self, from: data), !saved.isEmpty else {
            return [
                FrequencyPreset(name: "Vienna Radar", frequency: "129.050"),
                FrequencyPreset(name: "Airband test", frequency: "134.675"),
            ]
        }
        return saved
    }

    private func savePresets() {
        guard let data = try? JSONEncoder().encode(presets) else { return }
        UserDefaults.standard.set(data, forKey: "sbs3.presets")
    }

    private struct SavedControlState: Codable {
        let tunerFrequencies: [String]
        let tunerEnabled: [Bool]
        let receivers: [MacReceiver]
    }

    private static func loadControlState() -> SavedControlState {
        let fallback = SavedControlState(
            tunerFrequencies: ["125.000", "123.800"],
            tunerEnabled: [false, false],
            receivers: [
                MacReceiver(id: 0, frequency: "119.400"), MacReceiver(id: 1, frequency: "123.800"),
                MacReceiver(id: 2, frequency: "121.600"), MacReceiver(id: 3, frequency: "121.775"),
                MacReceiver(id: 4, frequency: "134.675"), MacReceiver(id: 5, frequency: "125.175"),
            ]
        )
        guard let data = UserDefaults.standard.data(forKey: "sbs3.control-state"),
              let state = try? JSONDecoder().decode(SavedControlState.self, from: data),
              state.tunerFrequencies.count == 2, state.tunerEnabled.count == 2, state.receivers.count == 6 else { return fallback }
        return state
    }

    private func saveControlState() {
        let state = SavedControlState(tunerFrequencies: tunerFrequencies, tunerEnabled: tunerEnabled, receivers: receivers)
        guard let data = try? JSONEncoder().encode(state) else { return }
        UserDefaults.standard.set(data, forKey: "sbs3.control-state")
    }

    private func appendLog(_ message: String, error: Bool) {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        logEntries.append(DeviceLogEntry(text: "\(formatter.string(from: Date()))  \(message)", isError: error))
        if logEntries.count > 500 { logEntries.removeFirst(logEntries.count - 500) }
    }

    private func receiveAcarsFrame(_ frame: SBS3NetworkFrame) {
        guard frame.payload.first == 0x58,
              frame.payload.count >= 32,
              Int(frame.payload[2]) + 5 == frame.payload.count,
              SBS3Frame.xmodemCRC(frame.payload) == frame.integrity else { return }
        func decode(_ range: Range<Int>) -> String {
            String(bytes: frame.payload[range].map { $0 & 0x7F }, encoding: .ascii) ?? ""
        }
        let record = MacAcarsRecord(
            timestamp: Date(),
            receiver: Int(frame.payload[1]),
            mode: decode(6..<7),
            registration: decode(8..<14).trimmingCharacters(in: CharacterSet(charactersIn: ". ")),
            sequence: decode(19..<23),
            flight: decode(23..<29).trimmingCharacters(in: .whitespaces),
            message: decode(29..<(frame.payload.count - 3)).trimmingCharacters(in: .whitespaces)
        )
        acarsRecords.append(record)
        if acarsRecords.count > 500 { acarsRecords.removeFirst(acarsRecords.count - 500) }
        appendLog("ACARS SDR \(record.receiver + 1): \(record.registration) \(record.flight)", error: false)
    }
}

@MainActor
private final class SDRControls {
    let source = NSPopUpButton()
    let mode = NSPopUpButton()
    let frequency: NSTextField
    let squelch = NSSlider(value: 75, minValue: 0, maxValue: 100, target: nil, action: nil)
    let volume = NSSlider(value: 75, minValue: 0, maxValue: 100, target: nil, action: nil)
    let squelchLabel = NSTextField(labelWithString: "")
    let volumeLabel = NSTextField(labelWithString: "")
    let mute = NSButton(title: "SIGNAL", target: nil, action: nil)
    let send = NSButton(title: "Send", target: nil, action: nil)
    var muted = false

    init(receiver: Int) {
        let defaults = ["119.400", "123.800", "121.600", "121.775", "134.675", "125.175"]
        frequency = NSTextField(string: defaults[receiver])
        squelch.controlSize = .small
        volume.controlSize = .small
        squelch.widthAnchor.constraint(equalToConstant: 105).isActive = true
        volume.widthAnchor.constraint(equalToConstant: 105).isActive = true
        mute.bezelStyle = .rounded
        send.bezelStyle = .rounded
    }
}
