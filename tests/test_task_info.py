import io
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pyodm.exceptions import OdmError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main
from app import config
from app import services
from app import routes
from app import tile_tasks
from app import gdal_utils


def test_runtime_dockerfile_copies_app_code():
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")
    runtime_stage = content.split("# ===================== 运行时阶段 =====================", 1)[1]

    assert "COPY app" in runtime_stage
    assert "gdal-bin" in content


def test_requirements_include_httpx():
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    lines = requirements.read_text(encoding="utf-8").splitlines()

    assert any(line.startswith("httpx==") for line in lines)
    assert any(line.startswith("oss2==") for line in lines)


def test_process_images_cleans_task_dir_when_nodeodm_create_fails(tmp_path, monkeypatch):
    class FailingNodeClient:
        def create_task(self, **kwargs):
            raise OdmError("node unavailable")

    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: FailingNodeClient())

    client = TestClient(main.app)
    response = client.post(
        "/api/v1/process",
        files={"files": ("image.jpg", b"fake image bytes", "image/jpeg")},
        data={"options": "{}"},
    )

    assert response.status_code == 503
    assert list(tmp_path.iterdir()) == []


def test_process_images_returns_400_for_invalid_options_json(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)

    client = TestClient(main.app)
    response = client.post(
        "/api/v1/process",
        files={"files": ("image.jpg", b"fake image bytes", "image/jpeg")},
        data={"options": "{invalid-json"},
    )

    assert response.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_process_images_sanitizes_uploaded_filename(tmp_path, monkeypatch):
    class SuccessfulTask:
        uuid = "node-task-uuid"

    class RecordingNodeClient:
        saved_files = []

        def create_task(self, **kwargs):
            self.saved_files = kwargs["files"]
            return SuccessfulTask()

    node_client = RecordingNodeClient()
    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: node_client)

    client = TestClient(main.app)
    response = client.post(
        "/api/v1/process",
        files={"files": ("../unsafe/image.jpg", b"fake image bytes", "image/jpeg")},
        data={"options": "{}"},
    )

    assert response.status_code == 200
    assert Path(node_client.saved_files[0]).name == "image.jpg"


def test_cleanup_expired_removes_old_dir(tmp_path):
    old_dir = tmp_path / "old-task"
    old_dir.mkdir()
    old_info = {
        "node_task_uuid": "old-uuid",
        "created_at": "2025-01-01T00:00:00+00:00",
        "status": "completed",
    }
    (old_dir / "task_info.json").write_text(json.dumps(old_info), encoding="utf-8")

    services._cleanup_expired_tasks(tmp_path, max_age_hours=24)

    assert not old_dir.exists()


def test_cleanup_expired_keeps_recent_dir(tmp_path):
    recent_dir = tmp_path / "recent-task"
    recent_dir.mkdir()
    now = datetime.now(timezone.utc).isoformat()
    recent_info = {
        "node_task_uuid": "recent-uuid",
        "created_at": now,
        "status": "completed",
    }
    (recent_dir / "task_info.json").write_text(json.dumps(recent_info), encoding="utf-8")

    services._cleanup_expired_tasks(tmp_path, max_age_hours=24)

    assert recent_dir.exists()


def test_cleanup_expired_removes_orphan_dir(tmp_path):
    orphan_dir = tmp_path / "orphan-task"
    orphan_dir.mkdir()

    services._cleanup_expired_tasks(tmp_path, max_age_hours=24)

    assert not orphan_dir.exists()


def test_cleanup_expired_removes_malformed_json(tmp_path):
    malformed_dir = tmp_path / "malformed-task"
    malformed_dir.mkdir()
    (malformed_dir / "task_info.json").write_text("{{broken", encoding="utf-8")

    services._cleanup_expired_tasks(tmp_path, max_age_hours=24)

    assert not malformed_dir.exists()


def test_read_task_info_supports_legacy_uuid_file(tmp_path):
    task_dir = tmp_path / "legacy-task"
    task_dir.mkdir()
    (task_dir / "task_info.json").write_text("node-task-uuid", encoding="utf-8")

    task_info = services.read_task_info(task_dir)

    assert task_info["node_task_uuid"] == "node-task-uuid"


