import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import topo2stl as topo


def cube_tris(size=(2.0, 4.0, 6.0)):
    return np.asarray(
        topo._box_tris((0, 0, 0), (size[0], 0, 0),
                       (0, size[1], 0), (0, 0, size[2])),
        dtype=np.float32,
    )


def write_ascii_stl(path: Path, tris: np.ndarray):
    lines = ["solid landmark"]
    for tri in tris:
        lines.extend(["facet normal 0 0 0", "outer loop"])
        lines.extend(f"vertex {x} {y} {z}" for x, y, z in tri)
        lines.extend(["endloop", "endfacet"])
    lines.append("endsolid landmark")
    path.write_text("\n".join(lines), encoding="ascii")


class LandmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_reads_binary_and_ascii_stl(self):
        source = cube_tris()
        binary = self.root / "binary.stl"
        ascii_path = self.root / "ascii.stl"
        topo.write_binary_stl(source, binary)
        write_ascii_stl(ascii_path, source)

        np.testing.assert_allclose(topo._read_landmark_stl(binary), source)
        np.testing.assert_allclose(topo._read_landmark_stl(ascii_path), source)

    def test_parses_places_scales_and_rotates_landmark(self):
        path = self.root / "marker.stl"
        topo.write_binary_stl(cube_tris(), path)
        landmarks = topo._parse_landmarks(
            [f"0.5,0.25,{path},2,90"], (0.0, 0.0, 1.0, 1.0))

        placed = topo._prepare_landmarks(
            landmarks, (0.0, 0.0, 1.0, 1.0), 100.0, 80.0,
            np.full((3, 3), 5.0),
        )
        lo, hi = placed[0]["bounds"]
        np.testing.assert_allclose(lo, [21.0, 38.0, 4.8], atol=1e-5)
        np.testing.assert_allclose(hi, [29.0, 42.0, 16.8], atol=1e-5)

    def test_rejects_bad_specs_before_mesh_build(self):
        solid_path = self.root / "solid.stl"
        open_path = self.root / "open.stl"
        topo.write_binary_stl(cube_tris(), solid_path)
        write_ascii_stl(open_path, cube_tris()[:1])
        bbox = (0.0, 0.0, 1.0, 1.0)

        invalid = [
            "0.5,0.5",
            f"2,0.5,{solid_path}",
            f"0.5,0.5,{solid_path},0",
            f"0.5,0.5,{self.root / 'missing.stl'}",
            f"0.5,0.5,{open_path}",
        ]
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(SystemExit):
                topo._parse_landmarks([spec], bbox)

    def test_landmark_crossing_tile_seam_is_clipped_and_fused_on_both_tiles(self):
        path = self.root / "wide-marker.stl"
        topo.write_binary_stl(cube_tris((12.0, 12.0, 8.0)), path)
        landmarks = topo._parse_landmarks(
            [f"0.5,0.5,{path}"], (0.0, 0.0, 1.0, 1.0))
        z_global = np.full((3, 5), 5.0)
        placed = topo._prepare_landmarks(
            landmarks, (0.0, 0.0, 1.0, 1.0), 100.0, 100.0, z_global)

        for x_offset in (0.0, 50.0):
            base, _ = topo._mesh_from_z(
                np.linspace(0.0, 50.0, 3),
                np.linspace(0.0, 100.0, 3)[::-1],
                np.full((3, 3), 5.0),
            )
            result = topo.add_landmarks(
                base, placed, x_offset, 0.0, 50.0, 100.0)
            verts = result.reshape(-1, 3)
            self.assertGreater(float(verts[:, 2].max()), 5.0)
            self.assertGreaterEqual(float(verts[:, 0].min()), -1e-5)
            self.assertLessEqual(float(verts[:, 0].max()), 50.0 + 1e-5)
            manifold = topo._soup_to_manifold(result)
            self.assertEqual(str(manifold.status()), "Error.NoError")
            self.assertEqual(len(manifold.decompose()), 1)

    def test_main_writes_landmark_into_finished_model_and_sidecar(self):
        landmark_path = self.root / "reference.stl"
        output = self.root / "map.stl"
        topo.write_binary_stl(cube_tris(), landmark_path)
        with mock.patch.object(
                topo, "cached_grid", return_value=np.zeros((3, 3), dtype=float)):
            topo.main([
                "--bbox", "0,0,1,1", "--grid", "3x3", "--model-width", "100",
                "--base", "5", "--smooth", "0", "--landmark",
                f"0.5,0.5,{landmark_path}", "-o", str(output),
            ])

        result = topo._read_landmark_stl(output)
        self.assertGreater(float(result[:, :, 2].max()), 5.0)
        meta = output.with_name("map.topo.json").read_text(encoding="utf-8")
        self.assertIn('"landmarks": 1', meta)


if __name__ == "__main__":
    unittest.main()
