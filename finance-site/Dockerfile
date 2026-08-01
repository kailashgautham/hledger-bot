FROM python:3.11-slim

WORKDIR /app

COPY balances.py data.py server.py index.html ./

RUN useradd --uid 1000 --create-home --home-dir /home/site site

USER site
ENV PORT=8080 \
    HLEDGER_PATH=/finance/journal.hledger

EXPOSE 8080

CMD ["python", "server.py"]
