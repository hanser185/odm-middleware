"""
基于 FastAPI 的 NodeODM 中间件微服务
提供无人机照片到正射影像的 HTTP 接口
"""
import logging
import asyncio
import uuid
from contextvars import ContextVar
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from .services import get_node_client, _cleanup_loop, get_mqtt_client
from .routes import router
from .tile_routes import tile_router
from .config import MQTT_HOST, MQTT_PORT

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()[:8]
        return True


logger = logging.getLogger("odm")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s",
)
for handler in logger.root.handlers:
    handler.addFilter(RequestIDFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        client = get_node_client()
        info = client.info()
        logger.info("已连接到 NodeODM 服务 v%s (%s)", info.version, info.engine)
    except Exception as e:
        logger.warning("无法连接到 NodeODM 服务：%s", e)

    if MQTT_HOST:
        get_mqtt_client()
        logger.info("MQTT 客户端已初始化（%s:%s）", MQTT_HOST, MQTT_PORT)

    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request_id_var.set(rid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid[:8]
        return response


app = FastAPI(
    title="ODM 正射影像生成服务",
    description="基于 NodeODM 的无人机照片处理服务，生成正射影像",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(tile_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestIDMiddleware)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
