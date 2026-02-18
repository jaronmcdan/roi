# device_comm.py

from __future__ import annotations

import queue
import struct
import time
import math
import re
from typing import Callable, Optional, TYPE_CHECKING

from .. import config
from ..devices.bk5491b import (
    FUNC_TO_SCPI_FUNC,
    FUNC_TO_SCPI_CONF,
    FUNC_TO_SCPI_FUNC2,
    FUNC_TO_RANGE_PREFIX_FUNC,
    FUNC_TO_NPLC_PREFIX_FUNC,
    FUNC_TO_REF_PREFIX_FUNC,
    MmeterFunc,
    func_name,
)

if TYPE_CHECKING:  # pragma: no cover
    from .hardware import HardwareManager


def _quantize_nplc(v: float) -> float:
    """Quantize NPLC to the supported set for 2831E/5491B.

    The user manual documents NPLC values of 0.1, 1, or 10.
    """

    try:
        x = float(v)
    except Exception:
        return 1.0
    choices = (0.1, 1.0, 10.0)
    return min(choices, key=lambda c: abs(x - c))


def _func_style_cmd_variants(cmd: str) -> list[str]:
    """Return robust :FUNC style command variants for firmware quirks.

    Some 5491/2831 firmware builds accept only a subset of:
      - long vs abbreviated parameter tokens (CURRent:DC vs CURR:DC)
      - quoted vs unquoted parameter forms
      - :FUNCtion vs :FUNC command header abbreviation
    """

    base = str(cmd or "").strip()
    if not base:
        return []

    # Expect "<header> <rhs>" (e.g. ":FUNCtion CURRent:DC").
    parts = base.split(None, 1)
    if len(parts) < 2:
        return [base]

    _hdr, rhs0 = parts[0], parts[1].strip().strip('"')
    if not rhs0:
        return [base]

    rhs_short = rhs0
    for k, v in (
        ("VOLTAGE", "VOLT"),
        ("CURRENT", "CURR"),
        ("RESISTANCE", "RES"),
        ("FREQUENCY", "FREQ"),
        ("PERIOD", "PER"),
        ("CONTINUITY", "CONT"),
    ):
        rhs_short = re.sub(k, v, rhs_short, flags=re.IGNORECASE)

    rhs_candidates: list[str] = [rhs0, f'"{rhs0}"']
    if rhs0.lower() != rhs_short.lower():
        rhs_candidates.append(rhs_short)
        rhs_candidates.append(f'"{rhs_short}"')

    # Try the exact mapped command first to avoid unnecessary startup errors.
    out: list[str] = [base]
    for h in (":FUNCtion", ":FUNC"):
        for rhs in rhs_candidates:
            out.append(f"{h} {rhs}")

    # De-dup while preserving order.
    uniq: list[str] = []
    seen: set[str] = set()
    for c in out:
        k = c.strip()
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(k)
    return uniq


