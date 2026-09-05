FROM python:3.13-slim-bookworm

ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.revision=${GIT_COMMIT} \
      com.find-apartment.image=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system apartment-bot \
    && useradd --system --gid apartment-bot --home-dir /app apartment-bot \
    && mkdir --parents /data \
    && chown apartment-bot:apartment-bot /data

COPY --chown=apartment-bot:apartment-bot notification_bot.py test_notification_bot.py /app/

USER apartment-bot

ENTRYPOINT ["python", "/app/notification_bot.py"]
CMD ["--state-file", "/data/notification-bot.sqlite3"]
