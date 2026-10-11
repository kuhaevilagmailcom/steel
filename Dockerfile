FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Fail the deployment early if Python syntax is broken.
RUN python -m py_compile deleted_message_logger_bot.py webapp_server.py

# Bothost mounts persistent bot data at /app/data.
RUN mkdir -p /app/data

CMD ["python", "deleted_message_logger_bot.py"]
