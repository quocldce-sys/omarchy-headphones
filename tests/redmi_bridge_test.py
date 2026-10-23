"""What redmi-bridge sends Redmi Buds 5, and what it prints.

    python -m unittest tests.redmi_bridge_test

The frozen session is tests/pins/redmi/buds-5.json. The frames here are
verbatim what a Redmi Buds 5 sent (docs/captures/redmi-buds-5.txt): left 75,
right 80, case 100, confirmed against the Xiaomi Earbuds app, and the noise
control going transparency -> cancelling -> off -> transparency. What is
here beyond the pin is the framing against those frames, damage the parser
must survive (synthetic, and marked as such), and the run loop driven through
the scheduled timers, the socket callbacks and a fake BlueZ: silence, a
dropped link, a closed pipe, a stop during connect.

No hardware and no socket: the bridge's effects on the world are write(),
emit() and the file descriptor callbacks, and all of them are captured.
"""
import contextlib
import errno
import io
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from tests import harness

bridge = harness.load_bridge("redmi-bridge")

ADDRESS = "AA:BB:CC:00:00:01"

# GetTargetInfo answer, as the earbuds sent it.
ANSWER = bytes.fromhex(
    "fe dc ba 00 02 00 2c 00 01 05 01 10 38 10 38 05 03 27 17 50 6a 02 04 01 "
    "02 05 00 03 06 10 38 04 07 4b 50 64 02 08 02 02 09 00 02 0c 00 02 0d 02 "
    "02 0e 00 ef")
# GetDeviceConfig(11) answers: transparency, cancelling, off.
NOISE_AMBIENT = bytes.fromhex("fe dc ba 00 f3 00 07 00 01 04 00 0b 02 00 ef")
NOISE_ANC = bytes.fromhex("fe dc ba 00 f3 00 07 00 05 04 00 0b 01 00 ef")
NOISE_OFF = bytes.fromhex("fe dc ba 00 f3 00 07 00 07 04 00 0b 00 00 ef")
# What the earbuds announce on their own after a set.
NOTIFY_ANC = bytes.fromhex("fe dc ba c7 f4 00 06 05 04 00 0b 01 00 ef")
SET_ACK = bytes.fromhex("fe dc ba 00 f2 00 02 00 04 ef")
STATUS_REPORT_ANC = bytes.fromhex("fe dc ba c7 0e 00 04 04 02 04 01 ef")


class Session(harness.Session):
    """A Redmi session. "device" in a pin is bytes as hex, fed through the
    bridge's framer. "sent" is every frame the bridge wrote, as hex. "open" is
    the channel coming up, which asks for the mode and the battery and
    schedules the first-answer check, the mode grace and the poll."""

    def __init__(self):
        super().__init__(bridge)
        self.bridge = bridge.Bridge(None, ADDRESS, harness.FakeLoop())
        self.bridge.write = self.frames.append

    def device(self, spec):
        self.bridge.buffer += harness.hexbytes(spec)
        self.bridge.parse_buffer()

    def do_open(self):
        self.bridge.opened(-1)


harness.pin_tests(globals(), "redmi-bridge", Session)


class AskTest(unittest.TestCase):
    def test_battery_ask_is_get_target_info_with_every_attribute(self):
        self.assertEqual(bridge.encode_get_target_info(1).hex(" "),
                         "fe dc ba c0 02 00 05 01 ff ff ff ff ef")

    def test_noise_ask_is_get_device_config_11(self):
        self.assertEqual(bridge.encode_get_noise(1).hex(" "),
                         "fe dc ba c0 f3 00 03 01 00 0b ef")

    def test_noise_set_is_the_config_the_app_wrote(self):
        # The sets in the capture: transparency (sn 2), cancelling (4), off (6).
        self.assertEqual(bridge.encode_set_noise(2, 2, 0).hex(" "),
                         "fe dc ba c0 f2 00 06 02 04 00 0b 02 00 ef")
        self.assertEqual(bridge.encode_set_noise(4, 1, 0).hex(" "),
                         "fe dc ba c0 f2 00 06 04 04 00 0b 01 00 ef")
        self.assertEqual(bridge.encode_set_noise(6, 0, 0).hex(" "),
                         "fe dc ba c0 f2 00 06 06 04 00 0b 00 00 ef")

    def test_sequence_number_wraps_to_a_byte(self):
        self.assertEqual(bridge.encode_get_target_info(0x101)[7], 0x01)


