"""
基于 FastAPI 的 NodeODM 中间件微服务
提供无人机照片到正射影像的 HTTP 接口
"""
import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .services import get_node_client, _cleanup_loop
from .routes import router

logger = logging.getLogger("odm")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        client = get_node_client()
        info = client.info()
        logger.info("已连接到 NodeODM 服务 v%s (%s)", info.version, info.engine)
    except Exception as e:
        logger.warning("无法连接到 NodeODM 服务：%s", e)

    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    cleanup_task.cancel()


app = FastAPI(
    title="ODM 正射影像生成服务",
    description="基于 NodeODM 的无人机照片处理服务，生成正射影像",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
