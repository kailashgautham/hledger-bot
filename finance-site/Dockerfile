FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home --home-dir /home/site site

WORKDIR /app

COPY finance-site/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY config.yaml .
COPY finance-site/balances.py \
     finance-site/data.py \
     finance-site/server.py \
     finance-site/workflows.py \
     finance-site/index.html \
     finance-site/login.html \
     finance-site/favicon.svg \
     finance-site/manifest.json \
     finance-site/apple-touch-icon.png ./

RUN chown -R site:site /app

USER site
ENV PORT=8080 \
    HOME=/home/site \
    HLEDGER_PATH=/finance/journal.hledger

EXPOSE 8080

CMD ["python", "server.py"]