def test_read_task_info_supports_json_file(tmp_path):
    task_dir = tmp_path / "json-task"
    task_dir.mkdir()
    expected = {
        "node_task_uuid": "node-task-uuid",
        "created_at": "2026-05-25T00:00:00+00:00",
        "name": "Task-abcd1234",
        "files_count": 3,
        "status": "queued",
    }
    (task_dir / "task_info.json").write_text(json.dumps(expected), encoding="utf-8")

    task_info = services.read_task_info(task_dir)

    assert task_info == expected


def test_read_task_info_missing_file_returns_404(tmp_path):
    task_dir = tmp_path / "missing-info"
    task_dir.mkdir()

    with pytest.raises(HTTPException) as exc_info:
        services.read_task_info(task_dir)

    assert exc_info.value.status_code == 404


def test_cleanup_task_dir_is_idempotent(tmp_path):
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()
    (task_dir / "file.txt").write_text("temporary", encoding="utf-8")

    services.cleanup_task_dir(task_dir)
    services.cleanup_task_dir(task_dir)

    assert not task_dir.exists()


def test_cleanup_expired_skips_running_task(tmp_path):
    running_dir = tmp_path / "running-task"
    running_dir.mkdir()
    running_info = {
        "node_task_uuid": "running-uuid",
        "created_at": "2025-01-01T00:00:00+00:00",
        "status": "running",
    }
    (running_dir / "task_info.json").write_text(json.dumps(running_info), encoding="utf-8")

    services._cleanup_expired_tasks(tmp_path, max_age_hours=24)

    assert running_dir.exists()


def test_cleanup_expired_tile_tasks_removes_terminal_task_files(tmp_path, monkeypatch):
    uploads_dir = tmp_path / "uploads"
    outputs_dir = tmp_path / "outputs"
    tasks_dir = tmp_path / "tasks"
    for directory in (uploads_dir, outputs_dir, tasks_dir):
        directory.mkdir()
    task_id = "tile-task-1"
    created_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    (uploads_dir / task_id).mkdir()
    (uploads_dir / task_id / "orthophoto.tif").write_text("upload", encoding="utf-8")
    (outputs_dir / task_id).mkdir()
    (outputs_dir / task_id / "tiles.zip").write_text("zip", encoding="utf-8")
    (tasks_dir / f"{task_id}.json").write_text(
        json.dumps({"status": "completed", "created_at": created_at}),
        encoding="utf-8",
    )

    services._cleanup_expired_tile_tasks(uploads_dir, outputs_dir, tasks_dir, max_age_hours=24)

    assert not (uploads_dir / task_id).exists()
    assert not (outputs_dir / task_id).exists()
    assert not (tasks_dir / f"{task_id}.json").exists()


def test_cleanup_expired_tile_tasks_keeps_running_task_files(tmp_path):
    uploads_dir = tmp_path / "uploads"
    outputs_dir = tmp_path / "outputs"
    tasks_dir = tmp_path / "tasks"
    for directory in (uploads_dir, outputs_dir, tasks_dir):
        directory.mkdir()
    task_id = "tile-task-running"
    created_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    (uploads_dir / task_id).mkdir()
    (outputs_dir / task_id).mkdir()
    (tasks_dir / f"{task_id}.json").write_text(
        json.dumps({"status": "processing", "created_at": created_at}),
        encoding="utf-8",
    )

    services._cleanup_expired_tile_tasks(uploads_dir, outputs_dir, tasks_dir, max_age_hours=24)

    assert (uploads_dir / task_id).exists()
    assert (outputs_dir / task_id).exists()
    assert (tasks_dir / f"{task_id}.json").exists()


def test_update_task_info_status_updates_file(tmp_path):
    task_dir = tmp_path / "status-task"
    task_dir.mkdir()
    initial = {
        "node_task_uuid": "status-uuid",
        "created_at": "2026-05-25T00:00:00+00:00",
        "name": "test-task",
        "files_count": 1,
        "status": "queued",
    }
    (task_dir / "task_info.json").write_text(json.dumps(initial), encoding="utf-8")

    services.update_task_info_status(task_dir, "running")

    updated = json.loads((task_dir / "task_info.json").read_text(encoding="utf-8"))
    assert updated["status"] == "running"
    assert updated["node_task_uuid"] == "status-uuid"


