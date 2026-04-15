FROM python:3.12-alpine

RUN apk add --no-cache curl tzdata ca-certificates

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    LOG_DIR=/app/logs

WORKDIR /app
COPY scheduler.py urls.txt ./
RUN mkdir -p /app/logs

CMD ["python", "/app/scheduler.py"]