class BatteryTest(unittest.TestCase):
    def test_left_right_case_from_the_captured_answer(self):
        self.assertEqual(bridge.battery_of(ANSWER),
                         {"left": 75, "right": 80, "case": 100, "charging": []})

    def test_the_status_report_the_earbuds_send_is_not_read_for_a_battery(self):
        # It carried none in the capture, so nothing is taken from it.
        self.assertIsNone(bridge.battery_of(STATUS_REPORT_ANC))

    # Synthetic below: the capture has no charging earbud and no empty one.
    def test_charging_bit_and_no_reading(self):
        self.assertEqual(bridge.battery_from([0xCB, 0x50, 0xFF]),
                         {"left": 75, "right": 80, "charging": ["left"]})

    def test_nothing_reported_is_none(self):
        self.assertIsNone(bridge.battery_from([0xFF, 0xFF, 0xFF]))

    def test_out_of_range_level_is_dropped(self):
        self.assertEqual(bridge.battery_from([0x7F, 0x50, 0x64]),
                         {"right": 80, "case": 100, "charging": []})

    def test_an_error_status_carries_no_battery(self):
        failed = bytearray(ANSWER)
        failed[7] = 0x01
        self.assertIsNone(bridge.battery_of(bytes(failed)))

    def test_other_opcodes_are_ignored(self):
        other = bytearray(ANSWER)
        other[4] = 0x05
        self.assertIsNone(bridge.battery_of(bytes(other)))

    def test_our_own_ask_echoed_back_is_not_an_answer(self):
        self.assertIsNone(bridge.battery_of(bridge.encode_get_target_info(1)))
        self.assertIsNone(bridge.noise_of(bridge.encode_get_noise(1)))


class NoiseTest(unittest.TestCase):
    def test_the_three_states(self):
        self.assertEqual(bridge.noise_of(NOISE_AMBIENT), (2, 0))
        self.assertEqual(bridge.noise_of(NOISE_ANC), (1, 0))
        self.assertEqual(bridge.noise_of(NOISE_OFF), (0, 0))

    def test_the_announcement_after_a_set(self):
        self.assertEqual(bridge.noise_of(NOTIFY_ANC), (1, 0))

    def test_the_set_ack_and_the_status_report_say_nothing_here(self):
        self.assertIsNone(bridge.noise_of(SET_ACK))
        self.assertIsNone(bridge.noise_of(STATUS_REPORT_ANC))

    # Synthetic: no state but 0, 1, 2 was seen.
    def test_an_unknown_state_is_not_a_mode(self):
        odd = bytearray(NOISE_ANC)
        odd[12] = 0x09
        self.assertIsNone(bridge.noise_of(bytes(odd)))

    def test_another_config_is_not_the_noise_control(self):
        other = bytearray(NOISE_ANC)
        other[11] = 0x25
        self.assertIsNone(bridge.noise_of(bytes(other)))

    def test_a_config_too_short_to_hold_state_and_level(self):
        short = bytes.fromhex("fe dc ba 00 f3 00 06 00 01 03 00 0b 02 ef")
        self.assertIsNone(bridge.noise_of(short))


