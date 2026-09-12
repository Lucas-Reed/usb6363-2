import unittest

from two_peak.frame_admission import FrameAdmissionGate


class FrameAdmissionTests(unittest.TestCase):
    def test_accepts_previous_frame_after_clean_boundary(self):
        gate = FrameAdmissionGate()
        self.assertEqual(gate.push({"frame_id": 1}, pfi1_count=4), [])
        accepted = gate.push({"frame_id": 2}, pfi1_count=4)
        self.assertEqual([frame["frame_id"] for frame in accepted], [1])

    def test_rejects_previous_frame_when_pfi1_count_changed(self):
        gate = FrameAdmissionGate()
        gate.push({"frame_id": 1}, pfi1_count=4)
        self.assertEqual(gate.push({"frame_id": 2}, pfi1_count=5), [])
        self.assertEqual(gate.rejected_frame_ids, [1])

    def test_disabled_gate_is_immediate(self):
        gate = FrameAdmissionGate(enabled=False)
        self.assertEqual(gate.push({"frame_id": 1}, pfi1_count=4)[0]["frame_id"], 1)


if __name__ == "__main__":
    unittest.main()
