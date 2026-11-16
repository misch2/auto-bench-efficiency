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

There are **no default test voltages, load currents or source current limit**.
Every measurement command must explicitly include `--voltages`, `--currents`
and `--source-current-limit`; they are read only from the command line.
Check the connected circuit,
leads and load cooling against every selected point. If you insert an ammeter,
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

List the ports currently available on the computer without opening them:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --list-ports
```

There are **no default COM ports**. Both `--source-port` and `--load-port` must
be specified for measurement and `--probe`. The commands below use **COM4 for
RK6006** and **COM3 for ATORCH as examples only**; replace them with the ports
you identified. Close other programs using either port.

First check communication:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --source-port COM4 --load-port COM3 --probe
```

This displays the identified supply, input voltage, OVP/OCP, load readings and
cutoff. **`--probe` does not change settings or output states.**

## Start with one point

For a direct connection that can accept 5 V:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --source-port COM4 --load-port COM3 --voltages 5 --currents 0.1 --source-current-limit 0.2 --seconds 10
```

By default, the program turns the identified instruments off, configures the test, turns
them on once and waits for acceptable readings. Once settled, it measures for
the requested 10 seconds and turns both outputs off at the end of the run.

Watch the instruments during the first test. Press **Ctrl+C** to stop. If the
program reports that OFF could not be verified, switch the equipment off locally.
A disconnected serial cable prevents the computer from confirming shutdown.

## Run a sweep

Explicitly select both ports and every desired voltage and current:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --source-port COM4 --load-port COM3 --voltages 5 9 12 15 20 --currents 0.1 0.2 0.25 0.5 0.75 1 1.5 2 --source-current-limit 2.1
```

This example sweep has **40 points** and reaches **20 V and 2 A, or 40 W**:

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
.\.venv\Scripts\python.exe .\measure_efficiency.py --source-port COM4 --load-port COM3 --voltages 9 12 --currents 0.5 1 2 --source-current-limit 2.1 --seconds 20
```

Voltages and currents are not restricted to the example values in the table.
Specify any finite positive values in steps of **0.01 V** and **0.01 A**
(for example `--voltages 3.3 1.8 --currents 0.33 0.12`). The program checks
the supply's available voltage range, current limit and protection limits before
enabling its output. The selected voltage is the **supply setpoint**;
current is the **load's CC setpoint**.
The lists are traversed in the order given: all currents at the first voltage,
then all currents at the next voltage. Unless a separate startup phase is
specified, the first voltage and first load current are the starting operating
point.

**The supply and load remain on between points and during point retries.**
The program changes setpoints without switching the RK6006 output off or
commanding 0 V. This keeps a device under test powered after its initial start.

By default, the initial setup switches the outputs off. Completion, Ctrl+C, exhausted
retries, communication errors or protection trips end the run and trigger
shutdown. Actual voltage continuity during setpoint changes depends on the
supply and the connected circuit.
Use `--keep-source-on` to preserve source power during setup and on exit as
described below; the load is still shut down.

### Give the device time to start

To start at **12 V with a 0.5 A source current limit for 60 seconds**, then measure each selected
point for **15 seconds**, specify all three startup options together:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --source-port COM4 --load-port COM3 --startup-voltage 12 --startup-current 0.5 --startup-seconds 60 --voltages 3.3 5 --currents 0.1 0.2 0.5 --source-current-limit 0.6 --seconds 15
```

The program sets the RK6006 startup voltage and current limit, enables its
output and waits for the startup duration. **The ATORCH load remains OFF
throughout startup.** The program then switches to the measurement current
limit, sets the first measurement voltage and load current, and enables the
load without switching the source off. Startup is performed
**only once per run**, even if a measurement point is retried. It is excluded
from the CSV points, measured energy and sweep progress/ETA; its settings and
duration are recorded in `debug.jsonl`.

Startup voltage uses **0.01 V** steps; `--startup-current` is the **RK6006
current limit**, in **0.001 A** steps. Both `--startup-current` and the required
measurement `--source-current-limit` are applied exactly as specified, with no
automatic margin. OVP/OCP cover both startup and
measurement settings. The ATORCH cutoff applies only to measurement voltages,
because its load is off during startup. Communication errors, protection trips or Ctrl+C abort startup
and trigger shutdown; temporary source CC mode is allowed during startup.

After startup, `--settle` (default 2 seconds) and the settling checks apply
before each point and retry. Increasing `--settle` affects every point;
`--startup-seconds` affects only the initial startup. Omitting all three startup
options skips the separate startup phase. They cannot be combined with `--probe`.

### Supply protections

For the 40-point example above, the program temporarily sets:

| Setting | Example full-sweep value |
| --- | ---: |
| Supply current limit | 2.1 A |
| OVP | 22 V |
| OCP | 2.31 A |

During measurement, the source current limit is exactly the required
`--source-current-limit` value (0.001 A steps), fixed across the whole sweep.
It is independent of `--currents`, which controls the load. Choose the source
limit for the **input current of the device under test**, including its losses
and the full selected voltage/load range. For a boost converter producing
12 V / 1 A from 5 V, even ideal input current is 2.4 A; at 90% efficiency it
would be about 2.67 A. A load-based input limit is therefore inappropriate.
There is no default source current limit or automatic current margin.

