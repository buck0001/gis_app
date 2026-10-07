"""Regression tests for persisting outputs from completed processing jobs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import jobs


class JobLayerStorageTests(unittest.TestCase):
    def test_layer_insert_placeholders_match_bound_values(self):
        statements = []
        layer = {
            "key": "slope",
            "name": "Slope",
            "type": "raster",
            "path": "slope.tif",
            "extra": {},
        }

        with (
            patch.object(
                jobs.db, "execute",
                side_effect=lambda sql, params=(): statements.append((sql, params)),
            ),
            patch.object(jobs.db, "new_id", return_value="layer_test"),
        ):
            jobs._store_layers("project_test", "job_test", [layer])

        self.assertEqual(len(statements), 3)
        layer_insert, statistics_insert = statements[1:]
        self.assertEqual(layer_insert[0].count("?"), len(layer_insert[1]))
        self.assertEqual(statistics_insert[0].count("?"), len(statistics_insert[1]))


if __name__ == "__main__":
    unittest.main()
