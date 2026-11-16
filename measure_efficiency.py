"""Measure power transfer between an RK6006 source and an ATORCH DL24/P load."""

import argparse
from contextlib import ExitStack
import csv
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from html import escape
import math
import json
from pathlib import Path
import sys
import time

TOOLS = Path(__file__).resolve().parent
ATORCH_REPORT = b"\xff\x55\x01\x02"
PX100_REPLY = b"\xca\xcb"
PX100_ACK = b"\x6f"
PX100_REQUEST_GAP = 0.05
PX100_REPLY_TIMEOUT = 1.0
ATORCH_REPORT_IDLE_TIMEOUT = 0.1


def no_debug(event, **details):
    pass


class DebugLog:
    """Flush each diagnostic event; a logging failure must never block OFF."""

    def __init__(self, output):
        self.output = output
        self.started = time.monotonic()
        self.failed = False

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        self(
            "session_end",
            status="error" if error_type else "complete",
            error_type=error_type.__name__ if error_type else None,
            error=str(error) if error is not None else None,
        )

    def __call__(self, event, **details):
        if self.failed:
            return
        record = dict(
            utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            elapsed_s=time.monotonic() - self.started,
            event=event,
            **details,
        )
        record = {
            key: (
                str(value)
                if isinstance(value, float) and not math.isfinite(value)
                else value
            )
            for key, value in record.items()
        }
        try:
            self.output.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.output.flush()
        except (OSError, ValueError) as error:
            self.failed = True
            print(f"Debug logging stopped: {error}", file=sys.stderr)


class OperatingPointError(RuntimeError):
    """Readable measurements have not reached the requested operating point."""


class SerialTrace:
    """Trace the actual transport, including RK6006's identification request."""

    def __init__(self, port, label, debug=no_debug):
        object.__setattr__(self, "_port", port)
        object.__setattr__(self, "_label", label)
        object.__setattr__(self, "_debug", debug)
        object.__setattr__(self, "_console_failed", False)

    def __getattr__(self, name):
        return getattr(self._port, name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._port, name, value)

    def trace(self, event, direction, data=b"", **details):
        self._debug(
            event, instrument=self._label, port=self.port,
            baudrate=self.baudrate, data_hex=data.hex(" "), **details,
        )
        if self._console_failed:
            return
        message = data.hex(" ") or "<no data>"
        if details:
            message += " | " + ", ".join(f"{key}={value}" for key, value in details.items())
        try:
            print(
                f"[{time.monotonic():.3f}] {self._label} {self.port} "
                f"({self.baudrate} baud) {direction}: {message}",
                file=sys.stderr, flush=True,
            )
        except (OSError, ValueError):
            # A closed console must not prevent an emergency OFF command.
            self._console_failed = True

    def write(self, data):
        self.trace("serial_tx", "TX", bytes(data))
        try:
            count = self._port.write(data)
        except (Exception, KeyboardInterrupt) as error:
            self.trace("serial_error", "TX ERROR", error=str(error))
            raise
        self._debug("serial_written", instrument=self._label, port=self.port,
                    bytes=count, expected_bytes=len(data))
        if count != len(data):
            self.trace("serial_partial_write", "TX PARTIAL", bytes=count, expected_bytes=len(data))
        return count

    def read(self, count=1):
        try:
            data = self._port.read(count)
        except (Exception, KeyboardInterrupt) as error:
            self.trace("serial_error", "RX ERROR", requested_bytes=count, error=str(error))
            raise
        self.trace("serial_rx", "RX", bytes(data), requested_bytes=count)
        return data


class RKSource:
    def __init__(self, module, debug=no_debug):
        self.module = module
        self.debug = debug
        self.instrument = module.instrument
        self.volts_resolution = module.volts_resolution
        self.amps_resolution = module.amps_resolution
        self.instrument.serial.write_timeout = 0.5

    def read_registers(self, address, count):
        started = time.monotonic()
        self.debug("rk_read_request", address=address, count=count, max_attempts=3)
        try:
            # The pinned library bounds read-only retries for missing/invalid
            # replies. Avoid its recursive write helper: writes stay single-shot.
            registers = self.module._read_registers(address, count, retries=3)
        except (Exception, KeyboardInterrupt) as error:
            self.debug(
                "rk_read_error", address=address, count=count,
                duration_s=time.monotonic() - started,
                error_type=type(error).__name__, error=str(error),
            )
            raise
        self.debug(
            "rk_read", address=address, registers=registers,
            duration_s=time.monotonic() - started,
        )
        return registers

    def snapshot_settings(self):
        voltage, current = self.read_registers(8, 2)
        ovp, ocp = self.read_registers(82, 2)
        settings = {8: voltage, 9: current, 82: ovp, 83: ocp}
        self.debug("rk_settings", registers=settings)
        return settings

    def write_verified(self, address, value):
        # Keep all writes bounded so a lost connection reaches cleanup.
        self.debug("rk_write", address=address, value=value)
        self.instrument.write_register(address, value)
        actual = self.instrument.read_register(address)
        self.debug("rk_verify", address=address, expected=value, actual=actual)
        if actual != value:
            raise RuntimeError(f"RK6006 register {address} did not confirm {value}")

    def set_enabled(self, enabled):
        self.write_verified(18, int(enabled))

    def set_voltage(self, voltage):
        self.write_verified(8, round(voltage * self.volts_resolution))

    def set_current_limit(self, current):
        self.write_verified(9, round(current * self.amps_resolution))

    def configure_limits(self, args):
        current_limit, ovp, ocp = planned_limits(args)
        voltages = args.voltages
        if (
            current_limit > self.module.max_set_current
            or ocp > self.module.max_ocp_current
        ):
            raise ValueError("Requested current limit or OCP exceeds the RK6006 range")
        if (
            max(voltages) > self.module.max_set_voltage
            or ovp > self.module.max_set_voltage + 2
        ):
            raise ValueError(
                "RK6006 input voltage is too low for the requested voltage/OVP"
            )
        ovp_register = round(ovp * self.volts_resolution)
        ocp_register = round(ocp * self.amps_resolution)
        if args.keep_source_on:
            # Keep existing higher protections until the new operating point
            # is set, so lowering OVP below the old voltage does not trip OFF.
            previous_ovp, previous_ocp = self.instrument.read_registers(82, 2)
            self.write_verified(82, max(previous_ovp, ovp_register))
            self.write_verified(83, max(previous_ocp, ocp_register))
            self.set_current_limit(current_limit)
            self.set_voltage(args.voltages[0])
        else:
            self.set_enabled(False)
        self.write_verified(82, ovp_register)
        self.write_verified(83, ocp_register)
        if not args.keep_source_on:
            self.set_current_limit(current_limit)
        print(
            f"RK6006 sweep limits: CC {current_limit:g} A, OVP {ovp:g} V, OCP {ocp:g} A",
            flush=True,
        )

    def read(self):
        registers = self.read_registers(10, 9)
        voltage, current, _, _, _, _, protection, mode, enabled = registers
        if protection or enabled != 1:
            name = {0: "OK", 1: "OVP", 2: "OCP"}.get(protection, "unknown")
            raise RuntimeError(
                f"RK6006 output interrupted (enabled={enabled}, protection={name}/{protection})"
            )
        if mode != 0:
            raise OperatingPointError(
                f"RK6006 is not in CV mode (mode={mode}); measurement rejected. "
                f"RK6006 {voltage / self.volts_resolution:.3f} V / "
                f"{current / self.amps_resolution:.3f} A"
            )
        return voltage / self.volts_resolution, current / self.amps_resolution


