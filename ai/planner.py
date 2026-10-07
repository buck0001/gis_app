"""Deterministic AI GIS planner (spec sections 31-33, 42, 52).

Converts natural language into a structured processing plan WITHOUT
inventing data: keyword -> registered-tool mapping only, plan shown BEFORE
execution, every step cites its dataset source, and interpretation is
labelled as interpretation (never presented as observed fact).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from gis import pipeline as pl  # noqa: E402

TERRAIN_WORDS = ["terrain", "slope", "aspect", "hillshade", "elevation",
                 "topograph"]
HYDRO_WORDS = ["hydro", "drainage", "flow", "stream", "river", "catchment",
               "watershed", "basin", "channel", "runoff"]
ADVANCED_WORDS = ["advanced", "curvature", "roughness", "ruggedness", "tri",
                  "tpi", "position index"]
WETNESS_WORDS = ["wetness", "twi", "saturation"]
DISTANCE_WORDS = ["distance", "proximity", "buffer"]
FLOOD_WORDS = ["flood"]
EROSION_WORDS = ["erosion", "gully", "landslide"]


def _has(text: str, words: list[str]) -> bool:
    return any(re.search(rf"\b{w}\b", text) for w in words)


def suggest_plan(text: str, dem_mode: str = "acquire") -> dict:
    t = (text or "").lower()
    tools: list[dict] = []
    notes: list[str] = []

    wants_basic = _has(t, ["basic", "all", "everything", "complete", "full",
                           "standard", "catchment", "lga"])
    wants_terrain = wants_basic or _has(t, TERRAIN_WORDS)
    wants_hydro = wants_basic or _has(t, HYDRO_WORDS)
    wants_advanced = _has(t, ADVANCED_WORDS)
    wants_twi = _has(t, WETNESS_WORDS)
    wants_dist = _has(t, DISTANCE_WORDS)

    if _has(t, FLOOD_WORDS) or _has(t, EROSION_WORDS):
        notes.append(
            "Flood/erosion susceptibility modelling is a Version 3 feature"
            " and is not available yet. The plan below covers the"
            " DEM-derived factors it will build on. Such output would be"
            " labelled 'susceptibility' - never 'prediction' - until a"
            " model is trained and validated.")

    if not (wants_terrain or wants_hydro or wants_advanced or wants_twi
            or wants_dist):
        notes.append("Request did not name specific products, so the basic"
                     " terrain + drainage plan was selected. Adjust before"
                     " running.")
        wants_terrain = wants_hydro = True

    if wants_terrain:
        tools += [{"tool": k} for k in
                  ["calculate_elevation", "calculate_slope",
                   "calculate_aspect", "calculate_hillshade"]]
    if wants_hydro:
        tools += [{"tool": k} for k in
                  ["breach_dem", "calculate_flow_direction",
                   "calculate_flow_accumulation", "extract_streams",
                   "calculate_stream_order", "extract_drainage_network",
                   "delineate_watershed", "calculate_drainage_density"]]
    if wants_advanced:
        tools += [{"tool": k} for k in
                  ["calculate_curvature", "calculate_roughness",
                   "calculate_tri", "calculate_tpi"]]
    if wants_twi:
        tools.append({"tool": "calculate_twi"})
    if wants_dist:
        tools.append({"tool": "calculate_distance_to_drainage"})

    m = re.search(r"threshold\s*(\d+)", t)
    if m:
        for item in tools:
            if item["tool"] == "extract_streams":
                item["params"] = {"threshold_cells": int(m.group(1))}
        thresh_note = (f"Stream threshold set to {m.group(1)} cells as"
                       " requested (one threshold does not fit all AOIs).")
    else:
        thresh_note = (
            "A stream threshold has not been selected. Enter a defensible "
            "flow-accumulation threshold in cells before running this plan."
            if any(item["tool"] == "extract_streams" for item in tools)
            else "No stream extraction was requested; no stream threshold is used."
        )

    seen, deduped = set(), []
    for item in tools:
        if item["tool"] not in seen:
            seen.add(item["tool"])
            deduped.append(item)

    ctx = pl.PipelineContext(project_dir="/tmp/ai_preview",
                             analysis_crs="EPSG:32632", dem_mode=dem_mode)
    est = pl.plan_estimate(ctx, deduped)

    dem_line = ("DEM: user upload (project's uploaded DEM)"
                if dem_mode == "upload"
                else "DEM: Copernicus GLO-30 (downloaded automatically)")

    guardrails = [
        thresh_note,
        "Sources: every layer records its dataset source and processing"
        " method - inspect any layer's metadata before reuse.",
        "Derived products (slope, drainage, ...) are computed from the"
        " DEM; they are not independent observations.",
        "High flow accumulation marks potential drainage concentration -"
        " not confirmed gullies or flood prediction.",
    ]
    checklist = [
        f"{'ADVANCED - ' if pl.REGISTRY[s['tool']].advanced else ''}"
        f"{s['label']}  [{pl.REGISTRY[s['tool']].group}]"
        for s in est["steps"]]

    return {
        "request": text, "dem_mode": dem_mode, "dem_line": dem_line,
        "plan": deduped, "checklist": checklist, "steps": est["steps"],
        "estimated": est["estimated_label"],
        "estimated_seconds": est["estimated_seconds"],
        "notes": notes, "guardrails": guardrails,
    }


def available_tools() -> list[dict]:
    return [{"tool": t, "label": s.label, "group": s.group,
             "description": s.description, "advanced": s.advanced,
             "params": s.params} for t, s in pl.REGISTRY.items()]
