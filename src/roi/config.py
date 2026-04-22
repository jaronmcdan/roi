"""Runtime configuration for ROI (Remote Operational Equipment).

All values in this file can be overridden via environment variables.
This is useful when running as a systemd service, where `/etc/roi/roi.env`
can hold per-host overrides.

Parsing rules
- booleans: 1/0, true/false, yes/no, on/off
- integers: decimal by default; `0x` prefix is allowed for hex
- floats: standard Python float format

Design note
This module is intentionally *side-effect free* (no I/O). Importing `roi.config`
must be safe in CI, unit tests, and on machines with no hardware attached.
"""

from __future__ import annotations

import os


def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None or v == "" else v


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    s = v.strip().lower()
    try:
        # allow hex like 0x1a
        return int(s, 0)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v.strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    s = v.strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


# -----------------------------------------------------------------------------
# Build / version tag
# -----------------------------------------------------------------------------

# A free-form tag to make it obvious which build is running (showed in logs).
# Override via env var ROI_BUILD_TAG.
BUILD_TAG = _env_str("ROI_BUILD_TAG", "dev")


# -----------------------------------------------------------------------------
# Multimeter (B&K Precision 2831E / 5491B) over USB-serial
# -----------------------------------------------------------------------------

MULTI_METER_PATH = _env_str("MULTI_METER_PATH", "/dev/ttyUSB0")
MULTI_METER_BAUD = _env_int("MULTI_METER_BAUD", 38400)
MULTI_METER_TIMEOUT = _env_float("MULTI_METER_TIMEOUT", 1.0)
MULTI_METER_WRITE_TIMEOUT = _env_float("MULTI_METER_WRITE_TIMEOUT", 1.0)

# If True, HardwareManager will send "*IDN?" again on startup to verify the
# meter is responsive. If False (default), we avoid sending extra commands on
# boot because some 5491B units will beep/throw a bus error if *anything* else
# touches the port during early init (e.g., VISA ASRL probing).
MULTI_METER_VERIFY_ON_STARTUP = _env_bool("MULTI_METER_VERIFY_ON_STARTUP", False)

# Optional cached IDN string (patched at runtime by device_discovery).
MULTI_METER_IDN = _env_str("MULTI_METER_IDN", "")

# Many USB-serial instruments echo commands and/or respond a moment later.
# These settings make IDN probing more robust.
MULTI_METER_IDN_DELAY = _env_float("MULTI_METER_IDN_DELAY", 0.05)
MULTI_METER_IDN_READ_LINES = _env_int("MULTI_METER_IDN_READ_LINES", 4)

# Measurement query commands to try for the multimeter.
# Different meters expose different SCPI subsets. We will try these in order
# until we get a parseable float.
MULTI_METER_FETCH_CMDS = _env_str(
    "MULTI_METER_FETCH_CMDS",
    ":FETCh?,:FETC?",
)

# SCPI dialect for the 2831E/5491B bench multimeters.
#
# Values:
#   - "auto" (default): choose a working dialect (tries "conf" then "func")
#   - "func": force :FUNCtion / :VOLTage/:CURRent... dialect
#   - "conf": force CONF:... / CONFigure:... dialect
MMETER_SCPI_STYLE = _env_str("MMETER_SCPI_STYLE", "auto").strip().lower()

# After any control command that changes measurement mode/range/etc, ROI pauses
# background meter polling briefly so the instrument can settle.
MMETER_CONTROL_SETTLE_SEC = _env_float("MMETER_CONTROL_SETTLE_SEC", 0.30)

# Enable extra multimeter SCPI logging (useful for diagnosing "BUS" errors).
MMETER_DEBUG = _env_bool("MMETER_DEBUG", False)

# Legacy CAN frame compatibility: the original MMETER_CTRL_ID frame includes a
# 2nd byte often called "range". Historically ROI did **not** apply it to the
# instrument (it only stored it).
MMETER_LEGACY_RANGE_ENABLE = _env_bool("MMETER_LEGACY_RANGE_ENABLE", False)

