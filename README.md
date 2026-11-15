# Auto Bench Efficiency

Automatic DC power-transfer measurements with a **RIDEN RK6006 supply** and an
**ATORCH DL24P electronic load**. Choose supply voltages and load currents, start
the test and open the resulting HTML report. It includes a graph, measured
power and energy, and a CSV file for further analysis.

The program reads power at the supply output and at the load input. With a
direct cable connection, it measures the transfer through that cable and its
contacts. **It does not measure the supply's input-to-output conversion
efficiency.** That requires a separate input-power measurement.

## Equipment and connections

- RIDEN **RK6006**, connected to the computer through its USB serial interface.
- ATORCH **DL24P with the wired serial interface**, connected through USB.
- A suitable input power supply for the RK6006 and the normal auxiliary supply
  for the electronic load.
- A Windows computer with Python 3 installed. Python 3.12 has been used for the
  software tests. Git is needed to obtain the included RK6006 interface library.

The hardware used during development was an RK6006 with firmware 1.07 and a
wired DL24P. Other RIDEN models, Bluetooth-only loads and other ATORCH hardware
versions have not been verified.

With both outputs **off**, connect the RK6006 positive output to the load's
positive input and the negative output to the load's negative input. Use leads
and contacts rated for the intended current. Connect the load's voltage-sense
terminals according to its manual; their connection determines which wiring
losses are included in the measured voltage.

Set the ATORCH locally to **CC (constant current)**. Set its voltage cutoff
below the lowest voltage expected at its terminals. This program checks the
cutoff but does not select the load mode for you.

The default test reaches **20 V and 2 A, or 40 W**. Check the connected circuit,
leads and load cooling before using the full test. If you insert an ammeter,
its input and range must cover the whole selected test, not just its first point.

Both devices may share a return path through the computer's USB connections.
The DL24P wired serial interface is not isolated. For small cable-loss
measurements, check that a USB ground connection is not providing another
current path; suitable isolated communication can avoid this problem.

## Install once

Clone this project **with its Git submodule**, then enter its folder:

```powershell
git clone --recurse-submodules https://github.com/misch2/auto-bench-efficiency.git
cd auto-bench-efficiency
```

If you already have the project folder, open PowerShell there. This command
fetches its RK6006 library:

```powershell
git submodule update --init --recursive
```

Create a separate Python environment and install the required packages:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

You do not need to activate the environment or change PowerShell's execution
policy. Use the Python command shown below each time.

## Identify the COM ports

Open **Device Manager → Ports (COM & LPT)**. Both devices commonly appear as
USB-SERIAL CH340, so the displayed name alone does not distinguish them.
Disconnect and reconnect one USB cable at a time to identify the port number.

The defaults are **COM3 for RK6006** and **COM4 for ATORCH**. Replace these in
the commands if your computer assigns different numbers. Close other programs
using either port.

First check communication:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --source-port COM3 --load-port COM4 --probe
```

This displays the identified supply, input voltage, OVP/OCP, load readings and
cutoff. **`--probe` does not change settings or output states.**

## Start with one point

For a direct connection that can accept 5 V:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --voltages 5 --currents 0.1 --seconds 10
```

The program turns the identified instruments off, configures the test, turns
them on and waits for acceptable readings. Once settled, it measures for the
requested 10 seconds and turns both outputs off.

Watch the instruments during the first test. Press **Ctrl+C** to stop. If the
program reports that OFF could not be verified, switch the equipment off locally.
A disconnected serial cable prevents the computer from confirming shutdown.

## Run a sweep

With the default COM ports:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py
```

The default sweep has **40 points**:

| Setting | Values |
| --- | --- |
| Supply voltage | 5, 9, 12, 15, 20 V |
| Load current | 0.1, 0.2, 0.25, 0.5, 0.75, 1, 1.5, 2 A |
| Measurement time | 10 seconds per point, after settling |

Allow roughly ten minutes, depending on settling and repeated points. The
console shows completed/total points, points remaining, elapsed time and an
estimated time to finish. After the first point, that estimate follows the
observed test speed. Time spent on repeated attempts is included.

To select a smaller part of the sweep:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --voltages 9 12 --currents 0.5 1 2 --seconds 20
```

Voltages and currents must come from the values in the table. The selected
voltage is the **supply setpoint**; current is the **load's CC setpoint**.

### Supply protections

For the default sweep, the program temporarily sets:

| Setting | Default full-sweep value |
| --- | ---: |
| Supply current limit | 2.1 A |
| OVP | 22 V |
| OCP | 2.31 A |

