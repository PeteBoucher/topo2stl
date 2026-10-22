import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import topo2stl as topo
from tileset_preview import read_binary_stl


class PinTests(unittest.TestCase):
    def test_parses_repeatable_pins(self):
        pins = topo._parse_pins(
            ["0.25,0.75", " 0.5 , 0.4 "], (0.0, 0.0, 1.0, 1.0))
        self.assertEqual(pins, [
            {"lat": 0.25, "lon": 0.75},
            {"lat": 0.5, "lon": 0.4},
        ])

    def test_rejects_bad_or_out_of_bounds_coordinates(self):
        bbox = (0.0, 0.0, 1.0, 1.0)
        for spec in ("0.5", "0.5,0.5,1", "north,0.5", "nan,0.5",
                     "2,0.5", "0.5,181"):
            with self.subTest(spec=spec), self.assertRaises(SystemExit):
                topo._parse_pins([spec], bbox)

    def test_requires_manifold_before_terrain_work(self):
        with mock.patch.object(topo, "_have_manifold", return_value=False):
            with self.assertRaisesRegex(SystemExit, "--pin needs manifold3d"):
                topo._parse_pins(["0.5,0.5"], (0.0, 0.0, 1.0, 1.0))

    def test_prepares_fixed_size_printable_pin(self):
        pins = topo._parse_pins(["0.5,0.25"], (0.0, 0.0, 1.0, 1.0))
        placed = topo._prepare_pins(
            pins, (0.0, 0.0, 1.0, 1.0), 100.0, 80.0,
            np.full((3, 3), 5.0),
        )

        lo, hi = placed[0]["bounds"]
        np.testing.assert_allclose(lo, [23.5, 38.5, 4.7], atol=1e-5)
        np.testing.assert_allclose(hi, [26.5, 41.5, 8.0], atol=1e-5)
        manifold = topo._soup_to_manifold(placed[0]["tris"])
        self.assertEqual(str(manifold.status()), "Error.NoError")

    def test_pin_crossing_tile_seam_is_clipped_and_fused_on_both_tiles(self):
        pins = topo._parse_pins(["0.5,0.5"], (0.0, 0.0, 1.0, 1.0))
        placed = topo._prepare_pins(
            pins, (0.0, 0.0, 1.0, 1.0), 100.0, 100.0,
            np.full((3, 5), 5.0),
        )

        for x_offset in (0.0, 50.0):
            base, _ = topo._mesh_from_z(
                np.linspace(0.0, 50.0, 3),
                np.linspace(0.0, 100.0, 3)[::-1],
                np.full((3, 3), 5.0),
            )
            result = topo.add_pins(base, placed, x_offset, 0.0, 50.0, 100.0)
            verts = result.reshape(-1, 3)
            self.assertGreater(float(verts[:, 2].max()), 5.0)
            self.assertGreaterEqual(float(verts[:, 0].min()), -1e-5)
            self.assertLessEqual(float(verts[:, 0].max()), 50.0 + 1e-5)
            manifold = topo._soup_to_manifold(result)
            self.assertEqual(str(manifold.status()), "Error.NoError")
            self.assertEqual(len(manifold.decompose()), 1)

    def test_main_writes_pin_into_finished_model_and_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "map.stl"
            with mock.patch.object(
                    topo, "cached_grid",
                    return_value=np.zeros((3, 3), dtype=float)):
                topo.main([
                    "--bbox", "0,0,1,1", "--grid", "3x3",
                    "--model-width", "100", "--base", "5", "--smooth", "0",
                    "--pin", "0.5,0.5", "-o", str(output),
                ])

            result = read_binary_stl(output)
            self.assertAlmostEqual(float(result[:, :, 2].max()), 8.0, places=4)
            meta = json.loads(output.with_name("map.topo.json").read_text())
            self.assertEqual(meta["pins"], [[0.5, 0.5]])


if __name__ == "__main__":
    unittest.main()