class AtorchLoad:
    def __init__(self, port, debug=no_debug):
        self.port = port
        self.debug = debug
        self.buffer = bytearray()
        self.next_request_at = 0.0
        self.synchronized = True

    def read_bytes(self, count):
        data = self.port.read(count)
        if data:
            self.debug("atorch_rx", data_hex=data.hex(" "))
        return data

    def write_packet(self, packet):
        self.debug("atorch_tx", command=f"0x{packet[2]:02x}", data_hex=packet.hex(" "))
        count = self.port.write(packet)
        self.debug("atorch_written", bytes=count, expected_bytes=len(packet))
        return count

    def pop_frame(self):
        """Keep partial frames and consume complete replies or unsolicited reports."""
        while self.buffer:
            if self.buffer[0] == PX100_ACK[0]:
                del self.buffer[0]
                self.debug(
                    "atorch_frame",
                    data_hex=PX100_ACK.hex(" "),
                    valid=True,
                    check="ack_byte",
                )
                return PX100_ACK
            if self.buffer[0] not in (ATORCH_REPORT[0], PX100_REPLY[0]):
                del self.buffer[0]
                continue
            if len(self.buffer) < 2:
                return None
            if self.buffer.startswith(PX100_REPLY):
                size = 7
            elif self.buffer.startswith(ATORCH_REPORT[:2]):
                if len(self.buffer) < 4:
                    return None
                size = {1: 36, 2: 8}.get(self.buffer[2], 0)
            else:
                size = 0
            if not size:
                del self.buffer[0]
                continue
            if len(self.buffer) < size:
                return None
            frame = bytes(self.buffer[:size])
            if frame.startswith(PX100_REPLY):
                valid = frame.endswith(b"\xce\xcf")
                check = "framing_only_no_checksum"
            else:
                valid = frame[-1] == ((sum(frame[2:-1]) & 0xFF) ^ 0x44)
                check = "checksum"
            if valid:
                del self.buffer[:size]
                self.debug(
                    "atorch_frame", data_hex=frame.hex(" "), valid=True, check=check
                )
                return frame
            self.debug(
                "atorch_frame", data_hex=frame.hex(" "), valid=False, check=check
            )
            del self.buffer[0]
        return None

    def recover_stalled_report(self, prefix, idle_s):
        """Recover a reply suffix after a stalled, incomplete telemetry frame.

        Some DL24P replies terminate an unsolicited report mid-frame. Wait for
        an idle gap before treating it as interrupted; never inspect inside a
        complete, checksum-validated report. ACK is only a hint: both callers
        of write commands still verify the actual setting with a separate query.
        """
        if idle_s < ATORCH_REPORT_IDLE_TIMEOUT:
            return False
        if not self.buffer.startswith(ATORCH_REPORT) or len(self.buffer) >= 36:
            return False
        if prefix == PX100_ACK and self.buffer.endswith(PX100_ACK):
            size = 1
        elif (
            prefix == PX100_REPLY
            and len(self.buffer) >= len(ATORCH_REPORT) + 7
            and self.buffer[-7:-5] == PX100_REPLY
            and self.buffer[-2:] == b"\xce\xcf"
        ):
            size = 7
        else:
            return False
        self.debug(
            "atorch_report_interrupted", idle_s=idle_s,
            discarded_hex=self.buffer[:-size].hex(" "),
            recovered_hex=self.buffer[-size:].hex(" "),
        )
        del self.buffer[:-size]
        return True

    def receive(self, prefix, timeout):
        deadline = time.monotonic() + timeout
        last_data_at = time.monotonic()
        while time.monotonic() < deadline:
            frame = self.pop_frame()
            if frame is None:
                available = min(self.port.in_waiting, 256)
                data = self.read_bytes(available or 1)
                if data:
                    self.buffer.extend(data)
                    last_data_at = time.monotonic()
                else:
                    self.recover_stalled_report(prefix, time.monotonic() - last_data_at)
            elif frame.startswith(prefix):
                return frame
            elif prefix == PX100_REPLY and frame == PX100_ACK:
                # An extra/delayed write ACK carries no reading. Continue to
                # wait for the data frame without extending this deadline.
                self.debug("atorch_ack_ignored", phase="waiting_for_data")
            elif prefix != ATORCH_REPORT and not frame.startswith(ATORCH_REPORT):
                raise RuntimeError(
                    "ATORCH unexpected PX100 reply; synchronization lost"
                )
        raise TimeoutError(
            f"ATORCH {self.port.port}: no reply matching {prefix.hex(' ')}"
        )

    def request(self, command, first=0, second=0):
        # PX100 replies have no transaction ID. After any uncertain transaction,
        # stop instead of retrying and mistaking a late reply for another value.
        if not self.synchronized:
            raise RuntimeError("ATORCH synchronization lost; restart the measurement")
        try:
            time.sleep(max(0, self.next_request_at - time.monotonic()))
            available = self.port.in_waiting
            if available:
                self.buffer.extend(self.read_bytes(available))
            while (frame := self.pop_frame()) is not None:
                if frame == PX100_ACK:
                    # A leftover ACK cannot identify any setting. Drop it;
                    # setters still require a fresh readback after their write.
                    self.debug("atorch_ack_ignored", phase="before_request", command=f"0x{command:02x}")
                    continue
                if not frame.startswith(ATORCH_REPORT):
                    raise RuntimeError(
                        "ATORCH stale PX100 reply before request; synchronization lost"
                    )
            if self.buffer and self.buffer[0] == PX100_REPLY[0]:
                raise RuntimeError(
                    "ATORCH incomplete stale PX100 reply; synchronization lost"
                )

            packet = bytes((0xB1, 0xB2, command, first, second, 0xB6))
            if self.write_packet(packet) != len(packet):
                raise OSError(f"ATORCH command 0x{command:02x}: incomplete write")
            return self.receive(
                PX100_REPLY if command >= 0x10 else PX100_ACK, PX100_REPLY_TIMEOUT
            )
        except TimeoutError as error:
            self.synchronized = False
            self.debug("atorch_error", command=f"0x{command:02x}", error=str(error))
            raise TimeoutError(f"ATORCH command 0x{command:02x}: {error}") from error
        except (Exception, KeyboardInterrupt) as error:
            self.synchronized = False
            self.debug(
                "atorch_error",
                command=f"0x{command:02x}",
                error_type=type(error).__name__,
                error=str(error),
            )
            raise
        finally:
            self.next_request_at = time.monotonic() + PX100_REQUEST_GAP

    def query(self, command):
        value = int.from_bytes(self.request(command)[2:5], "big")
        self.debug("atorch_query", command=f"0x{command:02x}", value=value)
        return value

    def read_enabled(self):
        enabled = self.query(0x10)
        if enabled not in (0, 1):
            raise RuntimeError(f"ATORCH returned invalid load state {enabled}")
        return bool(enabled)

    def set_enabled(self, enabled):
        if not enabled and not self.synchronized:
            # OFF is idempotent. Send it even when replies cannot be trusted, but
            # never claim that an old ACK or state reply verified shutdown.
            packet = bytes((0xB1, 0xB2, 0x01, 0, 0, 0xB6))
            self.debug("atorch_emergency_off", verified=False)
            if self.write_packet(packet) != len(packet):
                raise OSError("ATORCH emergency OFF: incomplete write")
            raise RuntimeError(
                "ATORCH OFF sent without verification after synchronization loss"
            )
        self.request(0x01, int(enabled))
        if self.read_enabled() != enabled:
            raise RuntimeError(
                f"ATORCH did not confirm load {'ON' if enabled else 'OFF'}"
            )

    def set_current(self, current):
        centiamps = round(current * 100)
        self.request(0x02, *divmod(centiamps, 100))
        if self.query(0x17) != centiamps:
            raise RuntimeError(
                f"ATORCH did not confirm the {current:g} A setpoint; select CC mode locally"
            )

    def read(self):
        return self.query(0x11) / 1000, self.query(0x12) / 1000


