# SBS-3 SDR Control User Manual

## Requirements

You need one reachable Kinetic SBS-3 and BaseStation must be completely closed.

- **Windows USB:** install the official FTDI D2XX driver. The application finds
  the SBS-3 control interface automatically; no Device Manager lookup or FTDI
  index is required.
- **Ethernet:** use the SBS-3 address and TCP port. Kinetic documents
  `192.168.1.170:10001` as the default, but use the address assigned on your
  own network.
- **macOS:** Ethernet control only.

Do not use the SBS-3 USB data connection and its Ethernet API at the same time.

## Connection

On Windows, open **Connection** and choose **USB** or **Network**. When using
Network, enter the address and TCP port, then use **NW Check** to verify TCP
reachability without sending SBS-3 data.

On macOS, the address, port, and **NW Check** button are in the network row at
the top of the main window.

## Tuners

Each tuner has a frequency field, a preset list, and an ON/OFF button.

- Enter a frequency in MHz and press Enter, or select a preset.
- The tuner becomes green only after the SBS-3 confirms the command.
- A grey tuner label means that the application has not confirmed that it is on.
- SDR activity indicators are hidden when their selected tuner is off.

## SDR receivers

Each of the six SDR rows has the same controls:

- **Tuner A/B** selects the hardware tuner source.
- **AM/FM** selects the receiver demodulation mode.
- **Frequency** accepts free input or a named preset.
- **SQ** sets squelch from 0 to 100 percent and sends after a short pause.
- **Vol** sets the analogue mixer level from 0 to 100 percent.
- **SIGNAL/MUTE** immediately unmutes or mutes that receiver.
- **Send** resends the full receiver configuration.

The RSSI display and green bar represent relative receiver activity from SBS-3
status frames. They are not dB or calibrated RF measurements.

## Digital audio

The **DIGITAL** button routes one selected SDR through the SBS-3 digital mixer
to the local computer over Ethernet. The button changes to **STOP** while it is
active. Only one SDR can use this output at a time.

The SBS-3 accepts a single Ethernet session. Stop digital audio before changing
normal tuner or SDR settings. Digital playback is experimental and should be
tested before any operational use.

## Frequency presets and audio recovery

Open **Admin** to add, update, or delete named frequency presets. Presets are
available for both tuners and all six SDRs, while direct frequency entry remains
available at all times.

**Restore SBS-3 Audio Mixer** restores the configured analogue and digital mixer
levels. It does not change tuner frequencies, SDR frequencies, modes, or mute
settings.

## Device Log and ACARS

**Device Log** shows the selected connection, sent commands, SBS-3 replies, and
errors. Include relevant log lines when reporting an issue.

**ACARS** shows CRC-checked records decoded by the SBS-3. The displayed source
frequency is the current application setting for that SDR; it is not a separate
frequency field supplied by the ACARS record.

For quiet ACARS monitoring, use **Vol 0%**. This keeps the receiver running
while removing it from the analogue audio mix.

## Troubleshooting

### The SBS-3 is not found over USB

1. Confirm that the official FTDI D2XX driver is installed.
2. Disconnect and reconnect the SBS-3.
3. Ensure that only one SBS-3 is attached.
4. Close BaseStation completely and restart the application.

### A command is not confirmed

Close BaseStation and any other software that might be connected to the SBS-3.
For Ethernet, verify the address and port with **NW Check**. The Device Log
contains the useful detail.

### There is no audio

1. Confirm that the selected tuner is on.
2. Confirm the SDR source, frequency, and AM/FM mode.
3. Check squelch, mute, and volume.
4. Confirm that the frequency is within the tuner reception range.
5. For digital audio, stop it and verify normal audio first.
