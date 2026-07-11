from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import uuid
import zipfile

logger = logging.getLogger("odm")

from .gdal_utils import build_aligned_aoi_crop_config, crop_orthophoto_to_aoi, split_orthophoto, zip_directory
from .oss_utils import upload_file_to_oss

task_store = {}
task_lock = threading.RLock()
TASKS_DIR = Path(os.getenv("TASKS_DIR", "data/tasks"))
TASKS_DIR.mkdir(parents=True, exist_ok=True)
MAX_TASK_WORKERS = max(1, int(os.getenv("MAX_TASK_WORKERS", "2")))
TASK_TERMINAL_STATUSES = {"completed", "failed"}
task_executor = ThreadPoolExecutor(max_workers=MAX_TASK_WORKERS, thread_name_prefix="tile-task")


def is_terminal_task(task: dict):
    return task.get("status") in TASK_TERMINAL_STATUSES

# 持久化与查询
def task_file_path(task_id: str):
    return TASKS_DIR / f"{task_id}.json"

# 每个任务对应一个JSON文件，路径是data/tasks/{task_id}.json
def persist_task_unlocked(task_id: str):
    task = task_store.get(task_id)
    if task is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    task["updated_at"] = now
    if is_terminal_task(task):
        task.setdefault("finished_at", now)
    tmp_path = task_file_path(task_id).with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(task, ensure_ascii=True, indent=2))
    tmp_path.replace(task_file_path(task_id))
    if is_terminal_task(task):
        task_store.pop(task_id, None)


def persist_task(task_id: str):
    with task_lock:
        persist_task_unlocked(task_id)

#·get_task 查任务时先查内存字典，没有再读文件。从文件读到后还会塞回内存字典，
# 下次就不用再读文件了。这样设计是因为后台线程更新状态时直接改内存字典，
# HTTP 查询线程读内存就能拿到最新值，不用每次读文件。
def get_task(task_id: str):
    with task_lock:
        task = task_store.get(task_id)
        if task is not None:
            return task

        path = task_file_path(task_id)
        if not path.exists():
            return None

        task = json.loads(path.read_text())
        if not is_terminal_task(task):
            task_store[task_id] = task
        return task

# create_task_record 创建新任务：往内存字典放初始状态，同时写文件
def create_task_record(task_id: str, initial: dict):
    with task_lock:
        now = datetime.now(timezone.utc).isoformat()
        initial.setdefault("created_at", now)
        initial.setdefault("updated_at", now)
        task_store[task_id] = initial
        persist_task_unlocked(task_id)
    return task_store[task_id]


def update_task(task_id: str, updates: dict):
    with task_lock:
        task = task_store.get(task_id)
        if task is None:
            return None
        task.update(updates)
        persist_task_unlocked(task_id)
        return task


def oss_result_fields(name: str, result: dict | None):
    if result is None:
        return {}
    return {
        f"{name}_oss_bucket": result["bucket"],
        f"{name}_oss_key": result["key"],
        f"{name}_oss_url": result["url"],
    }

# 扫描tasks目录所有文件，加载到内存字典
def load_existing_tasks():
    with task_lock:
        for path in TASKS_DIR.glob("*.json"):
            try:
                task = json.loads(path.read_text())
                if not is_terminal_task(task):
                    task_store[path.stem] = task
            except Exception:
                continue


load_existing_tasks()

