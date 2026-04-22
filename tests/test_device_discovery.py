from __future__ import annotations


def test_pick_by_id_honors_name_and_realpath_exclusions():
    from roi.core import device_discovery as dd

    entries = [
        (
            "usb-STMicroelectronics_LANYI_Mr.Signal_COM_Port_Demo_1.000-if00",
            "/dev/serial/by-id/mrs",
            "/dev/ttyACM1",
        ),
        (
            "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0",
            "/dev/serial/by-id/k1",
            "/dev/ttyUSB3",
        ),
    ]

    # "micro" would otherwise match MrSignal's STMicro by-id name.
    got = dd._pick_by_id(
        entries,
        ["micro", "cp2102"],
        exclude_realpaths=["/dev/ttyACM1"],
        exclude_name_hints=["mr.signal", "lanyi", "mrsignal"],
    )

    assert got == "/dev/serial/by-id/k1"


def test_autodetect_k1_avoids_mrsignal_port(monkeypatch):
    from roi.core import device_discovery as dd

    byid_entries = [
        (
            "usb-2184_AFG-2125_GES840464-if00",
            "/dev/serial/by-id/usb-2184_AFG-2125_GES840464-if00",
            "/dev/ttyACM0",
        ),
        (
            "usb-RM_Michaelides_RM_CANview-USB-if00-port0",
            "/dev/serial/by-id/usb-RM_Michaelides_RM_CANview-USB-if00-port0",
            "/dev/ttyUSB2",
        ),
        (
            "usb-STMicroelectronics_LANYI_Mr.Signal_COM_Port_Demo_1.000-if00",
            "/dev/serial/by-id/usb-STMicroelectronics_LANYI_Mr.Signal_COM_Port_Demo_1.000-if00",
            "/dev/ttyACM1",
        ),
        (
            "usb-Silicon_Labs_5491B_Multimeter_0001-if00-port0",
            "/dev/serial/by-id/usb-Silicon_Labs_5491B_Multimeter_0001-if00-port0",
            "/dev/ttyUSB0",
        ),
        (
            "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0",
            "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0",
            "/dev/ttyUSB1",
        ),
    ]

    # Enable autodetect paths used in this test.
    monkeypatch.setattr(dd.config, "AUTO_DETECT_ENABLE", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_VERBOSE", False, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_PREFER_BY_ID", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_BYID_ONLY", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_CANVIEW", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_MMETER", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_MRSIGNAL", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_K1_SERIAL", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_VISA", False, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_AFG", False, raising=False)
    monkeypatch.setattr(dd.config, "K1_ENABLE", True, raising=False)
    monkeypatch.setattr(dd.config, "MRSIGNAL_ENABLE", True, raising=False)

    monkeypatch.setattr(dd.config, "CAN_INTERFACE", "socketcan", raising=False)
    monkeypatch.setattr(dd.config, "CAN_CHANNEL", "can0", raising=False)
    monkeypatch.setattr(dd.config, "MULTI_METER_PATH", "", raising=False)
    monkeypatch.setattr(dd.config, "MRSIGNAL_PORT", "", raising=False)
    monkeypatch.setattr(dd.config, "K1_SERIAL_PORT", "", raising=False)
    monkeypatch.setattr(dd.config, "MULTI_METER_BAUD", 38400, raising=False)

    monkeypatch.setattr(dd.config, "AUTO_DETECT_CANVIEW_BYID_HINTS", "canview", raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_MMETER_IDN_HINTS", "multimeter,5491b", raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_MMETER_BYID_HINTS", "multimeter,5491b", raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_MRSIGNAL_BYID_HINTS", "mr.signal,lanyi,mrsignal", raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_K1_BYID_HINTS", "micro,cp2102,relay", raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_K1_BYID_EXCLUDE_HINTS", "mr.signal,lanyi,mrsignal", raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_AFG_BYID_HINTS", "afg", raising=False)

    monkeypatch.setattr(dd, "_serial_by_id_entries", lambda: byid_entries)
    monkeypatch.setattr(dd, "_serial_candidates", lambda: [])
    monkeypatch.setattr(
        dd,
        "_probe_multimeter_idn",
        lambda port, _baud: "5491B  Multimeter,Ver1.4.14.06.18,124G21119" if "5491B_Multimeter" in port else None,
    )
    monkeypatch.setattr(
        dd,
        "_try_mrsignal_on_port",
        lambda port: (True, 2) if "Mr.Signal" in port else (False, None),
    )

    res = dd.autodetect_and_patch_config(log_fn=None)

    def _norm(p: str | None) -> str:
        return str(p or "").replace("\\", "/")

    mrs = _norm(res.mrsignal_port)
    k1 = _norm(res.k1_serial_port)

    assert mrs.endswith("/dev/serial/by-id/usb-STMicroelectronics_LANYI_Mr.Signal_COM_Port_Demo_1.000-if00")
    assert k1.endswith("/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0")
    assert k1 != mrs
    assert _norm(getattr(dd.config, "K1_SERIAL_PORT", "")) == k1


def test_autodetect_can_prefers_pcan_over_canview(monkeypatch):
    from roi.core import device_discovery as dd

    byid_entries = [
        (
            "usb-RM_Michaelides_RM_CANview-USB-if00-port0",
            "/dev/serial/by-id/usb-RM_Michaelides_RM_CANview-USB-if00-port0",
            "/dev/ttyUSB2",
        ),
    ]

    monkeypatch.setattr(dd.config, "AUTO_DETECT_ENABLE", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_VERBOSE", False, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_CANVIEW", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_PCAN", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_MMETER", False, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_MRSIGNAL", False, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_K1_SERIAL", False, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_VISA", False, raising=False)
    monkeypatch.setattr(dd.config, "CAN_INTERFACE", "auto", raising=False)
    monkeypatch.setattr(dd.config, "CAN_CHANNEL", "can0", raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_CANVIEW_BYID_HINTS", "canview", raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_PCAN_PREFER_CHANNEL", "can0", raising=False)

    monkeypatch.setattr(dd, "_serial_by_id_entries", lambda: byid_entries)
    monkeypatch.setattr(dd, "_linux_can_netdevs", lambda: ["can0"])
    monkeypatch.setattr(dd, "_pcan_usb_present", lambda: True)

    res = dd.autodetect_and_patch_config(log_fn=None)

    assert getattr(dd.config, "CAN_INTERFACE", "") == "socketcan"
    assert getattr(dd.config, "CAN_CHANNEL", "") == "can0"
    assert res.can_channel == "can0"


def test_autodetect_can_uses_canview_when_pcan_absent(monkeypatch):
    from roi.core import device_discovery as dd

    canview_path = "/dev/serial/by-id/usb-RM_Michaelides_RM_CANview-USB-if00-port0"
    byid_entries = [
        (
            "usb-RM_Michaelides_RM_CANview-USB-if00-port0",
            canview_path,
            "/dev/ttyUSB2",
        ),
    ]

    monkeypatch.setattr(dd.config, "AUTO_DETECT_ENABLE", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_VERBOSE", False, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_CANVIEW", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_PCAN", True, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_MMETER", False, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_MRSIGNAL", False, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_K1_SERIAL", False, raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_VISA", False, raising=False)
    monkeypatch.setattr(dd.config, "CAN_INTERFACE", "auto", raising=False)
    monkeypatch.setattr(dd.config, "CAN_CHANNEL", "can0", raising=False)
    monkeypatch.setattr(dd.config, "AUTO_DETECT_CANVIEW_BYID_HINTS", "canview", raising=False)

    monkeypatch.setattr(dd, "_serial_by_id_entries", lambda: byid_entries)
    monkeypatch.setattr(dd, "_linux_can_netdevs", lambda: ["can0"])
    monkeypatch.setattr(dd, "_pcan_usb_present", lambda: False)

    res = dd.autodetect_and_patch_config(log_fn=None)

    def _norm(p: str | None) -> str:
        return str(p or "").replace("\\", "/")

    assert getattr(dd.config, "CAN_INTERFACE", "") == "rmcanview"
    assert _norm(getattr(dd.config, "CAN_CHANNEL", "")).endswith(canview_path)
    assert _norm(res.can_channel).endswith(canview_path)
