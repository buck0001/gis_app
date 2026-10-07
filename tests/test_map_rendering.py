"""Regression tests for raster previews and downloadable map rendering."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gis import maps, symbology


class MapRenderingTests(unittest.TestCase):
    def test_integer_dem_without_nodata_renders_preview_and_map(self):
        with tempfile.TemporaryDirectory(prefix="map_render_test_") as temp_dir:
            root = Path(temp_dir)
            dem_path = root / "dem.tif"
            data = np.arange(400, dtype="int16").reshape(20, 20) + 1300
            with rasterio.open(
                dem_path,
                "w",
                driver="GTiff",
                width=20,
                height=20,
                count=1,
                dtype="int16",
                crs="EPSG:32632",
                transform=from_origin(500000, 700000, 30, 30),
            ) as dst:
                dst.write(data, 1)

            preview = symbology.render_raster_preview(
                str(dem_path), str(root / "preview.png"), "elevation")
            map_result = maps.generate_map(
                str(root / "map.png"),
                "Elevation",
                "raster",
                str(dem_path),
                {
                    "type": "Polygon",
                    "coordinates": [[
                        [9.0, 6.2], [9.01, 6.2], [9.01, 6.21],
                        [9.0, 6.21], [9.0, 6.2],
                    ]],
                },
                "EPSG:32632",
                {"source": "Test DEM", "resolution": "30 m"},
                layer_key="elevation",
            )

            self.assertTrue(Path(preview["png"]).is_file())
            self.assertGreater(Path(preview["png"]).stat().st_size, 250)
            self.assertEqual(len(preview["bounds_4326"]), 4)
            self.assertTrue(Path(map_result["path"]).is_file())
            self.assertGreater(Path(map_result["path"]).stat().st_size, 5_000)
            self.assertEqual(maps.GENERATOR_LINE, "GIS Mapper by buck0001")
            self.assertEqual(
                maps.MAP_BRANDING["url"], "https://github.com/buck0001/")
            self.assertEqual(map_result["qa_report"]["status"], "NOT CLIENT-READY")
            self.assertTrue(Path(map_result["qa_path"]).is_file())
            checks = {item["check"]: item["status"]
                      for item in map_result["qa_report"]["checks"]}
            self.assertEqual(checks["Analysis CRS recorded"], "PASS")
            self.assertEqual(checks["Units recorded"], "PASS")
            with Image.open(map_result["path"]) as image:
                self.assertEqual(image.size, (3507, 2481))
                self.assertAlmostEqual(image.info["dpi"][0], 300, delta=1)

    def test_categorical_map_renders_with_legend_and_scale_bar(self):
        with tempfile.TemporaryDirectory(prefix="categorical_map_test_") as temp_dir:
            root = Path(temp_dir)
            raster_path = root / "flow_direction.tif"
            directions = np.array([1, 2, 4, 8, 16, 32, 64, 128], dtype="uint8")
            data = np.resize(directions, (20, 20))
            with rasterio.open(
                raster_path,
                "w",
                driver="GTiff",
                width=20,
                height=20,
                count=1,
                dtype="uint8",
                crs="EPSG:32632",
                transform=from_origin(500000, 700000, 30, 30),
            ) as dst:
                dst.write(data, 1)

            result = maps.generate_map(
                str(root / "flow_direction.png"),
                "Flow Direction",
                "raster",
                str(raster_path),
                {
                    "type": "Polygon",
                    "coordinates": [[
                        [9.0, 6.2], [9.01, 6.2], [9.01, 6.21],
                        [9.0, 6.21], [9.0, 6.2],
                    ]],
                },
                "EPSG:32632",
                {
                    "source": "Test DEM",
                    "resolution": "30 m",
                    "algorithm": "D8 pointer test",
                    "processing": ["breach", "D8 pointer"],
                    "units": "Classes: 8 (D8)",
                    "flow_direction_encoding": "ESRI D8 powers of two",
                    "flow_direction_verification": {
                        "passed": True, "sampled_cells": 8,
                    },
                },
                layer_key="flow_direction",
            )

            self.assertTrue(Path(result["path"]).is_file())
            self.assertGreater(Path(result["path"]).stat().st_size, 5_000)
            checks = {item["check"]: item["status"]
                      for item in result["qa_report"]["checks"]}
            self.assertEqual(checks["Flow directions numerically verified"], "PASS")
            self.assertEqual(result["qa_report"]["status"], "NOT CLIENT-READY")

            arrow_result = maps.generate_map(
                str(root / "flow_direction_arrows.png"),
                "Flow Direction Arrows",
                "raster",
                str(raster_path),
                {
                    "type": "Polygon",
                    "coordinates": [[
                        [9.0, 6.2], [9.01, 6.2], [9.01, 6.21],
                        [9.0, 6.21], [9.0, 6.2],
                    ]],
                },
                "EPSG:32632",
                {
                    "source": "Test DEM",
                    "resolution": "30 m",
                    "algorithm": "D8 pointer test",
                    "processing": ["breach", "D8 pointer"],
                    "units": "Classes: 8 (D8)",
                    "flow_direction_encoding": "ESRI D8 powers of two",
                    "flow_direction_verification": {
                        "passed": True, "sampled_cells": 8,
                    },
                },
                layer_key="flow_direction",
                flow_direction_arrows=True,
            )
            arrow_checks = {item["check"]: item["status"]
                            for item in arrow_result["qa_report"]["checks"]}
            self.assertTrue(Path(arrow_result["path"]).is_file())
            self.assertEqual(arrow_checks["D8 arrow sampling stride recorded"], "PASS")


if __name__ == "__main__":
    unittest.main()
