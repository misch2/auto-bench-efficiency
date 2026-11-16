"""Behavioral checks with simulated registers, serial traffic and time; no hardware."""

import csv
import importlib.util
import json
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import measure_efficiency as tool


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def report():
    payload = bytes((1, 2)) + bytes(31)
    return b"\xff\x55" + payload + bytes(((sum(payload) & 0xff) ^ 0x44,))


def reply(value):
    return tool.PX100_REPLY + value.to_bytes(3, "big") + b"\xce\xcf"


def arguments(**updates):
    values = dict(source_port="FAKE_SOURCE", load_port="FAKE_LOAD", probe=False,
                  voltages=(5, 9, 12, 15, 20),
                  currents=(0.1, 0.2, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0), seconds=1,
                  settle=0, settle_timeout=15, point_retries=1, output_dir=None, ovp=None, ocp=None,
                  startup_voltage=None, startup_current=None, startup_seconds=None, debug_serial=False,
                  source_current_limit=2.1,
                  keep_source_on=False,
                  voltage_tolerance=0.05, current_tolerance=0.05)
    values.update(updates)
    return SimpleNamespace(**values)


def command_line(options):
    return ["tool", "--source-port", "FAKE_SOURCE", "--load-port", "FAKE_LOAD",
            "--source-current-limit", "2.1"] + options


class Rig:
    def __init__(self, clock):
        self.clock = clock
        self.regs = {8: 500, 9: 100, 82: 550, 83: 150, 18: 1}
        self.original = dict(self.regs)
        self.load_on = True
        self.load_current = 10  # centiamps
        self.writes = []
        self.commands = []
        self.closed = []
        self.suppress_replies = False
        self.wrong_current = False
        self.current_query_override = None
        self.mode = 0
        self.fail_off = False
        self.fail_restore = None
        self.fail_input = False
        self.fail_load_open = False
        self.port = FakePort(self)
        self.instrument = FakeInstrument(self)
        self.module = SimpleNamespace(instrument=self.instrument, volts_resolution=100,
                                      amps_resolution=1000, max_set_current=6,
                                      max_ocp_current=6.2, max_set_voltage=25,
                                      get_input_voltage=self.input_voltage)

    def input_voltage(self):
        if self.fail_input:
            raise TimeoutError("input read failed")
        return 30

    def open_load(self, *args, **kwargs):
        if self.fail_load_open:
            raise OSError("load port unavailable")
        return self.port

    def open_serial(self, port, baudrate, **kwargs):
        if baudrate == 115200:
            return self.instrument.serial
        return self.open_load(port, baudrate, **kwargs)


class FakeInstrument:
    def __init__(self, rig):
        self.rig = rig
        self.serial = SimpleNamespace(port="FAKE_SOURCE", baudrate=115200,
                                      write_timeout=None, close=lambda: rig.closed.append("source"))

    def write_register(self, address, value):
        r = self.rig
        r.writes.append((address, value))
        if address == 18 and value == 0 and r.fail_off:
            raise TimeoutError("source OFF failed")
        if address == r.fail_restore and value == r.original[address]:
            raise TimeoutError("restore failed")
        r.regs[address] = value

    def read_register(self, address):
        return self.rig.regs[address]

    def read_registers(self, address, count):
        r = self.rig
        if (address, count) == (10, 9):
            voltage = r.regs[8] if r.regs[18] else 0
            current = r.load_current * 10 if r.load_on else 0
            protection = 1 if voltage > r.regs[82] else 2 if current > r.regs[83] else 0
            if protection:
                r.regs[18] = 0
            return [voltage, current, 0, 0, 3000, 0, protection, r.mode, r.regs[18]]
        return [r.regs[index] for index in range(address, address + count)]


