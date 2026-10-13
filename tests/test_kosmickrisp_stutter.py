"""Check stutter attribution on synthetic present and compile logs."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "runtime/kosmickrisp/stutter.py"
spec = importlib.util.spec_from_file_location("stutter", SCRIPT)
stutter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stutter)

MS = 1_000_000


def presents(frame_ms):
    t, starts = 0, [0]
    for ms in frame_ms:
        t += int(ms * MS)
        starts.append(t)
    return starts


class StutterTests(unittest.TestCase):
    def test_steady_frames_have_no_hitches(self):
        result = stutter.analyze_frames(presents([16.7] * 600), [])
        self.assertEqual(result["hitches"], 0)
        self.assertAlmostEqual(result["mean_fps"], 59.9, delta=0.2)
        self.assertAlmostEqual(result["median_ms"], 16.7, delta=0.01)

    def test_draw_compile_explains_hitch(self):
        frames = [16.7] * 100 + [120.0] + [16.7] * 100
        starts = presents(frames)
        hitch_start = starts[100]
        events = [{"kind": "pso", "origin": "draw", "start": hitch_start + 5 * MS,
                   "end": hitch_start + 100 * MS}]
        result = stutter.analyze_frames(starts, events)
        self.assertEqual(result["hitches"], 1)
        worst = result["worst_hitches"][0]
        self.assertEqual(worst["ms"], 120.0)
        self.assertAlmostEqual(worst["draw_compile_ms"], 95.0)
        self.assertTrue(worst["explained"])
        self.assertEqual(result["hitches_explained_by_compiles"], 1)

    def test_prewarm_compile_does_not_explain_hitch(self):
        frames = [16.7] * 100 + [120.0] + [16.7] * 100
        starts = presents(frames)
        events = [{"kind": "pso", "origin": "prewarm", "start": starts[100],
                   "end": starts[101]}]
        result = stutter.analyze_frames(starts, events)
        self.assertFalse(result["worst_hitches"][0]["explained"])

    def test_variant_waits_explain_draw_hitches(self):
        starts = presents([16.7] * 100 + [120.0] + [16.7] * 100)
        for kind in ("variant_wait", "fs_variant_wait"):
            events = [{"kind": kind, "origin": "draw", "start": starts[100],
                       "end": starts[100] + 100 * MS}]
            result = stutter.analyze_frames(starts, events)
            self.assertTrue(result["worst_hitches"][0]["explained"])

    def test_create_and_draw_overlap_is_not_added_twice(self):
        starts = presents([16.7] * 100 + [120.0] + [16.7] * 100)
        events = [{"kind": kind, "origin": origin, "start": starts[100],
                   "end": starts[100] + 30 * MS}
                  for kind, origin in (("variant_wait", "draw"),
                                       ("shaders", "create"))]
        result = stutter.analyze_frames(starts, events)["worst_hitches"][0]
        self.assertEqual(result["compile_overlap_ms"], 30)
        self.assertFalse(result["explained"])

    def test_parallel_compiles_count_wall_clock_once(self):
        frames = [16.7] * 100 + [200.0] + [16.7] * 100
        starts = presents(frames)
        a = starts[100]
        events = [{"kind": "shaders", "origin": "create", "start": a, "end": a + 150 * MS},
                  {"kind": "shaders", "origin": "create", "start": a + 50 * MS,
                   "end": a + 180 * MS},
                  {"kind": "shaders", "origin": "create", "start": a - 10 * MS,
                   "end": a + 20 * MS}]
        worst = stutter.analyze_frames(starts, events)["worst_hitches"][0]
        self.assertAlmostEqual(worst["create_compile_ms"], 180.0)

    def test_skip_seconds_drops_loading_frames(self):
        frames = [500.0] * 4 + [16.7] * 300
        result = stutter.analyze_frames(presents(frames), [], skip_seconds=2.5)
        self.assertEqual(result["hitches"], 0)

    def test_run_directory_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "dxvk").mkdir()
            starts = presents([16.7] * 50 + [80.0] + [16.7] * 50)
            (run / "present.csv").write_text(
                "".join(f"{s},{s + 1000},0,1\n" for s in starts))
            (run / "compile.csv").write_text(
                f"fs_variant,draw,{starts[50]},{starts[50] + 70 * MS}\n"
                f"shaders,create,0,{3 * MS}\n"
                "garbage line\n")
            (run / "dxvk" / "game_d3d9.log").write_text(
                "info:  DXVK: v3.1.1\n"
                "info:  Graphics pipeline libraries supported\n")
            summary = stutter.analyze_run(run, 0.0)
            self.assertTrue((run / "summary.json").is_file())
            self.assertEqual(summary["dxvk"]["version"], "v3.1.1")
            self.assertTrue(summary["dxvk"]["gpl"])
            self.assertEqual(summary["compiles"]["fs_variant/draw"]["count"], 1)
            self.assertEqual(summary["compiles"]["fs_variant/draw"]["max_ms"], 70.0)
            self.assertEqual(summary["frames"]["hitches"], 1)
            self.assertEqual(summary["prewarm_events"], 0)

    def test_dxvk_rejection_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "game_d3d9.log").write_text(
                "info:  Graphics pipeline libraries not supported\n"
                "err:   Device does not support required feature 'fillModeNonSolid'\n")
            (directory / "other_d3d9.log").write_text("info:  DXVK: native-1.9.1a-4-g37690dde+\n")
            result = stutter.parse_dxvk_logs(directory)
            self.assertEqual(result["version"], "native-1.9.1a-4-g37690dde+")
            self.assertFalse(result["gpl"])
            self.assertEqual(len(result["rejections"]), 1)
            self.assertEqual(result["errors"], 1)


if __name__ == "__main__":
    unittest.main()
