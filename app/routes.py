import io
import json
import logging
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse, StreamingResponse
from pyodm.exceptions import OdmError

from .models import TaskResponse, TaskStatus, TaskSummary
from .services import (
    get_node_client,
    cleanup_task_dir,
    write_task_info,
    read_task_info,
    update_task_info_status,
    update_process_and_tile_info,
    update_process_and_tile_started,
    update_process_and_tile_failed,
    safe_upload_filename,
    parse_odm_options,
    raise_nodeodm_error,
    publish_task_status,
)
from .config import TEMP_DIR, DEFAULT_ODM_OPTIONS, WEBHOOK_BASE_URL, MAX_UPLOAD_SIZE_BYTES, NODEODM_HOST, NODEODM_PORT, NODEODM_TOKEN
from .tile_routes import OUTPUT_DIR as TILE_OUTPUT_DIR, build_tile_config
from .tile_tasks import create_tiles_pair_task, create_tiles_task, get_task as get_tile_task

router = APIRouter()
logger = logging.getLogger("odm")

UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _validate_task_id(task_id: str) -> None:
    if not UUID_PATTERN.match(task_id):
        raise HTTPException(status_code=400, detail="无效的任务 ID 格式")


def _handle_odm_error(task_dir: Path, error: OdmError) -> None:
    err = str(error).lower()
    if "task not found" in err or "task does not exist" in err:
        cleanup_task_dir(task_dir)
        raise HTTPException(status_code=404, detail="任务在 NodeODM 侧已被删除")
    raise_nodeodm_error("NodeODM 服务错误", error)


def _nodeodm_download_url(task_uuid: str, asset_path: str) -> str:
    url = f"http://{NODEODM_HOST}:{NODEODM_PORT}/task/{task_uuid}/download/{asset_path}"
    if NODEODM_TOKEN:
        return f"{url}?token={NODEODM_TOKEN}"
    return url


async def _open_nodeodm_stream(node_url: str, error_status: int, error_detail: str):
    client = httpx.AsyncClient(timeout=None)
    stream_context = client.stream("GET", node_url)
    try:
        response = await stream_context.__aenter__()
        if response.status_code != 200:
            await stream_context.__aexit__(None, None, None)
            if hasattr(client, "aclose"):
                await client.aclose()
            raise HTTPException(status_code=error_status, detail=error_detail)

        async def iter_bytes():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await stream_context.__aexit__(None, None, None)
                if hasattr(client, "aclose"):
                    await client.aclose()

        return iter_bytes()
    except HTTPException:
        raise
    except Exception:
        if hasattr(client, "aclose"):
            await client.aclose()
        raise


async def _extract_orthophoto_from_nodeodm_zip_to_path(task_uuid: str, output_path: Path) -> Path:
    zip_url = _nodeodm_download_url(task_uuid, "all.zip")
    async with httpx.AsyncClient(timeout=None) as http_client:
        resp = await http_client.get(zip_url)
    if resp.status_code != 200:
        raise RuntimeError("从 NodeODM 下载压缩包失败")

    tif_rel = "odm_orthophoto/odm_orthophoto.tif"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        if tif_rel not in zf.namelist():
            raise RuntimeError("正射影像文件不存在")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(zf.read(tif_rel))
    return output_path


async def _extract_orthophoto_from_nodeodm_zip(task_uuid: str, task_dir: Path) -> Path:
    return await _extract_orthophoto_from_nodeodm_zip_to_path(task_uuid, task_dir / "orthophoto.tif")


async def _start_tile_task_after_odm_completion(task_id: str, task_dir: Path, task_uuid: str, task_info: dict) -> None:
    if task_info.get("workflow") != "process_and_tile":
        return
    if task_info.get("tile_task_id"):
        return

    try:
        orthophoto_path = await _extract_orthophoto_from_nodeodm_zip(task_uuid, task_dir)
        tile_task_id = create_tiles_task(
            str(orthophoto_path),
            str(TILE_OUTPUT_DIR),
            task_info.get("tile_config") or build_tile_config(
                1024,
                True,
                True,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        )
        update_process_and_tile_started(task_dir, tile_task_id, str(orthophoto_path))
    except Exception as e:
        logger.warning("任务 %s 自动切片启动失败：%s", task_id, e)
        update_process_and_tile_failed(task_dir, str(e))



def _pair_task_info_path(task_dir: Path) -> Path:
    return task_dir / "task_info.json"


def _read_pair_task_info(task_dir: Path) -> dict:
    path = _pair_task_info_path(task_dir)
    if not path.exists():
        raise HTTPException(status_code=404, detail="任务信息丢失")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=404, detail="任务信息丢失") from exc