@dataclass(frozen=True)
class Point:
    set_v: float
    set_a: float
    window_s: float
    samples: int
    source_v: float
    source_a: float
    source_w: float
    source_wh: float
    load_v: float
    load_a: float
    load_w: float
    load_wh: float
    efficiency_pct: float
    attempts: int = 1


def planned_limits(args):
    voltages = args.voltages
    current_limit = args.source_current_limit
    ovp = round(args.ovp if args.ovp is not None else max(voltages) * 1.1, 2)
    ocp = round(args.ocp if args.ocp is not None else current_limit * 1.1, 3)
    if not math.isfinite(ovp) or ovp <= max(voltages):
        raise ValueError(
            "--ovp must exceed the highest test voltage (at 0.01 V resolution)"
        )
    if not math.isfinite(ocp) or ocp <= current_limit:
        raise ValueError(
            "--ocp must exceed the source current limit (at 0.001 A resolution)"
        )
    return current_limit, ovp, ocp


def sample_pair(
    source,
    load,
    voltage,
    current,
    voltage_tolerance,
    current_tolerance,
    phase="measurement",
):
    before = time.monotonic()
    allowed_v = max(0.1, voltage * voltage_tolerance)
    allowed_a = max(0.01, current * current_tolerance)
    targets = (
        f"Requested source {voltage:g} V +/- {allowed_v:g} V; "
        f"load {current:g} A +/- {allowed_a:g} A"
    )
    try:
        source_v, source_a = source.read()
    except OperatingPointError as error:
        load_v, load_a = load.read()
        message = f"{error}; ATORCH {load_v:.3f} V / {load_a:.3f} A. {targets}"
        source.debug("sample_rejected", phase=phase, error=message,
                     load_v=load_v, load_a=load_a)
        raise OperatingPointError(message) from error
    load_v, load_a = load.read()
    after = time.monotonic()
    source.debug(
        "sample",
        phase=phase,
        set_v=voltage,
        set_a=current,
        source_v=source_v,
        source_a=source_a,
        load_v=load_v,
        load_a=load_a,
        read_duration_s=after - before,
    )
    readings = (
        f"RK6006 {source_v:.3f} V / {source_a:.3f} A; "
        f"ATORCH {load_v:.3f} V / {load_a:.3f} A"
    )
    invalid = [
        f"{label}={value:g} {unit}"
        for label, value, unit in (
            ("source voltage", source_v, "V"), ("source current", source_a, "A"),
            ("load voltage", load_v, "V"), ("load current", load_a, "A"),
        )
        if not math.isfinite(value) or value <= 0
    ]
    if invalid:
        raise OperatingPointError(
            "Invalid or missing voltage/current; measurement rejected. "
            f"Must be finite and > 0: {', '.join(invalid)}. {readings}. {targets}"
        )
    for label, measured, target, tolerance in (
        ("source voltage", source_v, voltage, allowed_v),
        ("load current", load_a, current, allowed_a),
    ):
        if abs(measured - target) > tolerance:
            raise OperatingPointError(
                f"Wrong {label}: measured {measured:g}, expected {target:g} "
                f"+/- {tolerance:g}; measurement rejected. "
                f"{readings}"
            )
    values = (source_v, source_a, source_v * source_a, load_v, load_a, load_v * load_a)
    return (before + after) / 2, values


