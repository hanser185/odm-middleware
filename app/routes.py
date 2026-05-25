import json
import uuid
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse
from pyodm.exceptions import OdmError

from .models import TaskResponse, TaskStatus
from .services import (
    get_node_client,
    cleanup_task_dir,
    write_task_info,
    read_task_info,
    update_task_info_status,
    safe_upload_filename,
    parse_odm_options,
    raise_nodeodm_error,
)
from .config import TEMP_DIR, DEFAULT_ODM_OPTIONS

router = APIRouter()


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
        if file.content_type not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型：{file.content_type}，仅支持 {allowed_types}",
            )

    task_id = str(uuid.uuid4())
    task_dir = TEMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    file_paths = []
    try:
        for file in files:
            file_path = task_dir / safe_upload_filename(file.filename)
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            file_paths.append(str(file_path))
    except HTTPException:
        cleanup_task_dir(task_dir)
        raise
    except Exception as e:
        cleanup_task_dir(task_dir)
        raise HTTPException(status_code=500, detail=f"文件保存失败：{str(e)}")

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
            "odm_georeferencing/odm_georeferenced_model.las",
            "odm_georeferencing/odm_georeferenced_model.copc.laz",
            "odm_georeferencing/odm_georeferenced_model.ply",
            "odm_texturing/odm_textured_model_geo.obj",
            "odm_texturing/odm_textured_model_geo.mtl",
        ]

        task = client.create_task(
            files=file_paths,
            options=odm_options,
            name=task_name,
            outputs=outputs,
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
        raise HTTPException(status_code=500, detail=f"服务器错误：{str(e)}")


@router.get("/api/v1/status/{task_id}", response_model=TaskStatus)
async def get_task_status(task_id: str):
    task_dir = TEMP_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    task_info = read_task_info(task_dir)
    task_uuid = task_info["node_task_uuid"]
    try:
        client = get_node_client()
        task = client.get_task(task_uuid)
        info = task.info()
        local_status = info.status.name.lower()
        update_task_info_status(task_dir, local_status)
        return TaskStatus(
            task_id=task_id,
            status=local_status,
            progress=info.progress,
            images_count=info.images_count,
            processing_time=info.processing_time if info.processing_time > 0 else None,
            error=info.last_error if info.last_error else None,
        )
    except OdmError as e:
        update_task_info_status(task_dir, "failed")
        raise_nodeodm_error("NodeODM 服务错误", e)
    except Exception as e:
        update_task_info_status(task_dir, "failed")
        raise HTTPException(status_code=500, detail=f"查询失败：{str(e)}")


@router.get("/api/v1/download/{task_id}")
async def download_orthophoto(task_id: str):
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
        assets_path = task.download_assets(str(task_dir))
        orthophoto_path = Path(assets_path) / "odm_orthophoto" / "odm_orthophoto.tif"
        if not orthophoto_path.exists():
            raise HTTPException(status_code=404, detail="正射影像文件不存在")
        return FileResponse(
            orthophoto_path,
            media_type="image/tiff",
            filename=f"orthophoto_{task_id[:8]}.tif",
        )
    except OdmError as e:
        raise_nodeodm_error("NodeODM 服务错误", e)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载失败：{str(e)}")


@router.get("/api/v1/download/{task_id}/zip")
async def download_all_assets(task_id: str):
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
        zip_path = task.download_zip(str(task_dir / "results.zip"))
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=f"odm_results_{task_id[:8]}.zip",
        )
    except OdmError as e:
        raise_nodeodm_error("NodeODM 服务错误", e)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载失败：{str(e)}")


@router.delete("/api/v1/tasks/{task_id}")
async def cancel_task(task_id: str):
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
        cleanup_task_dir(task_dir)
        raise_nodeodm_error("NodeODM 服务错误", e)
    except Exception as e:
        cleanup_task_dir(task_dir)
        raise HTTPException(status_code=500, detail=f"删除失败：{str(e)}")


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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取信息失败：{str(e)}")


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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取选项失败：{str(e)}")


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