def _write_pair_task_info(task_dir: Path, task_info: dict) -> None:
    path = _pair_task_info_path(task_dir)
    path.write_text(json.dumps(task_info, ensure_ascii=False, indent=2), encoding="utf-8")


def _require_pair_task(task_id: str) -> tuple[Path, dict]:
    _validate_task_id(task_id)
    task_dir = TEMP_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    task_info = _read_pair_task_info(task_dir)
    if task_info.get("workflow") != "process_pair_and_tile":
        raise HTTPException(status_code=400, detail="任务不是成对正射切片任务")
    return task_dir, task_info


def _validate_uploads(files: list[UploadFile], field_name: str) -> None:
    if not files:
        raise HTTPException(status_code=400, detail=f"{field_name} 至少需要上传一张照片")
    allowed_types = ["image/jpeg", "image/png", "image/tiff"]
    for file in files:
        if file.content_type.lower() not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型：{file.content_type}，仅支持 {allowed_types}",
            )


def _save_upload_files(files: list[UploadFile], target_dir: Path) -> list[str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    file_paths = []
    total_bytes = 0
    for file in files:
        file_path = target_dir / safe_upload_filename(file.filename)
        written = 0
        with open(file_path, "wb") as buffer:
            while True:
                chunk = file.file.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_SIZE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"单文件大小超过限制（{MAX_UPLOAD_SIZE_BYTES // 1024 // 1024}MB）",
                    )
                buffer.write(chunk)
        total_bytes += written
        if total_bytes > MAX_UPLOAD_SIZE_BYTES * 2:
            raise HTTPException(status_code=413, detail="所有文件总大小超过限制")
        file_paths.append(str(file_path))
    return file_paths


def _node_outputs() -> list[str]:
    return [
        "odm_orthophoto/odm_orthophoto.tif",
        "odm_orthophoto/odm_orthophoto.png",
        "odm_orthophoto/odm_orthophoto.mbtiles",
        "odm_orthophoto/odm_orthophoto.tiles",
        "odm_dem/odm_dem.tif",
    ]


def _phase_task_uuid(task_info: dict, phase: str) -> str:
    phase_info = task_info.get(phase)
    if not isinstance(phase_info, dict) or not phase_info.get("node_task_uuid"):
        raise HTTPException(status_code=404, detail="阶段任务信息丢失")
    return phase_info["node_task_uuid"]


def _pair_combined_status(task_id: str, task_info: dict) -> dict:
    tile_task_id = task_info.get("tile_task_id")
    tile_task = get_tile_task(tile_task_id) if tile_task_id else None
    tile_status = (tile_task or {}).get("status") or task_info.get("tile_status", "waiting_for_orthophotos")
    base_status = task_info.get("base", {}).get("status", "unknown")
    compare_status = task_info.get("compare", {}).get("status", "unknown")

    if base_status in ("failed", "canceled") or compare_status in ("failed", "canceled"):
        status = "failed"
    elif tile_status == "completed":
        status = "completed"
    elif tile_status == "failed":
        status = "failed"
    elif base_status == "completed" and compare_status == "completed":
        status = "tiling"
    elif base_status == "running" or compare_status == "running":
        status = "running"
    else:
        status = "queued"

    response = {
        "task_id": task_id,
        "status": status,
        "workflow": "process_pair_and_tile",
        "base_status": base_status,
        "compare_status": compare_status,
        "tile_status": tile_status,
        "tile_task_id": tile_task_id,
        "tiles_pair_download_url": f"/api/v1/process-pair-and-tile/{task_id}/download/tiles-pair",
    }
    for key in ("error", "tile_error"):
        if task_info.get(key):
            response[key] = task_info[key]
    if tile_task:
        response["progress"] = tile_task.get("progress", 0)
        response["children"] = tile_task.get("children")
        response["results"] = tile_task.get("results")
        response["tiles_pair_oss_url"] = tile_task.get("tiles_pair_oss_url")
    return response