class DeviceCommandProcessor:
    """Apply decoded *control* commands to physical devices.

    This module intentionally contains **no CAN I/O**. It receives (arb_id, data)
    tuples from a queue and performs the associated device writes.
    """

    SHAPE_MAP = {0: "SIN", 1: "SQU", 2: "RAMP"}

    def __init__(self, hardware: "HardwareManager", *, log_fn: Callable[[str], None] = print):
        self.hardware = hardware
        self.log = log_fn

        # De-bounce SCPI writes for legacy CAN frames that may be repeated.
        self._mmeter_last_autorange_cmd: tuple[int, bool] | None = None

        # Cached MrSignal arbitration id with a fallback.
        self._mrsignal_ctrl_id = int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800))

    def _mmeter_write(self, cmd: str, *, delay_s: float = 0.0, clear_input: bool = False) -> None:
        """Write a SCPI command to the multimeter.

        Caller should hold hardware.mmeter_lock.
        """
        cmd = (cmd or "").strip()
        if not cmd:
            return

        if bool(getattr(config, "MMETER_DEBUG", False)):
            self.log(f"[mmeter] >> {cmd}")

        try:
            if getattr(self.hardware, "mmeter", None) is not None:
                # Use the robust helper if available.
                self.hardware.mmeter.write(cmd, delay_s=delay_s, clear_input=clear_input)
            else:
                mm = getattr(self.hardware, "multi_meter", None)
                if not mm:
                    return

                if clear_input:
                    try:
                        mm.reset_input_buffer()
                    except Exception:
                        pass

                mm.write((cmd + "\n").encode("ascii", errors="ignore"))
                try:
                    mm.flush()
                except Exception:
                    pass
                if delay_s and delay_s > 0:
                    time.sleep(float(delay_s))

            # After any control write, pause background polling briefly so the meter
            # can settle and we don't immediately query while it's busy.
            try:
                settle = float(getattr(config, "MMETER_CONTROL_SETTLE_SEC", 0.0) or 0.0)
                if settle > 0:
                    now_m = time.monotonic()
                    until = now_m + settle
                    prev = float(getattr(self.hardware, "mmeter_quiet_until", 0.0) or 0.0)
                    if until > prev:
                        setattr(self.hardware, "mmeter_quiet_until", until)
            except Exception:
                pass

        except Exception as e:
            self.log(f"MMETER write error: {e}")

    def _mmeter_set_func(self, func: int) -> None:
        """Set the primary measurement function (VDC/IDC/etc).

        Supports both known 5491B SCPI dialects and will fall back automatically if
        the currently-selected dialect rejects the command.
        """
        func_i = int(func) & 0xFF

        style = str(
            getattr(self.hardware, "mmeter_scpi_style", getattr(config, "MMETER_SCPI_STYLE", "auto"))
        ).strip().lower()
        if style not in ("conf", "func", "auto"):
            style = "auto"

        func_cmd = FUNC_TO_SCPI_FUNC.get(func_i)
        conf_cmd = FUNC_TO_SCPI_CONF.get(func_i)

        if not func_cmd and not conf_cmd:
            self.log(f"MMETER: unsupported function enum {func_i}")
            return

        helper = getattr(self.hardware, "mmeter", None)

        def _is_no_error(line: str) -> bool:
            u = (line or "").strip().upper()
            return (not u) or u.startswith("0") or ("NO ERROR" in u)

        def _drain_errors(max_n: int = 8) -> list[str]:
            if helper is None:
                return []
            try:
                return helper.drain_errors(max_n=max_n, log=False)
            except Exception:
                return []

        def _try_cmd(cmd: str) -> bool:
            # Clear any prior errors so we can attribute the next error to this command.
            _drain_errors(max_n=8)

            self._mmeter_write(cmd, delay_s=0.12, clear_input=True)

            errs = _drain_errors(max_n=4)
            bad = [e for e in errs if not _is_no_error(e)]
            if bad:
                self.log(f"[mmeter] rejected '{cmd}': {bad[0]}")
                return False
            return True

        # Candidate command order:
        #   - auto: try FUNC first (preferred for 2831E/5491B), then CONF
        #   - func: FUNC only (avoid cross-dialect BUS errors)
        #   - conf: CONF only (avoid cross-dialect BUS errors)
        #
        # For FUNC-style commands, include robust token/header variants to avoid
        # startup "BUS: BAD COMMAND" on stricter firmware builds.
        candidates: list[tuple[str, str]] = []
        if style == "auto":
            if func_cmd:
                for c in _func_style_cmd_variants(func_cmd):
                    candidates.append(("func", c))
            if conf_cmd:
                base = conf_cmd.strip()
                with_ch = base
                # Add @1 if not specified and looks like a primary-selectable function.
                if ("@" not in base) and (":VOLT" in base or ":CURR" in base or ":FREQ" in base):
                    with_ch = base + ",@1"
                candidates.append(("conf", with_ch))
                if with_ch != base:
                    candidates.append(("conf", base))
                candidates.append(("conf", ":" + with_ch))
                if with_ch != base:
                    candidates.append(("conf", ":" + base))
        elif style == "func":
            if func_cmd:
                for c in _func_style_cmd_variants(func_cmd):
                    candidates.append(("func", c))
        else:  # style == "conf"
            if conf_cmd:
                base = conf_cmd.strip()
                with_ch = base
                if ("@" not in base) and (":VOLT" in base or ":CURR" in base or ":FREQ" in base):
                    with_ch = base + ",@1"
                candidates.append(("conf", with_ch))
                if with_ch != base:
                    candidates.append(("conf", base))
                candidates.append(("conf", ":" + with_ch))
                if with_ch != base:
                    candidates.append(("conf", ":" + base))

        # Remove duplicates while preserving order.
        seen: set[str] = set()
        uniq: list[tuple[str, str]] = []
        for sty, cmd in candidates:
            k = f"{sty}|{cmd}"
            if k in seen:
                continue
            seen.add(k)
            uniq.append((sty, cmd))
        candidates = uniq

        ok = False
        used_style = style
        used_cmd = ""
        for sty, cmd in candidates:
            if not cmd:
                continue
            if _try_cmd(cmd):
                ok = True
                used_style = sty
                used_cmd = cmd
                break

        if not ok:
            self.log(f"MMETER: failed to set function {func_name(func_i)} (style={style})")
            return

        # Commit function and (if auto/fallback) the discovered working dialect.
        self.hardware.mmeter_func = func_i
        if used_style in ("conf", "func") and used_style != style:
            setattr(self.hardware, "mmeter_scpi_style", used_style)

        if bool(getattr(config, "MMETER_DEBUG", False)):
            self.log(f"[mmeter] set func -> {func_name(func_i)} via {used_style}: {used_cmd}")

    def handle(self, arb: int, data: bytes) -> None:
        """Handle one control frame."""

        # Relay control (K1..K4 direct drive)
        if arb == int(config.RLY_CTRL_ID):
            if len(data) < 1:
                return

            b0 = int(data[0]) & 0xFF
            # PAT DBC uses 2-bit relay fields: K1=bits0..1, K2=2..3, K3=4..5, K4=6..7.
            fields = [((b0 >> (2 * i)) & 0x03) for i in range(4)]

            for ch, fld in enumerate(fields, start=1):
                drive = bool(fld != 0)
                # Keep legacy invert semantics on K1 only.
                if ch == 1 and bool(getattr(config, "K1_CAN_INVERT", False)):
                    drive = not drive

                try:
                    if hasattr(self.hardware, "set_k_drive"):
                        self.hardware.set_k_drive(ch, bool(drive))
                    elif ch == 1:
                        self.hardware.set_k1_drive(bool(drive))
                except Exception:
                    # Keep control path resilient: a bad channel write must not
                    # prevent other relay channels from updating.
                    pass
            return

        # AFG Control (Primary)
        if arb == int(config.AFG_CTRL_ID):
            if not self.hardware.afg or len(data) < 8:
                return

            enable = data[0] != 0
            shape_idx = data[1]
            freq = struct.unpack("<I", data[2:6])[0]
            ampl_mV = struct.unpack("<H", data[6:8])[0]
            ampl_V = ampl_mV / 1000.0

            try:
                with self.hardware.afg_lock:
                    if self.hardware.afg_output != enable:
                        try:
                            # GW Instek AFG-2000/2100 uses OUTP1 (not SOUR1:OUTP).
                            self.hardware.afg.write(f"OUTP1 {'ON' if enable else 'OFF'}")
                        except Exception:
                            # Fallback for other SCPI dialects.
                            self.hardware.afg.write(f"SOUR1:OUTP {'ON' if enable else 'OFF'}")
                        self.hardware.afg_output = enable
                    if self.hardware.afg_shape != shape_idx:
                        shape_str = self.SHAPE_MAP.get(shape_idx, "SIN")
                        self.hardware.afg.write(f"SOUR1:FUNC {shape_str}")
                        self.hardware.afg_shape = shape_idx
                    if self.hardware.afg_freq != freq:
                        self.hardware.afg.write(f"SOUR1:FREQ {freq}")
                        self.hardware.afg_freq = freq
                    if self.hardware.afg_ampl != ampl_mV:
                        self.hardware.afg.write(f"SOUR1:AMPL {ampl_V}")
                        self.hardware.afg_ampl = ampl_mV
            except Exception as e:
                self.log(f"AFG Control Error: {e}")
            return

        # AFG Control (Extended)
        if arb == int(config.AFG_CTRL_EXT_ID):
            if not self.hardware.afg or len(data) < 3:
                return

            offset_mV = struct.unpack("<h", data[0:2])[0]
            offset_V = offset_mV / 1000.0
            duty_cycle = int(data[2])
            duty_cycle = max(1, min(99, duty_cycle))

            try:
                with self.hardware.afg_lock:
                    if self.hardware.afg_offset != offset_mV:
                        try:
                            # GW Instek AFG-2000/2100 uses SOUR1:DCO for DC offset.
                            self.hardware.afg.write(f"SOUR1:DCO {offset_V}")
                        except Exception:
                            # Fallback for other SCPI dialects.
                            self.hardware.afg.write(f"SOUR1:VOLT:OFFS {offset_V}")
                        self.hardware.afg_offset = offset_mV
                    if self.hardware.afg_duty != duty_cycle:
                        self.hardware.afg.write(f"SOUR1:SQU:DCYC {duty_cycle}")
                        self.hardware.afg_duty = duty_cycle
            except Exception as e:
                self.log(f"AFG Ext Error: {e}")
            return

        # Multimeter control
        if arb == int(config.MMETER_CTRL_ID):
            if len(data) < 2:
                return

            meter_mode = int(data[0])
            meter_range = int(data[1])

            # Keep legacy semantics but actually drive the instrument.
            if self.hardware.multi_meter and (self.hardware.multi_meter_mode != meter_mode):
                try:
                    with self.hardware.mmeter_lock:
                        if meter_mode == 0:
                            if bool(getattr(config, "MMETER_LEGACY_MODE0_ENABLE", True)):
                                self._mmeter_set_func(int(MmeterFunc.VDC))
                        elif meter_mode == 1:
                            if bool(getattr(config, "MMETER_LEGACY_MODE1_ENABLE", True)):
                                self._mmeter_set_func(int(MmeterFunc.IDC))
                        self.hardware.multi_meter_mode = meter_mode
                except Exception:
                    pass

            # Legacy range byte:
            # By default we **do not** apply it (matches historical ROI
            # behavior and avoids "BUS: BAD COMMAND" on meters that don't
            # support the per-subsystem :RANGe:AUTO commands).
            if bool(getattr(config, "MMETER_LEGACY_RANGE_ENABLE", False)):
                try:
                    with self.hardware.mmeter_lock:
                        if int(meter_range) == 0:
                            func_i = int(getattr(self.hardware, "mmeter_func", int(MmeterFunc.VDC))) & 0xFF
                            prefix = FUNC_TO_RANGE_PREFIX_FUNC.get(func_i)
                            key = (func_i, True)
                            if prefix and self._mmeter_last_autorange_cmd != key:
                                self._mmeter_write(f"{prefix}:RANGe:AUTO ON")
                                self._mmeter_last_autorange_cmd = key
                            self.hardware.mmeter_autorange = True
                        else:
                            # Disable autorange (freeze at the currently selected range)
                            func_i = int(getattr(self.hardware, "mmeter_func", int(MmeterFunc.VDC))) & 0xFF
                            prefix = FUNC_TO_RANGE_PREFIX_FUNC.get(func_i)
                            key = (func_i, False)
                            if prefix and self._mmeter_last_autorange_cmd != key:
                                self._mmeter_write(f"{prefix}:RANGe:AUTO OFF")
                                self._mmeter_last_autorange_cmd = key
                            self.hardware.mmeter_autorange = False
                except Exception:
                    pass

            self.hardware.multi_meter_range = int(meter_range)
            return

        # Multimeter control (Extended)
        if arb == int(getattr(config, "MMETER_CTRL_EXT_ID", 0x0CFF0601)):
            if not bool(getattr(config, "MMETER_EXT_CTRL_ENABLE", True)):
                return

            if not self.hardware.multi_meter or len(data) < 1:
                return

            # Payload:
            #   byte0 = opcode
            #   byte1 = arg0
            #   byte2 = arg1
            #   byte3 = arg2
            #   bytes4..7 = float32 value (little endian)
            op = int(data[0]) & 0xFF
            arg0 = int(data[1]) & 0xFF if len(data) > 1 else 0
            arg1 = int(data[2]) & 0xFF if len(data) > 2 else 0
            arg2 = int(data[3]) & 0xFF if len(data) > 3 else 0
            fval = 0.0
            if len(data) >= 8:
                try:
                    fval = float(struct.unpack("<f", data[4:8])[0])
                except Exception:
                    fval = 0.0

            # Convention: arg0 == 0xFF means "apply to current function".
            tgt_func = int(self.hardware.mmeter_func) if arg0 == 0xFF else int(arg0)

            try:
                with self.hardware.mmeter_lock:
                    # Use the documented 2831E/5491B SCPI tree (FUNC-style).
                    # B&K's "Added Commands" doc extends this with :FUNCtion2 for
                    # the secondary display.

                    if op == 0x01:  # SET_FUNCTION (primary)
                        self._mmeter_set_func(tgt_func)
                        self.log(f"MMETER func -> {func_name(int(self.hardware.mmeter_func))}")

                    elif op == 0x02:  # SET_AUTORANGE (arg1=0/1)
                        on = bool(arg1)
                        if bool(getattr(self.hardware, "mmeter_autorange", True)) != on:
                            prefix = FUNC_TO_RANGE_PREFIX_FUNC.get(tgt_func)
                            if prefix:
                                self._mmeter_write(f"{prefix}:RANGe:AUTO {'ON' if on else 'OFF'}")
                        self.hardware.mmeter_autorange = on

                    elif op == 0x03:  # SET_RANGE (float = expected reading)
                        if not bool(getattr(config, "MMETER_EXT_SET_RANGE_ENABLE", True)):
                            return
                        if not math.isfinite(float(fval)):
                            return
                        prefix = FUNC_TO_RANGE_PREFIX_FUNC.get(tgt_func)
                        if prefix:
                            fv = float(fval)
                            # Avoid redundant writes.
                            if (not bool(getattr(self.hardware, "mmeter_autorange", True))) and abs(float(getattr(self.hardware, "mmeter_range_value", 0.0)) - fv) < 1e-12:
                                pass
                            else:
                                self._mmeter_write(f"{prefix}:RANGe {fv:g}")
                            self.hardware.mmeter_autorange = False
                            self.hardware.mmeter_range_value = fv

                    elif op == 0x04:  # SET_NPLC (float -> quantized)
                        prefix = FUNC_TO_NPLC_PREFIX_FUNC.get(tgt_func)
                        if prefix:
                            nplc = _quantize_nplc(float(fval))
                            if abs(float(getattr(self.hardware, "mmeter_nplc", 1.0)) - nplc) > 1e-12:
                                self._mmeter_write(f"{prefix}:NPLCycles {nplc:g}")
                            self.hardware.mmeter_nplc = float(nplc)

                    elif op == 0x05:  # SECONDARY_ENABLE (arg0=0/1)
                        if not bool(getattr(config, "MMETER_EXT_SECONDARY_ENABLE", True)):
                            return
                        on = bool(arg0)
                        if bool(getattr(self.hardware, "mmeter_func2_enabled", False)) != on:
                            self._mmeter_write(f":FUNCtion2:STATe {1 if on else 0}")
                        self.hardware.mmeter_func2_enabled = on

                        # If enabling, (re)apply the currently selected secondary function.
                        if on:
                            func2 = int(getattr(self.hardware, "mmeter_func2", int(MmeterFunc.VDC))) & 0xFF
                            cmd2 = FUNC_TO_SCPI_FUNC2.get(func2)
                            if cmd2:
                                self._mmeter_write(cmd2)
                            else:
                                self.log(f"MMETER secondary: unsupported func {func2}")

                    elif op == 0x06:  # SECONDARY_FUNCTION
                        if not bool(getattr(config, "MMETER_EXT_SECONDARY_ENABLE", True)):
                            return
                        func_i = int(tgt_func) & 0xFF
                        cmd2 = FUNC_TO_SCPI_FUNC2.get(func_i)
                        if not cmd2:
                            self.log(f"MMETER secondary: unsupported func {func_i}")
                            return

                        # Per B&K doc, secondary display must be enabled before FUNC2 is set.
                        if not bool(getattr(self.hardware, "mmeter_func2_enabled", False)):
                            self._mmeter_write(":FUNCtion2:STATe 1")
                            self.hardware.mmeter_func2_enabled = True

                        if int(getattr(self.hardware, "mmeter_func2", -1)) != func_i:
                            self._mmeter_write(cmd2)
                        self.hardware.mmeter_func2 = func_i

                    elif op == 0x07:  # TRIG_SOURCE (arg0=0 IMM,1 BUS,2 MAN)
                        if int(getattr(self.hardware, "mmeter_trig_source", -1)) != (int(arg0) & 0xFF):
                            src_map = {0: "IMM", 1: "BUS", 2: "MAN"}
                            src = src_map.get(int(arg0), "IMM")
                            self._mmeter_write(f":TRIGger:SOURce {src}")
                        self.hardware.mmeter_trig_source = int(arg0) & 0xFF

                    elif op == 0x08:  # BUS_TRIGGER
                        self._mmeter_write("*TRG")

                    elif op == 0x09:  # RELATIVE_ENABLE (arg0=0/1)
                        on = bool(arg0)
                        if bool(getattr(self.hardware, "mmeter_rel_enabled", False)) != on:
                            prefix = FUNC_TO_REF_PREFIX_FUNC.get(tgt_func)
                            if prefix:
                                self._mmeter_write(f"{prefix}:REFerence:STATe {'ON' if on else 'OFF'}")
                        self.hardware.mmeter_rel_enabled = on

                    elif op == 0x0A:  # RELATIVE_ACQUIRE
                        prefix = FUNC_TO_REF_PREFIX_FUNC.get(tgt_func)
                        if prefix:
                            self._mmeter_write(f"{prefix}:REFerence:ACQuire")

                    else:
                        if op != 0:
                            self.log(f"MMETER ext: unknown op=0x{op:02X} arg0={arg0} arg1={arg1} arg2={arg2}")

            except Exception as e:
                self.log(f"MMETER ext control error: {e}")

            return

        # E-load control
        if arb == int(config.LOAD_CTRL_ID):
            if not self.hardware.e_load or len(data) < 6:
                return

            first_byte = data[0]
            new_enable = 1 if (first_byte & 0x0C) == 0x04 else 0
            new_mode = 1 if (first_byte & 0x30) == 0x10 else 0
            new_short = 1 if (first_byte & 0xC0) == 0x40 else 0

            try:
                val_c = (data[3] << 8) | data[2]
                val_r = (data[5] << 8) | data[4]

                enable_changed = (self.hardware.e_load_enabled != new_enable)
                mode_changed = (self.hardware.e_load_mode != new_mode)
                short_changed = (self.hardware.e_load_short != new_short)
                c_changed = (self.hardware.e_load_csetting != val_c)
                r_changed = (self.hardware.e_load_rsetting != val_r)

                # Update cached commanded state immediately (used by the dashboard
                # and by redundant-write suppression).
                self.hardware.e_load_enabled = new_enable
                self.hardware.e_load_mode = new_mode
                self.hardware.e_load_short = new_short
                if c_changed:
                    self.hardware.e_load_csetting = val_c
                if r_changed:
                    self.hardware.e_load_rsetting = val_r

                # Build a minimal write list and hold the lock only once.
                # Order is chosen to avoid unexpected load transients:
                #   - If disabling: INP OFF first
                #   - Apply mode/short + relevant setpoint
                #   - If enabling: INP ON last
                writes: list[str] = []

                if enable_changed and (not new_enable):
                    writes.append("INP OFF")

                if mode_changed:
                    writes.append("FUNC RES" if new_mode else "FUNC CURR")

                if short_changed:
                    writes.append("INP:SHOR ON" if new_short else "INP:SHOR OFF")

                # Only write the setpoint relevant to the active mode. Also
                # force a write on mode change to ensure the new subsystem has a
                # valid setpoint.
                if new_mode == 0:
                    if c_changed or mode_changed:
                        writes.append(f"CURR {val_c/1000}")
                else:
                    if r_changed or mode_changed:
                        writes.append(f"RES {val_r/1000}")

                if enable_changed and new_enable:
                    writes.append("INP ON")

                if writes:
                    with self.hardware.eload_lock:
                        for cmd in writes:
                            self.hardware.e_load.write(cmd)
            except Exception:
                pass
            return

        # MrSignal control (MR2.0)
        if arb == self._mrsignal_ctrl_id:
            if len(data) < 6:
                return
            if not getattr(self.hardware, "mrsignal", None):
                return

            enable = (data[0] & 0x01) == 0x01
            output_select = int(data[1])  # direct register value (0=mA, 1=V, 4=mV, 6=24V)
            try:
                value = struct.unpack("<f", data[2:6])[0]
            except Exception:
                return

            # Safety: ignore unknown modes (extend later if desired)
            if output_select not in (0, 1, 4, 6):
                return

            try:
                self.hardware.set_mrsignal(
                    enable=bool(enable),
                    output_select=int(output_select),
                    value=float(value),
                    max_v=float(getattr(config, "MRSIGNAL_MAX_V", 24.0)),
                    max_ma=float(getattr(config, "MRSIGNAL_MAX_MA", 24.0)),
                )
            except Exception as e:
                self.log(f"MrSignal Control Error: {e}")
            return


