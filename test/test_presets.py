"""presets/<name>/ hold the build configuration of each preset in
create_nx_cgra4ml_project.py."""
import json
import unittest

import create_nx_cgra4ml_project as generator

PRESETS = generator.REPO_ROOT / "presets"


class PresetFileTests(unittest.TestCase):
    def test_every_preset_has_a_matching_file(self):
        names = {path.name for path in PRESETS.iterdir() if path.is_dir()}
        self.assertEqual(names, set(generator.HARDWARE_PRESETS))
        for name, build in generator.HARDWARE_PRESETS.items():
            with self.subTest(preset=name):
                saved = json.loads((PRESETS / name / "hardware.json").read_text())
                self.assertEqual(saved, build["hardware"])
                for header in ("config_hw.svh", "config_hw.tcl"):
                    self.assertTrue((PRESETS / name / header).is_file(), header)


if __name__ == "__main__":
    unittest.main()
