import json
import shutil
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from pyodm import Node
from pyodm.exceptions import OdmError

from .config import NODEODM_HOST, NODEODM_PORT, NODEODM_TOKEN, TEMP_DIR, TASK_TTL_HOURS, CLEANUP_INTERVAL

node_client = None


def get_node_client() -> Node:
    global node_client
    if node_client is None:
        node_client = Node(NODEODM_HOST, NODEODM_PORT, token=NODEODM_TOKEN)
    return node_client


def reset_node_client() -> None:
    global node_client
    node_client = None


def raise_nodeodm_error(message: str, error: Exception) -> None:
    reset_node_client()
    raise HTTPException(status_code=503, detail=f"{message}：{str(error)}")


def cleanup_task_dir(task_dir: Path) -> None:
    if task_dir.exists():
        shutil.rmtree(task_dir)


def write_task_info(task_dir: Path, task_uuid: str, task_name: str, files_count: int) -> None:
    task_info = {
        "node_task_uuid": task_uuid,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "name": task_name,
        "files_count": files_count,
        "status": "queued",
    }
    task_info_path = task_dir / "task_info.json"
    with open(task_info_path, "w", encoding="utf-8") as f:
        json.dump(task_info, f, ensure_ascii=False, indent=2)


def update_task_info_status(task_dir: Path, status: str) -> None:
    task_info = read_task_info(task_dir)
    task_info["status"] = status
    task_info_path = task_dir / "task_info.json"
    with open(task_info_path, "w", encoding="utf-8") as f:
        json.dump(task_info, f, ensure_ascii=False, indent=2)


def read_task_info(task_dir: Path) -> dict:
    task_info_path = task_dir / "task_info.json"
    if not task_info_path.exists():
        raise HTTPException(status_code=404, detail="任务信息丢失")
    with open(task_info_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    try:
        task_info = json.loads(content)
    except json.JSONDecodeError:
        task_info = {"node_task_uuid": content}
    if not task_info.get("node_task_uuid"):
        raise HTTPException(status_code=404, detail="任务信息丢失")
    return task_info


def safe_upload_filename(filename: str) -> str:
    safe_name = Path(filename or "").name
    if not safe_name:
        raise HTTPException(status_code=400, detail="上传文件名无效")
    return safe_name


def parse_odm_options(options: str) -> dict:
    try:
        parsed_options = json.loads(options)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="options 必须是合法的 JSON 字符串")
    if not isinstance(parsed_options, dict):
        raise HTTPException(status_code=400, detail="options 必须是 JSON 对象")
    return parsed_options


def _try_remove_remote_task(task_uuid: str) -> None:
    try:
        client = get_node_client()
        task = client.get_task(task_uuid)
        info = task.info()
        if info.status.name in ["RUNNING", "QUEUED"]:
            return
        task.remove()
    except Exception:
        pass


def _cleanup_expired_tasks(temp_dir: Path, max_age_hours: float) -> None:
    if not temp_dir.exists():
        return
    now = datetime.now(timezone.utc)
    for item in temp_dir.iterdir():
        if not item.is_dir():
            continue
        task_info_path = item / "task_info.json"
        if not task_info_path.exists():
            cleanup_task_dir(item)
            continue
        try:
            with open(task_info_path, "r", encoding="utf-8") as f:
                task_info = json.load(f)
        except (json.JSONDecodeError, OSError):
            cleanup_task_dir(item)
            continue
        created_at_str = task_info.get("created_at")
        if not created_at_str:
            cleanup_task_dir(item)
            continue
        try:
            created_at = datetime.fromisoformat(created_at_str)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            cleanup_task_dir(item)
            continue
        task_status = task_info.get("status", "")
        if task_status in ("queued", "running"):
            continue
        age_hours = (now - created_at).total_seconds() / 3600
        if age_hours > max_age_hours:
            node_task_uuid = task_info.get("node_task_uuid")
            if node_task_uuid:
                _try_remove_remote_task(node_task_uuid)
            cleanup_task_dir(item)


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        _cleanup_expired_tasks(TEMP_DIR, TASK_TTL_HOURS)
