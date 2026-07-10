from uuid import uuid4

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.concurrency import run_in_threadpool
import json
import os
from pathlib import Path
import re
import shutil
import threading

from .tile_tasks import create_tiles_pair_task, create_tiles_task, get_task

tile_router = APIRouter()
# 从环境变量读取四个数据目录路径
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "data/uploads"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "data/outputs"))
TASKS_DIR = Path(os.getenv("TASKS_DIR", "data/tasks"))
PROJECTS_DIR = Path(os.getenv("PROJECTS_DIR", "data/projects"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TASKS_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
project_lock = threading.RLock()
PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
DEFAULT_TILE_WIDTH_M = 120.0
DEFAULT_TILE_HEIGHT_M = 90.0


class AnnotationBoxPayload(BaseModel):
    id: str
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    area_m2: float | None = None
    source: str = "manual"
    status: str = "active"
    properties: dict | None = None


class AnnotationTilePayload(BaseModel):
    tile_id: str
    image_path: str | None = None
    annotated_image_path: str | None = None
    source_tif_path: str | None = None
    bbox: dict | None = None
    boxes: list[AnnotationBoxPayload] = Field(default_factory=list)


class AnnotationBoxesPayload(BaseModel):
    tiles: list[AnnotationTilePayload] = Field(default_factory=list)


def normalize_optional_positive(value: float | None):
    if value is None:
        return None
    if value <= 0:
        return None
    return value


def normalize_optional_text(value: str | None):
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_project_id(value: str | None):
    project_id = normalize_optional_text(value)  #去掉首尾空白，如果结果是空字符串就返回None
    if project_id is None:
        return None
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise HTTPException(400, "project_id can only contain letters, numbers, dots, underscores, and hyphens")
    return project_id


def parse_optional_float(value: str | float | None, positive: bool = False):
    if value is None:
        return None
    if isinstance(value, float):
        return normalize_optional_positive(value) if positive else value
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = float(cleaned)
        return normalize_optional_positive(parsed) if positive else parsed
    except ValueError as exc:
        raise HTTPException(400, f"Invalid number value: {value}") from exc


def parse_aoi_geojson(value: str | None):
    aoi_geojson = normalize_optional_text(value)
    if aoi_geojson is None:
        return None
    try:
        parsed = json.loads(aoi_geojson)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "aoi_geojson must be valid GeoJSON") from exc

    if not isinstance(parsed, dict):
        raise HTTPException(400, "aoi_geojson must be a GeoJSON object")

    geometry = parsed
    if parsed.get("type") == "Feature":
        geometry = parsed.get("geometry")
    if not isinstance(geometry, dict):
        raise HTTPException(400, "aoi_geojson must be a Polygon or MultiPolygon geometry")

    geometry_type = geometry.get("type")
    if geometry_type not in {"Polygon", "MultiPolygon"}:
        raise HTTPException(400, "aoi_geojson must be a Polygon or MultiPolygon geometry")
    if not geometry.get("coordinates"):
        raise HTTPException(400, "aoi_geojson coordinates cannot be empty")
    return geometry

# 先给任务起个唯一名字，再把上传的文件落地到磁盘，然后把切割参数校验打包好，然后启动后台线程
def build_tile_config(
    tile_size: int,
    skip_empty_tiles: bool,
    export_png: bool,
    grid_origin_x: str | float | None,
    grid_origin_y: str | float | None,
    grid_pixel_size_x: str | float | None,
    grid_pixel_size_y: str | float | None,
    aoi_geojson: str | None,
    aoi_crs: str | None,
):
    if tile_size <= 0:
        raise HTTPException(400, "tile_size must be greater than 0")
    parsed_aoi = parse_aoi_geojson(aoi_geojson)
    return {
        "tile_size": tile_size,
        "tile_width_m": DEFAULT_TILE_WIDTH_M,
        "tile_height_m": DEFAULT_TILE_HEIGHT_M,
        "skip_empty_tiles": skip_empty_tiles,
        "export_png": export_png,
        "grid_origin_x": parse_optional_float(grid_origin_x),
        "grid_origin_y": parse_optional_float(grid_origin_y),
        "grid_pixel_size_x": parse_optional_float(grid_pixel_size_x, positive=True),
        "grid_pixel_size_y": parse_optional_float(grid_pixel_size_y, positive=True),
        "aoi_geojson": parsed_aoi,
        "aoi_crs": normalize_optional_text(aoi_crs) if parsed_aoi is not None else None,
    }


def project_file_path(project_id: str):
    return PROJECTS_DIR / f"{project_id}.json"


def get_project(project_id: str):
    with project_lock:
        path = project_file_path(project_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())


def persist_project(project: dict):
    with project_lock:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = project_file_path(project["id"]).with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(project, ensure_ascii=True, indent=2))
        tmp_path.replace(project_file_path(project["id"]))


