FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# База и медиа-архив живут в /app/data — монтируй volume, чтобы не потерять
RUN mkdir -p /app/logger_data

CMD ["python", "deleted_message_logger_bot.py"]
