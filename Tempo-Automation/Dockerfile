FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
CMD sh -c "gunicorn bot:app --bind 0.0.0.0:${PORT:-10000} --preload"
