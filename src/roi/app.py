#!/usr/bin/env python3
# app.py

from __future__ import annotations

import argparse
import re
import socket
import sys
import threading
import time
import queue
from typing import Any, Dict, Optional

from . import config
from .can.metrics import BusLoadMeter
from .ui.dashboard import HAVE_RICH, build_dashboard, console
from .core.hardware import HardwareManager
from .can.comm import (
    OutgoingTxState,
    can_rx_loop,
    can_tx_loop,
    setup_can_interface,
    shutdown_can_interface,
)

# Dashboard-only: PAT switching matrix (PAT_J0..PAT_J5)
from .core.pat_matrix import PatSwitchMatrixState
from .core.device_comm import device_command_loop

from .devices.bk5491b import MmeterFunc, func_name, func_unit

# MrSignal register constants (used for stepwise polling that yields to control writes)
from .devices.mrsignal import (
    REG_ID,
    REG_INPUT_VALUE_FLOAT,
    REG_OUTPUT_ON,
    REG_OUTPUT_SELECT,
    REG_OUTPUT_VALUE_FLOAT,
    MrSignalStatus,
)

from .core.device_discovery import autodetect_and_patch_config

# Web dashboard + diagnostics
from .core.diagnostics import Diagnostics
from .web import WebDashboardServer, WebServerConfig

try:
    from rich.live import Live
except Exception:
    Live = None


def _u16_clamp(x: int) -> int:
    if x < 0:
        return 0
    if x > 0xFFFF:
        return 0xFFFF
    return x


def _i16_clamp(x: int) -> int:
    if x < -32768:
        return -32768
    if x > 32767:
        return 32767
    return x


def _log(msg: str) -> None:
    # Print to the primary console first.
    (console.log if HAVE_RICH else print)(msg)

    # Also mirror into the in-memory diagnostics log if enabled.
    try:
        diag: Diagnostics | None = globals().get("_DIAGNOSTICS")  # type: ignore[assignment]
        if diag is None:
            return

        s = str(msg or "")
        low = s.lower()
        level = "info"
        if (" error" in low) or low.startswith("error") or ("failed" in low) or ("exception" in low):
            level = "error"
        elif (" warn" in low) or ("warning" in low):
            level = "warn"

        # Best-effort source extraction from bracket prefix: "[mmeter] ..."
        source = "roi"
        if s.startswith("["):
            end = s.find("]")
            if 1 < end <= 24:
                cand = s[1:end].strip()
                if cand and all(ch.isalnum() or ch in "_-" for ch in cand):
                    source = cand

        diag.log(s, level=level, source=source)
    except Exception:
        # Never let diagnostics break the control plane.
        return


# Set by main() when diagnostics are enabled.
_DIAGNOSTICS: Diagnostics | None = None


