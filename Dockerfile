# ===================== 测试阶段 =====================
FROM python:3.12-slim AS test

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 安装依赖和测试工具
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest -i $PIP_INDEX_URL

# 复制全部代码（含测试）
COPY . .

# 运行测试（测试不通过则构建失败）
RUN pytest tests/


# ===================== 运行时阶段 =====================
FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

RUN addgroup --system appgroup && adduser --system --no-create-home --ingroup appgroup appuser

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i $PIP_INDEX_URL

# 复制应用代码并准备运行目录
COPY app ./app
RUN mkdir -p /tmp/odm_tasks && \
    chown appuser:appgroup /tmp/odm_tasks /app

# 健康检查（每 30 秒请求 /health）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# 暴露端口
EXPOSE 8000

# 以非 root 用户运行
USER appuser

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
