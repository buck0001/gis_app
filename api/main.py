"""FastAPI backend (spec section 26 backend service).

Browser <-> FastAPI <-> job queue <-> Python GIS worker.
All secrets (no API keys are even needed for the MVP providers) stay
server-side; the browser only ever receives JSON and file downloads.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db
from . import jobs
from .routers import ai, aoi, dem, exports, jobs as jobs_router, layers, projects

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE = BASE_DIR / "data" / "storage"


def create_app() -> FastAPI:
    app = FastAPI(title="GIS Mapper by buck0001",
                  description="Automated GIS analysis platform (MVP).",
                  version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    db.get_conn()          # create tables
    jobs.ensure_workers()  # background GIS workers
    app.include_router(projects.router)
    app.include_router(aoi.router)
    app.include_router(aoi.search_router)
    app.include_router(dem.router)
    app.include_router(jobs_router.router)
    app.include_router(layers.router)
    app.include_router(ai.router)
    app.include_router(exports.router)
    STORAGE.mkdir(parents=True, exist_ok=True)
    # Serve previews / maps / export zips as static files (read-only products).
    app.mount("/files", StaticFiles(directory=str(STORAGE)), name="files")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "ai-gis-map-generator"}

    @app.get("/api/data-sources")
    def data_sources():
        return db.rows("SELECT * FROM data_sources ORDER BY name")

    seed_sources()
    return app


def seed_sources() -> None:
    rows = [
        ("Copernicus DEM GLO-30", "https://copernicus-dem-30m.s3.amazonaws.com/",
         "DEM", "ESA / AWS Open Data",
         "Global 30 m DEM (EGM2008 heights). Acquired per AOI as clipped mosaic."),
        ("OpenStreetMap Nominatim", "https://nominatim.openstreetmap.org/",
         "Administrative boundaries", "ODbL",
         "Place-name search for AOI selection (Option C)."),
    ]
    for name, url, cat, lic, desc in rows:
        try:
            db.execute(
                "INSERT OR IGNORE INTO data_sources (name, url, category,"
                " license, description) VALUES (?,?,?,?,?)",
                (name, url, cat, lic, desc),
            )
        except Exception:
            pass


app = create_app()
