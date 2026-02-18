# Prerequisites

## Supported Platforms

- Primary target: Raspberry Pi / Debian-based Linux
- Windows and macOS: useful for tests and non-hardware logic, but hardware I/O
  paths are Linux-oriented

## Python

ROI requires Python 3.10+.

Check version:

```bash
python3 --version
```

## Hardware

Typical deployment:

- Raspberry Pi 4/5 (or similar)
- CAN interface:
  - SocketCAN device (for example PiCAN, MCP2515, USB-CAN), or
  - RM/Proemion CANview serial gateway
- Optional instruments (ROI tolerates partial hardware):
  - B&K Precision bench multimeter (USB serial)
  - Electronic load (VISA USBTMC)
  - AFG/function generator (VISA USB or VISA serial)
  - USB relay controller for K1 (Arduino-style or DSD Tech SH-URxx)
  - MrSignal/LANYI MR2.x Modbus PSU

## OS Packages (Recommended)

The Pi installer can install these automatically:

- `python3-venv`, `python3-pip`, `python3-dev`
- `can-utils`
- `libusb-1.0-0`, `usbutils`
- `rsync`
