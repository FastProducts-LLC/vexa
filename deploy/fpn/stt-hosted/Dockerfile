# syntax=docker/dockerfile:1
# stt-hosted (fpn) — the OpenAI-compatible /v1/audio/transcriptions shim over hosted STT providers.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
