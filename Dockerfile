# 使用 DaoCloud Docker Hub 镜像拉取基础镜像（国内直连）
# 如果你的 VPS 已经配置了 /etc/docker/daemon.json 的 registry-mirrors，
# 可以把下面这行改回 `FROM python:3.12-alpine`
FROM docker.m.daocloud.io/library/python:3.12-alpine

# 把 Alpine 的 apk 源切到中科大镜像，避免走国际 CDN
RUN sed -i 's|https\?://dl-cdn.alpinelinux.org|https://mirrors.ustc.edu.cn|g' /etc/apk/repositories \
 && apk add --no-cache curl tzdata ca-certificates

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    LOG_DIR=/app/logs

WORKDIR /app
COPY scheduler.py urls.txt ./
RUN mkdir -p /app/logs

CMD ["python", "/app/scheduler.py"]