@router.post("/api/v1/process-pair-and-tile")
async def process_pair_and_tile(
    base_files: list[UploadFile] = File(..., description="基准期无人机照片"),
    compare_files: list[UploadFile] = File(..., description="对比期无人机照片"),
    options: str = Form("{}", description="ODM 处理选项 JSON 字符串，支持所有 NodeODM 参数"),
    name: Optional[str] = Form(None, description="任务名称"),
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
    _validate_uploads(base_files, "base_files")
    _validate_uploads(compare_files, "compare_files")
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

    task_id = str(uuid.uuid4())
    task_dir = TEMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        base_paths = _save_upload_files(base_files, task_dir / "media" / "base")
        compare_paths = _save_upload_files(compare_files, task_dir / "media" / "compare")
    except HTTPException:
        cleanup_task_dir(task_dir)
        raise
    except Exception:
        cleanup_task_dir(task_dir)
        raise HTTPException(status_code=500, detail="文件保存失败")
    finally:
        for file in [*base_files, *compare_files]:
            file.file.close()

    try:
        client = get_node_client()
        user_options = parse_odm_options(options)
        odm_options = {**DEFAULT_ODM_OPTIONS, **user_options}
        task_name = name if name else f"Task-{task_id[:8]}"
        base_task = client.create_task(
            files=base_paths,
            options=odm_options,
            name=f"{task_name}-base",
            outputs=_node_outputs(),
            webhook=f"{WEBHOOK_BASE_URL}/api/v1/process-pair-and-tile/{task_id}/webhook/base",
        )
        compare_task = client.create_task(
            files=compare_paths,
            options=odm_options,
            name=f"{task_name}-compare",
            outputs=_node_outputs(),
            webhook=f"{WEBHOOK_BASE_URL}/api/v1/process-pair-and-tile/{task_id}/webhook/compare",
        )
        task_info = {
            "node_task_uuid": str(base_task.uuid),
            "workflow": "process_pair_and_tile",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "name": task_name,
            "status": "queued",
            "base": {"node_task_uuid": str(base_task.uuid), "status": "queued", "files_count": len(base_files)},
            "compare": {"node_task_uuid": str(compare_task.uuid), "status": "queued", "files_count": len(compare_files)},
            "tile_status": "waiting_for_orthophotos",
            "tile_config": tile_config,
        }
        _write_pair_task_info(task_dir, task_info)
        return {
            "task_id": task_id,
            "status": "queued",
            "workflow": "process_pair_and_tile",
            "message": f"任务已创建，base {len(base_files)} 张，compare {len(compare_files)} 张，完成后将在服务内自动成对切片",
        }
    except OdmError as e:
        cleanup_task_dir(task_dir)
        raise_nodeodm_error("NodeODM 服务错误", e)
    except HTTPException:
        cleanup_task_dir(task_dir)
        raise
    except Exception:
        cleanup_task_dir(task_dir)
        raise HTTPException(status_code=500, detail="服务器错误")


@router.post("/api/v1/process-pair-and-tile/{task_id}/webhook/{phase}")
async def process_pair_and_tile_webhook(task_id: str, phase: str):
    if phase not in {"base", "compare"}:
        raise HTTPException(status_code=400, detail="无效的任务阶段")
    task_dir, task_info = _require_pair_task(task_id)
    task_uuid = _phase_task_uuid(task_info, phase)
    try:
        client = get_node_client()
        task = client.get_task(task_uuid)
        info = task.info()
        node_status = info.status.name.lower()
        logger.info("[%s] webhook/%s 到达, NodeODM 状态: %s", task_id, phase, node_status)
        task_info[phase]["status"] = node_status
        if node_status == "failed" and info.last_error:
            task_info[phase]["error"] = info.last_error
            task_info["error"] = info.last_error
            logger.warning("[%s] webhook/%s ODM 任务失败: %s", task_id, phase, info.last_error)
        elif node_status != "failed":
            task_info[phase].pop("error", None)
        if node_status == "completed" and not task_info[phase].get("orthophoto_path"):
            logger.info("[%s] webhook/%s 开始提取正射图", task_id, phase)
            output_path = task_dir / "orthophotos" / ("base.tif" if phase == "base" else "compare.tif")
            orthophoto_path = await _extract_orthophoto_from_nodeodm_zip_to_path(task_uuid, output_path)
            task_info[phase]["orthophoto_path"] = str(orthophoto_path)
            logger.info("[%s] webhook/%s 正射图已提取: %s", task_id, phase, orthophoto_path)
        _write_pair_task_info(task_dir, task_info)

        latest_info = _read_pair_task_info(task_dir)
        if (
            latest_info.get("base", {}).get("status") == "completed"
            and latest_info.get("compare", {}).get("status") == "completed"
            and not latest_info.get("tile_task_id")
        ):
            logger.info("[%s] 正射图均已就绪 (base=%s, compare=%s), 启动成对切片任务",
                        task_id,
                        latest_info["base"]["orthophoto_path"],
                        latest_info["compare"]["orthophoto_path"])
            tile_task_id = create_tiles_pair_task(
                latest_info["base"]["orthophoto_path"],
                latest_info["compare"]["orthophoto_path"],
                str(TILE_OUTPUT_DIR),
                latest_info.get("tile_config") or build_tile_config(1024, True, True, None, None, None, None, None, None),
                task_id=f"{task_id}-pair",
            )
            logger.info("[%s] 成对切片任务已提交, tile_task_id=%s", task_id, tile_task_id)
            latest_info["tile_task_id"] = tile_task_id
            latest_info["tile_status"] = "processing"
            latest_info.pop("tile_error", None)
            _write_pair_task_info(task_dir, latest_info)
    except OdmError as e:
        if "task not found" in str(e).lower() or "task does not exist" in str(e).lower():
            if task_dir.exists():
                cleanup_task_dir(task_dir)
            return {"received": True}
        raise HTTPException(status_code=503, detail="NodeODM 暂时不可用")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("process-pair-and-tile webhook %s/%s 处理异常：%s", task_id, phase, e)
        task_info = _read_pair_task_info(task_dir)
        task_info["tile_status"] = "failed"
        task_info["tile_error"] = str(e)
        _write_pair_task_info(task_dir, task_info)
    return {"received": True}


@router.get("/api/v1/process-pair-and-tile/{task_id}/status")
async def get_process_pair_and_tile_status(task_id: str):
    _, task_info = _require_pair_task(task_id)
    return _pair_combined_status(task_id, task_info)


@router.get("/api/v1/process-pair-and-tile/{task_id}/download/tiles-pair")
async def download_process_pair_and_tile_tiles_pair(task_id: str):
    _, task_info = _require_pair_task(task_id)
    tile_task_id = task_info.get("tile_task_id")
    if not tile_task_id:
        raise HTTPException(status_code=400, detail="成对切片任务尚未开始")
    tile_task = get_tile_task(tile_task_id)
    if not tile_task or tile_task.get("status") != "completed":
        raise HTTPException(status_code=400, detail="成对切片任务尚未完成")
    path = tile_task.get("tiles_pair_zip_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path, media_type="application/zip", filename=f"tiles_pair_{task_id[:8]}.zip")

@router.post("/api/v1/process", response_model=TaskResponse)
async def process_images(
    files: list[UploadFile] = File(..., description="无人机拍摄的照片文件"),
    options: str = Form("{}", description="ODM 处理选项 JSON 字符串，支持所有 NodeODM 参数"),
    name: Optional[str] = Form(None, description="任务名称"),
):
    if not files:
        raise HTTPException(status_code=400, detail="至少需要上传一张照片")

    allowed_types = ["image/jpeg", "image/png", "image/tiff"]
    for file in files:
        if file.content_type.lower() not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型：{file.content_type}，仅支持 {allowed_types}",
            )

    task_id = str(uuid.uuid4())
    task_dir = TEMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    file_paths = []
    total_bytes = 0
    try:
        for file in files:
            file_path = task_dir / safe_upload_filename(file.filename)
            written = 0
            with open(file_path, "wb") as buffer:
                while True:
                    chunk = file.file.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD_SIZE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"单文件大小超过限制（{MAX_UPLOAD_SIZE_BYTES // 1024 // 1024}MB）",
                        )
                    buffer.write(chunk)
            total_bytes += written
            if total_bytes > MAX_UPLOAD_SIZE_BYTES * 2:
                raise HTTPException(
                    status_code=413,
                    detail="所有文件总大小超过限制",
                )
            file_paths.append(str(file_path))
    except HTTPException:
        cleanup_task_dir(task_dir)
        raise
    except Exception as e:
        cleanup_task_dir(task_dir)
        raise HTTPException(status_code=500, detail="文件保存失败")
    finally:
        for file in files:
            file.file.close()

    try:
        client = get_node_client()

        user_options = parse_odm_options(options)

        odm_options = {**DEFAULT_ODM_OPTIONS, **user_options}

        task_name = name if name else f"Task-{task_id[:8]}"

        outputs = [
            "odm_orthophoto/odm_orthophoto.tif",
            "odm_orthophoto/odm_orthophoto.png",
            "odm_orthophoto/odm_orthophoto.mbtiles",
            "odm_orthophoto/odm_orthophoto.tiles",
            "odm_dem/odm_dem.tif",
        ]

        task = client.create_task(
            files=file_paths,
            options=odm_options,
            name=task_name,
            outputs=outputs,
            webhook=f"{WEBHOOK_BASE_URL}/api/v1/webhook/{task_id}",
        )

        write_task_info(task_dir, str(task.uuid), task_name, len(files))

        return TaskResponse(
            task_id=task_id,
            status="queued",
            message=f"任务已创建，共 {len(files)} 张照片",
        )

    except OdmError as e:
        cleanup_task_dir(task_dir)
        raise_nodeodm_error("NodeODM 服务错误", e)
    except HTTPException:
        cleanup_task_dir(task_dir)
        raise
    except Exception as e:
        cleanup_task_dir(task_dir)
        raise HTTPException(status_code=500, detail="服务器错误")


