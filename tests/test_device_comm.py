from __future__ import annotations

import queue
import struct
import threading
import time


class DummyLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeSCPI:
    def __init__(self):
        self.commands: list[str] = []

    def write(self, cmd: str, *, delay_s: float = 0.0, clear_input: bool = False):
        self.commands.append(cmd)


class FakeBKHelper:
    def __init__(self, drain_script: list[list[str]]):
        self._script = list(drain_script)
        self.writes: list[str] = []

    def write(self, cmd: str, *, delay_s: float = 0.0, clear_input: bool = False):
        self.writes.append(cmd)

    def drain_errors(self, *, max_n: int = 8, log: bool = False):
        # Return next scripted response (or default to no error)
        if self._script:
            return self._script.pop(0)
        return ["0,No error"]


class FakeSerialMeter:
    def __init__(self):
        self.writes: list[bytes] = []
        self.reset_called = 0
        self.flush_called = 0

    def reset_input_buffer(self):
        self.reset_called += 1

    def write(self, b: bytes):
        self.writes.append(bytes(b))

    def flush(self):
        self.flush_called += 1


class FakeHardware:
    def __init__(self):
        # Locks
        self.mmeter_lock = DummyLock()
        self.afg_lock = DummyLock()
        self.eload_lock = DummyLock()

        # Devices
        self.mmeter = None
        self.multi_meter = None
        self.afg = None
        self.e_load = None
        self.mrsignal = True

        # cached state fields used by device_comm
        self.mmeter_scpi_style = "auto"
        self.mmeter_func = 0
        self.mmeter_autorange = True
        self.mmeter_range_value = 0.0
        self.mmeter_nplc = 1.0
        self.mmeter_func2_enabled = False
        self.mmeter_func2 = 0
        self.mmeter_trig_source = -1
        self.mmeter_rel_enabled = False

        self.mmeter_quiet_until = 0.0

        self.multi_meter_mode = -1
        self.multi_meter_range = -1

        # AFG cached state
        self.afg_output = False
        self.afg_shape = -1
        self.afg_freq = -1
        self.afg_ampl = -1
        self.afg_offset = 0
        self.afg_duty = 0

        # E-load cached state
        self.e_load_enabled = 0
        self.e_load_mode = 0
        self.e_load_short = 0
        self.e_load_csetting = 0
        self.e_load_rsetting = 0

        # outputs
        self.k1_drive: bool | None = None
        self.k_drives: dict[int, bool] = {}
        self.mrs_calls: list[tuple] = []

    def set_k1_drive(self, v: bool) -> None:
        self.k1_drive = bool(v)
        self.k_drives[1] = bool(v)

    def set_k_drive(self, channel: int, v: bool) -> None:
        ch = int(channel)
        self.k_drives[ch] = bool(v)
        if ch == 1:
            self.k1_drive = bool(v)

    def set_mrsignal(self, *, enable: bool, output_select: int, value: float, max_v: float, max_ma: float):
        self.mrs_calls.append((enable, output_select, value, max_v, max_ma))

    def apply_idle_all(self):
        self.idle_called = True


def test_mmeter_write_blank_and_missing_meter(monkeypatch):
    from roi.core import device_comm

    hw = FakeHardware()
    hw.mmeter = None
    hw.multi_meter = None

    p = device_comm.DeviceCommandProcessor(hw, log_fn=lambda s: None)
    # Blank command -> early return
    p._mmeter_write("   ")
    # No helper and no raw serial -> early return
    p._mmeter_write("CONF:VOLT:DC")


class RawSerial:
    def __init__(self, *, raise_reset=False, raise_flush=False):
        self.written: list[bytes] = []
        self.raise_reset = raise_reset
        self.raise_flush = raise_flush

    def reset_input_buffer(self):
        if self.raise_reset:
            raise RuntimeError("reset")

    def write(self, data: bytes):
        self.written.append(bytes(data))

    def flush(self):
        if self.raise_flush:
            raise RuntimeError("flush")


def test_mmeter_write_raw_serial_exceptions_delay_and_bad_settle(monkeypatch):
    from roi.core import device_comm
    import roi.config as config
    import time as _time

    hw = FakeHardware()
    hw.mmeter = None
    hw.multi_meter = RawSerial(raise_reset=True, raise_flush=True)

    slept: list[float] = []

    monkeypatch.setattr(_time, "sleep", lambda dt: slept.append(float(dt)))
    monkeypatch.setattr(config, "MMETER_CONTROL_SETTLE_SEC", "bad", raising=False)

    p = device_comm.DeviceCommandProcessor(hw, log_fn=lambda s: None)
    p._mmeter_write("CONF:VOLT:DC", delay_s=0.01, clear_input=True)
    assert slept == [0.01]
    assert hw.multi_meter.written


def test_mmeter_write_helper_exception_is_logged(monkeypatch):
    from roi.core import device_comm
    import roi.config as config

    hw = FakeHardware()

    class BoomHelper:
        def write(self, cmd, delay_s=0.0, clear_input=False):
            raise RuntimeError("boom")

    hw.mmeter = BoomHelper()
    logs: list[str] = []
    monkeypatch.setattr(config, "MMETER_DEBUG", True, raising=False)
    p = device_comm.DeviceCommandProcessor(hw, log_fn=logs.append)
    p._mmeter_write("CONF:VOLT:DC")
    assert any("MMETER write error" in m for m in logs)


def test_quantize_nplc():
    from roi.core.device_comm import _quantize_nplc

    assert _quantize_nplc("bad") == 1.0
    assert _quantize_nplc(0.09) == 0.1
    assert _quantize_nplc(1.2) == 1.0
    assert _quantize_nplc(100) == 10.0


