import json
import logging
import math
import os
import subprocess
import zipfile
from pathlib import Path

logger = logging.getLogger("odm")


def run_command(cmd: list[str], input_text: str | None = None):
    env = os.environ.copy()
    env["GDAL_PAM_ENABLED"] = "NO"
    logger.debug("执行命令: %s", " ".join(cmd))
    result = subprocess.run(cmd, input=input_text, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        logger.error("命令失败 (code %d): %s\nSTDERR: %s\nSTDOUT: %s",
                     result.returncode, " ".join(cmd), result.stderr, result.stdout)
        raise RuntimeError(
            f"Command failed (code {result.returncode})\nCMD: {' '.join(cmd)}\nSTDERR:\n{result.stderr}\nSTDOUT:\n{result.stdout}"
        )
    return result


def load_gdal_json(path: str, stats: bool = False):
    cmd = ["gdalinfo", "-json"]
    if stats:
        cmd.append("-stats")
    cmd.append(path)
    return json.loads(run_command(cmd).stdout)


def round_coord(value: float, digits: int = 9):
    return round(float(value), digits)


def safe_tile_name(row: int, col: int):
    return f"tile_r{row}_c{col}"


def transform_points_to_wgs84(points: list[tuple[float, float]], source_wkt: str):
    if not points:
        return []

    input_text = "\n".join(f"{x} {y}" for x, y in points) + "\n"
    result = run_command(
        ["gdaltransform", "-s_srs", source_wkt, "-t_srs", "EPSG:4326"],
        input_text=input_text,
    )
    transformed = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        transformed.append((float(parts[0]), float(parts[1])))
    if len(transformed) != len(points):
        raise RuntimeError("Failed to transform all tile coordinates to EPSG:4326")
    return transformed


def transform_points(points: list[tuple[float, float]], source_crs: str, target_crs: str):
    if not points:
        return []

    input_text = "\n".join(f"{x} {y}" for x, y in points) + "\n"
    result = run_command(
        ["gdaltransform", "-s_srs", source_crs, "-t_srs", target_crs],
        input_text=input_text,
    )
    transformed = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        transformed.append((float(parts[0]), float(parts[1])))
    if len(transformed) != len(points):
        raise RuntimeError("Failed to transform all points")
    return transformed


def polygon_points(aoi_geojson: dict):
    if aoi_geojson.get("type") == "Polygon":
        return [(float(point[0]), float(point[1])) for point in aoi_geojson.get("coordinates", [[]])[0]]
    raise RuntimeError("Only Polygon AOI is supported for aligned pair cropping")


def align_bounds_to_grid(bounds: dict, tile_width_m: float, tile_height_m: float):
    return {
        "min_x": math.floor(float(bounds["min_x"]) / tile_width_m) * tile_width_m,
        "min_y": math.floor(float(bounds["min_y"]) / tile_height_m) * tile_height_m,
        "max_x": math.ceil(float(bounds["max_x"]) / tile_width_m) * tile_width_m,
        "max_y": math.ceil(float(bounds["max_y"]) / tile_height_m) * tile_height_m,
    }


def build_aligned_aoi_crop_config(
    input_tif: str,
    aoi_geojson: dict,
    aoi_crs: str | None,
    tile_width_m: float,
    tile_height_m: float,
):
    dataset = load_gdal_json(input_tif)
    source_wkt = dataset.get("coordinateSystem", {}).get("wkt")
    if not source_wkt:
        raise RuntimeError("Source CRS is missing; aligned AOI cropping requires georeferenced imagery.")

    aoi_points = polygon_points(aoi_geojson)
    transformed_points = (
        transform_points(aoi_points, aoi_crs, source_wkt)
        if aoi_crs
        else aoi_points
    )
    xs = [point[0] for point in transformed_points]
    ys = [point[1] for point in transformed_points]
    bounds = align_bounds_to_grid(
        {
            "min_x": min(xs),
            "min_y": min(ys),
            "max_x": max(xs),
            "max_y": max(ys),
        },
        float(tile_width_m),
        float(tile_height_m),
    )
    geo_transform = dataset["geoTransform"]
    result = {
        "bounds": bounds,
        "resolution": {
            "pixel_size_x": abs(float(geo_transform[1])),
            "pixel_size_y": abs(float(geo_transform[5])),
        },
    }
    logger.info("[build_aligned_aoi_crop_config] 对齐后边界: %s, 分辨率: %s",
                result["bounds"], result["resolution"])
    return result


def build_tile_coordinates(tiles: list[dict], source_wkt: str | None):
    coordinates = {
        "crs": "EPSG:4326",
        "tiles": [],
    }
    if not source_wkt:
        coordinates["error"] = "Source CRS is missing; tile coordinates cannot be transformed to longitude/latitude."
        return coordinates

    source_points = []
    for tile in tiles:
        bbox = tile["bbox"]
        source_points.append((bbox["min_x"], bbox["max_y"]))
        source_points.append((bbox["max_x"], bbox["min_y"]))

    transformed_points = transform_points_to_wgs84(source_points, source_wkt)
    for index, tile in enumerate(tiles):
        top_left = transformed_points[index * 2]
        bottom_right = transformed_points[index * 2 + 1]
        coordinates["tiles"].append(
            {
                "name": tile["name"],
                "row": tile["row"],
                "col": tile["col"],
                "top_left": {
                    "lon": round_coord(top_left[0]),
                    "lat": round_coord(top_left[1]),
                },
                "bottom_right": {
                    "lon": round_coord(bottom_right[0]),
                    "lat": round_coord(bottom_right[1]),
                },
            }
        )
    return coordinates


def write_aoi_cutline(aoi_geojson: dict, aoi_crs: str | None, output_path: Path):
    feature_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": aoi_geojson,
            }
        ],
    }
    if aoi_crs:
        feature_collection["crs"] = {
            "type": "name",
            "properties": {
                "name": aoi_crs,
            },
        }
    output_path.write_text(json.dumps(feature_collection, ensure_ascii=True))


