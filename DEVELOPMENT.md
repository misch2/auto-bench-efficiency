# Development

Run the offline behavioral suite from this repository's root:

```powershell
python -B -m unittest discover -s tests -p test_measure_efficiency.py -v
```

The tests use simulated registers, serial traffic and time. They do not open
physical COM ports or verify electrical measurement accuracy. Real hardware
verification must identify the supply/load versions, wiring and operating point.
Install `requirements.txt` to also exercise the real MinimalModbus identification
path with simulated serial bytes; that integration test is skipped if the
dependency is unavailable. It covers the zero-filled and empty responses without
opening any COM port.

MinimalModbus 2.1 or newer is required for its already-opened serial transport
API. The runner owns the RK6006 transport before constructing the upstream
module, enabling raw tracing of the first request and reliable port cleanup
when identification fails. The serial trace delegates configuration and I/O;
it does not modify frames or retry commands. The upstream submodule is unchanged.

ATORCH can deliver an incomplete `ff 55 01 02` telemetry report followed by
the command ACK (`6f`) or a framed PX100 reply. Previously the incomplete report
blocked recognition of that reply until timeout. After a 100 ms receive idle
gap, the parser can discard the incomplete report prefix and recover an expected
reply suffix. Complete checksum-validated reports are consumed normally, and
short gaps between fragments preserve the frame. A recovered ACK never replaces
the existing current/state readback checks; no command is retried. Regression
tests cover both observed ACK sequences, reply framing, fragmentation, readback
mismatch and genuine timeouts.

Standalone ACK bytes can also arrive before a new request or while awaiting
the framed readback reply. These carry no measurement or setting value, so they
are discarded and logged as `atorch_ack_ignored`. Waiting continues within the
original deadline; it is never extended by ACK traffic. Setters still require
the actual queried value/state to match. Unexpected framed data before a new
request still invalidates synchronization, and timeouts still block later
requests, preventing a late data reply from being used for another query.

`--keep-source-on` skips source OFF in initialization, limit configuration and
cleanup, and skips all source restoration writes. Load shutdown remains active.
For live configuration, higher existing OVP/OCP thresholds are retained until
the new voltage/current settings are applied, then the requested protections
are written. Regression tests cover normal completion, communication/operating
errors, Ctrl+C in measurement and startup, early load-open failure and lowering
live protections. Hardware protection trips are never automatically re-enabled.

The `RK6006-Python-Module` submodule points to
[`dzwer/RK6006-Python-Module`](https://github.com/dzwer/RK6006-Python-Module),
commit `b6a76d515553f19db00c93b79081b04cb20ed9c9`. Its source is not copied into
this project's MIT-licensed files. No license file was present in that upstream
commit; do not assume this project's license grants rights to the upstream code.
Keep its attribution and review its licensing before redistributing it in a
package or binary release.

The tool originated in the UPS12V hardware diagnostic tools. Measurement evidence
from that project is retained there, outside this standalone repository.

Python dependencies are in `requirements.txt`. No credentials, device recordings,
virtual environment or historical hardware results belong in this repository.