# Legacy mode mapping controls (MMETER_CTRL_ID byte0: METER_MODE).
# Defaults preserve historical behavior:
#   mode 0 -> set primary function to VDC
#   mode 1 -> set primary function to IDC
# Set MMETER_LEGACY_MODE0_ENABLE=0 on firmware that rejects VDC mode changes.
MMETER_LEGACY_MODE0_ENABLE = _env_bool("MMETER_LEGACY_MODE0_ENABLE", True)
MMETER_LEGACY_MODE1_ENABLE = _env_bool("MMETER_LEGACY_MODE1_ENABLE", True)

# Extended MMETER control frame handling (MMETER_CTRL_EXT_ID).
# Set to 0 to hard-disable processing of extended opcodes. Useful when PAT
# scripts use only legacy METER_MODE/METER_RANGE and you want to eliminate any
# chance of stray/ext opcodes causing meter-side BUS errors.
MMETER_EXT_CTRL_ENABLE = _env_bool("MMETER_EXT_CTRL_ENABLE", True)

# Fine-grained guards for EXT opcodes that are unsupported on some 5491B
# firmware variants. Defaults keep current ROI behavior.
MMETER_EXT_SET_RANGE_ENABLE = _env_bool("MMETER_EXT_SET_RANGE_ENABLE", True)
MMETER_EXT_SECONDARY_ENABLE = _env_bool("MMETER_EXT_SECONDARY_ENABLE", True)

# If True, query and drain the multimeter error queue on startup.
MMETER_CLEAR_ERRORS_ON_STARTUP = _env_bool("MMETER_CLEAR_ERRORS_ON_STARTUP", True)

# If we don't yet know the correct fetch command, we will probe at this
# interval (seconds) to avoid spamming the meter with unknown commands.
MULTI_METER_PROBE_BACKOFF_SEC = _env_float("MULTI_METER_PROBE_BACKOFF_SEC", 2.0)


# -----------------------------------------------------------------------------
# Optional USB / VISA auto-detection (Raspberry Pi)
# -----------------------------------------------------------------------------

# When enabled, `roi.app` will scan /dev/serial/by-id + PyVISA resources at
# startup and patch these config values at runtime:
#   - MULTI_METER_PATH
#   - MRSIGNAL_PORT
#   - K1_SERIAL_PORT
#   - CAN_INTERFACE + CAN_CHANNEL (auto-select rmcanview when CANview is present)
#   - AFG_VISA_ID
#   - ELOAD_VISA_ID
AUTO_DETECT_ENABLE = _env_bool("AUTO_DETECT_ENABLE", True)
AUTO_DETECT_VERBOSE = _env_bool("AUTO_DETECT_VERBOSE", True)

# If True, prefer mapping by /dev/serial/by-id (name matching) and avoid probing
# *unknown* serial ports.
AUTO_DETECT_BYID_ONLY = _env_bool("AUTO_DETECT_BYID_ONLY", False)

# Sub-features
AUTO_DETECT_MMETER = _env_bool("AUTO_DETECT_MMETER", True)
AUTO_DETECT_MRSIGNAL = _env_bool("AUTO_DETECT_MRSIGNAL", True)
AUTO_DETECT_K1_SERIAL = _env_bool("AUTO_DETECT_K1_SERIAL", True)
AUTO_DETECT_CANVIEW = _env_bool("AUTO_DETECT_CANVIEW", True)
# Prefer SocketCAN (PCAN-style netdev) over CANview when both are attached.
AUTO_DETECT_PCAN = _env_bool("AUTO_DETECT_PCAN", True)
# Comma-separated USB VID:PID list used to detect PCAN hardware presence.
AUTO_DETECT_PCAN_USB_IDS = _env_str("AUTO_DETECT_PCAN_USB_IDS", "0c72:000c")
# Preferred SocketCAN channel when PCAN is detected.
AUTO_DETECT_PCAN_PREFER_CHANNEL = _env_str("AUTO_DETECT_PCAN_PREFER_CHANNEL", "can0")

