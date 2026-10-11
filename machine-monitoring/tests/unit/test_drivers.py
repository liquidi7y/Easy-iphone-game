import inspect

import pytest

from collector.drivers import DRIVERS, Driver, DriverNotImplemented, create_driver


def test_no_driver_exposes_a_write_path():
    for name, cls in DRIVERS.items():
        for attr, _ in inspect.getmembers(cls):
            low = attr.lower()
            assert not low.startswith(("write", "force", "set_", "upload", "download")), f"{name}.{attr}"


def test_defining_a_write_method_is_refused():
    with pytest.raises(TypeError, match="read-only"):
        class Evil(Driver):  # noqa: F841
            async def write_words(self, *a):
                ...


def test_interface_is_complete():
    for cls in DRIVERS.values():
        for m in ("connect", "health", "read_bits", "read_words", "read_block", "disconnect"):
            assert callable(getattr(cls, m))


async def test_mitsubishi_is_a_placeholder_until_m4():
    d = create_driver("mitsubishi_slmp", "192.0.2.1", 5007, 1000, {})
    with pytest.raises(DriverNotImplemented):
        await d.connect()


def test_unknown_driver():
    with pytest.raises(DriverNotImplemented):
        create_driver("opcua", "h", 1, 1000, {})


def test_mitsubishi_radix():
    d = create_driver("mitsubishi_slmp", "h", 1, 1000, {})
    assert d.address_index("X", "1F") == 31
    assert d.address_index("M", "100") == 100
    d8 = create_driver("mitsubishi_slmp", "h", 1, 1000, {"xy_radix": "octal"})
    assert d8.address_index("X", "17") == 15
    assert d8.format_number("Y", 15) == "17"