def test_health_returns_detailed_info_when_connected(monkeypatch):
    class FakeInfo:
        version = "2.2.3"
        engine = "odm"
        engine_version = "5.0.0"
        cpu_cores = 8
        total_memory = 16 * 1024**3
        available_memory = 8 * 1024**3
        task_queue_count = 2
        max_parallel_tasks = 1

    mock_node = FakeInfo()
    mock_node.info = lambda: mock_node
    monkeypatch.setattr(routes, "get_node_client", lambda: mock_node)

    with TestClient(main.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["nodeodm_connected"] is True
    assert data["nodeodm_version"] == "2.2.3"
    assert data["nodeodm_engine_version"] == "5.0.0"
    assert data["nodeodm_cpu_cores"] == 8
    assert data["nodeodm_task_queue_count"] == 2
    assert "version" in data


def test_health_returns_unhealthy_when_nodeodm_down(monkeypatch):
    class FakeNode:
        def info(self):
            raise Exception("connection refused")

    monkeypatch.setattr(routes, "get_node_client", lambda: FakeNode())

    with TestClient(main.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["nodeodm_connected"] is False
    assert "version" in data
    assert "error" in data


def test_raise_nodeodm_error_resets_cached_client():
    services.node_client = object()

    with pytest.raises(HTTPException) as exc_info:
        services.raise_nodeodm_error("NodeODM 服务错误", OdmError("node unavailable"))

    assert exc_info.value.status_code == 503
    assert services.node_client is None


def test_webhook_updates_local_cache_and_publishes_mqtt(tmp_path, monkeypatch):
    class FakeNodeClient:
        def get_task(self, uuid):
            class FakeInfo:
                status = type("Status", (), {"name": "COMPLETED"})()
                progress = 100.0
                images_count = 3
                processing_time = 60000
                last_error = ""
            class FakeTask:
                def info(self):
                    return FakeInfo()
            return FakeTask()

    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: FakeNodeClient())
    published = []
    monkeypatch.setattr(routes, "publish_task_status", lambda **kw: published.append(kw))

    task_dir = tmp_path / "00000000-0000-0000-0000-000000000001"
    task_dir.mkdir()
    services.write_task_info(task_dir, "node-task-uuid", "test", 3)

    client = TestClient(main.app)
    response = client.post("/api/v1/webhook/00000000-0000-0000-0000-000000000001")

    assert response.status_code == 200
    assert response.json() == {"received": True}

    task_info = json.loads((task_dir / "task_info.json").read_text(encoding="utf-8"))
    assert task_info["status"] == "completed"

    assert len(published) == 1
    assert published[0]["task_id"] == "00000000-0000-0000-0000-000000000001"
    assert published[0]["status"] == "completed"


def test_webhook_does_not_crash_on_missing_task(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    client = TestClient(main.app)
    response = client.post("/api/v1/webhook/00000000-0000-0000-0000-000000000002")
    assert response.status_code == 200
    assert response.json() == {"received": True}


def test_status_returns_local_cache_for_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)

    task_dir = tmp_path / "00000000-0000-0000-0000-000000000003"
    task_dir.mkdir()
    services.write_task_info(task_dir, "node-uuid", "test", 5)
    services.update_task_info_status(task_dir, "completed")
    (task_dir / "task_info.json").write_text(
        json.dumps({
            "node_task_uuid": "node-uuid",
            "status": "completed",
            "name": "test",
            "files_count": 5,
            "created_at": "2026-05-26T00:00:00+00:00",
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    node_queried = []
    original_get_node = routes.get_node_client
    monkeypatch.setattr(routes, "get_node_client", lambda: (node_queried.append(True), None)[1])

    client = TestClient(main.app)
    response = client.get("/api/v1/status/00000000-0000-0000-0000-000000000003")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["progress"] == 100.0
    assert len(node_queried) == 0


def test_status_queries_nodeodm_for_running(tmp_path, monkeypatch):
    class FakeNodeClient:
        def get_task(self, uuid):
            class FakeInfo:
                status = type("Status", (), {"name": "RUNNING"})()
                progress = 45.5
                images_count = 3
                processing_time = 120000
                last_error = ""
            class FakeTask:
                def info(self):
                    return FakeInfo()
            return FakeTask()

    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: FakeNodeClient())

    task_dir = tmp_path / "00000000-0000-0000-0000-000000000004"
    task_dir.mkdir()
    services.write_task_info(task_dir, "node-uuid", "test", 3)

    client = TestClient(main.app)
    response = client.get("/api/v1/status/00000000-0000-0000-0000-000000000004")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["progress"] == 45.5
    assert data["images_count"] == 3


def test_status_transient_nodeodm_error_does_not_mark_failed(tmp_path, monkeypatch):
    class FailingNodeClient:
        def get_task(self, uuid):
            raise OdmError("node temporarily unavailable")

    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: FailingNodeClient())

    task_id = "00000000-0000-0000-0000-000000000005"
    task_dir = tmp_path / task_id
    task_dir.mkdir()
    services.write_task_info(task_dir, "node-uuid", "test", 3)

    client = TestClient(main.app)
    response = client.get(f"/api/v1/status/{task_id}")

    assert response.status_code == 503
    task_info = json.loads((task_dir / "task_info.json").read_text(encoding="utf-8"))
    assert task_info["status"] == "queued"


def test_webhook_persists_failed_error(tmp_path, monkeypatch):
    class FakeNodeClient:
        def get_task(self, uuid):
            class FakeInfo:
                status = type("Status", (), {"name": "FAILED"})()
                progress = 35.0
                images_count = 3
                processing_time = 60000
                last_error = "camera calibration failed"

            class FakeTask:
                def info(self):
                    return FakeInfo()

            return FakeTask()

    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: FakeNodeClient())
    monkeypatch.setattr(routes, "publish_task_status", lambda **kw: None)

    task_id = "00000000-0000-0000-0000-000000000006"
    task_dir = tmp_path / task_id
    task_dir.mkdir()
    services.write_task_info(task_dir, "node-task-uuid", "test", 3)

    client = TestClient(main.app)
    response = client.post(f"/api/v1/webhook/{task_id}")

    assert response.status_code == 200
    task_info = json.loads((task_dir / "task_info.json").read_text(encoding="utf-8"))
    assert task_info["status"] == "failed"
    assert task_info["error"] == "camera calibration failed"


def test_download_orthophoto_downloads_all_zip_and_extracts_tif(tmp_path, monkeypatch):
    class FakeNodeClient:
        def get_task(self, uuid):
            class FakeInfo:
                status = type("Status", (), {"name": "COMPLETED"})()

            class FakeTask:
                def info(self):
                    return FakeInfo()

            return FakeTask()

    # 构造一个包含正射图的 zip 字节流
    import io
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("odm_orthophoto/odm_orthophoto.tif", b"fake-tif-content")
        zf.writestr("odm_dem/odm_dem.tif", b"fake-dem-content")
    zip_bytes = zip_buffer.getvalue()

    class FakeResponse:
        status_code = 200
        content = zip_bytes

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self._url = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            called["url"] = url
            return FakeResponse()

    called = {}
    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "NODEODM_TOKEN", "secret-token", raising=False)
    monkeypatch.setattr(routes, "get_node_client", lambda: FakeNodeClient())
    monkeypatch.setattr(routes.httpx, "AsyncClient", FakeAsyncClient)

    task_id = "00000000-0000-0000-0000-000000000007"
    task_dir = tmp_path / task_id
    task_dir.mkdir()
    services.write_task_info(task_dir, "node-uuid", "test", 3)

    client = TestClient(main.app)
    response = client.get(f"/api/v1/download/{task_id}")

    assert response.status_code == 200
    assert response.content == b"fake-tif-content"
    assert response.headers["content-type"] == "image/tiff"
    parsed_url = urlparse(called["url"])
    assert parsed_url.path.endswith("/task/node-uuid/download/all.zip")
    assert parse_qs(parsed_url.query)["token"] == ["secret-token"]
    # 验证缓存文件已写入本地
    assert (task_dir / "orthophoto.tif").read_bytes() == b"fake-tif-content"


