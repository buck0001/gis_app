"""Background job queue for GIS processing (spec section 24).

Slow GIS work NEVER runs inside a normal HTTP request. Jobs are queued,
executed by daemon worker threads through the GIS pipeline, and report
QUEUED -> RUNNING -> COMPLETED | FAILED | CANCELLED with progress %.
"""

from __future__ import annotations

import itertools
import queue
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import db

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE = BASE_DIR / "data" / "storage"

_job_queue: "queue.Queue[str]" = queue.Queue()
_workers: list[threading.Thread] = []
_seq = itertools.count(1)


def project_dir(project_id: str) -> Path:
    d = STORAGE / project_id
    for sub in ("source", "processed", "outputs", "maps",
                "previews", "exports"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def ensure_workers(n: int = 2) -> None:
    while len(_workers) < n:
        t = threading.Thread(target=_worker_loop, daemon=True,
                             name=f"gis-worker-{len(_workers) + 1}")
        t.start()
        _workers.append(t)


def enqueue(project_id: str, title: str, plan: list[dict],
            params: dict | None = None) -> dict:
    job_id = db.new_id("job")
    db.execute(
        "INSERT INTO processing_jobs (id, project_id, title, plan_json,"
        " params_json, status, progress, created_at)"
        " VALUES (?,?,?,?,?, 'QUEUED', 0, datetime('now'))",
        (job_id, project_id, title, db.jdump(plan), db.jdump(params or {})),
    )
    ensure_workers()
    _job_queue.put(job_id)
    return get_job(job_id)


def get_job(job_id: str) -> dict | None:
    job = db.row("SELECT * FROM processing_jobs WHERE id=?", (job_id,))
    if not job:
        return None
    job["plan"] = db.jload(job.pop("plan_json"), [])
    job["params"] = db.jload(job.pop("params_json", None), {})
    return job


def list_jobs(project_id: str) -> list[dict]:
    jobs = db.rows(
        "SELECT * FROM processing_jobs WHERE project_id=? ORDER BY created_at",
        (project_id,),
    )
    for j in jobs:
        j["plan"] = db.jload(j.pop("plan_json"), [])
        j["params"] = db.jload(j.pop("params_json", None), {})
    return jobs


def cancel(job_id: str) -> bool:
    job = get_job(job_id)
    if not job or job["status"] not in ("QUEUED", "RUNNING"):
        return False
    db.execute("UPDATE processing_jobs SET cancel_requested=1 WHERE id=?",
               (job_id,))
    return True


def job_history(job_id: str) -> list[dict]:
    return db.rows(
        "SELECT seq, time, tool, message FROM processing_steps"
        " WHERE job_id=? ORDER BY seq", (job_id,))


def _set(job_id: str, **kw) -> None:
    cols = ", ".join(f"{k}=?" for k in kw)
    db.execute(f"UPDATE processing_jobs SET {cols} WHERE id=?",
               (*kw.values(), job_id))


def _store_layers(project_id: str, job_id: str, layers: list[dict]) -> None:
    for lyr in layers:
        db.execute("DELETE FROM layers WHERE project_id=? AND layer_key=?",
                   (project_id, lyr["key"]))
        lid = db.new_id("lyr")
        extra = lyr.get("extra") or {}
        db.execute(
            "INSERT INTO layers (id, project_id, job_id, layer_key, name,"
            " layer_group, type, geometry_type, source, source_date,"
            " data_kind, crs, resolution, units, algorithm, processing,"
            " params, method_text, file_path, stats, style, preview, extra,"
            " hidden, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            " datetime('now'))",
            (lid, project_id, job_id, lyr["key"], lyr["name"],
             lyr.get("group"), lyr.get("type"), lyr.get("geometry_type"),
             lyr.get("source"), extra.get("source_date"),
             lyr.get("data_kind"), lyr.get("crs"), lyr.get("resolution"),
             lyr.get("units"), lyr.get("algorithm"),
             db.jdump(lyr.get("processing", [])),
             db.jdump(lyr.get("params", {})),
             lyr.get("method_text"), lyr.get("path"),
             db.jdump(extra.get("stats")), db.jdump(extra.get("style")),
             db.jdump(extra.get("preview")), db.jdump(extra),
             1 if lyr.get("hidden") else 0),
        )
        db.execute(
            "INSERT INTO statistics (layer_id, kind, payload, created_at)"
            " VALUES (?,?,?, datetime('now'))",
            (lid, lyr.get("type"), db.jdump(extra.get("stats"))),
        )


