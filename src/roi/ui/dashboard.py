# dashboard.py
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from rich import box

import os
import re

from .. import config

from ..devices.bk5491b import func_name

# Optional: PAT switch-matrix J0 pin labels (parsed from PAT.dbc if available)
try:
    from ..core.pat_matrix import j0_pin_names as _pat_j0_pin_names
except Exception:
    _pat_j0_pin_names = lambda: {}

# Try to initialize Rich Console
try:
    console = Console(highlight=False)
    HAVE_RICH = True
except Exception:
    console = None
    HAVE_RICH = False

def _badge(ok: bool, true_label="ON", false_label="OFF"):
    """Helper to create color-coded text badges."""
    return f"[bold {'green' if ok else 'red'}]{true_label if ok else false_label}[/]"

def _short_can_channel(can_channel: str) -> str:
    """Make CAN channel labels compact for the dashboard.

    When using the rmcanview backend, CAN_CHANNEL is often a long /dev/serial/by-id
    path. For display we shorten it to something like 'CANview-USB' or 'ttyUSB0'.
    """
    ch = str(can_channel or '').strip()
    if not ch:
        return '--'

    iface = str(getattr(config, 'CAN_INTERFACE', 'socketcan') or 'socketcan').strip().lower()
    if iface not in ('rmcanview', 'rm-canview', 'proemion'):
        return ch

    base = os.path.basename(ch)
    low = base.lower()

    # Common by-id example:
    #   usb-RM_Michaelides_RM_CANview-USB-if00-port0
    if 'canview' in low:
        idx = low.find('canview')
        s = base[idx:]
        # Drop interface/port suffixes.
        s = re.sub(r'-if\d+.*$', '', s)
        s = re.sub(r'[_-]port\d+$', '', s)
        return s

    # Otherwise, just show the basename (e.g. ttyUSB0).
    return base


def _wd_cell(watchdog, key: str) -> str:
    """Format watchdog age/state for a given device key.

    The watchdog snapshot is produced by WatchdogManager.snapshot() in main.py.
    """
    try:
        if not watchdog or not isinstance(watchdog, dict):
            return "[dim]--[/]"

        ages = watchdog.get("ages", {}) or {}
        states = watchdog.get("states", {}) or {}
        timed_out = watchdog.get("timed_out", {}) or {}

        age = ages.get(key)
        st = str(states.get(key) or "").strip().lower()
        to = bool(timed_out.get(key, False))

        if age is None:
            return "[dim]--[/]"

        # Prefer explicit state.
        if st == "warn":
            return f"[yellow]LAG {age:.1f}s[/]"
        if st in ("to", "timeout") or to:
            return f"[red]TO {age:.1f}s[/]"
        return f"[green]{age:.1f}s[/]"
    except Exception:
        return "[dim]--[/]"

