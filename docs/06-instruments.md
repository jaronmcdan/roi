# Instruments

ROI is designed to run with any subset of supported instruments. Missing devices
should appear as unavailable without stopping the process.

## Multimeter (BK 2831E / 5491B class)

```bash
MULTI_METER_PATH=/dev/ttyUSB0
MULTI_METER_BAUD=38400
```

Recommendations:

- Prefer `/dev/serial/by-id/...` paths for stability.
- If probing causes device errors, use `AUTO_DETECT_BYID_ONLY=1`.

Diagnostics:

```bash
roi-mmter-diag
roi-mmter-diag --roi-cmds --style func
roi-autodetect-diag
```

Expected ROI meter settings/behavior:

- Serial link is dedicated to ROI (`/dev/serial/by-id/...`, 38400 baud, 8N1).
- ROI expects SCPI responses on newline termination.
- Preferred SCPI style for 5491B is `func`.
- ROI may change measurement function, range/auto-range, NPLC, relative mode,
  trigger source, and secondary display while tests run.
- Some 5491B firmware revisions support only a subset of these controls. Use
  `roi-mmter-diag --roi-cmds --style func` to identify unsupported commands and
  gate them with:
  - `MMETER_LEGACY_MODE0_ENABLE=0`
  - `MMETER_EXT_SET_RANGE_ENABLE=0`
  - `MMETER_EXT_SECONDARY_ENABLE=0`

## Electronic Load (VISA / USBTMC)

```bash
VISA_BACKEND=@py
ELOAD_VISA_ID=USB0::...::INSTR
```

Permissions:

- Pi installer option `--install-udev-rules` installs USBTMC rules.
- Running as root also works.

Diagnostics:

```bash
roi-visa-diag
```

## AFG / Function Generator (VISA)

```bash
VISA_BACKEND=@py
AFG_VISA_ID=USB0::...::INSTR
# or ASRL/dev/ttyACM0::INSTR
```

If using ASRL resources, tune auto-detect probing with:

- `AUTO_DETECT_BYID_ONLY`
- `AUTO_DETECT_VISA_PROBE_ASRL`
- `AUTO_DETECT_VISA_ASRL_ALLOW_PREFIXES`
- `AUTO_DETECT_VISA_ASRL_EXCLUDE_PREFIXES`

## K Relays (USB serial controller, K1..K4)

```bash
# Auto backend:
# - K1_CHANNEL_COUNT=1 -> serial first, then dsdtech fallback
# - K1_CHANNEL_COUNT>1 -> dsdtech first, then serial fallback
K1_BACKEND=auto
K1_SERIAL_PORT=/dev/serial/by-id/<your-relay>
K1_CHANNEL_COUNT=1

# Force legacy single-channel protocol (ON='1', OFF='a', etc.)
K1_BACKEND=serial
K1_SERIAL_PORT=/dev/serial/by-id/<your-relay>
K1_CHANNEL_COUNT=1
K1_SERIAL_BAUD=9600
K1_SERIAL_RELAY_INDEX=1

# Force DSD Tech SH-URxx AT protocol
# K1_BACKEND=dsdtech
# K1_CHANNEL_COUNT=4
# K1_DSDTECH_CMD_TEMPLATE=AT+CH{index}={state}
# K1_DSDTECH_CMD_SUFFIX=\r\n
```

Relay CAN control remains on `CTRL_RLY` ID `0x0CFF0500`:

- Byte0 bits 0..1: `K1` (non-zero = ON)
- Byte0 bits 2..3: `K2` (non-zero = ON)
- Byte0 bits 4..5: `K3` (non-zero = ON)
- Byte0 bits 6..7: `K4` (non-zero = ON)

## MrSignal / LANYI MR2.x Modbus PSU

```bash
MRSIGNAL_ENABLE=1
MRSIGNAL_PORT=/dev/ttyUSB1
MRSIGNAL_BAUD=9600
MRSIGNAL_SLAVE_ID=1
```

Notes:

- Setpoint clamps are enforced by `MRSIGNAL_MAX_V` and `MRSIGNAL_MAX_MA`.
- Float byteorder can be overridden if your unit requires it.

Diagnostics:

```bash
roi-mrsignal-diag --read-count 3
```
