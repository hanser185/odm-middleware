import os
import tempfile
from pathlib import Path

NODEODM_HOST = os.getenv("NODEODM_HOST", "localhost")
NODEODM_PORT = int(os.getenv("NODEODM_PORT", "3000"))
NODEODM_TOKEN = os.getenv("NODEODM_TOKEN", "")

TEMP_DIR = Path(tempfile.gettempdir()) / "odm_tasks"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

TASK_TTL_HOURS = int(os.getenv("TASK_TTL_HOURS", "24"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "3600"))

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