def wait_for_settle(source, load, voltage, current, args):
    """Require three consecutive acceptable pairs before starting integration."""
    started = time.monotonic()
    deadline = started + args.settle_timeout
    consecutive = 0
    last_status = "No settled readings yet"
    source.debug("phase", phase="settling", set_v=voltage, set_a=current)
    time.sleep(args.settle)
    while time.monotonic() < deadline:
        try:
            _, values = sample_pair(
                source,
                load,
                voltage,
                current,
                args.voltage_tolerance,
                args.current_tolerance,
                phase="settling",
            )
        except OperatingPointError as error:
            consecutive = 0
            last_status = str(error)
            source.debug("settling_rejected", error=str(error))
        else:
            consecutive += 1
            last_status = (
                f"Only {consecutive}/3 consecutive readings in tolerance. "
                f"RK6006 {values[0]:.3f} V / {values[1]:.3f} A; "
                f"ATORCH {values[3]:.3f} V / {values[4]:.3f} A"
            )
            if time.monotonic() <= deadline and consecutive >= 3:
                print(
                    f"  Settled after {time.monotonic() - started:.1f} s "
                    f"({values[0]:.3f} V -> {values[4]:.3f} A)",
                    flush=True,
                )
                source.debug(
                    "settled",
                    waited_s=time.monotonic() - started,
                    set_v=voltage,
                    set_a=current,
                )
                return
        # Communication errors and source protection trips are never retried.
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise OperatingPointError(
        f"Did not settle at {voltage:g} V / {current:g} A "
        f"within {args.settle_timeout:g} s. {last_status}"
    )


def measure_point(
    source,
    load,
    voltage,
    current,
    seconds,
    voltage_tolerance=0.05,
    current_tolerance=0.05,
):
    source.debug(
        "phase", phase="measurement", set_v=voltage, set_a=current, window_s=seconds
    )

    def sample():
        return sample_pair(
            source, load, voltage, current, voltage_tolerance, current_tolerance
        )

    start, previous = sample()
    deadline = start + seconds
    timestamp = start
    totals = [0.0] * len(previous)
    samples = 1
    while timestamp < deadline:
        next_sample = min(timestamp + 0.5, deadline)
        time.sleep(max(0, next_sample - time.monotonic()))
        following_time, following = sample()
        # Both channels use the same interval. Interpolate the last pair to its
        # exact boundary instead of including time after the requested window.
        interval = min(following_time, deadline) - timestamp
        fraction = interval / (following_time - timestamp)
        for index, (left, right) in enumerate(zip(previous, following)):
            totals[index] += interval * (left + (right - left) * fraction / 2)
        timestamp, previous = following_time, following
        samples += 1

    if not load.read_enabled():
        raise RuntimeError("ATORCH load switched off during measurement")
    source_v, source_a, source_w, load_v, load_a, load_w = (
        total / seconds for total in totals
    )
    if source_w <= 0 or load_w <= 0:
        raise RuntimeError(
            f"Missing power at {voltage:g} V / {current:g} A: {source_w:g} W / {load_w:g} W"
        )
    return Point(
        voltage,
        current,
        seconds,
        samples,
        source_v,
        source_a,
        source_w,
        totals[2] / 3600,
        load_v,
        load_a,
        load_w,
        totals[5] / 3600,
        100 * totals[5] / totals[2],
    )


