# Configuration

ROI defaults live in `src/roi/config.py`. Every value can be overridden via
environment variables.

For Raspberry Pi + systemd installs, use:

- `/etc/roi/roi.env`

The service unit loads that file automatically.

## Quick Start Settings

### Build tag

```bash
ROI_BUILD_TAG=lab-pi-01
```

### CAN backend

SocketCAN (default):

```bash
CAN_INTERFACE=socketcan
CAN_CHANNEL=can0
CAN_BITRATE=250000
CAN_SETUP=1
```

RM/Proemion CANview:

```bash
CAN_INTERFACE=rmcanview
CAN_CHANNEL=/dev/serial/by-id/<your-canview>
CAN_SERIAL_BAUD=115200
CAN_BITRATE=250000
CAN_SETUP=1
CAN_CLEAR_ERRORS_ON_INIT=1
```

### TX cadence and traffic shaping

```bash
CAN_TX_ENABLE=1
CAN_TX_PERIOD_MS=50

CAN_TX_PERIOD_MMETER_LEGACY_MS=200
CAN_TX_PERIOD_MMETER_EXT_MS=200
CAN_TX_PERIOD_MMETER_STATUS_MS=200
CAN_TX_PERIOD_ELOAD_MS=200
CAN_TX_PERIOD_AFG_EXT_MS=1000
CAN_TX_PERIOD_MRS_STATUS_MS=1000
CAN_TX_PERIOD_MRS_INPUT_MS=1000

CAN_TX_SEND_ON_CHANGE=1
CAN_TX_SEND_ON_CHANGE_MIN_MS=50
```

### RX filtering and rmcanview tuning

```bash
CAN_RX_KERNEL_FILTER_MODE=control
# or control+pat

CAN_RMCANVIEW_FLUSH_EVERY_SEND=0
```

### Device auto-detection

```bash
AUTO_DETECT_ENABLE=1
AUTO_DETECT_VERBOSE=1
AUTO_DETECT_PREFER_BY_ID=1
AUTO_DETECT_BYID_ONLY=1

# Prefer PCAN (SocketCAN netdev) when both PCAN and CANview are present
AUTO_DETECT_PCAN=1
AUTO_DETECT_PCAN_USB_IDS=0c72:000c
AUTO_DETECT_PCAN_PREFER_CHANNEL=can0

AUTO_DETECT_MMETER_BYID_HINTS=5491b,multimeter
AUTO_DETECT_MRSIGNAL_BYID_HINTS=mr.signal,lanyi
AUTO_DETECT_K1_BYID_HINTS=dsd,dsdtech,relay,cp2102
AUTO_DETECT_K1_BYID_EXCLUDE_HINTS=mr.signal,lanyi,mrsignal,multimeter,5491,canview,proemion,afg
AUTO_DETECT_CANVIEW_BYID_HINTS=canview,proemion
```

ASRL probing controls:

```bash
AUTO_DETECT_VISA_PROBE_ASRL=1
AUTO_DETECT_VISA_ASRL_ALLOW_PREFIXES=/dev/ttyACM
AUTO_DETECT_VISA_ASRL_EXCLUDE_PREFIXES=/dev/ttyAMA,/dev/ttyS,/dev/ttyUSB
```

### VISA and explicit ports

```bash
VISA_BACKEND=@py
VISA_TIMEOUT_MS=500

MULTI_METER_PATH=/dev/ttyUSB0
MRSIGNAL_PORT=/dev/ttyUSB1
K1_SERIAL_PORT=/dev/ttyACM0
K1_CHANNEL_COUNT=1
K1_BACKEND=dsdtech
ELOAD_VISA_ID=USB0::...
AFG_VISA_ID=USB0::...
```

Legacy `K1_BACKEND=auto|serial|gpio` values are accepted for compatibility and
treated as `dsdtech`.

Relay CAN control ID stays fixed at `0x0CFF0500` (`CTRL_RLY` in `PAT.dbc`).

## Runtime and Safety Settings

### Watchdog and loop timing

```bash
CONTROL_TIMEOUT_SEC=2.0
WATCHDOG_GRACE_SEC=0.25
CAN_TIMEOUT_SEC=2.0

HEADLESS_LOOP_PERIOD_S=0.1
MEAS_POLL_PERIOD=0.2
STATUS_POLL_PERIOD=1.0
```

### Queue and buffering controls

```bash
CAN_CMD_QUEUE_MAX=256
CAN_RMCANVIEW_RX_MAX=2048
```

### Bus-load display tuning

```bash
CAN_BUS_LOAD_ENABLE=1
CAN_BUS_LOAD_WINDOW_SEC=1.0
CAN_BUS_LOAD_STUFFING_FACTOR=1.2
CAN_BUS_LOAD_SMOOTH_ALPHA=0.25
CAN_BUS_LOAD_OVERHEAD_BITS=48
```

### Multimeter behavior controls

```bash
MMETER_SCPI_STYLE=auto
MMETER_CONTROL_SETTLE_SEC=0.30
MMETER_DEBUG=0
MMETER_CLEAR_ERRORS_ON_STARTUP=1
```

### Web dashboard controls

```bash
ROI_WEB_ENABLE=0
ROI_WEB_HOST=0.0.0.0
ROI_WEB_PORT=8080
ROI_WEB_TOKEN=
ROI_WEB_DIAG_MAX_EVENTS=250
ROI_WEB_DIAG_DEDUPE_WINDOW_S=0.75
```

## Notes

- Start with CAN settings and only the device settings you need.
- If you are unsure about a variable, check `src/roi/config.py` first.
