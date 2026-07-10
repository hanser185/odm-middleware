# Process And Tile Design

## Goal

Add one frontend-facing workflow that accepts drone photos, creates an orthophoto through NodeODM, and automatically tiles the resulting GeoTIFF when NodeODM completes.

## API Shape

Keep existing APIs:

- `POST /api/v1/process` for orthophoto-only processing.
- `POST /api/tiles/process` and related `/api/task/{task_id}/...` endpoints for tiling an existing GeoTIFF.

Add:

- `POST /api/v1/process-and-tile`
- `GET /api/v1/process-and-tile/{task_id}/status`
- `GET /api/v1/process-and-tile/{task_id}/download/orthophoto`
- `GET /api/v1/process-and-tile/{task_id}/download/tiles`
- `GET /api/v1/process-and-tile/{task_id}/download/manifest`

## Architecture

The existing ODM task directory remains the source of truth for the combined task. `task_info.json` gains `workflow`, `tile_config`, `tile_task_id`, and `tile_status` fields for combined workflows. The NodeODM webhook remains the trigger point: when a combined task reaches `completed`, the service downloads `all.zip`, extracts `odm_orthophoto/odm_orthophoto.tif`, and starts the tile worker against that file.

The tiling service is imported as focused modules: `tile_routes.py`, `tile_tasks.py`, `gdal_utils.py`, and `oss_utils.py`. Its original routes and task persistence are kept intact for direct GeoTIFF tiling.

## Error Handling

ODM creation and status errors keep the existing Chinese HTTP responses. Tile startup failures are persisted into the combined task as `tile_status=failed` and `tile_error`. Direct tile endpoints keep their existing English error strings to avoid changing client behavior.

## Deployment

The runtime image installs GDAL command line tools. Docker Compose mounts separate tile upload/output/task/project volumes. `oss2` is included so OSS upload works when Aliyun OSS environment variables are configured.

## Testing

Add tests that prove:

- `POST /api/v1/process-and-tile` creates an ODM task with combined workflow metadata and tile config.
- The webhook starts a tile task after ODM completion by extracting GeoTIFF from NodeODM `all.zip`.
- The combined status endpoint reports ODM and tile task state.
- Original tile routes are registered.
