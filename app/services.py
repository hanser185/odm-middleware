import json
import logging
import shutil
import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from pyodm import Node
from pyodm.exceptions import OdmError

import paho.mqtt.client as mqtt

from .config import (
    NODEODM_HOST, NODEODM_PORT, NODEODM_TOKEN,
    TEMP_DIR, TASK_TTL_HOURS, TILE_TASK_TTL_HOURS, CLEANUP_INTERVAL,
    TILE_UPLOAD_DIR, TILE_OUTPUT_DIR, TILE_TASKS_DIR,
    MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_TOPIC_PREFIX,
)

logger = logging.getLogger("odm")

node_client = None
_node_client_lock = threading.Lock()

_mqtt_client = None
_mqtt_connected = False
_mqtt_lock = threading.Lock()

_task_info_locks = {}
_task_info_locks_lock = threading.Lock()
_TILE_TERMINAL_STATUSES = {"completed", "failed"}


def get_mqtt_client() -> mqtt.Client:
    global _mqtt_client, _mqtt_connected
    if not MQTT_HOST:
        return None
    with _mqtt_lock:
        if _mqtt_client is None:
            client = mqtt.Client(client_id="", protocol=mqtt.MQTTv311)
            client.reconnect_delay_set(min_delay=1, max_delay=60)
            if MQTT_USERNAME:
                client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
            def on_connect(c, userdata, flags, rc):
                global _mqtt_connected
                _mqtt_connected = rc == 0
                if rc == 0:
                    logger.info("已连接到 MQTT 服务器 %s:%s", MQTT_HOST, MQTT_PORT)
                else:
                    logger.warning("MQTT 连接失败，返回码 %d", rc)
            def on_disconnect(c, userdata, rc):
                global _mqtt_connected
                _mqtt_connected = False
                if rc != 0:
                    logger.warning("MQTT 连接断开，rc=%d", rc)
            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            try:
                client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
                client.loop_start()
            except Exception as e:
                logger.warning("MQTT 连接失败：%s", e)
            _mqtt_client = client
        return _mqtt_client


def publish_task_status(task_id: str, status: str, progress: float = 0.0, images_count: int = 0, error: str = None) -> None:
    """向 MQTT 发布任务状态通知，发布失败只记日志不抛异常"""
    if not MQTT_HOST:
        return
    client = get_mqtt_client()
    if client is None:
        return
    payload = json.dumps({
        "task_id": task_id,
        "status": status,
        "progress": progress,
        "images_count": images_count,
        "error": error,
    }, ensure_ascii=False)
    topic = f"{MQTT_TOPIC_PREFIX}/task/{task_id}/status"
    try:
        result = client.publish(topic, payload, qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info("MQTT 已发布 topic=%s status=%s", topic, status)
        else:
            logger.warning("MQTT 发布失败 topic=%s rc=%d", topic, result.rc)
    except Exception as e:
        logger.warning("MQTT 发布异常：%s", e)


def _get_task_lock(task_dir: Path) -> threading.Lock:
    with _task_info_locks_lock:
        key = str(task_dir.resolve())
        if key not in _task_info_locks:
            _task_info_locks[key] = threading.Lock()
        return _task_info_locks[key]


def get_node_client() -> Node:
    global node_client
    with _node_client_lock:
        if node_client is None:
            node_client = Node(NODEODM_HOST, NODEODM_PORT, token=NODEODM_TOKEN)
        return node_client


def reset_node_client() -> None:
    global node_client
    with _node_client_lock:
        node_client = None


def raise_nodeodm_error(message: str, error: Exception) -> None:
    reset_node_client()
    logger.error("NodeODM 错误: %s", error)
    raise HTTPException(status_code=503, detail=message)


def cleanup_task_dir(task_dir: Path) -> None:
    """递归删除任务本地目录，目录不存在时静默跳过"""
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
    with _get_task_lock(task_dir):
        with open(task_info_path, "w", encoding="utf-8") as f:
            json.dump(task_info, f, ensure_ascii=False, indent=2)


def update_task_info_status(task_dir: Path, status: str, error: str = None) -> None:
    task_info_path = task_dir / "task_info.json"
    with _get_task_lock(task_dir):
        task_info = _read_task_info_inner(task_info_path)
        task_info["status"] = status
        if error:
            task_info["error"] = error
        elif status != "failed":
            task_info.pop("error", None)
        with open(task_info_path, "w", encoding="utf-8") as f:
            json.dump(task_info, f, ensure_ascii=False, indent=2)


def update_process_and_tile_info(task_dir: Path, tile_config: dict) -> None:
    task_info_path = task_dir / "task_info.json"
    with _get_task_lock(task_dir):
        task_info = _read_task_info_inner(task_info_path)
        task_info["workflow"] = "process_and_tile"
        task_info["tile_config"] = tile_config
        task_info["tile_status"] = "waiting_for_orthophoto"
        with open(task_info_path, "w", encoding="utf-8") as f:
            json.dump(task_info, f, ensure_ascii=False, indent=2)


def update_process_and_tile_started(task_dir: Path, tile_task_id: str, orthophoto_path: str) -> None:
    task_info_path = task_dir / "task_info.json"
    with _get_task_lock(task_dir):
        task_info = _read_task_info_inner(task_info_path)
        task_info["tile_task_id"] = tile_task_id
        task_info["orthophoto_path"] = orthophoto_path
        task_info["tile_status"] = "processing"
        task_info.pop("tile_error", None)
        with open(task_info_path, "w", encoding="utf-8") as f:
            json.dump(task_info, f, ensure_ascii=False, indent=2)


def update_process_and_tile_failed(task_dir: Path, error: str) -> None:
    task_info_path = task_dir / "task_info.json"
    with _get_task_lock(task_dir):
        task_info = _read_task_info_inner(task_info_path)
        task_info["tile_status"] = "failed"
        task_info["tile_error"] = error
        with open(task_info_path, "w", encoding="utf-8") as f:
            json.dump(task_info, f, ensure_ascii=False, indent=2)


def read_task_info(task_dir: Path) -> dict:
    task_info_path = task_dir / "task_info.json"
    with _get_task_lock(task_dir):
        return _read_task_info_inner(task_info_path)


def _read_task_info_inner(task_info_path: Path) -> dict:
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
    """从上传文件名中提取安全的文件名（去掉路径前缀防止目录穿越）"""
    safe_name = Path(filename or "").name
    if not safe_name:
        raise HTTPException(status_code=400, detail="上传文件名无效")
    return safe_name


def parse_odm_options(options: str) -> dict:
    """解析客户端传入的 ODM 选项 JSON 字符串为字典"""
    try:
        parsed_options = json.loads(options)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="options 必须是合法的 JSON 字符串")
    if not isinstance(parsed_options, dict):
        raise HTTPException(status_code=400, detail="options 必须是 JSON 对象")
    return parsed_options