@router.post("/api/v1/process-and-tile")
async def process_images_and_tile(
    files: list[UploadFile] = File(..., description="无人机拍摄的照片文件"),
    options: str = Form("{}", description="ODM 处理选项 JSON 字符串，支持所有 NodeODM 参数"),
    name: Optional[str] = Form(None, description="任务名称"),
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
    if not files:
        raise HTTPException(status_code=400, detail="至少需要上传一张照片")

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

    allowed_types = ["image/jpeg", "image/png", "image/tiff"]
    for file in files:
        if file.content_type.lower() not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型：{file.content_type}，仅支持 {allowed_types}",
            )

    task_id = str(uuid.uuid4())
    task_dir = TEMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    file_paths = []
    total_bytes = 0
    try:
        for file in files:
            file_path = task_dir / safe_upload_filename(file.filename)
            written = 0
            with open(file_path, "wb") as buffer:
                while True:
                    chunk = file.file.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD_SIZE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"单文件大小超过限制（{MAX_UPLOAD_SIZE_BYTES // 1024 // 1024}MB）",
                        )
                    buffer.write(chunk)
            total_bytes += written
            if total_bytes > MAX_UPLOAD_SIZE_BYTES * 2:
                raise HTTPException(status_code=413, detail="所有文件总大小超过限制")
            file_paths.append(str(file_path))
    except HTTPException:
        cleanup_task_dir(task_dir)
        raise
    except Exception:
        cleanup_task_dir(task_dir)
        raise HTTPException(status_code=500, detail="文件保存失败")
    finally:
        for file in files:
            file.file.close()

    try:
        client = get_node_client()
        user_options = parse_odm_options(options)
        odm_options = {**DEFAULT_ODM_OPTIONS, **user_options}
        task_name = name if name else f"Task-{task_id[:8]}"
        outputs = [
            "odm_orthophoto/odm_orthophoto.tif",
            "odm_orthophoto/odm_orthophoto.png",
            "odm_orthophoto/odm_orthophoto.mbtiles",
            "odm_orthophoto/odm_orthophoto.tiles",
            "odm_dem/odm_dem.tif",
        ]
        task = client.create_task(
            files=file_paths,
            options=odm_options,
            name=task_name,
            outputs=outputs,
            webhook=f"{WEBHOOK_BASE_URL}/api/v1/webhook/{task_id}",
        )

        write_task_info(task_dir, str(task.uuid), task_name, len(files))
        update_process_and_tile_info(task_dir, tile_config)

        return {
            "task_id": task_id,
            "status": "queued",
            "workflow": "process_and_tile",
            "message": f"任务已创建，共 {len(files)} 张照片，完成后将自动切片",
            "tile_config": tile_config,
        }
    except OdmError as e:
        cleanup_task_dir(task_dir)
        raise_nodeodm_error("NodeODM 服务错误", e)
    except HTTPException:
        cleanup_task_dir(task_dir)
        raise
    except Exception:
        cleanup_task_dir(task_dir)
        raise HTTPException(status_code=500, detail="服务器错误")