def test_process_images_passes_webhook_to_nodeodm(tmp_path, monkeypatch):
    class SuccessfulTask:
        uuid = "node-task-uuid"
    call_kwargs = {}
    class RecordingNodeClient:
        def create_task(self, **kwargs):
            call_kwargs.update(kwargs)
            return SuccessfulTask()

    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: RecordingNodeClient())
    monkeypatch.setattr(routes, "WEBHOOK_BASE_URL", "http://test-server:8000")

    client = TestClient(main.app)
    response = client.post(
        "/api/v1/process",
        files={"files": ("image.jpg", b"fake", "image/jpeg")},
        data={"options": "{}"},
    )

    assert response.status_code == 200
    assert "webhook" in call_kwargs
    assert call_kwargs["webhook"] == "http://test-server:8000/api/v1/webhook/" + response.json()["task_id"]


def test_process_and_tile_creates_odm_task_with_tile_metadata(tmp_path, monkeypatch):
    class SuccessfulTask:
        uuid = "node-task-uuid"

    class RecordingNodeClient:
        def create_task(self, **kwargs):
            created.update(kwargs)
            return SuccessfulTask()

    created = {}
    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: RecordingNodeClient())

    client = TestClient(main.app)
    response = client.post(
        "/api/v1/process-and-tile",
        files={"files": ("image.jpg", b"fake image bytes", "image/jpeg")},
        data={
            "options": "{}",
            "tile_size": "512",
            "skip_empty_tiles": "true",
            "export_png": "false",
        },
    )

    assert response.status_code == 200
    data = response.json()
    task_dir = tmp_path / data["task_id"]
    task_info = json.loads((task_dir / "task_info.json").read_text(encoding="utf-8"))
    assert data["status"] == "queued"
    assert data["workflow"] == "process_and_tile"
    assert task_info["workflow"] == "process_and_tile"
    assert task_info["tile_status"] == "waiting_for_orthophoto"
    assert task_info["tile_config"]["tile_size"] == 512
    assert task_info["tile_config"]["export_png"] is False
    assert created["webhook"].endswith(f"/api/v1/webhook/{data['task_id']}")


