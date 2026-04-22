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


def test_relay_legacy_auto_unavailable_uses_mock(monkeypatch):
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


def test_relay_legacy_serial_alias_maps_to_dsdtech_fail_fast(monkeypatch):
    import pytest
    from roi import config
    from roi.core import hardware as hw

    monkeypatch.setattr(config, "K1_ENABLE", True, raising=False)
    monkeypatch.setattr(config, "K1_BACKEND", "serial", raising=False)
    monkeypatch.setattr(config, "K1_SERIAL_PORT", "/dev/serial/by-id/k1", raising=False)
    monkeypatch.setattr(config, "K1_CHANNEL_COUNT", 1, raising=False)
    monkeypatch.setattr(config, "K1_INIT_RETRIES", 1, raising=False)
    monkeypatch.setattr(config, "K1_INIT_RETRY_DELAY_SEC", 0.0, raising=False)

    def _boom(*_args, **_kwargs):
        raise OSError("serial open failed")

    monkeypatch.setattr(hw.serial, "Serial", _boom, raising=False)

    with pytest.raises(RuntimeError, match="K1 dsdtech relay unavailable"):
        hw.HardwareManager()


def test_relay_suffix_rn_from_systemd_env_is_normalized(monkeypatch):
    from roi import config
    from roi.core import hardware as hw

    class _FakeSerial:
        last = None

        def __init__(self, *_args, **_kwargs):
            self.writes = []
            _FakeSerial.last = self

        def write(self, payload):
            self.writes.append(bytes(payload))
            return len(payload)

        def flush(self):
            return None

        def reset_input_buffer(self):
            return None

        def reset_output_buffer(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(config, "K1_ENABLE", True, raising=False)
    monkeypatch.setattr(config, "K1_BACKEND", "dsdtech", raising=False)
    monkeypatch.setattr(config, "K1_SERIAL_PORT", "/dev/serial/by-id/k1", raising=False)
    monkeypatch.setattr(config, "K1_CHANNEL_COUNT", 1, raising=False)
    monkeypatch.setattr(config, "K1_DSDTECH_CHANNEL", 1, raising=False)
    monkeypatch.setattr(config, "K1_DSDTECH_BAUD", 9600, raising=False)
    monkeypatch.setattr(config, "K1_DSDTECH_BOOT_DELAY_SEC", 0.0, raising=False)
    monkeypatch.setattr(config, "K1_DSDTECH_CMD_TEMPLATE", "AT+CH{index}={state}", raising=False)
    # systemd EnvironmentFile often loads \r\n as plain "rn".
    monkeypatch.setattr(config, "K1_DSDTECH_CMD_SUFFIX", "rn", raising=False)
    monkeypatch.setattr(config, "K1_INIT_RETRIES", 1, raising=False)
    monkeypatch.setattr(config, "K1_INIT_RETRY_DELAY_SEC", 0.0, raising=False)

    monkeypatch.setattr(hw.serial, "Serial", _FakeSerial, raising=False)

    mgr = hw.HardwareManager()
    assert mgr.relay_backend == "dsdtech"
    assert _FakeSerial.last is not None
    # Initial idle apply emits one command; ensure it is CRLF-terminated.
    assert _FakeSerial.last.writes
    assert _FakeSerial.last.writes[0].endswith(b"\r\n")