def create_project(project_id: str, base_task_id: str, period_name: str | None = None):
    project = {
        "id": project_id,
        "base_task_id": base_task_id,
        "tasks": [base_task_id],
        "periods": [
            {
                "task_id": base_task_id,
                "period_name": period_name,
                "is_base": True,
            }
        ],
    }
    persist_project(project)
    return project


def append_project_task(
    project_id: str,
    task_id: str,
    period_name: str | None = None,
    reference_task_id: str | None = None,
):
    project = get_project(project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    if task_id not in project["tasks"]:
        project["tasks"].append(task_id)
    project["periods"].append(
        {
            "task_id": task_id,
            "period_name": period_name,
            "is_base": False,
            "reference_task_id": reference_task_id,
        }
    )
    persist_project(project)
    return project


def require_reference_grid(reference_task_id: str, tile_size: int):
    task = get_task(reference_task_id)
    if task is None:
        raise HTTPException(404, "Reference task not found")
    if task.get("status") != "completed":
        raise HTTPException(400, "Reference task is not completed")
    if task.get("tile_size") != tile_size:
        raise HTTPException(400, "tile_size must match the reference task")

    grid = task.get("grid")
    if not grid:
        raise HTTPException(400, "Reference task has no grid")
    return grid


def apply_reference_grid(tile_config: dict, grid: dict):
    tile_config["grid_origin_x"] = grid["origin_x"]
    tile_config["grid_origin_y"] = grid["origin_y"]
    tile_config["grid_pixel_size_x"] = grid["pixel_size_x"]
    tile_config["grid_pixel_size_y"] = grid["pixel_size_y"]
    return tile_config


def save_uploaded_file(task_id: str, file_obj: UploadFile):
    task_upload_dir = UPLOAD_DIR / task_id
    task_upload_dir.mkdir(parents=True, exist_ok=True)

    path = task_upload_dir / Path(file_obj.filename or "orthophoto.tif").name
    with open(path, "wb") as buffer:
        shutil.copyfileobj(file_obj.file, buffer)
    return str(path)


def require_completed_task(task_id: str):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    if task["status"] != "completed":
        raise HTTPException(
            400,
            {
                "message": "Task not completed yet",
                "status": task.get("status"),
                "progress": task.get("progress"),
                "error": task.get("error"),
            },
        )
    return task


def model_to_plain_dict(model: BaseModel):
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def task_output_dir(task_id: str, task: dict):
    manifest_path = task.get("manifest_path")
    if manifest_path:
        return Path(manifest_path).parent.parent

    orthophoto_path = task.get("orthophoto_path")
    if orthophoto_path:
        return Path(orthophoto_path).parent

    return OUTPUT_DIR / task_id


def annotation_boxes_path(task_id: str, task: dict):
    return task_output_dir(task_id, task) / "changed_boxes_edited.json"


def build_annotation_boxes_from_manifest(task_id: str, task: dict):
    manifest_path = task.get("manifest_path")
    tiles = []
    if manifest_path and os.path.exists(manifest_path):
        manifest = json.loads(Path(manifest_path).read_text())
        for tile in manifest.get("tiles", []):
            tile_id = tile.get("name")
            if not tile_id:
                continue
            tiles.append(
                {
                    "tile_id": tile_id,
                    "image_path": tile.get("png_path"),
                    "annotated_image_path": None,
                    "source_tif_path": tile.get("tif_path"),
                    "bbox": tile.get("bbox"),
                    "boxes": [],
                }
            )

    return {
        "task_id": task_id,
        "schema_version": 1,
        "tiles": tiles,
    }


@tile_router.post("/api/tiles/process")
async def process_tiles(
    orthophoto_file: UploadFile | str | None = File(...),
    tile_size: int = Form(1024),
    skip_empty_tiles: bool = Form(True),
    export_png: bool = Form(True),
    grid_origin_x: str | None = Form(None),
    grid_origin_y: str | None = Form(None),
    grid_pixel_size_x: str | None = Form(None),
    grid_pixel_size_y: str | None = Form(None),
    aoi_geojson: str | None = Form(None),
    aoi_crs: str | None = Form(None),
    reference_task_id: str | None = Form(None),
    project_id: str | None = Form(None),
    period_name: str | None = Form(None),
):
    if not isinstance(orthophoto_file, StarletteUploadFile):
        raise HTTPException(400, "orthophoto_file must be an uploaded file")

    # 去掉首尾空白，如果结果是空字符串就返回None
    project_id = normalize_project_id(project_id)
    # (r"^[A-Za-z0-9_.-]+$")
    period_name = normalize_optional_text(period_name)
    # 用来告诉后端"这次切割要复用那个任务的网格"
    reference_task_id = normalize_optional_text(reference_task_id)

    task_id = str(uuid4()) # 生成唯一id给前端
    # 把前端上传的文件从内存写到磁盘
    orthophoto_path = await run_in_threadpool(save_uploaded_file, task_id, orthophoto_file)
    # 把前端传的那些散装参数校验后组装成一个字典，这个字典会贯穿整个切割流程
    tile_config = build_tile_config(
        tile_size,
        skip_empty_tiles,
        export_png,
        grid_origin_x,
        grid_origin_y,
        grid_pixel_size_x,
        grid_pixel_size_y,
        aoi_geojson,
        aoi_crs,
    )

    # 多期对比时网格复用的逻辑，然后启动后台任务。分两层来看。切分网格
    # 第一层是确定用不用参考网格，通过两个来源——project_id 和 reference_task_id：
    project = get_project(project_id) if project_id is not None else None
    base_task_id = project.get("base_task_id") if project is not None else None
    if base_task_id is not None:
        if reference_task_id is not None and reference_task_id != base_task_id:
            raise HTTPException(400, "reference_task_id must match the project base_task_id")
        reference_task_id = base_task_id

    if reference_task_id is not None:
        reference_grid = require_reference_grid(reference_task_id, tile_size)
        tile_config = apply_reference_grid(tile_config, reference_grid)

    # 调 create_tiles_task 在内存里创建一条pending记录
    task_id = create_tiles_task(
        orthophoto_path,
        str(OUTPUT_DIR),
        tile_config,
        task_id=task_id,
    )

    if project_id is not None:
        # 如果 project_id 有值，但当前内存中的 project对象为空
        # 调用create_project(project_id, task_id, period_name) 在数据库中创建该项目记录。
        if project is None:
            project = create_project(project_id, task_id, period_name)
            base_task_id = task_id
        else:
            # 如果project对象不为空（数据库里已有该项目
            project = append_project_task(project_id, task_id, period_name, reference_task_id)
            base_task_id = project["base_task_id"]

    return {
        "task_id": task_id,
        "status": "pending",
        "task_type": "tiles",
        "tile_config": tile_config,
        "project_id": project_id,
        "period_name": period_name,
        "reference_task_id": reference_task_id,
        "base_task_id": base_task_id,
    }


@tile_router.post("/api/tiles/process-pair")
async def process_tiles_pair(
    orthophoto_file_a: UploadFile | str | None = File(...),
    orthophoto_file_b: UploadFile | str | None = File(...),
    tile_size: int = Form(1024),
    skip_empty_tiles: bool = Form(True),
    export_png: bool = Form(True),
    grid_origin_x: str | None = Form(None),
    grid_origin_y: str | None = Form(None),
    grid_pixel_size_x: str | None = Form(None),
    grid_pixel_size_y: str | None = Form(None),
    aoi_geojson: str | None = Form(None),
    aoi_crs: str | None = Form(None),
):
    if not isinstance(orthophoto_file_a, StarletteUploadFile):
        raise HTTPException(400, "orthophoto_file_a must be an uploaded file")
    if not isinstance(orthophoto_file_b, StarletteUploadFile):
        raise HTTPException(400, "orthophoto_file_b must be an uploaded file")

    task_id = str(uuid4())
    image_a_path = await run_in_threadpool(save_uploaded_file, f"{task_id}-a", orthophoto_file_a)
    image_b_path = await run_in_threadpool(save_uploaded_file, f"{task_id}-b", orthophoto_file_b)
    tile_config = build_tile_config(
        tile_size,
        skip_empty_tiles,
        export_png,
        grid_origin_x,
        grid_origin_y,
        grid_pixel_size_x,
        grid_pixel_size_y,
        aoi_geojson,
        aoi_crs,
    )

    task_id = create_tiles_pair_task(
        image_a_path,
        image_b_path,
        str(OUTPUT_DIR),
        tile_config,
        task_id=task_id,
    )

    return {
        "task_id": task_id,
        "status": "pending",
        "task_type": "tiles_pair",
        "tile_config": tile_config,
    }


@tile_router.get("/api/task/{task_id}/status")
async def get_status(task_id: str):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@tile_router.get("/api/task/{task_id}/download/orthophoto")
async def download_orthophoto(task_id: str):
    task = require_completed_task(task_id)
    path = task.get("orthophoto_path")
    if not path or not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="image/tiff", filename="orthophoto.tif")