def run_tiles_task(task_id: str, orthophoto_path: str, output_dir: str, tile_config: dict):
    logger.info("[%s] 单图切片任务开始, source=%s", task_id, orthophoto_path)
    try:
        task = get_task(task_id)
        task["status"] = "processing"
        # 把状态改为 processing，把 tile_config、tile_size、源文件路径写入任务记录，持久化。
        task["progress"] = 10
        task["tile_size"] = tile_config["tile_size"]
        task["tile_width_m"] = tile_config["tile_width_m"]
        task["tile_height_m"] = tile_config["tile_height_m"]
        task["tile_config"] = tile_config

        task["orthophoto_source_path"] = orthophoto_path
        persist_task(task_id)
        source_path = Path(orthophoto_path)

        if not source_path.exists():
            raise RuntimeError(f"Orthophoto file not found: {orthophoto_path}")

        # 构建任务专属工作区output_dir输出目录
        out_dir = Path(output_dir) / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        # 定义四种核心产出物的最终路径
        ortho_file = out_dir / "orthophoto.tif"
        source_ortho_file = out_dir / "source_orthophoto.tif"
        tiles_dir = out_dir / "tiles"
        zip_file = out_dir / "tiles.zip"

        # 创建输出目录结构，把上传的源文件复制为source_orthophoto.tif。然后看有没有AOI
        # 有AOI就调crop_orthophoto_to_aoi裁剪出orthophoto.tif，并把裁剪信息记到任务的aoi字段
        # 没有AOI就直接复制一份。两条路最终都保证orthophoto.tif存在
        task["progress"] = 30
        shutil.copyfile(str(source_path), str(source_ortho_file))
        # 将 source_ortho_file的路径（转为字符串）存入任务字典，以便后续记录或追踪源正射影像的位置
        task["source_orthophoto_path"] = str(source_ortho_file)
        aoi_geojson = tile_config.get("aoi_geojson")
        if aoi_geojson is not None:
            # 有aoi走gdalwarp裁剪，对正射图进行裁切
            logger.info("[%s] 开始 AOI 裁剪", task_id)
            crop_summary = crop_orthophoto_to_aoi(
                str(source_ortho_file),
                str(ortho_file), # 最终生成的裁剪后正射影像
                aoi_geojson,
                tile_config.get("aoi_crs"),
                tile_config.get("aoi_target_bounds"),
                tile_config.get("aoi_target_resolution"),
            )
            logger.info("[%s] AOI 裁剪完成", task_id)
            # 存入 task["aoi"]字典
            task["aoi"] = {
                "geojson": crop_summary["aoi_geojson"],
                "crs": crop_summary["aoi_crs"],
                "cutline_path": crop_summary["cutline_path"],
                "source_bounds": crop_summary["source_bounds"],
                "cropped_bounds": crop_summary["cropped_bounds"],
                "target_bounds": crop_summary.get("target_bounds"),
                "target_resolution": crop_summary.get("target_resolution"),
            }
        else:
            # 无aoi，直接拷贝
            shutil.copyfile(str(source_ortho_file), str(ortho_file))
        # 将最终生成的正射影像文件路径ortho_file（转为字符串）赋值给任务字典task的"orthophoto_path"键
        task["orthophoto_path"] = str(ortho_file)
        # 内存中的task字典只在当前进程有效。调用persist_task后，即使服务重启或前端轮询状态，
        # 也能从存储中读取到任务的完成进度和最终文件路径，确保前后端数据一致
        persist_task(task_id)
        # 调split_orthophoto执行核心切割。这个函数在gdal_utils.py里，用gdalwark按地理网格逐片裁剪tiles，
        # 检测空切片，导出PNG，写manifest.json。返回的摘要写入任务记录：生成数量、跳过数量、网格参数、manifest 路径
        task["progress"] = 60
        persist_task(task_id) # 写入持久化
        logger.info("[%s] 开始瓦片切割, tile_size=%s, tile_width_m=%s, tile_height_m=%s",
                    task_id, tile_config["tile_size"], tile_config["tile_width_m"], tile_config["tile_height_m"])
        tile_summary = split_orthophoto(
            str(ortho_file), # 输入的正射影像路径
            str(tiles_dir),  # 输出瓦片目录
            tile_size=tile_config["tile_size"],
            tile_width_m=tile_config["tile_width_m"], # 120m
            tile_height_m=tile_config["tile_height_m"], # 90m
            skip_empty_tiles=tile_config["skip_empty_tiles"],
            export_png=tile_config["export_png"],
            grid_origin_x=tile_config["grid_origin_x"],
            grid_origin_y=tile_config["grid_origin_y"],
            grid_pixel_size_x=tile_config["grid_pixel_size_x"],
            grid_pixel_size_y=tile_config["grid_pixel_size_y"],
        )
        task["generated_tiles"] = tile_summary["generated_tiles"] # 实际生成的瓦片数量
        task["skipped_empty_tiles"] = tile_summary["skipped_empty_tiles"] # 因无内容而被跳过的瓦片数
        task["grid"] = tile_summary["grid"] # 网格划分的元信息（如行列数、范围等）
        task["manifest_path"] = tile_summary["manifest_path"] #生成的文件清单（manifest）路径，记录每个瓦片的文件名、位置等信息
        task["tile_coordinates_path"] = tile_summary["tile_coordinates_path"]
        persist_task(task_id)
        logger.info("[%s] 瓦片切割完成, generated=%d, skipped_empty=%d, grid=%dx%d",
                    task_id, tile_summary["generated_tiles"], tile_summary["skipped_empty_tiles"],
                    tile_summary["grid"]["col_end"] - tile_summary["grid"]["col_start"] + 1,
                    tile_summary["grid"]["row_end"] - tile_summary["grid"]["row_start"] + 1)
        # 开始打包
        task["progress"] = 85
        persist_task(task_id)
        zip_directory(str(tiles_dir), str(zip_file)) # 将文件压缩成zip
        task["tiles_zip_path"] = str(zip_file)
        logger.info("[%s] 开始上传 tiles.zip 到 OSS", task_id)
        oss_result = upload_file_to_oss(str(zip_file), f"tiles/{task_id}/tiles.zip")
        task.update(oss_result_fields("tiles", oss_result))
        task["status"] = "completed"
        task["progress"] = 100
        persist_task(task_id)
        logger.info("[%s] 单图切片任务完成", task_id)
    except Exception as e:
        logger.error("[%s] 单图切片任务失败: %s", task_id, e, exc_info=True)
        task = get_task(task_id)
        if task is not None:
            task["status"] = "failed"
            task["error"] = str(e)
            persist_task(task_id)

