"""Regression tests for depression breaching and D8 verification."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gis import hydrology


class HydrologySpecificationTests(unittest.TestCase):
    def _write_raster(self, path: Path, data: np.ndarray) -> None:
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=data.shape[1],
            height=data.shape[0],
            count=1,
            dtype="float32",
            nodata=-9999,
            crs="EPSG:32632",
            transform=from_origin(500000, 700000, 30, 30),
        ) as dst:
            dst.write(data.astype("float32"), 1)

    def test_breach_uses_breaching_without_fill_fallback(self):
        with tempfile.TemporaryDirectory(prefix="breach_dem_test_") as temp_dir:
            output = Path(temp_dir) / "dem_breached.tif"

            class FakeEngine:
                def run_variants(self, tool, outputs, variants):
                    self.tool = tool
                    self.kwargs = variants[0]
                    Path(outputs[0]).write_bytes(b"test")

            engine = FakeEngine()
            with patch.object(hydrology, "HydroEngine", return_value=engine):
                result = hydrology.breach_dem("source.tif", str(output))

            self.assertEqual(engine.tool, "breach_depressions")
            self.assertFalse(engine.kwargs["fill_pits"])
            self.assertIn("breach", result["algorithm"])
            self.assertTrue(output.is_file())

    def test_flow_direction_verifier_detects_uphill_pointer(self):
        with tempfile.TemporaryDirectory(prefix="d8_verify_test_") as temp_dir:
            root = Path(temp_dir)
            pointer_path = root / "pointer.tif"
            dem_path = root / "conditioned.tif"
            pointer = np.ones((5, 5), dtype="float32")
            dem = np.tile(np.arange(5, dtype="float32"), (5, 1))
            self._write_raster(pointer_path, pointer)
            self._write_raster(dem_path, dem)

            result = hydrology.verify_flow_direction(
                str(pointer_path), str(dem_path))

            self.assertFalse(result["passed"])
            self.assertGreater(result["uphill_cells"], 0)

    def test_flow_direction_verifier_accepts_downhill_pointer(self):
        with tempfile.TemporaryDirectory(prefix="d8_verify_test_") as temp_dir:
            root = Path(temp_dir)
            pointer_path = root / "pointer.tif"
            dem_path = root / "conditioned.tif"
            pointer = np.ones((5, 5), dtype="float32")
            dem = np.tile(np.arange(5, 0, -1, dtype="float32"), (5, 1))
            self._write_raster(pointer_path, pointer)
            self._write_raster(dem_path, dem)

            result = hydrology.verify_flow_direction(
                str(pointer_path), str(dem_path))

            self.assertTrue(result["passed"])
            self.assertEqual(result["uphill_cells"], 0)


if __name__ == "__main__":
    unittest.main()
