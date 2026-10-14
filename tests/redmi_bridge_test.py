"""What redmi-bridge asks Redmi Buds 5, how it reads the answer, and what it
works out from the battery history.

    python -m unittest tests.redmi_bridge_test

The frozen session is tests/pins/redmi/buds-5.json. The frames here are
verbatim what a Redmi Buds 5 sent (docs/captures/redmi-buds-5.txt): left 75,
right 80, case 100, confirmed against the Xiaomi Earbuds app, and the noise
control going transparency -> cancelling -> off -> transparency. No hardware
and no socket.
"""
import json
import os
import tempfile
import unittest

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
        self.bridge = bridge.Bridge(None, ADDRESS, harness.FakeLoop(),
                                    clock=lambda: 1000.0)
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

    def test_noise_set_is_the_config_the_app_writes(self):
        # The three the app wrote in the capture, sn 2, 4, 6, 8.
        self.assertEqual(bridge.encode_set_noise(2, 2, 0).hex(" "),
                         "fe dc ba c0 f2 00 06 02 04 00 0b 02 00 ef")
        self.assertEqual(bridge.encode_set_noise(4, 1, 0).hex(" "),
                         "fe dc ba c0 f2 00 06 04 04 00 0b 01 00 ef")
        self.assertEqual(bridge.encode_set_noise(6, 0, 0).hex(" "),
                         "fe dc ba c0 f2 00 06 06 04 00 0b 00 00 ef")

    def test_sequence_number_wraps_to_a_byte(self):
        self.assertEqual(bridge.encode_get_target_info(0x101)[7], 0x01)


class BatteryTest(unittest.TestCase):
    def test_left_right_case(self):
        self.assertEqual(bridge.battery_of(ANSWER),
                         {"left": 75, "right": 80, "case": 100, "charging": []})

    def test_charging_bit_and_no_reading(self):
        self.assertEqual(bridge.battery_from([0xCB, 0x50, 0xFF]),
                         {"left": 75, "right": 80, "charging": ["left"]})

    def test_nothing_reported_is_none(self):
        self.assertIsNone(bridge.battery_from([0xFF, 0xFF, 0xFF]))

    def test_out_of_range_level_is_dropped(self):
        self.assertEqual(bridge.battery_from([0x7F, 0x50, 0x64]),
                         {"right": 80, "case": 100, "charging": []})

    def test_status_report_the_earbuds_send_on_a_change(self):
        report = bytes.fromhex("fe dc ba 80 0e 00 06 07 04 00 4b 50 64 ef")
        self.assertEqual(bridge.battery_of(report),
                         {"left": 75, "right": 80, "case": 100, "charging": []})

    def test_an_error_status_carries_no_battery(self):
        failed = bytearray(ANSWER)
        failed[7] = 0x01
        self.assertIsNone(bridge.battery_of(bytes(failed)))

    def test_other_opcodes_are_ignored(self):
        other = bytearray(ANSWER)
        other[4] = 0x05
        self.assertIsNone(bridge.battery_of(bytes(other)))


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

    def test_an_unknown_state_is_not_a_mode(self):
        odd = bytearray(NOISE_ANC)
        odd[12] = 0x09
        self.assertIsNone(bridge.noise_of(bytes(odd)))

    def test_another_config_is_not_the_noise_control(self):
        other = bytearray(NOISE_ANC)
        other[11] = 0x25
        self.assertIsNone(bridge.noise_of(bytes(other)))


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

    def test_unknown_commands_are_ignored(self):
        s = self.ready()
        for line in ("set talkthru", "set", "level 3", "set anc now", ""):
            s.command(line)
        self.assertEqual(s.frames, [])

    def test_a_set_is_followed_by_one_more_get(self):
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
        s.bridge.grace_timeout()
        self.assertEqual(s.lines[-1], {"modes": True, "available": [],
                                       "battery": {"left": 75, "right": 80, "case": 100,
                                                   "charging": []}})


class Poll(unittest.TestCase):
    def test_the_poll_asks_for_the_mode_and_the_battery_again(self):
        s = Session()
        s.do_open()
        s.frames.clear()
        self.assertTrue(s.bridge.poll())
        self.assertEqual([f[4] for f in s.frames], [0xF3, 0x02])
        self.assertEqual([f[7] for f in s.frames], [3, 4])


class Silent(unittest.TestCase):
    def test_silence_after_the_ask_parks_the_address(self):
        s = Session()
        s.do_open()
        s.bridge.first_answer_timeout()
        self.assertEqual(s.bridge.exit_code, bridge.EXIT_UNSUPPORTED)

    def test_an_answer_stops_the_silence_check(self):
        s = Session()
        s.do_open()
        s.device(ANSWER.hex(" "))
        s.bridge.first_answer_timeout()
        self.assertIsNone(s.bridge.exit_code)

    def test_a_noise_answer_alone_is_an_answer_too(self):
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
