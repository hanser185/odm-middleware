import os
import tempfile
from pathlib import Path

NODEODM_HOST = os.getenv("NODEODM_HOST", "localhost")
NODEODM_PORT = int(os.getenv("NODEODM_PORT", "3000"))
NODEODM_TOKEN = os.getenv("NODEODM_TOKEN", "")

TEMP_DIR = Path(tempfile.gettempdir()) / "odm_tasks"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

TASK_TTL_HOURS = int(os.getenv("TASK_TTL_HOURS", "24"))
TILE_TASK_TTL_HOURS = int(os.getenv("TILE_TASK_TTL_HOURS", str(TASK_TTL_HOURS)))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "3600"))

TILE_UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "data/uploads"))
TILE_OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "data/outputs"))
TILE_TASKS_DIR = Path(os.getenv("TASKS_DIR", "data/tasks"))
for directory in (TILE_UPLOAD_DIR, TILE_OUTPUT_DIR, TILE_TASKS_DIR):
    directory.mkdir(parents=True, exist_ok=True)

MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "odm")

WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "http://odm-middleware:8000").rstrip("/")

MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "500"))
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024

DEFAULT_ODM_OPTIONS = {
    "end-with": "odm_orthophoto",
    "matcher-type": "flann",
    "sfm-algorithm": "incremental",
    "sfm-no-partial": True,
    "use-exif": True,
    "max-concurrency": 6,
    "pc-quality": "low",
    "pc-filter": 3,
    "skip-3dmodel": True,
    "skip-report": True,
    "dsm": True,
    "dem-resolution": 10,
    "dem-decimation": 2,
    "orthophoto-resolution": 5,
    "orthophoto-compression": "DEFLATE",
    "orthophoto-png": True,
    "fast-orthophoto": True,
    "crop": 2,
    "optimize-disk-space": True,
    "tiles": True,
}
