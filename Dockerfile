FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS web
ENV UV_THREADPOOL_SIZE=2
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
COPY scripts/pwa-cache.mjs /build/scripts/pwa-cache.mjs
COPY VERSION /build/VERSION
RUN npm run build

FROM mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822 AS media
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 TZ=Asia/Shanghai
COPY --from=media /ffmpeg /ffprobe /usr/local/bin/
COPY --from=media /versions.json /usr/local/share/ffmpeg/versions.json
WORKDIR /app
COPY requirements.runtime.lock /app/requirements.runtime.lock
RUN pip install --no-cache-dir -r requirements.runtime.lock
COPY app /app/app
COPY VERSION /app/VERSION
COPY --from=web /build/frontend/dist /app/web
ENV DATA_DIR=/data MUSIC_DIR=/music INBOX_DIR=/inbox STATIC_DIR=/app/web
LABEL org.opencontainers.image.title="留声 · 个人音乐服务" org.opencontainers.image.version="2.2.1"
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn","app.main:create_app","--factory","--host","0.0.0.0","--port","8000","--workers","1","--no-proxy-headers"]