def build_dashboard(hardware, *,
                    meter_current_mA: int,
                    mmeter_func_str: str = "",
                    mmeter_primary_str: str = "",
                    mmeter_secondary_str: str = "",
                    load_volts_mV: int,
                    load_current_mA: int,
                    load_stat_func: str,
                    load_stat_curr: str,
                    load_stat_res: str,
                    load_stat_imp: str,
                    load_stat_short: str,
                    afg_freq_read: str,
                    afg_ampl_read: str,
                    afg_offset_read: str,
                    afg_duty_read: str,
                    afg_out_read: str,
                    afg_shape_read: str,
                    mrs_id: str = "",
                    mrs_out: str = "",
                    mrs_mode: str = "",
                    mrs_set: str = "",
                    mrs_in: str = "",
                    mrs_bo: str = "",
                    can_channel: str,
                    can_bitrate: int,
                    status_poll_period: float,
                    bus_load_pct=None,
                    bus_rx_fps=None,
                    bus_tx_fps=None,
                    pat_matrix=None,
                    watchdog=None):
    
    if not HAVE_RICH:
        mm = mmeter_primary_str or f"{meter_current_mA}mA"
        return f"E-Load V: {load_volts_mV}mV | AFG Freq: {afg_freq_read} | Meter: {mm}"

    # Dashboard layout is organized primarily by *device*, so each panel is
    # self-contained (status + key readings + watchdog freshness).
    #
    # We also reserve a small footer bar for the PAT switching-matrix view
    # (PAT_J0..PAT_J5).
    layout = Layout()
    layout.split(
        Layout(name="grid", ratio=1),
        # Two-line PAT footer (J0..J5 in a 2x3 grid) + panel border.
        Layout(name="pat", size=4),
    )

    layout["grid"].split(
        Layout(name="row1", ratio=1),
        Layout(name="row2", ratio=1),
    )

    def _pat_active_list(vals, *, names: dict[int, str] | None = None, show_pin: bool = True) -> str:
        """Render PAT_Jx values as a compact list of *active* pins.

        Instead of drawing a 12-char bitmap, we list the pin numbers (1..12)
        that are non-zero.

        - Pin numbers (or names) are color-coded by the 2-bit value:
            1 -> green, 2 -> yellow, 3 -> red
        - If `names` is provided (e.g. for J0), the label becomes:
            "<pin>:<name>" (still color-coded)
        """

        try:
            if not vals or len(vals) < 12:
                return "[dim]n/a[/]"

            style_map = {1: "green", 2: "yellow", 3: "red"}
            items = []
            for pin, v in enumerate(list(vals)[:12], start=1):
                vi = int(v) & 0x3
                if vi == 0:
                    continue
                st = style_map.get(vi, "red")
                if isinstance(names, dict) and pin in names:
                    label = f"{pin}:{names.get(pin, '')}" if show_pin else str(names.get(pin, ''))
                else:
                    label = str(pin)
                items.append(f"[{st}]{label}[/]")

            return " ".join(items) if items else "[dim]--[/]"
        except Exception:
            return "[dim]--[/]"

    layout["row1"].split_row(
        Layout(name="eload"),
        Layout(name="afg"),
        Layout(name="mmeter"),
    )
    layout["row2"].split_row(
        Layout(name="mrsignal"),
        Layout(name="k1"),
        Layout(name="can"),
    )

    # --------------------
    # E-LOAD panel
    # --------------------
    eload_table = Table.grid(padding=(0, 1))
    eload_table.add_column(justify="right", style="bold cyan", no_wrap=True)
    eload_table.add_column()
    eload_table.add_row("WD", _wd_cell(watchdog, "eload"))
    if hardware.e_load:
        visa_id = getattr(hardware.e_load, "resource_name", "")
        eload_table.add_row("ID", f"[white]{visa_id}[/]")

        # Prefer polled status when available, but fall back to the last
        # commanded state to reduce perceived UI lag.
        if str(load_stat_imp or "").strip() != "":
            el_on = str(load_stat_imp or "").strip().upper() in ["ON", "1"]
        else:
            el_on = bool(getattr(hardware, "e_load_enabled", 0))
        eload_table.add_row("Enable", f"{_badge(el_on)}")

        mode_polled = str(load_stat_func or "").strip()
        if mode_polled:
            mode_str = mode_polled
        else:
            mode_str = "RES" if bool(getattr(hardware, "e_load_mode", 0)) else "CURR"
        eload_table.add_row("Mode", f"[white]{mode_str}[/]")

        mode_u = mode_str.strip().upper()
        if mode_u.startswith("CURR"):
            sp = str(load_stat_curr or "").strip()
            if not sp:
                try:
                    sp = f"{float(getattr(hardware, 'e_load_csetting', 0)) / 1000.0:g}"
                except Exception:
                    sp = ""
            eload_table.add_row("Set (I)", f"[yellow]{sp}[/]")
        elif mode_u.startswith("RES"):
            sp = str(load_stat_res or "").strip()
            if not sp:
                try:
                    sp = f"{float(getattr(hardware, 'e_load_rsetting', 0)) / 1000.0:g}"
                except Exception:
                    sp = ""
            eload_table.add_row("Set (R)", f"[yellow]{sp}[/]")
        else:
            # Unknown: best-effort
            sp = (str(load_stat_curr or "").strip() or str(load_stat_res or "").strip())
            eload_table.add_row("Set", f"[yellow]{sp}[/]")

        # Key measurements
        eload_table.add_row("Meas V", f"[green]{load_volts_mV/1000:.3f} V[/]")
        eload_table.add_row("Meas I", f"[green]{load_current_mA/1000:.3f} A[/]")

        # Short state (when available)
        short_s = str(load_stat_short or "").strip()
        if short_s:
            short_on = short_s.upper() in ["ON", "1"]
            eload_table.add_row("Short", _badge(short_on, "ON", "OFF"))
    else:
        eload_table.add_row("Status", "[red]NOT DETECTED[/]")

    # --------------------
    # AFG panel
    # --------------------
    afg_table = Table.grid(padding=(0, 1))
    afg_table.add_column(justify="right", style="bold green", no_wrap=True)
    afg_table.add_column()
    afg_table.add_row("WD", _wd_cell(watchdog, "afg"))
    if hardware.afg:
        afg_table.add_row("ID", f"[white]{hardware.afg_id or 'Unknown'}[/]")

        out_polled = str(afg_out_read or "").strip()
        if out_polled:
            is_on = out_polled.upper() in ["ON", "1"]
        else:
            is_on = bool(getattr(hardware, "afg_output", False))
        afg_table.add_row("Output", _badge(is_on))

        freq = str(afg_freq_read or "").strip()
        if not freq:
            try:
                freq = str(int(getattr(hardware, "afg_freq", 0) or 0))
            except Exception:
                freq = ""
        ampl = str(afg_ampl_read or "").strip()
        if not ampl:
            try:
                ampl = f"{float(getattr(hardware, 'afg_ampl', 0) or 0) / 1000.0:g}"
            except Exception:
                ampl = ""
        offs = str(afg_offset_read or "").strip()
        if not offs:
            try:
                offs = f"{float(getattr(hardware, 'afg_offset', 0) or 0) / 1000.0:g}"
            except Exception:
                offs = ""

        afg_table.add_row("Freq", f"[yellow]{freq} Hz[/]")
        afg_table.add_row("Ampl", f"[yellow]{ampl} Vpp[/]")
        afg_table.add_row("Offset", f"[cyan]{offs} V[/]")

        shape_polled = str(afg_shape_read or "").strip()
        if not shape_polled:
            shape_polled = {0: "SIN", 1: "SQU", 2: "RAMP"}.get(int(getattr(hardware, "afg_shape", 0) or 0), "")

        duty = str(afg_duty_read or "").strip()
        if not duty:
            try:
                duty = str(int(getattr(hardware, "afg_duty", 50) or 50))
            except Exception:
                duty = ""

        duty_style = "yellow" if "SQU" in str(shape_polled).upper() else "dim white"
        afg_table.add_row("Duty", f"[{duty_style}]{duty} %[/]")
        afg_table.add_row("Shape", f"[white]{shape_polled}[/]")
    else:
        afg_table.add_row("Status", "[red]NOT DETECTED[/]")

    # --------------------
    # Multimeter panel
    # --------------------
    meter_table = Table.grid(padding=(0, 1))
    meter_table.add_column(justify="right", style="bold magenta", no_wrap=True)
    meter_table.add_column()
    meter_table.add_row("WD", _wd_cell(watchdog, "mmeter"))
    meter_table.add_row("ID", f"[white]{hardware.mmeter_id or '—'}[/]")

    try:
        f_i = int(getattr(hardware, "mmeter_func", 0)) & 0xFF
        f2_i = int(getattr(hardware, "mmeter_func2", f_i)) & 0xFF
        f2_en = bool(getattr(hardware, "mmeter_func2_enabled", False))
        auto = bool(getattr(hardware, "mmeter_autorange", True))
        rng_val = float(getattr(hardware, "mmeter_range_value", 0.0) or 0.0)
        nplc = float(getattr(hardware, "mmeter_nplc", 1.0) or 1.0)
        rel = bool(getattr(hardware, "mmeter_rel_enabled", False))
        trig = int(getattr(hardware, "mmeter_trig_source", 0)) & 0xFF
    except Exception:
        f_i, f2_i, f2_en, auto, rng_val, nplc, rel, trig = 0, 0, False, True, 0.0, 1.0, False, 0

    meter_table.add_row("Func", f"[yellow]{func_name(f_i)}[/]")
    meter_table.add_row("Range", f"[white]{'AUTO' if auto else (f'{rng_val:g}' if rng_val else '--')}[/]")
    meter_table.add_row("NPLC", f"[white]{nplc:g}[/]")
    meter_table.add_row("Rel", _badge(rel, "ON", "OFF"))
    trig_name = {0: "IMM", 1: "BUS", 2: "MAN"}.get(trig, str(trig))
    meter_table.add_row("Trig", f"[white]{trig_name}[/]")
    meter_table.add_row("Func2", f"[white]{func_name(f2_i) if f2_en else 'OFF'}[/]")

    # Key measurements (shown inside the device panel so it's device-categorized)
    if mmeter_primary_str:
        if mmeter_func_str:
            meter_table.add_row("Meas", f"[yellow]{mmeter_func_str}[/]")
        meter_table.add_row("Val", f"[yellow]{mmeter_primary_str}[/]")
        if mmeter_secondary_str:
            meter_table.add_row("Val2", f"[yellow]{mmeter_secondary_str}[/]")
    else:
        meter_table.add_row("Val", f"[yellow]{meter_current_mA/1000:.3f} A[/]")

    # --------------------
    # MrSignal panel
    # --------------------
    mrs_table = Table.grid(padding=(0, 1))
    mrs_table.add_column(justify="right", style="bold white", no_wrap=True)
    mrs_table.add_column()
    mrs_table.add_row("WD", _wd_cell(watchdog, "mrsignal"))
    if getattr(hardware, "mrsignal", None):
        mrs_table.add_row("ID", f"[white]{mrs_id or getattr(hardware, 'mrsignal_id', '—') or '—'}[/]")

        out_polled = str(mrs_out or "").strip()
        if out_polled:
            out_on = out_polled.upper() in ["ON", "1", "TRUE"]
        else:
            out_on = bool(getattr(hardware, "mrsignal_output_on", False))
        mrs_table.add_row("Output", _badge(out_on, "ON", "OFF"))

        mode_label = str(mrs_mode or "").strip()
        if not mode_label:
            sel = int(getattr(hardware, "mrsignal_output_select", 0) or 0)
            mode_label = {0: "mA", 1: "V", 2: "XMT", 3: "PULSE", 4: "mV", 5: "R", 6: "24V"}.get(sel, "—")
        mrs_table.add_row("Mode", f"[white]{mode_label or '—'}[/]")

        set_str = str(mrs_set or "").strip()
        if not set_str:
            try:
                sel = int(getattr(hardware, "mrsignal_output_select", 0) or 0)
                v = float(getattr(hardware, "mrsignal_output_value", 0.0) or 0.0)
                if sel == 0:
                    set_str = f"{v:.4g} mA"
                elif sel == 4:
                    set_str = f"{v:.4g} mV"
                else:
                    set_str = f"{v:.4g} V"
            except Exception:
                set_str = ""
        mrs_table.add_row("Set", f"[yellow]{set_str or '—'}[/]")

        in_str = str(mrs_in or "").strip()
        if not in_str:
            try:
                sel = int(getattr(hardware, "mrsignal_output_select", 0) or 0)
                v = float(getattr(hardware, "mrsignal_input_value", 0.0) or 0.0)
                if sel == 0:
                    in_str = f"{v:.4g} mA"
                elif sel == 4:
                    in_str = f"{v:.4g} mV"
                else:
                    in_str = f"{v:.4g} V"
            except Exception:
                in_str = ""
        mrs_table.add_row("Input", f"[cyan]{in_str or '—'}[/]")

        bo = str(mrs_bo or "").strip() or str(getattr(hardware, "mrsignal_float_byteorder", "") or "").strip()
        if bo:
            mrs_table.add_row("Float", f"[dim]{bo}[/]")
    else:
        mrs_table.add_row("Status", "[red]NOT DETECTED[/]")

    # --------------------
    # K relay panel
    # --------------------
    backend = str(getattr(hardware, "relay_backend", "") or "").strip() or "unknown"
    relay_states = []
    try:
        kmap = hardware.get_k_relays_state()
        for ch in sorted(int(k) for k in kmap.keys()):
            st = kmap.get(ch, {}) or {}
            relay_states.append((int(ch), bool(st.get("drive", False)), st.get("pin_level", None)))
    except Exception:
        try:
            drive_on = bool(hardware.get_k1_drive())
        except Exception:
            drive_on = bool(getattr(hardware.relay, "is_lit", False))

        try:
            pin_level = hardware.get_k1_pin_level()
        except Exception:
            pin_level = None
        relay_states = [(1, drive_on, pin_level)]

    k1_table = Table.grid(padding=(0, 1))
    k1_table.add_column(justify="right", style="bold yellow", no_wrap=True)
    k1_table.add_column()
    k1_table.add_row("WD", _wd_cell(watchdog, "k1"))
    k1_table.add_row("Backend", f"[dim]{backend}[/]")
    k1_table.add_row("Channels", f"[white]{len(relay_states)}[/]")
    for ch, drive_on, pin_level in relay_states:
        level_txt = "[dim]--[/]" if pin_level is None else _badge(bool(pin_level), "HIGH", "LOW")
        k1_table.add_row(f"K{int(ch)}", f"{_badge(bool(drive_on), 'ON', 'OFF')}  {level_txt}")

    # --------------------
    # CAN panel
    # --------------------
    can_table = Table.grid(padding=(0, 1))
    can_table.add_column(justify="right", style="bold cyan", no_wrap=True)
    can_table.add_column()
    can_table.add_row("WD", _wd_cell(watchdog, "can"))
    can_table.add_row("IF", f"[white]{str(getattr(config, 'CAN_INTERFACE', '') or 'socketcan')}[/]")
    can_table.add_row("Chan", f"[white]{_short_can_channel(can_channel)}[/]")
    can_table.add_row("Bitrate", f"[white]{int(can_bitrate)//1000} kbps[/]")
    can_table.add_row(
        "Load",
        f"[yellow]{bus_load_pct:.1f}%[/]" if isinstance(bus_load_pct, (int, float)) else "[dim]--[/]",
    )
    # Global status poll period (thread that polls device status snapshots)
    try:
        can_table.add_row("Poll", f"[dim]{float(status_poll_period):.2f}s[/]")
    except Exception:
        can_table.add_row("Poll", "[dim]--[/]")
    if isinstance(bus_rx_fps, (int, float)):
        can_table.add_row("RX", f"[white]{bus_rx_fps:.0f} fps[/]")
    if isinstance(bus_tx_fps, (int, float)):
        can_table.add_row("TX", f"[white]{bus_tx_fps:.0f} fps[/]")

    # Render into layout slots
    layout["eload"].update(Panel(eload_table, title="[bold]E-Load[/]", border_style="cyan", box=box.ROUNDED))
    layout["afg"].update(Panel(afg_table, title="[bold]AFG-2125[/]", border_style="green", box=box.ROUNDED))
    layout["mmeter"].update(Panel(meter_table, title="[bold]Multimeter[/]", border_style="magenta", box=box.ROUNDED))
    layout["mrsignal"].update(Panel(mrs_table, title="[bold]MrSignal[/]", border_style="white", box=box.ROUNDED))
    layout["k1"].update(Panel(k1_table, title="[bold]K Relays[/]", border_style="yellow", box=box.ROUNDED))
    layout["can"].update(Panel(can_table, title="[bold]CAN[/]", border_style="blue", box=box.ROUNDED))

    # --------------------
    # PAT switching matrix footer (PAT_J0..PAT_J5)
    # --------------------
    try:
        ps = pat_matrix if isinstance(pat_matrix, dict) else {}
    except Exception:
        ps = {}

    # Display as a 2x3 compact grid so we have enough horizontal space to
    # show pin numbers (and J0 names) without wrapping.
    j0_names = {}
    try:
        j0_names = _pat_j0_pin_names() or {}
    except Exception:
        j0_names = {}

    pat_grid = Table.grid(expand=True)
    pat_grid.add_column(ratio=1)
    pat_grid.add_column(ratio=1)
    pat_grid.add_column(ratio=1)

    # PAT matrix freshness: blank the view if we haven't seen PAT_Jx frames
    # recently so we don't display a stale "active" route forever.
    try:
        pat_timeout_s = float(getattr(config, "PAT_MATRIX_TIMEOUT_SEC", getattr(config, "CAN_TIMEOUT_SEC", 2.0)))
    except Exception:
        pat_timeout_s = 2.0

    def _cell(j: int) -> str:
        entry = ps.get(f"J{j}", {}) if isinstance(ps, dict) else {}
        vals = entry.get("vals") if isinstance(entry, dict) else None
        age = entry.get("age") if isinstance(entry, dict) else None

        # If we've never seen this J-frame, or it has gone stale, don't show
        # the last captured pin list (which can be very misleading).
        if age is None:
            return f"[bold]J{j}[/] [dim]n/a[/]"

        try:
            age_f = float(age)
        except Exception:
            age_f = None

        if (pat_timeout_s is not None) and (pat_timeout_s > 0) and (age_f is not None) and (age_f > pat_timeout_s):
            # Keep it short to avoid wrapping; matches other watchdog indicators.
            return f"[bold]J{j}[/] [dim]TO {age_f:.1f}s[/]"

        if j == 0:
            body = _pat_active_list(vals, names=j0_names, show_pin=True)
        else:
            body = _pat_active_list(vals)
        return f"[bold]J{j}[/] {body}"

    pat_grid.add_row(_cell(0), _cell(1), _cell(2))
    pat_grid.add_row(_cell(3), _cell(4), _cell(5))

    layout["pat"].update(
        Panel(
            pat_grid,
            title="[bold]PAT switching matrix[/]",
            border_style="blue",
            box=box.SQUARE,
            padding=(0, 1),
        )
    )
    return layout
