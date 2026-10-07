"""Processing-job endpoints (spec section 24)."""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from api import db, jobs
router = APIRouter(prefix="/api/projects/{project_id}/jobs", tags=["jobs"])
class RunJob(BaseModel):
    title: str = "GIS analysis"
    plan: list[dict]
    stream_threshold: Optional[int] = Field(default=None, ge=1)
@router.post("")
def run_job(project_id: str, body: RunJob):
    proj = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not proj:
        raise HTTPException(404, "Project not found.")
    aoi = db.row("SELECT id FROM areas_of_interest WHERE project_id=?", (project_id,))
    upload_mode = (proj.get("dem_mode") or "acquire") == "upload"
    if not aoi and not upload_mode:
        raise HTTPException(400, "Set the project AOI before running analysis.")
    if upload_mode:
        if not proj.get("dem_source") or not Path(proj["dem_source"]).is_file():
            raise HTTPException(400, "Upload a DEM before running analysis.")
        report = db.jload(proj.get("dem_validation"), {})
        if report.get("issues") and any(i.get("severity") == "error" for i in report["issues"]):
            raise HTTPException(400, "Uploaded DEM has blocking validation errors. Resolve them (assign CRS / upload another) before running.")
        confirm = db.jload(proj.get("dem_confirm"), {}) or {}
        if confirm.get("action") not in ("continue", "fill_nodata", "assign_crs"):
            raise HTTPException(400, "Confirm the uploaded DEM (continue / fill / assign CRS) before running analysis.")
        if not aoi and not report.get("report", {}).get("crs") and not confirm.get("epsg"):
            raise HTTPException(400, "Assign a CRS to the uploaded DEM before running analysis.")
    from gis import pipeline as pl
    tools = [it.get("tool") for it in (body.plan or [])]
    unknown = [t for t in tools if t not in pl.REGISTRY]
    if unknown:
        raise HTTPException(400, f"Unknown tools in plan: {unknown}. Use /api/ai/tools for the controlled tool list.")
    stream_tools = {
        "extract_streams", "calculate_stream_order",
        "extract_drainage_network", "delineate_watershed",
        "calculate_drainage_density", "calculate_distance_to_drainage",
    }
    planned_threshold = next(
        ((item.get("params") or {}).get("threshold_cells")
         for item in (body.plan or [])
         if (item.get("params") or {}).get("threshold_cells") is not None),
        None,
    )
    selected_threshold = (
        body.stream_threshold if body.stream_threshold is not None
        else planned_threshold
    )
    if stream_tools.intersection(tools) and selected_threshold is None:
        raise HTTPException(
            400,
            "A stream accumulation threshold in cells is required for this "
            "plan. Enter a threshold based on the project and methodology.",
        )
    job = jobs.enqueue(
        project_id, body.title, body.plan or [],
        params=({"stream_threshold": selected_threshold}
                if selected_threshold is not None else {}),
    )
    return job
@router.get("")
def job_list(project_id: str):
    return jobs.list_jobs(project_id)
@router.get("/{job_id}")
def job_detail(project_id: str, job_id: str):
    job = jobs.get_job(job_id)
    if not job or job["project_id"] != project_id:
        raise HTTPException(404, "Job not found.")
    job["history"] = jobs.job_history(job_id)
    return job
@router.post("/{job_id}/cancel")
def job_cancel(project_id: str, job_id: str):
    job = jobs.get_job(job_id)
    if not job or job["project_id"] != project_id:
        raise HTTPException(404, "Job not found.")
    if not jobs.cancel(job_id):
        raise HTTPException(400, "Job is already finished and cannot be cancelled.")
    return {"status": "cancellation_requested"}
