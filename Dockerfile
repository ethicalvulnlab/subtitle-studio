# Subtitle Studio — CPU image with ffmpeg and the translation stack.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/data/models \
    HF_HUB_DISABLE_XET=1 \
    OMP_NUM_THREADS=4

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch

COPY app ./app
COPY cli.py run.sh ./

# models and job state live on a volume so they survive a rebuild
VOLUME ["/data", "/app/state", "/app/output"]
ENV SUBTITLE_DATA=/data

EXPOSE 8099
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8099"]
