# VPS 随机凌晨下行流量调度器

一个部署在 VPS 上的轻量 Docker 服务。每天在**凌晨随机时刻**从**国内镜像站**下载大文件到 `/dev/null`，用来：

- 保证 VPS 下行流量 >> 上行流量（应对上下行比例考核）
- 避免产生国际出境流量（只使用中国大陆镜像源）
- 带宽不打满（默认限速 100 Mbps，可调）

## 工作原理

- 常驻 Python 守护进程（容器 `CMD`，无需 cron）
- 每天在 `[WINDOW_START, WINDOW_END]`（默认 `02:00–06:00`）之间 `random.uniform` 一个触发时刻，`sleep` 等待
- 到点后，在 `MIN_FILES`–`MAX_FILES` 之间随机一个文件数（默认 1–3 个），从可用 URL 池里随机无放回抽取这些 ISO 完整下载
- 每天只触发 **1 次**；session 结束日期写入 `./logs/last_run_date.txt`，即使 `docker compose restart` 也不会让当天再跑一次
- 启动时对 `urls.txt` 里的每个 URL 做 `HEAD` 预检，自动剔除不可用 / 过小的文件
- 调用 `curl -o /dev/null --limit-rate 12500k ...` 按选中顺序依次下载
- 日志写 stdout + `./logs/scheduler.log`（挂载到宿主机，10MB × 5 轮转）

## 快速部署

```bash
git clone https://github.com/mino354e67/hhhhjjjmmmmm123.git
cd hhhhjjjmmmmm123
docker compose up -d --build

# 查看日志
docker compose logs -f
# 或
tail -f logs/scheduler.log
```

> **国内 VPS 注意**：`Dockerfile` 默认通过 DaoCloud 镜像拉取 `python:3.12-alpine`，
> Alpine 的 apk 源也已切到中科大镜像，所以构建全程不走国际网络。
> 如果你已经配置了 `/etc/docker/daemon.json` 的 `registry-mirrors`，
> 可以把 `FROM` 改回 `python:3.12-alpine` 使用你自己的镜像策略。
> 一个可选的 daemon.json 示例：
>
> ```json
> {
>   "registry-mirrors": [
>     "https://docker.m.daocloud.io",
>     "https://docker.1ms.run",
>     "https://dockerproxy.cn"
>   ]
> }
> ```
>
> 编辑完执行 `systemctl restart docker`。

首次启动后，你应该能看到：

```
配置: 窗口 02:00-06:00, 每天 1-3 个文件, 限速 12500k
加载 17 个 URL，开始 HEAD 预检...
可用 mirrors.tuna.tsinghua.edu.cn/... (4.74 GB)
...
15 个 URL 通过预检
下一次触发时刻: 2026-04-16 03:47:12
```

## 配置（环境变量）

全部在 `docker-compose.yml` 里修改：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TZ` | `Asia/Shanghai` | 时区 |
| `WINDOW_START` | `02:00` | 触发窗口起（HH:MM） |
| `WINDOW_END` | `06:00` | 触发窗口止（HH:MM，支持跨零点） |
| `MIN_FILES` | `1` | 每天随机下载的 ISO 文件数下限 |
| `MAX_FILES` | `3` | 每天随机下载的 ISO 文件数上限（不超过可用 URL 数） |
| `RATE_LIMIT` | `12500k` | `curl --limit-rate` 格式。12500k ≈ 100 Mbps，5000k ≈ 40 Mbps |
| `MIN_FILE_MB` | `500` | 预检时小于此大小的文件会被丢弃 |
| `CONNECT_TIMEOUT` | `15` | curl 连接超时（秒） |
| `MAX_TIME` | `3600` | 单次 curl 最长执行时间（秒） |

> 当日是否已跑过 session 由 `./logs/last_run_date.txt` 记录。
> 想强制今天再跑一次：`rm logs/last_run_date.txt` 然后 `docker compose restart`（会触发容器在剩余窗口内随机）。

## 自定义镜像源

编辑 `urls.txt`（按行一个 URL，`#` 开头为注释）。务必只使用**国内落地**的镜像站，避免国际流量。

建议按 VPS 所在运营商选近源：

- **电信**：清华 TUNA、阿里云
- **联通**：中科大 USTC、北外 BFSU
- **移动**：网易、华为云

手工验证某个 URL 是否走境内：

```bash
# 查看路由
traceroute mirrors.tuna.tsinghua.edu.cn
# 快速测速（只下 100 MB）
curl -o /dev/null --max-time 30 -w "%{speed_download}\n" \
  https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/22.04/ubuntu-22.04.4-desktop-amd64.iso
```

编辑后无需重建镜像，`urls.txt` 是以只读卷挂载的：

```bash
docker compose restart
```

## 手动触发一次（调试）

```bash
# 按 MIN_FILES~MAX_FILES 随机下载
docker compose exec speedtest-scheduler python /app/scheduler.py --once

# 指定下载 1 个文件（冒烟测试）
docker compose exec speedtest-scheduler python /app/scheduler.py --once --files 1
```

`--once` 不受 `last_run_date.txt` 影响，不写入当日标记，可以随意反复调用。

## 查看下载效果

```bash
# 宿主机流量统计（需 vnstat）
vnstat -h
vnstat -d

# 容器内 curl 日志
tail -f logs/scheduler.log
```

## 常见问题

**Q: 某个镜像启动时就被剔除了？**
A: 看 `scheduler.log` 里的 `HEAD 失败` 或 `跳过小文件` 行。镜像站偶尔会把老 ISO 下架，改用 `urls.txt` 里其它源即可。

**Q: 想每天多下几个 / 少下几个？**
A: 调 `MIN_FILES` / `MAX_FILES`。例如「每天稳定下 2 个」就 `MIN_FILES=2 MAX_FILES=2`。`MAX_FILES` 超过可用 URL 数时会自动截到可用数。

**Q: 想一天多跑几次 session？**
A: 当前设计是「每天 1 次 session，session 内下 N 个文件」。想要多次触发，需要改 `main_loop` 里基于 `last_run_date.txt` 的去重判定。

**Q: 带宽比 100 Mbps 还小？**
A: 调低 `RATE_LIMIT` 并减少 `MAX_FILES`。例如 50 Mbps + 每天 1 个文件：`RATE_LIMIT=6250k`、`MIN_FILES=1`、`MAX_FILES=1`。

**Q: 怎么停？**
A: `docker compose down`。守护进程会捕获 `SIGTERM` 并优雅退出。
