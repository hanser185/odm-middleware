# ODM Middleware — 正射影像微服务

FastAPI 中间件，封装 NodeODM 提供无人机照片到正射影像的 HTTP API。Docker Compose 部署。

## Commands

```bash
# 本地运行
python -m app.main
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 测试
pytest tests/

# Docker
docker build -t odm-middleware .
docker build --target test .        # 仅跑测试（不构建最终镜像）
docker-compose up -d
docker-compose logs -f odm-middleware
```

## Architecture

| Module | Role |
|--------|------|
| `app/main.py` | FastAPI app + lifespan (NodeODM/MQTT init, cleanup loop) + CORS + RequestID middleware |
| `app/config.py` | Env-driven config (NodeODM host/port/token, MQTT host/port/user/pass/topic, webhook base URL, TTL, size limits, default ODM options) |
| `app/models.py` | Pydantic response models: `TaskResponse`, `TaskStatus`, `TaskSummary` |
| `app/routes.py` | `APIRouter` — 10 endpoints: process, webhook, status, download tif, download zip, delete, node info, node options, task list, health |
| `app/services.py` | NodeODM client singleton (with reset on error), MQTT client, task info R/W with per-dir locks, legacy UUID compat, remote task removal, cleanup loop, helpers |
| `tests/test_task_info.py` | pytest + TestClient (26 tests): upload validation, cleanup logic (expired/orphan/malformed/skip-running), webhook + MQTT, status query, download, health check, Dockerfile/requirements validation |
| `docs/` | OpenAPI 3.0 spec (`openapi.json`), ODM 选项说明, 中文默认模块文档 |
| `Dockerfile` | Multi-stage (test → runtime), Python 3.12-slim, non‑root user, HEALTHCHECK |
| `docker-compose.yml` | `nodeodm` (opendronemap/nodeodm:gpu) + `odm-middleware`, bridge network, Nvidia GPU reservation, named volume |

**Data flow**: `POST /api/v1/process` → save files to `TEMP_DIR/{uuid}/` → `pyodm.Node.create_task(webhook=…)` → NodeODM processes → calls back `POST /api/v1/webhook/{task_id}` → updates local `task_info.json` → publishes MQTT message (if configured).

## Conventions

- **Logging**: logger name `"odm"`, format includes `request_id` via `ContextVar`, messages in Chinese.
- **Error handling**: `HTTPException` with Chinese `detail` strings. ODM errors → `raise_nodeodm_error()` → `503`.
- **Task persistence**: JSON file `task_dir/task_info.json` per task, thread-safe via per-dir locks.
- **Testing**: `pytest` + `TestClient` + `monkeypatch`. Tests use `tmp_path` fixture. No async test utilities.
- **Naming**: snake_case for Python. Route functions use descriptive names. Private helpers prefixed `_`.
- **Imports**: relative (`from .services import …`).
- **Config**: all via `os.getenv()` with defaults, loaded eagerly at module level in `config.py`.
- **Thread safety**: `Node`/`MQTT` clients use `threading.Lock` singletons. Task info uses per-directory locks to avoid file contention.
- **Legacy compat**: `_read_task_info_inner` supports bare UUID filename format (pre‑JSON migration).
- **CI/CD**: Not configured.

## Notes

(Add quick notes here as needed)
