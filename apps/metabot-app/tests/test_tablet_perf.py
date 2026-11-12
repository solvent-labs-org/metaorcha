"""tools/tablet_perf.py: the framestats parser and the budget math. The fixture is
the real `dumpsys gfxinfo framestats` schema captured from the tablet (2026-11-12):
the new column set with FrameTimelineVsyncId and InputEventId, no OldestInputEvent."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import tablet_perf as tp  # noqa: E402

HEADER = ("Flags,FrameTimelineVsyncId,IntendedVsync,Vsync,InputEventId,HandleInputStart,"
          "AnimationStart,PerformTraversalsStart,DrawStart,FrameDeadline,FrameStartTime,"
          "FrameInterval,WorkloadTarget,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,"
          "FrameCompleted,DequeueBufferDuration,QueueBufferDuration,GpuCompleted,"
          "SwapBuffersCompleted,DisplayPresentTime,CommandSubmissionCompleted,")


def row(flags, vid, vsync, input_id, completed, present):
    cells = {name: "0" for name in HEADER.split(",") if name}
    cells.update({"Flags": str(flags), "FrameTimelineVsyncId": str(vid), "Vsync": str(vsync),
                  "IntendedVsync": str(vsync), "InputEventId": str(input_id),
                  "HandleInputStart": str(vsync + 100), "FrameCompleted": str(completed),
                  "DisplayPresentTime": str(present)})
    return ",".join(cells[name] for name in HEADER.split(",") if name) + ","


V = 73327525449400
DUMP = ("some earlier prose\n---PROFILEDATA---\n" + HEADER + "\n"
        + row(0, 1, V, 0, V + 20000000, V + 30000000) + "\n"
        + row(0, 2, V + 16600000, 11, V + 36600000, V + 46600000) + "\n"
        + row(0, 3, V + 33200000, 0, V + 53200000, 0) + "\n"   # no present time: FrameCompleted wins
        + "---PROFILEDATA---\ntrailer\n")


class ParseTests(unittest.TestCase):
    def test_rows_come_out_in_vsync_order_with_the_fields_we_read(self):
        rows = tp.parse_profiledata(DUMP)
        self.assertEqual([r["InputEventId"] for r in rows], [0, 11, 0])
        self.assertEqual(rows[1]["FrameCompleted"], V + 36600000)

    def test_a_repeated_block_is_deduped_by_vsync(self):
        rows = tp.parse_profiledata(DUMP + DUMP)
        self.assertEqual(len(rows), 3)

    def test_junk_lines_and_short_rows_are_skipped(self):
        text = "---PROFILEDATA---\n" + HEADER + "\nnot,a,row\n1,2\n" + row(0, 9, V + 9, 0, V + 10, V + 11) + "\n---PROFILEDATA---\n"
        rows = tp.parse_profiledata(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Vsync"], V + 9)

    def test_a_missing_column_fails_closed(self):
        bad = DUMP.replace("DisplayPresentTime", "SomethingElse")
        self.assertEqual(tp.parse_profiledata(bad), [])


class BudgetMathTests(unittest.TestCase):
    def test_scroll_fps_is_the_median_delta(self):
        rows = tp.parse_profiledata(DUMP)
        fps, n = tp.scroll_fps(rows)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(fps, 1e9 / 16600000, places=3)   # 60.24 fps at 16.6 ms

    def test_the_watermark_excludes_old_frames(self):
        rows = tp.parse_profiledata(DUMP)
        fps, n = tp.scroll_fps(rows, since_vsync=V + 16600000)
        self.assertEqual((fps, n), (None, 0))

    def test_key_latencies_stay_on_the_frame_clock(self):
        rows = tp.parse_profiledata(DUMP)
        keys = [(123, 456)]   # /proc/uptime windows: recorded, never in the math (suspend time)
        lat, matched = tp.key_latencies(rows, keys)
        self.assertEqual(matched, 1)
        self.assertEqual(len(lat), 1)
        # frame 2 consumed the key: present V+46600000 minus HandleInputStart V+16600100
        self.assertAlmostEqual(lat[0]["ms"], (V + 46600000 - (V + 16600100)) / 1e6, places=3)
        self.assertAlmostEqual(lat[0]["window_ms"], 0.000333, places=3)

    def test_frames_without_handle_input_start_are_not_matched(self):
        rows = tp.parse_profiledata(DUMP.replace(str(V + 16600100), "0", 1))
        # the one input frame lost its HandleInputStart: nothing to subtract, no latency
        lat, matched = tp.key_latencies(rows, [(1, 2)])
        self.assertEqual((matched, len(lat)), (0, 0))

    def test_frame_completed_stands_in_when_present_is_zero(self):
        self.assertEqual(tp.present_ns({"DisplayPresentTime": 0, "FrameCompleted": 5}), 5)
        self.assertEqual(tp.present_ns({"DisplayPresentTime": 7, "FrameCompleted": 5}), 7)

    def test_percentiles(self):
        self.assertEqual(tp.median([3, 1, 2]), 2)
        self.assertEqual(tp.percentile(list(range(100)), 95), 94)   # nearest-rank
        self.assertEqual(tp.percentile([5, 1, 9], 50), 5)
        self.assertIsNone(tp.median([]))
        self.assertIsNone(tp.percentile([], 95))


if __name__ == "__main__":
    unittest.main()