def shutdown(source, load, settings=None, *, keep_source_on=False):
    problems = []
    source_off = False
    debug = source.debug if source is not None else no_debug
    debug("shutdown_start", restoring_settings=settings is not None and not keep_source_on,
          keep_source_on=keep_source_on)
    if keep_source_on:
        debug("rk_shutdown_skipped", reason="keep_source_on")
        print("RK6006: OFF and settings restoration skipped (--keep-source-on).", flush=True)
    for label, instrument in (("RK6006", source), ("ATORCH", load)):
        if instrument is None or (instrument is source and keep_source_on):
            continue
        try:
            instrument.set_enabled(False)
            if instrument is source:
                source_off = True
        except (Exception, KeyboardInterrupt) as error:
            problems.append(f"{label} OFF could not be verified: {error}")
            debug("shutdown_error", instrument=label, error=str(error))
    if settings is not None and not keep_source_on:
        if source_off:
            for address, value in settings.items():
                try:
                    debug("rk_restore", address=address, value=value)
                    source.write_verified(address, value)
                except (Exception, KeyboardInterrupt) as error:
                    problems.append(
                        f"RK6006 register {address} could not be restored: {error}"
                    )
        else:
            problems.append("RK6006 settings not restored because OFF was not verified")
    if problems:
        debug("shutdown_end", success=False, problems=problems)
        raise RuntimeError("; ".join(problems))
    debug("shutdown_end", success=True)


