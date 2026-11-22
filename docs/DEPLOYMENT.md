# 部署留声

留声由网页与 API 服务、独立 LX 执行器组成。下面从源码构建，不需要预先安装 Node.js；Docker 构建阶段会编译网页。

## 环境要求

- Linux Docker Engine + Docker Compose v2，或 macOS / Windows Docker Desktop（Linux 容器）。
- Git、Python 3，用于获取源码和生成私有配置。
- 构建时需要访问容器镜像仓库、npm 和 PyPI；建议至少预留 2 GB 可用内存和足够的镜像、音乐存储空间。
- 运行数据和 SQLite 必须位于本机磁盘；不要让多个实例或机器同时写入同一数据库。

当前仓库为私有，需要先使用有权限的 GitHub 账号完成 Git 认证。以下命令在仓库根目录执行；Windows 建议使用 WSL2。

## 1. 获取源码并初始化

```sh
git clone https://github.com/SeafyLiang/liusheng.git
cd liusheng
python3 scripts/init-deploy.py
```

初始化脚本创建权限为 0600 的 `.env`、随机执行器令牌和 `data`、`music`、`inbox` 三个目录。脚本不会覆盖已有 `.env`，不会修改已有音乐文件。请以普通用户执行，不使用 sudo；Linux 下容器 UID/GID 默认跟随该用户。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `BIND_ADDRESS` | `127.0.0.1` | 默认仅本机访问；局域网使用时改成宿主机 LAN IP |
| `HTTP_PORT` | `38661` | 网页与 API 端口，冲突时更改 |
| `LIUSHENG_UID` / `LIUSHENG_GID` | 初始化用户的 ID | 必须对三个挂载目录有读写权限 |
| `DATA_PATH` | `./data` | 数据库、封面、任务、账号、配置及暂存 |
| `MUSIC_PATH` | `./music` | 音乐库，可替换为已授权的绝对路径 |
| `INBOX_PATH` | `./inbox` | 本地文件投递目录 |
| `LX_RUNNER_TOKEN` | 随机生成 | 两个容器共用；不得公开或提交 |
| `TZ` | `Asia/Shanghai` | 调度时区 |
| `SPOTIFY_HTTPS_PROXY` | 空 | 可选，仅用于 Spotify 元数据请求；填容器可达的代理地址 |

接入已有音乐库前先确认备份与目录权限。扫描不会主动删库，但下载与整理会写入文件或元数据。默认目录适合先用少量复制的样本验证。不要为解决权限问题直接递归修改整个 NAS 曲库的属主。

## 2. 构建与启动

```sh
docker compose config --quiet
docker compose build app
docker compose build lx-runner
docker compose up -d
docker compose ps
```

两个镜像串行构建，避免同时占用过多资源。主服务等待 LX 执行器健康后启动。只有主服务映射端口，执行器不挂载曲库、数据库或私有配置文件。

打开 `http://127.0.0.1:38661`。在另一台设备访问时，应先修改 `BIND_ADDRESS` 为服务器 LAN IP，并使用该 IP 与配置端口访问。

## 3. 首次登录

账号为 `admin`。在自己的终端读取随机初始密码：

```sh
docker compose exec app cat /data/initial-password.txt
```

登录后在设置中修改密码。密码文件会在改密后删除；已初始化的数据目录重启不会生成新账号或清空曲库。不要把该文件、命令输出或 `.env` 放入截图和问题报告。

## 4. 配置音源、下载和整理

1. 在设置中检查音乐目录与投递目录；容器内对应 `/music` 和 `/inbox`。
2. 可先扫描自己的音乐文件，在网页音乐库中播放，验证目录和基础播放链路。
3. 在来源管理中导入有权使用的兼容 LX JS 文件或链接，检查平台与音质能力并验证样本整曲，再设置来源顺序。
4. 在“找音乐”中按歌名、歌手搜索，确认版本后试听或创建获取任务；来源不可用或版本有歧义时，查看失败/待匹配原因。
5. 音频入库后按缺项整理标签、封面与歌词，必要时人工修正并锁定。
6. 在任务设置中调整启动间隔、每小时上限；默认启用限流，单并发、间隔 10 秒、每小时最多启动 120 次。来源自身还可能进入冷却。

