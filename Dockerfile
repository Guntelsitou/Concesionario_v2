FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# git: el bot publica la web con `git push` cuando GIT_PUSH=true
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

WORKDIR /app
COPY bot/requirements.txt /app/bot/requirements.txt
RUN pip install --no-cache-dir -r /app/bot/requirements.txt

COPY bot /app/bot
COPY web /app/web

WORKDIR /app
EXPOSE 8000
CMD ["uvicorn", "bot.whatsapp_bot:app", "--host", "0.0.0.0", "--port", "8000"]