def test_webhook_starts_tile_task_after_combined_odm_completion(tmp_path, monkeypatch):
    class FakeNodeClient:
        def get_task(self, uuid):
            class FakeInfo:
                status = type("Status", (), {"name": "COMPLETED"})()
                progress = 100.0
                images_count = 3
                processing_time = 60000
                last_error = ""

            class FakeTask:
                def info(self):
                    return FakeInfo()

            return FakeTask()

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("odm_orthophoto/odm_orthophoto.tif", b"combined-tif")
    zip_bytes = zip_buffer.getvalue()

    class FakeResponse:
        status_code = 200
        content = zip_bytes

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            requested_urls.append(url)
            return FakeResponse()

    requested_urls = []
    started_tiles = []
    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: FakeNodeClient())
    monkeypatch.setattr(routes, "publish_task_status", lambda **kw: None)
    monkeypatch.setattr(routes.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        routes,
        "create_tiles_task",
        lambda orthophoto_path, output_dir, tile_config, task_id=None: started_tiles.append(
            {
                "orthophoto_path": orthophoto_path,
                "output_dir": output_dir,
                "tile_config": tile_config,
                "task_id": task_id,
            }
        ) or "tile-task-id",
    )

    task_id = "00000000-0000-0000-0000-000000000008"
    task_dir = tmp_path / task_id
    task_dir.mkdir()
    services.write_task_info(task_dir, "node-task-uuid", "test", 3)
    services.update_process_and_tile_info(
        task_dir,
        {
            "tile_size": 512,
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
        },
    )

    client = TestClient(main.app)
    response = client.post(f"/api/v1/webhook/{task_id}")

    assert response.status_code == 200
    assert requested_urls[0].endswith("/task/node-task-uuid/download/all.zip")
    assert len(started_tiles) == 1
    assert Path(started_tiles[0]["orthophoto_path"]).read_bytes() == b"combined-tif"
    task_info = json.loads((task_dir / "task_info.json").read_text(encoding="utf-8"))
    assert task_info["tile_task_id"] == "tile-task-id"
    assert task_info["tile_status"] == "processing"
    assert task_info["orthophoto_path"].endswith("orthophoto.tif")


def test_process_and_tile_status_includes_tile_task_state(tmp_path, monkeypatch):
    task_id = "00000000-0000-0000-0000-000000000009"
    task_dir = tmp_path / task_id
    task_dir.mkdir()
    services.write_task_info(task_dir, "node-task-uuid", "test", 3)
    services.update_process_and_tile_info(task_dir, {"tile_size": 512})
    services.update_process_and_tile_started(task_dir, "tile-task-id", str(task_dir / "orthophoto.tif"))
    services.update_task_info_status(task_dir, "completed")

    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(
        routes,
        "get_tile_task",
        lambda tile_task_id: {
            "status": "completed",
            "progress": 100,
            "tiles_zip_path": str(task_dir / "tiles.zip"),
            "manifest_path": str(task_dir / "manifest.json"),
        },
    )

    client = TestClient(main.app)
    response = client.get(f"/api/v1/process-and-tile/{task_id}/status")

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == task_id
    assert data["status"] == "completed"
    assert data["odm_status"] == "completed"
    assert data["tile_status"] == "completed"
    assert data["tile_task_id"] == "tile-task-id"
    assert data["tiles_download_url"] == f"/api/v1/process-and-tile/{task_id}/download/tiles"