AUTO_DETECT_VISA = _env_bool("AUTO_DETECT_VISA", True)
AUTO_DETECT_AFG = _env_bool("AUTO_DETECT_AFG", True)
AUTO_DETECT_ELOAD = _env_bool("AUTO_DETECT_ELOAD", True)

# IDN matching hints (comma-separated, case-insensitive)
AUTO_DETECT_MMETER_IDN_HINTS = _env_str("AUTO_DETECT_MMETER_IDN_HINTS", "multimeter,5491b")
AUTO_DETECT_AFG_IDN_HINTS = _env_str("AUTO_DETECT_AFG_IDN_HINTS", "afg,function,generator,arb")
AUTO_DETECT_ELOAD_IDN_HINTS = _env_str(
    "AUTO_DETECT_ELOAD_IDN_HINTS",
    "load,eload,electronic load,dl,it,bk,b&k,b&k precision,bk precision,8600",
)

# By-id name matching hints (comma-separated, case-insensitive).
# These are matched against the /dev/serial/by-id symlink names.
AUTO_DETECT_MMETER_BYID_HINTS = _env_str("AUTO_DETECT_MMETER_BYID_HINTS", AUTO_DETECT_MMETER_IDN_HINTS)
AUTO_DETECT_MRSIGNAL_BYID_HINTS = _env_str("AUTO_DETECT_MRSIGNAL_BYID_HINTS", "mr.signal,lanyi,mr2,mrsignal")
AUTO_DETECT_K1_BYID_HINTS = _env_str("AUTO_DETECT_K1_BYID_HINTS", "dsd,dsdtech,arduino,relay,cp2102")
# Additional by-id name hints that should *not* be considered for K1 relay
# auto-detection (helps avoid accidental collisions with other serial devices).
AUTO_DETECT_K1_BYID_EXCLUDE_HINTS = _env_str(
    "AUTO_DETECT_K1_BYID_EXCLUDE_HINTS",
    "mr.signal,lanyi,mrsignal,multimeter,5491,canview,rm_canview,proemion,afg",
)
AUTO_DETECT_CANVIEW_BYID_HINTS = _env_str("AUTO_DETECT_CANVIEW_BYID_HINTS", "canview,rm_canview,proemion")
AUTO_DETECT_AFG_BYID_HINTS = _env_str("AUTO_DETECT_AFG_BYID_HINTS", "afg")

# VISA/serial probing safety:
# - Probing ASRL resources sends *IDN? over a serial port. If the baud is wrong
#   for some attached device, that device may show an error.
AUTO_DETECT_VISA_PROBE_ASRL = _env_bool("AUTO_DETECT_VISA_PROBE_ASRL", True)
AUTO_DETECT_ASRL_BAUD = _env_int("AUTO_DETECT_ASRL_BAUD", 115200)

# Comma-separated device-node prefixes to exclude from ASRL probing.
AUTO_DETECT_VISA_ASRL_EXCLUDE_PREFIXES = _env_str(
    "AUTO_DETECT_VISA_ASRL_EXCLUDE_PREFIXES",
    "/dev/ttyAMA,/dev/ttyS,/dev/ttyUSB",
)

# Comma-separated device-node prefixes to *allow* for ASRL probing.
# If set, only these serial devices will be probed via VISA.
AUTO_DETECT_VISA_ASRL_ALLOW_PREFIXES = _env_str(
    "AUTO_DETECT_VISA_ASRL_ALLOW_PREFIXES",
    "/dev/ttyACM",
)

# Prefer stable by-id symlinks when present.
AUTO_DETECT_PREFER_BY_ID = _env_bool("AUTO_DETECT_PREFER_BY_ID", True)

# Optional: force a PyVISA backend ("@py" for pyvisa-py).
AUTO_DETECT_VISA_BACKEND = _env_str("AUTO_DETECT_VISA_BACKEND", "@py")