class FramingTest(unittest.TestCase):
    def test_a_frame_split_across_reads(self):
        buffer = bytearray()
        got = []
        for i in range(0, len(ANSWER), 5):
            buffer += ANSWER[i:i + 5]
            frame, buffer = bridge.split_frame(buffer)
            if frame:
                got.append(frame)
        self.assertEqual(got, [ANSWER])

    def test_three_frames_in_one_read_as_the_earbuds_send_after_a_set(self):
        burst = STATUS_REPORT_ANC + NOTIFY_ANC + SET_ACK
        got = []
        buffer = bytearray(burst)
        while True:
            frame, buffer = bridge.split_frame(buffer)
            if frame is None:
                break
            got.append(frame)
        self.assertEqual(got, [STATUS_REPORT_ANC, NOTIFY_ANC, SET_ACK])

    # Synthetic damage.
    def test_garbage_before_and_two_frames(self):
        frame, rest = bridge.split_frame(bytearray(b"\x00\x01" + ANSWER + ANSWER))
        self.assertEqual(frame, ANSWER)
        self.assertEqual(bytes(rest), ANSWER)

    def test_an_absurd_length_does_not_stall(self):
        frame, rest = bridge.split_frame(bytearray(b"\xfe\xdc\xba\x00\x02\xff\xff" + ANSWER))
        self.assertEqual(frame, ANSWER)

    def test_a_bad_tail_resyncs(self):
        broken = bytearray(ANSWER)
        broken[-1] = 0x00
        frame, rest = bridge.split_frame(bytearray(bytes(broken) + ANSWER))
        self.assertEqual(frame, ANSWER)

    def test_a_header_cut_in_two_is_kept(self):
        frame, rest = bridge.split_frame(bytearray(b"\x00\x00\xfe\xdc"))
        self.assertIsNone(frame)
        self.assertEqual(bytes(rest), b"\xfe\xdc")
        frame, rest = bridge.split_frame(rest + ANSWER[2:])
        self.assertEqual(frame, ANSWER)

    def test_empty_and_header_only_input(self):
        self.assertEqual(bridge.split_frame(bytearray())[0], None)
        self.assertEqual(bridge.split_frame(bytearray(b"\xfe\xdc\xba"))[0], None)


class Modes(unittest.TestCase):
    def ready(self):
        s = Session()
        s.do_open()
        s.device(NOISE_AMBIENT.hex(" "))
        s.device(ANSWER.hex(" "))
        s.frames.clear()
        return s

    def test_a_set_sends_the_config_and_does_not_claim_the_new_mode(self):
        s = self.ready()
        s.command("set anc")
        self.assertEqual(len(s.frames), 1)
        self.assertEqual(s.frames[0][4:5], b"\xf2")
        self.assertEqual(bytes(s.frames[0][-5:-1]), bytes([0x00, 0x0b, 1, 0]))
        self.assertEqual(s.lines[-1]["mode"], "ambient")

    def test_the_announcement_moves_the_mode(self):
        s = self.ready()
        s.device(NOTIFY_ANC.hex(" "))
        self.assertEqual(s.lines[-1]["mode"], "anc")

    def test_a_set_keeps_the_strength_the_state_was_last_seen_at(self):
        # Synthetic: every level in the capture was 0.
        s = self.ready()
        deep = bytearray(NOTIFY_ANC)
        deep[12] = 2
        s.device(bytes(deep).hex(" "))
        s.device(NOISE_AMBIENT.hex(" "))
        s.frames.clear()
        s.command("set anc")
        self.assertEqual(bytes(s.frames[0][-5:-1]), bytes([0x00, 0x0b, 1, 2]))

    def test_nothing_is_sent_before_the_earbuds_have_said_their_mode(self):
        s = Session()
        s.do_open()
        s.frames.clear()
        s.command("set anc")
        self.assertEqual(s.frames, [])

    def test_unknown_commands_are_ignored_without_sending(self):
        s = self.ready()
        for line in ("set talkthru", "set", "level 3", "voice on", "set anc now", ""):
            s.command(line)
        self.assertEqual(s.frames, [])

    def test_the_readback_after_a_set_is_driven_by_the_timer(self):
        s = self.ready()
        s.command("set off")
        self.assertIn(bridge.RECHECK_MS, s.timers)
        s.glib.timers = [t for t in s.glib.timers if t[0] == bridge.RECHECK_MS]
        s.frames.clear()
        s.fire()
        self.assertEqual(s.frames[0][4:5], b"\xf3")

    def test_the_battery_goes_out_alone_when_no_mode_comes(self):
        s = Session()
        s.do_open()
        s.device(ANSWER.hex(" "))
        self.assertEqual(s.lines, [])
        self.assertIn(bridge.MODE_GRACE_MS, s.timers)
        s.fire()
        self.assertEqual(s.lines[-1], {"modes": True, "available": [],
                                       "battery": {"left": 75, "right": 80, "case": 100,
                                                   "charging": []}})

    def test_the_same_state_is_not_printed_twice(self):
        s = self.ready()
        before = len(s.lines)
        s.device(NOISE_AMBIENT.hex(" "))
        s.device(ANSWER.hex(" "))
        self.assertEqual(len(s.lines), before)

    def test_the_poll_asks_for_the_mode_and_the_battery_again(self):
        s = self.ready()
        self.assertTrue(s.bridge.poll())
        self.assertEqual([f[4] for f in s.frames], [0xF3, 0x02])