仓库不附带第三方音源脚本或音乐。搜索/榜单元数据能读取，不代表该曲目一定能获得完整音频；不要把来源初始化或样本成功视为全部曲目可用。

## 5. HTTPS 与反向代理

基础部署使用 HTTP；PWA 安装与离线界面需要可信 HTTPS。反向代理应转发 Host、X-Forwarded-Proto 和 X-Forwarded-For。若代理到应用，需通过单独的 Compose override 覆盖 `command`，启用 `--proxy-headers`，且 `--forwarded-allow-ips` 仅填写实际可信代理地址或受控网段。

不要直接设为 `*`。代理地址与拓扑由实际部署决定，本指南不预设网络号；先验证 HTTPS 登录、Cookie 与写操作，再对外使用。默认配置未开放公网，也不自动配置域名、证书或防火墙。

## 6. 日常检查与排障

```sh
curl --fail http://127.0.0.1:38661/api/health
docker compose logs --tail 100 app
docker compose logs --tail 100 lx-runner
```

- **Permission denied**：检查 `.env` 中 UID/GID 与三个目录权限。NAS 用户 ID 常与示例不同。
- **执行器启动失败**：检查令牌是否至少 32 字符、两服务配置是否一致；不要输出实际令牌到公共日志。
- **构建下载失败**：确认 Docker 的镜像与依赖源连接。不要用删除业务数据解决构建网络问题。
- **来源失败或 429**：查看来源冷却和任务状态，降低频率或换用可用来源；保留失败事实。
- **HTTPS 登录或保存失败**：检查反向代理的 Host、协议头与可信代理设置。
- **NAS 不支持资源参数**：先核对该 Docker 实现，再通过本地 override 调整不支持的限制；基础配置没有绑定特定 CPU 编号。

## 7. 备份、升级与回退

简单且一致的备份方式：在任务页暂停领取，等待在途任务结束，执行 `docker compose stop`，再备份 `.env`、完整 `data` 目录及自己需要保护的 `music`、`inbox`。停止期间播放不可用。完成后 `docker compose start`，检查健康并按原状态恢复任务。

运行中的 SQLite 不能仅复制主数据库文件；需要在线备份时使用 SQLite backup API，并额外规划配置、媒体与一致性验证。

升级前记录当前提交及镜像 ID，完成备份，再获取目标代码并依次构建两个镜像，执行 `docker compose up -d`。不要执行清空数据目录或 `down -v`。新版本可能迁移数据库；回退镜像前必须核对数据库兼容性，不得用旧备份直接覆盖升级后新增的账号、曲库与任务。

## 本地开发与验证

开发环境需要 Python 3.12、Node.js 24、FFmpeg / ffprobe。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
cd frontend
npm ci
npm run build
cd ..
.venv/bin/python -m pytest -q
.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 38661
```

本地默认数据保存在 `runtime/`，不要指向生产目录。原生开发的 LX 需要单独配置执行器地址与令牌；完整首次运行推荐 Compose 路径。

## 本次验证范围

2026-11-22：本机 ARM64 Docker 环境完成两个镜像构建、Compose 启动与健康检查、首次登录、网页静态资源、合成音频扫描与 HTTP Range 206 读取、重启后账号/曲库/暂停设置保留；后端 123 项测试通过。测试框架有一条 httpx 弃用提示，不影响本次结果。

AMD64、其他 NAS 环境、实际 HTTPS 反代与第三方音源整曲可用性未在此次部署包验证中覆盖。上述结果不代表所有来源或曲目都能下载。