# Runtime VISA backend for instrument I/O.
VISA_BACKEND = _env_str("VISA_BACKEND", AUTO_DETECT_VISA_BACKEND)

# VISA Resource IDs (PyVISA)
ELOAD_VISA_ID = _env_str("ELOAD_VISA_ID", "USB0::11975::34816::*::0::INSTR")
AFG_VISA_ID = _env_str("AFG_VISA_ID", "ASRL/dev/ttyACM0::INSTR")

# PyVISA I/O timeout (milliseconds). Lower values reduce "sluggish" feel when a
# device is disconnected or slow to respond, at the cost of more timeouts.
VISA_TIMEOUT_MS = _env_int("VISA_TIMEOUT_MS", 500)

# USB devices can enumerate slightly after the process starts (especially on boot
# or when starting under systemd). If the first VISA list_resources() call returns
# no USB devices, we retry a few times before giving up.
VISA_ENUM_RETRIES = _env_int("VISA_ENUM_RETRIES", 3)
VISA_ENUM_RETRY_DELAY_SEC = _env_float("VISA_ENUM_RETRY_DELAY_SEC", 0.5)


# -----------------------------------------------------------------------------
# K1 relay drive
# -----------------------------------------------------------------------------

K1_ENABLE = _env_bool("K1_ENABLE", True)

# K1 backend selection.
#
# Values:
#   - "auto" (default): For K1_CHANNEL_COUNT=1, try "serial" then "dsdtech".
#                       For K1_CHANNEL_COUNT>1, try "dsdtech" then "serial".
#   - "serial":  Force USB-serial relay backend (e.g. Arduino + relay board).
#   - "dsdtech": Force DSD Tech AT-command serial backend.
#   - "mock":    Always use a mock relay (no hardware).
#   - "disabled": Disable K1 entirely (same as K1_ENABLE=0).
K1_BACKEND = _env_str("K1_BACKEND", "auto").strip().lower()

# Number of logical relay channels controlled from CTRL_RLY.
# 1 keeps legacy behavior (single-channel relay mapped to K1).
K1_CHANNEL_COUNT = _env_int("K1_CHANNEL_COUNT", 1)

# USB-serial relay backend (Arduino relay controller).
# Use a stable by-id path when possible, e.g.:
#   /dev/serial/by-id/usb-Arduino*  (Linux)
K1_SERIAL_PORT = _env_str("K1_SERIAL_PORT", "")
K1_SERIAL_BAUD = _env_int("K1_SERIAL_BAUD", 9600)
K1_SERIAL_RELAY_INDEX = _env_int("K1_SERIAL_RELAY_INDEX", 1)
K1_SERIAL_BOOT_DELAY_SEC = _env_float("K1_SERIAL_BOOT_DELAY_SEC", 2.0)

# Optional explicit command bytes for the serial backend.
# If left empty, ROI will generate them from K1_SERIAL_RELAY_INDEX using the
# default protocol: ON = '1'..'4', OFF = 'a'..'d'.
K1_SERIAL_ON_CHAR = _env_str("K1_SERIAL_ON_CHAR", "")
K1_SERIAL_OFF_CHAR = _env_str("K1_SERIAL_OFF_CHAR", "")

# DSD Tech SH-URxx serial backend (AT-command style).
K1_DSDTECH_BAUD = _env_int("K1_DSDTECH_BAUD", K1_SERIAL_BAUD)
K1_DSDTECH_BOOT_DELAY_SEC = _env_float("K1_DSDTECH_BOOT_DELAY_SEC", 0.2)
# Starting physical channel index for logical K1 (1-based).
K1_DSDTECH_CHANNEL = _env_int("K1_DSDTECH_CHANNEL", 1)
# Format vars:
#   {index} -> physical relay index
#   {state} -> 1 (on) / 0 (off)
K1_DSDTECH_CMD_TEMPLATE = _env_str("K1_DSDTECH_CMD_TEMPLATE", "AT+CH{index}={state}")
# Command suffix. Supports escaped sequences (e.g. "\r\n").
K1_DSDTECH_CMD_SUFFIX = _env_str("K1_DSDTECH_CMD_SUFFIX", r"\r\n")

