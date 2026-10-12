# SBS-3 SDR Control

An independent control application for the Kinetic SBS-3 receiver. It provides
direct control of the two hardware tuners and six virtual SDR receivers over
the FTDI USB interface on Windows or the SBS-3 Ethernet API on Windows and
macOS.

> This community project is not affiliated with, endorsed by, or supported by
> Kinetic Avionics or FTDI.

## Features

- Tuner A/B frequency and confirmed ON/OFF control
- Six SDR receivers with tuner source, AM/FM, frequency, squelch, mute, and volume
- Named frequency presets shared by tuners and SDRs
- Automatic Windows USB discovery of the SBS-3 control channel
- Ethernet control with saved address/port settings and a non-invasive network check
- Device Log, relative RSSI/activity indicators, and CRC-checked ACARS records
- Per-SDR **DIGITAL** output over Ethernet, using the SBS-3 digital mixer

## Supported platforms

| Platform | USB control | Ethernet control | Digital audio |
| --- | --- | --- | --- |
| Windows 10/11 | Yes, with the FTDI D2XX driver | Yes | Experimental |
| macOS 14+ (Apple Silicon) | No | Yes | Experimental |

The project does not include or redistribute `ftd2xx.dll`. Install the official
FTDI D2XX driver before using USB control on Windows.

## Quick start

### Windows

Run the Python application from the project folder:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m sbs3 windows-gui
```

To build a double-clickable executable on Windows:

```powershell
.\build-windows-app.ps1
```

The result is `dist\SBS-3 SDR Control.exe`.

### macOS

Install Apple Command Line Tools, then run:

```sh
./build-macos-app.sh
```

The result is `dist/SBS-3 SDR Control.app`. See [MACOS.md](MACOS.md) for the
macOS-specific setup notes.

## Important safety notes

- Close BaseStation before using this application. Never control the same SBS-3
  from two programs at once.
- Use **either** the USB data connection **or** Ethernet control, not both.
- The application changes real tuner, receiver, and mixer state. Verify settings
  before relying on reception.
- The Ethernet API accepts one active session at a time. Stop direct digital
  audio before sending regular controls.
- RSSI is a relative activity indicator, not a calibrated signal-strength value.

## Documentation

- [User manual](MANUAL.md)
- [macOS guide](MACOS.md)
- [MIT License](LICENSE)
