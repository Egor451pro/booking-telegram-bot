FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /app \
    && chown appuser:appuser /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser bot ./bot
COPY --chown=appuser:appuser webapp ./webapp

USER appuser

CMD ["python", "-m", "bot.main"]