class ControlWatchdog:
    """Tracks freshness of control messages and enforces idle behavior."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_seen: Dict[str, float] = {}
        self._timed_out: Dict[str, bool] = {
            "can": True,
            "k1": True,
            "eload": True,
            "afg": True,
            "mmeter": True,
            "mrsignal": True,
        }

        # Soft timeout threshold is the per-key timeout; hard timeout is
        # timeout + grace. We only enforce idle on hard timeout transitions.
        self._grace_s: float = float(getattr(config, "WATCHDOG_GRACE_SEC", 0.25))

        self._timeouts: Dict[str, float] = {
            "can": float(getattr(config, "CAN_TIMEOUT_SEC", float(config.CONTROL_TIMEOUT_SEC))),
            "k1": float(config.K1_TIMEOUT_SEC),
            "eload": float(config.ELOAD_TIMEOUT_SEC),
            "afg": float(config.AFG_TIMEOUT_SEC),
            "mmeter": float(config.MMETER_TIMEOUT_SEC),
            "mrsignal": float(getattr(config, "MRSIGNAL_TIMEOUT_SEC", float(config.CONTROL_TIMEOUT_SEC))),
        }

    def mark(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._last_seen[key] = now
            self._timed_out[key] = False

    def snapshot(self) -> Dict:
        now = time.monotonic()
        with self._lock:
            ages: Dict[str, Optional[float]] = {}
            states: Dict[str, str] = {}
            for k in self._timeouts.keys():
                if k in self._last_seen:
                    ages[k] = now - self._last_seen[k]
                else:
                    ages[k] = None

                timeout_s = float(self._timeouts.get(k, 0.0))
                grace_s = float(self._grace_s)
                age = ages[k]
                if age is None:
                    states[k] = "to"
                elif age > (timeout_s + grace_s):
                    states[k] = "to"
                elif age > timeout_s:
                    states[k] = "warn"
                else:
                    states[k] = "ok"
            return {
                "ages": ages,
                "states": states,
                "timed_out": dict(self._timed_out),
                "timeouts": dict(self._timeouts),
                "grace_s": float(self._grace_s),
            }

    def enforce(self, hardware: HardwareManager) -> None:
        """Enforce per-key timeouts and apply idle behavior.

        **Important:** hardware idle actions can block (VISA / serial timeouts),
        so we must *not* hold the watchdog lock while performing them.
        Otherwise, readers (Rich UI / web dashboard) can hang while waiting for
        snapshot() to acquire the lock.
        """

        now = time.monotonic()

        # Decide what needs idling while holding the lock, then perform the
        # potentially-blocking device calls outside the lock.
        to_idle: list[str] = []
        with self._lock:
            for key, timeout_s in self._timeouts.items():
                last = self._last_seen.get(key)
                if last is None:
                    # Never seen => consider timed out, but idle likely already applied.
                    self._timed_out[key] = True
                    continue

                age = now - last
                hard_timeout_s = float(timeout_s) + float(self._grace_s)
                if age > hard_timeout_s:
                    if not self._timed_out.get(key, False):
                        # Transition into timeout => apply idle once
                        self._timed_out[key] = True
                        if key != "can":
                            to_idle.append(str(key))
                else:
                    self._timed_out[key] = False

        # Apply idles outside the lock.
        for key in to_idle:
            try:
                if key == "k1":
                    hardware.set_k1_idle()
                elif key == "eload":
                    hardware.apply_idle_eload()
                elif key == "afg":
                    hardware.apply_idle_afg()
                elif key == "mmeter":
                    # Nothing safety-critical to command on timeout.
                    pass
                elif key == "mrsignal":
                    hardware.apply_idle_mrsignal()
            except Exception:
                # Watchdog must never crash the control plane.
                pass

class TelemetryState:
    """Thread-safe snapshot of instrument telemetry for the dashboard and logs.

    The key goal is to keep the Rich TUI responsive by ensuring *no* slow
    instrument I/O happens on the render loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Fast measurements (updated at MEAS_POLL_PERIOD)
        self.meter_current_mA: int = 0
        # Extended multimeter view (always valid even when not in current mode)
        self.mmeter_func_str: str = ""
        self.mmeter_primary_str: str = ""
        self.mmeter_secondary_str: str = ""
        self.load_volts_mV: int = 0
        self.load_current_mA: int = 0

        # Slow status (updated at STATUS_POLL_PERIOD)
        self.load_stat_func: str = ""
        self.load_stat_curr: str = ""
        self.load_stat_res: str = ""
        self.load_stat_imp: str = ""
        self.load_stat_short: str = ""

        self.afg_freq_str: str = ""
        self.afg_ampl_str: str = ""
        self.afg_offset_str: str = "0"
        self.afg_duty_str: str = "50"
        self.afg_out_str: str = ""
        self.afg_shape_str: str = ""

        # MrSignal status
        self.mrs_id_str: str = ""
        self.mrs_out_str: str = ""
        self.mrs_mode_str: str = ""
        self.mrs_set_str: str = ""
        self.mrs_in_str: str = ""
        self.mrs_bo_str: str = ""

        # Timestamps (monotonic seconds)
        self.last_meas_ts: float = 0.0
        self.last_status_ts: float = 0.0

    def update_meas(self, *,
                    meter_current_mA: int | None = None,
                    mmeter_func_str: str | None = None,
                    mmeter_primary_str: str | None = None,
                    mmeter_secondary_str: str | None = None,
                    load_volts_mV: int | None = None,
                    load_current_mA: int | None = None,
                    ts: float | None = None) -> None:
        with self._lock:
            if meter_current_mA is not None:
                self.meter_current_mA = int(meter_current_mA)
            if mmeter_func_str is not None:
                self.mmeter_func_str = mmeter_func_str
            if mmeter_primary_str is not None:
                self.mmeter_primary_str = mmeter_primary_str
            if mmeter_secondary_str is not None:
                self.mmeter_secondary_str = mmeter_secondary_str
            if load_volts_mV is not None:
                self.load_volts_mV = int(load_volts_mV)
            if load_current_mA is not None:
                self.load_current_mA = int(load_current_mA)
            self.last_meas_ts = float(ts if ts is not None else time.monotonic())

    def update_status(self, *, load_stat_func: str | None = None,
                      load_stat_curr: str | None = None,
                      load_stat_res: str | None = None,
                      load_stat_imp: str | None = None,
                      load_stat_short: str | None = None,
                      afg_freq_str: str | None = None,
                      afg_ampl_str: str | None = None,
                      afg_offset_str: str | None = None,
                      afg_duty_str: str | None = None,
                      afg_out_str: str | None = None,
                      afg_shape_str: str | None = None,
                      mrs_id_str: str | None = None,
                      mrs_out_str: str | None = None,
                      mrs_mode_str: str | None = None,
                      mrs_set_str: str | None = None,
                      mrs_in_str: str | None = None,
                      mrs_bo_str: str | None = None,
                      ts: float | None = None) -> None:
        with self._lock:
            if load_stat_func is not None:
                self.load_stat_func = load_stat_func
            if load_stat_curr is not None:
                self.load_stat_curr = load_stat_curr
            if load_stat_res is not None:
                self.load_stat_res = load_stat_res
            if load_stat_imp is not None:
                self.load_stat_imp = load_stat_imp
            if load_stat_short is not None:
                self.load_stat_short = load_stat_short

            if afg_freq_str is not None:
                self.afg_freq_str = afg_freq_str
            if afg_ampl_str is not None:
                self.afg_ampl_str = afg_ampl_str
            if afg_offset_str is not None:
                self.afg_offset_str = afg_offset_str
            if afg_duty_str is not None:
                self.afg_duty_str = afg_duty_str
            if afg_out_str is not None:
                self.afg_out_str = afg_out_str
            if afg_shape_str is not None:
                self.afg_shape_str = afg_shape_str

            if mrs_id_str is not None:
                self.mrs_id_str = mrs_id_str
            if mrs_out_str is not None:
                self.mrs_out_str = mrs_out_str
            if mrs_mode_str is not None:
                self.mrs_mode_str = mrs_mode_str
            if mrs_set_str is not None:
                self.mrs_set_str = mrs_set_str
            if mrs_in_str is not None:
                self.mrs_in_str = mrs_in_str
            if mrs_bo_str is not None:
                self.mrs_bo_str = mrs_bo_str

            self.last_status_ts = float(ts if ts is not None else time.monotonic())

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "meter_current_mA": int(self.meter_current_mA),
                "mmeter_func_str": str(self.mmeter_func_str),
                "mmeter_primary_str": str(self.mmeter_primary_str),
                "mmeter_secondary_str": str(self.mmeter_secondary_str),
                "load_volts_mV": int(self.load_volts_mV),
                "load_current_mA": int(self.load_current_mA),
                "load_stat_func": str(self.load_stat_func),
                "load_stat_curr": str(self.load_stat_curr),
                "load_stat_res": str(self.load_stat_res),
                "load_stat_imp": str(self.load_stat_imp),
                "load_stat_short": str(self.load_stat_short),
                "afg_freq_str": str(self.afg_freq_str),
                "afg_ampl_str": str(self.afg_ampl_str),
                "afg_offset_str": str(self.afg_offset_str),
                "afg_duty_str": str(self.afg_duty_str),
                "afg_out_str": str(self.afg_out_str),
                "afg_shape_str": str(self.afg_shape_str),
                "mrs_id_str": str(self.mrs_id_str),
                "mrs_out_str": str(self.mrs_out_str),
                "mrs_mode_str": str(self.mrs_mode_str),
                "mrs_set_str": str(self.mrs_set_str),
                "mrs_in_str": str(self.mrs_in_str),
                "mrs_bo_str": str(self.mrs_bo_str),
                "last_meas_ts": float(self.last_meas_ts),
                "last_status_ts": float(self.last_status_ts),
            }