@router.post("/api/v1/webhook/{task_id}")
async def task_webhook(task_id: str):
    _validate_task_id(task_id)
    task_dir = TEMP_DIR / task_id
    if not task_dir.exists():
        return {"received": True}
    task_info = read_task_info(task_dir)
    task_uuid = task_info["node_task_uuid"]
    try:
        client = get_node_client()
        task = client.get_task(task_uuid)
        info = task.info()
        node_status = info.status.name.lower()
        update_task_info_status(
            task_dir,
            node_status,
            error=info.last_error if node_status == "failed" and info.last_error else None,
        )
        if node_status in ("completed", "failed", "canceled"):
            publish_task_status(
                task_id=task_id,
                status=node_status,
                progress=info.progress,
                images_count=info.images_count,
                error=info.last_error if info.last_error else None,
            )
        if node_status == "completed":
            await _start_tile_task_after_odm_completion(task_id, task_dir, task_uuid, task_info)
    except OdmError as e:
        if "task not found" in str(e).lower() or "task does not exist" in str(e).lower():
            if task_dir.exists():
                cleanup_task_dir(task_dir)
            return {"received": True}
        raise HTTPException(status_code=503, detail="NodeODM 暂时不可用")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("webhook %s 处理异常：%s", task_id, e)
    return {"received": True}