class FakePort:
    port = "FAKE_LOAD"
    baudrate = 9600

    def __init__(self, rig):
        self.rig = rig
        self.rx = bytearray(report())
        self.chunk_size = 256

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.rig.closed.append("load")

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, count):
        count = min(count, self.chunk_size)
        data = bytes(self.rx[:count])
        del self.rx[:count]
        if not data:
            self.rig.clock.sleep(0.05)
        return data

    def write(self, packet):
        r = self.rig
        command, first, second = packet[2:5]
        r.commands.append(command)
        if command == 1:
            r.load_on = bool(first)
        elif command == 2:
            r.load_current = first * 100 + second
        if r.suppress_replies:
            return len(packet)
        if command < 0x10:
            self.rx.extend(tool.PX100_ACK)
        else:
            values = {0x10: int(r.load_on), 0x11: r.regs[8] * 10 - 100,
                      0x12: 100 if r.wrong_current else r.load_current * 10,
                      0x17: r.load_current, 0x18: 0}
            if r.current_query_override is not None:
                values[0x12] = r.current_query_override
            self.rx.extend(reply(values[command]))
        return len(packet)


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.rig = Rig(self.clock)
        self.source = tool.RKSource(self.rig.module)
        self.load = tool.AtorchLoad(self.rig.port)
        self.out = StringIO()
        for context in (patch.object(tool, "time", self.clock), redirect_stdout(self.out),
                        redirect_stderr(self.out), patch.object(sys, "path", sys.path[:])):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def configured(self):
        self.source.configure_limits(arguments())
        self.source.set_enabled(True)
        self.load.receive(tool.ATORCH_REPORT, 4)

    def run_rig(self, args):
        if not args.probe and args.output_dir is None:
            temporary = TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            args.output_dir = Path(temporary.name) / "results"
        with patch.dict(sys.modules, {
            "serial": SimpleNamespace(Serial=self.rig.open_serial),
            "RK6006_module": SimpleNamespace(RK6006=lambda port: self.rig.module),
        }):
            tool.run(args)

    def assert_restored(self):
        self.assertEqual({key: self.rig.regs[key] for key in (8, 9, 82, 83)},
                         {key: self.rig.original[key] for key in (8, 9, 82, 83)})
        self.assertEqual(self.rig.regs[18], 0)
        self.assertFalse(self.rig.load_on)

    def assert_continuously_powered(self, events):
        enabled = next(i for i, e in enumerate(events)
                       if e["event"] == "rk_write" and e["address"] == 18 and e["value"] == 1)
        last_point = max(i for i, e in enumerate(events) if e["event"] == "point_complete")
        active = events[enabled:last_point + 1]
        self.assertFalse(any(e["event"] == "shutdown_start" for e in active))
        self.assertEqual([e["value"] for e in active
                          if e["event"] == "rk_write" and e["address"] == 18], [1])
        self.assertTrue(all(e["value"] > 0 for e in active
                            if e["event"] == "rk_write" and e["address"] == 8))
        load_switches = [bytes.fromhex(e["data_hex"])[3] for e in active
                         if e["event"] == "atorch_tx" and e["command"] == "0x01"]
        self.assertEqual(load_switches, [1])

    def test_explicit_source_limit_default_protections_and_restoration(self):
        self.assertEqual(tool.planned_limits(arguments()), (2.1, 22, 2.31))
        settings = self.source.snapshot_settings()
        self.source.configure_limits(arguments())
        self.assertEqual((self.rig.regs[82], self.rig.regs[83], self.rig.regs[9]), (2200, 2310, 2100))
        self.source.set_voltage(20)
        tool.shutdown(self.source, self.load, settings)
        self.assert_restored()

    def test_progress_initial_estimate_and_time_format(self):
        self.assertEqual(tool.format_duration(0), "0s")
        self.assertEqual(tool.format_duration(60), "1m 00s")
        self.assertEqual(tool.format_duration(3661), "1h 01m 01s")
        self.assertIn("0/40 done, 40 left", tool.format_progress(0, 40, 0, 14))
        self.assertIn("ETA ~9m 20s", tool.format_progress(0, 40, 0, 14))

    def test_default_output_is_inside_standalone_project(self):
        with TemporaryDirectory() as temporary:
            project = Path(temporary) / "bench-efficiency"
            with patch.object(tool, "TOOLS", project):
                folder = tool.create_results_folder(arguments(), tool.datetime(2026, 11, 15, 12, 0, 0))
            self.assertEqual(folder.parent, project / "results")
            self.assertTrue(folder.is_dir())

    def test_progress_estimate_uses_elapsed_time_and_finishes_at_zero(self):
        self.assertIn("elapsed 20s | ETA ~30s", tool.format_progress(2, 5, 20, 14))
        self.assertIn("elapsed 40s | ETA ~1m 00s", tool.format_progress(2, 5, 40, 14))
        self.assertIn("5/5 done, 0 left", tool.format_progress(5, 5, 50, 14))
        self.assertIn("ETA ~0s", tool.format_progress(5, 5, 50, 14))

    def test_explicit_limits(self):
        self.assertEqual(tool.planned_limits(arguments(ovp=23, ocp=2.5)), (2.1, 23, 2.5))

    def test_startup_contributes_to_limits_and_range_checks(self):
        args = arguments(voltages=(3.3, 5), currents=(0.1, 0.2),
                         source_current_limit=0.3,
                         startup_voltage=12, startup_current=0.5, startup_seconds=60)
        self.assertEqual(tool.planned_limits(args), (0.3, 13.2, 0.55))
        for changes in ({"startup_voltage": 26}, {"startup_current": 6.1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.source.configure_limits(SimpleNamespace(**(vars(args) | changes)))
        self.assertEqual(self.rig.writes, [])

    def test_startup_once_precedes_sweep_and_is_excluded_from_energy(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / "results"
            self.run_rig(arguments(output_dir=folder, voltages=(3.3, 5), currents=(0.1, 0.2),
                                   source_current_limit=0.3,
                                   seconds=15, startup_voltage=12, startup_current=0.5, startup_seconds=60))
            events = [json.loads(line) for line in (folder / "debug.jsonl").read_text(encoding="utf-8").splitlines()]
            with (folder / "results.csv").open(newline="", encoding="utf-8") as output:
                points = list(csv.DictReader(output))
        starts = [e for e in events if e["event"] == "startup_start"]
        ends = [e for e in events if e["event"] == "startup_complete"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertEqual((starts[0]["set_v"], starts[0]["source_current_limit_a"]), (12, 0.5))
        self.assertFalse(starts[0]["load_enabled"])
        self.assertAlmostEqual(ends[0]["elapsed_s"] - starts[0]["elapsed_s"], 60)
        self.assertTrue(all(e["elapsed_s"] >= ends[0]["elapsed_s"] for e in events if e["event"] == "point_start"))
        boot_reads = [e["registers"] for e in events if e["event"] == "rk_read"
                      and starts[0]["elapsed_s"] <= e["elapsed_s"] < ends[0]["elapsed_s"]]
        self.assertTrue(boot_reads)
        self.assertTrue(all(r[0] == 1200 and r[1] == 0 and r[8] == 1 for r in boot_reads))
        boot_events = [e for e in events if e["elapsed_s"] < ends[0]["elapsed_s"]]
        self.assertEqual([e["value"] for e in boot_events
                          if e["event"] == "rk_write" and e["address"] == 9], [500])
        self.assertFalse(any(e["event"] == "atorch_tx" and e["command"] == "0x02" for e in boot_events))
        self.assertTrue(all(bytes.fromhex(e["data_hex"])[3] == 0 for e in boot_events
                            if e["event"] == "atorch_tx" and e["command"] == "0x01"))
        measurement_events = events[events.index(ends[0]) + 1:]
        self.assertEqual(next(e["value"] for e in measurement_events
                              if e["event"] == "rk_write" and e["address"] == 9), 300)
        self.assertEqual(len(points), 4)
        for point in points:
            self.assertEqual(float(point["window_s"]), 15)
            self.assertAlmostEqual(float(point["source_wh"]), float(point["set_v"]) * float(point["set_a"]) * 15 / 3600)
        self.assert_continuously_powered(events)
        self.assert_restored()

    def test_startup_current_limit_is_exact_and_independent_of_sweep(self):
        observed = []
        real_read = self.rig.instrument.read_registers

        def observe(address, count):
            if (address, count) == (10, 9):
                observed.append((self.rig.regs[8], self.rig.regs[9], self.rig.load_on))
            return real_read(address, count)

        with patch.object(self.rig.instrument, "read_registers", side_effect=observe):
            self.run_rig(arguments(voltages=(5,), currents=(0.5,),
                                   source_current_limit=0.6,
                                   startup_voltage=12, startup_current=0.123, startup_seconds=2))
        boot = [state for state in observed if state[0] == 1200]
        measurement = [state for state in observed if state[0] == 500]
        self.assertTrue(boot)
        self.assertTrue(all(state[1:] == (123, False) for state in boot))
        self.assertTrue(measurement)
        self.assertTrue(all(state[1:] == (600, True) for state in measurement))
        states = [value for address, value in self.rig.writes if address == 18]
        self.assertEqual(states[states.index(1):], [1, 0])
        self.assert_restored()

    def test_load_cutoff_does_not_apply_to_unloaded_startup_voltage(self):
        query = tool.AtorchLoad.query

        def cutoff(load, command):
            return 250 if command == 0x18 else query(load, command)

        with patch.object(tool.AtorchLoad, "query", cutoff):
            self.run_rig(arguments(voltages=(5,), currents=(0.1,),
                                   startup_voltage=1.8, startup_current=0.5, startup_seconds=2))
        self.assert_restored()

    def test_startup_is_not_repeated_after_rejected_measurement(self):
        measure = tool.measure_point
        calls = 0

        def reject_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise tool.OperatingPointError("deviation")
            return measure(*args, **kwargs)

        with TemporaryDirectory() as temporary, patch.object(tool, "measure_point", side_effect=reject_once):
            folder = Path(temporary) / "results"
            self.run_rig(arguments(output_dir=folder, voltages=(5,), currents=(0.1,),
                                   startup_voltage=12, startup_current=0.5, startup_seconds=60))
            events = [json.loads(line) for line in (folder / "debug.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls, 2)
        self.assertEqual(sum(e["event"] == "startup_start" for e in events), 1)
        self.assert_continuously_powered(events)
        self.assert_restored()

    def test_startup_failure_aborts_before_measurement_and_restores(self):
        real_read = self.rig.instrument.read_registers

        def fail_startup(address, count):
            if (address, count) == (10, 9):
                raise TimeoutError("startup communication lost")
            return real_read(address, count)

        with patch.object(self.rig.instrument, "read_registers", side_effect=fail_startup), patch.object(tool, "measure_point") as measure:
            with self.assertRaisesRegex(TimeoutError, "startup communication lost"):
                self.run_rig(arguments(startup_voltage=12, startup_current=0.5, startup_seconds=60))
            measure.assert_not_called()
        self.assert_restored()

    def test_startup_allows_temporary_cc_mode(self):
        real_read = self.rig.instrument.read_registers

        def startup_cc(address, count):
            self.rig.mode = int(self.rig.regs[8] == 1200)
            return real_read(address, count)

        with patch.object(self.rig.instrument, "read_registers", side_effect=startup_cc):
            self.run_rig(arguments(voltages=(5,), currents=(0.1,),
                                   startup_voltage=12, startup_current=0.5, startup_seconds=2))
        self.assert_restored()

    def test_invalid_limits(self):
        for changes in ({"ovp": 20}, {"ocp": 2.1}, {"ovp": float("nan")},
                        {"ocp": float("inf")}, {"ovp": 20.001}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                tool.planned_limits(arguments(**changes))

    def test_source_range_checks_before_writes(self):
        for changes in ({"ovp": 28}, {"ocp": 6.3}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.source.configure_limits(arguments(**changes))
        self.assertEqual(self.rig.writes, [])

    def test_register_readback_failure(self):
        with patch.object(self.rig.instrument, "read_register", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "did not confirm"):
                self.source.set_voltage(5)

    def test_constant_power_integration(self):
        self.configured()
        point = tool.measure_point(self.source, self.load, 5, 0.1, 10)
        self.assertAlmostEqual(point.source_wh, 0.5 * 10 / 3600)
        self.assertAlmostEqual(point.load_wh, 0.49 * 10 / 3600)
        self.assertAlmostEqual(point.efficiency_pct, 98)
        self.assertEqual(point.window_s, 10)

    def test_wrong_voltage_is_rejected(self):
        self.configured()
        with self.assertRaisesRegex(RuntimeError, "Wrong source voltage"):
            tool.measure_point(self.source, self.load, 20, 0.1, 1)

    def test_wrong_current_is_rejected(self):
        self.configured()
        with self.assertRaisesRegex(RuntimeError, "Wrong load current"):
            tool.measure_point(self.source, self.load, 5, 2, 1)

    def test_mid_window_current_error_is_rejected(self):
        self.configured()
        with patch.object(self.load, "read", side_effect=[(4.9, 0.1), (4.9, 0.01)]):
            with self.assertRaisesRegex(RuntimeError, "Wrong load current"):
                tool.measure_point(self.source, self.load, 5, 0.1, 1)

    def test_settling_waits_for_delayed_current_readings(self):
        self.configured()
        readings = [(4.9, 0.086)] * 4 + [(4.9, 0.1)] * 3
        with patch.object(self.load, "read", side_effect=readings) as read:
            tool.wait_for_settle(self.source, self.load, 5, 0.1, arguments(settle=2))
        self.assertEqual(read.call_count, 7)
        self.assertGreaterEqual(self.clock.monotonic(), 5)

    def test_settling_requires_consecutive_acceptable_samples(self):
        self.configured()
        readings = [(4.9, 0.1)] * 2 + [(4.9, 0.086)] + [(4.9, 0.1)] * 3
        with patch.object(self.load, "read", side_effect=readings) as read:
            tool.wait_for_settle(self.source, self.load, 5, 0.1, arguments())
        self.assertEqual(read.call_count, 6)

    def test_settling_readings_are_excluded_from_energy_window(self):
        self.configured()
        readings = iter([(4.9, 0.086)] * 4 + [(4.9, 0.1)] * 50)
        with patch.object(self.load, "read", side_effect=lambda: next(readings)):
            tool.wait_for_settle(self.source, self.load, 5, 0.1, arguments())
            point = tool.measure_point(self.source, self.load, 5, 0.1, 10)
        self.assertAlmostEqual(point.source_wh, 0.5 * 10 / 3600)
        self.assertAlmostEqual(point.load_wh, 0.49 * 10 / 3600)
        self.assertAlmostEqual(point.efficiency_pct, 98)

    def test_settling_timeout_includes_last_reading_error(self):
        self.configured()
        start = self.clock.monotonic()
        with patch.object(self.load, "read", return_value=(4.9, 0.086)):
            with self.assertRaisesRegex(tool.OperatingPointError, "Did not settle.*Wrong load current.*0.086"):
                tool.wait_for_settle(self.source, self.load, 5, 0.1, arguments(settle_timeout=3))
        self.assertAlmostEqual(self.clock.monotonic() - start, 3)

    def test_settling_timeout_reports_invalid_channel_readings_and_targets(self):
        for values, invalid in (
            ((5, 0, 12, 0.1), "source current=0 A"),
            ((5, 0.02, 12, 0), "load current=0 A"),
            ((5, 0.02, 0, 0.1), "load voltage=0 V"),
            ((float("nan"), 0.02, 12, 0.1), "source voltage=nan V"),
        ):
            with self.subTest(invalid=invalid), patch.object(self.source, "read", return_value=values[:2]), patch.object(self.load, "read", return_value=values[2:]):
                with self.assertRaises(tool.OperatingPointError) as stopped:
                    tool.wait_for_settle(self.source, self.load, 5, 0.1, arguments(settle_timeout=1))
            message = str(stopped.exception)
            self.assertIn("Did not settle", message)
            self.assertIn(invalid, message)
            self.assertIn("Must be finite and > 0", message)
            self.assertIn(f"RK6006 {values[0]:.3f} V / {values[1]:.3f} A", message)
            self.assertIn(f"ATORCH {values[2]:.3f} V / {values[3]:.3f} A", message)
            self.assertIn("Requested source 5 V +/- 0.25 V; load 0.1 A +/- 0.01 A", message)

    def test_settling_cc_error_reports_both_instrument_readings(self):
        self.configured()
        self.rig.mode = 1
        with patch.object(self.load, "read", return_value=(12, 0.02)):
            with self.assertRaises(tool.OperatingPointError) as stopped:
                tool.wait_for_settle(self.source, self.load, 5, 0.1, arguments(settle_timeout=1))
        message = str(stopped.exception)
        self.assertIn("not in CV mode (mode=1)", message)
        self.assertIn("RK6006 5.000 V / 0.100 A", message)
        self.assertIn("ATORCH 12.000 V / 0.020 A", message)
        self.assertIn("Requested source 5 V", message)

    def test_settling_allows_temporary_cc_mode(self):
        with patch.object(self.source, "read", side_effect=[tool.OperatingPointError("CC mode")] + [(5, 0.1)] * 3):
            with patch.object(self.load, "read", return_value=(4.9, 0.1)):
                tool.wait_for_settle(self.source, self.load, 5, 0.1, arguments())

    def test_settling_does_not_retry_protection_or_communication_errors(self):
        for error in (RuntimeError("OCP/2"), TimeoutError("No communication")):
            with self.subTest(error=error), patch.object(self.source, "read", side_effect=error) as read:
                with self.assertRaises(type(error)):
                    tool.wait_for_settle(self.source, self.load, 5, 0.1, arguments())
                self.assertEqual(read.call_count, 1)

    def test_nonfinite_measurement_is_rejected(self):
        self.configured()
        with patch.object(self.load, "read", return_value=(float("nan"), 0.1)):
            with self.assertRaisesRegex(RuntimeError, "Invalid or missing"):
                tool.measure_point(self.source, self.load, 5, 0.1, 1)

    def test_cc_mode_is_rejected(self):
        self.configured()
        self.rig.mode = 1
        with self.assertRaisesRegex(RuntimeError, "not in CV"):
            self.source.read()

    def test_protection_is_named(self):
        self.rig.regs[82] = 450
        with self.assertRaisesRegex(RuntimeError, "OVP/1"):
            self.source.read()

    def test_fragmented_report_and_reply(self):
        self.rig.port.chunk_size = 1
        self.assertEqual(self.load.query(0x11), 4900)
        self.assertEqual(self.load.query(0x12), 100)

    def test_current_ack_after_interrupted_report_is_recovered_and_verified(self):
        prefixes = (
            "ff 55 01 02 00 00 49 00 00 00 00 00 0f 00 00 00 00 00 00 00 00 00 00 00 00 17 00",
            "ff 55 01 02 00 00 49 00 00 00",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.rig.port.rx.clear()
                self.rig.load_current = 0
                output = StringIO()
                load = tool.AtorchLoad(self.rig.port, tool.DebugLog(output))
                write = self.rig.port.write

                def interrupted_write(packet):
                    if packet[2] == 2:
                        self.rig.port.rx.extend(bytes.fromhex(prefix))
                    return write(packet)

                started = self.clock.monotonic()
                with patch.object(self.rig.port, "write", side_effect=interrupted_write):
                    load.set_current(0.1)
                self.assertEqual(self.rig.load_current, 10)
                self.assertEqual(self.rig.commands[-2:], [0x02, 0x17])
                self.assertTrue(load.synchronized)
                self.assertLess(self.clock.monotonic() - started, tool.PX100_REPLY_TIMEOUT)
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                recovered = [e for e in events if e["event"] == "atorch_report_interrupted"]
                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0]["recovered_hex"], "6f")
                self.assertEqual(recovered[0]["discarded_hex"], prefix)

    def test_recovered_ack_does_not_bypass_current_readback(self):
        self.rig.port.rx.clear()
        write = self.rig.port.write

        def reject_current(packet):
            if packet[2] == 2:
                self.rig.port.rx.extend(bytes.fromhex("ff 55 01 02 00 00 49 00 00 00"))
            count = write(packet)
            if packet[2] == 2:
                self.rig.load_current = 10  # Device did not accept requested 0.2 A.
            return count

        with patch.object(self.rig.port, "write", side_effect=reject_current):
            with self.assertRaisesRegex(RuntimeError, "did not confirm the 0.2 A setpoint"):
                self.load.set_current(0.2)
        self.assertEqual(self.rig.commands, [0x02, 0x17])

    def test_extra_ack_after_report_recovery_does_not_replace_readback(self):
        self.rig.port.rx.clear()
        self.rig.load_current = 0
        output = StringIO()
        load = tool.AtorchLoad(self.rig.port, tool.DebugLog(output))
        write, read = self.rig.port.write, self.rig.port.read
        data_reply = bytearray()

        def duplicate_ack(packet):
            if packet[2] == 2:
                self.rig.port.rx.extend(bytes.fromhex(
                    "ff 55 01 02 00 00 2b 00 00 00 00 00 0f 00 00 00 00 00 00 00 00 00 00 00 00"))
            count = write(packet)
            if packet[2] == 0x17:
                data_reply.extend(self.rig.port.rx)
                self.rig.port.rx[:] = tool.PX100_ACK
            return count

        def delayed_data(count):
            if not self.rig.port.rx and data_reply:
                self.clock.sleep(0.05)
                self.rig.port.rx.extend(data_reply)
                data_reply.clear()
            return read(count)

        with patch.object(self.rig.port, "write", side_effect=duplicate_ack), patch.object(self.rig.port, "read", side_effect=delayed_data):
            load.set_current(0.1)
        self.assertTrue(load.synchronized)
        self.assertEqual(self.rig.commands, [0x02, 0x17])
        self.assertEqual(self.rig.load_current, 10)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(any(e["event"] == "atorch_report_interrupted" for e in events))
        self.assertTrue(any(e["event"] == "atorch_ack_ignored" and e["phase"] == "waiting_for_data" for e in events))
        self.assertTrue(any(e["event"] == "atorch_query" and e["value"] == 10 for e in events))

    def test_extra_ack_does_not_hide_readback_mismatch(self):
        self.rig.port.rx.clear()
        write = self.rig.port.write

        def reject_current(packet):
            if packet[2] == 0x17:
                self.rig.port.rx.extend(tool.PX100_ACK)
                self.rig.load_current = 10
            return write(packet)

        with patch.object(self.rig.port, "write", side_effect=reject_current):
            with self.assertRaisesRegex(RuntimeError, "did not confirm the 0.2 A setpoint"):
                self.load.set_current(0.2)
        self.assertEqual(self.rig.commands, [0x02, 0x17])

    def test_ack_only_query_still_times_out_and_blocks_late_data(self):
        self.rig.port.rx.clear()
        self.rig.suppress_replies = True
        write = self.rig.port.write

        def ack_only(packet):
            self.rig.port.rx.extend(tool.PX100_ACK)
            return write(packet)

        started = self.clock.monotonic()
        with patch.object(self.rig.port, "write", side_effect=ack_only):
            with self.assertRaises(TimeoutError):
                self.load.query(0x17)
        self.assertLess(self.clock.monotonic() - started, tool.PX100_REPLY_TIMEOUT + 0.06)
        self.assertFalse(self.load.synchronized)
        self.rig.port.rx.extend(reply(10))
        with self.assertRaisesRegex(RuntimeError, "synchronization lost"):
            self.load.query(0x17)
        self.assertEqual(self.rig.commands, [0x17])

    def test_extra_ack_before_request_is_discarded_but_stale_data_is_not(self):
        self.rig.port.rx[:] = tool.PX100_ACK
        self.assertEqual(self.load.query(0x17), 10)
        self.rig.port.rx.extend(tool.PX100_ACK + reply(20))
        with self.assertRaisesRegex(RuntimeError, "stale PX100"):
            self.load.query(0x17)
        self.assertEqual(self.rig.commands, [0x17])

    def test_repeated_acks_do_not_extend_query_deadline(self):
        self.rig.port.rx.clear()
        self.rig.suppress_replies = True

        def repeated_ack(count):
            self.clock.sleep(0.05)
            return tool.PX100_ACK

        started = self.clock.monotonic()
        with patch.object(self.rig.port, "read", side_effect=repeated_ack):
            with self.assertRaises(TimeoutError):
                self.load.query(0x17)
        self.assertLess(self.clock.monotonic() - started, tool.PX100_REPLY_TIMEOUT + 0.06)
        self.assertFalse(self.load.synchronized)

    def test_query_reply_after_interrupted_report_is_recovered(self):
        self.rig.port.rx.clear()
        write = self.rig.port.write

        def interrupted_write(packet):
            self.rig.port.rx.extend(bytes.fromhex("ff 55 01 02 00 00 49 00 00 00"))
            return write(packet)

        with patch.object(self.rig.port, "write", side_effect=interrupted_write):
            self.assertEqual(self.load.query(0x11), 4900)
        self.assertEqual(self.rig.commands, [0x11])

    def test_complete_report_containing_ack_byte_is_not_an_ack(self):
        telemetry = bytearray(report())
        telemetry[12] = 0x6f
        telemetry[-1] = (sum(telemetry[2:-1]) & 0xff) ^ 0x44
        self.rig.port.rx.clear()
        self.rig.port.chunk_size = 1
        self.rig.suppress_replies = True
        write = self.rig.port.write

        def no_ack(packet):
            self.rig.port.rx.extend(telemetry)
            return write(packet)

        with patch.object(self.rig.port, "write", side_effect=no_ack):
            with self.assertRaises(TimeoutError):
                self.load.set_current(0.1)
        self.assertFalse(self.load.synchronized)
        self.assertEqual(self.rig.commands, [0x02])

    def test_incomplete_report_without_reply_still_times_out(self):
        self.rig.port.rx.clear()
        self.rig.suppress_replies = True
        write = self.rig.port.write

        def no_ack(packet):
            self.rig.port.rx.extend(bytes.fromhex("ff 55 01 02 00 00 49 00 00 00"))
            return write(packet)

        with patch.object(self.rig.port, "write", side_effect=no_ack):
            with self.assertRaises(TimeoutError):
                self.load.set_current(0.1)
        self.assertFalse(self.load.synchronized)

    def test_short_gap_inside_fragmented_report_does_not_trigger_recovery(self):
        telemetry = bytearray(report())
        telemetry[12] = 0x6f
        telemetry[-1] = (sum(telemetry[2:-1]) & 0xff) ^ 0x44
        self.rig.port.rx.clear()
        output = StringIO()
        load = tool.AtorchLoad(self.rig.port, tool.DebugLog(output))
        write, read = self.rig.port.write, self.rig.port.read
        tail = bytearray()
        paused = False

        def fragment_reply(packet):
            count = write(packet)
            self.rig.port.rx[:0] = telemetry[:13]
            tail.extend(telemetry[13:] + self.rig.port.rx[13:])
            del self.rig.port.rx[13:]
            return count

        def pause_once(count):
            nonlocal paused
            if not self.rig.port.rx and tail:
                if not paused:
                    paused = True
                    self.clock.sleep(0.05)
                    return b""
                self.rig.port.rx.extend(tail)
                tail.clear()
            return read(count)

        with patch.object(self.rig.port, "write", side_effect=fragment_reply), patch.object(self.rig.port, "read", side_effect=pause_once):
            self.assertEqual(load.request(0x02, 0, 10), tool.PX100_ACK)
        self.assertTrue(paused)
        self.assertFalse(any(json.loads(line)["event"] == "atorch_report_interrupted" for line in output.getvalue().splitlines()))

    def test_stale_partial_reply_prevents_new_request(self):
        self.rig.port.rx.clear()
        self.load.buffer.extend(reply(5000)[:3])
        with self.assertRaisesRegex(RuntimeError, "incomplete stale"):
            self.load.query(0x12)
        self.assertEqual(self.rig.commands, [])
        self.assertFalse(self.load.synchronized)

    def test_stale_complete_reply_prevents_new_request(self):
        self.rig.port.rx.extend(reply(5000))
        with self.assertRaisesRegex(RuntimeError, "stale PX100"):
            self.load.query(0x12)
        self.assertEqual(self.rig.commands, [])

    def test_timeout_blocks_late_reply_and_only_allows_unverified_off(self):
        self.rig.suppress_replies = True
        with self.assertRaises(TimeoutError):
            self.load.query(0x11)
        self.rig.port.rx.extend(reply(5000))
        with self.assertRaisesRegex(RuntimeError, "synchronization lost"):
            self.load.query(0x12)
        with self.assertRaisesRegex(RuntimeError, "synchronization lost"):
            self.load.set_enabled(True)
        with self.assertRaisesRegex(RuntimeError, "without verification"):
            self.load.set_enabled(False)
        self.assertEqual(self.rig.commands, [0x11, 0x01])
        self.assertFalse(self.rig.load_on)

    def test_interrupted_transaction_blocks_more_queries(self):
        self.rig.port.rx.clear()
        with patch.object(self.rig.port, "read", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.load.query(0x11)
        with self.assertRaisesRegex(RuntimeError, "synchronization lost"):
            self.load.query(0x12)

    def test_full_sweep_csv_html_and_restoration(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / "results"
            args = arguments(output_dir=folder)
            total_points = len(args.voltages) * len(args.currents)
            self.run_rig(args)
            with (folder / "results.csv").open(newline="", encoding="utf-8") as output:
                points = list(csv.DictReader(output))
            self.assertEqual(len(points), total_points)
            self.assertEqual({(float(p["set_v"]), float(p["set_a"])) for p in points},
                             {(v, a) for v in args.voltages for a in args.currents})
            for point in points:
                self.assertAlmostEqual(float(point["efficiency_pct"]),
                                       100 * (float(point["set_v"]) - 0.1) / float(point["set_v"]))
            html = (folder / "results.html").read_text(encoding="utf-8")
            self.assertIn(f"{total_points}/{total_points} points", html)
            self.assertIn("<svg", html)
            self.assertIn('href="debug.jsonl"', html)
            self.assertNotIn("Run stopped", html)
            events = [json.loads(line) for line in (folder / "debug.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["event"], "session_start")
            self.assertEqual(events[-1]["event"], "session_end")
            self.assertEqual(events[-1]["status"], "complete")
            self.assertEqual(sum(event["event"] == "point_complete" for event in events), total_points)
            self.assert_continuously_powered(events)
            self.assertIn(f"0/{total_points} done, {total_points} left", self.out.getvalue())
            self.assertIn(f"{total_points}/{total_points} done, 0 left", self.out.getvalue())
            self.assertIn("ETA ~0s", self.out.getvalue())
        self.assert_restored()
        self.assertCountEqual(self.rig.closed, ["source", "load"])

    def test_sweep_preserves_starting_point_and_order_through_voltage_decreases(self):
        measure = tool.measure_point
        observed = []

        def observe(source, load, voltage, current, *args):
            self.assertEqual(self.rig.regs[18], 1)
            self.assertTrue(self.rig.load_on)
            self.assertEqual(self.rig.regs[8], round(voltage * 100))
            self.assertEqual(self.rig.load_current, round(current * 100))
            observed.append((voltage, current))
            return measure(source, load, voltage, current, *args)

        with patch.object(tool, "measure_point", side_effect=observe):
            self.run_rig(arguments(voltages=(12, 5, 9), currents=(0.2, 0.1)))
        self.assertEqual(observed, [(12, 0.2), (12, 0.1), (5, 0.2), (5, 0.1), (9, 0.2), (9, 0.1)])
        states = [value for address, value in self.rig.writes if address == 18]
        self.assertEqual(states[states.index(1):], [1, 0])
        self.assert_restored()

    def test_initial_report_timeout_turns_source_off(self):
        self.rig.port.rx.clear()
        with self.assertRaises(TimeoutError):
            self.run_rig(arguments())
        self.assertEqual(self.rig.regs[18], 0)
        self.assertEqual(self.rig.commands, [])  # Unidentified load is not controlled.
        self.assertCountEqual(self.rig.closed, ["source", "load"])

    def test_debug_log_preserves_rejected_spike_raw_reply_and_shutdown(self):
        measure = tool.measure_point

        def spike(*args, **kwargs):
            self.rig.current_query_override = 187
            return measure(*args, **kwargs)

        with TemporaryDirectory() as temporary, patch.object(tool, "measure_point", side_effect=spike):
            folder = Path(temporary) / "results"
            with self.assertRaisesRegex(tool.OperatingPointError, "ATORCH 4.900 V / 0.187 A"):
                self.run_rig(arguments(output_dir=folder, voltages=(5,), currents=(0.1,)))
            events = [json.loads(line) for line in (folder / "debug.jsonl").read_text(encoding="utf-8").splitlines()]
        observed = [event for event in events if event["event"] == "sample" and event["phase"] == "measurement"]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["load_a"], 0.187)
        self.assertEqual(observed[0]["source_a"], 0.1)
        self.assertTrue(any(event["event"] == "atorch_query" and event["command"] == "0x12"
                            and event["value"] == 187 for event in events))
        self.assertTrue(any(event["event"] == "atorch_rx" and "ca cb 00 00 bb ce cf" in event["data_hex"]
                            for event in events))
        self.assertTrue(any(event["event"] == "sample" and event["phase"] == "settling" for event in events))
        self.assertTrue(any(event["event"] == "rk_restore" and event["address"] == 82 for event in events))
        self.assertEqual(events[-1]["status"], "error")
        self.assertIn("0.187", events[-1]["error"])
        self.assertEqual([event["elapsed_s"] for event in events], sorted(event["elapsed_s"] for event in events))
        self.assert_restored()

    def test_debug_log_covers_initialization_failure(self):
        self.rig.fail_load_open = True
        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / "results"
            with self.assertRaises(OSError):
                self.run_rig(arguments(output_dir=folder))
            events = [json.loads(line) for line in (folder / "debug.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["error"], "load port unavailable")
        self.assertTrue(any(event["event"] == "shutdown_end" and event["success"] for event in events))

    def test_debug_disk_failure_does_not_prevent_off(self):
        output = SimpleNamespace(write=lambda line: self.fail_disk(), flush=lambda: None)
        debug = tool.DebugLog(output)
        source = tool.RKSource(self.rig.module, debug)
        source.set_enabled(False)
        self.assertEqual(self.rig.regs[18], 0)
        self.assertTrue(debug.failed)
        self.assertIn("Debug logging stopped", self.out.getvalue())

    def fail_disk(self):
        raise OSError("disk full")

    def test_invalid_checksum_is_preserved_in_debug_log(self):
        output = StringIO()
        bad_report = bytearray(report())
        bad_report[-1] ^= 1
        self.rig.port.rx = bad_report + report()
        load = tool.AtorchLoad(self.rig.port, tool.DebugLog(output))
        self.assertTrue(load.read_enabled())
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(any(event["event"] == "atorch_frame" and not event["valid"] for event in events))

    def test_nonfinite_debug_values_remain_valid_json(self):
        output = StringIO()
        tool.DebugLog(output)("test", voltage=float("nan"))
        event = json.loads(output.getvalue(), parse_constant=lambda constant: self.fail("Nonstandard JSON"))
        self.assertEqual(event["voltage"], "nan")

    def test_load_open_failure_turns_source_off(self):
        self.rig.fail_load_open = True
        with self.assertRaisesRegex(OSError, "load port unavailable"):
            self.run_rig(arguments())
        self.assertEqual(self.rig.regs[18], 0)
        self.assertEqual(self.rig.closed, ["source"])

    def test_input_read_failure_turns_source_off(self):
        self.rig.fail_input = True
        with self.assertRaisesRegex(TimeoutError, "input read failed"):
            self.run_rig(arguments())
        self.assertEqual(self.rig.regs[18], 0)

    def test_probe_does_not_change_settings_or_outputs(self):
        self.run_rig(arguments(probe=True))
        self.assertEqual(self.rig.writes, [])
        self.assertTrue(all(command >= 0x10 for command in self.rig.commands))
        self.assertEqual(self.rig.regs, self.rig.original)
        self.assertTrue(self.rig.load_on)

    def test_failed_point_is_not_recorded_and_completed_point_survives(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / "results"
            # First 0.1 A point succeeds. A failed 0.2 A regulation is rejected.
            self.rig.wrong_current = True
            with self.assertRaisesRegex(RuntimeError, "Wrong load current"):
                self.run_rig(arguments(output_dir=folder, voltages=(5,), currents=(0.1, 0.2)))
            with (folder / "results.csv").open(newline="", encoding="utf-8") as output:
                points = list(csv.DictReader(output))
            self.assertEqual(len(points), 1)
            self.assertEqual(float(points[0]["set_a"]), 0.1)
            self.assertIn("Wrong load current", (folder / "results.html").read_text(encoding="utf-8"))
        self.assert_restored()

    def test_interrupted_measurement_restores_settings(self):
        with TemporaryDirectory() as temporary, patch.object(tool, "measure_point", side_effect=KeyboardInterrupt):
            folder = Path(temporary) / "results"
            with self.assertRaises(KeyboardInterrupt):
                self.run_rig(arguments(output_dir=folder))
            self.assertIn("KeyboardInterrupt", (folder / "results.html").read_text(encoding="utf-8"))
        self.assert_restored()

    def test_keep_source_on_completion_leaves_last_settings_and_stops_load(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / "results"
            self.run_rig(arguments(output_dir=folder, keep_source_on=True,
                                   voltages=(9, 12), currents=(0.1, 0.2), source_current_limit=1))
            events = [json.loads(line) for line in (folder / "debug.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertNotIn((18, 0), self.rig.writes)
        self.assertEqual(self.rig.regs, {8: 1200, 9: 1000, 82: 1320, 83: 1100, 18: 1})
        self.assertFalse(self.rig.load_on)
        self.assertFalse(any(e["event"] == "rk_restore" for e in events))
        self.assertTrue(events[0]["keep_source_on"])
        self.assertTrue(any(e["event"] == "rk_shutdown_skipped" for e in events))
        self.assertCountEqual(self.rig.closed, ["source", "load"])

    def test_keep_source_on_errors_and_ctrl_c_never_send_off_or_restore(self):
        for error in (TimeoutError("read failed"), tool.OperatingPointError("deviation"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.rig = Rig(self.clock)
                with patch.object(tool, "measure_point", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.run_rig(arguments(keep_source_on=True, voltages=(9,), currents=(0.1,), source_current_limit=1))
                self.assertNotIn((18, 0), self.rig.writes)
                self.assertEqual(self.rig.regs, {8: 900, 9: 1000, 82: 990, 83: 1100, 18: 1})
                self.assertFalse(self.rig.load_on)
                self.assertCountEqual(self.rig.closed, ["source", "load"])

    def test_keep_source_on_load_open_failure_preserves_initial_source_state(self):
        self.rig.fail_load_open = True
        with self.assertRaisesRegex(OSError, "load port unavailable"):
            self.run_rig(arguments(keep_source_on=True))
        self.assertEqual(self.rig.writes, [])
        self.assertEqual(self.rig.regs, self.rig.original)
        self.assertEqual(self.rig.closed, ["source"])

    def test_keep_source_on_startup_interrupt_leaves_startup_settings(self):
        read = self.rig.instrument.read_registers

        def interrupt_startup(address, count):
            if (address, count) == (10, 9):
                raise KeyboardInterrupt()
            return read(address, count)

        with patch.object(self.rig.instrument, "read_registers", side_effect=interrupt_startup):
            with self.assertRaises(KeyboardInterrupt):
                self.run_rig(arguments(keep_source_on=True, voltages=(5,), currents=(0.1,), source_current_limit=1,
                                       startup_voltage=12, startup_current=0.5, startup_seconds=60))
        self.assertNotIn((18, 0), self.rig.writes)
        self.assertEqual(self.rig.regs, {8: 1200, 9: 500, 82: 1320, 83: 1100, 18: 1})
        self.assertFalse(self.rig.load_on)

    def test_keep_source_on_lowers_protections_after_setting_new_operating_point(self):
        self.rig.regs.update({8: 1200, 9: 2000, 82: 1320, 83: 2200})
        write = self.rig.instrument.write_register

        def check_live_protections(address, value):
            if self.rig.regs[18] and address in (82, 83):
                setting = self.rig.regs[8 if address == 82 else 9]
                self.assertGreater(value, setting)
            return write(address, value)

        with patch.object(self.rig.instrument, "write_register", side_effect=check_live_protections):
            self.run_rig(arguments(keep_source_on=True, voltages=(5,), currents=(0.1,), source_current_limit=1))
        self.assertNotIn((18, 0), self.rig.writes)
        self.assertEqual(self.rig.regs, {8: 500, 9: 1000, 82: 550, 83: 1100, 18: 1})

    def test_keep_source_on_does_not_reenable_after_hardware_protection_trip(self):
        def trip(*args, **kwargs):
            self.rig.regs[18] = 0  # Hardware shutdown, not a program OFF command.
            raise RuntimeError("OVP/1")

        with patch.object(tool, "measure_point", side_effect=trip):
            with self.assertRaisesRegex(RuntimeError, "OVP/1"):
                self.run_rig(arguments(keep_source_on=True, voltages=(5,), currents=(0.1,)))
        self.assertNotIn((18, 0), self.rig.writes)
        self.assertEqual(self.rig.writes.count((18, 1)), 1)
        self.assertEqual(self.rig.regs[18], 0)
        self.assertFalse(self.rig.load_on)

    def test_rejected_window_is_repeated_whole_and_marked_in_results(self):
        measure = tool.measure_point
        calls = 0

        def first_window_spike(source, load, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls != 1:
                return measure(source, load, *args, **kwargs)
            read = load.read
            readings = iter((998, 1003, 808))

            def spike_read():
                self.rig.current_query_override = next(readings, 1000)
                try:
                    return read()
                finally:
                    self.rig.current_query_override = None

            with patch.object(load, "read", side_effect=spike_read):
                return measure(source, load, *args, **kwargs)

        with TemporaryDirectory() as temporary, patch.object(tool, "measure_point", side_effect=first_window_spike):
            folder = Path(temporary) / "results"
            self.run_rig(arguments(output_dir=folder, voltages=(20,), currents=(1, 2), seconds=10))
            with (folder / "results.csv").open(newline="", encoding="utf-8") as output:
                points = list(csv.DictReader(output))
            self.assertEqual([int(p["attempts"]) for p in points], [2, 1])
            self.assertAlmostEqual(float(points[0]["source_wh"]), 20 * 10 / 3600, places=10)
            self.assertAlmostEqual(float(points[0]["load_wh"]), 19.9 * 10 / 3600, places=10)
            self.assertEqual(float(points[0]["window_s"]), 10)
            self.assertIn("Attempts", (folder / "results.html").read_text(encoding="utf-8"))
            events = [json.loads(line) for line in (folder / "debug.jsonl").read_text(encoding="utf-8").splitlines()]
        starts = [e for e in events if e["event"] == "point_start"]
        self.assertEqual([(e["set_a"], e["attempt"]) for e in starts], [(1, 1), (1, 2), (2, 1)])
        self.assertEqual(sum(e["event"] == "point_rejected" for e in events), 1)
        self.assertTrue(any(e["event"] == "sample" and e["load_a"] == 0.808 for e in events))
        self.assert_continuously_powered(events)
        self.assertEqual(sum(e["event"] == "point_complete" for e in events), 2)
        self.assertIn("Repeating complete point (retry 1/1)", self.out.getvalue())
        self.assertIn("0/2 done, 2 left", self.out.getvalue())
        self.assertIn("2/2 done, 0 left", self.out.getvalue())
        self.assert_restored()

    def test_persistent_operating_point_error_stops_after_retry_limit(self):
        with TemporaryDirectory() as temporary, patch.object(tool, "measure_point", side_effect=tool.OperatingPointError("persistent deviation")) as measure:
            folder = Path(temporary) / "results"
            with self.assertRaisesRegex(tool.OperatingPointError, "persistent deviation"):
                self.run_rig(arguments(output_dir=folder, voltages=(20,), currents=(1,)))
            with (folder / "results.csv").open(newline="", encoding="utf-8") as output:
                self.assertEqual(list(csv.DictReader(output)), [])
        self.assertEqual(measure.call_count, 2)
        self.assert_restored()

    def test_point_retries_can_be_disabled(self):
        with patch.object(tool, "measure_point", side_effect=tool.OperatingPointError("deviation")) as measure:
            with self.assertRaisesRegex(tool.OperatingPointError, "deviation"):
                self.run_rig(arguments(voltages=(20,), currents=(1,), point_retries=0))
        self.assertEqual(measure.call_count, 1)
        self.assert_restored()

    def test_protection_errors_do_not_repeat_point(self):
        with patch.object(tool, "measure_point", side_effect=RuntimeError("OCP/2")) as measure:
            with self.assertRaisesRegex(RuntimeError, "OCP/2"):
                self.run_rig(arguments(voltages=(20,), currents=(1,)))
        self.assertEqual(measure.call_count, 1)
        self.assert_restored()

    def test_failed_final_off_does_not_hide_operating_point_error(self):
        def reject_and_lose_off(*args, **kwargs):
            self.rig.fail_off = True
            raise tool.OperatingPointError("deviation")

        with patch.object(tool, "measure_point", side_effect=reject_and_lose_off) as measure:
            with self.assertRaisesRegex(tool.OperatingPointError, "deviation"):
                self.run_rig(arguments(voltages=(20,), currents=(1,)))
        self.assertEqual(measure.call_count, 2)
        self.assertEqual(self.rig.writes.count((18, 1)), 1)
        self.assertFalse(self.rig.load_on)
        self.assertIn("OFF could not be verified", self.out.getvalue())

    def test_measurement_timeout_restores_source_and_reports_unverified_load_off(self):
        measure = tool.measure_point

        def lose_replies(*args, **kwargs):
            self.rig.suppress_replies = True
            return measure(*args, **kwargs)

        with TemporaryDirectory() as temporary, patch.object(tool, "measure_point", side_effect=lose_replies):
            folder = Path(temporary) / "results"
            with self.assertRaises(TimeoutError):
                self.run_rig(arguments(output_dir=folder))
            self.assertIn("no reply", (folder / "results.html").read_text(encoding="utf-8"))
        self.assert_restored()
        self.assertIn("OFF sent without verification", self.out.getvalue())
        self.assertCountEqual(self.rig.closed, ["source", "load"])
        self.assertEqual(self.rig.writes.count((18, 1)), 1)

    def test_partial_protection_update_is_restored_without_enabling_source(self):
        write = self.rig.instrument.write_register

        def fail_new_ocp(address, value):
            if (address, value) == (83, 2310):
                raise TimeoutError("OCP write failed")
            write(address, value)

        with TemporaryDirectory() as temporary, patch.object(self.rig.instrument, "write_register", side_effect=fail_new_ocp):
            with self.assertRaisesRegex(TimeoutError, "OCP write failed"):
                self.run_rig(arguments(output_dir=Path(temporary) / "results"))
        self.assert_restored()
        self.assertNotIn((18, 1), self.rig.writes)

    def test_invalid_cli_options_are_rejected_before_connecting(self):
        for option in (["--ovp", "5"], ["--ocp", "0.15"],
                       ["--current-tolerance", "nan"], ["--voltage-tolerance", "1"],
                       ["--settle-timeout", "nan"], ["--settle-timeout", "2"],
                       ["--point-retries", "-1"]):
            with self.subTest(option=option), patch.object(sys, "argv", command_line(["--voltages", "5", "--currents", "0.1"] + option)), patch.object(tool, "run") as run:
                with self.assertRaises(SystemExit) as stopped:
                    tool.main()
                self.assertEqual(stopped.exception.code, 2)
                run.assert_not_called()

    def test_startup_cli_requires_complete_valid_group(self):
        startup = ["--startup-voltage", "12", "--startup-current", "0.5", "--startup-seconds", "60"]
        invalid = [startup[:2], startup[2:4], startup[4:], startup[:4], startup[:2] + startup[4:], startup[2:]]
        invalid += [startup[:-1] + [value] for value in ("0", "-1", "nan", "inf")]
        invalid += [startup + ["--probe"], startup + ["--ovp", "6"], startup + ["--ocp", "0.5"]]
        for options in invalid:
            with self.subTest(options=options), patch.object(sys, "argv", command_line(["--voltages", "3.3", "--currents", "0.1"] + options)), patch.object(tool, "run") as run:
                with self.assertRaises(SystemExit) as stopped:
                    tool.main()
                self.assertEqual(stopped.exception.code, 2)
                run.assert_not_called()

    def test_cli_accepts_startup_and_serial_debug(self):
        options = command_line(["--voltages", "3.3", "5", "--currents", "0.1", "--seconds", "15",
                   "--startup-voltage", "12", "--startup-current", "0.123", "--startup-seconds", "60", "--debug-serial", "--keep-source-on"])
        with patch.object(sys, "argv", options), patch.object(tool, "run") as run:
            self.assertEqual(tool.main(), 0)
        args = run.call_args.args[0]
        self.assertEqual((args.startup_voltage, args.startup_current, args.startup_seconds, args.seconds), (12, 0.123, 60, 15))
        self.assertTrue(args.debug_serial)
        self.assertTrue(args.keep_source_on)

    def test_serial_trace_preserves_bytes_and_forwards_configuration(self):
        output = StringIO()
        port = tool.SerialTrace(self.rig.port, "ATORCH", tool.DebugLog(output))
        packet = bytes((0xb1, 0xb2, 0x11, 0, 0, 0xb6))
        self.assertEqual(port.write(packet), len(packet))
        expected = report() + reply(4900)
        self.assertEqual(port.read(len(expected)), expected)
        port.timeout = 0.25
        self.assertEqual(self.rig.port.timeout, 0.25)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[0]["event"], "serial_tx")
        self.assertEqual(bytes.fromhex(events[0]["data_hex"]), packet)
        self.assertEqual(events[-1]["event"], "serial_rx")
        self.assertEqual(bytes.fromhex(events[-1]["data_hex"]), expected)
        self.assertIn("ATORCH FAKE_LOAD (9600 baud) TX: b1 b2 11 00 00 b6", self.out.getvalue())
        self.assertIn("ATORCH FAKE_LOAD (9600 baud) RX:", self.out.getvalue())

    def test_serial_trace_reports_empty_reads_partial_writes_and_errors(self):
        output = StringIO()
        raw = SimpleNamespace(port="COM3", baudrate=115200, read=lambda count: b"", write=lambda data: 1)
        port = tool.SerialTrace(raw, "RK6006", tool.DebugLog(output))
        self.assertEqual(port.read(35), b"")
        self.assertEqual(port.write(b"\x01\x03"), 1)
        with patch.object(raw, "read", side_effect=OSError("serial read failed")):
            with self.assertRaisesRegex(OSError, "serial read failed"):
                port.read(35)
        with patch.object(raw, "write", side_effect=OSError("serial write failed")):
            with self.assertRaisesRegex(OSError, "serial write failed"):
                port.write(b"\x01\x03")
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[0]["data_hex"], "")
        self.assertEqual(events[0]["requested_bytes"], 35)
        self.assertTrue(any(e["event"] == "serial_partial_write" for e in events))
        self.assertEqual(sum(e["event"] == "serial_error" for e in events), 2)
        self.assertIn("<no data>", self.out.getvalue())

    def test_serial_trace_console_failure_does_not_block_writes(self):
        raw = SimpleNamespace(port="COM3", baudrate=115200, write=lambda data: len(data))
        port = tool.SerialTrace(raw, "RK6006")
        with patch("builtins.print", side_effect=OSError("console closed")):
            self.assertEqual(port.write(b"\x01\x03"), 2)
            self.assertEqual(port.write(b"\x01\x06"), 2)

    def test_serial_debug_on_probe_remains_read_only(self):
        self.run_rig(arguments(probe=True, debug_serial=True))
        self.assertEqual(self.rig.writes, [])
        self.assertTrue(all(command >= 0x10 for command in self.rig.commands))
        self.assertIn("ATORCH FAKE_LOAD (9600 baud) RX:", self.out.getvalue())

    def test_real_modbus_identification_failure_is_traced_and_closes_port(self):
        try:
            import minimalmodbus
        except ImportError:
            self.skipTest("install requirements.txt to test the real Modbus integration")
        spec = importlib.util.spec_from_file_location("rk_integration", tool.TOOLS / "RK6006-Python-Module" / "RK6006_module.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for response in (bytes(35), b""):
            with self.subTest(response=response), TemporaryDirectory() as temporary:
                received = BytesIO(response)
                transmitted = []
                closed = []
                raw = SimpleNamespace(port="FAKE_SOURCE", baudrate=115200, timeout=0.5, is_open=True,
                                      open=lambda: None, close=lambda: closed.append(True),
                                      read=received.read, write=lambda data: transmitted.append(data) or len(data),
                                      reset_input_buffer=lambda: None, reset_output_buffer=lambda: None)
                folder = Path(temporary) / "results"
                with patch.dict(sys.modules, {"serial": SimpleNamespace(Serial=lambda *args, **kwargs: raw), "RK6006_module": module}):
                    with self.assertRaisesRegex(RuntimeError, "Could not identify RK6006 on FAKE_SOURCE"):
                        tool.run(arguments(output_dir=folder, debug_serial=True))
                self.assertEqual(closed, [True])
                self.assertTrue(transmitted)
                self.assertTrue(all(packet.startswith(b"\x01\x03\x00\x00\x00\x0f") for packet in transmitted))
                events = [json.loads(line) for line in (folder / "debug.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertTrue(any(e["event"] == "serial_tx" and e["port"] == "FAKE_SOURCE" for e in events))
                self.assertTrue(any(e["event"] == "serial_rx" and bytes.fromhex(e["data_hex"]) == response for e in events))
                self.assertFalse(any(e["event"] == "rk_write" for e in events))

    def test_measurement_requires_both_setpoint_lists_before_connecting(self):
        for options in ([], ["--voltages", "5"], ["--currents", "0.1"]):
            with self.subTest(options=options), patch.object(sys, "argv", command_line(options)), patch.object(tool, "run") as run:
                with self.assertRaises(SystemExit) as stopped:
                    tool.main()
                self.assertEqual(stopped.exception.code, 2)
                run.assert_not_called()
        self.assertIn("measurement requires explicit --voltages and --currents", self.out.getvalue())

    def test_cli_uses_only_explicit_setpoints_in_given_order(self):
        options = command_line(["--voltages", "3.3", "1.8", "--currents", "0.33", "0.12", "--source-current-limit", "3.123"])
        with patch.object(sys, "argv", options), patch.object(tool, "run") as run:
            self.assertEqual(tool.main(), 0)
        args = run.call_args.args[0]
        self.assertEqual(args.voltages, [3.3, 1.8])
        self.assertEqual(args.currents, [0.33, 0.12])
        self.assertEqual(args.source_current_limit, 3.123)

    def test_measurement_requires_explicit_source_current_limit_before_connecting(self):
        options = ["tool", "--source-port", "FAKE_SOURCE", "--load-port", "FAKE_LOAD",
                   "--voltages", "5", "--currents", "1"]
        with patch.object(sys, "argv", options), patch.object(tool, "run") as run:
            with self.assertRaises(SystemExit) as stopped:
                tool.main()
            self.assertEqual(stopped.exception.code, 2)
            run.assert_not_called()
        self.assertIn("measurement requires explicit --source-current-limit", self.out.getvalue())

    def test_source_current_limit_invalid_values_rejected_before_connecting(self):
        for value in ("0", "-1", "nan", "inf", "0.0001", "3.1234"):
            options = command_line(["--voltages", "5", "--currents", "1", "--source-current-limit", value])
            with self.subTest(value=value), patch.object(sys, "argv", options), patch.object(tool, "run") as run:
                with self.assertRaises(SystemExit) as stopped:
                    tool.main()
                self.assertEqual(stopped.exception.code, 2)
                run.assert_not_called()

    def test_source_current_limit_is_independent_of_load_setpoint(self):
        self.rig.port.rx.clear()
        for current_limit in (0.25, 3.123):
            with self.subTest(current_limit=current_limit):
                args = arguments(voltages=(5,), currents=(1,), source_current_limit=current_limit)
                self.assertEqual(tool.planned_limits(args), (current_limit, 5.5, round(current_limit * 1.1, 3)))
                self.source.configure_limits(args)
                self.assertEqual(self.rig.regs[9], round(current_limit * 1000))

    def test_boost_converter_measurement_with_higher_input_current(self):
        read = self.rig.instrument.read_registers
        query = tool.AtorchLoad.query

        def boost_source(address, count):
            values = read(address, count)
            if (address, count) == (10, 9):
                values[1] = 2700  # Source: 5 V / 2.7 A, with a 3 A limit.
            return values

        def boost_load(load, command):
            return 12000 if command == 0x11 else query(load, command)

        with TemporaryDirectory() as temporary, patch.object(self.rig.instrument, "read_registers", side_effect=boost_source), patch.object(tool.AtorchLoad, "query", boost_load):
            folder = Path(temporary) / "results"
            self.run_rig(arguments(output_dir=folder, voltages=(5,), currents=(1,), source_current_limit=3))
            with (folder / "results.csv").open(newline="", encoding="utf-8") as output:
                point, = csv.DictReader(output)
        self.assertEqual(float(point["source_a"]), 2.7)
        self.assertEqual(float(point["load_a"]), 1)
        self.assertEqual(float(point["load_v"]), 12)
        self.assertAlmostEqual(float(point["efficiency_pct"]), 100 * 12 / 13.5)
        self.assertIn((9, 3000), self.rig.writes)
        self.assert_restored()

    def test_source_current_limit_and_protection_range_checks_precede_writes(self):
        for changes in ({"source_current_limit": 6.1}, {"source_current_limit": 3, "ocp": 2.5}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.source.configure_limits(arguments(**changes))
        self.assertEqual(self.rig.writes, [])

    def test_arbitrary_setpoints_reach_hardware_and_results(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / "results"
            self.run_rig(arguments(output_dir=folder, voltages=(3.3, 1.8), currents=(0.33, 0.12)))
            with (folder / "results.csv").open(newline="", encoding="utf-8") as output:
                points = list(csv.DictReader(output))
        self.assertEqual([(float(p["set_v"]), float(p["set_a"])) for p in points],
                         [(3.3, 0.33), (3.3, 0.12), (1.8, 0.33), (1.8, 0.12)])
        self.assertIn((8, 330), self.rig.writes)
        self.assertIn((8, 180), self.rig.writes)
        self.assert_restored()

    def test_invalid_setpoints_are_rejected_before_connecting(self):
        for option in ("--voltages", "--currents"):
            for value in ("0", "-1", "nan", "inf", "0.001", "3.333"):
                options = command_line(["--voltages", "3.3", "--currents", "0.33", option, value])
                with self.subTest(option=option, value=value), patch.object(sys, "argv", options), patch.object(tool, "run") as run:
                    with self.assertRaises(SystemExit) as stopped:
                        tool.main()
                    self.assertEqual(stopped.exception.code, 2)
                    run.assert_not_called()

    def test_probe_cli_needs_no_setpoints_and_changes_nothing(self):
        options = ["tool", "--source-port", "FAKE_SOURCE", "--load-port", "FAKE_LOAD", "--probe"]
        with patch.object(sys, "argv", options), patch.object(tool, "run", wraps=tool.run) as run, patch.dict(sys.modules, {
            "serial": SimpleNamespace(Serial=self.rig.open_serial),
            "RK6006_module": SimpleNamespace(RK6006=lambda port: self.rig.module),
        }):
            self.assertEqual(tool.main(), 0)
        args = run.call_args.args[0]
        self.assertIsNone(args.voltages)
        self.assertIsNone(args.currents)
        self.assertIsNone(args.source_current_limit)
        self.assertEqual(self.rig.writes, [])
        self.assertTrue(all(command >= 0x10 for command in self.rig.commands))
        self.assertEqual(self.rig.regs, self.rig.original)

    def test_measurement_and_probe_require_both_ports_before_connecting(self):
        for mode in (["--probe", "--debug-serial"], ["--voltages", "3.3", "--currents", "0.1"]):
            for ports in ([], ["--source-port", "FAKE_SOURCE"], ["--load-port", "FAKE_LOAD"]):
                with self.subTest(mode=mode, ports=ports), patch.object(sys, "argv", ["tool"] + mode + ports), patch.object(tool, "run") as run:
                    with self.assertRaises(SystemExit) as stopped:
                        tool.main()
                    self.assertEqual(stopped.exception.code, 2)
                    run.assert_not_called()
        self.assertIn("use --list-ports", self.out.getvalue())

    def test_list_ports_needs_no_ports_or_setpoints_and_does_not_connect(self):
        devices = [SimpleNamespace(device="COM9", description="USB serial B"),
                   SimpleNamespace(device="COM7", description="USB serial A")]
        for ports in ([], devices):
            with self.subTest(ports=ports), patch.object(sys, "argv", ["tool", "--list-ports"]), patch.object(tool, "run") as run, patch.dict(sys.modules, {
                "serial.tools.list_ports": SimpleNamespace(comports=lambda: ports),
            }):
                self.assertEqual(tool.main(), 0)
                run.assert_not_called()
        text = self.out.getvalue()
        self.assertIn("No serial ports found.", text)
        self.assertIn("COM7: USB serial A", text)
        self.assertIn("COM9: USB serial B", text)
        self.assertLess(text.index("COM7:"), text.index("COM9:"))

    def test_restore_failure_still_restores_other_registers(self):
        settings = self.source.snapshot_settings()
        self.configured()
        self.source.set_voltage(20)
        self.rig.fail_restore = 8
        with self.assertRaisesRegex(RuntimeError, "register 8 could not be restored"):
            tool.shutdown(self.source, self.load, settings)
        self.assertEqual(self.rig.regs[18], 0)
        self.assertFalse(self.rig.load_on)
        for address in (9, 82, 83):
            self.assertEqual(self.rig.regs[address], settings[address])

    def test_failed_source_off_skips_restoration_but_attempts_load_off(self):
        settings = self.source.snapshot_settings()
        self.configured()
        self.rig.fail_off = True
        before = dict(self.rig.regs)
        with self.assertRaisesRegex(RuntimeError, "settings not restored"):
            tool.shutdown(self.source, self.load, settings)
        self.assertEqual(self.rig.regs, before)
        self.assertFalse(self.rig.load_on)

    def test_cleanup_failure_does_not_hide_original_error(self):
        self.rig.fail_input = True
        with patch.object(self.rig.module, "get_input_voltage", side_effect=self.fail_input_then_off):
            with self.assertRaisesRegex(TimeoutError, "original input error"):
                self.run_rig(arguments())
        self.assertIn("source OFF failed", self.out.getvalue())

    def fail_input_then_off(self):
        self.rig.fail_off = True
        raise TimeoutError("original input error")


if __name__ == "__main__":
    unittest.main()