During startup the source limit is exactly `--startup-current`.
OVP is 110% of the highest selected
voltage, including startup, and OCP is 110% of the higher of the two source
current limits. You can specify different
OVP/OCP values with `--ovp` and `--ocp`; they must exceed the selected test
voltage/current limit and fit the instrument's range.

By default, on exit the program attempts to restore the original supply voltage, current
limit, OVP and OCP **after verifying supply OFF**. It leaves the outputs off,
even if they were on before the test. Any restoration failure is reported.

### Keep the source powered

Add **`--keep-source-on`** to prevent the program from sending any RK6006 OFF
command during setup, between points, on completion, after an error or on Ctrl+C:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --source-port COM4 --load-port COM3 --voltages 5 --currents 0.1 --source-current-limit 1 --keep-source-on
```

The source is configured and enabled for the requested startup/measurement as
usual. On exit, its output state and the last applied settings are left in
place. **Original source voltage, current limit and OVP/OCP are not restored.**
If initialization fails early, the source retains its existing state; the flag
does not force an unidentified or unconfigured source ON. The console and debug
log record that source OFF and restoration were skipped.

The ATORCH load remains OFF during startup and is still turned off and verified
when the run ends, including errors. Failure to verify load OFF is still reported.
The source's hardware OVP/OCP remain active and can turn it off independently;
the program does not re-enable it after a protection trip. Changing voltage or
current limits can still affect actual output voltage. Without this flag, the
normal source shutdown and restoration behavior applies.

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
keeps both outputs on, waits for settling again and retries the point once.
The CSV/HTML **Attempts**
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
| `--voltages`, `--currents` | None; both required for measurement | Explicitly choose the operating points for this run |
| `--source-current-limit` | None; required for measurement | Set the RK6006 measurement current limit independently of load current |
| `--source-port`, `--load-port` | None; both required | Explicitly identify the connected devices |
| `--list-ports` | Off | List available serial ports and exit without opening them |
| `--seconds` | 10 | Measure each settled point for longer |
| `--startup-voltage`, `--startup-current`, `--startup-seconds` | None; all three required together | Run a separate one-time startup phase |
| `--debug-serial` | Off | Print both ports' raw TX/RX in hex, including identification |
| `--keep-source-on` | Off | Skip all source OFF commands and source restoration, including on errors/Ctrl+C; still shut down the load |
| `--settle` | 2 | Wait longer before checking settling |
| `--settle-timeout` | 15 | Allow slower settling; must exceed `--settle` |
| `--point-retries` | 1 | Set `0` to stop on the first rejected window |
| `--output-dir` | A dated folder in `results/` | Save one run to a different **new** folder |

For example, allow a slower setup to settle:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --source-port COM4 --load-port COM3 --voltages 5 --currents 0.1 --source-current-limit 0.2 --settle 5 --settle-timeout 30
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

For detailed serial diagnostics without changing settings, use the actual ports:

```powershell
.\.venv\Scripts\python.exe .\measure_efficiency.py --source-port COM4 --load-port COM3 --probe --debug-serial
```

`--debug-serial` prints monotonic timestamps, device name, port, baud rate,
TX/RX bytes in hex, read sizes, empty reads and transport errors to stderr.
It includes RK6006 identification, so it also captures checksum errors or no
answer before the device is recognized. During measurement these raw exchanges
are additionally recorded in `debug.jsonl`; `--probe` creates no results folder.
Tracing does not add commands or retries and can increase communication overhead.

The parser handles a command reply following an interrupted automatic ATORCH
report. Such recovery is recorded as `atorch_report_interrupted` in the debug
log; current and ON/OFF settings still require a separate readback confirmation.
Missing replies or failed readbacks still stop the run.
Extra standalone ACK bytes are logged as `atorch_ack_ignored`; when reading a
value, the parser keeps waiting for a complete data reply within the original
timeout. An ACK alone cannot confirm a current setting or load state.

RK6006 uses 115200 baud and Modbus address 1; ATORCH uses 9600 baud. A checksum
error containing only zero bytes means the received frame was invalid. A
"no answer" error means no valid reply arrived. Neither identifies the physical
cause: check which port belongs to the supply, its communication settings and
the USB connection. A failure during RK6006 identification occurs before this
program can issue an OFF command or change any settings.

**OVP or OCP:** the program displays the test limits. Read the reported
protection and check the wiring and intended current before raising limits.

**Did not settle / wrong load current:** confirm the load is in CC mode, check
its cutoff and compare the displays. The program records both instruments'
readings and retains rejected samples in `debug.jsonl`. An isolated outlier may
come from measurement or communication; a correctly decoded number alone does
not prove a real current transient. The retry repeats the entire point.
Settling errors include the actual source/load voltage and current, identify
zero, negative or nonfinite readings, and show the requested operating point
and tolerances. A source CV-mode failure includes its mode and actual readings;
the error also reports the load readings. If the last samples were acceptable
but fewer than three consecutive pairs were collected, the error gives that
count and the last measurements.

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