def crop_orthophoto_to_aoi(
    input_tif: str,
    output_tif: str,
    aoi_geojson: dict,
    aoi_crs: str | None = None,
    target_bounds: dict | None = None,
    target_resolution: dict | None = None,
):
    logger.info("[crop_orthophoto_to_aoi] 开始裁剪: %s -> %s, CRS=%s, bounds=%s",
                input_tif, output_tif, aoi_crs, target_bounds)
    cutline_path = Path(output_tif).with_suffix(".aoi.geojson")
    write_aoi_cutline(aoi_geojson, aoi_crs, cutline_path)

    cmd = [
        "gdalwarp",
        "-overwrite",
        "-of",
        "GTiff",
        "-cutline",
        str(cutline_path),
        "-dstalpha",
        "-r",
        "bilinear",
        "-co",
        "TILED=YES",
        "-co",
        "COMPRESS=LZW",
    ]
    if target_bounds is None:
        cmd.append("-crop_to_cutline")
    else:
        cmd.extend(
            [
                "-te",
                str(target_bounds["min_x"]),
                str(target_bounds["min_y"]),
                str(target_bounds["max_x"]),
                str(target_bounds["max_y"]),
            ]
        )
    if target_resolution is not None:
        cmd.extend(
            [
                "-tr",
                str(target_resolution["pixel_size_x"]),
                str(target_resolution["pixel_size_y"]),
            ]
        )
    if aoi_crs:
        cmd.extend(["-cutline_srs", aoi_crs])
    cmd.extend([input_tif, output_tif])
    run_command(cmd)
    logger.info("[crop_orthophoto_to_aoi] 裁剪完成: %s", output_tif)

    source_info = load_gdal_json(input_tif)
    cropped_info = load_gdal_json(output_tif)
    return {
        "path": output_tif,
        "cutline_path": str(cutline_path),
        "aoi_crs": aoi_crs,
        "aoi_geojson": aoi_geojson,
        "source_bounds": source_info.get("cornerCoordinates"),
        "cropped_bounds": cropped_info.get("cornerCoordinates"),
        "target_bounds": target_bounds,
        "target_resolution": target_resolution,
    }


def detect_empty_tile(tile_path: Path):
    info = load_gdal_json(str(tile_path), stats=True)
    bands = info.get("bands", [])

    alpha_band = None
    for band in bands:
        if band.get("colorInterpretation") == "Alpha":
            alpha_band = band
            break

    if alpha_band is not None:
        alpha_max = alpha_band.get("maximum")
        if alpha_max is not None:
            return alpha_max == 0

    for band in bands[:3]:
        metadata = band.get("metadata", {})
        valid_percent = metadata.get("", {}).get("STATISTICS_VALID_PERCENT")
        if valid_percent not in (None, "0", "0.0", 0, 0.0):
            return False

    return True


