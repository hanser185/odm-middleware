import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pyodm.exceptions import OdmError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main
from app import config
from app import services
from app import routes


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
    recent_info = {
        "node_task_uuid": "recent-uuid",
        "created_at": "2026-05-25T00:00:00+00:00",
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