def instrument_poll_loop(
    hardware: HardwareManager,
    tx_state: OutgoingTxState,
    telemetry: TelemetryState,
    stop_event: threading.Event,
    status_period_s: float,
    meas_period_s: float,
    diagnostics: Diagnostics | None = None,
) -> None:
    """Poll instruments in the background so the UI render loop stays snappy."""

    try:
        status_period_s = float(status_period_s)
    except Exception:
        status_period_s = 1.0
    if status_period_s <= 0:
        status_period_s = 1.0

    try:
        meas_period_s = float(meas_period_s)
    except Exception:
        meas_period_s = 0.2
    if meas_period_s <= 0:
        meas_period_s = 0.2

    _log(f"Instrument poll thread started (meas={meas_period_s:.3f}s, status={status_period_s:.3f}s).")

    def _ok(key: str) -> None:
        try:
            if diagnostics is not None:
                diagnostics.mark_ok(key)
        except Exception:
            pass

    def _err(key: str, exc: BaseException, *, where: str = "") -> None:
        try:
            if diagnostics is not None:
                diagnostics.mark_error(key, exc, where=where)
        except Exception:
            pass

    last_status = 0.0
    last_mrs = 0.0
    next_meas = time.monotonic()

    # Multimeter polling: try to learn which query command works for this meter.
    # Probing unknown commands too fast can make some meters beep; use backoff.
    mmeter_cmds = [
        c.strip() for c in str(getattr(config, "MULTI_METER_FETCH_CMDS", ":FETCh?"))
        .split(",")
        if c.strip()
    ]
    if not mmeter_cmds:
        mmeter_cmds = [":FETCh?"]
    mmeter_probe_idx = 0
    mmeter_next_probe = 0.0
    mmeter_backoff_s = float(getattr(config, "MULTI_METER_PROBE_BACKOFF_SEC", 2.0))
    _num_re = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")

    while not stop_event.is_set():
        now_m = time.monotonic()

        # --- Fast measurements (tight loop) ---
        if now_m >= next_meas:
            next_meas += meas_period_s
            # avoid runaway if we get stalled for a while
            if next_meas < now_m - (10.0 * meas_period_s):
                next_meas = now_m + meas_period_s

            meter_current_mA = None
            mmeter_func_str = None
            mmeter_primary_str = None
            mmeter_secondary_str = None
            load_volts_mV = None
            load_current_mA = None

            # Multimeter read (supports dual display + non-current functions)
            if hardware.multi_meter:
                try:
                    # If we don't yet know the right query command, only probe
                    # occasionally (backoff) so we don't spam/beep the meter.
                    known_cmd = str(getattr(hardware, "mmeter_fetch_cmd", "") or "").strip()
                    if known_cmd:
                        cmds_to_try = [known_cmd]
                    else:
                        if now_m < mmeter_next_probe:
                            cmds_to_try = []
                        else:
                            cmds_to_try = [mmeter_cmds[mmeter_probe_idx % len(mmeter_cmds)]]
                            mmeter_probe_idx += 1

                    if cmds_to_try:
                        # If we recently changed meter mode/range, give the instrument
                        # a brief window to settle before we query it.
                        quiet_until = float(getattr(hardware, "mmeter_quiet_until", 0.0) or 0.0)
                        if quiet_until and now_m < quiet_until:
                            cmds_to_try = []
                        mm_primary = None
                        mm_secondary = None
                        mm_raw = ""

                        # Don't let polling contend with control writes.
                        if hardware.mmeter_lock.acquire(timeout=0.0):
                            try:
                                for cmd in cmds_to_try:
                                    if getattr(hardware, "mmeter", None) is not None:
                                        mm_primary, mm_secondary, mm_raw = hardware.mmeter.query_values(cmd, delay_s=0.01)
                                    else:
                                        # Fallback: do a very small/robust parse
                                        hardware.multi_meter.write((cmd + "\n").encode("ascii", errors="ignore"))
                                        raw = hardware.multi_meter.readline()
                                        resp = raw.decode("ascii", errors="replace").strip()
                                        mm_raw = resp
                                        nums = _num_re.findall(resp)
                                        if nums:
                                            mm_primary = float(nums[0])
                                            if len(nums) > 1:
                                                mm_secondary = float(nums[1])

                                    if mm_primary is None:
                                        continue

                                    # Learn the command that worked.
                                    if not known_cmd:
                                        hardware.mmeter_fetch_cmd = cmd
                                        _log(f"[mmeter] using fetch cmd: {cmd}")
                                    break
                            finally:
                                hardware.mmeter_lock.release()

                        if mm_primary is not None:
                            # Status packing for CAN
                            func_i = int(getattr(hardware, "mmeter_func", int(MmeterFunc.VDC))) & 0xFF
                            flags = 0
                            if bool(getattr(hardware, "mmeter_func2_enabled", False)):
                                flags |= 0x01
                            if bool(getattr(hardware, "mmeter_autorange", True)):
                                flags |= 0x02
                            if bool(getattr(hardware, "mmeter_rel_enabled", False)):
                                flags |= 0x04
                            tx_state.update_mmeter_values(mm_primary, mm_secondary)
                            tx_state.update_mmeter_status(func=func_i, flags=flags)

                            # Legacy current readback (only valid in current modes)
                            if func_i in (int(MmeterFunc.IDC), int(MmeterFunc.IAC)):
                                meter_current_mA = int(round(float(mm_primary) * 1000.0))
                                tx_state.update_meter_current(meter_current_mA)
                            else:
                                meter_current_mA = 0
                                tx_state.clear_meter_current()

                            # Format for dashboard/logs
                            p_unit = func_unit(func_i)
                            mmeter_func_str = func_name(func_i)
                            # Inside instrument_poll_loop
                            if not headless:
                                # Only do this heavy string lifting if a human is watching
                                mmeter_primary_str = f"{mm_primary:g} {p_unit}".strip()
                                # ... other string formats ...
                            else:
                                mmeter_primary_str = ""
                            mmeter_secondary_str = ""
                            if mm_secondary is not None:
                                s_func = int(getattr(hardware, "mmeter_func2", func_i)) & 0xFF
                                s_unit = func_unit(s_func)
                                mmeter_secondary_str = f"{mm_secondary:g} {s_unit}".strip()

                            # We successfully talked to the multimeter.
                            _ok("mmeter")
                        else:
                            # If we were probing (unknown cmd) and didn't get a value,
                            # back off before trying the next candidate.
                            if not known_cmd:
                                mmeter_next_probe = now_m + mmeter_backoff_s

                except Exception as e:
                    if not str(getattr(hardware, "mmeter_fetch_cmd", "") or "").strip():
                        mmeter_next_probe = now_m + mmeter_backoff_s
                    _err("mmeter", e, where="meas")

            # E-Load measurement
            if hardware.e_load:
                try:
                    # Don't block controls. Keep lock holds short by doing one
                    # query per acquisition; this caps worst-case control stall
                    # time to a single VISA timeout.
                    v_str, i_str = "", ""
                    if hardware.eload_lock.acquire(timeout=0.0):
                        try:
                            v_str = hardware.e_load.query("MEAS:VOLT?").strip()
                        finally:
                            hardware.eload_lock.release()
                    if hardware.eload_lock.acquire(timeout=0.0):
                        try:
                            i_str = hardware.e_load.query("MEAS:CURR?").strip()
                        finally:
                            hardware.eload_lock.release()

                    if v_str and i_str:
                        load_volts_mV = int(float(v_str) * 1000)
                        load_current_mA = int(float(i_str) * 1000)
                        tx_state.update_eload(load_volts_mV, load_current_mA)
                        _ok("eload")
                except Exception as e:
                    _err("eload", e, where="meas")

            if (
                (meter_current_mA is not None)
                or (mmeter_primary_str is not None)
                or (load_volts_mV is not None)
                or (load_current_mA is not None)
            ):
                telemetry.update_meas(
                    meter_current_mA=meter_current_mA,
                    mmeter_func_str=mmeter_func_str,
                    mmeter_primary_str=mmeter_primary_str,
                    mmeter_secondary_str=mmeter_secondary_str,
                    load_volts_mV=load_volts_mV,
                    load_current_mA=load_current_mA,
                    ts=now_m,
                )

        # --- Slow status poll (setpoints/mode) ---
        if (now_m - last_status) >= status_period_s:
            last_status = now_m

            load_stat_func = None
            load_stat_curr = None
            load_stat_imp = None
            load_stat_res = None
            load_stat_short = None

            afg_freq_str = None
            afg_ampl_str = None
            afg_out_str = None
            afg_shape_str = None
            afg_offset_str = None
            afg_duty_str = None

            if hardware.e_load:
                try:
                    # Keep lock holds short and avoid unnecessary queries.
                    # We only query the setpoint relevant to the active mode.

                    # FUNC?
                    if hardware.eload_lock.acquire(timeout=0.0):
                        try:
                            load_stat_func = hardware.e_load.query("FUNC?").strip()
                        finally:
                            hardware.eload_lock.release()

                    # INP?
                    if hardware.eload_lock.acquire(timeout=0.0):
                        try:
                            load_stat_imp = hardware.e_load.query("INP?").strip()
                        finally:
                            hardware.eload_lock.release()

                    # INP:SHOR? (optional)
                    if hardware.eload_lock.acquire(timeout=0.0):
                        try:
                            try:
                                load_stat_short = hardware.e_load.query("INP:SHOR?").strip()
                            except Exception:
                                load_stat_short = ""
                        finally:
                            hardware.eload_lock.release()

                    # Active setpoint based on mode
                    func_u = (str(load_stat_func or "").strip().upper())
                    if func_u.startswith("CURR"):
                        # Clear RES setpoint so the dashboard doesn't show stale values.
                        load_stat_res = ""
                        if hardware.eload_lock.acquire(timeout=0.0):
                            try:
                                load_stat_curr = hardware.e_load.query("CURR?").strip()
                            finally:
                                hardware.eload_lock.release()
                    elif func_u.startswith("RES"):
                        load_stat_curr = ""
                        if hardware.eload_lock.acquire(timeout=0.0):
                            try:
                                load_stat_res = hardware.e_load.query("RES?").strip()
                            finally:
                                hardware.eload_lock.release()
                    else:
                        # Unknown mode: keep the old behavior (best-effort read both)
                        if hardware.eload_lock.acquire(timeout=0.0):
                            try:
                                load_stat_curr = hardware.e_load.query("CURR?").strip()
                                load_stat_res = hardware.e_load.query("RES?").strip()
                            finally:
                                hardware.eload_lock.release()
                    _ok("eload")
                except Exception as e:
                    _err("eload", e, where="status")

            if hardware.afg:
                try:
                    # Keep lock holds short: one query per acquisition so
                    # control writes are only blocked for a single VISA transaction.
                    if hardware.afg_lock.acquire(timeout=0.0):
                        try:
                            try:
                                # GW Instek AFG-2000/2100 series uses OUTP1? (not SOUR1:OUTP?).
                                afg_out_str = hardware.afg.query("OUTP1?").strip()
                            except Exception:
                                # Fallback for other SCPI dialects.
                                afg_out_str = hardware.afg.query("SOUR1:OUTP?").strip()
                        finally:
                            hardware.afg_lock.release()
                    if afg_out_str is not None and str(afg_out_str).strip() != "":
                        is_actually_on = str(afg_out_str).strip().upper() in ["ON", "1"]
                        if hardware.afg_output != is_actually_on:
                            hardware.afg_output = is_actually_on

                    if hardware.afg_lock.acquire(timeout=0.0):
                        try:
                            afg_freq_str = hardware.afg.query("SOUR1:FREQ?").strip()
                        finally:
                            hardware.afg_lock.release()
                    if hardware.afg_lock.acquire(timeout=0.0):
                        try:
                            afg_ampl_str = hardware.afg.query("SOUR1:AMPL?").strip()
                        finally:
                            hardware.afg_lock.release()
                    if hardware.afg_lock.acquire(timeout=0.0):
                        try:
                            afg_shape_str = hardware.afg.query("SOUR1:FUNC?").strip()
                        finally:
                            hardware.afg_lock.release()
                    if hardware.afg_lock.acquire(timeout=0.0):
                        try:
                            try:
                                # GW Instek AFG-2000/2100 series uses SOUR1:DCO? for DC offset.
                                afg_offset_str = hardware.afg.query("SOUR1:DCO?").strip()
                            except Exception:
                                # Fallback for other SCPI dialects.
                                afg_offset_str = hardware.afg.query("SOUR1:VOLT:OFFS?").strip()
                        finally:
                            hardware.afg_lock.release()
                    if hardware.afg_lock.acquire(timeout=0.0):
                        try:
                            afg_duty_str = hardware.afg.query("SOUR1:SQU:DCYC?").strip()
                        finally:
                            hardware.afg_lock.release()

                    if afg_offset_str and afg_duty_str:
                        off_mv = _i16_clamp(int(float(afg_offset_str) * 1000))
                        duty_pct = max(0, min(100, int(float(afg_duty_str))))
                        tx_state.update_afg_ext(off_mv, duty_pct)
                    _ok("afg")
                except Exception as e:
                    _err("afg", e, where="status")

            # MrSignal (MR2.0) status/input
            mrs_id_str = None
            mrs_out_str = None
            mrs_mode_str = None
            mrs_set_str = None
            mrs_in_str = None
            mrs_bo_str = None

            if getattr(hardware, "mrsignal", None):
                try:
                    poll_p = float(getattr(config, "MRSIGNAL_POLL_PERIOD", status_period_s))
                    if poll_p <= 0:
                        poll_p = status_period_s

                    if (now_m - last_mrs) >= poll_p:
                        last_mrs = now_m
                        client = hardware.mrsignal

                        # Read status in small chunks so control writes are only
                        # blocked for a single Modbus transaction at a time.
                        dev_id = None
                        out_on = None
                        out_sel = None
                        out_val = None
                        in_val = None
                        bo = str(getattr(client, "_last_used_bo", "DEFAULT") or "DEFAULT")

                        # If the device thread is actively writing, skip this poll.
                        if not hardware.mrsignal_lock.acquire(timeout=0.0):
                            raise RuntimeError("mrsignal busy")
                        try:
                            try:
                                dev_id = client._read_u16(REG_ID, signed=False)
                            except Exception:
                                dev_id = None
                        finally:
                            hardware.mrsignal_lock.release()

                        if hardware.mrsignal_lock.acquire(timeout=0.0):
                            try:
                                try:
                                    out_on = bool(client._read_u16(REG_OUTPUT_ON, signed=False))
                                except Exception:
                                    out_on = None
                            finally:
                                hardware.mrsignal_lock.release()

                        if hardware.mrsignal_lock.acquire(timeout=0.0):
                            try:
                                try:
                                    out_sel = int(client._read_u16(REG_OUTPUT_SELECT, signed=False))
                                except Exception:
                                    out_sel = None
                            finally:
                                hardware.mrsignal_lock.release()

                        if hardware.mrsignal_lock.acquire(timeout=0.0):
                            try:
                                try:
                                    out_val, bo = client._read_float(REG_OUTPUT_VALUE_FLOAT)
                                except Exception:
                                    out_val = None
                            finally:
                                hardware.mrsignal_lock.release()

                        if hardware.mrsignal_lock.acquire(timeout=0.0):
                            try:
                                try:
                                    in_val, bo2 = client._read_float(REG_INPUT_VALUE_FLOAT)
                                    bo = bo2 or bo
                                except Exception:
                                    in_val = None
                            finally:
                                hardware.mrsignal_lock.release()

                        st = MrSignalStatus(
                            device_id=dev_id,
                            output_on=out_on,
                            output_select=out_sel,
                            output_value=out_val,
                            input_value=in_val,
                            float_byteorder=bo,
                        )

                        # Update hardware cached fields for dashboard (only when present)
                        if st.device_id is not None:
                            hardware.mrsignal_id = st.device_id
                        if st.output_on is not None:
                            hardware.mrsignal_output_on = bool(st.output_on)
                        if st.output_select is not None:
                            hardware.mrsignal_output_select = int(st.output_select or 0)
                        if st.output_value is not None:
                            hardware.mrsignal_output_value = float(st.output_value)
                        if st.input_value is not None:
                            hardware.mrsignal_input_value = float(st.input_value)
                        hardware.mrsignal_float_byteorder = str(st.float_byteorder or "DEFAULT")

                        if st.device_id is not None:
                            mrs_id_str = str(st.device_id)
                        if st.output_on is not None:
                            mrs_out_str = "ON" if bool(st.output_on) else "OFF"
                        if st.output_select is not None:
                            mrs_mode_str = st.mode_label

                        # Render set/input with units based on mode
                        if st.output_value is not None and st.output_select is not None:
                            if int(st.output_select or 0) == 0:
                                mrs_set_str = f"{float(st.output_value):.4g} mA"
                            elif int(st.output_select or 0) == 4:
                                mrs_set_str = f"{float(st.output_value):.4g} mV"
                            else:
                                mrs_set_str = f"{float(st.output_value):.4g} V"
                        if st.input_value is not None and st.output_select is not None:
                            if int(st.output_select or 0) == 0:
                                mrs_in_str = f"{float(st.input_value):.4g} mA"
                            elif int(st.output_select or 0) == 4:
                                mrs_in_str = f"{float(st.input_value):.4g} mV"
                            else:
                                mrs_in_str = f"{float(st.input_value):.4g} V"
                        if st.float_byteorder:
                            mrs_bo_str = str(st.float_byteorder)

                        # We successfully talked to MrSignal.
                        _ok("mrsignal")

                        # CAN readback publisher state
                        try:
                            # Use the last-known value if the float read failed; output_on/off
                            # and mode are still useful for remote clients.
                            out_v = float(st.output_value) if st.output_value is not None else float(getattr(hardware, "mrsignal_output_value", 0.0) or 0.0)
                            if st.output_on is not None and st.output_select is not None:
                                tx_state.update_mrsignal_status(
                                    output_on=bool(st.output_on),
                                    output_select=int(st.output_select or 0),
                                    output_value=out_v,
                                )
                            if st.input_value is not None:
                                tx_state.update_mrsignal_input(float(st.input_value))
                        except Exception:
                            pass
                except Exception as e:
                    # "mrsignal busy" just means the device thread is actively
                    # writing (we intentionally skip polling to avoid lock contention).
                    if "mrsignal busy" not in str(e).lower():
                        _err("mrsignal", e, where="status")
            telemetry.update_status(
                load_stat_func=load_stat_func,
                load_stat_curr=load_stat_curr,
                load_stat_res=load_stat_res,
                load_stat_imp=load_stat_imp,
                load_stat_short=load_stat_short,
                afg_freq_str=afg_freq_str,
                afg_ampl_str=afg_ampl_str,
                afg_out_str=afg_out_str,
                afg_shape_str=afg_shape_str,
                afg_offset_str=afg_offset_str,
                afg_duty_str=afg_duty_str,
                mrs_id_str=mrs_id_str,
                mrs_out_str=mrs_out_str,
                mrs_mode_str=mrs_mode_str,
                mrs_set_str=mrs_set_str,
                mrs_in_str=mrs_in_str,
                mrs_bo_str=mrs_bo_str,
                ts=now_m,
            )

        # Adaptive wait: sleep until the next scheduled poll to avoid waking up
        # hundreds of times per second when periods are slow.
        now2 = time.monotonic()
        next_status_due = (last_status + status_period_s) if status_period_s > 0 else float("inf")
        next_due = min(next_meas, next_status_due)
        sleep_s = max(0.0, float(next_due) - float(now2))
        # Always yield at least a tiny amount if we're "due now" to avoid a tight spin
        # when I/O is failing fast.
        if sleep_s <= 0:
            sleep_s = 0.001
        stop_event.wait(timeout=sleep_s)

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ROI Instrument Bridge")
    try:
        from .build_info import get_version_with_revision

        version_str = f"ROI {get_version_with_revision()}"
    except Exception:
        version_str = "ROI unknown"

    p.add_argument("--version", action="version", version=version_str)
    p.add_argument("--headless", action="store_true", help="Disable Rich TUI (better for systemd)")
    p.add_argument("--no-can-setup", action="store_true", help="Do not run 'ip link set ... up type can ...'")
    p.add_argument("--no-auto-detect", action="store_true", help="Disable USB/VISA auto-detection at startup")
    p.add_argument("--web", action="store_true", help="Enable the read-only web dashboard")
    p.add_argument("--web-host", default=None, help="Web dashboard bind host (default: ROI_WEB_HOST)")
    p.add_argument("--web-port", type=int, default=None, help="Web dashboard port (default: ROI_WEB_PORT)")
    p.add_argument("--web-token", default=None, help="Optional bearer token for web dashboard (default: ROI_WEB_TOKEN)")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()

    # Process start time (monotonic) for uptime/health calculations.
    process_start_mono = time.monotonic()

    # Enable in-memory diagnostics (used by the web dashboard). This is cheap
    # enough to keep on even when the web UI is disabled.
    global _DIAGNOSTICS
    try:
        _DIAGNOSTICS = Diagnostics(
            max_events=int(getattr(config, "ROI_WEB_DIAG_MAX_EVENTS", 250)),
            dedupe_window_s=float(getattr(config, "ROI_WEB_DIAG_DEDUPE_WINDOW_S", 0.75)),
        )
    except Exception:
        _DIAGNOSTICS = None

    # Print version + revision early so we can verify which build is actually running.
    build_tag = str(getattr(config, "BUILD_TAG", "unknown"))
    build_rev = "unknown"
    try:
        from .build_info import build_banner, get_revision

        build_rev = get_revision(short=True)
        _log(build_banner(build_tag=build_tag))
    except Exception:
        # Fallback: at least show the configured build tag.
        try:
            _log(f"ROI build: {build_tag}")
        except Exception:
            pass

    # Optional: auto-detect device connection paths on Raspberry Pi so we
    # don't depend on /dev/ttyUSB0 style numbering.
    if (not bool(getattr(args, "no_auto_detect", False))) and bool(getattr(config, "AUTO_DETECT_ENABLE", True)):
        try:
            autodetect_and_patch_config(log_fn=_log)
        except Exception as e:
            _log(f"[autodetect] warning: {e}")

    try:
        hardware = HardwareManager()
    except Exception as e:
        _log(f"Hardware init failed: {e}")
        return 2
    stop_event = threading.Event()
    watchdog = ControlWatchdog()

    # CAN bus load estimator (dashboard)
    busload = BusLoadMeter(
        bitrate=int(config.CAN_BITRATE),
        window_s=float(getattr(config, 'CAN_BUS_LOAD_WINDOW_SEC', 1.0)),
        stuffing_factor=float(getattr(config, 'CAN_BUS_LOAD_STUFFING_FACTOR', 1.2)),
        overhead_bits=int(getattr(config, 'CAN_BUS_LOAD_OVERHEAD_BITS', 48)),
        smooth_alpha=float(getattr(config, 'CAN_BUS_LOAD_SMOOTH_ALPHA', 0.0)),
        enabled=bool(getattr(config, 'CAN_BUS_LOAD_ENABLE', True)),
    )

    # measurement vars
    meter_current_mA = 0
    load_volts_mV = 0
    load_current_mA = 0

    # outgoing CAN readback publisher state (sent by TX thread)
    tx_state = OutgoingTxState()

    # PAT switching matrix snapshot (updated by CAN RX thread)
    pat_matrix = PatSwitchMatrixState()

    # status vars
    load_stat_func, load_stat_curr, load_stat_imp, load_stat_res, load_stat_short = "", "", "", "", ""
    afg_freq_str, afg_ampl_str, afg_out_str, afg_shape_str = "", "", "", ""
    afg_offset_str, afg_duty_str = "0", "50"
    # Decide UI mode
    headless = bool(args.headless or config.ROI_HEADLESS or (not sys.stdout.isatty()) or (not HAVE_RICH) or (Live is None))

    # Optional web dashboard (started later, stopped in finally).
    web_server: WebDashboardServer | None = None

    try:
        hardware.initialize_devices()

        if bool(config.APPLY_IDLE_ON_STARTUP):
            hardware.apply_idle_all()

        cbus = setup_can_interface(
            config.CAN_CHANNEL,
            int(config.CAN_BITRATE),
            do_setup=bool(config.CAN_SETUP) and (not args.no_can_setup),
        )
        if not cbus:
            return 2

        # --- CAN RX is isolated from *all* device I/O ---
        # CAN RX thread only enqueues control frames. A dedicated device
        # command worker applies them to instruments/IO.
        cmd_queue = queue.Queue(maxsize=int(getattr(config, "CAN_CMD_QUEUE_MAX", 256)))

        device_thread = threading.Thread(
            target=device_command_loop,
            args=(cmd_queue, hardware, stop_event),
            kwargs={"log_fn": _log, "watchdog_mark_fn": watchdog.mark},
            daemon=True,
        )
        device_thread.start()

        # FIX: Define web_enable HERE so it is available for use_pat
        web_enable = bool(getattr(args, "web", False)) or bool(getattr(config, "ROI_WEB_ENABLE", False))

        # FIX: Now this will work
        use_pat = (not headless) or web_enable

        can_rx_thread = threading.Thread(
            target=can_rx_loop,
            args=(cbus, cmd_queue, stop_event, watchdog),
            kwargs={
                "busload": busload, 
                "log_fn": _log, 
                "pat_matrix": pat_matrix if use_pat else None
            },
            daemon=True,
        )
        can_rx_thread.start()

        tx_thread = None
        if bool(getattr(config, 'CAN_TX_ENABLE', True)):
            try:
                period_ms = float(getattr(config, 'CAN_TX_PERIOD_MS', 50))
            except Exception:
                period_ms = 50.0
            if period_ms > 0:
                tx_thread = threading.Thread(
                    target=can_tx_loop,
                    args=(cbus, tx_state, stop_event, period_ms / 1000.0, busload),
                    kwargs={"log_fn": _log},
                    daemon=True,
                )
                tx_thread.start()
            else:
                _log('CAN_TX_PERIOD_MS <= 0; TX rate regulation disabled.')

                
        # Start background instrument polling so the UI stays responsive even if instrument I/O blocks.
        try:
            status_period = float(getattr(config, "STATUS_POLL_PERIOD", 1.0))
        except Exception:
            status_period = 1.0
        try:
            meas_period = float(getattr(config, "MEAS_POLL_PERIOD", 0.2))
        except Exception:
            meas_period = 0.2
        try:
            dash_fps = int(getattr(config, "DASH_FPS", 15))
        except Exception:
            dash_fps = 15
        if dash_fps <= 0:
            dash_fps = 10

        # In headless/systemd mode we don't need a high-rate UI loop, but we still
        # want to enforce watchdog timeouts reasonably promptly.
        try:
            headless_tick_s = float(getattr(config, "HEADLESS_LOOP_PERIOD_S", 0.1))
        except Exception:
            headless_tick_s = 0.1
        if headless_tick_s <= 0:
            headless_tick_s = 0.1

        telemetry = TelemetryState()
        poll_thread = threading.Thread(
            target=instrument_poll_loop,
            args=(hardware, tx_state, telemetry, stop_event, status_period, meas_period, _DIAGNOSTICS),
            daemon=True,
        )
        poll_thread.start()

        # ------------------------------------------------------------------
        # Web dashboard (read-only)
        # ------------------------------------------------------------------
        web_enable = bool(getattr(args, "web", False)) or bool(getattr(config, "ROI_WEB_ENABLE", False))
        if web_enable:
            host = (
                str(getattr(args, "web_host", "") or "")
                if getattr(args, "web_host", None) is not None
                else str(getattr(config, "ROI_WEB_HOST", "0.0.0.0") or "0.0.0.0")
            )
            port = (
                int(getattr(args, "web_port", 0) or 0)
                if getattr(args, "web_port", None) is not None
                else int(getattr(config, "ROI_WEB_PORT", 8080) or 8080)
            )
            token = (
                str(getattr(args, "web_token", "") or "")
                if getattr(args, "web_token", None) is not None
                else str(getattr(config, "ROI_WEB_TOKEN", "") or "")
            )

            def _snapshot() -> Dict[str, Any]:
                # IMPORTANT: Do NOT do instrument I/O here. Only read cached state.
                try:
                    host_name = socket.gethostname()
                except Exception:
                    host_name = "roi"

                def _short_can_channel(can_channel: str) -> str:
                    """Shorten long /dev/serial/by-id CAN paths for display.

                    Mirrors the Rich dashboard behaviour (ui/dashboard.py) but
                    keeps this snapshot dependency-free.
                    """

                    ch = str(can_channel or "").strip()
                    if not ch:
                        return "--"

                    iface = str(getattr(config, "CAN_INTERFACE", "socketcan") or "socketcan").strip().lower()
                    if iface not in ("rmcanview", "rm-canview", "proemion"):
                        return ch

                    base = ch.rsplit("/", 1)[-1]
                    low = base.lower()
                    if "canview" in low:
                        idx = low.find("canview")
                        s = base[idx:]
                        s = re.sub(r"-if\d+.*$", "", s)
                        s = re.sub(r"[_-]port\d+$", "", s)
                        return s
                    return base

                # PAT display metadata (used by the web dashboard to match the Rich TUI)
                pat_timeout_s: float | None = None
                try:
                    pat_timeout_s = float(getattr(config, "PAT_MATRIX_TIMEOUT_SEC", getattr(config, "CAN_TIMEOUT_SEC", 2.0)))
                except Exception:
                    pat_timeout_s = None

                pat_j0_names: Dict[int, str] = {}
                try:
                    from .core.pat_matrix import j0_pin_names as _j0_pin_names

                    pat_j0_names = _j0_pin_names() or {}
                except Exception:
                    pat_j0_names = {}

                # Bus metrics
                load_pct, rx_fps, tx_fps = (None, None, None)
                try:
                    if busload is not None:
                        load_pct, rx_fps, tx_fps = busload.snapshot()
                except Exception:
                    pass

                # K relay state (K1..K4)
                relay_backend = str(getattr(hardware, "relay_backend", ""))
                relay_states: list[Dict[str, Any]] = []
                try:
                    kmap = hardware.get_k_relays_state()
                    for ch in sorted(int(k) for k in kmap.keys()):
                        st = kmap.get(ch, {}) or {}
                        relay_states.append(
                            {
                                "name": f"K{ch}",
                                "index": int(ch),
                                "drive": bool(st.get("drive", False)),
                                "pin_level": st.get("pin_level", None),
                            }
                        )
                except Exception:
                    # Backward-compatible fallback: synthesize K1 from legacy API.
                    k1_drive = None
                    k1_level = None
                    try:
                        k1_drive = bool(hardware.get_k1_drive())
                    except Exception:
                        try:
                            k1_drive = bool(getattr(hardware.relay, "is_lit", False))
                        except Exception:
                            k1_drive = None
                    try:
                        k1_level = hardware.get_k1_pin_level()
                    except Exception:
                        k1_level = None
                    relay_states = [{"name": "K1", "index": 1, "drive": k1_drive, "pin_level": k1_level}]

                k1_drive = relay_states[0].get("drive", None) if relay_states else None
                k1_level = relay_states[0].get("pin_level", None) if relay_states else None

                # Device summaries
                devices: Dict[str, Any] = {}
                devices["can"] = {
                    "present": True,
                    "interface": str(getattr(config, "CAN_INTERFACE", "socketcan")),
                    "channel": str(getattr(config, "CAN_CHANNEL", "can0")),
                    "channel_short": _short_can_channel(str(getattr(config, "CAN_CHANNEL", "can0"))),
                    "bitrate": int(getattr(config, "CAN_BITRATE", 500000)),
                    "bus_load_pct": load_pct,
                    "rx_fps": rx_fps,
                    "tx_fps": tx_fps,
                }

                devices["k1"] = {
                    "present": True,
                    "backend": relay_backend,
                    "drive": k1_drive,
                    "pin_level": k1_level,
                    "channel_count": len(relay_states),
                    "channels": relay_states,
                }

                devices["k_relays"] = {
                    "present": True,
                    "backend": relay_backend,
                    "channel_count": len(relay_states),
                    "channels": relay_states,
                }

                devices["mmeter"] = {
                    "present": bool(getattr(hardware, "multi_meter", None)),
                    "id": str(getattr(hardware, "mmeter_id", "") or ""),
                    "scpi_style": str(getattr(hardware, "mmeter_scpi_style", "") or ""),
                    "fetch_cmd": str(getattr(hardware, "mmeter_fetch_cmd", "") or ""),
                    "func": func_name(int(getattr(hardware, "mmeter_func", int(MmeterFunc.VDC))) & 0xFF),
                    "autorange": bool(getattr(hardware, "mmeter_autorange", True)),
                    "range_value": float(getattr(hardware, "mmeter_range_value", 0.0) or 0.0),
                    "nplc": float(getattr(hardware, "mmeter_nplc", 1.0) or 1.0),
                    "rel": bool(getattr(hardware, "mmeter_rel_enabled", False)),
                    "trig": int(getattr(hardware, "mmeter_trig_source", 0) or 0) & 0xFF,
                    "func2": func_name(int(getattr(hardware, "mmeter_func2", int(MmeterFunc.VDC))) & 0xFF),
                    "func2_enabled": bool(getattr(hardware, "mmeter_func2_enabled", False)),
                    "path": str(getattr(config, "MULTI_METER_PATH", "")),
                }

                devices["eload"] = {
                    "present": bool(getattr(hardware, "e_load", None)),
                    "id": str(getattr(hardware, "e_load_id", "") or ""),
                    "resource": str(getattr(hardware, "e_load_resource", "") or "")
                    or str(getattr(getattr(hardware, "e_load", None), "resource_name", "") or ""),
                    # Last commanded state (used as a fallback when status polling is stale)
                    "cmd_enabled": int(getattr(hardware, "e_load_enabled", 0) or 0),
                    "cmd_mode": int(getattr(hardware, "e_load_mode", 0) or 0),
                    "cmd_short": int(getattr(hardware, "e_load_short", 0) or 0),
                    "cmd_csetting_mA": int(getattr(hardware, "e_load_csetting", 0) or 0),
                    "cmd_rsetting_mOhm": int(getattr(hardware, "e_load_rsetting", 0) or 0),
                }

                devices["afg"] = {
                    "present": bool(getattr(hardware, "afg", None)),
                    "id": str(getattr(hardware, "afg_id", "") or ""),
                    "resource": str(getattr(config, "AFG_VISA_ID", "") or ""),
                    # Last commanded state (used as a fallback when status polling is stale)
                    "cmd_output": bool(getattr(hardware, "afg_output", False)),
                    "cmd_shape": int(getattr(hardware, "afg_shape", 0) or 0),
                    "cmd_freq_hz": int(getattr(hardware, "afg_freq", 0) or 0),
                    "cmd_ampl_mVpp": int(getattr(hardware, "afg_ampl", 0) or 0),
                    "cmd_offset_mV": int(getattr(hardware, "afg_offset", 0) or 0),
                    "cmd_duty": int(getattr(hardware, "afg_duty", 0) or 0),
                }

                devices["mrsignal"] = {
                    "present": bool(getattr(hardware, "mrsignal", None)),
                    "id": getattr(hardware, "mrsignal_id", None),
                    "port": str(getattr(config, "MRSIGNAL_PORT", "") or ""),
                    # Last known state (status thread updates these; safe to read)
                    "cmd_output_on": bool(getattr(hardware, "mrsignal_output_on", False)),
                    "cmd_output_select": int(getattr(hardware, "mrsignal_output_select", 0) or 0),
                    "cmd_output_value": float(getattr(hardware, "mrsignal_output_value", 0.0) or 0.0),
                    "cmd_input_value": float(getattr(hardware, "mrsignal_input_value", 0.0) or 0.0),
                    "cmd_float_byteorder": str(getattr(hardware, "mrsignal_float_byteorder", "") or ""),
                }

                devices["pat"] = {"present": True}

                diag_payload: Dict[str, Any] = {"events": [], "health": {}}
                try:
                    if _DIAGNOSTICS is not None:
                        diag_payload = _DIAGNOSTICS.snapshot()
                except Exception:
                    pass

                snap: Dict[str, Any] = {
                    "host": host_name,
                    "time_unix": time.time(),
                    "build_tag": str(build_tag),
                    "build_rev": str(build_rev),
                    "uptime_s": float(time.monotonic() - process_start_mono),
                    "config": {
                        "roi_headless": bool(headless),
                        "can_interface": str(getattr(config, "CAN_INTERFACE", "socketcan")),
                        "can_channel": str(getattr(config, "CAN_CHANNEL", "can0")),
                        "can_bitrate": int(getattr(config, "CAN_BITRATE", 500000)),
                        "status_poll_period_s": float(status_period),
                        "meas_poll_period_s": float(meas_period),
                    },
                    "devices": devices,
                    "telemetry": telemetry.snapshot(),
                    "watchdog": watchdog.snapshot(),
                    "pat_matrix": pat_matrix.snapshot() if pat_matrix is not None else {},
                    "pat_meta": {
                        "timeout_s": pat_timeout_s,
                        "j0_pin_names": pat_j0_names,
                    },
                    "diagnostics": diag_payload,
                }
                return snap

            try:
                web_server = WebDashboardServer(
                    cfg=WebServerConfig(host=str(host), port=int(port), token=str(token)),
                    get_snapshot=_snapshot,
                    log_fn=_log,
                )
                web_server.start()
            except Exception as e:
                _log(f"[web] error: {e}")
        
        # Headless loop (no rich Live)
        if headless:
            _log("Running headless (no Rich TUI).")
            next_log = 0.0
            while True:
                now = time.time()
        
                # Enforce watchdog first so timed-out controls go idle promptly
                watchdog.enforce(hardware)
        
                # Periodic log line
                if now >= next_log:
                    next_log = now + 5.0
                    wd = watchdog.snapshot()
                    snap = telemetry.snapshot()
        
                    relay_summary = "K1=OFF"
                    try:
                        kmap = hardware.get_k_relays_state()
                        parts: list[str] = []
                        for ch in sorted(int(k) for k in kmap.keys()):
                            st = kmap.get(ch, {}) or {}
                            drive = bool(st.get("drive", False))
                            level = st.get("pin_level", None)
                            level_s = "--" if level is None else ("H" if bool(level) else "L")
                            parts.append(f"K{ch}={'ON' if drive else 'OFF'}(L={level_s})")
                        if parts:
                            relay_summary = " ".join(parts)
                    except Exception:
                        k1_drive = False
                        try:
                            k1_drive = bool(hardware.get_k1_drive())
                        except Exception:
                            k1_drive = bool(getattr(hardware.relay, "is_lit", False))
                        try:
                            k1_level = hardware.get_k1_pin_level()
                        except Exception:
                            k1_level = None
                        level_s = "--" if k1_level is None else ("H" if bool(k1_level) else "L")
                        relay_summary = f"K1={'ON' if k1_drive else 'OFF'}(L={level_s})"
        
                    load_pct, rx_fps, tx_fps = busload.snapshot() if busload else (None, None, None)
                    bus_str = '--' if load_pct is None else f"{load_pct:.1f}%"

                    _log(
                        f"{relay_summary} Bus={bus_str} "
                        f"Load={int(snap.get('load_volts_mV', 0))/1000:.3f}V {int(snap.get('load_current_mA', 0))/1000:.3f}A "
                        f"Meter={int(snap.get('meter_current_mA', 0))/1000:.3f}A "
                        f"WD={wd.get('timed_out')}"
                    )
        
                stop_event.wait(timeout=headless_tick_s)
        
        # Rich TUI loop
        else:
            with Live(console=console, screen=True, refresh_per_second=dash_fps) as live:
                render_period = 1.0 / float(dash_fps) if dash_fps > 0 else 0.1
                while True:
                    watchdog.enforce(hardware)
                    snap = telemetry.snapshot()
        
                    bus_load_pct, bus_rx_fps, bus_tx_fps = busload.snapshot() if busload else (None, None, None)
                    renderable = build_dashboard(
                        hardware,
                        meter_current_mA=int(snap.get("meter_current_mA", 0)),
                        mmeter_func_str=str(snap.get("mmeter_func_str", "")),
                        mmeter_primary_str=str(snap.get("mmeter_primary_str", "")),
                        mmeter_secondary_str=str(snap.get("mmeter_secondary_str", "")),
                        load_volts_mV=int(snap.get("load_volts_mV", 0)),
                        load_current_mA=int(snap.get("load_current_mA", 0)),
                        load_stat_func=str(snap.get("load_stat_func", "")),
                        load_stat_curr=str(snap.get("load_stat_curr", "")),
                        load_stat_res=str(snap.get("load_stat_res", "")),
                        load_stat_imp=str(snap.get("load_stat_imp", "")),
                        load_stat_short=str(snap.get("load_stat_short", "")),
                        afg_freq_read=str(snap.get("afg_freq_str", "")),
                        afg_ampl_read=str(snap.get("afg_ampl_str", "")),
                        afg_offset_read=str(snap.get("afg_offset_str", "0")),
                        afg_duty_read=str(snap.get("afg_duty_str", "50")),
                        afg_out_read=str(snap.get("afg_out_str", "")),
                        afg_shape_read=str(snap.get("afg_shape_str", "")),
                        mrs_id=str(snap.get("mrs_id_str", "")),
                        mrs_out=str(snap.get("mrs_out_str", "")),
                        mrs_mode=str(snap.get("mrs_mode_str", "")),
                        mrs_set=str(snap.get("mrs_set_str", "")),
                        mrs_in=str(snap.get("mrs_in_str", "")),
                        mrs_bo=str(snap.get("mrs_bo_str", "")),
                        can_channel=config.CAN_CHANNEL,
                        can_bitrate=int(config.CAN_BITRATE),
                        status_poll_period=status_period,
                        bus_load_pct=bus_load_pct,
                        bus_rx_fps=bus_rx_fps,
                        bus_tx_fps=bus_tx_fps,
                        pat_matrix=pat_matrix.snapshot(),
                        watchdog=watchdog.snapshot(),
                    )
                    live.update(renderable, refresh=True)
                    stop_event.wait(timeout=render_period)
        
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            if web_server is not None:
                web_server.stop()
        except Exception:
            pass

        stop_event.set()
        try:
            if "can_rx_thread" in locals():
                can_rx_thread.join(timeout=2.0)
            if "device_thread" in locals():
                device_thread.join(timeout=2.0)
            if "poll_thread" in locals():
                poll_thread.join(timeout=2.0)
            if 'tx_thread' in locals() and tx_thread:
                tx_thread.join(timeout=2.0)
        except Exception:
            pass

        try:
            if "cbus" in locals() and cbus:
                cbus.shutdown()
        except Exception:
            pass

        try:
            shutdown_can_interface(config.CAN_CHANNEL, do_setup=bool(config.CAN_SETUP) and (not args.no_can_setup))
        except Exception:
            pass

        try:
            hardware.close_devices()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
