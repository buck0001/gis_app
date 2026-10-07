"""Tests for Copernicus DEM mosaic metadata and NoData handling."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gis.providers.dem import CopernicusDEMProvider, mosaic_and_clip


class CopernicusDemProviderTests(unittest.TestCase):
    def test_clipped_output_declares_fill_value_as_nodata(self):
        with tempfile.TemporaryDirectory(prefix="dem_clip_test_") as temp_dir:
            root = Path(temp_dir)
            tile_path = root / "tile.tif"
            output_path = root / "clip.tif"
            with rasterio.open(
                tile_path,
                "w",
                driver="GTiff",
                width=10,
                height=10,
                count=1,
                dtype="float32",
                crs="EPSG:4326",
                transform=from_origin(0, 10, 1, 1),
            ) as dst:
                data = np.full((10, 10), 100, dtype="float32")
                data[4, 4] = -9999
                dst.write(data, 1)

            mosaic_and_clip(
                [tile_path],
                mapping(box(2, 2, 8, 8)),
                [0, 0, 10, 10],
                output_path,
            )

            with rasterio.open(output_path) as src:
                self.assertEqual(src.nodata, -9999)
                pixels = src.read(1, masked=True)
                self.assertTrue(np.ma.getmaskarray(pixels).any())
                self.assertEqual(float(pixels.min()), 100)
                self.assertEqual(float(pixels.max()), 100)

            report = CopernicusDEMProvider().validate(str(output_path))
            self.assertTrue(report["ok"], report["checks"])


if __name__ == "__main__":
    unittest.main()