@tile_router.get("/api/task/{task_id}/download/tiles")
async def download_tiles(task_id: str):
    task = require_completed_task(task_id)
    path = task.get("tiles_zip_path")
    if not path or not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="application/zip", filename="tiles.zip")


@tile_router.get("/api/task/{task_id}/download/tiles-pair")
async def download_tiles_pair(task_id: str):
    task = require_completed_task(task_id)
    if task.get("task_type") != "tiles_pair":
        raise HTTPException(400, "Task is not a tiles_pair task")
    path = task.get("tiles_pair_zip_path")
    if not path or not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="application/zip", filename="tiles_pair.zip")


@tile_router.get("/api/task/{task_id}/download/manifest")
async def download_manifest(task_id: str):
    task = require_completed_task(task_id)
    path = task.get("manifest_path")
    if not path or not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="application/json", filename="manifest.json")


@tile_router.get("/api/task/{task_id}/annotations/boxes")
async def get_annotation_boxes(task_id: str):
    task = require_completed_task(task_id)
    path = annotation_boxes_path(task_id, task)
    if path.exists():
        return json.loads(path.read_text())
    return build_annotation_boxes_from_manifest(task_id, task)


@tile_router.put("/api/task/{task_id}/annotations/boxes")
async def save_annotation_boxes(task_id: str, payload: AnnotationBoxesPayload):
    task = require_completed_task(task_id)
    path = annotation_boxes_path(task_id, task)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = model_to_plain_dict(payload)
    data["task_id"] = task_id
    data["schema_version"] = 1
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2))
    return data
