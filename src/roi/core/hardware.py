# hardware.py

from __future__ import annotations

import fnmatch
import glob
import math
import threading
import time
from typing import Callable, Optional

import pyvisa
import serial
import os

from .. import config
from ..devices.bk5491b import BK5491B, MmeterFunc
from ..devices.mrsignal import MrSignalClient, MrSignalStatus
from ..devices.usbtmc_file import UsbTmcFileInstrument, UsbTmcError


class _NullRelay:
    """Fallback relay implementation used when the K1 relay interface is unavailable.

    Provides a minimal relay interface (on/off/is_lit/pin) so the rest of the
    application can run in dev environments, containers, or hosts that do not
    have the K1 relay controller attached.
    """

    def __init__(self, initial_drive: bool = False, *, channels: int = 1):
        try:
            n = int(channels)
        except Exception:
            n = 1
        self._channels = max(1, min(4, n))
        self._states = [False] * self._channels
        self._states[0] = bool(initial_drive)

    @property
    def is_lit(self) -> bool:
        return self.get_channel(1)

    @property
    def channels(self) -> int:
        return int(self._channels)

    def on(self) -> None:
        self.set_channel(1, True)

    def off(self) -> None:
        self.set_channel(1, False)

    def set_channel(self, channel: int, drive_on: bool) -> None:
        try:
            idx = int(channel) - 1
        except Exception:
            return
        if idx < 0 or idx >= self._channels:
            return
        self._states[idx] = bool(drive_on)

    def get_channel(self, channel: int) -> bool:
        try:
            idx = int(channel) - 1
        except Exception:
            return False
        if idx < 0 or idx >= self._channels:
            return False
        return bool(self._states[idx])

    def get_pin_level(self, channel: int = 1):
        return None

    @property
    def pin(self):
        return None


class _SerialRelay:
    """Relay backend that toggles a channel via a USB-serial ASCII protocol.

    A command builder translates logical channel + state into bytes to write.

    Interface remains LED-like for channel 1 (on/off/is_lit/pin) while also
    exposing set_channel/get_channel for K1..K4.
    """

    def __init__(
        self,
        port: str,
        *,
        baud: int = 9600,
        command_for: Callable[[int, bool], bytes],
        channels: int = 1,
        initial_drive: bool = False,
        boot_delay_s: float = 2.0,
        timeout_s: float = 0.5,
    ):
        self._port = str(port)
        self._baud = int(baud)
        self._command_for = command_for
        try:
            n = int(channels)
        except Exception:
            n = 1
        self._channels = max(1, min(4, n))
        self._states = [False] * self._channels
        self._states[0] = bool(initial_drive)
        self._lock = threading.Lock()

        self.ser = serial.Serial(
            self._port,
            self._baud,
            timeout=float(timeout_s),
            write_timeout=float(timeout_s),
        )

        # Many Arduino-class boards either reset on open or need a moment to
        # finish USB enumeration / setup(). Keep it configurable.
        if boot_delay_s and boot_delay_s > 0:
            time.sleep(float(boot_delay_s))

        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass

        # Apply the initial drive state so the software and hardware match.
        self._apply(1, self._states[0], force=True)

    def _write(self, payload: bytes) -> None:
        if not payload:
            return
        with self._lock:
            self.ser.write(payload)
            try:
                self.ser.flush()
            except Exception:
                pass

    def _apply(self, channel: int, drive_on: bool, *, force: bool = False) -> None:
        try:
            idx = int(channel)
        except Exception:
            return
        if idx < 1 or idx > self._channels:
            return
        if (not force) and (self._states[idx - 1] == bool(drive_on)):
            return
        try:
            payload = self._command_for(int(idx), bool(drive_on))
            self._write(payload)
            self._states[idx - 1] = bool(drive_on)
        except Exception as e:
            # Keep running; device may be unplugged. Caller will see the log.
            print(f"WARNING: K relay serial write failed ({self._port}, K{idx}): {e}")

    @property
    def is_lit(self) -> bool:
        return self.get_channel(1)

    @property
    def channels(self) -> int:
        return int(self._channels)

    def on(self) -> None:
        self.set_channel(1, True)

    def off(self) -> None:
        self.set_channel(1, False)

    def set_channel(self, channel: int, drive_on: bool) -> None:
        self._apply(channel, drive_on)

    def get_channel(self, channel: int) -> bool:
        try:
            idx = int(channel) - 1
        except Exception:
            return False
        if idx < 0 or idx >= self._channels:
            return False
        return bool(self._states[idx])

    def get_pin_level(self, channel: int = 1):
        return None

    @property
    def pin(self):
        return None


def _clamp_i16(x: int) -> int:
    if x < -32768:
        return -32768
    if x > 32767:
        return 32767
    return x


def _relay_auto_backend_order(channel_count: int) -> tuple[str, str]:
    """Return preferred backend order for K1_BACKEND=auto."""

    try:
        n = int(channel_count)
    except Exception:
        n = 1
    # Multi-channel relay controllers are commonly DSD Tech AT-command devices.
    if n > 1:
        return ("dsdtech", "serial")
    return ("serial", "dsdtech")