def _try_remove_remote_task(task_uuid: str) -> None:
    """尝试从 NodeODM 删除任务，运行中的任务不删，失败时静默跳过"""
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
    """扫描临时目录，清理超过 TTL 的已完成/失败/损坏的任务目录"""
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


def _parse_task_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _task_file_timestamp(task_file: Path, task_info: dict) -> datetime:
    timestamp = _parse_task_timestamp(task_info.get("finished_at"))
    if timestamp is None:
        timestamp = _parse_task_timestamp(task_info.get("updated_at"))
    if timestamp is None:
        timestamp = _parse_task_timestamp(task_info.get("created_at"))
    if timestamp is not None:
        return timestamp
    return datetime.fromtimestamp(task_file.stat().st_mtime, timezone.utc)


def _safe_remove_child(parent: Path, child_name: str) -> None:
    if not child_name:
        return
    parent = parent.resolve()
    target = (parent / child_name).resolve()
    if target == parent or parent not in target.parents:
        logger.warning("跳过不安全的清理路径: %s", target)
        return
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def _tile_related_task_ids(task_id: str, task_info: dict) -> set[str]:
    task_ids = {task_id}
    if task_info.get("task_type") != "tiles_pair":
        return task_ids
    children = task_info.get("children")
    if isinstance(children, dict):
        for child in children.values():
            if isinstance(child, dict) and child.get("task_id"):
                task_ids.add(str(child["task_id"]))
    task_ids.add(f"{task_id}-a")
    task_ids.add(f"{task_id}-b")
    return task_ids


def _remove_tile_task_files(upload_dir: Path, output_dir: Path, tasks_dir: Path, task_ids: set[str]) -> None:
    for task_id in task_ids:
        _safe_remove_child(upload_dir, task_id)
        _safe_remove_child(output_dir, task_id)
        _safe_remove_child(tasks_dir, f"{task_id}.json")
    try:
        from . import tile_tasks
        with tile_tasks.task_lock:
            for task_id in task_ids:
                tile_tasks.task_store.pop(task_id, None)
    except Exception:
        logger.debug("清理 tile 内存任务缓存失败", exc_info=True)


def _cleanup_orphan_tile_dirs(base_dir: Path, active_task_ids: set[str], max_age_hours: float, now: datetime) -> None:
    if not base_dir.exists():
        return
    for item in base_dir.iterdir():
        if item.name in active_task_ids:
            continue
        if not item.is_dir():
            continue
        age_hours = (now - datetime.fromtimestamp(item.stat().st_mtime, timezone.utc)).total_seconds() / 3600
        if age_hours > max_age_hours:
            cleanup_task_dir(item)


def _cleanup_expired_tile_tasks(upload_dir: Path, output_dir: Path, tasks_dir: Path, max_age_hours: float) -> None:
    """清理超过 TTL 的终态切割任务产物，保留运行中任务和项目索引。"""
    if not tasks_dir.exists():
        return
    now = datetime.now(timezone.utc)
    active_task_ids: set[str] = set()
    for task_file in list(tasks_dir.glob("*.json")):
        if not task_file.exists():
            continue
        task_id = task_file.stem
        try:
            task_info = json.loads(task_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            age_hours = (now - datetime.fromtimestamp(task_file.stat().st_mtime, timezone.utc)).total_seconds() / 3600
            if age_hours > max_age_hours:
                task_file.unlink(missing_ok=True)
            continue
        if task_info.get("status") not in _TILE_TERMINAL_STATUSES:
            active_task_ids.update(_tile_related_task_ids(task_id, task_info))
            continue
        task_time = _task_file_timestamp(task_file, task_info)
        age_hours = (now - task_time).total_seconds() / 3600
        if age_hours <= max_age_hours:
            active_task_ids.update(_tile_related_task_ids(task_id, task_info))
            continue
        _remove_tile_task_files(upload_dir, output_dir, tasks_dir, _tile_related_task_ids(task_id, task_info))

    _cleanup_orphan_tile_dirs(upload_dir, active_task_ids, max_age_hours, now)
    _cleanup_orphan_tile_dirs(output_dir, active_task_ids, max_age_hours, now)


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            _cleanup_expired_tasks(TEMP_DIR, TASK_TTL_HOURS)
            _cleanup_expired_tile_tasks(TILE_UPLOAD_DIR, TILE_OUTPUT_DIR, TILE_TASKS_DIR, TILE_TASK_TTL_HOURS)
        except Exception as e:
            logger.error("后台清理任务异常: %s", e)
