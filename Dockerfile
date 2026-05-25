# ===================== 测试阶段 =====================
FROM python:3.12-slim AS test

WORKDIR /app

# 安装依赖和测试工具
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制全部代码（含测试）
COPY . .

# 运行测试（测试不通过则构建失败）
RUN pytest tests/


# ===================== 运行时阶段 =====================
FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 只复制运行所需代码
COPY app/ ./app/

# 创建临时目录
RUN mkdir -p /tmp/odm_tasks

# 健康检查（每 30 秒请求 /health）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
