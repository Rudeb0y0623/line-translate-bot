FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

WORKDIR /app

# 依存を先に入れてキャッシュを効かせる
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリ本体
COPY app.py .

EXPOSE 8080
# Render の無料枠で安定しやすい設定：ワーカー1つ
CMD gunicorn -w 1 -b 0.0.0.0:$PORT app:app