# Relay startup retries (helps absorb transient USB/CP210 bring-up races).
K1_INIT_RETRIES = _env_int("K1_INIT_RETRIES", 3)
K1_INIT_RETRY_DELAY_SEC = _env_float("K1_INIT_RETRY_DELAY_SEC", 0.25)

# If True, invert the incoming CAN bit0 before driving K1.
K1_CAN_INVERT = _env_bool("K1_CAN_INVERT", False)

# Idle/default drive state for K1 when control is missing (watchdog timeout)
# and (optionally) on program startup.
K1_IDLE_DRIVE = _env_bool("K1_IDLE_DRIVE", False)


# -----------------------------------------------------------------------------
# CAN bus
# -----------------------------------------------------------------------------

# CAN backend:
#   socketcan = Linux SocketCAN netdev (e.g. can0/can1)
#   rmcanview  = RM/Proemion CANview USB/RS232 gateways via serial (Byte Command Protocol)
CAN_INTERFACE = _env_str("CAN_INTERFACE", "socketcan").strip().lower()

# Channel identifier used by the selected backend:
#   - socketcan: "can0", "can1", ...
#   - rmcanview: "/dev/ttyUSB0", "/dev/serial/by-id/...", ...
CAN_CHANNEL = _env_str("CAN_CHANNEL", "can0")

# Serial baud rate for CAN_INTERFACE="rmcanview" (USB-serial link baud).
# This is *not* the CAN bus bitrate; use CAN_BITRATE for that.
CAN_SERIAL_BAUD = _env_int("CAN_SERIAL_BAUD", 115200)
CAN_BITRATE = _env_int("CAN_BITRATE", 250000)

# If True, ROI will try to bring the CAN interface up.
#   - socketcan: runs `ip link set <CAN_CHANNEL> up type can bitrate <CAN_BITRATE>`
#   - rmcanview: configures adapter CAN bitrate + forces active mode
CAN_SETUP = _env_bool("CAN_SETUP", True)

# If True and CAN_INTERFACE="rmcanview", ROI will issue a CAN controller reset
# on startup to clear any latched error status in the gateway.
CAN_CLEAR_ERRORS_ON_INIT = _env_bool("CAN_CLEAR_ERRORS_ON_INIT", CAN_SETUP)

# Max number of incoming CAN control frames buffered between the CAN RX thread
# and the device command worker.
CAN_CMD_QUEUE_MAX = _env_int("CAN_CMD_QUEUE_MAX", 256)

# For CAN_INTERFACE="rmcanview": max number of received frames buffered inside
# the adapter driver (between the serial reader thread and python-can recv()).
CAN_RMCANVIEW_RX_MAX = _env_int("CAN_RMCANVIEW_RX_MAX", 2048)


# -----------------------------------------------------------------------------
# Control watchdog / timeouts
# -----------------------------------------------------------------------------

# If a given device doesn't receive its control message within the timeout,
# ROI drives that device back to its configured idle state.
CONTROL_TIMEOUT_SEC = _env_float("CONTROL_TIMEOUT_SEC", 2.0)

# Extra grace before declaring a *hard* timeout (beyond CONTROL_TIMEOUT_SEC).
# This eliminates most UI flicker caused by borderline jitter when control
# frames arrive near the threshold.
WATCHDOG_GRACE_SEC = _env_float("WATCHDOG_GRACE_SEC", 0.25)

