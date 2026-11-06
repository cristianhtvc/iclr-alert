FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    CHECK_INTERVAL_SECONDS=120
COPY iclr_alert /app/iclr_alert
CMD ["python", "-m", "iclr_alert", "watch"]