def _require_process_and_tile_task(task_id: str) -> tuple[Path, dict]:
    _validate_task_id(task_id)
    task_dir = TEMP_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    task_info = read_task_info(task_dir)
    if task_info.get("workflow") != "process_and_tile":
        raise HTTPException(status_code=400, detail="任务不是自动切片任务")
    return task_dir, task_info


def _combined_status_from_info(task_id: str, task_info: dict) -> dict:
    tile_task_id = task_info.get("tile_task_id")
    tile_task = get_tile_task(tile_task_id) if tile_task_id else None
    tile_status = (tile_task or {}).get("status") or task_info.get("tile_status", "waiting_for_orthophoto")
    odm_status = task_info.get("status", "unknown")

    if odm_status in ("failed", "canceled"):
        status = odm_status
    elif tile_status == "completed":
        status = "completed"
    elif tile_status == "failed":
        status = "failed"
    elif odm_status == "completed":
        status = "tiling"
    else:
        status = odm_status

    response = {
        "task_id": task_id,
        "status": status,
        "workflow": "process_and_tile",
        "odm_status": odm_status,
        "tile_status": tile_status,
        "tile_task_id": tile_task_id,
        "images_count": task_info.get("files_count", 0),
        "orthophoto_download_url": f"/api/v1/process-and-tile/{task_id}/download/orthophoto",
        "tiles_download_url": f"/api/v1/process-and-tile/{task_id}/download/tiles",
        "manifest_download_url": f"/api/v1/process-and-tile/{task_id}/download/manifest",
    }
    if task_info.get("error"):
        response["error"] = task_info["error"]
    if task_info.get("tile_error"):
        response["tile_error"] = task_info["tile_error"]
    if tile_task:
        response["tile_progress"] = tile_task.get("progress", 0)
        response["generated_tiles"] = tile_task.get("generated_tiles")
        response["skipped_empty_tiles"] = tile_task.get("skipped_empty_tiles")
        response["grid"] = tile_task.get("grid")
        response["tiles_oss_url"] = tile_task.get("tiles_oss_url")
    return response


