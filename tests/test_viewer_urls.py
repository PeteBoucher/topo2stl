import re
import unittest
from pathlib import Path


class ViewerUrlTests(unittest.TestCase):
    def test_fetches_are_relative_for_path_prefix_proxies(self):
        html = Path(__file__).parents[1].joinpath("viewer.html").read_text()
        self.assertIsNone(re.search(r"fetch\(\s*['\"]/", html))
        for endpoint in ("version", "model.stl", "meta", "regen/available",
                         "exists?name=", "regen", "regen/status"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(f"fetch('./{endpoint}", html)


if __name__ == "__main__":
    unittest.main()