def device_command_loop(
    cmd_queue: "queue.Queue[tuple[int, bytes]]",
    hardware: "HardwareManager",
    stop_event,
    *,
    log_fn: Callable[[str], None] = print,
    watchdog_mark_fn: Optional[Callable[[str], None]] = None,
    idle_on_stop: bool = True,
) -> None:
    """Process queued control frames and apply them to devices.

    The loop is resilient: any per-frame exception is contained.

    Parameters
    - cmd_queue: receives (arb_id, data) tuples from the CAN RX thread.
    - hardware: "HardwareManager" instance.
    - stop_event: threading.Event used to signal shutdown.
    """

    log_fn("Device command thread started.")
    proc = DeviceCommandProcessor(hardware, log_fn=log_fn)

    # For "knob-like" controls where only the *latest* value matters, we coalesce
    # bursts of frames to keep the device response snappy.
    #
    # This is especially important when the controller transmits at a higher rate
    # than the physical instruments can accept (SCPI/Modbus/serial writes are
    # comparatively slow).
    coalesce_ids = {
        int(config.RLY_CTRL_ID),
        int(config.AFG_CTRL_ID),
        int(config.AFG_CTRL_EXT_ID),
        int(config.MMETER_CTRL_ID),
        int(getattr(config, "MMETER_CTRL_EXT_ID", 0x0CFF0601)),
        int(config.LOAD_CTRL_ID),
        int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800)),
    }

    # Apply in a stable order so dependent frames behave predictably.
    apply_order = [
        int(config.RLY_CTRL_ID),
        int(config.LOAD_CTRL_ID),
        int(config.AFG_CTRL_ID),
        int(config.AFG_CTRL_EXT_ID),
        int(config.MMETER_CTRL_ID),
        int(getattr(config, "MMETER_CTRL_EXT_ID", 0x0CFF0601)),
        int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800)),
    ]

    while not stop_event.is_set():
        # Block for at least one command, then drain a small burst and coalesce.
        try:
            first_arb, first_data = cmd_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        except Exception:
            continue

        latest: dict[int, bytes] = {}

        def _record(a: int, d: bytes) -> None:
            a_i = int(a)
            if a_i in coalesce_ids:
                latest[a_i] = bytes(d)
            else:
                # Non-coalesced frames are processed immediately.
                latest[a_i] = bytes(d)

        _record(int(first_arb), bytes(first_data))

        # Drain anything currently queued without blocking.
        # This keeps latency low while still allowing bursts to be collapsed.
        for _ in range(1024):
            try:
                a, d = cmd_queue.get_nowait()
                _record(int(a), bytes(d))
            except queue.Empty:
                break
            except Exception:
                break

        # Apply in deterministic order; then apply any other IDs (unlikely).
        applied = set()
        for a in apply_order:
            if a in latest:
                try:
                    if watchdog_mark_fn:
                        if a == int(config.RLY_CTRL_ID):
                            watchdog_mark_fn("k1")
                        elif a in (int(config.AFG_CTRL_ID), int(config.AFG_CTRL_EXT_ID)):
                            watchdog_mark_fn("afg")
                        elif a in (int(config.MMETER_CTRL_ID), int(getattr(config, "MMETER_CTRL_EXT_ID", 0x0CFF0601))):
                            watchdog_mark_fn("mmeter")
                        elif a == int(config.LOAD_CTRL_ID):
                            watchdog_mark_fn("eload")
                        elif a == int(getattr(config, "MRSIGNAL_CTRL_ID", 0x0CFF0800)):
                            watchdog_mark_fn("mrsignal")
                    proc.handle(int(a), bytes(latest[a]))
                except Exception as e:
                    log_fn(f"Device command error: {e}")
                applied.add(a)

        # Any other IDs (future extensions) are applied last.
        for a, d in latest.items():
            if a in applied:
                continue
            try:
                proc.handle(int(a), bytes(d))
            except Exception as e:
                log_fn(f"Device command error: {e}")

    if idle_on_stop:
        try:
            hardware.apply_idle_all()
        except Exception:
            pass
