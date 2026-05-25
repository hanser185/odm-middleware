# ODM 正射影像生成微服务

基于 FastAPI 的 NodeODM 中间件服务，提供无人机照片到正射影像的 HTTP 接口。

## 功能特性

- ✅ 支持多张无人机照片上传
- ✅ 自动生成正射影像 (GeoTIFF)
- ✅ 异步任务处理
- ✅ 实时进度查询
- ✅ 结果下载（单文件或 ZIP 包）
- ✅ Docker 容器化部署（含健康检查）
- ✅ 后台自动清理过期任务（24 小时 TTL）
- ✅ 任务状态查询自动回写本地缓存

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
# 1. 从示例复制环境变量配置（按需修改）
cp .env.example .env

# 2. 启动所有服务（NodeODM + FastAPI 中间件）
docker-compose up -d

# 查看日志
docker-compose logs -f odm-middleware
```

### 方式二：本地运行

1. **启动 NodeODM 服务**
```bash
docker run -ti -p 3000:3000 webodm/nodeodm
```

2. **安装依赖**
```bash
pip install -r requirements.txt
```

3. **启动服务**
```bash
python -m app.main
# 或
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 构建说明

Dockerfile 采用多阶段构建，测试通过后才构建运行镜像：

```bash
# 只验证测试（不构建最终镜像）
docker build --target test .

# 构建部署镜像（自动先跑测试，不通过则中止）
docker build -t odm-middleware .

# Docker Compose 方式（底层自动先测试后构建）
docker-compose build
```

## API 接口

### 1. 上传照片并处理

```bash
curl -X POST "http://localhost:8000/api/v1/process" \
  -F "files=@image1.jpg" \
  -F "files=@image2.jpg" \
  -F "files=@image3.jpg" \
  -F 'options={"orthophoto_resolution": 5.0}'
```

响应：
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "message": "任务已创建，共 3 张照片"
}
```

### 2. 查询任务状态

```bash
curl "http://localhost:8000/api/v1/status/{task_id}"
```

响应：
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "running",
  "progress": 45.5,
  "images_count": 3,
  "processing_time": 120000
}
```

### 3. 下载正射影像

```bash
curl -o orthophoto.tif "http://localhost:8000/api/v1/download/{task_id}"
```

### 4. 下载所有结果（ZIP）

```bash
curl -o results.zip "http://localhost:8000/api/v1/download/{task_id}/zip"
```

### 5. 健康检查

```bash
curl "http://localhost:8000/health"
```

## API 文档

启动服务后访问：
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## 使用示例

```python
import requests

# 上传照片
files = [
    ("files", open("image1.jpg", "rb")),
    ("files", open("image2.jpg", "rb")),
]
response = requests.post(
    "http://localhost:8000/api/v1/process",
    files=files,
    data={"options": '{"orthophoto_resolution": 5.0}'}
)
task_id = response.json()["task_id"]

# 等待完成
import time
while True:
    status = requests.get(f"http://localhost:8000/api/v1/status/{task_id}")
    if status.json()["status"] == "completed":
        break
    time.sleep(5)

# 下载结果
response = requests.get(f"http://localhost:8000/api/v1/download/{task_id}")
with open("orthophoto.tif", "wb") as f:
    f.write(response.content)
```

完整示例请参见 `tests/` 目录下的测试用例

## 配置选项

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| NODEODM_HOST | NodeODM 服务地址 | localhost |
| NODEODM_PORT | NodeODM 服务端口 | 3000 |
| NODEODM_TOKEN | NodeODM 认证令牌 | (空) |
| TASK_TTL_HOURS | 任务本地目录保留时长（超时自动清理） | 24 |
| CLEANUP_INTERVAL | 后台清理检查间隔（秒） | 3600 |

所有环境变量可通过 `.env` 文件或 `docker-compose.yml` 的 `environment` 字段配置。参考 `.env.example`。

## 处理参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| orthophoto_resolution | float | 5.0 | 正射影像分辨率 (cm/pixel) |
| name | string | (自动生成) | 任务名称 |

## 任务状态

- `queued`: 任务已排队，等待处理
- `running`: 正在处理中
- `completed`: 处理完成
- `failed`: 处理失败
- `canceled`: 已取消

## 注意事项

1. **照片要求**：
   - 支持格式：JPG、PNG、TIFF
   - 建议：重叠度 60-80% 的航拍照片
   - 需要包含 GPS 信息（EXIF）

2. **处理时间**：
   - 取决于照片数量和分辨率
   - 一般 10-50 张照片需要 5-30 分钟

3. **资源需求**：
   - NodeODM 需要较多内存（建议 8GB+）
   - CPU 核心数影响处理速度

## 故障排除

**无法连接 NodeODM**
```bash
# 检查所有服务是否运行
docker-compose ps

# 查看 NodeODM 日志
docker-compose logs nodeodm

# 从中间件容器内部测试连接（docker-compose 模式下 3000 端口不对外暴露）
docker-compose exec odm-middleware python -c "import urllib.request; print(urllib.request.urlopen('http://nodeodm:3000').status)"
```

**任务处理失败**
- 检查照片数量（至少 3 张）
- 检查照片质量（足够的重叠度）
- 查看 NodeODM 日志：`docker-compose logs nodeodm`

## 自动清理

已完成或失败的任务本地目录会在 `TASK_TTL_HOURS`（默认 24 小时）后自动删除。
运行中的任务不会被清理。容器启动后会立即开始后台清理循环。

## 运行测试

```bash
# 安装测试依赖（pytest）
pip install pytest

# 运行所有测试
pytest tests/
```

## License

BSD 3-Clause
