# Process And Tile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a combined drone-photo-to-orthophoto-to-tiles workflow while preserving the existing orthophoto-only and tile-only APIs.

**Architecture:** Import the existing tile service as separate modules and include its router in the current FastAPI app. Add a combined route that creates a normal NodeODM task with extra workflow metadata, then let the existing webhook extract the completed GeoTIFF and start a tile task.

**Tech Stack:** FastAPI, pyodm, httpx, GDAL command line tools, paho-mqtt, oss2, pytest/TestClient.

---

### Task 1: Add Combined Workflow Tests

**Files:**
- Modify: `tests/test_task_info.py`

- [ ] **Step 1: Write failing tests**

Add tests for `POST /api/v1/process-and-tile`, webhook tile startup, and combined status response.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_task_info.py -v`
Expected: fail because routes and helper functions do not exist yet.

### Task 2: Import Tile Modules

**Files:**
- Create: `app/tile_routes.py`
- Create: `app/tile_tasks.py`
- Create: `app/gdal_utils.py`
- Create: `app/oss_utils.py`
- Modify: `app/main.py`

- [ ] **Step 1: Copy tile service modules**

Copy `ortho_tile_api` logic into namespaced modules and update imports from `.tasks` to `.tile_tasks`.

- [ ] **Step 2: Include tile router**

Import `tile_router` in `app/main.py` and call `app.include_router(tile_router)`.

- [ ] **Step 3: Run tile route registration test**

Run: `python -m pytest tests/test_task_info.py::test_tile_routes_are_registered -v`
Expected: pass once routes are included.

### Task 3: Add Combined API

**Files:**
- Modify: `app/routes.py`
- Modify: `app/services.py`

- [ ] **Step 1: Persist workflow metadata**

Add service helpers that mark a task as `process_and_tile` and save tile config/status.

- [ ] **Step 2: Create combined process endpoint**

Add `POST /api/v1/process-and-tile` using the existing upload and NodeODM creation flow plus tile config parsing.

- [ ] **Step 3: Start tiling from webhook**

When a combined task reaches completed, download `all.zip`, extract `odm_orthophoto.tif`, and call `create_tiles_task`.

- [ ] **Step 4: Add combined status and downloads**

Add status plus orthophoto, tiles, and manifest download endpoints under `/api/v1/process-and-tile/{task_id}`.

### Task 4: Deployment Wiring

**Files:**
- Modify: `requirements.txt`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `README.md`

- [ ] **Step 1: Add Python dependency**

Add `oss2==2.19.1`.

- [ ] **Step 2: Install GDAL in Docker**

Install `gdal-bin` in test and runtime stages.

- [ ] **Step 3: Add tile volumes and env vars**

Mount tile data directories and configure `UPLOAD_DIR`, `OUTPUT_DIR`, `TASKS_DIR`, and `PROJECTS_DIR`.

### Task 5: Verify

**Files:**
- All changed files

- [ ] **Step 1: Run focused tests**

Run: `python -m pytest tests/test_task_info.py -v`

- [ ] **Step 2: Run import check if pytest is unavailable**

Run: `python -m compileall app`

- [ ] **Step 3: Inspect git diff**

Run: `git diff --stat`