class HardwareManager:
    """Manages communication and state for the e-load, multimeter, AFG, and relay."""

    def __init__(self):
        # --- State Variables ---
        # e-load
        self.e_load_enabled: int = 0
        self.e_load_mode: int = 0
        self.e_load_short: int = 0
        self.e_load_csetting: int = 0
        self.e_load_rsetting: int = 0

        # multimeter
        self.multi_meter: Optional[serial.Serial] = None
        # Higher-level helper (preferred)
        self.mmeter: BK5491B | None = None
        self.multi_meter_mode: int = 0
        self.multi_meter_range: int = 0
        self.mmeter_id: Optional[str] = None
        # Determined at runtime by the polling thread (see main.py).
        self.mmeter_fetch_cmd: Optional[str] = None
        # When control commands change meter mode/range, we pause background polling
        # until this monotonic timestamp.
        self.mmeter_quiet_until: float = 0.0

        # Expanded DMM state (used for CAN status/readback)
        self.mmeter_func: int = int(MmeterFunc.VDC)
        self.mmeter_autorange: bool = True
        self.mmeter_range_value: float = 0.0
        self.mmeter_nplc: float = 1.0
        self.mmeter_func2: int = int(MmeterFunc.VDC)
        self.mmeter_func2_enabled: bool = False
        self.mmeter_rel_enabled: bool = False
        self.mmeter_trig_source: int = 0  # 0=IMM,1=BUS,2=MAN

        # SCPI command dialect for the multimeter.
        #
        # For the 2831E/5491B family, the documented / recommended dialect is
        # the classic tree rooted at :FUNCtion (plus :FUNCtion2 for secondary
        # display on newer firmware). Default is "auto" (choose a working dialect).
        try:
            self.mmeter_scpi_style: str = str(getattr(config, "MMETER_SCPI_STYLE", "func") or "func").strip().lower()
        except Exception:
            self.mmeter_scpi_style = "func"

        # AFG
        self.afg = None
        self.afg_id: Optional[str] = None
        self.afg_output: bool = False
        self.afg_shape: int = 0  # 0=SIN, 1=SQU, 2=RAMP
        self.afg_freq: int = 1000
        self.afg_ampl: int = 1000  # mVpp
        self.afg_offset: int = 0  # mV
        self.afg_duty: int = 50  # %

        # MrSignal (MR2.0) via Modbus RTU over USB-serial
        self.mrsignal: MrSignalClient | None = None
        self.mrsignal_id: Optional[int] = None
        self.mrsignal_output_on: bool = False
        self.mrsignal_output_select: int = 1  # default V
        self.mrsignal_output_value: float = 0.0
        self.mrsignal_input_value: float = 0.0
        self.mrsignal_float_byteorder: str = "DEFAULT"

        # Last commanded values (to suppress redundant Modbus writes)
        self._mrs_last_enable: Optional[bool] = None
        self._mrs_last_select: Optional[int] = None
        self._mrs_last_value: Optional[float] = None

        # VISA
        self.resource_manager = None
        self.e_load = None
        self.e_load_id: Optional[str] = None
        self.e_load_resource: Optional[str] = None

        # Locks (Thread Safety)
        self.eload_lock = threading.Lock()
        self.mmeter_lock = threading.Lock()
        self.afg_lock = threading.Lock()

        self.mrsignal_lock = threading.Lock()

        # --- K1 Relay ---
        # K1 is treated as a direct drive output. We intentionally do not infer "DUT power"
        # from contact wiring (NC/NO). If you need true DUT power status, measure it.
        initial_drive = bool(getattr(config, "K1_IDLE_DRIVE", False))
        try:
            relay_channels_cfg = int(getattr(config, "K1_CHANNEL_COUNT", 1) or 1)
        except Exception:
            relay_channels_cfg = 1
        self.relay_channel_count = max(1, min(4, relay_channels_cfg))

        self.relay_backend: str = "disabled"

        # Respect the legacy enable switch first.
        if not bool(getattr(config, "K1_ENABLE", True)):
            self.relay = _NullRelay(initial_drive, channels=self.relay_channel_count)
            self.relay_backend = "disabled"
        else:
            backend = str(getattr(config, "K1_BACKEND", "auto") or "auto").strip().lower()
            try:
                relay_init_retries = int(getattr(config, "K1_INIT_RETRIES", 3) or 3)
            except Exception:
                relay_init_retries = 3
            relay_init_retries = max(1, relay_init_retries)
            try:
                relay_init_retry_delay = float(getattr(config, "K1_INIT_RETRY_DELAY_SEC", 0.25) or 0.0)
            except Exception:
                relay_init_retry_delay = 0.25
            relay_init_retry_delay = max(0.0, relay_init_retry_delay)

            # Helper: construct the serial relay (Arduino controller)
            def _try_serial(*, fatal_on_fail: bool = False) -> bool:
                port = str(getattr(config, "K1_SERIAL_PORT", "") or "").strip()
                if not port:
                    if fatal_on_fail:
                        raise RuntimeError("K1 serial relay requested but K1_SERIAL_PORT is empty.")
                    return False

                start_idx = int(getattr(config, "K1_SERIAL_RELAY_INDEX", 1) or 1)
                start_idx = max(1, min(8, start_idx))
                channels = int(self.relay_channel_count)

                on_char = str(getattr(config, "K1_SERIAL_ON_CHAR", "") or "").strip()
                off_char = str(getattr(config, "K1_SERIAL_OFF_CHAR", "") or "").strip()
                if channels > 1 and (on_char or off_char):
                    print("WARNING: K1_SERIAL_ON_CHAR/OFF_CHAR only apply when K1_CHANNEL_COUNT=1; ignoring overrides.")
                    on_char = ""
                    off_char = ""

                baud = int(getattr(config, "K1_SERIAL_BAUD", 9600) or 9600)
                boot_delay = float(getattr(config, "K1_SERIAL_BOOT_DELAY_SEC", 2.0) or 0.0)

                def _cmd_for(channel: int, drive_on: bool) -> bytes:
                    idx = start_idx + int(channel) - 1
                    idx = max(1, min(8, idx))

                    if channels == 1 and (on_char or off_char):
                        on_s = on_char or str(idx)
                        off_s = off_char or chr(ord("a") + idx - 1)
                        token = on_s if drive_on else off_s
                    else:
                        token = str(idx) if drive_on else chr(ord("a") + idx - 1)

                    return str(token).encode("ascii", errors="ignore")

                last_error = None
                for attempt in range(1, relay_init_retries + 1):
                    try:
                        self.relay = _SerialRelay(
                            port,
                            baud=baud,
                            command_for=_cmd_for,
                            channels=channels,
                            initial_drive=bool(initial_drive),
                            boot_delay_s=boot_delay,
                        )
                        self.relay_backend = "serial"
                        print(f"K relay: serial backend on {port} (K1->{start_idx}, channels={channels})")
                        return True
                    except Exception as e:
                        last_error = e
                        if attempt < relay_init_retries:
                            print(
                                f"WARNING: K1 serial relay init failed ({port}); "
                                f"retry {attempt}/{relay_init_retries} in {relay_init_retry_delay:.3f}s. ({e})"
                            )
                            if relay_init_retry_delay > 0:
                                time.sleep(relay_init_retry_delay)

                if fatal_on_fail:
                    raise RuntimeError(
                        f"K1 serial relay unavailable ({port}) after {relay_init_retries} attempt(s): {last_error}"
                    )

                print(f"WARNING: K1 serial relay unavailable ({port}); running with a mock relay. ({last_error})")
                return False

            def _try_dsdtech(*, fatal_on_fail: bool = False) -> bool:
                port = str(getattr(config, "K1_SERIAL_PORT", "") or "").strip()
                if not port:
                    if fatal_on_fail:
                        raise RuntimeError("K1 dsdtech relay requested but K1_SERIAL_PORT is empty.")
                    return False

                channels = int(self.relay_channel_count)
                base_idx = int(getattr(config, "K1_DSDTECH_CHANNEL", 1) or 1)
                max_base = max(1, 4 - channels + 1)
                base_idx = max(1, min(max_base, base_idx))
                baud = int(getattr(config, "K1_DSDTECH_BAUD", getattr(config, "K1_SERIAL_BAUD", 9600)) or 9600)
                boot_delay = float(getattr(config, "K1_DSDTECH_BOOT_DELAY_SEC", 0.2) or 0.0)
                template = str(getattr(config, "K1_DSDTECH_CMD_TEMPLATE", "AT+CH{index}={state}") or "AT+CH{index}={state}")
                suffix_raw = str(getattr(config, "K1_DSDTECH_CMD_SUFFIX", r"\r\n") or "")
                try:
                    suffix = bytes(suffix_raw, "utf-8").decode("unicode_escape")
                except Exception:
                    suffix = suffix_raw

                def _cmd_for(channel: int, drive_on: bool) -> bytes:
                    idx = base_idx + int(channel) - 1
                    idx = max(1, min(4, idx))
                    state = 1 if bool(drive_on) else 0
                    try:
                        cmd = template.format(index=idx, state=state)
                    except Exception:
                        cmd = f"AT+CH{idx}={state}"
                    return (str(cmd) + str(suffix)).encode("ascii", errors="ignore")

                last_error = None
                for attempt in range(1, relay_init_retries + 1):
                    try:
                        self.relay = _SerialRelay(
                            port,
                            baud=baud,
                            command_for=_cmd_for,
                            channels=channels,
                            initial_drive=bool(initial_drive),
                            boot_delay_s=boot_delay,
                        )
                        self.relay_backend = "dsdtech"
                        print(
                            f"K relay: dsdtech backend on {port} "
                            f"(K1->{base_idx}, channels={channels}, template='{template}')"
                        )
                        return True
                    except Exception as e:
                        last_error = e
                        if attempt < relay_init_retries:
                            print(
                                f"WARNING: K1 dsdtech relay init failed ({port}); "
                                f"retry {attempt}/{relay_init_retries} in {relay_init_retry_delay:.3f}s. ({e})"
                            )
                            if relay_init_retry_delay > 0:
                                time.sleep(relay_init_retry_delay)

                if fatal_on_fail:
                    raise RuntimeError(
                        f"K1 dsdtech relay unavailable ({port}) after {relay_init_retries} attempt(s): {last_error}"
                    )

                print(f"WARNING: K1 dsdtech relay unavailable ({port}); running with a mock relay. ({last_error})")
                return False

            # Backend selection
            if backend == "disabled":
                self.relay = _NullRelay(initial_drive, channels=self.relay_channel_count)
                self.relay_backend = "disabled"
            elif backend == "mock":
                self.relay = _NullRelay(initial_drive, channels=self.relay_channel_count)
                self.relay_backend = "mock"
            elif backend == "serial":
                if not _try_serial(fatal_on_fail=True):
                    self.relay = _NullRelay(initial_drive, channels=self.relay_channel_count)
                    self.relay_backend = "mock"
            elif backend == "dsdtech":
                if not _try_dsdtech(fatal_on_fail=True):
                    self.relay = _NullRelay(initial_drive, channels=self.relay_channel_count)
                    self.relay_backend = "mock"
            elif backend == "gpio":
                # GPIO relay-hat support was removed. Treat this as a request for
                # the standard K1 serial interface.
                print("WARNING: K1_BACKEND='gpio' is no longer supported; using serial instead.")
                if not _try_serial(fatal_on_fail=True):
                    self.relay = _NullRelay(initial_drive, channels=self.relay_channel_count)
                    self.relay_backend = "mock"
            else:
                # auto: choose a sensible default order and fall back to the
                # other supported serial protocol.
                order = _relay_auto_backend_order(self.relay_channel_count)
                ok = False
                for candidate in order:
                    if candidate == "serial":
                        ok = _try_serial(fatal_on_fail=False)
                    elif candidate == "dsdtech":
                        ok = _try_dsdtech(fatal_on_fail=False)
                    if ok:
                        break
                if not ok:
                    tried = ",".join(order)
                    print(
                        f"WARNING: K1 relay auto backend failed (tried {tried}); "
                        "set K1_BACKEND/K1_SERIAL_PORT (or disable K1). Using mock relay."
                    )
                    self.relay = _NullRelay(initial_drive, channels=self.relay_channel_count)
                    self.relay_backend = "mock"


    def _maybe_detect_mmeter_scpi_style(self) -> None:
        """One-time SCPI dialect detection for the 5491B.

        Some 5491/5492 command sets use :CONFigure (CONF:...) while others use
        :FUNCtion + per-subsystem trees. Sending the wrong style can make the
        meter display "BUS: BAD COMMAND".
        """

        style = str(getattr(self, "mmeter_scpi_style", "auto") or "auto").strip().lower()
        if style not in ("conf", "func", "auto"):
            style = "auto"

        # If explicitly configured, honor it.
        if style != "auto":
            self.mmeter_scpi_style = style
            print(f"MMETER SCPI style: {self.mmeter_scpi_style}")
            return

        helper = getattr(self, "mmeter", None)
        if helper is None:
            # Can't probe; default to the more backwards-compatible dialect.
            self.mmeter_scpi_style = "conf"
            print(f"MMETER SCPI style: {self.mmeter_scpi_style} (default)")
            return

        # If IDN indicates the known 2831E/5491B family, prefer FUNC-style
        # directly to avoid sending a potentially-invalid probe first.
        idn_u = str(getattr(self, "mmeter_id", "") or "").upper()
        if ("5491" in idn_u) or ("2831" in idn_u):
            self.mmeter_scpi_style = "func"
            print(f"MMETER SCPI style: {self.mmeter_scpi_style} (idn)")
            return

        # 1) Try FUNC-style query first (classic SCPI tree for this family).
        resp = ""
        try:
            resp = helper.query_line(":FUNCtion?", delay_s=0.05, read_lines=6)
        except Exception:
            resp = ""
        r = (resp or "").upper()
        if any(tok in r for tok in ("VOLT", "CURR", "RES", "FREQ", "PER", "DIO", "CONT")):
            self.mmeter_scpi_style = "func"
            print(f"MMETER SCPI style: {self.mmeter_scpi_style} (auto)")
            return

        # 2) Then try CONF-style query (legacy/alternate firmware).
        resp = ""
        try:
            resp = helper.query_line(":CONFigure:FUNCtion?", delay_s=0.05, read_lines=6)
        except Exception:
            resp = ""
        r = (resp or "").upper()
        if any(tok in r for tok in ("DCV", "ACV", "DCA", "ACA", "HZ", "RES", "DIOC", "NONE")):
            self.mmeter_scpi_style = "conf"
            print(f"MMETER SCPI style: {self.mmeter_scpi_style} (auto)")
            return

        # Fallback
        self.mmeter_scpi_style = "func"
        print(f"MMETER SCPI style: {self.mmeter_scpi_style} (auto default)")

    # --- Relay helpers ---
    def get_k_channel_count(self) -> int:
        try:
            n = int(getattr(self.relay, "channels", getattr(self, "relay_channel_count", 1)) or 1)
        except Exception:
            n = int(getattr(self, "relay_channel_count", 1) or 1)
        return max(1, min(4, n))

    def get_k_drive(self, channel: int = 1) -> bool:
        "Return the logical drive state for K<channel>."
        try:
            ch = int(channel)
        except Exception:
            return False
        try:
            if hasattr(self.relay, "get_channel"):
                return bool(self.relay.get_channel(ch))
        except Exception:
            pass
        if ch == 1:
            return bool(getattr(self.relay, "is_lit", False))
        return False

    def get_k_pin_level(self, channel: int = 1):
        "Return the raw pin level for K<channel> when available."
        try:
            ch = int(channel)
        except Exception:
            return None
        try:
            if hasattr(self.relay, "get_pin_level"):
                lvl = self.relay.get_pin_level(ch)
                return None if lvl is None else bool(lvl)
        except Exception:
            pass

        # Legacy single-pin path (channel 1 only).
        if ch != 1:
            return None
        try:
            pin = getattr(self.relay, "pin", None)
            if pin is None:
                return None
            if hasattr(pin, "state"):
                return bool(pin.state)
            if hasattr(pin, "value"):
                return bool(pin.value)
        except Exception:
            return None
        return None

    def set_k_drive(self, channel: int, drive_on: bool) -> None:
        "Set K<channel> drive directly (no DUT inference)."
        try:
            ch = int(channel)
        except Exception:
            return
        if ch < 1 or ch > self.get_k_channel_count():
            return

        if hasattr(self.relay, "set_channel"):
            self.relay.set_channel(ch, bool(drive_on))
            return

        # Legacy single-channel interface
        if ch == 1:
            if bool(drive_on):
                self.relay.on()
            else:
                self.relay.off()

    def set_k_idle_all(self) -> None:
        "Apply idle drive state to all configured K relay channels."
        idle = bool(getattr(config, "K1_IDLE_DRIVE", False))
        for ch in range(1, self.get_k_channel_count() + 1):
            self.set_k_drive(ch, idle)

    def get_k_relays_state(self) -> dict[int, dict[str, object]]:
        "Return per-channel relay state for dashboards/diagnostics."
        out: dict[int, dict[str, object]] = {}
        for ch in range(1, self.get_k_channel_count() + 1):
            try:
                drive = bool(self.get_k_drive(ch))
            except Exception:
                drive = False
            try:
                pin_level = self.get_k_pin_level(ch)
            except Exception:
                pin_level = None
            out[ch] = {"drive": drive, "pin_level": pin_level}
        return out

    # Backward-compatible K1 wrappers
    def get_k1_drive(self) -> bool:
        "Return the logical drive state we are commanding for K1 (ON/OFF)."
        return bool(self.get_k_drive(1))

    def get_k1_pin_level(self):
        "Return the raw pin level (True=HIGH, False=LOW) if available."
        return self.get_k_pin_level(1)

    def set_k1_drive(self, drive_on: bool) -> None:
        "Set K1 drive directly (no DUT inference)."
        self.set_k_drive(1, bool(drive_on))

    def set_k1_idle(self) -> None:
        "Apply idle drive state to all configured K relay channels."
        self.set_k_idle_all()





    def initialize_devices(self) -> None:
        """Initializes the multi-meter, e-load, and AFG."""
        self._initialize_multimeter()
        self._initialize_visa_devices()
        self._initialize_mrsignal()

    def _initialize_multimeter(self) -> None:
        """Initialize the serial multimeter.

        Many USB-serial instruments will *echo* the command you send before
        replying with the actual IDN string. Also, some respond a beat later.
        We therefore read a few lines and skip obvious echoes.
        """
        def _seed_fetch_cmd_from_idn() -> None:
            """Pick a stable default fetch command for known 5491/2831 families.

            This avoids startup probe churn that can trigger front-panel BUS
            errors on some units.
            """
            try:
                idn_u = str(getattr(self, "mmeter_id", "") or "").upper()
                if ("5491" not in idn_u) and ("2831" not in idn_u):
                    return

                if str(getattr(self, "mmeter_fetch_cmd", "") or "").strip():
                    return

                cmds = [
                    c.strip()
                    for c in str(getattr(config, "MULTI_METER_FETCH_CMDS", ":FETCh?")).split(",")
                    if c.strip()
                ]
                if not cmds:
                    cmds = [":FETCh?"]

                # For 5491/2831 family, prefer the long FETCH form first.
                # This avoids startup "BUS: BAD COMMAND" on units that reject
                # short-form :FETC? but accept :FETCh?.
                for c in cmds:
                    cu = c.upper().replace(" ", "")
                    if "FETCH?" in cu:
                        self.mmeter_fetch_cmd = c
                        break
                else:
                    self.mmeter_fetch_cmd = str(cmds[0])
                print(f"MMETER fetch cmd: {self.mmeter_fetch_cmd} (idn)")
            except Exception:
                pass

        try:
            mmeter = serial.Serial(
                config.MULTI_METER_PATH,
                int(config.MULTI_METER_BAUD),
                timeout=float(getattr(config, 'MULTI_METER_TIMEOUT', 1.0)),
                write_timeout=float(getattr(config, 'MULTI_METER_WRITE_TIMEOUT', 1.0)),
            )
            # Clear any garbage that could cause decode issues.
            try:
                mmeter.reset_input_buffer()
                mmeter.reset_output_buffer()
            except Exception:
                pass

            # If device_discovery already identified the meter, avoid sending
            # another *IDN? immediately on boot unless verification is enabled.
            cached_idn = str(getattr(config, 'MULTI_METER_IDN', '') or '').strip()
            verify = bool(getattr(config, 'MULTI_METER_VERIFY_ON_STARTUP', False))
            if cached_idn and not verify:
                self.mmeter_id = cached_idn
                print(f"MULTI-METER ID: {self.mmeter_id}")
                self.multi_meter = mmeter
                try:
                    self.mmeter = BK5491B(mmeter, log_fn=print)
                except Exception:
                    self.mmeter = None

                # Clear any stale error queue entries so the front panel doesn't
                # keep showing a persistent BUS error after experimentation.
                try:
                    if bool(getattr(config, "MMETER_CLEAR_ERRORS_ON_STARTUP", True)) and self.mmeter is not None:
                        self.mmeter.drain_errors(log=True)
                except Exception:
                    pass

                _seed_fetch_cmd_from_idn()

                # Optional SCPI dialect detection (one-time) to avoid sending
                # commands the meter doesn't understand (prevents "BUS: BAD COMMAND").
                self._maybe_detect_mmeter_scpi_style()
                return

            # Query IDN and tolerate command echo.
            try:
                mmeter.write(b"*IDN?\n")
                mmeter.flush()
            except Exception:
                pass

            # Give the device a moment; many respond ~10-100ms later.
            time.sleep(float(getattr(config, 'MULTI_METER_IDN_DELAY', 0.05)))

            idn: Optional[str] = None
            for _ in range(int(getattr(config, 'MULTI_METER_IDN_READ_LINES', 4))):
                raw = mmeter.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                # Ignore the common echo patterns.
                if line.upper().startswith("*IDN?"):
                    continue
                # Some devices include stray prompts; prefer lines that look like an IDN.
                if ("," in line) or ("multimeter" in line.lower()) or ("5491" in line.lower()):
                    idn = line
                    break
                # Fallback: accept the first non-empty non-echo line.
                if idn is None:
                    idn = line

            self.mmeter_id = idn
            print(f"MULTI-METER ID: {self.mmeter_id or 'Unknown'}")
            self.multi_meter = mmeter
            try:
                self.mmeter = BK5491B(mmeter, log_fn=print)
            except Exception:
                self.mmeter = None

            try:
                if bool(getattr(config, "MMETER_CLEAR_ERRORS_ON_STARTUP", True)) and self.mmeter is not None:
                    self.mmeter.drain_errors(log=True)
            except Exception:
                pass

            _seed_fetch_cmd_from_idn()

            self._maybe_detect_mmeter_scpi_style()
        except (serial.SerialException, IOError) as e:
            print(f"Failed to communicate with multi-meter: {e}")
            self.multi_meter = None
            self.mmeter = None

    def _initialize_visa_devices(self) -> None:
        """Initializes both E-Load and AFG via PyVISA."""
        try:
            # Prefer a configured backend so discovery/runtime are consistent.
            # On Raspberry Pi this defaults to pyvisa-py (@py).
            backend = str(getattr(config, "VISA_BACKEND", "") or "").strip()
            try:
                if backend:
                    self.resource_manager = pyvisa.ResourceManager(backend)
                else:
                    self.resource_manager = pyvisa.ResourceManager()
            except Exception:
                # Best-effort fallback to pyvisa-py
                self.resource_manager = pyvisa.ResourceManager("@py")
                backend = "@py"

            if backend:
                print(f"[visa] backend: {backend}")

            # --- 1. E-LOAD (Scan for USBTMC / match pattern) ---
            try:
                available_resources = []
                usb_resources = []

                # USB enumeration can lag slightly at boot / after hotplug.
                retries = int(getattr(config, "VISA_ENUM_RETRIES", 3) or 1)
                delay_s = float(getattr(config, "VISA_ENUM_RETRY_DELAY_SEC", 0.5) or 0.0)
                for i in range(max(1, retries)):
                    try:
                        available_resources = list(self.resource_manager.list_resources())
                    except Exception:
                        available_resources = []

                    # CRITICAL SAFETY: Never probe ASRL resources when looking for an E-load.
                    # Many unrelated USB-serial devices show up as ASRL/dev/ttyUSB*::INSTR
                    # (including the 5491B DMM). Touching them via VISA can make them beep
                    # "bus command error" and stop responding. The E-load is USBTMC and
                    # should appear as a USB* resource.
                    usb_resources = [r for r in available_resources if str(r).startswith("USB")]
                    if usb_resources or (i >= max(1, retries) - 1):
                        break
                    if delay_s > 0:
                        time.sleep(delay_s)

                print(f"Scanning for E-Load in (USB only): {usb_resources}")

                eload_pat = str(getattr(config, "ELOAD_VISA_ID", "") or "").strip()
                eload_hints = [
                    t.strip().lower()
                    for t in str(getattr(config, "AUTO_DETECT_ELOAD_IDN_HINTS", "") or "").split(",")
                    if t.strip()
                ]

                def _is_specific_resource(pat: str) -> bool:
                    if not pat:
                        return False
                    # If there are any glob metacharacters, treat as a pattern.
                    return not any(ch in pat for ch in ("*", "?", "["))

                eload_specific = _is_specific_resource(eload_pat)

                if eload_specific:
                    # If config points at a specific VISA resource (e.g. from autodetect),
                    # trust it and connect directly. Do NOT require IDN hints here.
                    # Try the specific resource first, even if list_resources() is empty.
                    candidates = [eload_pat] if eload_pat else []
                    # Then fall back to scanning any other USB resources we can see.
                    if usb_resources:
                        candidates += [r for r in usb_resources if r != eload_pat]
                    print(f"E-Load target (direct): {eload_pat}")
                else:
                    # Pattern scan across all USB resources.
                    candidates = [r for r in usb_resources if (not eload_pat or fnmatch.fnmatch(r, eload_pat))]
                    print(f"E-Load scan: pattern={eload_pat or '*'} hints={eload_hints or 'none'}")

                for resource_id in candidates:
                    try:
                        dev = self.resource_manager.open_resource(resource_id)
                        # Bound I/O time so a slow/missing instrument doesn't stall controls.
                        try:
                            dev.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
                        except Exception:
                            pass
                        # USBTMC devices usually speak SCPI with newline termination.
                        try:
                            dev.read_termination = "\n"
                            dev.write_termination = "\n"
                        except Exception:
                            pass

                        dev_id = str(dev.query("*IDN?")).strip()

                        # Only enforce hints when doing a broad scan.
                        if (not eload_specific) and eload_hints:
                            low = (dev_id or "").lower()
                            if not any(h in low for h in eload_hints):
                                print(f"E-Load candidate rejected: {resource_id} -> {dev_id}")
                                try:
                                    dev.close()
                                except Exception:
                                    pass
                                continue

                        print(f"E-LOAD FOUND: {dev_id} @ {resource_id}")
                        # Keep these best-effort; some loads dislike *RST.
                        try:
                            dev.write("SYST:CLE")
                        except Exception:
                            pass
                        self.e_load = dev
                        self.e_load_id = dev_id or None
                        self.e_load_resource = str(resource_id)
                        break
                    except Exception as e:
                        print(f"Skip E-LOAD ({resource_id}): {e}")

                # --- Fallback: kernel USBTMC device nodes (/dev/usbtmc*) ---
                # If PyVISA can't enumerate USB resources (missing libusb / permissions),
                # we can still often talk to the load through the kernel driver.
                if not self.e_load:
                    usbtmc_nodes = sorted(glob.glob("/dev/usbtmc*"))
                    if usbtmc_nodes:
                        print(f"Attempting E-load fallback via /dev/usbtmc*: {usbtmc_nodes}")
                    for p in usbtmc_nodes:
                        dev2 = None
                        try:
                            dev2 = UsbTmcFileInstrument(p)
                            # Mirror the VISA timeout configuration.
                            try:
                                dev2.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
                            except Exception:
                                pass
                            dev_id = str(dev2.query("*IDN?")).strip()

                            if eload_hints:
                                low = (dev_id or "").lower()
                                if not any(h in low for h in eload_hints):
                                    print(f"E-Load USBTMC candidate rejected: {p} -> {dev_id}")
                                    try:
                                        dev2.close()
                                    except Exception:
                                        pass
                                    continue

                            print(f"E-LOAD FOUND (usbtmc): {dev_id} @ {p}")
                            try:
                                dev2.write("SYST:CLE")
                            except Exception:
                                pass
                            self.e_load = dev2
                            self.e_load_id = dev_id or None
                            self.e_load_resource = str(p)
                            break
                        except Exception as e:
                            # Best-effort cleanup
                            try:
                                if dev2 is not None:
                                    dev2.close()
                            except Exception:
                                pass
                            print(f"Skip E-LOAD USBTMC ({p}): {e}")
            except Exception as e:
                print(f"E-Load Scan Error: {e}")

            # --- 2. AFG (Direct Connect) ---
            try:
                print(f"Attempting AFG connection at {config.AFG_VISA_ID}...")
                afg_dev = self.resource_manager.open_resource(config.AFG_VISA_ID)
                # Bound I/O time so polling doesn't block control writes for long.
                try:
                    afg_dev.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
                except Exception:
                    pass
                # Some VISA backends expose serial config fields
                try:
                    afg_dev.baud_rate = 115200
                except Exception:
                    pass
                afg_dev.read_termination = "\n"
                afg_dev.write_termination = "\n"

                dev_id = afg_dev.query("*IDN?").strip()
                print(f"AFG FOUND: {dev_id}")
                self.afg = afg_dev
                self.afg_id = dev_id

            except Exception as e:
                print(f"AFG Connection Failed ({config.AFG_VISA_ID}): {e}")

            if not self.e_load:
                print("WARNING: E-LOAD not found.")
                # Provide actionable hints only when we're clearly missing USB.
                try:
                    if 'usb_resources' in locals() and not usb_resources:
                        # If PyUSB isn't installed, pyvisa-py can't enumerate USBTMC
                        # instruments at all, so the E-load will never show up as a
                        # USB* VISA resource.
                        missing_pyusb = False
                        try:
                            import usb  # type: ignore
                        except Exception:
                            missing_pyusb = True


                        # Build the message as a list of lines to avoid fragile implicit
                        # string concatenation (mixing implicit concatenation with a conditional
                        # "+ ..." is easy to get wrong).
                        lines = [
                            "[visa] No USB VISA resources were enumerated. If your e-load is connected via USBTMC, try:",
                            "  - Install OS deps: sudo ./scripts/pi_install.sh --easy   (or --install-os-deps)",
                        ]
                        if missing_pyusb:
                            lines.append("  - Install PyUSB: python3 -m pip install -U pyusb")
                        lines.extend(
                            [
                                "  - Check VISA backend + USB support: python3 -m pyvisa info",
                                "  - Check the USB device is visible: lsusb",
                                "  - If you installed new udev rules, unplug/replug the USB cable",
                            ]
                        )
                        print("\n".join(lines))
                except Exception:
                    pass
            if not self.afg:
                print("WARNING: AFG not found.")

        except Exception as e:
            print(f"Critical VISA Error: {e}")

    def _initialize_mrsignal(self) -> None:
        """Initialize MrSignal (LANYI MR2.0) Modbus RTU device if enabled.

        MrSignal is controlled via Modbus RTU over a USB-serial adapter and
        driven by CAN control frames handled by the receiver thread.
        """

        if not bool(getattr(config, "MRSIGNAL_ENABLE", False)):
            self.mrsignal = None
            return

        port = str(getattr(config, "MRSIGNAL_PORT", "") or "").strip()
        if not port:
            print("MrSignal disabled: MRSIGNAL_PORT is empty")
            self.mrsignal = None
            return

        try:
            client = MrSignalClient(
                port=port,
                slave_id=int(getattr(config, "MRSIGNAL_SLAVE_ID", 1)),
                baud=int(getattr(config, "MRSIGNAL_BAUD", 9600)),
                parity=str(getattr(config, "MRSIGNAL_PARITY", "N")),
                stopbits=int(getattr(config, "MRSIGNAL_STOPBITS", 1)),
                timeout_s=float(getattr(config, "MRSIGNAL_TIMEOUT", 0.5)),
                float_byteorder=(str(getattr(config, "MRSIGNAL_FLOAT_BYTEORDER", "") or "").strip() or None),
                float_byteorder_auto=bool(getattr(config, "MRSIGNAL_FLOAT_BYTEORDER_AUTO", True)),
            )
            client.connect()

            # Best-effort initial read so we can surface status immediately.
            st = client.read_status()

            self.mrsignal = client
            self.mrsignal_id = st.device_id
            self.mrsignal_output_on = bool(st.output_on) if st.output_on is not None else False
            self.mrsignal_output_select = int(st.output_select or 0)
            if st.output_value is not None:
                self.mrsignal_output_value = float(st.output_value)
            if st.input_value is not None:
                self.mrsignal_input_value = float(st.input_value)
            self.mrsignal_float_byteorder = str(st.float_byteorder or "DEFAULT")

            print(
                f"MrSignal FOUND: port={port} slave={getattr(client, 'slave_id', '?')} "
                f"id={self.mrsignal_id} mode={st.mode_label} bo={self.mrsignal_float_byteorder}"
            )

        except Exception as e:
            print(f"MrSignal connection failed ({port}): {e}")
            try:
                if self.mrsignal:
                    self.mrsignal.close()
            except Exception:
                pass
            self.mrsignal = None

    # --- Idle / shutdown helpers (used by watchdog) ---
    def apply_idle_eload(self) -> None:
        if not self.e_load:
            return
        try:
            with self.eload_lock:
                # Input off is the safety-critical part.
                self.e_load.write("INP ON" if config.ELOAD_IDLE_INPUT_ON else "INP OFF")
                self.e_load.write("INP:SHOR ON" if config.ELOAD_IDLE_SHORT_ON else "INP:SHOR OFF")
            self.e_load_enabled = 1 if config.ELOAD_IDLE_INPUT_ON else 0
            self.e_load_short = 1 if config.ELOAD_IDLE_SHORT_ON else 0
        except Exception:
            pass

    def apply_idle_afg(self) -> None:
        if not self.afg:
            return
        try:
            with self.afg_lock:
                try:
                    self.afg.write(f"OUTP1 {'ON' if config.AFG_IDLE_OUTPUT_ON else 'OFF'}")
                except Exception:
                    self.afg.write(f"SOUR1:OUTP {'ON' if config.AFG_IDLE_OUTPUT_ON else 'OFF'}")
            self.afg_output = bool(config.AFG_IDLE_OUTPUT_ON)
        except Exception:
            pass


    def apply_idle_mrsignal(self) -> None:
        if not self.mrsignal:
            return
        try:
            with self.mrsignal_lock:
                # Output enable is the safety-critical part.
                self.mrsignal.set_enable(bool(getattr(config, "MRSIGNAL_IDLE_OUTPUT_ON", False)))
            self.mrsignal_output_on = bool(getattr(config, "MRSIGNAL_IDLE_OUTPUT_ON", False))
        except Exception:
            pass


    def set_mrsignal(self, *, enable: bool, output_select: int, value: float,
                     max_v: float | None = None, max_ma: float | None = None) -> None:
        """Apply MrSignal control with safety clamps and redundant-write suppression."""
        if not self.mrsignal:
            return

        # Clamp setpoint based on mode (0=mA, 1=V, 4=mV, 6=24V)
        v = float(value)
        sel = int(output_select)

        if sel == 0:  # mA
            lim = float(max_ma if max_ma is not None else getattr(config, "MRSIGNAL_MAX_MA", 24.0))
            if v < 0.0:
                v = 0.0
            if v > lim:
                v = lim
        elif sel in (1, 6):  # V / 24V
            lim = float(max_v if max_v is not None else getattr(config, "MRSIGNAL_MAX_V", 24.0))
            if v < 0.0:
                v = 0.0
            if v > lim:
                v = lim
        elif sel == 4:  # mV
            lim = float(max_v if max_v is not None else getattr(config, "MRSIGNAL_MAX_V", 24.0)) * 1000.0
            if v < 0.0:
                v = 0.0
            if v > lim:
                v = lim
        # else: unknown mode, do minimal clamping
        if not math.isfinite(v):
            v = 0.0

        # Redundant suppression
        if (self._mrs_last_enable is not None and self._mrs_last_select is not None and self._mrs_last_value is not None):
            if (bool(enable) == bool(self._mrs_last_enable)) and (int(sel) == int(self._mrs_last_select)) and (abs(float(v) - float(self._mrs_last_value)) < 1e-6):
                return

        with self.mrsignal_lock:
            self.mrsignal.set_output(enable=bool(enable), output_select=int(sel), value=float(v))

        self._mrs_last_enable = bool(enable)
        self._mrs_last_select = int(sel)
        self._mrs_last_value = float(v)

        # Update last-known commanded state (dashboard uses polled values too)
        self.mrsignal_output_on = bool(enable)
        self.mrsignal_output_select = int(sel)
        self.mrsignal_output_value = float(v)

    def apply_idle_all(self) -> None:
        # Relay is always present
        try:
            self.set_k1_idle()
        except Exception:
            pass
        self.apply_idle_eload()
        self.apply_idle_afg()
        self.apply_idle_mrsignal()

    def close_devices(self) -> None:
        # Best-effort safety shutdown
        try:
            self.apply_idle_all()
        except Exception:
            pass

        if self.multi_meter:
            try:
                self.multi_meter.close()
            except Exception:
                pass
        if self.mrsignal:
            try:
                self.mrsignal.close()
            except Exception:
                pass
        if self.e_load:
            try:
                self.e_load.close()
            except Exception:
                pass
        if self.afg:
            try:
                self.afg.close()
            except Exception:
                pass
        if self.resource_manager:
            try:
                self.resource_manager.close()
            except Exception:
                pass