def _worker_loop() -> None:
    import sys as _sys
    _sys.path.insert(0, str(BASE_DIR))
    from gis import pipeline as pl

    while True:
        job_id = _job_queue.get()
        try:
            _run_job(job_id, pl)
        except Exception as exc:
            traceback.print_exc()
            try:
                _set(
                    job_id,
                    status="FAILED",
                    message="Failed while saving processing results",
                    error=f"Could not finalize processing results: {exc}",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                traceback.print_exc()
        finally:
            _job_queue.task_done()


def _ensure_job_aoi(project_id: str, project: dict) -> dict | None:
    aoi = db.row("SELECT * FROM areas_of_interest WHERE project_id=?",
                 (project_id,))
    if aoi:
        return aoi
    if (project.get("dem_mode") or "acquire") != "upload":
        return None

    from gis import aoi as aoi_mod, dem_source

    confirm = db.jload(project.get("dem_confirm"), {}) or {}
    footprint = dem_source.dem_footprint_geojson(
        project["dem_source"], assign_crs=confirm.get("epsg"))
    prepared = aoi_mod.prepare_aoi(
        footprint["geometry"], name=f"{project['name']} DEM extent")
    aoi_id = db.new_id("aoi")
    db.execute(
        "INSERT INTO areas_of_interest (id, project_id, name,"
        " geometry_geojson, original_crs, analysis_crs, area_km2,"
        " perimeter_km, bbox, centroid, fixes, warnings, input_method,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))",
        (aoi_id, project_id, prepared["name"],
         db.jdump(prepared["geometry"]), prepared["original_crs"],
         prepared["analysis_crs"], prepared["area_km2"],
         prepared["perimeter_km"], db.jdump(prepared["bbox"]),
         db.jdump(prepared["centroid"]), db.jdump(prepared["fixes"]),
         db.jdump(prepared["warnings"]), "dem_footprint"),
    )
    db.execute(
        "UPDATE projects SET analysis_crs=?, suggested_crs=? WHERE id=?",
        (prepared["analysis_crs"], prepared["suggested_crs"], project_id),
    )
    return db.row("SELECT * FROM areas_of_interest WHERE id=?", (aoi_id,))


def _pipeline_aoi(aoi: dict) -> dict:
    """Decode the saved AOI fields required by GIS providers and operations."""
    geometry = db.jload(aoi.get("geometry_geojson"))
    bbox = db.jload(aoi.get("bbox"), [])
    if not bbox and geometry:
        from shapely.geometry import shape
        bbox = list(shape(geometry).bounds)
    if not geometry or len(bbox) != 4:
        raise ValueError("Saved AOI is missing valid geometry or bounding-box data.")
    return {
        "geometry": geometry,
        "bbox": bbox,
        "analysis_crs": aoi["analysis_crs"],
        "area_km2": aoi["area_km2"],
    }


def _run_job(job_id: str, pl) -> None:
    job = get_job(job_id)
    if not job:
        return
    if job.get("cancel_requested"):
        _set(job_id, status="CANCELLED")
        return
    project_id = job["project_id"]
    proj = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not proj:
        _set(job_id, status="FAILED",
             error="Project missing. Recommended action: recreate the project.")
        return
    try:
        aoi = _ensure_job_aoi(project_id, proj)
    except Exception as exc:
        _set(job_id, status="FAILED",
             error=f"Could not use the uploaded DEM footprint as the AOI: {exc}")
        return
    if not aoi:
        _set(job_id, status="FAILED",
             error="AOI missing. Recommended action: save an AOI before running"
                   " analysis, or upload a DEM to use its footprint.")
        return

    pdir = project_dir(project_id)
    seq = [0]

    def progress_cb(frac: float, msg: str) -> None:
        _set(job_id, progress=round(frac * 100, 1), message=msg)

    def history_cb(step: dict) -> None:
        seq[0] += 1
        db.execute(
            "INSERT INTO processing_steps (job_id, seq, time, tool,"
            " message, detail, created_at)"
            " VALUES (?,?,?,?,?,?, datetime('now'))",
            (job_id, seq[0], step.get("time"), step.get("tool"),
             step.get("message"), db.jdump(step.get("detail", {}))),
        )

    def cancel_cb() -> bool:
        j = get_job(job_id)
        return bool(j and j.get("cancel_requested"))

    ctx = pl.PipelineContext(
        project_dir=str(pdir),
        analysis_crs=aoi["analysis_crs"],
        dem_mode=proj.get("dem_mode") or "acquire",
        dem_source_path=proj.get("dem_source"),
        dem_report=db.jload(proj.get("dem_validation")),
        aoi=_pipeline_aoi(aoi),
        params=job.get("params") or {},
        progress_cb=progress_cb, history_cb=history_cb,
        cancel_cb=cancel_cb,
    )
    _set(job_id, status="RUNNING", progress=1.0, message="Starting GIS")
    try:
        result = pl.run_plan(ctx, job["plan"] or [])
    except pl.PipelineCancelled:
        _set(job_id, status="CANCELLED", message="Cancelled by user")
        return
    except pl.PipelineError as exc:
        _set(job_id, status="FAILED", error=str(exc))
        return
    except Exception as exc:
        from gis.validation import diagnose
        info = diagnose("processing job", exc)
        _set(job_id, status="FAILED",
             error=f"Operation failed:\nProcessing job\n\nReason:\n"
                   f"{info['reason']}\n\nRecommended action:\n"
                   f"{info['recommended_action']}")
        return
    _store_layers(project_id, job_id, result["layers"])
    _set(job_id, status="COMPLETED", progress=100.0,
         message=f"Completed: {len(result['layers'])} layers")