class RunLoop(unittest.TestCase):
    """The bridge through its timers and its descriptor callbacks."""

    def open(self):
        s = Session()
        s.do_open()
        return s

    def feed(self, s, *chunks):
        reads = list(chunks)

        def read(fd, size):
            item = reads.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(bridge.os, "read", side_effect=read):
            result = None
            for _ in chunks:
                result = s.bridge.on_io(-1, bridge.GLib.IO_IN)
            return result

    def test_silence_is_found_by_the_timer_and_is_exit_3(self):
        s = self.open()
        self.assertIn(bridge.FIRST_ANSWER_MS, s.timers)
        s.fire()
        self.assertEqual(s.bridge.exit_code, bridge.EXIT_UNSUPPORTED)
        self.assertEqual(s.lines[-1]["modes"], False)

    def test_an_answer_means_the_timer_finds_nothing_to_report(self):
        s = self.open()
        self.feed(s, NOISE_AMBIENT)
        s.fire()
        self.assertIsNone(s.bridge.exit_code)

    def test_replies_through_the_socket_callback_in_pieces(self):
        s = self.open()
        self.feed(s, ANSWER[:10], ANSWER[10:30], ANSWER[30:] + NOISE_ANC[:6], NOISE_ANC[6:])
        self.assertEqual(s.lines[-1]["mode"], "anc")
        self.assertEqual(s.lines[-1]["battery"]["left"], 75)

    def test_a_burst_of_frames_after_a_set(self):
        s = self.open()
        self.feed(s, NOISE_AMBIENT, ANSWER)
        self.feed(s, STATUS_REPORT_ANC + NOTIFY_ANC + SET_ACK)
        self.assertEqual(s.lines[-1]["mode"], "anc")

    def test_echoes_and_junk_print_nothing(self):
        s = self.open()
        self.feed(s, bridge.encode_get_noise(1) + bridge.encode_get_target_info(2),
                  b"\x00\x01\x02", SET_ACK)
        self.assertEqual(s.lines, [])
        self.assertIsNone(s.bridge.exit_code)

    def test_the_link_closing_is_exit_1(self):
        s = self.open()
        self.assertFalse(self.feed(s, b""))
        self.assertEqual(s.bridge.exit_code, bridge.EXIT_TRANSIENT)

    def test_a_read_error_is_exit_1(self):
        s = self.open()
        self.assertFalse(self.feed(s, OSError(errno.ECONNRESET, "reset")))
        self.assertEqual(s.bridge.exit_code, bridge.EXIT_TRANSIENT)

    def test_a_hangup_condition_is_exit_1(self):
        s = self.open()
        self.assertFalse(s.bridge.on_io(-1, bridge.GLib.IO_HUP))
        self.assertEqual(s.bridge.exit_code, bridge.EXIT_TRANSIENT)

    def test_a_write_error_is_exit_1(self):
        s = self.open()
        s.bridge.write = bridge.Bridge.write.__get__(s.bridge)
        s.bridge.fd = 99
        with mock.patch.object(bridge.os, "write", side_effect=OSError(errno.EPIPE, "pipe")):
            s.bridge.write(b"x")
        self.assertEqual(s.bridge.exit_code, bridge.EXIT_TRANSIENT)

    def test_nothing_is_written_once_the_bridge_has_stopped(self):
        s = self.open()
        self.feed(s, NOISE_AMBIENT)
        s.bridge.finish(0)
        s.frames.clear()
        s.bridge.command("set anc")
        s.bridge.ask()
        self.assertFalse(s.bridge.poll())
        self.assertEqual(s.frames, [])

    def test_a_stop_during_connect_makes_no_later_attempt(self):
        bus = mock.Mock()
        loop = mock.Mock()
        b = bridge.Bridge(bus, ADDRESS, loop)
        b.device = mock.Mock()
        b.finish(0)
        self.assertFalse(b.connect())
        b.device.ConnectProfile.assert_not_called()
        loop.quit.assert_called_once()

    def test_a_connect_that_fails_after_the_stop_changes_nothing(self):
        b = bridge.Bridge(mock.Mock(), ADDRESS, mock.Mock())
        b.device = mock.Mock()
        b.connect()
        failed = b.device.ConnectProfile.call_args.kwargs["error_handler"]
        b.finish(0)
        failed(types.SimpleNamespace(get_dbus_message=lambda: "late"))
        self.assertEqual(b.exit_code, 0)

    def test_a_refused_channel_is_exit_1_with_the_reason(self):
        s = Session()
        b = bridge.Bridge(mock.Mock(), ADDRESS, mock.Mock())
        b.device = mock.Mock()
        b.connect()
        failed = b.device.ConnectProfile.call_args.kwargs["error_handler"]
        failed(types.SimpleNamespace(get_dbus_message=lambda: "Connection refused"))
        self.assertEqual(b.exit_code, bridge.EXIT_TRANSIENT)
        self.assertIn("Connection refused", s.lines[-1]["error"])

    def test_an_unpaired_address_is_exit_1(self):
        s = Session()
        b = bridge.Bridge(mock.Mock(), ADDRESS, mock.Mock())
        with mock.patch.object(bridge, "find_device_path", return_value=None):
            b.register()
        self.assertEqual(b.exit_code, bridge.EXIT_TRANSIENT)
        self.assertIn(ADDRESS, s.lines[-1]["error"])

    def test_cleanup_closes_the_socket_and_the_profile(self):
        b = bridge.Bridge(mock.Mock(), ADDRESS, mock.Mock())
        b.fd = 42
        b.manager = mock.Mock()
        b.registered = True
        with mock.patch.object(bridge.os, "close") as close:
            b.unregister()
        close.assert_called_once_with(42)
        b.manager.UnregisterProfile.assert_called_once()
        self.assertIsNone(b.fd)
        self.assertFalse(b.registered)

    def test_a_second_channel_is_closed_and_ignored(self):
        b = bridge.Bridge(mock.Mock(), ADDRESS, mock.Mock())
        b.fd = 7
        with mock.patch.object(bridge.os, "close") as close:
            b.opened(8)
        close.assert_called_once_with(8)
        self.assertEqual(b.fd, 7)

    def test_a_closed_stdout_is_a_clean_stop(self):
        # A Session replaces emit on the shared module; the real one is what a
        # closed pipe has to survive, so it comes from a fresh load.
        real = harness.load_bridge("redmi-bridge")
        closed = mock.Mock(write=mock.Mock(side_effect=BrokenPipeError))
        with mock.patch.object(real.sys, "stdout", closed), \
                mock.patch.object(real.os, "_exit", side_effect=SystemExit) as leave:
            with self.assertRaises(SystemExit):
                real.emit({"modes": True})
        leave.assert_called_once_with(0)