@router.get("/api/v1/process-and-tile/{task_id}/status")
async def get_process_and_tile_status(task_id: str):
    _, task_info = _require_process_and_tile_task(task_id)
    return _combined_status_from_info(task_id, task_info)


def _require_completed_tile_result(task_info: dict, path_key: str) -> str:
    tile_task_id = task_info.get("tile_task_id")
    if not tile_task_id:
        raise HTTPException(status_code=400, detail="切片任务尚未开始")
    tile_task = get_tile_task(tile_task_id)
    if not tile_task or tile_task.get("status") != "completed":
        raise HTTPException(status_code=400, detail="切片任务尚未完成")
    path = tile_task.get(path_key)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return path


@router.get("/api/v1/process-and-tile/{task_id}/download/orthophoto")
async def download_process_and_tile_orthophoto(task_id: str):
    _, task_info = _require_process_and_tile_task(task_id)
    path = task_info.get("orthophoto_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="正射影像文件不存在")
    return FileResponse(path, media_type="image/tiff", filename=f"orthophoto_{task_id[:8]}.tif")


@router.get("/api/v1/process-and-tile/{task_id}/download/tiles")
async def download_process_and_tile_tiles(task_id: str):
    _, task_info = _require_process_and_tile_task(task_id)
    path = _require_completed_tile_result(task_info, "tiles_zip_path")
    return FileResponse(path, media_type="application/zip", filename=f"tiles_{task_id[:8]}.zip")


@router.get("/api/v1/process-and-tile/{task_id}/download/manifest")
async def download_process_and_tile_manifest(task_id: str):
    _, task_info = _require_process_and_tile_task(task_id)
    path = _require_completed_tile_result(task_info, "manifest_path")
    return FileResponse(path, media_type="application/json", filename=f"manifest_{task_id[:8]}.json")


@router.get("/api/v1/status/{task_id}", response_model=TaskStatus)
async def get_task_status(task_id: str):
    _validate_task_id(task_id)
    task_dir = TEMP_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    task_info = read_task_info(task_dir)
    local_status = task_info.get("status", "")
    if local_status in ("completed", "failed", "canceled"):
        return TaskStatus(
            task_id=task_id,
            status=local_status,
            progress=100.0 if local_status == "completed" else 0.0,
            images_count=task_info.get("files_count", 0),
            processing_time=None,
            error=task_info.get("error") if local_status == "failed" else None,
        )
    task_uuid = task_info["node_task_uuid"]
    try:
        client = get_node_client()
        task = client.get_task(task_uuid)
        info = task.info()
        node_status = info.status.name.lower()
        update_task_info_status(task_dir, node_status)
        return TaskStatus(
            task_id=task_id,
            status=node_status,
            progress=info.progress,
            images_count=info.images_count,
            processing_time=info.processing_time if info.processing_time > 0 else None,
            error=info.last_error if info.last_error else None,
        )
    except OdmError as e:
        _handle_odm_error(task_dir, e)
    except Exception:
        raise HTTPException(status_code=500, detail="任务状态查询失败")


@router.get("/api/v1/download/{task_id}")
async def download_orthophoto(task_id: str):
    _validate_task_id(task_id)
    task_dir = TEMP_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    task_info = read_task_info(task_dir)
    task_uuid = task_info["node_task_uuid"]
    try:
        client = get_node_client()
        task = client.get_task(task_uuid)
        info = task.info()
        if info.status.name != "COMPLETED":
            raise HTTPException(status_code=400, detail=f"任务未完成，当前状态：{info.status.name}")

        zip_url = _nodeodm_download_url(task_uuid, "all.zip")
        async with httpx.AsyncClient(timeout=None) as http_client:
            resp = await http_client.get(zip_url)
            if resp.status_code != 200:
                raise HTTPException(status_code=500, detail="从 NodeODM 下载压缩包失败")

            zf = zipfile.ZipFile(io.BytesIO(resp.content))
            tif_rel = "odm_orthophoto/odm_orthophoto.tif"
            if tif_rel not in zf.namelist():
                raise HTTPException(status_code=404, detail="正射影像文件不存在")

            output_path = task_dir / "orthophoto.tif"
            output_path.write_bytes(zf.read(tif_rel))

        return FileResponse(
            str(output_path),
            media_type="image/tiff",
            filename=f"orthophoto_{task_id[:8]}.tif",
        )
    except OdmError as e:
        _handle_odm_error(task_dir, e)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="正射影像下载失败")