# 用于异步创建并启动一个生成瓦片（tiles）的后台任务。它不直接执行切片，而是将任务放入独立线程中运行，
# 并立即返回任务 ID 供调用方跟踪进度
def create_tiles_task(
    orthophoto_path: str, # 输入正射图路径
    output_dir: str,
    tile_config: dict | None = None,
    task_id: str | None = None,
):
    task_id = task_id or str(uuid.uuid4())
    create_task_record(task_id, {"status": "pending", "progress": 0, "task_type": "tiles"})
    tile_config = tile_config or {
        "tile_size": 1024,
        "tile_width_m": 120.0,
        "tile_height_m": 90.0,
        "skip_empty_tiles": True,
        "export_png": True,
        "grid_origin_x": None,
        "grid_origin_y": None,
        "grid_pixel_size_x": None,
        "grid_pixel_size_y": None,
        "aoi_geojson": None,
        "aoi_crs": None,
    }
    task_executor.submit(run_tiles_task, task_id, orthophoto_path, output_dir, tile_config)
    return task_id


def child_result(task_id: str, task: dict):
    return {
        "task_id": task_id,
        "status": task.get("status"),
        "progress": task.get("progress"),
        "generated_tiles": task.get("generated_tiles"),
        "skipped_empty_tiles": task.get("skipped_empty_tiles"),
        "grid": task.get("grid"),
        "tiles_download_url": f"/api/task/{task_id}/download/tiles",
        "tiles_oss_url": task.get("tiles_oss_url"),
        "manifest_download_url": f"/api/task/{task_id}/download/manifest",
        "orthophoto_download_url": f"/api/task/{task_id}/download/orthophoto",
    }


