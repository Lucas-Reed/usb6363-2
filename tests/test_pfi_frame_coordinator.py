import unittest

from two_peak.pfi_frame_coordinator import PfiFrameCoordinator
from two_peak.pfi_monitor import PfiMonitorEvent


class PfiFrameCoordinatorTests(unittest.TestCase):
    def test_frame_is_delayed_and_rejected_on_pfi1_change(self):
        accepted = []
        rejected = []
        coordinator = PfiFrameCoordinator(accepted.append, lambda frame, reason: rejected.append((frame, reason)), enabled=True)
        coordinator.on_counter_event(PfiMonitorEvent(0, 1, 4, True, False))
        self.assertEqual(coordinator.submit_frame({"frame_id": 1}), [])
        coordinator.on_counter_event(PfiMonitorEvent(1, 2, 5, True, True))
        self.assertEqual(coordinator.submit_frame({"frame_id": 2}), [])
        self.assertEqual(rejected[0][0]["frame_id"], 1)

    def test_clean_frame_is_accepted_on_next_boundary(self):
        coordinator = PfiFrameCoordinator(enabled=True)
        coordinator.on_counter_event(PfiMonitorEvent(0, 1, 4, True, False))
        coordinator.submit_frame({"frame_id": 1})
        coordinator.on_counter_event(PfiMonitorEvent(1, 2, 4, True, False))
        accepted = coordinator.submit_frame({"frame_id": 2})
        self.assertEqual(accepted[0]["frame_id"], 1)


if __name__ == "__main__":
    unittest.main()