def test_process_pair_and_tile_creates_two_odm_tasks_with_local_pair_workflow(tmp_path, monkeypatch):
    class SuccessfulTask:
        def __init__(self, uuid):
            self.uuid = uuid

    class RecordingNodeClient:
        def create_task(self, **kwargs):
            created.append(kwargs)
            return SuccessfulTask(f"node-task-{len(created)}")

    created = []
    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: RecordingNodeClient())
    monkeypatch.setattr(routes, "WEBHOOK_BASE_URL", "http://test-server:8000")

    client = TestClient(main.app)
    response = client.post(
        "/api/v1/process-pair-and-tile",
        files=[
            ("base_files", ("base1.jpg", b"base-one", "image/jpeg")),
            ("base_files", ("base2.jpg", b"base-two", "image/jpeg")),
            ("compare_files", ("compare1.jpg", b"compare-one", "image/jpeg")),
        ],
        data={
            "options": "{}",
            "tile_size": "512",
            "skip_empty_tiles": "true",
            "export_png": "false",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "queued"
    assert data["workflow"] == "process_pair_and_tile"
    assert "tile_config" not in data
    assert len(created) == 2
    assert [Path(file).name for file in created[0]["files"]] == ["base1.jpg", "base2.jpg"]
    assert [Path(file).name for file in created[1]["files"]] == ["compare1.jpg"]
    assert created[0]["webhook"] == f"http://test-server:8000/api/v1/process-pair-and-tile/{data['task_id']}/webhook/base"
    assert created[1]["webhook"] == f"http://test-server:8000/api/v1/process-pair-and-tile/{data['task_id']}/webhook/compare"

    task_info = json.loads((tmp_path / data["task_id"] / "task_info.json").read_text(encoding="utf-8"))
    assert task_info["workflow"] == "process_pair_and_tile"
    assert task_info["base"]["node_task_uuid"] == "node-task-1"
    assert task_info["compare"]["node_task_uuid"] == "node-task-2"
    assert task_info["base"]["status"] == "queued"
    assert task_info["compare"]["status"] == "queued"
    assert task_info["tile_status"] == "waiting_for_orthophotos"
    assert task_info["tile_config"]["tile_size"] == 512
    assert task_info["tile_config"]["export_png"] is False


def test_pair_webhook_starts_local_pair_tiling_after_both_odm_tasks_complete(tmp_path, monkeypatch):
    class FakeNodeClient:
        def get_task(self, uuid):
            class FakeInfo:
                status = type("Status", (), {"name": "COMPLETED"})()
                progress = 100.0
                images_count = 2
                processing_time = 60000
                last_error = ""

            class FakeTask:
                def info(self):
                    return FakeInfo()

            return FakeTask()

    def zip_with_tif(content: bytes):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("odm_orthophoto/odm_orthophoto.tif", content)
        return zip_buffer.getvalue()

    class FakeResponse:
        status_code = 200

        def __init__(self, content):
            self.content = content

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            requested_urls.append(url)
            if "base-node-uuid" in url:
                return FakeResponse(zip_with_tif(b"base-tif"))
            return FakeResponse(zip_with_tif(b"compare-tif"))

    requested_urls = []
    started_pairs = []
    monkeypatch.setattr(routes, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_node_client", lambda: FakeNodeClient())
    monkeypatch.setattr(routes.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        routes,
        "create_tiles_pair_task",
        lambda image_a_path, image_b_path, output_dir, tile_config, task_id=None: started_pairs.append(
            {
                "image_a_path": image_a_path,
                "image_b_path": image_b_path,
                "output_dir": output_dir,
                "tile_config": tile_config,
                "task_id": task_id,
            }
        ) or "pair-tile-task-id",
    )

    task_id = "00000000-0000-0000-0000-000000000010"
    task_dir = tmp_path / task_id
    task_dir.mkdir()
    task_info = {
        "workflow": "process_pair_and_tile",
        "created_at": "2026-07-08T00:00:00+00:00",
        "name": "pair-task",
        "base": {"node_task_uuid": "base-node-uuid", "status": "queued", "files_count": 2},
        "compare": {"node_task_uuid": "compare-node-uuid", "status": "queued", "files_count": 1},
        "tile_status": "waiting_for_orthophotos",
        "tile_config": {
            "tile_size": 512,
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
        },
    }
    (task_dir / "task_info.json").write_text(json.dumps(task_info), encoding="utf-8")

    client = TestClient(main.app)
    first = client.post(f"/api/v1/process-pair-and-tile/{task_id}/webhook/base")
    second = client.post(f"/api/v1/process-pair-and-tile/{task_id}/webhook/compare")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(started_pairs) == 1
    assert Path(started_pairs[0]["image_a_path"]).read_bytes() == b"base-tif"
    assert Path(started_pairs[0]["image_b_path"]).read_bytes() == b"compare-tif"
    assert started_pairs[0]["task_id"] == "00000000-0000-0000-0000-000000000010-pair"
    assert requested_urls[0].endswith("/task/base-node-uuid/download/all.zip")
    assert requested_urls[1].endswith("/task/compare-node-uuid/download/all.zip")

    updated = json.loads((task_dir / "task_info.json").read_text(encoding="utf-8"))
    assert updated["base"]["status"] == "completed"
    assert updated["compare"]["status"] == "completed"
    assert updated["base"]["orthophoto_path"].endswith("orthophotos/base.tif")
    assert updated["compare"]["orthophoto_path"].endswith("orthophotos/compare.tif")
    assert updated["tile_status"] == "processing"
    assert updated["tile_task_id"] == "pair-tile-task-id"


def test_aoi_crop_transforms_cutline_without_cutline_srs(tmp_path, monkeypatch):
    input_tif = tmp_path / "source.tif"
    output_tif = tmp_path / "orthophoto.tif"
    input_tif.write_bytes(b"fake")

    source_wkt = 'PROJCS["WGS 84 / UTM zone 51N"]'
    commands = []

    def fake_run_command(cmd, input_text=None):
        commands.append(cmd)

        class Result:
            stdout = ""

        if cmd[0] == "gdalinfo":
            Result.stdout = json.dumps({
                "coordinateSystem": {"wkt": source_wkt},
                "cornerCoordinates": {},
            })
        elif cmd[0] == "gdaltransform":
            assert cmd == ["gdaltransform", "-s_srs", "EPSG:4326", "-t_srs", source_wkt]
            Result.stdout = "\n".join([
                "370440 3313890 0",
                "370800 3313890 0",
                "370800 3314160 0",
                "370440 3314160 0",
                "370440 3313890 0",
            ])
        return Result()

    monkeypatch.setattr(gdal_utils, "run_command", fake_run_command)

    summary = gdal_utils.crop_orthophoto_to_aoi(
        str(input_tif),
        str(output_tif),
        {
            "type": "Polygon",
            "coordinates": [[
                [121.6584, 29.9504],
                [121.6591, 29.9493],
                [121.6605, 29.9499],
                [121.6597, 29.9511],
                [121.6584, 29.9504],
            ]],
        },
        "EPSG:4326",
    )

    gdalwarp_cmd = next(cmd for cmd in commands if cmd[0] == "gdalwarp")
    assert "-cutline_srs" not in gdalwarp_cmd
    assert summary["aoi_crs"] == "EPSG:4326"
    cutline = json.loads(Path(summary["cutline_path"]).read_text(encoding="utf-8"))
    assert cutline["crs"]["properties"]["name"] == source_wkt
    assert cutline["features"][0]["geometry"]["coordinates"][0][0] == [370440.0, 3313890.0]


def test_tile_routes_are_registered():
    paths = set()
    for route in main.app.routes:
        if hasattr(route, "path"):
            paths.add(route.path)
        for nested_route in getattr(getattr(route, "original_router", None), "routes", []):
            if hasattr(nested_route, "path"):
                paths.add(nested_route.path)

    assert "/api/tiles/process" in paths
    assert "/api/tiles/process-pair" in paths
    assert "/api/task/{task_id}/status" in paths
    assert "/api/task/{task_id}/download/tiles" in paths
    assert "/api/task/{task_id}/download/manifest" in paths
    assert "/api/task/{task_id}/download/orthophoto" in paths