class Stdin(unittest.TestCase):
    def read(self, s, *chunks):
        reads = list(chunks)
        with mock.patch.object(bridge.os, "read", side_effect=lambda fd, n: reads.pop(0)):
            result = True
            for _ in chunks:
                result = s.bridge.on_stdin(0, bridge.GLib.IO_IN)
            return result

    def ready(self):
        s = Session()
        s.do_open()
        s.device(NOISE_AMBIENT.hex(" "))
        s.frames.clear()
        return s

    def test_a_command_in_two_reads(self):
        s = self.ready()
        self.read(s, b"set a", b"nc\n")
        self.assertEqual(len(s.frames), 1)

    def test_eof_is_a_clean_stop(self):
        s = self.ready()
        self.assertFalse(self.read(s, b""))
        self.assertEqual(s.bridge.exit_code, 0)

    def test_a_hangup_is_a_clean_stop(self):
        s = self.ready()
        self.assertFalse(s.bridge.on_stdin(0, bridge.GLib.IO_HUP))
        self.assertEqual(s.bridge.exit_code, 0)


class Arguments(unittest.TestCase):
    def run_main(self, argv):
        with mock.patch.object(bridge.sys, "argv", ["redmi-bridge"] + argv):
            lines = []
            with mock.patch.object(bridge, "emit", lines.append):
                with self.assertRaises(SystemExit) as raised:
                    bridge.main()
        return raised.exception.code, lines

    def test_no_address_is_exit_4(self):
        code, lines = self.run_main([])
        self.assertEqual(code, bridge.EXIT_SETUP)
        self.assertFalse(lines[0]["modes"])

    def test_a_string_that_is_not_an_address_is_exit_4_and_never_reaches_bluez(self):
        for bad in ("nope", "AA:BB:CC:00:00", "AA:BB:CC:00:00:0G", "; rm -rf /",
                    "AA:BB:CC:00:00:01 --evil"):
            code, lines = self.run_main([bad])
            self.assertEqual(code, bridge.EXIT_SETUP, bad)

    def test_help_prints_usage_and_exits_0(self):
        with mock.patch.object(bridge.sys, "argv", ["redmi-bridge", "--help"]), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(bridge.main(), 0)
        self.assertIn("usage: redmi-bridge", printed.getvalue())


