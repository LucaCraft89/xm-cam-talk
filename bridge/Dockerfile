FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends espeak-ng ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir aiohttp==3.10.11
COPY bridge.py /app/bridge.py
WORKDIR /app
EXPOSE 8090
CMD ["python","bridge.py"]
