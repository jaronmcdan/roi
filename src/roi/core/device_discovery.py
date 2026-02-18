"""USB / VISA device discovery for Raspberry Pi.

Goal: make ROI resilient to /dev/ttyUSB* renumbering.

We try to discover:
  - MULTI_METER_PATH (USB-serial multimeter)
  - MRSIGNAL_PORT (USB-serial Modbus)
  - AFG_VISA_ID (PyVISA ASRL resource)
  - ELOAD_VISA_ID (PyVISA USBTMC resource)

Discovery is best-effort and safe:
  - We keep timeouts short.
  - We prefer stable symlinks under /dev/serial/by-id when available.
  - If a configured value already works, we keep it.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pyvisa
import serial
from serial.tools import list_ports

from .. import config
from ..devices.mrsignal import MrSignalClient


LogFn = Callable[[str], None]


# -------- by-id helpers --------


def _serial_by_id_entries() -> List[Tuple[str, str, str]]:
    """List /dev/serial/by-id entries as (name, path, realpath)."""

    out: List[Tuple[str, str, str]] = []
    base = "/dev/serial/by-id"
    if not os.path.isdir(base):
        return out
    try:
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            try:
                real = os.path.realpath(p)
            except Exception:
                real = ""
            out.append((name, p, real))
    except Exception:
        return []
    return out


def _match_hint(name_l: str, hint_l: str) -> bool:
    """Return True if the hint matches the by-id name.

    - If hint contains glob metacharacters, use fnmatch.
    - Otherwise do a simple substring match.
    """

    if not hint_l:
        return False
    try:
        if any(ch in hint_l for ch in ("*", "?", "[")):
            import fnmatch

            return bool(fnmatch.fnmatch(name_l, hint_l))
    except Exception:
        pass
    return hint_l in name_l


def _pick_by_id(entries: Sequence[Tuple[str, str, str]], hints: Sequence[str]) -> Optional[str]:
    """Pick the best /dev/serial/by-id path matching the hints.

    Returns the by-id *symlink path* (not the real /dev/tty* node).
    """

    hs = [h.strip().lower() for h in (hints or []) if (h or "").strip()]
    if not entries or not hs:
        return None

    best_path: Optional[str] = None
    best_score = 0

    for name, path, _real in entries:
        nl = (name or "").lower()
        score = 0
        for h in hs:
            if _match_hint(nl, h):
                score += 1
        if score > best_score:
            best_score = score
            best_path = path

    return best_path if best_score > 0 else None


def _stable_asrl_resource_id(rid: str, *, prefer_by_id: bool = True) -> str:
    """If rid is ASRL/dev/ttyX::INSTR, rewrite to ASRL/dev/serial/by-id/...::INSTR when available."""

    dn = _asrl_devnode(rid)
    if not dn:
        return rid
    stable = _stable_serial_path(dn, prefer_by_id=prefer_by_id)
    if not stable or stable == dn:
        return rid
    try:
        i = rid.find(dn)
        if i >= 0:
            return rid[:i] + stable + rid[i + len(dn) :]
    except Exception:
        pass
    # Fallback: typical ASRL resource format
    return f"ASRL{stable}::INSTR"


def _split_hints(s: str) -> List[str]:
    toks = []
    for t in (s or "").split(","):
        t = t.strip().lower()
        if t:
            toks.append(t)
    return toks


def _contains_any(hay: str, needles: Sequence[str]) -> bool:
    h = (hay or "").lower()
    return any(n in h for n in needles)


def _asrl_devnode(resource_id: str) -> Optional[str]:
    """Extract /dev/... from an ASRL resource string when present."""
    if not (resource_id or "").startswith("ASRL"):
        return None
    if "/dev/" not in resource_id:
        return None
    start = resource_id.find("/dev/")
    end = resource_id.find("::", start)
    if end == -1:
        end = len(resource_id)
    return resource_id[start:end] or None


def _log(log_fn: Optional[LogFn], msg: str) -> None:
    if log_fn:
        try:
            log_fn(msg)
            return
        except Exception:
            pass
    print(msg)


def _stable_serial_path(dev: str, prefer_by_id: bool = True) -> str:
    """Return a stable /dev/serial/by-id (or by-path) symlink if it exists."""

    dev = os.path.realpath(dev)

    def _search_dir(d: str) -> Optional[str]:
        if not os.path.isdir(d):
            return None
        try:
            for name in sorted(os.listdir(d)):
                p = os.path.join(d, name)
                try:
                    if os.path.realpath(p) == dev:
                        return p
                except Exception:
                    continue
        except Exception:
            return None
        return None

    by_id = _search_dir("/dev/serial/by-id")
    by_path = _search_dir("/dev/serial/by-path")
    if prefer_by_id and by_id:
        return by_id
    if by_path:
        return by_path
    if by_id:
        return by_id
    return dev


def _serial_candidates() -> List[str]:
    """Return candidate serial device nodes (e.g. /dev/ttyUSB0, /dev/ttyACM0)."""
    out = []
    try:
        for p in list_ports.comports():
            if p.device:
                out.append(p.device)
    except Exception:
        pass
    # De-dupe while preserving order
    seen = set()
    uniq = []
    for d in out:
        if d not in seen:
            uniq.append(d)
            seen.add(d)
    return uniq


def _probe_multimeter_idn(port: str, baud: int) -> Optional[str]:
    """Try to read an ASCII *IDN? response from a serial multimeter."""
    try:
        with serial.Serial(
            port,
            int(baud),
            timeout=0.2,
            write_timeout=0.2,
        ) as s:
            try:
                s.reset_input_buffer()
                s.reset_output_buffer()
            except Exception:
                pass
            s.write(b"*IDN?\n")
            s.flush()
            time.sleep(0.05)

            # Some instruments echo the command then return IDN on the next line.
            idn: Optional[str] = None
            for _ in range(int(getattr(config, "MULTI_METER_IDN_READ_LINES", 4))):
                raw = s.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                if line.upper().startswith("*IDN?"):
                    continue
                # Prefer an IDN-like line.
                if ("," in line) or ("multimeter" in line.lower()) or ("5491" in line.lower()):
                    idn = line
                    break
                if idn is None:
                    idn = line
            return idn or None
    except Exception:
        return None


def _try_mrsignal_on_port(port: str) -> Tuple[bool, Optional[int]]:
    """Return (ok, device_id)"""
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
        st = client.read_status()
        client.close()
        if st and st.device_id is not None:
            return True, int(st.device_id)
    except Exception:
        pass
    return False, None


def _visa_rm() -> Optional[pyvisa.ResourceManager]:
    """Create a ResourceManager; prefer configured backend if set."""
    # Prefer the runtime backend if set; fall back to the autodetect backend.
    backend = str(getattr(config, "VISA_BACKEND", "") or "").strip()
    if not backend:
        backend = str(getattr(config, "AUTO_DETECT_VISA_BACKEND", "") or "").strip()
    try:
        if backend:
            return pyvisa.ResourceManager(backend)
        return pyvisa.ResourceManager()
    except Exception:
        # Best-effort fallback to pyvisa-py
        try:
            return pyvisa.ResourceManager("@py")
        except Exception:
            return None


def _probe_visa_idn(rm: pyvisa.ResourceManager, rid: str) -> Optional[str]:
    try:
        inst = rm.open_resource(rid)
        try:
            inst.timeout = int(getattr(config, "VISA_TIMEOUT_MS", 500))
        except Exception:
            pass

        # Be friendly to serial instruments
        try:
            inst.read_termination = "\n"
            inst.write_termination = "\n"
        except Exception:
            pass
        # Some serial SCPI devices need baud set. This can be risky if the ASRL
        # resource is not the intended instrument, so we make it configurable.
        if rid.startswith("ASRL") and bool(getattr(config, "AUTO_DETECT_VISA_PROBE_ASRL", True)):
            try:
                inst.baud_rate = int(getattr(config, "AUTO_DETECT_ASRL_BAUD", 115200))
            except Exception:
                pass

        try:
            txt = str(inst.query("*IDN?")).strip()
        finally:
            try:
                inst.close()
            except Exception:
                pass
        return txt or None
    except Exception:
        return None
@dataclass
class DiscoveryResult:
    multimeter_path: Optional[str] = None
    multimeter_idn: Optional[str] = None
    mrsignal_port: Optional[str] = None
    mrsignal_id: Optional[int] = None
    afg_visa_id: Optional[str] = None
    afg_idn: Optional[str] = None
    eload_visa_id: Optional[str] = None
    eload_idn: Optional[str] = None
    # Optional extras (not strictly required by ROI core, but useful on a closed system)
    can_channel: Optional[str] = None
    k1_serial_port: Optional[str] = None


def autodetect_and_patch_config(*, log_fn: Optional[LogFn] = None) -> DiscoveryResult:
    """Best-effort discovery. Mutates the imported config module in-place."""

    res = DiscoveryResult()

    if not bool(getattr(config, "AUTO_DETECT_ENABLE", True)):
        return res

    verbose = bool(getattr(config, "AUTO_DETECT_VERBOSE", True))
    prefer_by_id = bool(getattr(config, "AUTO_DETECT_PREFER_BY_ID", True))
    byid_only = bool(getattr(config, "AUTO_DETECT_BYID_ONLY", False))

    byid_entries = _serial_by_id_entries()
    if verbose:
        _log(log_fn, f"[autodetect] /dev/serial/by-id: {[n for (n, _p, _r) in byid_entries]}")

    mm_hints = _split_hints(str(getattr(config, "AUTO_DETECT_MMETER_IDN_HINTS", "") or ""))
    mm_byid_hints = _split_hints(
        str(getattr(config, "AUTO_DETECT_MMETER_BYID_HINTS", getattr(config, "AUTO_DETECT_MMETER_IDN_HINTS", "")) or "")
    )
    mrs_enabled = bool(getattr(config, "MRSIGNAL_ENABLE", False))
    mrs_byid_hints = _split_hints(str(getattr(config, "AUTO_DETECT_MRSIGNAL_BYID_HINTS", "") or ""))
    afg_hints = _split_hints(str(getattr(config, "AUTO_DETECT_AFG_IDN_HINTS", "") or ""))
    afg_byid_hints = _split_hints(str(getattr(config, "AUTO_DETECT_AFG_BYID_HINTS", "") or ""))
    eload_hints = _split_hints(str(getattr(config, "AUTO_DETECT_ELOAD_IDN_HINTS", "") or ""))

    # ---- Closed-system helpers: map by-id names to config *without* probing ----
    # CAN backend auto-select:
    #   - Prefer an RM/Proemion CANview gateway (rmcanview) when present
    #   - Otherwise fall back to SocketCAN
    #
    # This is intentionally best-effort. If you need to force a backend, disable
    # AUTO_DETECT_CANVIEW (or AUTO_DETECT_ENABLE) and set CAN_INTERFACE/CAN_CHANNEL.
    if bool(getattr(config, "AUTO_DETECT_CANVIEW", True)):
        iface = str(getattr(config, "CAN_INTERFACE", "socketcan") or "socketcan").strip().lower()

        # Remember the current SocketCAN channel as the fallback *before* we
        # potentially overwrite CAN_CHANNEL with a /dev/... serial path.
        cur_chan = str(getattr(config, "CAN_CHANNEL", "") or "").strip()
        socket_fallback = cur_chan if (cur_chan and (not cur_chan.startswith("/dev/"))) else "can0"

        can_hints = _split_hints(str(getattr(config, "AUTO_DETECT_CANVIEW_BYID_HINTS", "") or ""))
        cand = _pick_by_id(byid_entries, can_hints) if (byid_entries and can_hints) else None

        # Only auto-switch for the two supported backends. If the user selected a
        # different python-can interface, don't override it.
        auto_ok = iface in (
            "auto",
            "socketcan",
            "socketcan_native",
            "socketcan_ctypes",
            "rmcanview",
            "rm-canview",
            "proemion",
        )

        if cand and auto_ok:
            # CANview present: switch to rmcanview + pin channel to stable by-id path
            setattr(config, "CAN_INTERFACE", "rmcanview")
            setattr(config, "CAN_CHANNEL", cand)
            res.can_channel = cand
            if verbose:
                _log(log_fn, f"[autodetect] CAN backend: rmcanview ({cand})")

        elif auto_ok:
            # No CANview present: ensure we are on SocketCAN and have a sane netdev channel
            if iface in ("rmcanview", "rm-canview", "proemion", "auto"):
                setattr(config, "CAN_INTERFACE", "socketcan")
                setattr(config, "CAN_CHANNEL", socket_fallback)
                if verbose:
                    _log(log_fn, f"[autodetect] CAN backend: socketcan ({socket_fallback})")

            # If socketcan is selected but CAN_CHANNEL is a /dev/... path (misconfig),
            # fall back to the remembered netdev name.
            cur2 = str(getattr(config, "CAN_CHANNEL", "") or "").strip()
            if cur2.startswith("/dev/") or (not cur2):
                setattr(config, "CAN_CHANNEL", socket_fallback)

        # If we ended up on rmcanview, normalize any volatile /dev/tty* node to a
        # stable symlink, and if still unset try by-id matching.
        iface2 = str(getattr(config, "CAN_INTERFACE", "socketcan") or "socketcan").strip().lower()
        if iface2 in ("rmcanview", "rm-canview", "proemion"):
            cur = str(getattr(config, "CAN_CHANNEL", "") or "").strip()
            if cur.startswith("/dev/"):
                stable = _stable_serial_path(cur, prefer_by_id=prefer_by_id)
                if stable and stable != cur:
                    setattr(config, "CAN_CHANNEL", stable)
                    res.can_channel = stable
                    if verbose:
                        _log(log_fn, f"[autodetect] canview: {stable} (from {cur})")

            cur3 = str(getattr(config, "CAN_CHANNEL", "") or "").strip()
            if (not cur3) or cur3.startswith("/dev/tty"):
                cand2 = cand or (_pick_by_id(byid_entries, can_hints) if (byid_entries and can_hints) else None)
                if cand2:
                    setattr(config, "CAN_CHANNEL", cand2)
                    res.can_channel = cand2
                    if verbose:
                        _log(log_fn, f"[autodetect] canview: {cand2}")

    # USB relay controller (K1 serial backend; Arduino/DSD Tech style)
    if bool(getattr(config, "AUTO_DETECT_K1_SERIAL", True)) and bool(getattr(config, "K1_ENABLE", True)):
        cur = str(getattr(config, "K1_SERIAL_PORT", "") or "").strip()
        if cur:
            stable = _stable_serial_path(cur, prefer_by_id=prefer_by_id)
            if stable and stable != cur:
                setattr(config, "K1_SERIAL_PORT", stable)
                res.k1_serial_port = stable
                if verbose:
                    _log(log_fn, f"[autodetect] k1 serial: {stable} (from {cur})")
        cur2 = str(getattr(config, "K1_SERIAL_PORT", "") or "").strip()
        if not cur2:
            k1_hints = _split_hints(str(getattr(config, "AUTO_DETECT_K1_BYID_HINTS", "") or ""))
            cand = _pick_by_id(byid_entries, k1_hints)
            if cand:
                setattr(config, "K1_SERIAL_PORT", cand)
                res.k1_serial_port = cand
                if verbose:
                    _log(log_fn, f"[autodetect] k1 serial: {cand}")

    # --- Serial discovery (multimeter + MrSignal) ---
    ports = _serial_candidates()
    if verbose:
        _log(log_fn, f"[autodetect] serial ports: {ports}")

    # Build a skip list for *serial probing* so we don't spam unrelated ports.
    reserved_realpaths = set()
    for p in (
        str(getattr(config, "CAN_CHANNEL", "") or "").strip(),
        str(getattr(config, "K1_SERIAL_PORT", "") or "").strip(),
        str(getattr(config, "MULTI_METER_PATH", "") or "").strip(),
        str(getattr(config, "MRSIGNAL_PORT", "") or "").strip(),
    ):
        if not p or not p.startswith("/dev/"):
            continue
        try:
            reserved_realpaths.add(os.path.realpath(p))
        except Exception:
            reserved_realpaths.add(p)

    def _is_reserved(dev: str) -> bool:
        try:
            return os.path.realpath(dev) in reserved_realpaths
        except Exception:
            return dev in reserved_realpaths

    # Multimeter
    if bool(getattr(config, "AUTO_DETECT_MMETER", True)):
        baud = int(getattr(config, "MULTI_METER_BAUD", 38400))

        # 0) If config points at a volatile /dev/tty*, rewrite to a stable by-id symlink.
        cur = str(getattr(config, "MULTI_METER_PATH", "") or "").strip()
        if cur and cur.startswith("/dev/tty"):
            stable = _stable_serial_path(cur, prefer_by_id=prefer_by_id)
            if stable and stable != cur:
                setattr(config, "MULTI_METER_PATH", stable)
                cur = stable

        # 1) Try by-id name matching first (closed system = safest).
        if not res.multimeter_path and byid_entries and mm_byid_hints:
            cand = _pick_by_id(byid_entries, mm_byid_hints)
            if cand:
                idn = _probe_multimeter_idn(cand, baud)
                if idn and (not mm_hints or _contains_any(idn, mm_hints)):
                    res.multimeter_path = _stable_serial_path(cand, prefer_by_id=prefer_by_id)
                    res.multimeter_idn = idn
                elif byid_only:
                    # Trust the by-id mapping even if *IDN? doesn't respond.
                    res.multimeter_path = _stable_serial_path(cand, prefer_by_id=prefer_by_id)
                    res.multimeter_idn = idn

        # 2) Keep current if it answers with expected IDN
        if (not res.multimeter_path) and cur:
            idn = _probe_multimeter_idn(cur, baud)
            if idn and (not mm_hints or _contains_any(idn, mm_hints)):
                res.multimeter_path = _stable_serial_path(cur, prefer_by_id=prefer_by_id)
                res.multimeter_idn = idn

        # 3) As a fallback, probe other serial ports (unless by-id-only mode is set).
        if (not res.multimeter_path) and (not byid_only):
            for dev in ports:
                if cur and os.path.realpath(dev) == os.path.realpath(cur):
                    continue
                # Skip ports we already know are used by other roles.
                if _is_reserved(dev):
                    continue
                idn = _probe_multimeter_idn(dev, baud)
                if not idn:
                    continue
                if mm_hints and not _contains_any(idn, mm_hints):
                    continue
                res.multimeter_path = _stable_serial_path(dev, prefer_by_id=prefer_by_id)
                res.multimeter_idn = idn
                break

        if res.multimeter_path:
            setattr(config, "MULTI_METER_PATH", res.multimeter_path)
            # Cache the IDN so HardwareManager can avoid re-querying during
            # early boot (some meters beep/throw a bus error if multiple
            # subsystems touch the port during startup).
            if res.multimeter_idn:
                setattr(config, "MULTI_METER_IDN", res.multimeter_idn)
            if verbose:
                _log(log_fn, f"[autodetect] multimeter: {res.multimeter_path} ({res.multimeter_idn})")

    # MrSignal: only if enabled
    if mrs_enabled and bool(getattr(config, "AUTO_DETECT_MRSIGNAL", True)):
        # 0) If config points at a volatile /dev/tty*, rewrite to stable by-id.
        cur = str(getattr(config, "MRSIGNAL_PORT", "") or "").strip()
        if cur and cur.startswith("/dev/tty"):
            stable = _stable_serial_path(cur, prefer_by_id=prefer_by_id)
            if stable and stable != cur:
                setattr(config, "MRSIGNAL_PORT", stable)
                cur = stable

        # 1) Try by-id matching first.
        if not res.mrsignal_port and byid_entries and mrs_byid_hints:
            cand = _pick_by_id(byid_entries, mrs_byid_hints)
            if cand:
                ok, dev_id = _try_mrsignal_on_port(cand)
                if ok:
                    res.mrsignal_port = _stable_serial_path(cand, prefer_by_id=prefer_by_id)
                    res.mrsignal_id = dev_id
                elif byid_only:
                    res.mrsignal_port = _stable_serial_path(cand, prefer_by_id=prefer_by_id)
                    res.mrsignal_id = dev_id

        # 2) Keep current if it works
        if (not res.mrsignal_port) and cur:
            ok, dev_id = _try_mrsignal_on_port(cur)
            if ok:
                res.mrsignal_port = _stable_serial_path(cur, prefer_by_id=prefer_by_id)
                res.mrsignal_id = dev_id

        # 3) Fallback scan across serial ports (unless by-id-only mode).
        if (not res.mrsignal_port) and (not byid_only):
            for dev in ports:
                # Avoid probing the same port chosen for the multimeter
                try:
                    if res.multimeter_path and os.path.realpath(dev) == os.path.realpath(res.multimeter_path):
                        continue
                except Exception:
                    pass
                # Skip ports we already know are used by other roles.
                if _is_reserved(dev):
                    continue
                ok, dev_id = _try_mrsignal_on_port(dev)
                if not ok:
                    continue
                res.mrsignal_port = _stable_serial_path(dev, prefer_by_id=prefer_by_id)
                res.mrsignal_id = dev_id
                break

        if res.mrsignal_port:
            setattr(config, "MRSIGNAL_PORT", res.mrsignal_port)
            if verbose:
                _log(log_fn, f"[autodetect] mrsignal: {res.mrsignal_port} (id={res.mrsignal_id})")

    # --- VISA discovery (E-Load + AFG) ---
    if bool(getattr(config, "AUTO_DETECT_VISA", True)):
        rm = _visa_rm()
        if rm is None:
            if verbose:
                _log(log_fn, "[autodetect] pyvisa resource manager unavailable; skipping VISA discovery")
        else:
            try:
                rids = list(rm.list_resources())
            except Exception:
                rids = []

            # NOTE: rm.list_resources() may include ASRL/dev/ttyUSB* entries for
            # generic USB-serial ports (including the 5491B DMM). Seeing them
            # in this *raw* list is normal. We must never *probe* them.
            if verbose:
                _log(log_fn, f"[autodetect] visa resources (raw): {rids}")
            # Narrow to USB + serial instruments.
            # IMPORTANT: probing an ASRL resource sends bytes over a serial port.
            # If that port belongs to some other device (e.g., the 5491B DMM), it
            # can show a "bus command error". We therefore exclude:
            #   - onboard UARTs / console ports (ttyAMA*, ttyS*)
            #   - serial ports already claimed by discovered devices (multimeter, MrSignal)
            #   - ASRL probing entirely when AUTO_DETECT_VISA_PROBE_ASRL=0
            exclude_prefixes = _split_hints(
                str(getattr(config, "AUTO_DETECT_VISA_ASRL_EXCLUDE_PREFIXES", "") or "")
            )
            allow_prefixes = _split_hints(
                str(getattr(config, "AUTO_DETECT_VISA_ASRL_ALLOW_PREFIXES", "") or "")
            )

            # Safety defaults: even if env vars are empty/cleared, never probe
            # generic USB-serial ports (/dev/ttyUSB*) via VISA ASRL. Only probe
            # CDC-ACM (/dev/ttyACM*) unless the user explicitly changes code.
            if not allow_prefixes:
                allow_prefixes = ["/dev/ttyacm"]
            if not exclude_prefixes:
                exclude_prefixes = ["/dev/ttyama", "/dev/ttys", "/dev/ttyusb"]
            else:
                if "/dev/ttyusb" not in exclude_prefixes:
                    exclude_prefixes.append("/dev/ttyusb")

            # Build a realpath set for serial devices we must not poke via VISA.
            # This is stricter than the *serial probing* skip list.
            skip_serial_realpaths = set()
            for p in [
                res.multimeter_path,
                res.mrsignal_port,
                res.k1_serial_port,
                res.can_channel,
                str(getattr(config, "MULTI_METER_PATH", "") or "").strip() or None,
                str(getattr(config, "MRSIGNAL_PORT", "") or "").strip() or None,
                str(getattr(config, "K1_SERIAL_PORT", "") or "").strip() or None,
                str(getattr(config, "CAN_CHANNEL", "") or "").strip() or None,
            ]:
                if not p:
                    continue
                # Only add true device nodes/paths; skip interface names like "can0".
                if isinstance(p, str) and (not p.startswith("/dev/")):
                    continue
                try:
                    skip_serial_realpaths.add(os.path.realpath(p))
                except Exception:
                    try:
                        skip_serial_realpaths.add(str(p))
                    except Exception:
                        continue

            # If we can identify the AFG by its /dev/serial/by-id name, probe it first
            # (and optionally probe *only* it in AUTO_DETECT_BYID_ONLY mode).
            afg_byid_rid: Optional[str] = None
            afg_byid_real: Optional[str] = None
            if afg_byid_hints and byid_entries and bool(getattr(config, "AUTO_DETECT_AFG", True)):
                byid_path = _pick_by_id(byid_entries, afg_byid_hints)
                if byid_path:
                    afg_byid_rid = f"ASRL{byid_path}::INSTR"
                    try:
                        dn = _asrl_devnode(afg_byid_rid)
                        if dn:
                            afg_byid_real = os.path.realpath(dn)
                    except Exception:
                        afg_byid_real = None
                    if verbose:
                        _log(log_fn, f"[autodetect] afg by-id candidate: {afg_byid_rid}")

            cand: List[str] = []
            if afg_byid_rid:
                cand.append(afg_byid_rid)

            for r in rids:
                if r.startswith("USB"):
                    cand.append(r)
                    continue
                if not r.startswith("ASRL"):
                    continue
                if not bool(getattr(config, "AUTO_DETECT_VISA_PROBE_ASRL", True)):
                    continue
                devnode = _asrl_devnode(r)
                if devnode:
                    # Exclude obvious non-instrument serial ports.
                    dn_real = None
                    try:
                        dn_real = os.path.realpath(devnode)
                    except Exception:
                        dn_real = devnode

                    # In closed-system (by-id-only) mode, never probe random ASRL ports.
                    # Only probe the AFG candidate if we have one.
                    if byid_only and afg_byid_real and dn_real and (dn_real != afg_byid_real):
                        if verbose:
                            _log(log_fn, f"[autodetect] skip VISA probe on {r} (by-id-only)")
                        continue

                    dn_l = (dn_real or devnode).lower()

                    # Hard safety: never send *IDN? over VISA ASRL to /dev/ttyUSB*
                    # devices. These are often USB-serial adapters (including the
                    # 5491B multimeter). Probing them at the wrong baud can make
                    # them beep / throw a "bus command error" and stop answering.
                    if dn_l.startswith("/dev/ttyusb"):
                        if verbose:
                            _log(log_fn, f"[autodetect] skip VISA probe on {r} (unsafe ttyUSB)")
                        continue

                    # Allow-list (if configured): only probe these.
                    if allow_prefixes and not any(dn_l.startswith(pfx) for pfx in allow_prefixes):
                        if verbose:
                            _log(log_fn, f"[autodetect] skip VISA probe on {r} (not in allow-list)")
                        continue

                    # Exclude-list: never probe these.
                    if exclude_prefixes and any(dn_l.startswith(pfx) for pfx in exclude_prefixes):
                        if verbose:
                            _log(log_fn, f"[autodetect] skip VISA probe on {r} (in exclude-list)")
                        continue
                    try:
                        if (dn_real or os.path.realpath(devnode)) in skip_serial_realpaths:
                            if verbose:
                                _log(log_fn, f"[autodetect] skip VISA probe on {r} (claimed by serial device)")
                            continue
                    except Exception:
                        pass
                cand.append(r)

            # De-dupe while preserving order
            seen = set()
            cand = [x for x in cand if not (x in seen or seen.add(x))]
            if verbose:
                _log(log_fn, f"[autodetect] visa resources: {cand}")

            idn_map: Dict[str, str] = {}
            for rid in cand:
                idn = _probe_visa_idn(rm, rid)
                if idn:
                    idn_map[rid] = idn
                    if verbose:
                        _log(log_fn, f"[autodetect] visa idn: {rid} -> {idn}")

            # Prefer configured patterns first
            cfg_eload_pat = str(getattr(config, "ELOAD_VISA_ID", "") or "").strip()
            cfg_afg = str(getattr(config, "AFG_VISA_ID", "") or "").strip()

            # E-load
            if bool(getattr(config, "AUTO_DETECT_ELOAD", True)):
                # 1) if current config matches a discovered resource, keep it
                chosen = None
                chosen_idn = None
                for rid, idn in idn_map.items():
                    try:
                        import fnmatch
                        if cfg_eload_pat and fnmatch.fnmatch(rid, cfg_eload_pat):
                            chosen = rid
                            chosen_idn = idn
                            break
                    except Exception:
                        pass
                # 2) otherwise match by IDN hints
                if not chosen and eload_hints:
                    for rid, idn in idn_map.items():
                        if rid.startswith("USB") and _contains_any(idn, eload_hints):
                            chosen = rid
                            chosen_idn = idn
                            break
                if chosen:
                    res.eload_visa_id = chosen
                    res.eload_idn = chosen_idn
                    setattr(config, "ELOAD_VISA_ID", chosen)
                    if verbose:
                        _log(log_fn, f"[autodetect] eload: {chosen} ({chosen_idn})")

            # AFG
            if bool(getattr(config, "AUTO_DETECT_AFG", True)):
                chosen = None
                chosen_idn = None
                # 1) if configured AFG is present and responds, keep it
                if cfg_afg and cfg_afg in idn_map:
                    chosen = cfg_afg
                    chosen_idn = idn_map.get(cfg_afg)
                # 2) otherwise match by IDN hints over ASRL resources
                if not chosen and afg_hints:
                    for rid, idn in idn_map.items():
                        if rid.startswith("ASRL") and _contains_any(idn, afg_hints):
                            chosen = rid
                            chosen_idn = idn
                            break
                # 3) as a fallback, pick *any* ASRL device with an IDN response
                if not chosen:
                    for rid, idn in idn_map.items():
                        if rid.startswith("ASRL"):
                            chosen = rid
                            chosen_idn = idn
                            break
                if chosen:
                    chosen_stable = _stable_asrl_resource_id(chosen, prefer_by_id=prefer_by_id)
                    res.afg_visa_id = chosen_stable
                    res.afg_idn = chosen_idn
                    setattr(config, "AFG_VISA_ID", chosen_stable)
                    if verbose:
                        extra = "" if chosen_stable == chosen else f" (stable={chosen_stable})"
                        _log(log_fn, f"[autodetect] afg: {chosen} ({chosen_idn}){extra}")

            try:
                rm.close()
            except Exception:
                pass

    return res