class Silent(unittest.TestCase):
    def test_a_noise_answer_alone_is_an_answer(self):
        s = Session()
        s.do_open()
        s.device(NOISE_AMBIENT.hex(" "))
        s.bridge.first_answer_timeout()
        self.assertIsNone(s.bridge.exit_code)


def sample(minute, left, right, case=100, bits=0):
    return [minute * 60.0, left, right, case, bits]


class Estimate(unittest.TestCase):
    def test_a_steady_drain_gives_the_time_left(self):
        # Left loses five points every 30 minutes: 10 points an hour, so at 60
        # it has six hours. Right is slower, so it has longer.
        samples = [sample(0, 75, 95), sample(30, 70, 95), sample(60, 65, 90),
                   sample(90, 60, 90)]
        self.assertEqual(bridge.remaining_minutes(samples, 1), 360)
        self.assertGreater(bridge.remaining_minutes(samples, 2), 360)

    def test_too_little_history_says_nothing(self):
        self.assertIsNone(bridge.remaining_minutes([sample(0, 75, 80)], 1))
        self.assertIsNone(bridge.remaining_minutes(
            [sample(0, 75, 80), sample(20, 70, 80)], 1))  # under half an hour
        self.assertIsNone(bridge.remaining_minutes(
            [sample(0, 75, 80), sample(40, 75, 80)], 1))  # no step yet

    def test_a_flat_stretch_after_the_last_step_pulls_the_rate_down(self):
        steady = [sample(0, 80, 80), sample(30, 75, 80), sample(60, 70, 80)]
        later = steady + [sample(80, 70, 80), sample(100, 70, 80)]
        self.assertLess(bridge.remaining_minutes(steady, 1) or 10**9,
                        bridge.remaining_minutes(later, 1))

    def test_charging_breaks_the_stretch(self):
        samples = [sample(0, 50, 80), sample(30, 55, 80, bits=1),
                   sample(60, 70, 80, bits=1), sample(90, 70, 80),
                   sample(120, 65, 80)]
        # Only the last two samples are discharging: under half an hour... no,
        # an hour of it would be needed, and there is 30 minutes and one step.
        self.assertEqual(bridge.remaining_minutes(samples, 1), 390)

    def test_a_bud_that_is_charging_now_has_no_estimate(self):
        samples = [sample(0, 75, 80), sample(30, 70, 80), sample(60, 70, 80, bits=1)]
        self.assertIsNone(bridge.remaining_minutes(samples, 1))

    def test_a_gap_in_the_history_breaks_the_stretch(self):
        # Hours with nothing recorded between 30 and 300: the pair was away,
        # and what it did meanwhile is not a rate. What follows is 15 minutes.
        samples = [sample(0, 90, 90), sample(30, 85, 90), sample(300, 60, 90),
                   sample(315, 55, 90)]
        self.assertIsNone(bridge.remaining_minutes(samples, 1))

    def test_a_bud_that_stops_reporting_breaks_the_stretch(self):
        samples = [sample(0, 90, 90), sample(30, 85, 90), sample(60, -1, 90),
                   sample(75, 80, 90), sample(90, 75, 90)]
        self.assertIsNone(bridge.remaining_minutes(samples, 1))

    def test_a_level_that_rose_is_a_charge_not_a_drain(self):
        samples = [sample(0, 60, 90), sample(30, 70, 90), sample(60, 65, 90),
                   sample(90, 60, 90)]
        self.assertEqual(bridge.remaining_minutes(samples, 1), 360)

    def test_only_the_recent_hours_count(self):
        # Slow for the first stretch, then quick for the last few hours.
        samples = [sample(m, 100 - m // 20, 90) for m in range(0, 300, 30)]
        quick = [sample(300 + m, 87 - m // 6, 90) for m in range(0, 180, 30)]
        slow_only = bridge.remaining_minutes(samples, 1)
        both = bridge.remaining_minutes(samples + quick, 1)
        self.assertLess(both, slow_only)

    def test_an_absurdly_long_estimate_is_not_offered(self):
        samples = [sample(0, 100, 100), sample(1000, 95, 100)]
        self.assertIsNone(bridge.remaining_minutes(samples, 1))

    def test_estimates_come_for_the_buds_and_not_the_case(self):
        samples = [sample(0, 75, 95), sample(30, 70, 92), sample(60, 65, 90)]
        self.assertEqual(set(bridge.estimates_from(samples)), {"left", "right"})


class History(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "omaphones", "redmi-battery.json")
        self.now = [1000.0]

    def bridge(self):
        s = Session.__new__(Session)
        harness.Session.__init__(s, bridge)
        s.bridge = bridge.Bridge(None, ADDRESS, harness.FakeLoop(),
                                 history_file=self.path, clock=lambda: self.now[0])
        s.bridge.write = s.frames.append
        return s

    def test_a_change_is_recorded_and_saved(self):
        s = self.bridge()
        s.device(ANSWER.hex(" "))
        with open(self.path) as handle:
            data = json.load(handle)
        self.assertEqual(data["address"], ADDRESS)
        self.assertEqual(data["samples"], [[1000.0, 75, 80, 100, 0]])

    def test_an_unchanged_reading_is_not_recorded_again_until_it_is_time(self):
        s = self.bridge()
        s.device(ANSWER.hex(" "))
        self.now[0] += 60
        s.device(ANSWER.hex(" "))
        self.assertEqual(len(s.bridge.samples), 1)
        self.now[0] += bridge.SAMPLE_EVERY_SECONDS
        s.device(ANSWER.hex(" "))
        self.assertEqual(len(s.bridge.samples), 2)

    def test_history_survives_a_restart_and_gives_an_estimate_at_once(self):
        s = self.bridge()
        for minutes, level in ((0, 80), (30, 75), (60, 70)):
            self.now[0] = 1000.0 + minutes * 60
            frame = bytearray(ANSWER)
            frame[frame.index(b"\x04\x07") + 2] = level
            s.device(bytes(frame).hex(" "))
        s.bridge.grace_timeout()
        self.assertEqual(s.lines[-1]["estimate"]["left"], 420)
        again = self.bridge()
        self.assertEqual(again.bridge.estimate["left"], 420)

    def test_another_pairs_history_is_not_read(self):
        s = self.bridge()
        s.device(ANSWER.hex(" "))
        other = bridge.load_history(self.path, "00:11:22:33:44:55", 1000.0)
        self.assertEqual(other, [])

    def test_old_and_damaged_history_is_dropped(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            json.dump({"address": ADDRESS, "samples": [
                [1.0, 50, 50, 50, 0], "junk", [1000.0, 70, 70, 100],
                [900.0, 70, 71, 100, 0]]}, handle)
        self.assertEqual(bridge.load_history(self.path, ADDRESS, 1000.0 + 8 * 24 * 3600),
                         [])
        self.assertEqual(bridge.load_history(self.path, ADDRESS, 1000.0),
                         [[1.0, 50, 50, 50, 0], [900.0, 70, 71, 100, 0]])

    def test_an_unreadable_file_is_an_empty_history(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            handle.write("{not json")
        self.assertEqual(bridge.load_history(self.path, ADDRESS, 1000.0), [])


if __name__ == "__main__":
    unittest.main()
