"""AI planner endpoints (spec sections 31-33)."""
from __future__ import annotations
import sys
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from api import db
from ai import planner
router = APIRouter(prefix="/api/projects/{project_id}/ai", tags=["ai"])
class PlanIn(BaseModel):
    request: str
@router.post("/plan")
def make_plan(project_id: str, body: PlanIn):
    proj = db.row("SELECT dem_mode FROM projects WHERE id=?", (project_id,))
    if not proj:
        raise HTTPException(404, "Project not found.")
    return planner.suggest_plan(body.request or "", dem_mode=proj.get("dem_mode") or "acquire")
@router.get("/tools")
def tool_list():
    return planner.available_tools()
@router.post("/quick/basic-terrain-drainage")
def quick_basic(project_id: str):
    proj = db.row("SELECT dem_mode FROM projects WHERE id=?", (project_id,))
    if not proj:
        raise HTTPException(404, "Project not found.")
    return planner.suggest_plan("Generate the basic terrain and drainage analysis.", dem_mode=proj.get("dem_mode") or "acquire")
