from roi.core.hardware import _relay_auto_backend_order


def test_relay_auto_backend_order_single_channel_prefers_serial():
    assert _relay_auto_backend_order(1) == ("serial", "dsdtech")


def test_relay_auto_backend_order_multi_channel_prefers_dsdtech():
    assert _relay_auto_backend_order(4) == ("dsdtech", "serial")


def test_relay_auto_backend_order_invalid_defaults_to_single_channel():
    assert _relay_auto_backend_order("bad") == ("serial", "dsdtech")
