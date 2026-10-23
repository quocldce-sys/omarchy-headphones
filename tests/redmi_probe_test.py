"""What tools/redmi_probe.py writes to Redmi Buds 5, and whether it puts them back.

    python -m unittest tests.redmi_probe_test

The probe reads by default. With --cycle it sets modes, and then what matters
is that it does not write unless the initial mode is known, that every set is
checked against what the earbuds report, and that the mode they were in is set
again and checked, on every way out. The earbuds here are a fake that answers
the way the real ones did in docs/captures/redmi-buds-5.txt, on a fake clock.
"""
import contextlib
import importlib.machinery
import io as stdio
import importlib.util
import os
import unittest

from tests import harness

ROOT = harness.ROOT
bridge = harness.load_bridge("redmi-bridge")


def load_probe():
    path = os.path.join(ROOT, "tools", "redmi_probe.py")
    loader = importlib.machinery.SourceFileLoader("redmi_probe", path)
    spec = importlib.util.spec_from_loader("redmi_probe", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


probe = load_probe()

# GetTargetInfo answer, as the earbuds sent it (75, 80, 100).
ANSWER = bytes.fromhex(
    "fe dc ba 00 02 00 2c 00 01 05 01 10 38 10 38 05 03 27 17 50 6a 02 04 01 "
    "02 05 00 03 06 10 38 04 07 4b 50 64 02 08 02 02 09 00 02 0c 00 02 0d 02 "
    "02 0e 00 ef")


def noise(state, level=0, sn=1):
    return bytes.fromhex("fe dc ba 00 f3 00 07 00 %02x 04 00 0b %02x %02x ef" % (sn, state, level))


def notify(state, level=0, sn=1):
    return bytes.fromhex("fe dc ba c7 f4 00 06 %02x 04 00 0b %02x %02x ef" % (sn, state, level))


def ack(sn, status=0):
    return bytes.fromhex("fe dc ba 00 f2 00 02 %02x %02x ef" % (status, sn))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Earbuds:
    """A pair of earbuds that answer as the captured ones did. Synthetic in what
    it is made to refuse, to drop or to keep quiet about."""

    def __init__(self, clock, state=2, level=0, announces=True, silent=False,
                 refuse_from=None, drop_after_sets=None, interrupt_on_set=None):
        self.clock = clock
        self.state, self.level = state, level
        self.announces = announces
        self.silent = silent
        self.refuse_from = refuse_from        # refuse the Nth set and on
        self.drop_after_sets = drop_after_sets
        self.interrupt_on_set = interrupt_on_set
        self.sets = []
        self.writes = []
        self.queue = []
        self.dead = False

    def write(self, data):
        self.writes.append(bytes(data))
        frame, _ = bridge.split_frame(bytearray(data))
        opcode, sn = frame[4], frame[7]
        if self.silent:
            return
        if opcode == bridge.OP_GET_CONFIG:
            self.queue.append(noise(self.state, self.level, sn))
        elif opcode == bridge.OP_GET_TARGET_INFO:
            self.queue.append(ANSWER)
        elif opcode == bridge.OP_SET_CONFIG:
            self.sets.append((frame[11], frame[12]))
            if self.interrupt_on_set == len(self.sets):
                self.interrupt_on_set = None
                raise KeyboardInterrupt
            if self.refuse_from is not None and len(self.sets) >= self.refuse_from:
                self.queue.append(ack(sn, status=1))
                return
            self.state, self.level = frame[11], frame[12]
            if self.announces:
                self.queue.append(notify(self.state, self.level, sn))
            self.queue.append(ack(sn))
            if self.drop_after_sets == len(self.sets):
                self.queue.append(None)

    def read(self, timeout):
        if self.dead:
            return None
        if self.queue:
            item = self.queue.pop(0)
            if item is None:
                self.dead = True
            return item
        self.clock.now += timeout
        return b""


def run(modes, listen=1.0, **earbuds):
    clock = Clock()
    io = Earbuds(clock, **earbuds)
    lines = []
    try:
        code = probe.run(bridge, io, modes, listen, lines.append, clock)
    except KeyboardInterrupt:
        code = "interrupted"
    return code, io, lines


def sent_states(io):
    return [state for state, _level in io.sets]


class Reading(unittest.TestCase):
    def test_by_default_it_only_reads(self):
        code, io, lines = run([])
        self.assertEqual(code, probe.EXIT_OK)
        self.assertEqual(io.sets, [])
        self.assertEqual([w[4] for w in io.writes], [bridge.OP_GET_CONFIG, bridge.OP_GET_TARGET_INFO])

    def test_it_prints_both_directions_decoded(self):
        _, _, lines = run([])
        text = "\n".join(lines)
        self.assertIn("TX  fe dc ba c0 f3 00 03 01 00 0b ef", text)
        self.assertIn("noise control ambient (state 2, level 0)", text)
        self.assertIn("battery left 75%, right 80%, case 100%; charging: none", text)

    def test_a_silent_pair_is_reported_not_hung_on(self):
        code, io, lines = run([], silent=True)
        self.assertEqual(code, probe.EXIT_OK)
        self.assertIn("did not report their noise control", "\n".join(lines))

    def test_a_closed_link_is_exit_1(self):
        code, io, lines = run([], drop_after_sets=0)
        io.dead = True
        clock = Clock()
        io2 = Earbuds(clock)
        io2.dead = True
        self.assertEqual(probe.run(bridge, io2, [], 1.0, lines.append, clock), probe.EXIT_LINK)

    def test_the_decoding_of_a_refused_set_and_an_announcement(self):
        p = probe.Probe(bridge, None, lambda line: None)
        self.assertEqual(p.describe(ack(3, status=1)), "set REFUSED (status 1)")
        self.assertEqual(p.describe(ack(3)), "set acknowledged")
        self.assertIn("status report", p.describe(bytes.fromhex("fe dc ba c7 0e 00 04 04 02 04 01 ef")))
        self.assertEqual(p.describe(bytes.fromhex("fe dc ba 00 05 00 02 00 01 ef")), "not read")


class Cycling(unittest.TestCase):
    def test_every_mode_is_set_and_the_initial_one_comes_back(self):
        code, io, lines = run(["off", "anc", "ambient"])
        self.assertEqual(code, probe.EXIT_OK)
        self.assertEqual(sent_states(io), [0, 1, 2, 2])
        self.assertEqual((io.state, io.level), (2, 0))
        self.assertIn("restored: ambient", "\n".join(lines))

    def test_each_set_is_checked_against_what_the_earbuds_say(self):
        _, _, lines = run(["anc"])
        self.assertIn("anc: the earbuds agree", "\n".join(lines))

    def test_without_an_announcement_the_probe_reads_it_back(self):
        code, io, lines = run(["anc"], announces=False)
        self.assertEqual(code, probe.EXIT_OK)
        self.assertIn("read it back", "\n".join(lines))
        self.assertEqual(sent_states(io), [1, 2])

    def test_the_strength_the_earbuds_were_at_is_what_comes_back(self):
        code, io, lines = run(["off"], state=1, level=2)
        self.assertEqual(io.sets, [(0, 0), (1, 2)])
        self.assertEqual(code, probe.EXIT_OK)

    def test_an_unknown_initial_mode_means_nothing_is_written(self):
        code, io, lines = run(["anc"], silent=True)
        self.assertEqual(code, probe.EXIT_NO_INITIAL)
        self.assertEqual(io.sets, [])
        self.assertIn("nothing is written", "\n".join(lines))

    def test_earbuds_that_refuse_cannot_be_restored_and_it_says_so(self):
        code, io, lines = run(["off", "anc"], refuse_from=2)
        self.assertEqual(code, probe.EXIT_NOT_RESTORED)
        self.assertIn("RESTORE FAILED: set ambient by hand", "\n".join(lines))

    def test_a_refusal_is_not_taken_for_agreement(self):
        code, io, lines = run(["off"], refuse_from=1)
        self.assertIn("off: NOT CONFIRMED", "\n".join(lines))

    def test_a_link_that_drops_mid_cycle_says_the_restore_failed(self):
        code, io, lines = run(["off", "anc"], drop_after_sets=1)
        self.assertEqual(code, probe.EXIT_NOT_RESTORED)
        text = "\n".join(lines)
        self.assertIn("RESTORE FAILED: the link closed", text)
        self.assertEqual(sent_states(io), [0])

    def test_a_write_that_fails_is_a_closed_link_not_a_traceback(self):
        clock = Clock()
        io = Earbuds(clock)
        real_write = io.write

        def write(data):
            frame, _ = bridge.split_frame(bytearray(data))
            if frame[4] == bridge.OP_SET_CONFIG:
                raise BrokenPipeError("gone")
            real_write(data)

        io.write = write
        lines = []
        code = probe.run(bridge, io, ["off"], 1.0, lines.append, clock)
        self.assertEqual(code, probe.EXIT_NOT_RESTORED)
        self.assertIn("write failed", "\n".join(lines))

    def test_an_interrupt_still_puts_the_earbuds_back(self):
        code, io, lines = run(["off", "anc"], interrupt_on_set=2)
        self.assertEqual(code, "interrupted")
        self.assertEqual((io.state, io.level), (2, 0))
        self.assertIn("restored: ambient", "\n".join(lines))

    def test_the_restoration_is_verified_not_assumed(self):
        # The earbuds acknowledge the restoring set and then report the other mode.
        clock = Clock()
        io = Earbuds(clock)
        real_write = io.write

        def write(data):
            real_write(data)
            frame, _ = bridge.split_frame(bytearray(data))
            if frame[4] == bridge.OP_SET_CONFIG and len(io.sets) == 2:
                io.queue[:] = [notify(1), ack(frame[7])]

        io.write = write
        lines = []
        code = probe.run(bridge, io, ["anc"], 1.0, lines.append, clock)
        self.assertEqual(code, probe.EXIT_NOT_RESTORED)


class Arguments(unittest.TestCase):
    def test_a_bad_address_or_mode_is_refused_before_any_connection(self):
        for argv in (["nope"], ["AA:BB:CC:00:00:01", "--cycle", "talkthru"]):
            with contextlib.redirect_stderr(stdio.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                probe.main(argv)
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