@router.get("/api/v1/download/{task_id}/zip")
async def download_all_assets(task_id: str):
    _validate_task_id(task_id)
    task_dir = TEMP_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    task_info = read_task_info(task_dir)
    task_uuid = task_info["node_task_uuid"]
    try:
        client = get_node_client()
        task = client.get_task(task_uuid)
        info = task.info()
        if info.status.name != "COMPLETED":
            raise HTTPException(status_code=400, detail=f"任务未完成，当前状态：{info.status.name}")
        node_url = _nodeodm_download_url(task_uuid, "all.zip")
        stream = await _open_nodeodm_stream(node_url, 500, "NodeODM 返回异常")
        return StreamingResponse(
            stream,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="odm_results_{task_id[:8]}.zip"'},
        )
    except OdmError as e:
        _handle_odm_error(task_dir, e)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="结果下载失败")
        

@router.delete("/api/v1/tasks/{task_id}")
async def cancel_task(task_id: str):
    _validate_task_id(task_id)
    task_dir = TEMP_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    task_info = read_task_info(task_dir)
    task_uuid = task_info["node_task_uuid"]
    try:
        client = get_node_client()
        task = client.get_task(task_uuid)
        if task.info().status.name in ["QUEUED", "RUNNING"]:
            task.cancel()
        task.remove()
        cleanup_task_dir(task_dir)
        return {"message": "任务已删除", "task_id": task_id}
    except OdmError as e:
        _handle_odm_error(task_dir, e)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="删除任务失败")


@router.get("/api/v1/node/info")
async def get_node_info():
    try:
        client = get_node_client()
        info = client.info()
        return {
            "version": info.version,
            "engine": info.engine,
            "engine_version": info.engine_version,
            "cpu_cores": info.cpu_cores,
            "total_memory_gb": round(info.total_memory / (1024**3), 2),
            "available_memory_gb": round(info.available_memory / (1024**3), 2),
            "task_queue_count": info.task_queue_count,
            "max_parallel_tasks": info.max_parallel_tasks,
        }
    except OdmError as e:
        raise_nodeodm_error("NodeODM 服务不可用", e)
    except Exception:
        raise HTTPException(status_code=500, detail="获取节点信息失败")


@router.get("/api/v1/node/options")
async def get_node_options():
    try:
        client = get_node_client()
        options = client.options()
        return [
            {
                "name": opt.name,
                "type": opt.type,
                "default": opt.value,
                "help": opt.help,
                "domain": opt.domain,
            }
            for opt in options
        ]
    except OdmError as e:
        raise_nodeodm_error("NodeODM 服务不可用", e)
    except Exception:
        raise HTTPException(status_code=500, detail="获取处理选项失败")


@router.get("/api/v1/tasks", response_model=list[TaskSummary])
async def list_tasks():
    if not TEMP_DIR.exists():
        return []
    items = []
    for entry in sorted(TEMP_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_dir():
            continue
        info_path = entry / "task_info.json"
        if not info_path.exists():
            continue
        try:
            info = json.loads(info_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not info.get("node_task_uuid"):
            continue
        status = info.get("status", "unknown")
        items.append(TaskSummary(
            task_id=entry.name,
            name=info.get("name", ""),
            status=status,
            progress=100.0 if status == "completed" else float(info.get("progress", 0.0)),
            images_count=info.get("files_count", 0),
            created_at=info.get("created_at", ""),
            error=info.get("error"),
        ))
    return items


@router.get("/health")
async def health_check():
    result = {
        "status": "unhealthy",
        "version": "1.0.0",
        "nodeodm_connected": False,
    }
    try:
        client = get_node_client()
        info = client.info()
        result.update(
            status="healthy",
            nodeodm_connected=True,
            nodeodm_version=info.version,
            nodeodm_engine_version=info.engine_version,
            nodeodm_cpu_cores=info.cpu_cores,
            nodeodm_task_queue_count=info.task_queue_count,
        )
    except Exception as e:
        result["error"] = str(e)
    return result
