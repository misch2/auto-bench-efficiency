# Development

Run the offline behavioral suite from this repository's root:

```powershell
python -B -m unittest discover -s tests -p test_measure_efficiency.py -v
```

The tests use simulated registers, serial traffic and time. They do not open
physical COM ports or verify electrical measurement accuracy. Real hardware
verification must identify the supply/load versions, wiring and operating point.

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
