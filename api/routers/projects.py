"""Projects endpoints (spec sections 3, 27)."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from api import db, jobs  # noqa: E402

router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateProject(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    country: Optional[str] = ""
    region: Optional[str] = ""
    lga: Optional[str] = ""
    analysis_type: Optional[str] = "basic"
    dem_mode: str = Field("acquire", pattern="^(acquire|upload)$")


def public_project(proj: dict) -> dict:
    out = dict(proj)
    out["dem_validation"] = db.jload(proj.get("dem_validation"))
    out["dem_confirm"] = db.jload(proj.get("dem_confirm"))
    return out


def public_aoi(a: dict) -> dict:
    out = dict(a)
    out["geometry"] = db.jload(a.pop("geometry_geojson"), {})
    out["bbox"] = db.jload(a.get("bbox"), [])
    out["centroid"] = db.jload(a.get("centroid"), [])
    out["fixes"] = db.jload(a.get("fixes"), [])
    out["warnings"] = db.jload(a.get("warnings"), [])
    return out


@router.post("")
def create_project(body: CreateProject):
    pid = db.new_id("buck_project")
    db.execute(
        "INSERT INTO projects (id, name, country, region, lga,"
        " analysis_type, dem_mode, created_at)"
        " VALUES (?,?,?,?,?,?,?, datetime('now'))",
        (pid, body.name.strip(), body.country or "", body.region or "",
         body.lga or "", body.analysis_type or "basic", body.dem_mode),
    )
    jobs.project_dir(pid)
    return public_project(db.row("SELECT * FROM projects WHERE id=?", (pid,)))


@router.get("")
def list_projects():
    return [public_project(p)
            for p in db.rows("SELECT * FROM projects ORDER BY created_at DESC")]


@router.get("/{project_id}")
def get_project(project_id: str):
    proj = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not proj:
        raise HTTPException(404, "Project not found.")
    out = public_project(proj)
    aoi_row = db.row("SELECT * FROM areas_of_interest WHERE project_id=?",
                     (project_id,))
    out["aoi"] = public_aoi(aoi_row) if aoi_row else None
    out["layers"] = db.rows(
        "SELECT id, layer_key, name, layer_group, type, geometry_type,"
        " source, data_kind, crs, resolution, units, algorithm, method_text,"
        " hidden, created_at FROM layers WHERE project_id=? ORDER BY created_at",
        (project_id,),
    )
    out["jobs"] = jobs.list_jobs(project_id)
    return out