# Timeout used for the "CAN" freshness indicator (any CAN message received).
CAN_TIMEOUT_SEC = _env_float("CAN_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)

K1_TIMEOUT_SEC = _env_float("K1_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)
ELOAD_TIMEOUT_SEC = _env_float("ELOAD_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)
AFG_TIMEOUT_SEC = _env_float("AFG_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)
MMETER_TIMEOUT_SEC = _env_float("MMETER_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)

# Timeout for the dashboard-only PAT switching matrix footer (PAT_J0..PAT_J5).
PAT_MATRIX_TIMEOUT_SEC = _env_float("PAT_MATRIX_TIMEOUT_SEC", CAN_TIMEOUT_SEC)

# If True, apply idle states immediately on startup before processing controls.
APPLY_IDLE_ON_STARTUP = _env_bool("APPLY_IDLE_ON_STARTUP", True)

# Headless mode disables the Rich TUI (useful for systemd/journald).
ROI_HEADLESS = _env_bool("ROI_HEADLESS", False)


# ----------------------------------------------------------------------------
# Web dashboard (read-only)
# ----------------------------------------------------------------------------

# A lightweight, dependency-free web UI to view device status + diagnostics.
# Disabled by default.
ROI_WEB_ENABLE = _env_bool("ROI_WEB_ENABLE", False)
ROI_WEB_HOST = _env_str("ROI_WEB_HOST", "0.0.0.0")
ROI_WEB_PORT = _env_int("ROI_WEB_PORT", 8080)

# Optional bearer token for basic access control.
# If set, clients must provide either:
#   - Authorization: Bearer <token>
#   - ?token=<token>
ROI_WEB_TOKEN = _env_str("ROI_WEB_TOKEN", "")

# In-memory diagnostics ring buffer settings (used by the web UI).
ROI_WEB_DIAG_MAX_EVENTS = _env_int("ROI_WEB_DIAG_MAX_EVENTS", 250)
ROI_WEB_DIAG_DEDUPE_WINDOW_S = _env_float("ROI_WEB_DIAG_DEDUPE_WINDOW_S", 0.75)


# -----------------------------------------------------------------------------
# Dashboard / polling
# -----------------------------------------------------------------------------

# DASH_FPS controls only the Rich TUI render rate (it does NOT affect CAN).
DASH_FPS = _env_int("DASH_FPS", 15)

# Headless main-loop cadence (seconds). This does NOT affect instrument polling or CAN.
HEADLESS_LOOP_PERIOD_S = _env_float("HEADLESS_LOOP_PERIOD_S", 0.1)

# Instrument polling cadence (seconds). These govern how often values update on the dashboard
# and how frequently outgoing readback frames can change.
MEAS_POLL_PERIOD = _env_float("MEAS_POLL_PERIOD", 0.2)      # fast measurements (V/I, meter)
STATUS_POLL_PERIOD = _env_float("STATUS_POLL_PERIOD", 1.0)  # slow status (setpoints/mode)


# -----------------------------------------------------------------------------
# Instrument idle behavior
# -----------------------------------------------------------------------------

# E-load: idle means input off and short off.
ELOAD_IDLE_INPUT_ON = _env_bool("ELOAD_IDLE_INPUT_ON", False)
ELOAD_IDLE_SHORT_ON = _env_bool("ELOAD_IDLE_SHORT_ON", False)

# AFG: idle means output off.
AFG_IDLE_OUTPUT_ON = _env_bool("AFG_IDLE_OUTPUT_ON", False)

# MrSignal / LANYI MR2.0 (Modbus RTU over USB-serial)
MRSIGNAL_ENABLE = _env_bool("MRSIGNAL_ENABLE", True)

# Default is /dev/ttyUSB1 to avoid colliding with the multimeter default (/dev/ttyUSB0).
MRSIGNAL_PORT = _env_str("MRSIGNAL_PORT", "/dev/ttyUSB1")
MRSIGNAL_BAUD = _env_int("MRSIGNAL_BAUD", 9600)
MRSIGNAL_SLAVE_ID = _env_int("MRSIGNAL_SLAVE_ID", 1)
MRSIGNAL_PARITY = _env_str("MRSIGNAL_PARITY", "N")  # N/E/O
MRSIGNAL_STOPBITS = _env_int("MRSIGNAL_STOPBITS", 1)  # 1 or 2
MRSIGNAL_TIMEOUT = _env_float("MRSIGNAL_TIMEOUT", 0.5)

# Float byteorder handling (minimalmodbus varies between versions/devices)
# Examples: BYTEORDER_BIG, BYTEORDER_LITTLE, BYTEORDER_BIG_SWAP, BYTEORDER_LITTLE_SWAP
MRSIGNAL_FLOAT_BYTEORDER = _env_str("MRSIGNAL_FLOAT_BYTEORDER", "")
MRSIGNAL_FLOAT_BYTEORDER_AUTO = _env_bool("MRSIGNAL_FLOAT_BYTEORDER_AUTO", True)

# Safety clamps (applied to incoming CAN setpoints)
MRSIGNAL_MAX_V = _env_float("MRSIGNAL_MAX_V", 24.0)
MRSIGNAL_MAX_MA = _env_float("MRSIGNAL_MAX_MA", 24.0)

# Idle behavior: output OFF by default (safety)
MRSIGNAL_IDLE_OUTPUT_ON = _env_bool("MRSIGNAL_IDLE_OUTPUT_ON", False)

# Watchdog timeout (seconds)
MRSIGNAL_TIMEOUT_SEC = _env_float("MRSIGNAL_TIMEOUT_SEC", CONTROL_TIMEOUT_SEC)

# Poll cadence for status/input reads (seconds)
MRSIGNAL_POLL_PERIOD = _env_float("MRSIGNAL_POLL_PERIOD", STATUS_POLL_PERIOD)


# -----------------------------------------------------------------------------
# CAN IDs
# -----------------------------------------------------------------------------

# --- CAN IDs (Control) ---
LOAD_CTRL_ID = 0x0CFF0400
# Relay control (CTRL_RLY in PAT.dbc). Keep this aligned with sender DBC.
RLY_CTRL_ID = 0x0CFF0500
MMETER_CTRL_ID = 0x0CFF0600
MMETER_CTRL_EXT_ID = 0x0CFF0601  # Extended multimeter control (op-code based)
AFG_CTRL_ID = 0x0CFF0700  # Enable, Shape, Freq, Ampl
AFG_CTRL_EXT_ID = 0x0CFF0701  # Offset, Duty Cycle

# MrSignal control (enable/mode/value float)
MRSIGNAL_CTRL_ID = 0x0CFF0800

# --- CAN IDs (Readback) ---
ELOAD_READ_ID = 0x0CFF0003
MMETER_READ_ID = 0x0CFF0004
MMETER_READ_EXT_ID = 0x0CFF0009  # Float32 primary + Float32 secondary (NaN if absent)
MMETER_STATUS_ID = 0x0CFF000A    # Function/flags/status (byte-oriented)
AFG_READ_ID = 0x0CFF0005  # Status: Enable, Freq, Ampl
AFG_READ_EXT_ID = 0x0CFF0006  # Status: Offset, Duty Cycle

# MrSignal readback (optional)
MRSIGNAL_READ_STATUS_ID = 0x0CFF0007
MRSIGNAL_READ_INPUT_ID = 0x0CFF0008


# -----------------------------------------------------------------------------
# CAN bus load estimator (dashboard)
# -----------------------------------------------------------------------------

# Enabled by default; set to 0 to hide/disable bus load calculation.
CAN_BUS_LOAD_ENABLE = _env_bool("CAN_BUS_LOAD_ENABLE", True)

# Sliding window for the estimator (seconds).
CAN_BUS_LOAD_WINDOW_SEC = _env_float("CAN_BUS_LOAD_WINDOW_SEC", 1.0)

# Physical-layer bit stuffing increases actual bits on-wire; 1.2 is a reasonable heuristic.
CAN_BUS_LOAD_STUFFING_FACTOR = _env_float("CAN_BUS_LOAD_STUFFING_FACTOR", 1.2)

# Exponential smoothing for the displayed bus load percent. 0.0 disables.
CAN_BUS_LOAD_SMOOTH_ALPHA = _env_float("CAN_BUS_LOAD_SMOOTH_ALPHA", 0.25)

# Approximate overhead bits per classic CAN frame excluding data (SOF..IFS). This is an estimate.
CAN_BUS_LOAD_OVERHEAD_BITS = _env_int("CAN_BUS_LOAD_OVERHEAD_BITS", 48)


# -----------------------------------------------------------------------------
# CAN transmit behavior
# -----------------------------------------------------------------------------

# Regulate outgoing readback frames (ELOAD/MMETER/AFG status) to a fixed rate.
CAN_TX_ENABLE = _env_bool("CAN_TX_ENABLE", True)
CAN_TX_PERIOD_MS = _env_int("CAN_TX_PERIOD_MS", 50)

# Advanced per-frame transmit periods (milliseconds).
# These allow reducing CAN traffic without changing the overall TX scheduler tick.
# By default they inherit CAN_TX_PERIOD_MS (legacy behavior).
CAN_TX_PERIOD_MMETER_LEGACY_MS = _env_int("CAN_TX_PERIOD_MMETER_LEGACY_MS", CAN_TX_PERIOD_MS)
CAN_TX_PERIOD_MMETER_EXT_MS = _env_int("CAN_TX_PERIOD_MMETER_EXT_MS", CAN_TX_PERIOD_MS)
CAN_TX_PERIOD_MMETER_STATUS_MS = _env_int("CAN_TX_PERIOD_MMETER_STATUS_MS", CAN_TX_PERIOD_MS)
CAN_TX_PERIOD_ELOAD_MS = _env_int("CAN_TX_PERIOD_ELOAD_MS", CAN_TX_PERIOD_MS)
CAN_TX_PERIOD_AFG_EXT_MS = _env_int("CAN_TX_PERIOD_AFG_EXT_MS", CAN_TX_PERIOD_MS)
CAN_TX_PERIOD_MRS_STATUS_MS = _env_int("CAN_TX_PERIOD_MRS_STATUS_MS", CAN_TX_PERIOD_MS)
CAN_TX_PERIOD_MRS_INPUT_MS = _env_int("CAN_TX_PERIOD_MRS_INPUT_MS", CAN_TX_PERIOD_MS)

# Optional: when enabled, a frame is also sent immediately when its payload changes
# (still rate-limited by CAN_TX_SEND_ON_CHANGE_MIN_MS). Periodic keepalive still applies.
CAN_TX_SEND_ON_CHANGE = _env_bool("CAN_TX_SEND_ON_CHANGE", False)
CAN_TX_SEND_ON_CHANGE_MIN_MS = _env_int("CAN_TX_SEND_ON_CHANGE_MIN_MS", 0)

# -----------------------------------------------------------------------------
# CAN receive filtering (optional performance/CPU optimization)
# -----------------------------------------------------------------------------
# When set, ROI will attempt to apply kernel/driver-level filters (cbus.set_filters)
# so only relevant frames are delivered to the Python process.
#
# Values:
#   - "none"        : do not apply filters (default; preserves accurate bus-load meter)
#   - "control"     : only deliver ROI control IDs
#   - "control+pat" : control IDs + PAT_J0..PAT_J5 frames for the dashboard
CAN_RX_KERNEL_FILTER_MODE = _env_str("CAN_RX_KERNEL_FILTER_MODE", "none").strip().lower()

# -----------------------------------------------------------------------------
# RM/Proemion CANview serial backend tuning
# -----------------------------------------------------------------------------
# pyserial.flush() waits for the OS serial TX buffer to drain; doing this on every
# frame can severely limit throughput. Keep default True for backward compatibility.
CAN_RMCANVIEW_FLUSH_EVERY_SEND = _env_bool("CAN_RMCANVIEW_FLUSH_EVERY_SEND", True)
