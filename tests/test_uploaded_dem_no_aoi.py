"""Regression tests for running an uploaded, AOI-clipped DEM without drawing an AOI."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import jobs
from api.routers import jobs as jobs_router


class UploadedDemWithoutAoiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="dem_no_aoi_test_")
        self.dem_path = Path(self.temp_dir.name) / "clipped_dem.tif"
        with rasterio.open(
            self.dem_path,
            "w",
            driver="GTiff",
            width=40,
            height=30,
            count=1,
            dtype="float32",
            crs="EPSG:32632",
            transform=from_origin(500000, 700000, 30, 30),
            nodata=-9999,
        ) as dst:
            dst.write(
                np.arange(1200, dtype="float32").reshape(30, 40) + 100,
                1,
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_worker_derives_and_saves_aoi_from_dem_footprint(self):
        project_id = "test_upload_without_aoi"
        project = {
            "id": project_id,
            "name": "Clipped DEM",
            "dem_mode": "upload",
            "dem_source": str(self.dem_path),
            "dem_confirm": json.dumps({"action": "continue"}),
        }
        statements = []

        def fake_row(sql, params=()):
            if "WHERE project_id=?" in sql:
                return None
            if "WHERE id=?" in sql:
                return {"id": params[0], "project_id": project_id}
            raise AssertionError(f"Unexpected query: {sql}")

        with (
            patch.object(jobs.db, "row", side_effect=fake_row),
            patch.object(
                jobs.db, "execute",
                side_effect=lambda *args: statements.append(args),
            ),
        ):
            aoi = jobs._ensure_job_aoi(project_id, project)

        self.assertEqual(aoi["project_id"], project_id)
        self.assertEqual(len(statements), 2)
        self.assertEqual(statements[0][1][-1], "dem_footprint")
        self.assertIn("EPSG:", statements[1][1][0])

    def test_saved_aoi_for_pipeline_includes_decoded_bbox(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [[
                [6.05, 7.04], [6.10, 7.04], [6.10, 7.09],
                [6.05, 7.09], [6.05, 7.04],
            ]],
        }
        aoi = jobs._pipeline_aoi({
            "geometry_geojson": json.dumps(geometry),
            "bbox": json.dumps([6.05, 7.04, 6.10, 7.09]),
            "analysis_crs": "EPSG:32632",
            "area_km2": 30,
        })

        self.assertEqual(aoi["bbox"], [6.05, 7.04, 6.10, 7.09])
        self.assertEqual(aoi["geometry"], geometry)

    def test_pipeline_aoi_reconstructs_legacy_missing_bbox(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [[
                [6.05, 7.04], [6.10, 7.04], [6.10, 7.09],
                [6.05, 7.09], [6.05, 7.04],
            ]],
        }
        aoi = jobs._pipeline_aoi({
            "geometry_geojson": json.dumps(geometry),
            "bbox": None,
            "analysis_crs": "EPSG:32632",
            "area_km2": 30,
        })

        self.assertEqual(aoi["bbox"], [6.05, 7.04, 6.10, 7.09])

    def test_run_job_accepts_confirmed_upload_without_aoi(self):
        project_id = "test_upload_without_aoi"
        project = {
            "id": project_id,
            "dem_mode": "upload",
            "dem_source": str(self.dem_path),
            "dem_validation": json.dumps({
                "ok_to_proceed": True,
                "report": {"crs": "EPSG:32632"},
                "issues": [],
            }),
            "dem_confirm": json.dumps({"action": "continue"}),
        }
        queued = {"id": "job_test", "status": "QUEUED"}
        body = jobs_router.RunJob(plan=[{"tool": "calculate_slope"}])

        with (
            patch.object(jobs_router.db, "row", side_effect=[project, None]),
            patch.object(jobs_router.jobs, "enqueue", return_value=queued) as enqueue,
        ):
            result = jobs_router.run_job(project_id, body)

        self.assertEqual(result, queued)
        enqueue.assert_called_once_with(
            project_id, body.title, body.plan, params={})


if __name__ == "__main__":
    unittest.main()
