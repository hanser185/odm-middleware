import io
import json
import sys
import zipfile
from datetime import datetime, timezone
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


def test_runtime_dockerfile_copies_app_code():
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")
    runtime_stage = content.split("# ===================== 运行时阶段 =====================", 1)[1]

    assert "COPY app" in runtime_stage


def test_requirements_include_httpx():
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    lines = requirements.read_text(encoding="utf-8").splitlines()

    assert any(line.startswith("httpx==") for line in lines)


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
