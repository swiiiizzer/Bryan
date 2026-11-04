FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir 'yt-dlp[default]'
WORKDIR /app
COPY server.py ./
COPY dist ./dist
ENV PORT=10000
EXPOSE 10000
CMD ["python3", "server.py"]