def build_tiles_pair_zip(pair_task_id: str, output_dir: str, image_a_task: dict, image_b_task: dict):
    pair_dir = Path(output_dir) / pair_task_id
    pair_dir.mkdir(parents=True, exist_ok=True)
    pair_zip_path = pair_dir / "tiles_pair.zip"
    pair_summary_path = pair_dir / "pair_summary.json"
    pair_coordinates_path = pair_dir / "pair_tile_coordinates.json"
    summary = {
        "task_id": pair_task_id,
        "task_type": "tiles_pair",
        "image_a": child_result(f"{pair_task_id}-a", image_a_task),
        "image_b": child_result(f"{pair_task_id}-b", image_b_task),
    }
    pair_summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2))
    pair_coordinates = {
        "task_id": pair_task_id,
        "crs": "EPSG:4326",
        "image_a": {"tiles": []},
        "image_b": {"tiles": []},
    }
    for label, child_task in (("image_a", image_a_task), ("image_b", image_b_task)):
        coordinates_path = child_task.get("tile_coordinates_path")
        if coordinates_path and Path(coordinates_path).exists():
            pair_coordinates[label] = json.loads(Path(coordinates_path).read_text())
    pair_coordinates_path.write_text(json.dumps(pair_coordinates, ensure_ascii=True, indent=2))

    with zipfile.ZipFile(pair_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for label, child_task in (("image_a", image_a_task), ("image_b", image_b_task)):
            manifest_path = child_task.get("manifest_path")
            if not manifest_path:
                continue
            tiles_dir = Path(manifest_path).parent
            for file in tiles_dir.rglob("*"):
                if file.is_file() and not file.name.endswith(".aux.xml"):
                    zf.write(file, Path(label) / file.relative_to(tiles_dir))
        zf.write(pair_summary_path, "pair_summary.json")
        zf.write(pair_coordinates_path, "pair_tile_coordinates.json")
    return str(pair_zip_path)


def copy_grid_to_tile_config(tile_config: dict, grid: dict):
    copied = dict(tile_config)
    copied["grid_origin_x"] = grid["origin_x"]
    copied["grid_origin_y"] = grid["origin_y"]
    copied["grid_pixel_size_x"] = grid["pixel_size_x"]
    copied["grid_pixel_size_y"] = grid["pixel_size_y"]
    return copied


def refresh_pair_children(pair_task_id: str, image_a_task_id: str, image_b_task_id: str):
    image_a_task = get_task(image_a_task_id) or {}
    image_b_task = get_task(image_b_task_id) or {}
    update_task(
        pair_task_id,
        {
            "children": {
                "image_a": {
                    "task_id": image_a_task_id,
                    "status": image_a_task.get("status"),
                    "progress": image_a_task.get("progress", 0),
                },
                "image_b": {
                    "task_id": image_b_task_id,
                    "status": image_b_task.get("status"),
                    "progress": image_b_task.get("progress", 0),
                },
            },
            "progress": int(
                ((image_a_task.get("progress", 0) or 0) + (image_b_task.get("progress", 0) or 0)) / 2
            ),
        },
    )


def run_tiles_pair_task(
    pair_task_id: str,
    image_a_path: str,
    image_b_path: str,
    output_dir: str,
    tile_config: dict,
):
    image_a_task_id = f"{pair_task_id}-a"
    image_b_task_id = f"{pair_task_id}-b"
    logger.info("[%s] 成对切片任务开始, image_a=%s, image_b=%s", pair_task_id, image_a_path, image_b_path)
    try:
        update_task(pair_task_id, {"status": "processing", "progress": 1})
        create_task_record(image_a_task_id, {"status": "pending", "progress": 0, "task_type": "tiles"})
        create_task_record(image_b_task_id, {"status": "pending", "progress": 0, "task_type": "tiles"})
        refresh_pair_children(pair_task_id, image_a_task_id, image_b_task_id)

        image_a_config = dict(tile_config)
        if image_a_config.get("aoi_geojson") is not None:
            logger.info("[%s] 计算 AOI 对齐裁剪参数", pair_task_id)
            crop_config = build_aligned_aoi_crop_config(
                image_a_path,
                image_a_config["aoi_geojson"],
                image_a_config.get("aoi_crs"),
                image_a_config["tile_width_m"],
                image_a_config["tile_height_m"],
            )
            image_a_config["aoi_target_bounds"] = crop_config["bounds"]
            image_a_config["aoi_target_resolution"] = crop_config["resolution"]
            logger.info("[%s] AOI 对齐参数: bounds=%s, resolution=%s",
                        pair_task_id, crop_config["bounds"], crop_config["resolution"])

        logger.info("[%s] 开始处理 image_a (base)", pair_task_id)
        run_tiles_task(image_a_task_id, image_a_path, output_dir, image_a_config)
        image_a_task = get_task(image_a_task_id)
        if image_a_task is None or image_a_task.get("status") != "completed":
            raise RuntimeError(image_a_task.get("error") if image_a_task else "First image task failed")
        logger.info("[%s] image_a 处理完成, generated=%d", pair_task_id, image_a_task.get("generated_tiles", 0))
        refresh_pair_children(pair_task_id, image_a_task_id, image_b_task_id)

        image_b_config = copy_grid_to_tile_config(image_a_config, image_a_task["grid"])
        logger.info("[%s] image_b 复用 image_a 网格: origin=(%s, %s), pixel_size=(%s, %s)",
                    pair_task_id, image_b_config["grid_origin_x"], image_b_config["grid_origin_y"],
                    image_b_config["grid_pixel_size_x"], image_b_config["grid_pixel_size_y"])
        logger.info("[%s] 开始处理 image_b (compare)", pair_task_id)
        run_tiles_task(image_b_task_id, image_b_path, output_dir, image_b_config)
        image_b_task = get_task(image_b_task_id)
        if image_b_task is None or image_b_task.get("status") != "completed":
            raise RuntimeError(image_b_task.get("error") if image_b_task else "Second image task failed")
        logger.info("[%s] image_b 处理完成, generated=%d", pair_task_id, image_b_task.get("generated_tiles", 0))

        logger.info("[%s] 开始打包 tiles_pair.zip", pair_task_id)
        pair_zip_path = build_tiles_pair_zip(pair_task_id, output_dir, image_a_task, image_b_task)
        logger.info("[%s] 开始上传 tiles_pair.zip 到 OSS", pair_task_id)
        oss_result = upload_file_to_oss(pair_zip_path, f"tiles_pair/{pair_task_id}/tiles_pair.zip")
        oss_fields = oss_result_fields("tiles_pair", oss_result)
        results = {
            "tiles_pair_download_url": f"/api/task/{pair_task_id}/download/tiles-pair",
            "image_a": child_result(image_a_task_id, image_a_task),
            "image_b": child_result(image_b_task_id, image_b_task),
        }
        if oss_result is not None:
            results["tiles_pair_oss_url"] = oss_result["url"]
        update_task(
            pair_task_id,
            {
                "status": "completed",
                "progress": 100,
                "tiles_pair_zip_path": pair_zip_path,
                **oss_fields,
                "children": {
                    "image_a": {
                        "task_id": image_a_task_id,
                        "status": image_a_task.get("status"),
                        "progress": image_a_task.get("progress"),
                    },
                    "image_b": {
                        "task_id": image_b_task_id,
                        "status": image_b_task.get("status"),
                        "progress": image_b_task.get("progress"),
                    },
                },
                "results": results,
            },
        )
        logger.info("[%s] 成对切片任务完成, a_tiles=%d, b_tiles=%d",
                    pair_task_id,
                    image_a_task.get("generated_tiles", 0),
                    image_b_task.get("generated_tiles", 0))
    except Exception as e:
        logger.error("[%s] 成对切片任务失败: %s", pair_task_id, e, exc_info=True)
        refresh_pair_children(pair_task_id, image_a_task_id, image_b_task_id)
        update_task(pair_task_id, {"status": "failed", "error": str(e)})


def create_tiles_pair_task(
    image_a_path: str,
    image_b_path: str,
    output_dir: str,
    tile_config: dict,
    task_id: str | None = None,
):
    task_id = task_id or str(uuid.uuid4())
    logger.info("[%s] 创建成对切片任务, tile_size=%s", task_id, tile_config.get("tile_size"))
    create_task_record(
        task_id,
        {
            "status": "pending",
            "progress": 0,
            "task_type": "tiles_pair",
            "tile_size": tile_config["tile_size"],
            "tile_config": tile_config,
            "children": {
                "image_a": {"task_id": f"{task_id}-a", "status": "pending", "progress": 0},
                "image_b": {"task_id": f"{task_id}-b", "status": "pending", "progress": 0},
            },
        },
    )
    task_executor.submit(run_tiles_pair_task, task_id, image_a_path, image_b_path, output_dir, tile_config)
    return task_id
