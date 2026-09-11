import unittest
from uuid import uuid4
import numpy as np

from daq_app.contracts import BlockMeta, SessionInfo, TriggerEvent
from daq_app.sample_buffer import Expired, NotYetAvailable, SampleBuffer
from daq_app.windows import WindowRoute, WindowScheduler
from daq_app.processing import SlowDriftProcessor, TwoPeakProcessor, auto_identify_two_peaks_placeholder
from daq_app.control import AoManager, PiController


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.session = SessionInfo(uuid4(), ("ai0", "ai1"), 100000.0)
        self.buffer = SampleBuffer(self.session, 1000)

    def append(self, start, count):
        values = np.vstack([np.arange(start, start + count), np.arange(start, start + count) + 100])
        self.buffer.append(BlockMeta(self.session.session_id, start // count, start, count, self.session.rate_hz), values)

    def test_ring_bounds_and_expiry(self):
        self.append(0, 1500)
        self.assertEqual(self.buffer.bounds(), (500, 1500))
        with self.assertRaises(Expired): self.buffer.copy_range(self.session.session_id, 0, 1)
        with self.assertRaises(NotYetAvailable): self.buffer.copy_range(self.session.session_id, 1500, 1501)
        np.testing.assert_array_equal(self.buffer.copy_range(self.session.session_id, 500, 502)[0], [500, 501])

    def test_one_and_hundred_ms_windows(self):
        self.buffer = SampleBuffer(self.session, 20000)
        self.append(0, 20000)
        scheduler = WindowScheduler(self.session, self.buffer)
        scheduler.add_trigger(TriggerEvent(self.session.session_id, "PFI0", 1, 10000), WindowRoute("one", "PFI0", 0, .001, ("ai0",)))
        scheduler.add_trigger(TriggerEvent(self.session.session_id, "PFI0", 2, 10000), WindowRoute("hundred", "PFI0", 0, .1, ("ai0",)))
        windows = scheduler.poll()
        self.assertEqual([w.end_sample - w.start_sample for w in windows], [100, 10000])

    def test_known_peaks_and_placeholder(self):
        signal = np.zeros(1000); signal[300] = 3; signal[700] = 4
        values = np.vstack([signal, signal])
        scheduler = WindowScheduler(self.session, self.buffer)
        window = __import__('daq_app.contracts', fromlist=['Window']).Window(self.session.session_id, "w", 0, 1000, self.session.rate_hz, ("ai0", "ai1"), values)
        result = TwoPeakProcessor("ai0", (300, 700), search_half=5, smooth_window=0).process(window)
        self.assertEqual(result.values["peak_a_index"], 300)
        self.assertEqual(result.values["peak_b_index"], 700)
        with self.assertRaises(NotImplementedError): auto_identify_two_peaks_placeholder()

    def test_slow_drift_pi_and_ao_ownership(self):
        values = np.ones((2, 100)) * 2
        window = __import__('daq_app.contracts', fromlist=['Window']).Window(self.session.session_id, "w", 0, 100, self.session.rate_hz, ("ai0", "ai1"), values)
        result = SlowDriftProcessor("ai0").process(window)
        self.assertEqual(result.values["mean"], 2.0)
        self.assertAlmostEqual(PiController(1, 1).update(0.5, .1), .5)
        manager = AoManager(); lease = manager.acquire("lock", ("ao0",))
        with self.assertRaises(RuntimeError): manager.acquire("scan", ("ao0",))
        self.assertEqual(manager.proposal(lease, {"ao0": 1})["values"]["ao0"], 1)
        manager.release(lease)


if __name__ == "__main__": unittest.main()
