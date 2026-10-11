# Hardware tests

Tests that talk to a real controller live here and are marked
`@pytest.mark.hardware`. They are **never** run in CI and never against a
production PLC without the plant's agreement. They must stay read-only.

First planned test (M4/M5): read one known word from the RDC-53 PLC via the
`mitsubishi_slmp` driver and compare it with the value shown in GX Works3.