Smaller sweeps use correspondingly smaller limits. The current limit is 0.1 A
above the highest selected load current. OVP is 110% of the highest selected
voltage, and OCP is 110% of the supply current limit. You can specify different
OVP/OCP values with `--ovp` and `--ocp`; they must exceed the selected test
voltage/current limit and fit the instrument's range.

On exit, the program attempts to restore the original supply voltage, current
limit, OVP and OCP **after verifying supply OFF**. It leaves the outputs off,
even if they were on before the test. Any restoration failure is reported.

## Open the results

The console prints a new folder under **`results/`** in this project:

| File | Use |
| --- | --- |
| `results.html` | Open in a browser for the graph and table; no server or internet connection needed |
| `results.csv` | Completed points, averaged readings, integrated Wh and calculated efficiency |
| `debug.jsonl` | Detailed diagnostic record, including rejected readings and shutdown |

Completed points are saved as the test progresses. If a later point fails or
you stop the run, earlier completed points remain available. Initialization
failures may leave only the debug log.

Each reported point contains one complete measurement window. If a reading
leaves the permitted operating range, the program rejects that entire window,
turns both outputs off and retries the point once. The CSV/HTML **Attempts**
column shows whether a point needed repeating. Rejected attempts remain in the
debug log. Repeated deviations stop the test; ATORCH communication errors and
supply protection trips stop it immediately. Read-only RK6006 queries allow
up to three attempts for a missing or invalid reply; if all fail, the run stops
and attempts shutdown. Writes are not retried.

### What the percentage means

**Efficiency = energy measured at the load / energy measured at the supply
output × 100%.** Both are calculated from sampled voltage and current readings.
The readings are sequential; this is a steady-state test, not a transient recorder.

A result slightly above 100% is possible because the two instruments disagree.
It does not mean the cable produces energy. Low-current readings deserve
particular care: the RK6006 manual specifies current accuracy of
±(0.5% + 5 counts), with 1 mA resolution. Around 100 mA, that amounts to about
±5.5 mA. This setup cannot establish a small efficiency difference at that
current without checking/calibrating the instruments. Longer averaging does
not remove a systematic measurement error.

## Useful adjustments

| Option | Default | When to change it |
| --- | --- | --- |
| `--source-port`, `--load-port` | COM3, COM4 | Windows assigned different ports |
| `--seconds` | 10 | Measure each settled point for longer |
| `--settle` | 2 | Wait longer before checking settling |
| `--settle-timeout` | 15 | Allow slower settling; must exceed `--settle` |
| `--point-retries` | 1 | Set `0` to stop on the first rejected window |
| `--output-dir` | A dated folder in `results/` | Save one run to a different **new** folder |

For example, allow a slower setup to settle:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --settle 5 --settle-timeout 30
```

By default, acceptable readings require source CV mode, supply voltage within
the larger of 0.1 V or 5% of its setpoint, and load current within the larger of
0.01 A or 5% of its setpoint. Three consecutive acceptable pairs are required
before measurement. `--voltage-tolerance` and `--current-tolerance` change the
relative fractions (for example `0.05` means 5%). These are operating-point
checks, not a statement of instrument accuracy. Check the cause of a rejected
reading before relaxing them.

## Troubleshooting

**No communication / no answer:** check the power, port assignment and USB
cable, and close other applications using the port. Start with `--probe`.
After a communication fault, check OFF locally before restarting.

**OVP or OCP:** the program displays the test limits. Read the reported
protection and check the wiring and intended current before raising limits.

**Did not settle / wrong load current:** confirm the load is in CC mode, check
its cutoff and compare the displays. The program records both instruments'
readings and retains rejected samples in `debug.jsonl`. An isolated outlier may
come from measurement or communication; a correctly decoded number alone does
not prove a real current transient. The retry repeats the entire point.

**Unexpected voltage drop or efficiency:** measure voltage directly at both
sets of terminals with a multimeter in DC voltage mode. Check contacts, sense
connections and alternative returns through USB before interpreting small losses.

When reporting a problem, include the hardware/firmware, wiring, command used
and the run's `debug.jsonl` and CSV. The log includes device port names, readings
and settings, but no passwords are requested by this program.

## References

- [RK6006 manual, 2024-04-23](https://download.bastelgarage.ch/Produkte/RK6006-2024-4-23.pdf)
- [DL24P serial interface notes](https://github.com/tshaddack/dl24#protocol)
- [RK6006 Python interface](https://github.com/dzwer/RK6006-Python-Module)

Auto Bench Efficiency is provided under the [MIT license](LICENSE). The separate
RK6006 interface is an upstream Git submodule; see [DEVELOPMENT.md](DEVELOPMENT.md)
for its provenance and the offline test command.
