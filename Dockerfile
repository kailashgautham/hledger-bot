FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home --home-dir /home/bot bot

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY config.yaml .
RUN chown -R bot:bot /app

USER bot
ENV HOME=/home/bot

CMD ["python", "-m", "bot.main"]
