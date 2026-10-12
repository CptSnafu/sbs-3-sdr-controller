# macOS Guide

The native macOS application controls an SBS-3 over Ethernet. USB control is
Windows-only because it relies on the FTDI D2XX driver.

## Build

1. Install Apple Command Line Tools:

   ```sh
   xcode-select --install
   ```

2. In the project folder, build the app:

   ```sh
   ./build-macos-app.sh
   ```

The application is created at `dist/SBS-3 SDR Control.app`.

## Connecting to an SBS-3

1. Close BaseStation.
2. Start **SBS-3 SDR Control**.
3. Enter the SBS-3 Ethernet address and TCP port in the network row.
4. Use **NW Check**. It tests TCP connectivity only and does not send SBS-3
   protocol data.
5. Configure a tuner or SDR and verify the confirmation in **Device Log**.

The app stores the address, port, command sequence, controls, and presets in
local macOS preferences.

## Notes

- The app is locally built and ad-hoc signed. If macOS warns when opening it,
  Control-click the app, choose **Open**, then confirm once.
- Only one SBS-3 Ethernet session may be active. Stop **DIGITAL** audio before
  starting ACARS or sending normal controls.
- Digital audio is experimental. It uses the SBS-3 digital mixer and plays the
  selected SDR through both Mac speakers.

For all controls and troubleshooting, see the [User Manual](MANUAL.md).
