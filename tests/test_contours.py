"""Tests for generating vector contour layers from elevation rasters."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import geopandas as gpd
import rasterio
from rasterio.transform import from_origin

from gis import pipeline, terrain


class ContourGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        self.dem_path = self.project_dir / "elevation.tif"
        elevations = np.tile(
            np.arange(20, dtype="float32") * 2.0, (20, 1),
        )
        profile = {
            "driver": "GTiff",
            "height": elevations.shape[0],
            "width": elevations.shape[1],
            "count": 1,
            "dtype": "float32",
            "crs": "EPSG:32631",
            "transform": from_origin(500000, 700000, 1, 1),
            "nodata": -9999.0,
        }
        with rasterio.open(self.dem_path, "w", **profile) as dst:
            dst.write(elevations, 1)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pipeline_creates_contour_vector_layer(self):
        elevation = pipeline.LayerOut(
            key="elevation",
            name="Elevation",
            group="Terrain",
            type="raster",
            path=str(self.dem_path),
            source="Test DEM",
            algorithm="synthetic",
            crs="EPSG:32631",
            resolution="1 m",
        )
        context = pipeline.PipelineContext(
            project_dir=str(self.project_dir),
            analysis_crs="EPSG:32631",
            outputs={"elevation": elevation},
        )

        layers = pipeline.op_generate_contours(context, {"interval_m": 10})

        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0].key, "contours")
        self.assertEqual(layers[0].type, "vector")
        self.assertEqual(layers[0].params["interval_m"], 10.0)
        contours = gpd.read_file(layers[0].path, layer="contours")
        self.assertGreater(len(contours), 0)
        self.assertTrue(contours.geometry.is_valid.all())
        self.assertEqual(str(contours.crs), "EPSG:32631")
        self.assertTrue((contours["elevation_m"] % 10 == 0).all())

    def test_rejects_non_positive_interval(self):
        with self.assertRaisesRegex(ValueError, "positive finite"):
            terrain.generate_contours(
                str(self.dem_path), str(self.project_dir / "invalid.gpkg"), 0,
            )


if __name__ == "__main__":
    unittest.main()
