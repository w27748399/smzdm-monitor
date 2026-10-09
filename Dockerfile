FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PORT=8000 \
    POLL_INTERVAL=10 \
    NO_HEALTH_SERVER=0

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

EXPOSE 8000

# 常驻轮询；健康检查 HTTP 由脚本内部起在 $PORT
CMD ["python", "-u", "monitor.py"]