def format_duration(seconds):
    seconds = max(0, math.ceil(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def format_progress(completed, total, elapsed, initial_point_s):
    remaining = total - completed
    point_s = elapsed / completed if completed else initial_point_s
    eta = remaining * point_s
    return (
        f"{completed}/{total} done, {remaining} left | elapsed {format_duration(elapsed)} "
        f"| ETA ~{format_duration(eta)}"
    )


def sweep(source, load, args, record):
    source.configure_limits(args)
    total = len(args.voltages) * len(args.currents)
    completed = 0
    source_enabled = False
    load_enabled = False
    started = time.monotonic()

    def progress():
        # Initially allow for settling checks and serial/control overhead.
        # Afterwards use actual throughput, including voltage waits and retries.
        return format_progress(
            completed, total, time.monotonic() - started,
            args.seconds + args.settle + 2
            + (args.voltage_startup_seconds
               + (0 if args.keep_source_on else args.voltage_shutdown_seconds)) / len(args.currents),
        )

    for voltage in args.voltages:
        if not args.keep_source_on or args.voltage_startup_seconds > 0:
            load.set_enabled(False)
            load_enabled = False
        if not args.keep_source_on:
            source.set_enabled(False)
            source_enabled = False
            source.debug("voltage_shutdown_wait", set_v=voltage, seconds=args.voltage_shutdown_seconds)
            print(f"Source OFF for {args.voltage_shutdown_seconds:g} s before setting {voltage:g} V", flush=True)
            time.sleep(args.voltage_shutdown_seconds)
        source.set_voltage(voltage)
        if args.voltage_startup_seconds > 0:
            if not source_enabled:
                source.set_enabled(True)
                source_enabled = True
            startup_started = time.monotonic()
            source.debug("voltage_startup_start", set_v=voltage,
                         source_current_limit_a=args.source_current_limit,
                         load_enabled=False, seconds=args.voltage_startup_seconds)
            print(
                f"Voltage startup: {voltage:g} V, source CC limit {args.source_current_limit:g} A, load OFF "
                f"for {args.voltage_startup_seconds:g} s (excluded from measurement)", flush=True,
            )
            deadline = startup_started + args.voltage_startup_seconds
            while time.monotonic() < deadline:
                # Allow temporary CC during boot, but abort on protection trips
                # or communication errors without restarting the startup phase.
                try:
                    source.read()
                except OperatingPointError as error:
                    source.debug("voltage_startup_unsettled", error=str(error))
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            source.debug("voltage_startup_complete", set_v=voltage,
                         waited_s=time.monotonic() - startup_started)
        for current in args.currents:
            print(
                f"Measuring flow: source {voltage:g} V -> load {current:g} A ... | {progress()}",
                flush=True,
            )
            for attempt in range(1, args.point_retries + 2):
                source.debug(
                    "point_start", set_v=voltage, set_a=current, attempt=attempt,
                    point_index=completed + 1, total_points=total,
                )
                try:
                    load.set_current(current)
                    if not source_enabled:
                        source.set_enabled(True)
                        source_enabled = True
                    if not load_enabled:
                        load.set_enabled(True)
                        load_enabled = True
                    wait_for_settle(source, load, voltage, current, args)
                    point = measure_point(
                        source,
                        load,
                        voltage,
                        current,
                        args.seconds,
                        args.voltage_tolerance,
                        args.current_tolerance,
                    )
                except OperatingPointError as error:
                    source.debug(
                        "point_rejected",
                        set_v=voltage,
                        set_a=current,
                        attempt=attempt,
                        error=str(error),
                    )
                    if attempt > args.point_retries:
                        raise
                    # Keep the DUT powered while starting a fresh settled
                    # window. Never splice around an anomalous sample.
                    print(f"  Point rejected: {error}", flush=True)
                    print(
                        f"  Repeating complete point (retry {attempt}/{args.point_retries}) ... "
                        f"| {progress()}",
                        flush=True,
                    )
                    continue
                point = replace(point, attempts=attempt)
                record(point)
                completed += 1
                source.debug(
                    "point_complete", **asdict(point), completed_points=completed,
                    total_points=total, elapsed_sweep_s=time.monotonic() - started,
                )
                print(
                    f"  {point.source_w:.3f} W -> {point.load_w:.3f} W ({point.efficiency_pct:.2f}%) "
                    f"| {progress()}",
                    flush=True,
                )
                break


def chart(points):
    if not points:
        return "<p>No completed measurement points.</p>"
    voltages = sorted({point.set_v for point in points})
    currents = sorted({point.set_a for point in points})
    low = math.floor(min(100, *(p.efficiency_pct for p in points)) - 0.5)
    high = math.ceil(max(100, *(p.efficiency_pct for p in points)) + 0.5)
    left, right, top, bottom = 76, 838, 26, 326

    def x(current):
        if len(currents) == 1:
            return (left + right) / 2
        return left + (right - left) * (current - currents[0]) / (
            currents[-1] - currents[0]
        )

    def y(efficiency):
        return bottom - (bottom - top) * (efficiency - low) / (high - low)

    svg = [
        '<svg viewBox="0 0 864 390" role="img" aria-label="Efficiency versus load current">'
    ]
    for tick in range(6):
        value = low + (high - low) * tick / 5
        pos = y(value)
        svg.append(
            f'<line x1="{left}" x2="{right}" y1="{pos}" y2="{pos}" stroke="#e2e8f0"/>'
        )
        svg.append(
            f'<text x="{left - 10}" y="{pos + 4}" text-anchor="end">{value:g}%</text>'
        )
    svg.append(
        f'<line x1="{left}" x2="{right}" y1="{y(100)}" y2="{y(100)}" '
        'stroke="#64748b" stroke-dasharray="5 5"><title>100%</title></line>'
    )
    for current in currents:
        svg.append(
            f'<text x="{x(current)}" y="350" text-anchor="middle">{current:g}</text>'
        )
    svg.append('<text x="457" y="379" text-anchor="middle">Load setpoint (A)</text>')

    legend = []
    colors = ("#12604b", "#325dc4", "#8f3bc1", "#b34720", "#af215e")
    for voltage, color in zip(voltages, colors):
        series = sorted(
            (p for p in points if p.set_v == voltage), key=lambda p: p.set_a
        )
        coords = " ".join(f"{x(p.set_a):.2f},{y(p.efficiency_pct):.2f}" for p in series)
        legend.append(f'<span style="--series:{color}">{voltage:g} V</span>')
        svg.append(
            f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2.5"/>'
        )
        for p in series:
            svg.append(
                f'<circle cx="{x(p.set_a):.2f}" cy="{y(p.efficiency_pct):.2f}" r="4.5" '
                f'fill="{color}"><title>{voltage:g} V, {p.set_a:g} A: '
                f"{p.efficiency_pct:.2f}%</title></circle>"
            )
    return "".join(svg) + '</svg><div class="legend">' + "".join(legend) + "</div>"


def write_html(path, points, args, started, error):
    rows = []
    for p in points:
        values = (
            f"{p.set_v:g}",
            f"{p.set_a:g}",
            f"{p.source_v:.3f}",
            f"{p.source_a:.3f}",
            f"{p.source_w:.4f}",
            f"{p.source_wh:.6f}",
            f"{p.load_v:.3f}",
            f"{p.load_a:.3f}",
            f"{p.load_w:.4f}",
            f"{p.load_wh:.6f}",
            f"{p.efficiency_pct:.2f}",
            str(p.attempts),
        )
        rows.append("<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>")
    csv_link = '<a href="results.csv">Download CSV</a>'
    if (path.parent / "debug.jsonl").exists():
        csv_link += ' | <a href="debug.jsonl">Debug log (JSONL)</a>'
    status = f"<p class='error'>Run stopped. {escape(error)}</p>" if error else ""
    path.write_text(
        f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RK6006 to ATORCH | Efficiency</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0 auto; max-width: 1120px; padding: 36px 20px; background: #f4f7fa;
        color: #162437; font: 15px/1.5 Inter, system-ui, sans-serif; }}
h1 {{ margin: 0; font-size: clamp(24px, 4vw, 38px); letter-spacing: -.03em; }}
h2 {{ margin-top: 0; font-size: 18px; }}
p {{ color: #42536a; }} a {{ color: #154e95; }}
section {{ background: white; border: 1px solid #dce4ed; border-radius: 12px;
           padding: 20px; margin: 18px 0; }}
svg {{ display: block; width: 100%; }} svg text {{ fill: #42536a; font-size: 14px; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 8px 20px; justify-content: center; }}
.legend span::before {{ content: ''; display: inline-block; width: 18px; height: 3px;
                        margin: 0 6px 4px 0; background: var(--series); }}
.table {{ overflow-x: auto; }} table {{ width: 100%; border-collapse: collapse; white-space: nowrap;
                                      font-variant-numeric: tabular-nums; }}
th, td {{ text-align: right; padding: 8px; border-bottom: 1px solid #e2e8f0; }}
th {{ background: #f1f5f9; font-size: 13px; }} .error {{ color: #a52424; }}
</style>
<main>
<h1>Power transfer efficiency</h1>
<p>RK6006 ({escape(args.source_port)}) → ATORCH ({escape(args.load_port)}) ·
{escape(started.isoformat(timespec='seconds'))} · {len(points)}/{len(args.voltages) * len(args.currents)} points ·
{args.seconds:g} s per point</p>
{status}
<section><h2>Efficiency by voltage and load current</h2>{chart(points)}</section>
<section><h2>Measured averages and integrated energy</h2><p>{csv_link}</p>
<div class="table"><table><thead>
<tr><th colspan="2" scope="colgroup">Setpoints</th><th colspan="4" scope="colgroup">Source</th>
<th colspan="4" scope="colgroup">Load</th><th scope="col">Efficiency</th><th scope="col">Attempts</th></tr>
<tr><th>V</th><th>A</th><th>V</th><th>A</th><th>W</th><th>Wh</th>
<th>V</th><th>A</th><th>W</th><th>Wh</th><th>%</th><th>#</th></tr>
</thead><tbody>{''.join(rows)}</tbody></table></div></section>
<p>Attempts greater than 1 mark a repeated point. Rejected windows remain in debug.jsonl;
only the final complete measurement window is included in CSV and this table.</p>
<p>Efficiency = load Wh / source Wh × 100. Each pair of voltage and current readings is
multiplied before integration over the shared window. With the instruments directly connected,
the result includes the cable and contacts between their measurement points. Meter errors
can move the calculated ratio above 100%.</p>
</main></html>
""",
        encoding="utf-8",
    )


def create_results_folder(args, started):
    folder = args.output_dir or (
        TOOLS / "results" / started.strftime("rk6006-atorch-%Y%m%d-%H%M%S-%f")
    )
    folder.mkdir(parents=True, exist_ok=False)
    print(f"Results: {folder}", flush=True)
    return folder


def run_report(source, load, args, folder=None, started=None):
    started = started or datetime.now().astimezone()
    folder = folder if folder is not None else create_results_folder(args, started)
    points = []
    error = ""
    with (folder / "results.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow([field.name for field in fields(Point)])
        output.flush()

        def record(point):
            points.append(point)
            writer.writerow(
                [f"{getattr(point, field.name):.10g}" for field in fields(Point)]
            )
            output.flush()

        try:
            sweep(source, load, args, record)
        except (Exception, KeyboardInterrupt) as exc:
            error = str(exc) or type(exc).__name__
            source.debug(
                "measurement_error", error_type=type(exc).__name__, error=error
            )
            raise
        finally:
            write_html(folder / "results.html", points, args, started, error)


def run(args):
    import serial

    sys.path.insert(0, str(TOOLS / "RK6006-Python-Module"))
    from RK6006_module import RK6006

    with ExitStack() as stack:
        debug = no_debug
        folder = None
        started = datetime.now().astimezone()
        if not args.probe:
            folder = create_results_folder(args, started)
            output = stack.enter_context(
                (folder / "debug.jsonl").open("w", encoding="utf-8")
            )
            debug = stack.enter_context(DebugLog(output))
            debug(
                "session_start",
                source_port=args.source_port,
                load_port=args.load_port,
                voltages=args.voltages,
                currents=args.currents,
                source_current_limit=args.source_current_limit,
                voltage_startup_seconds=args.voltage_startup_seconds,
                debug_serial=args.debug_serial,
                keep_source_on=args.keep_source_on,
                voltage_shutdown_seconds=args.voltage_shutdown_seconds,
                seconds=args.seconds,
                settle=args.settle,
                settle_timeout=args.settle_timeout,
                point_retries=args.point_retries,
                voltage_tolerance=args.voltage_tolerance,
                current_tolerance=args.current_tolerance,
                ovp=args.ovp,
                ocp=args.ocp,
            )
        debug("source_connect", port=args.source_port)
        # Own the transport before identification so even the very first
        # Modbus exchange is traced, and a failed constructor closes its port.
        source_port = serial.Serial(args.source_port, 115200, timeout=0.5, write_timeout=0.5)
        stack.callback(source_port.close)
        if args.debug_serial:
            source_port = SerialTrace(source_port, "RK6006", debug)
        try:
            module = RK6006(source_port)
        except SystemExit as error:
            raise RuntimeError(f"Expected an RK6006 on {args.source_port}") from error
        except Exception as error:
            raise RuntimeError(
                f"Could not identify RK6006 on {args.source_port} "
                f"(115200 baud, Modbus address 1): {error}"
            ) from error
        source = RKSource(module, debug)
        load = None
        settings = None
        try:
            if not args.probe and not args.keep_source_on:
                # The source is already identified: turn it off before opening
                # or probing the load, which may fail or take several seconds.
                source.set_enabled(False)
            input_voltage = module.get_input_voltage()
            debug(
                "source_connected",
                port=args.source_port,
                input_v=input_voltage,
                max_set_v=module.max_set_voltage,
            )

            debug("load_connect", port=args.load_port)
            port = stack.enter_context(
                serial.Serial(args.load_port, 9600, timeout=0.05, write_timeout=0.5)
            )
            if args.debug_serial:
                port = SerialTrace(port, "ATORCH", debug)
            candidate = AtorchLoad(port, debug)
            candidate.receive(ATORCH_REPORT, 4)
            debug("load_identified", port=args.load_port)
            load = candidate  # Only control a load after a valid identifying report.
            if not args.probe:
                load.set_enabled(False)
            enabled = load.read_enabled()
            measured_v, measured_a = load.read()
            set_a = load.query(0x17) / 100
            cutoff = load.query(0x18) / 100
            settings = source.snapshot_settings() if not args.probe else None
            probe_settings = (
                settings if settings is not None else source.snapshot_settings()
            )
            print(
                f"{module} on {args.source_port}, input {input_voltage:.2f} V, "
                f"max setpoint {module.max_set_voltage:.2f} V, "
                f"OVP {probe_settings[82] / source.volts_resolution:g} V, "
                f"OCP {probe_settings[83] / source.amps_resolution:g} A",
                flush=True,
            )
            print(
                f"ATORCH on {args.load_port}, load {'ON' if enabled else 'OFF'}, "
                f"{measured_v:.3f} V / {measured_a:.3f} A, set {set_a:.2f} A, cutoff {cutoff:.2f} V",
                flush=True,
            )
            if args.probe:
                return
            if min(args.voltages) <= cutoff:
                raise RuntimeError(
                    f"ATORCH cutoff {cutoff:g} V must be below the lowest test voltage"
                )
            run_report(source, load, args, folder, started)
        finally:
            if not args.probe:
                failed = sys.exc_info()[0] is not None
                try:
                    shutdown(source, load, settings, keep_source_on=args.keep_source_on)
                except RuntimeError as error:
                    if not failed:
                        raise
                    # Preserve the measurement/init error, but also report any
                    # failure to turn off or restore the equipment.
                    print(f"Check the equipment manually. {error}", file=sys.stderr)


def parse_setpoint(value, decimals=2):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("setpoint must be finite and greater than zero")
    if number != round(number, decimals):
        raise argparse.ArgumentTypeError(f"setpoint must use a {10 ** -decimals:g} step")
    return number


def parse_source_current(value):
    return parse_setpoint(value, decimals=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-port", help="RK6006 port (required for measurement and probe; no default)"
    )
    parser.add_argument(
        "--load-port", help="ATORCH port (required for measurement and probe; no default)"
    )
    parser.add_argument(
        "--list-ports", action="store_true",
        help="list available serial ports without opening them, then exit",
    )
    parser.add_argument(
        "--voltages", type=parse_setpoint, nargs="+",
        help="positive supply voltages in V, step 0.01 (required for measurement; no default)",
    )
    parser.add_argument(
        "--currents", type=parse_setpoint, nargs="+",
        help="positive load currents in A, step 0.01 (required for measurement; no default)",
    )
    parser.add_argument(
        "--source-current-limit", type=parse_source_current,
        help="RK6006 measurement current limit in A, step 0.001 (required for measurement; no default)",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=10,
        help="measurement window per point (default 10 s)",
    )
    parser.add_argument(
        "--voltage-startup-seconds", type=float, default=0,
        help="source ON duration with load OFF at the start of every voltage cycle (default 0 s; excluded from measurement)",
    )
    parser.add_argument(
        "--debug-serial", action="store_true",
        help="print hex TX/RX for both serial ports, including initial identification",
    )
    parser.add_argument(
        "--keep-source-on", action="store_true",
        help="never send RK6006 OFF, including on errors/Ctrl+C; leave source settings in place",
    )
    parser.add_argument(
        "--voltage-shutdown-seconds", type=float, default=2,
        help="source OFF duration before every voltage cycle (default 2 s; ignored with --keep-source-on)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=2,
        help="minimum wait before checking settling (default 2 s)",
    )
    parser.add_argument(
        "--settle-timeout",
        type=float,
        default=15,
        help="total settling timeout, including minimum wait (default 15 s)",
    )
    parser.add_argument(
        "--point-retries",
        type=int,
        default=1,
        help="repeat complete point after an operating-point error (default 1; 0 disables)",
    )
    parser.add_argument(
        "--ovp",
        type=float,
        help="temporary RK6006 OVP in V (default 110%% of highest voltage)",
    )
    parser.add_argument(
        "--ocp",
        type=float,
        help="temporary RK6006 OCP in A (default 110%% of source current limit)",
    )
    parser.add_argument(
        "--voltage-tolerance",
        type=float,
        default=0.05,
        help="allowed source voltage relative error (default 0.05; minimum allowance 0.1 V)",
    )
    parser.add_argument(
        "--current-tolerance",
        type=float,
        default=0.05,
        help="allowed load current relative error (default 0.05; minimum allowance 0.01 A)",
    )
    parser.add_argument(
        "--output-dir", type=Path, help="new directory for CSV and HTML"
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="identify both devices and query readings without changing settings",
    )
    args = parser.parse_args()
    if args.list_ports:
        from serial.tools.list_ports import comports

        ports = sorted(comports(), key=lambda port: port.device)
        if not ports:
            print("No serial ports found.")
        for port in ports:
            print(f"{port.device}: {port.description}")
        return 0
    if not args.source_port or not args.load_port:
        parser.error("specify --source-port and --load-port explicitly; use --list-ports to see available ports")
    if not args.probe and (args.voltages is None or args.currents is None):
        parser.error("measurement requires explicit --voltages and --currents")
    if not args.probe and args.source_current_limit is None:
        parser.error("measurement requires explicit --source-current-limit; choose it for the DUT input current")
    if not math.isfinite(args.voltage_startup_seconds) or args.voltage_startup_seconds < 0:
        parser.error("--voltage-startup-seconds must be finite and nonnegative")
    if args.probe and args.voltage_startup_seconds > 0:
        parser.error("--voltage-startup-seconds cannot be combined with --probe")
    if args.source_port.upper() == args.load_port.upper():
        parser.error("source and load must use different ports")
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and greater than zero")
    if not math.isfinite(args.voltage_shutdown_seconds) or args.voltage_shutdown_seconds <= 0:
        parser.error("--voltage-shutdown-seconds must be finite and greater than zero")
    if not math.isfinite(args.settle) or args.settle < 0:
        parser.error("--settle must be finite and nonnegative")
    if not math.isfinite(args.settle_timeout) or args.settle_timeout <= args.settle:
        parser.error("--settle-timeout must be finite and greater than --settle")
    if args.point_retries < 0:
        parser.error("--point-retries must be nonnegative")
    for name in ("voltages", "currents"):
        values = getattr(args, name)
        if values is not None and len(set(values)) != len(values):
            parser.error("voltage and current setpoints must be unique")
    for name in ("voltage_tolerance", "current_tolerance"):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0 <= value < 1:
            parser.error(
                f"--{name.replace('_', '-')} must be finite and in the range [0, 1)"
            )
    if not args.probe:
        try:
            planned_limits(args)
        except ValueError as error:
            parser.error(str(error))

    try:
        run(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
