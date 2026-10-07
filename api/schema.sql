-- AI GIS Map Generator - database schema (spec section 27)
-- SQLite for the MVP; tables match the specified structure so a PostGIS
-- migration stays straightforward. Raster files are NOT stored in the DB.

CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    country         TEXT,
    region          TEXT,
    lga             TEXT,
    analysis_type   TEXT,
    analysis_crs    TEXT,
    suggested_crs   TEXT,
    aoi_id          TEXT,
    dem_mode        TEXT DEFAULT 'acquire',   -- acquire | upload
    dem_source      TEXT,
    dem_validation  TEXT,                     -- JSON inspection report
    dem_confirm     TEXT,                     -- JSON {action, epsg, resolution_m}
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS areas_of_interest (
    id              TEXT PRIMARY KEY,
    project_id      TEXT,
    name            TEXT,
    geometry_geojson TEXT,
    original_crs    TEXT,
    analysis_crs    TEXT,
    area_km2        REAL,
    perimeter_km    REAL,
    bbox            TEXT,
    centroid        TEXT,
    fixes           TEXT,
    warnings        TEXT,
    input_method    TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS datasets (
    id              TEXT PRIMARY KEY,
    project_id      TEXT,
    provider        TEXT,
    title           TEXT,
    source          TEXT,
    category        TEXT,
    resolution      TEXT,
    crs             TEXT,
    date            TEXT,
    origin          TEXT,                     -- user_upload | acquired
    file_path       TEXT,
    metadata        TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS processing_jobs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT,
    title           TEXT,
    plan_json       TEXT,
    params_json     TEXT,
    status          TEXT DEFAULT 'QUEUED',    -- QUEUED RUNNING COMPLETED FAILED CANCELLED
    progress        REAL DEFAULT 0,
    message         TEXT,
    error           TEXT,
    cancel_requested INTEGER DEFAULT 0,
    created_at      TEXT,
    started_at      TEXT,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS processing_steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT,
    seq             INTEGER,
    time            TEXT,
    tool            TEXT,
    message         TEXT,
    detail          TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS layers (
    id              TEXT PRIMARY KEY,
    project_id      TEXT,
    job_id          TEXT,
    layer_key       TEXT,
    name            TEXT,
    layer_group     TEXT,
    type            TEXT,                     -- raster | vector
    geometry_type   TEXT,
    source          TEXT,
    source_date     TEXT,
    data_kind       TEXT,                     -- direct | derived | model
    crs             TEXT,
    resolution      TEXT,
    units           TEXT,
    algorithm       TEXT,
    processing      TEXT,                     -- JSON list
    params          TEXT,                     -- JSON dict
    method_text     TEXT,
    file_path       TEXT,
    bbox            TEXT,
    stats           TEXT,                     -- JSON
    style           TEXT,                     -- JSON (overridable)
    preview         TEXT,                     -- JSON {file, bounds, legend}
    extra           TEXT,                     -- JSON (validation etc.)
    hidden          INTEGER DEFAULT 0,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS layer_styles (
    layer_id        TEXT PRIMARY KEY,
    style_json      TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS statistics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    layer_id        TEXT,
    kind            TEXT,
    payload         TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS maps (
    id              TEXT PRIMARY KEY,
    project_id      TEXT,
    layer_id        TEXT,
    title           TEXT,
    format          TEXT,
    file_path       TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS exports (
    id              TEXT PRIMARY KEY,
    project_id      TEXT,
    kind            TEXT,                     -- package | config
    file_path       TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS data_sources (
    name            TEXT PRIMARY KEY,
    url             TEXT,
    category        TEXT,
    license         TEXT,
    description     TEXT
);

CREATE INDEX IF NOT EXISTS idx_layers_project ON layers(project_id);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON processing_jobs(project_id);
CREATE INDEX IF NOT EXISTS idx_steps_job ON processing_steps(job_id);
