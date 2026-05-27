import io
import json
import logging
import re
import shutil
import uuid
import zipfile
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
    safe_upload_filename,
    parse_odm_options,
    raise_nodeodm_error,
    publish_task_status,
)
from .config import TEMP_DIR, DEFAULT_ODM_OPTIONS, WEBHOOK_BASE_URL, MAX_UPLOAD_SIZE_BYTES, NODEODM_HOST, NODEODM_PORT, NODEODM_TOKEN

router = APIRouter()

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
