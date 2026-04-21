from roi.core.hardware import _relay_auto_backend_order


def test_relay_auto_backend_order_single_channel_prefers_serial():
    assert _relay_auto_backend_order(1) == ("serial", "dsdtech")


def test_relay_auto_backend_order_multi_channel_prefers_dsdtech():
    assert _relay_auto_backend_order(4) == ("dsdtech", "serial")


def test_relay_auto_backend_order_invalid_defaults_to_single_channel():
    assert _relay_auto_backend_order("bad") == ("serial", "dsdtech")


def test_relay_forced_dsdtech_fail_fast(monkeypatch):
    import pytest
    from roi import config
    from roi.core import hardware as hw

    monkeypatch.setattr(config, "K1_ENABLE", True, raising=False)
    monkeypatch.setattr(config, "K1_BACKEND", "dsdtech", raising=False)
    monkeypatch.setattr(config, "K1_SERIAL_PORT", "/dev/serial/by-id/k1", raising=False)
    monkeypatch.setattr(config, "K1_CHANNEL_COUNT", 1, raising=False)
    monkeypatch.setattr(config, "K1_DSDTECH_CHANNEL", 1, raising=False)
    monkeypatch.setattr(config, "K1_DSDTECH_BAUD", 9600, raising=False)
    monkeypatch.setattr(config, "K1_DSDTECH_BOOT_DELAY_SEC", 0.0, raising=False)
    monkeypatch.setattr(config, "K1_DSDTECH_CMD_TEMPLATE", "AT+CH{index}={state}", raising=False)
    monkeypatch.setattr(config, "K1_DSDTECH_CMD_SUFFIX", r"\r\n", raising=False)
    monkeypatch.setattr(config, "K1_INIT_RETRIES", 1, raising=False)
    monkeypatch.setattr(config, "K1_INIT_RETRY_DELAY_SEC", 0.0, raising=False)

    def _boom(*_args, **_kwargs):
        raise OSError("serial open failed")

    monkeypatch.setattr(hw.serial, "Serial", _boom, raising=False)

    with pytest.raises(RuntimeError, match="K1 dsdtech relay unavailable"):
        hw.HardwareManager()


def test_relay_auto_backend_unavailable_uses_mock(monkeypatch):
    from roi import config
    from roi.core import hardware as hw

    monkeypatch.setattr(config, "K1_ENABLE", True, raising=False)
    monkeypatch.setattr(config, "K1_BACKEND", "auto", raising=False)
    monkeypatch.setattr(config, "K1_SERIAL_PORT", "/dev/serial/by-id/k1", raising=False)
    monkeypatch.setattr(config, "K1_CHANNEL_COUNT", 1, raising=False)
    monkeypatch.setattr(config, "K1_INIT_RETRIES", 1, raising=False)
    monkeypatch.setattr(config, "K1_INIT_RETRY_DELAY_SEC", 0.0, raising=False)

    def _boom(*_args, **_kwargs):
        raise OSError("serial open failed")

    monkeypatch.setattr(hw.serial, "Serial", _boom, raising=False)

    mgr = hw.HardwareManager()
    assert mgr.relay_backend == "mock"