def test_mmeter_write_uses_helper_and_sets_quiet_until(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    helper = FakeSCPI()
    hw.mmeter = helper
    logs: list[str] = []

    monkeypatch.setattr(config, "MMETER_DEBUG", True, raising=False)
    monkeypatch.setattr(config, "MMETER_CONTROL_SETTLE_SEC", 0.5, raising=False)

    t = {"x": 10.0}

    def fake_monotonic():
        return t["x"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    p = DeviceCommandProcessor(hw, log_fn=logs.append)
    p._mmeter_write(":FUNCtion VOLTage:DC", delay_s=0.0, clear_input=True)
    assert helper.commands == [":FUNCtion VOLTage:DC"]
    assert hw.mmeter_quiet_until == 10.0 + 0.5
    assert any("[mmeter] >>" in m for m in logs)


def test_mmeter_write_raw_serial_path(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    hw.mmeter = None
    hw.multi_meter = FakeSerialMeter()
    monkeypatch.setattr(config, "MMETER_DEBUG", False, raising=False)
    monkeypatch.setattr(config, "MMETER_CONTROL_SETTLE_SEC", 0.0, raising=False)

    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)
    p._mmeter_write("CONF:VOLT:DC", delay_s=0.0, clear_input=True)
    assert hw.multi_meter.reset_called == 1
    assert hw.multi_meter.flush_called == 1
    assert hw.multi_meter.writes and hw.multi_meter.writes[0].endswith(b"\n")


def test_mmeter_set_func_fallback_and_style_commit(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    # Script drain_errors to fail first candidate and succeed second.
    helper = FakeBKHelper(
        drain_script=[
            ["0,No error"],  # pre-clear for candidate #1
            ["-100,BUS"],    # post-write errors -> fail
            ["0,No error"],  # pre-clear for candidate #2
            ["0,No error"],  # post-write -> ok
        ]
    )
    hw.mmeter = helper
    hw.mmeter_scpi_style = "auto"
    monkeypatch.setattr(config, "MMETER_DEBUG", True, raising=False)

    logs: list[str] = []
    p = DeviceCommandProcessor(hw, log_fn=logs.append)
    p._mmeter_set_func(MmeterFunc.VDC)
    assert hw.mmeter_func == MmeterFunc.VDC
    # Style should have been committed to the successful one.
    assert hw.mmeter_scpi_style in ("conf", "func")
    assert any("set func" in m for m in logs)


def test_mmeter_set_func_invalid_style_and_no_helper(monkeypatch):
    """Covers style normalization + helper-absent error draining path."""
    from roi.core import device_comm
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.mmeter = None
    hw.multi_meter = None
    hw.mmeter_scpi_style = "weird"  # will normalize to 'auto'

    p = device_comm.DeviceCommandProcessor(hw, log_fn=lambda s: None)
    p._mmeter_set_func(int(MmeterFunc.VDC))
    assert hw.mmeter_func == int(MmeterFunc.VDC)
    assert hw.mmeter_scpi_style in ("conf", "func", "auto")


def test_mmeter_set_func_drain_errors_exception(monkeypatch):
    from roi.core import device_comm
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()

    class Helper:
        def drain_errors(self, max_n=1, log=False):
            raise RuntimeError("boom")

        def write(self, cmd, delay_s=0.0, clear_input=False):
            return None

    hw.mmeter = Helper()
    hw.mmeter_scpi_style = "conf"

    p = device_comm.DeviceCommandProcessor(hw, log_fn=lambda s: None)
    p._mmeter_set_func(int(MmeterFunc.VDC))
    assert hw.mmeter_func == int(MmeterFunc.VDC)


def test_mmeter_set_func_all_candidates_fail_logs(monkeypatch):
    from roi.core import device_comm
    import roi.config as config
    from roi.devices.bk5491b import MmeterFunc

    # Drain errors: no error before, BUS error after -> every candidate fails.
    class Helper:
        def __init__(self):
            self.calls = 0

        def drain_errors(self, max_n=1, log=False):
            self.calls += 1
            if self.calls % 2 == 0:
                return ["-200,BUS"]
            return ["0,No error"]

        def write(self, cmd, delay_s=0.0, clear_input=False):
            return None

    hw = FakeHardware()
    hw.mmeter = Helper()
    hw.mmeter_scpi_style = "auto"

    logs: list[str] = []
    monkeypatch.setattr(config, "MMETER_DEBUG", True, raising=False)
    p = device_comm.DeviceCommandProcessor(hw, log_fn=logs.append)
    p._mmeter_set_func(int(MmeterFunc.VDC))
    assert any("failed to set func" in m for m in logs)


def test_mmeter_set_func_skips_empty_candidate(monkeypatch):
    from roi.core import device_comm
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.mmeter = None
    hw.multi_meter = None

    # Force a whitespace-only CONF command so base becomes empty.
    monkeypatch.setitem(device_comm.FUNC_TO_SCPI_CONF, int(MmeterFunc.VDC), "   ")

    p = device_comm.DeviceCommandProcessor(hw, log_fn=lambda s: None)
    p._mmeter_set_func(int(MmeterFunc.VDC))
    assert hw.mmeter_func == int(MmeterFunc.VDC)


def test_mmeter_set_func_dedup_continue_branch(monkeypatch):
    """Cover the duplicate-elision `continue` inside candidate de-duplication.

    The production candidate generator is careful to avoid duplicates, so this
    test forces the de-dup `if k in seen: continue` branch by patching the
    module-local `set` factory used to build the `seen` container.
    """

    from roi.core import device_comm
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    helper = FakeBKHelper(drain_script=[["0,No error"], ["0,No error"]])
    hw.mmeter = helper

    class FakeSet:
        def __init__(self):
            self._s = set()
            self._calls = 0

        def __contains__(self, item):
            # First membership check reports "already seen" to trigger the
            # continue branch; subsequent checks behave normally.
            self._calls += 1
            if self._calls == 1:
                return True
            return item in self._s

        def add(self, item):
            self._s.add(item)

    monkeypatch.setattr(device_comm, "set", FakeSet, raising=False)

    p = device_comm.DeviceCommandProcessor(hw, log_fn=lambda s: None)
    p._mmeter_set_func(int(MmeterFunc.VDC))

    assert hw.mmeter_func == int(MmeterFunc.VDC)
    assert helper.writes, "expected at least one SCPI write"


def test_handle_relay_and_invert(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    # no data -> ignored
    p.handle(int(config.RLY_CTRL_ID), b"")
    assert hw.k1_drive is None

    monkeypatch.setattr(config, "K1_CAN_INVERT", True, raising=False)
    p.handle(int(config.RLY_CTRL_ID), b"\x01")
    # inverted => False
    assert hw.k1_drive is False
    assert hw.k_drives.get(1) is False
    # K2..K4 are decoded too (all zero in this payload).
    assert hw.k_drives.get(2) is False
    assert hw.k_drives.get(3) is False
    assert hw.k_drives.get(4) is False


def test_handle_relay_multichannel_decodes_k1_to_k4(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    monkeypatch.setattr(config, "K1_CAN_INVERT", False, raising=False)
    # K1=1, K2=3, K3=0, K4=1  (2-bit fields)
    p.handle(int(config.RLY_CTRL_ID), b"\x4d")

    assert hw.k_drives.get(1) is True
    assert hw.k_drives.get(2) is True
    assert hw.k_drives.get(3) is False
    assert hw.k_drives.get(4) is True


def test_handle_relay_legacy_set_k1_only(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    class LegacyHardware:
        def __init__(self):
            self.k1_drive = None

        def set_k1_drive(self, v: bool) -> None:
            self.k1_drive = bool(v)

    hw = LegacyHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    monkeypatch.setattr(config, "K1_CAN_INVERT", False, raising=False)
    p.handle(int(config.RLY_CTRL_ID), b"\x01")
    assert hw.k1_drive is True


def test_handle_afg_primary_and_ext(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    hw.afg = FakeSCPI()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    # Primary: enable=1, shape=2 (RAMP), freq=100, ampl=2000mV
    data = bytes([1, 2]) + struct.pack("<I", 100) + struct.pack("<H", 2000)
    p.handle(int(config.AFG_CTRL_ID), data)
    assert "OUTP1 ON" in hw.afg.commands[0]
    assert any("SOUR1:FUNC" in c for c in hw.afg.commands)
    assert any("SOUR1:FREQ 100" in c for c in hw.afg.commands)
    assert any("SOUR1:AMPL 2.0" in c for c in hw.afg.commands)

    # Extended: offset=-100mV, duty=250 -> clamped to 99
    ext = struct.pack("<h", -100) + bytes([250])
    p.handle(int(config.AFG_CTRL_EXT_ID), ext)
    assert any("SOUR1:DCO -0.1" in c for c in hw.afg.commands)
    assert any("SOUR1:SQU:DCYC 99" in c for c in hw.afg.commands)


def test_handle_afg_early_return_and_error_logs(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    # No AFG attached -> ignored
    p.handle(int(config.AFG_CTRL_ID), b"\x00" * 8)

    class BoomAfg(FakeSCPI):
        def write(self, cmd: str):
            raise RuntimeError("boom")

    hw2 = FakeHardware()
    hw2.afg = BoomAfg()
    logs: list[str] = []
    p2 = DeviceCommandProcessor(hw2, log_fn=logs.append)
    # Valid payload but write raises -> error logged
    data = bytes([1, 0]) + struct.pack("<I", 100) + struct.pack("<H", 1000)
    p2.handle(int(config.AFG_CTRL_ID), data)
    assert any("AFG Control Error" in m for m in logs)

    # Extended too short -> ignored
    p2.handle(int(config.AFG_CTRL_EXT_ID), b"\x00")
    # Extended with write error
    p2.handle(int(config.AFG_CTRL_EXT_ID), struct.pack("<h", 0) + bytes([50]))
    assert any("AFG Ext Error" in m for m in logs)


def test_handle_mmeter_legacy_and_range(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.multi_meter = True
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    called = []

    def fake_set_func(func):
        called.append(int(func))
        hw.mmeter_func = int(func)

    monkeypatch.setattr(p, "_mmeter_set_func", fake_set_func)

    # Mode 0 -> VDC
    p.handle(int(config.MMETER_CTRL_ID), bytes([0, 0]))
    assert called == [int(MmeterFunc.VDC)]
    assert hw.multi_meter_mode == 0

    # Enable legacy range behavior and ensure autorange ON command is sent.
    monkeypatch.setattr(config, "MMETER_LEGACY_RANGE_ENABLE", True, raising=False)
    writes: list[str] = []
    monkeypatch.setattr(p, "_mmeter_write", lambda cmd, **kw: writes.append(cmd))

    # range=0 => autorange on
    p.handle(int(config.MMETER_CTRL_ID), bytes([0, 0]))
    assert any(":RANGe:AUTO ON" in w for w in writes)

    # range!=0 => autorange off
    p.handle(int(config.MMETER_CTRL_ID), bytes([0, 1]))
    assert any(":RANGe:AUTO OFF" in w for w in writes)


def test_handle_mmeter_ctrl_short_data_and_idc(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.multi_meter = True
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    called: list[int] = []

    def fake_set_func(func):
        called.append(int(func))
        hw.mmeter_func = int(func)

    monkeypatch.setattr(p, "_mmeter_set_func", fake_set_func)

    # Short payload ignored
    p.handle(int(config.MMETER_CTRL_ID), b"\x00")
    assert called == []

    # Mode 1 -> IDC
    p.handle(int(config.MMETER_CTRL_ID), bytes([1, 0]))
    assert called == [int(MmeterFunc.IDC)]


def test_handle_mmeter_ctrl_mode0_disabled(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    hw.multi_meter = True
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    called: list[int] = []
    monkeypatch.setattr(p, "_mmeter_set_func", lambda f: called.append(int(f)))
    monkeypatch.setattr(config, "MMETER_LEGACY_MODE0_ENABLE", False, raising=False)
    monkeypatch.setattr(config, "MMETER_LEGACY_MODE1_ENABLE", True, raising=False)

    p.handle(int(config.MMETER_CTRL_ID), bytes([0, 0]))
    assert called == []
    assert hw.multi_meter_mode == 0


def test_handle_mmeter_ctrl_lock_exception_is_swallowed(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    class BadLock:
        def __enter__(self):
            raise RuntimeError("lock")

        def __exit__(self, exc_type, exc, tb):
            return False

    hw = FakeHardware()
    hw.multi_meter = True
    hw.mmeter_lock = BadLock()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    # Should not raise even though lock acquisition fails.
    p.handle(int(config.MMETER_CTRL_ID), bytes([0, 0]))

    # Also swallow errors in legacy-range block
    monkeypatch.setattr(config, "MMETER_LEGACY_RANGE_ENABLE", True, raising=False)
    p.handle(int(config.MMETER_CTRL_ID), bytes([0, 1]))


def test_handle_mmeter_ctrl_len_short_and_idc_and_exception_swallow(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.multi_meter = True
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    calls: list[int] = []
    monkeypatch.setattr(p, "_mmeter_set_func", lambda f: calls.append(int(f)))

    # Too short -> ignored
    p.handle(int(config.MMETER_CTRL_ID), b"\x00")
    assert calls == []

    # Mode 1 -> IDC
    p.handle(int(config.MMETER_CTRL_ID), bytes([1, 0]))
    assert int(MmeterFunc.IDC) in calls

    # Lock errors are swallowed
    class BadLock:
        def __enter__(self):
            raise RuntimeError("lock")

        def __exit__(self, exc_type, exc, tb):
            return False

    hw2 = FakeHardware()
    hw2.multi_meter = True
    hw2.mmeter_lock = BadLock()
    p2 = DeviceCommandProcessor(hw2, log_fn=lambda s: None)
    monkeypatch.setattr(config, "MMETER_LEGACY_RANGE_ENABLE", True, raising=False)
    # Should not raise
    p2.handle(int(config.MMETER_CTRL_ID), bytes([0, 1]))


def test_handle_mmeter_ext_opcodes(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.multi_meter = True
    hw.mmeter_func = int(MmeterFunc.VDC)
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    writes: list[str] = []
    monkeypatch.setattr(p, "_mmeter_write", lambda cmd, **kw: writes.append(cmd))
    monkeypatch.setattr(p, "_mmeter_set_func", lambda f: setattr(hw, "mmeter_func", int(f)))

    # SET_AUTORANGE uses arg1
    payload = bytes([0x02, 0xFF, 0x00, 0]) + struct.pack("<f", 0.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert hw.mmeter_autorange is False

    # SET_RANGE with finite float
    payload = bytes([0x03, 0xFF, 0, 0]) + struct.pack("<f", 12.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert any(":RANGe 12" in w for w in writes)

    # SET_NPLC quantizes
    payload = bytes([0x04, 0xFF, 0, 0]) + struct.pack("<f", 9.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert any(":NPLCycles" in w for w in writes)

    # SECONDARY_ENABLE and SECONDARY_FUNCTION
    payload = bytes([0x05, 1, 0, 0]) + struct.pack("<f", 0.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert hw.mmeter_func2_enabled is True
    payload = bytes([0x06, int(MmeterFunc.VAC), 0, 0]) + struct.pack("<f", 0.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert hw.mmeter_func2 == int(MmeterFunc.VAC)

    # TRIG_SOURCE, BUS_TRIGGER
    payload = bytes([0x07, 1, 0, 0]) + struct.pack("<f", 0.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert hw.mmeter_trig_source == 1
    payload = bytes([0x08, 0, 0, 0]) + struct.pack("<f", 0.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert "*TRG" in writes

    # RELATIVE_ENABLE uses arg0
    payload = bytes([0x09, 1, 0, 0]) + struct.pack("<f", 0.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert hw.mmeter_rel_enabled is True

    # RELATIVE_ACQUIRE
    payload = bytes([0x0A, 0xFF, 0, 0]) + struct.pack("<f", 0.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert any(":REFerence:ACQuire" in w for w in writes)

    # Unknown op logs (op!=0)
    logs: list[str] = []
    p2 = DeviceCommandProcessor(hw, log_fn=logs.append)
    monkeypatch.setattr(p2, "_mmeter_write", lambda cmd, **kw: None)
    p2.handle(int(config.MMETER_CTRL_EXT_ID), bytes([0xAA, 1, 2, 3, 0, 0, 0, 0]))
    assert any("unknown op" in m for m in logs)


def test_handle_mmeter_ext_early_return_and_more_branches(monkeypatch):
    import roi.config as config
    from roi.core import device_comm
    from roi.devices.bk5491b import MmeterFunc

    # Early return without a meter
    hw0 = FakeHardware()
    hw0.multi_meter = False
    p0 = device_comm.DeviceCommandProcessor(hw0, log_fn=lambda s: None)
    p0.handle(int(config.MMETER_CTRL_EXT_ID), b"")

    # Now exercise additional opcodes/branches.
    hw = FakeHardware()
    hw.multi_meter = True
    hw.mmeter_func = int(MmeterFunc.VDC)
    p = device_comm.DeviceCommandProcessor(hw, log_fn=lambda s: None)

    writes: list[str] = []
    monkeypatch.setattr(p, "_mmeter_write", lambda cmd, **kw: writes.append(cmd))

    # SET_FUNC logs and updates func
    logs: list[str] = []
    p_log = device_comm.DeviceCommandProcessor(hw, log_fn=logs.append)
    monkeypatch.setattr(p_log, "_mmeter_write", lambda cmd, **kw: None)
    monkeypatch.setattr(p_log, "_mmeter_set_func", lambda f: setattr(hw, "mmeter_func", int(f)))
    payload = bytes([0x01, int(MmeterFunc.IDC), 0, 0]) + struct.pack("<f", 0.0)
    p_log.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert any("MMETER func" in m for m in logs)

    # SET_RANGE with NaN should be ignored
    payload = bytes([0x03, 0xFF, 0, 0]) + struct.pack("<f", float("nan"))
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)

    # Redundant range write is suppressed (pass branch)
    hw.mmeter_autorange = False
    hw.mmeter_range_value = 12.0
    before = list(writes)
    payload = bytes([0x03, 0xFF, 0, 0]) + struct.pack("<f", 12.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert writes == before

    # Secondary enable with unsupported function logs
    hw.mmeter_func2 = 255
    logs2: list[str] = []
    p2 = device_comm.DeviceCommandProcessor(hw, log_fn=logs2.append)
    monkeypatch.setattr(p2, "_mmeter_write", lambda cmd, **kw: None)
    payload = bytes([0x05, 1, 0, 0]) + struct.pack("<f", 0.0)
    p2.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert any("unsupported" in m.lower() for m in logs2)

    # Secondary function with unsupported code logs and returns
    logs3: list[str] = []
    p3 = device_comm.DeviceCommandProcessor(hw, log_fn=logs3.append)
    monkeypatch.setattr(p3, "_mmeter_write", lambda cmd, **kw: None)
    # Note: 0xFF means "use current func"; use an out-of-range code to trigger
    # the "unsupported" branch.
    payload = bytes([0x06, 254, 0, 0]) + struct.pack("<f", 0.0)
    p3.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert any("unsupported" in m.lower() for m in logs3)

    # Secondary function enables display when currently disabled
    hw4 = FakeHardware()
    hw4.multi_meter = True
    hw4.mmeter_func = int(MmeterFunc.VDC)
    hw4.mmeter_func2_enabled = False
    p4 = device_comm.DeviceCommandProcessor(hw4, log_fn=lambda s: None)
    writes4: list[str] = []
    monkeypatch.setattr(p4, "_mmeter_write", lambda cmd, **kw: writes4.append(cmd))
    payload = bytes([0x06, int(MmeterFunc.VAC), 0, 0]) + struct.pack("<f", 0.0)
    p4.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert any(":FUNCtion2:STATe 1" in w for w in writes4)


def test_mmeter_ext_control_error_is_logged(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.multi_meter = True
    hw.mmeter_func = int(MmeterFunc.VDC)
    logs: list[str] = []
    p = DeviceCommandProcessor(hw, log_fn=logs.append)
    monkeypatch.setattr(p, "_mmeter_set_func", lambda f: (_ for _ in ()).throw(RuntimeError("boom")))
    payload = bytes([0x01, int(MmeterFunc.IDC), 0, 0]) + struct.pack("<f", 0.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert any("MMETER ext control error" in m for m in logs)


def test_handle_mmeter_ext_more_branches(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.multi_meter = True
    hw.mmeter_func = int(MmeterFunc.VDC)
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    writes: list[str] = []
    monkeypatch.setattr(p, "_mmeter_write", lambda cmd, **kw: writes.append(cmd))

    # FUNC opcode (0x01) should log a friendly label when set_func updates state.
    logs: list[str] = []
    p2 = DeviceCommandProcessor(hw, log_fn=logs.append)
    monkeypatch.setattr(p2, "_mmeter_write", lambda cmd, **kw: None)

    def fake_set_func(func):
        hw.mmeter_func = int(func)

    monkeypatch.setattr(p2, "_mmeter_set_func", fake_set_func)
    payload = bytes([0x01, int(MmeterFunc.IDC), 0, 0]) + struct.pack("<f", 0.0)
    p2.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert any("MMETER func ->" in m for m in logs)

    # SET_RANGE with NaN -> ignored
    payload_nan = bytes([0x03, 0xFF, 0, 0]) + struct.pack("<f", float("nan"))
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload_nan)

    # SET_RANGE redundant write -> pass branch
    hw.mmeter_autorange = False
    hw.mmeter_range_value = 12.0
    before = list(writes)
    payload_same = bytes([0x03, 0xFF, 0, 0]) + struct.pack("<f", 12.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload_same)
    assert writes == before

    # SECONDARY_ENABLE with unsupported secondary func logs
    logs2: list[str] = []
    hw2 = FakeHardware()
    hw2.multi_meter = True
    hw2.mmeter_func2 = 255
    p3 = DeviceCommandProcessor(hw2, log_fn=logs2.append)
    monkeypatch.setattr(p3, "_mmeter_write", lambda cmd, **kw: None)
    payload_en = bytes([0x05, 1, 0, 0]) + struct.pack("<f", 0.0)
    p3.handle(int(config.MMETER_CTRL_EXT_ID), payload_en)
    assert any("unsupported" in m.lower() for m in logs2)

    # SECONDARY_FUNCTION unsupported func logs
    logs3: list[str] = []
    p4 = DeviceCommandProcessor(hw2, log_fn=logs3.append)
    monkeypatch.setattr(p4, "_mmeter_write", lambda cmd, **kw: None)
    payload_bad = bytes([0x06, 254, 0, 0]) + struct.pack("<f", 0.0)
    p4.handle(int(config.MMETER_CTRL_EXT_ID), payload_bad)
    assert any("unsupported" in m.lower() for m in logs3)

    # SECONDARY_FUNCTION enables secondary display when disabled.
    hw3 = FakeHardware()
    hw3.multi_meter = True
    hw3.mmeter_func2_enabled = False
    p5 = DeviceCommandProcessor(hw3, log_fn=lambda s: None)
    writes2: list[str] = []
    monkeypatch.setattr(p5, "_mmeter_write", lambda cmd, **kw: writes2.append(cmd))
    payload_ok = bytes([0x06, int(MmeterFunc.VAC), 0, 0]) + struct.pack("<f", 0.0)
    p5.handle(int(config.MMETER_CTRL_EXT_ID), payload_ok)
    assert any(":FUNCtion2:STATe 1" in w for w in writes2)

    # Exception during EXT handling logs and is swallowed.
    logs4: list[str] = []
    p6 = DeviceCommandProcessor(hw3, log_fn=logs4.append)
    monkeypatch.setattr(p6, "_mmeter_set_func", lambda f: (_ for _ in ()).throw(RuntimeError("boom")))
    p6.handle(int(config.MMETER_CTRL_EXT_ID), bytes([0x01, int(MmeterFunc.VDC), 0, 0]) + struct.pack("<f", 0.0))
    assert any("MMETER ext control error" in m for m in logs4)


def test_handle_mmeter_ext_early_return(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    hw.multi_meter = None
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)
    p.handle(int(config.MMETER_CTRL_EXT_ID), b"")


def test_handle_mmeter_ext_disabled_by_config(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    hw.multi_meter = True
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    monkeypatch.setattr(config, "MMETER_EXT_CTRL_ENABLE", False, raising=False)

    # If MMETER_EXT processing is disabled, no command should be written.
    writes: list[str] = []
    monkeypatch.setattr(p, "_mmeter_write", lambda cmd, **kw: writes.append(cmd))

    payload = bytes([0x08, 0, 0, 0]) + struct.pack("<f", 0.0)  # BUS_TRIGGER
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)

    assert writes == []


def test_handle_mmeter_ext_set_range_disabled(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.multi_meter = True
    hw.mmeter_func = int(MmeterFunc.VDC)
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    writes: list[str] = []
    monkeypatch.setattr(p, "_mmeter_write", lambda cmd, **kw: writes.append(cmd))
    monkeypatch.setattr(config, "MMETER_EXT_SET_RANGE_ENABLE", False, raising=False)

    payload = bytes([0x03, 0xFF, 0, 0]) + struct.pack("<f", 12.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)
    assert writes == []


def test_handle_mmeter_ext_secondary_disabled(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.multi_meter = True
    hw.mmeter_func = int(MmeterFunc.VDC)
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    writes: list[str] = []
    monkeypatch.setattr(p, "_mmeter_write", lambda cmd, **kw: writes.append(cmd))
    monkeypatch.setattr(config, "MMETER_EXT_SECONDARY_ENABLE", False, raising=False)

    payload_en = bytes([0x05, 1, 0, 0]) + struct.pack("<f", 0.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload_en)
    payload_fn = bytes([0x06, int(MmeterFunc.VAC), 0, 0]) + struct.pack("<f", 0.0)
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload_fn)
    assert writes == []


def test_handle_mmeter_ext_additional_branches(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)
    # Not a multimeter -> ignored
    p.handle(int(config.MMETER_CTRL_EXT_ID), b"\x01")

    hw2 = FakeHardware()
    hw2.multi_meter = True
    hw2.mmeter_func = int(MmeterFunc.VDC)
    p2 = DeviceCommandProcessor(hw2, log_fn=lambda s: None)

    writes: list[str] = []
    monkeypatch.setattr(p2, "_mmeter_write", lambda cmd, **kw: writes.append(cmd))

    # SET_FUNCTION logs
    logs: list[str] = []
    p3 = DeviceCommandProcessor(hw2, log_fn=logs.append)
    monkeypatch.setattr(p3, "_mmeter_set_func", lambda f: setattr(hw2, "mmeter_func", int(f)))
    p3.handle(int(config.MMETER_CTRL_EXT_ID), bytes([0x01, int(MmeterFunc.IDC), 0, 0]) + struct.pack("<f", 0.0))
    assert any("MMETER func" in m for m in logs)

    # SET_RANGE with NaN -> returns early
    p2.handle(int(config.MMETER_CTRL_EXT_ID), bytes([0x03, 0xFF, 0, 0]) + struct.pack("<f", float("nan")))

    # Redundant range write suppression
    hw2.mmeter_autorange = False
    hw2.mmeter_range_value = 12.0
    before = list(writes)
    p2.handle(int(config.MMETER_CTRL_EXT_ID), bytes([0x03, 0xFF, 0, 0]) + struct.pack("<f", 12.0))
    assert writes == before

    # Secondary enable with unsupported secondary func logs
    hw2.mmeter_func2 = 255
    logs2: list[str] = []
    p4 = DeviceCommandProcessor(hw2, log_fn=logs2.append)
    monkeypatch.setattr(p4, "_mmeter_write", lambda cmd, **kw: writes.append(cmd))
    p4.handle(int(config.MMETER_CTRL_EXT_ID), bytes([0x05, 1, 0, 0]) + struct.pack("<f", 0.0))
    assert any("unsupported" in m for m in logs2)

    # Secondary function unsupported -> logs and returns
    logs3: list[str] = []
    p5 = DeviceCommandProcessor(hw2, log_fn=logs3.append)
    monkeypatch.setattr(p5, "_mmeter_write", lambda cmd, **kw: writes.append(cmd))
    p5.handle(int(config.MMETER_CTRL_EXT_ID), bytes([0x06, 254, 0, 0]) + struct.pack("<f", 0.0))
    assert any("unsupported" in m for m in logs3)

    # Secondary function when display disabled -> forces enable
    hw3 = FakeHardware()
    hw3.multi_meter = True
    hw3.mmeter_func = int(MmeterFunc.VDC)
    hw3.mmeter_func2_enabled = False
    p6 = DeviceCommandProcessor(hw3, log_fn=lambda s: None)
    wrote: list[str] = []
    monkeypatch.setattr(p6, "_mmeter_write", lambda cmd, **kw: wrote.append(cmd))
    p6.handle(int(config.MMETER_CTRL_EXT_ID), bytes([0x06, int(MmeterFunc.VAC), 0, 0]) + struct.pack("<f", 0.0))
    assert any(":FUNCtion2:STATe 1" == c for c in wrote)

    # Exception inside handler is logged
    logs4: list[str] = []
    p7 = DeviceCommandProcessor(hw3, log_fn=logs4.append)
    monkeypatch.setattr(p7, "_mmeter_set_func", lambda f: (_ for _ in ()).throw(RuntimeError("boom")))
    p7.handle(int(config.MMETER_CTRL_EXT_ID), bytes([0x01, int(MmeterFunc.VDC), 0, 0]) + struct.pack("<f", 0.0))
    assert any("MMETER ext control error" in m for m in logs4)


def test_handle_eload_builds_write_list():
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    hw.e_load = FakeSCPI()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    # enable=1 (0x04), mode=RES (0x10), short=ON (0x40)
    first = 0x04 | 0x10 | 0x40
    # val_c=1000, val_r=2000
    data = bytes([first, 0, 0xE8, 0x03, 0xD0, 0x07])
    p.handle(int(config.LOAD_CTRL_ID), data)
    assert any("FUNC RES" == c for c in hw.e_load.commands)
    assert any("INP:SHOR ON" == c for c in hw.e_load.commands)
    assert any(c.startswith("RES ") for c in hw.e_load.commands)
    assert hw.e_load.commands[-1] == "INP ON"

    # Disabling writes INP OFF first
    first2 = 0x00
    data2 = bytes([first2, 0, 0, 0, 0, 0])
    p.handle(int(config.LOAD_CTRL_ID), data2)
    assert "INP OFF" in hw.e_load.commands


def test_handle_eload_early_return_and_exception_swallowed():
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    # No e-load -> ignored
    p.handle(int(config.LOAD_CTRL_ID), b"\x00" * 6)

    class BoomLoad(FakeSCPI):
        def write(self, cmd: str):
            raise RuntimeError("boom")

    hw2 = FakeHardware()
    hw2.e_load = BoomLoad()
    p2 = DeviceCommandProcessor(hw2, log_fn=lambda s: None)
    # Should swallow write exceptions
    data = bytes([0x04, 0, 0, 0, 0, 0])
    p2.handle(int(config.LOAD_CTRL_ID), data)


def test_handle_eload_early_return_and_exception_swallow():
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    # No load attached -> ignored
    p.handle(int(config.LOAD_CTRL_ID), b"\x00" * 6)

    class BoomLoad(FakeSCPI):
        def write(self, cmd: str):
            raise RuntimeError("boom")

    hw2 = FakeHardware()
    hw2.e_load = BoomLoad()
    p2 = DeviceCommandProcessor(hw2, log_fn=lambda s: None)
    # Should swallow write exceptions.
    data = bytes([0x04, 0, 0, 0, 0, 0])
    p2.handle(int(config.LOAD_CTRL_ID), data)


def test_handle_mrsignal_valid_and_invalid(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    # invalid mode is ignored
    bad = bytes([1, 99]) + struct.pack("<f", 1.0)
    p.handle(int(config.MRSIGNAL_CTRL_ID), bad)
    assert hw.mrs_calls == []

    # valid
    ok = bytes([1, 1]) + struct.pack("<f", 2.0)
    p.handle(int(config.MRSIGNAL_CTRL_ID), ok)
    assert hw.mrs_calls and hw.mrs_calls[-1][0] is True

    # exception in set_mrsignal is logged
    logs: list[str] = []

    def boom(**kwargs):
        raise RuntimeError("x")

    hw2 = FakeHardware()
    hw2.set_mrsignal = boom  # type: ignore
    p2 = DeviceCommandProcessor(hw2, log_fn=logs.append)
    p2.handle(int(config.MRSIGNAL_CTRL_ID), ok)
    assert any("MrSignal Control Error" in m for m in logs)


def test_handle_mrsignal_early_returns(monkeypatch):
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    # Too short
    p.handle(int(config.MRSIGNAL_CTRL_ID), b"\x00")

    # No mrsignal attribute on hardware => ignored
    delattr(hw, "mrsignal")
    p.handle(int(config.MRSIGNAL_CTRL_ID), bytes([1, 1]) + struct.pack("<f", 1.0))


def test_handle_mrsignal_early_returns():
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)
    # Too short
    p.handle(int(config.MRSIGNAL_CTRL_ID), b"\x00")
    # No mrsignal attached
    hw2 = FakeHardware()
    hw2.mrsignal = None
    p2 = DeviceCommandProcessor(hw2, log_fn=lambda s: None)
    p2.handle(int(config.MRSIGNAL_CTRL_ID), bytes([1, 1]) + struct.pack("<f", 1.0))


def test_device_command_loop_coalesces_and_calls_idle(monkeypatch):
    import roi.config as config
    from roi.core import device_comm

    q: queue.Queue[tuple[int, bytes]] = queue.Queue()
    hw = FakeHardware()
    stop = threading.Event()

    handled: list[int] = []

    def fake_handle(self, arb: int, data: bytes):
        handled.append(int(arb))

    monkeypatch.setattr(device_comm.DeviceCommandProcessor, "handle", fake_handle)

    marks: list[str] = []

    def mark(name: str):
        marks.append(name)

    # Burst of relay commands should coalesce to last.
    q.put((int(config.RLY_CTRL_ID), b"\x01"))
    q.put((int(config.RLY_CTRL_ID), b"\x00"))
    q.put((int(config.AFG_CTRL_ID), b"\x00" * 8))
    # Stop once we've given the thread time to drain.
    def stopper():
        time.sleep(0.05)
        stop.set()

    t = threading.Thread(target=stopper)
    t.start()

    device_comm.device_command_loop(q, hw, stop, log_fn=lambda s: None, watchdog_mark_fn=mark, idle_on_stop=True)
    t.join()

    assert int(config.RLY_CTRL_ID) in handled
    assert "k1" in marks
    assert getattr(hw, "idle_called", False) is True


def test_device_command_loop_more_branches(monkeypatch):
    import roi.config as config
    from roi.core import device_comm

    logs: list[str] = []
    marks: list[str] = []

    def mark(name: str):
        marks.append(name)

    # Processor handle raises to exercise error logging.
    def boom_handle(self, arb: int, data: bytes):
        raise RuntimeError("boom")

    monkeypatch.setattr(device_comm.DeviceCommandProcessor, "handle", boom_handle)

    # Custom queue that yields one command, then get_nowait raises.
    stop = threading.Event()

    class Q:
        def __init__(self):
            self._got = False

        def get(self, timeout=0.5):
            if self._got:
                stop.set()
                raise queue.Empty()
            self._got = True
            return (0x123, b"\x00")  # non-coalesced id -> hits else branch

        def get_nowait(self):
            stop.set()
            raise RuntimeError("nope")

    hw = FakeHardware()
    # Make idle handler raise to cover swallow.
    def idle_boom():
        raise RuntimeError("idle")

    hw.apply_idle_all = idle_boom  # type: ignore
    device_comm.device_command_loop(Q(), hw, stop, log_fn=logs.append, watchdog_mark_fn=mark, idle_on_stop=True)
    assert any("Device command error" in m for m in logs)


def test_device_command_loop_queue_get_exceptions(monkeypatch):
    from roi.core import device_comm

    stop = threading.Event()

    class QEmpty:
        def get(self, timeout=0.5):
            stop.set()
            raise queue.Empty()

    hw = FakeHardware()
    device_comm.device_command_loop(QEmpty(), hw, stop, log_fn=lambda s: None, idle_on_stop=False)

    class QBoom:
        def get(self, timeout=0.5):
            stop.set()
            raise RuntimeError("boom")

    device_comm.device_command_loop(QBoom(), hw, stop, log_fn=lambda s: None, idle_on_stop=False)


def test_device_command_loop_queue_exceptions_and_watchdog_marks(monkeypatch):
    import roi.config as config
    from roi.core import device_comm

    # Custom queue that raises queue.Empty once then stops.
    stop = threading.Event()

    class EmptyQ:
        def get(self, timeout=0.5):
            stop.set()
            raise queue.Empty()

    hw = FakeHardware()
    device_comm.device_command_loop(EmptyQ(), hw, stop, log_fn=lambda s: None, idle_on_stop=True)
    assert getattr(hw, "idle_called", False) is True

    # Custom queue that raises a generic exception on get.
    stop2 = threading.Event()

    class BoomGetQ:
        def get(self, timeout=0.5):
            stop2.set()
            raise RuntimeError("boom")

    hw2 = FakeHardware()
    device_comm.device_command_loop(BoomGetQ(), hw2, stop2, log_fn=lambda s: None, idle_on_stop=True)

    # Queue that yields one command then get_nowait raises.
    stop3 = threading.Event()
    items = [(int(config.LOAD_CTRL_ID), b"\x00" * 6)]

    class OneThenBoomNowait:
        def get(self, timeout=0.5):
            return items.pop(0)

        def get_nowait(self):
            stop3.set()
            raise RuntimeError("boom")

    handled: list[int] = []

    def fake_handle(self, arb: int, data: bytes):
        handled.append(int(arb))
        raise RuntimeError("device")

    monkeypatch.setattr(device_comm.DeviceCommandProcessor, "handle", fake_handle)
    marks: list[str] = []
    logs: list[str] = []

    def mark(name: str):
        marks.append(name)

    device_comm.device_command_loop(OneThenBoomNowait(), FakeHardware(), stop3, log_fn=logs.append, watchdog_mark_fn=mark, idle_on_stop=False)
    assert "eload" in marks
    assert any("Device command error" in m for m in logs)


def test_device_command_loop_other_id_and_idle_exception(monkeypatch):
    from roi.core import device_comm

    stop = threading.Event()

    class Q:
        def __init__(self):
            self.first = True

        def get(self, timeout=0.5):
            if self.first:
                self.first = False
                return (0x123, b"\x00")
            stop.set()
            raise queue.Empty()

        def get_nowait(self):
            raise queue.Empty()

    hw = FakeHardware()

    def boom_idle():
        raise RuntimeError("idle")

    hw.apply_idle_all = boom_idle  # type: ignore

    called: list[int] = []

    def fake_handle(self, arb: int, data: bytes):
        called.append(int(arb))

    monkeypatch.setattr(device_comm.DeviceCommandProcessor, "handle", fake_handle)
    # Should apply the unknown id in the "other IDs" pass and swallow idle exceptions.
    device_comm.device_command_loop(Q(), hw, stop, log_fn=lambda s: None, idle_on_stop=True)
    assert 0x123 in called


def test_mmeter_set_func_unsupported_function_logs():
    """Cover the early-return branch for unsupported MmeterFunc values."""
    from roi.core.device_comm import DeviceCommandProcessor

    hw = FakeHardware()
    logs: list[str] = []
    p = DeviceCommandProcessor(hw, log_fn=logs.append)

    # 0xEE is outside our known mapping tables.
    p._mmeter_set_func(0xEE)
    assert hw.mmeter_func == 0  # unchanged
    assert any("unsupported function" in m.lower() for m in logs)


def test_mmeter_set_func_style_func_builds_candidates(monkeypatch):
    """Cover the 'func' style candidate-building path (different from auto/conf)."""
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.mmeter_scpi_style = "func"
    hw.mmeter = FakeBKHelper(drain_script=[["0,No error"], ["0,No error"]])

    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)
    p._mmeter_set_func(int(MmeterFunc.VDC))

    assert hw.mmeter_func == int(MmeterFunc.VDC)
    # First candidate in 'func' style is the canonical mapped command.
    assert hw.mmeter.writes and hw.mmeter.writes[0] == ":FUNCtion VOLT:DC"


def test_mmeter_set_func_style_func_uses_mapped_idc_command_first():
    """FUNC-style candidate order should try the mapped IDC command first."""
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    hw = FakeHardware()
    hw.mmeter_scpi_style = "func"
    hw.mmeter = FakeBKHelper(drain_script=[["0,No error"], ["0,No error"]])

    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)
    p._mmeter_set_func(int(MmeterFunc.IDC))

    assert hw.mmeter.writes
    assert hw.mmeter.writes[0] == ":FUNCtion CURR:DC"


def test_handle_mmeter_ext_bad_float_unpack_is_swallowed(monkeypatch):
    """Cover the struct.unpack() exception path in MMETER_CTRL_EXT handling."""
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor
    from roi.devices.bk5491b import MmeterFunc

    class BadBytes(bytes):
        def __getitem__(self, key):
            # Make data[4:8] the wrong length to trigger struct.error.
            if isinstance(key, slice) and key.start == 4 and key.stop == 8:
                return b""
            return super().__getitem__(key)

    hw = FakeHardware()
    hw.multi_meter = True
    hw.mmeter_func = int(MmeterFunc.VDC)
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    payload = BadBytes(bytes([0x03, 0xFF, 0x00, 0x00, 1, 2, 3, 4]))
    p.handle(int(config.MMETER_CTRL_EXT_ID), payload)

    # It should still have processed the opcode and forced autorange off.
    assert hw.mmeter_autorange is False


def test_handle_mrsignal_unpack_error_returns_early():
    """Cover the float-unpack failure branch in MrSignal CAN control."""
    import roi.config as config
    from roi.core.device_comm import DeviceCommandProcessor

    class BadBytes(bytes):
        def __getitem__(self, key):
            if isinstance(key, slice) and key.start == 2 and key.stop == 6:
                return b""  # wrong length for struct.unpack
            return super().__getitem__(key)

    hw = FakeHardware()
    p = DeviceCommandProcessor(hw, log_fn=lambda s: None)

    # enable=1, output_select=1 (V), but float bytes will fail to unpack.
    payload = BadBytes(bytes([1, 1, 0, 0, 0, 0]))
    p.handle(int(config.MRSIGNAL_CTRL_ID), payload)

    assert hw.mrs_calls == []


def test_device_command_loop_watchdog_marks_mmeter_and_mrsignal(monkeypatch):
    import roi.config as config
    from roi.core import device_comm

    q: queue.Queue[tuple[int, bytes]] = queue.Queue()
    hw = FakeHardware()
    stop = threading.Event()

    handled: list[int] = []

    def fake_handle(self, arb: int, data: bytes):
        handled.append(int(arb))
        # Stop after we see the MrSignal frame.
        if int(arb) == int(config.MRSIGNAL_CTRL_ID):
            stop.set()

    monkeypatch.setattr(device_comm.DeviceCommandProcessor, "handle", fake_handle)

    marks: list[str] = []

    def mark(name: str):
        marks.append(name)

    q.put((int(config.MMETER_CTRL_ID), b"\x00\x00"))
    q.put((int(config.MRSIGNAL_CTRL_ID), bytes([1, 1]) + struct.pack("<f", 1.0)))

    device_comm.device_command_loop(q, hw, stop, log_fn=lambda s: None, watchdog_mark_fn=mark, idle_on_stop=False)

    assert "mmeter" in marks
    assert "mrsignal" in marks
    assert int(config.MMETER_CTRL_ID) in handled
    assert int(config.MRSIGNAL_CTRL_ID) in handled
