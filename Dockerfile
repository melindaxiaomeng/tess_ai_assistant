FROM python:3.13-slim

# 不缓冲日志（容器里实时看到 stdout）、不写 __pycache__ 进镜像
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TESS_PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 只复制后端包，保持镜像精简（tess_backend/ 内含 thresholds.json 运行时需要）
COPY tess_backend ./tess_backend

EXPOSE 8080

# 端口经环境变量覆盖；host 固定 0.0.0.0 以便容器外访问
CMD ["sh", "-c", "uvicorn tess_backend.app:app --host 0.0.0.0 --port ${TESS_PORT}"]