def split_orthophoto(
    input_tif: str,
    output_dir: str,
    tile_size: int = 1024,
    tile_width_m: float = 120.0,
    tile_height_m: float = 90.0,
    skip_empty_tiles: bool = True,
    export_png: bool = True,
    grid_origin_x: float | None = None,
    grid_origin_y: float | None = None,
    grid_pixel_size_x: float | None = None,
    grid_pixel_size_y: float | None = None,
):
    """按固定地理网格切割正射图，并输出 GeoTIFF、PNG 和 manifest。"""
    output_path = Path(output_dir)
    tif_dir = output_path / "geotiff"
    png_dir = output_path / "png"
    output_path.mkdir(parents=True, exist_ok=True)
    tif_dir.mkdir(parents=True, exist_ok=True)
    if export_png:
        png_dir.mkdir(parents=True, exist_ok=True)

    logger.info("[split_orthophoto] 读取正射图元数据: %s", input_tif)
    dataset = load_gdal_json(input_tif)
    size = dataset["size"]
    width, height = int(size[0]), int(size[1])
    geo_transform = dataset["geoTransform"]
    origin_x, pixel_size_x, _, origin_y, _, pixel_size_y = geo_transform
    pixel_size_y_abs = abs(pixel_size_y)
    source_bounds = dataset["cornerCoordinates"]
    min_x = float(source_bounds["lowerLeft"][0])
    max_x = float(source_bounds["upperRight"][0])
    min_y = float(source_bounds["lowerLeft"][1])
    max_y = float(source_bounds["upperRight"][1])
    logger.info("[split_orthophoto] 源图尺寸: %dx%d, 像素分辨率: (%.6f, %.6f), 范围: [%.2f, %.2f] - [%.2f, %.2f]",
                width, height, abs(pixel_size_x), pixel_size_y_abs, min_x, min_y, max_x, max_y)

    grid_pixel_size_x = abs(grid_pixel_size_x if grid_pixel_size_x is not None else pixel_size_x)
    grid_pixel_size_y = abs(grid_pixel_size_y if grid_pixel_size_y is not None else pixel_size_y_abs)
    tile_span_x = float(tile_width_m)
    tile_span_y = float(tile_height_m)
    tile_pixel_width = max(1, math.ceil(tile_span_x / grid_pixel_size_x))
    tile_pixel_height = max(1, math.ceil(tile_span_y / grid_pixel_size_y))

    if grid_origin_x is None:
        grid_origin_x = math.floor(min_x / tile_span_x) * tile_span_x
    if grid_origin_y is None:
        grid_origin_y = math.ceil(max_y / tile_span_y) * tile_span_y

    col_start = math.floor((min_x - grid_origin_x) / tile_span_x)
    col_end = math.ceil((max_x - grid_origin_x) / tile_span_x) - 1
    row_start = math.floor((grid_origin_y - max_y) / tile_span_y)
    row_end = math.ceil((grid_origin_y - min_y) / tile_span_y) - 1

    total_tiles = (row_end - row_start + 1) * (col_end - col_start + 1)
    logger.info("[split_orthophoto] 网格: origin=(%.2f, %.2f), span=(%.1f, %.1f)m, "
                "rows=%d..%d, cols=%d..%d, 预计 %d 个瓦片",
                grid_origin_x, grid_origin_y, tile_span_x, tile_span_y,
                row_start, row_end, col_start, col_end, total_tiles)

    manifest = {
        "input_file": str(input_tif),
        "tile_size": tile_size,
        "tile_width_m": tile_width_m,
        "tile_height_m": tile_height_m,
        "skip_empty_tiles": skip_empty_tiles,
        "export_png": export_png,
        "source": {
            "width": width,
            "height": height,
            "pixel_size_x": round_coord(abs(pixel_size_x)),
            "pixel_size_y": round_coord(pixel_size_y_abs),
            "bounds": {
                "min_x": round_coord(min_x),
                "min_y": round_coord(min_y),
                "max_x": round_coord(max_x),
                "max_y": round_coord(max_y),
            },
            "crs_wkt": dataset.get("coordinateSystem", {}).get("wkt"),
        },
        "grid": {
            "origin_x": round_coord(grid_origin_x),
            "origin_y": round_coord(grid_origin_y),
            "pixel_size_x": round_coord(grid_pixel_size_x),
            "pixel_size_y": round_coord(grid_pixel_size_y),
            "tile_span_x": round_coord(tile_span_x),
            "tile_span_y": round_coord(tile_span_y),
            "tile_pixel_width": tile_pixel_width,
            "tile_pixel_height": tile_pixel_height,
            "row_start": row_start,
            "row_end": row_end,
            "col_start": col_start,
            "col_end": col_end,
            "note": "For time-series comparison, reuse this grid.origin_x/grid.origin_y/grid.pixel_size_x/grid.pixel_size_y in future runs.",
        },
        "tiles": [],
        "summary": {
            "generated_tiles": 0,
            "skipped_empty_tiles": 0,
        },
    }

    processed = 0
    for row in range(row_start, row_end + 1):
        for col in range(col_start, col_end + 1):
            tile_ulx = grid_origin_x + col * tile_span_x
            tile_uly = grid_origin_y - row * tile_span_y
            tile_lrx = tile_ulx + tile_span_x
            tile_lry = tile_uly - tile_span_y

            tile_name = safe_tile_name(row, col)
            tile_tif = tif_dir / f"{tile_name}.tif"
            tile_png = png_dir / f"{tile_name}.png"

            run_command(
                [
                    "gdalwarp",
                    "-overwrite",
                    "-of",
                    "GTiff",
                    "-te",
                    str(tile_ulx),
                    str(tile_lry),
                    str(tile_lrx),
                    str(tile_uly),
                    "-ts",
                    str(tile_pixel_width),
                    str(tile_pixel_height),
                    "-dstalpha",
                    "-r",
                    "bilinear",
                    "-co",
                    "TILED=YES",
                    "-co",
                    "COMPRESS=LZW",
                    input_tif,
                    str(tile_tif),
                ]
            )

            is_empty = detect_empty_tile(tile_tif)
            if skip_empty_tiles and is_empty:
                tile_tif.unlink(missing_ok=True)
                manifest["summary"]["skipped_empty_tiles"] += 1
                processed += 1
                if processed % 10 == 0:
                    logger.info("[split_orthophoto] 进度: %d/%d (跳过 %d 空白)",
                                processed, total_tiles, manifest["summary"]["skipped_empty_tiles"])
                continue

            png_path = None
            if export_png:
                run_command(["gdal_translate", "-of", "PNG", str(tile_tif), str(tile_png)])
                png_path = str(tile_png)

            manifest["tiles"].append(
                {
                    "name": tile_name,
                    "row": row,
                    "col": col,
                    "bbox": {
                        "min_x": round_coord(tile_ulx),
                        "min_y": round_coord(tile_lry),
                        "max_x": round_coord(tile_lrx),
                        "max_y": round_coord(tile_uly),
                    },
                    "tif_path": str(tile_tif),
                    "png_path": png_path,
                }
            )
            manifest["summary"]["generated_tiles"] += 1
            processed += 1
            if processed % 10 == 0:
                logger.info("[split_orthophoto] 进度: %d/%d (已生成 %d, 跳过 %d 空白)",
                            processed, total_tiles,
                            manifest["summary"]["generated_tiles"],
                            manifest["summary"]["skipped_empty_tiles"])

    manifest_path = output_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2))
    tile_coordinates_path = output_path / "tile_coordinates.json"
    tile_coordinates = build_tile_coordinates(
        manifest["tiles"],
        manifest["source"].get("crs_wkt"),
    )
    tile_coordinates_path.write_text(json.dumps(tile_coordinates, ensure_ascii=False, indent=2))
    logger.info("[split_orthophoto] 切割完成: 生成 %d 张瓦片, 跳过 %d 张空白",
                manifest["summary"]["generated_tiles"],
                manifest["summary"]["skipped_empty_tiles"])
    return {
        "tiles_dir": str(output_path),
        "manifest_path": str(manifest_path),
        "tile_coordinates_path": str(tile_coordinates_path),
        "generated_tiles": manifest["summary"]["generated_tiles"],
        "skipped_empty_tiles": manifest["summary"]["skipped_empty_tiles"],
        "grid": manifest["grid"],
    }


def zip_directory(source_dir: str, output_zip: str):
    """将目录打包为 ZIP 文件"""
    source = Path(source_dir)
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in source.rglob("*"):
            if file.is_file() and not file.name.endswith(".aux.xml"):
                zf.write(file, file.relative_to(source))
