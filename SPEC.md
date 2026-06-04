# hledger Statement Bot — Product Spec

## Overview

A Telegram bot that accepts a credit card statement PDF, extracts transactions since the last recorded date, categorises them (asking for confirmation on uncertain ones), appends them to an hledger journal file, commits, and pushes to a remote git repository — all from your phone with minimal effort.

---

## Goals

- **Minimal friction**: Drop a PDF into Telegram, answer a few categorisation questions, done.
- **Stateful**: Remembers the last date it processed so you never double-count.
- **Learns over time**: Builds a merchant→account mapping that improves with each run.
- **Single source of truth**: hledger file on git is always up to date after each session.

---

## Architecture

```
Telegram (phone)
     │  PDF + commands
     ▼
Telegram Bot (Python, python-telegram-bot)
     │
     ├─► PDF Parser        — extracts transactions from bank PDF
     │
     ├─► Claude API        — categorises transactions, asks clarifying Qs
     │
     ├─► State Store       — tracks last-processed date, merchant mappings
     │
     └─► hledger + Git     — appends journal entries, commits, pushes
```

The bot runs as a long-running process on a server/home machine (or a cheap VPS). Your phone only ever needs Telegram.

---

## Core User Flow

```
You: [send PDF]
Bot: "Found 12 transactions from 2025-05-28 to 2025-06-04. Processing..."

Bot: "How should I categorise this?
      GRAB* 12345  SGD 18.50  (2025-06-01)
      My best guess: expenses:food:dining
      [✅ Confirm] [✏️ Change] [⏭ Skip]"

... (repeats for uncertain transactions only) ...

Bot: "All done! Here's a summary:
      ✅ 9 auto-categorised
      ✅ 3 confirmed by you
      
      expenses:food:dining       SGD 62.40
      expenses:transport         SGD 23.10
      expenses:shopping          SGD 154.00
      
      Appended to journal.hledger
      Committed: 'Add transactions 2025-05-28 to 2025-06-04'
      Pushed to origin/main ✓"
```

---

## Components

### 1. PDF Parser

- Accepts a PDF from Telegram.
- Uses a bank-specific parser (one per bank/card). Parser config is a small Python module that knows:
  - Column layout / field order
  - Date format
  - How to identify the card name
- Outputs a list of structured transactions:
  ```
  { date, description, amount_sgd, card }
  ```
- **Multi-card support**: Each bank has its own parser module. The bot auto-detects which bank's PDF it is based on text content heuristics (e.g. header/footer strings). You can also tag the PDF filename as a hint (e.g. `dbs-june.pdf`).

---

### 2. Date Filtering

- A `state.json` file (stored alongside the hledger file, committed to git) tracks:
  ```json
  {
    "last_date": "2025-06-04",
    "card_last_dates": {
      "DBS Altitude": "2025-06-04",
      "OCBC 90N": "2025-05-10"
    }
  }
  ```
- On each run, only transactions **after** the stored `last_date` for that card are processed.
- After a successful commit, `last_date` is updated and committed alongside the journal.

---

### 3. Categorisation Engine

**Known merchants** (from `merchant_map.json`):
- If a merchant has been seen before and confirmed, it is auto-categorised silently.
- Shown in the final summary but not asked about.

**Unknown / uncertain merchants**:
- Sent to Claude API with context:
  - Transaction description + amount
  - Your existing hledger account list (read from journal file)
  - Recent examples from your journal for few-shot context
- Claude suggests the most likely account.
- Bot presents this to you in Telegram with three inline buttons:
  - **✅ Confirm** — accept suggestion, save to merchant map
  - **✏️ Change** — bot prompts you to type the correct account; saved to merchant map
  - **⏭ Skip** — leaves transaction with a `; TODO` comment in the journal for you to fix later

**Merchant map** (`merchant_map.json`):
```json
{
  "GRAB*": "expenses:transport",
  "FAIRPRICE": "expenses:groceries",
  "NETFLIX.COM": "expenses:subscriptions"
}
```
- Matching is by prefix/substring, case-insensitive.
- Committed to git alongside the journal.

---

### 4. hledger Journal Writer

Appends entries in standard hledger format:

```
2025-06-01 Grab
    expenses:transport        SGD 12.50
    liabilities:creditcard:dbs

2025-06-02 Netflix
    expenses:subscriptions    SGD 10.98
    liabilities:creditcard:dbs
```

- The credit account is configurable per card (e.g. `liabilities:creditcard:dbs`, `liabilities:creditcard:ocbc`).
- Transactions with unresolved categories get a `; TODO` tag so `hledger` can still parse the file.
- A blank line separates each transaction.
- Entries are appended in date order.

---

### 5. Git Integration

After all entries are written:

```bash
git add journal.hledger merchant_map.json state.json
git commit -m "Add transactions {start_date} to {end_date} [{card_name}]"
git push origin main
```

- Uses the git remote already configured in the repo.
- If push fails (e.g. conflict), bot reports the error and leaves the commit local so you can resolve manually.

---

## Telegram Bot Commands

| Command | Description |
|---|---|
| _(send a PDF)_ | Triggers the main flow |
| `/status` | Shows last processed date per card, and any `; TODO` entries pending |
| `/accounts` | Lists all hledger accounts found in your journal |
| `/undo` | Reverts the last commit (runs `git revert HEAD`) |
| `/merchants` | Shows the current merchant map |

---

## Configuration (`config.yaml`)

```yaml
telegram:
  allowed_user_ids: [123456789]   # whitelist — only you can use the bot

hledger:
  journal_path: ~/finance/journal.hledger
  git_branch: main

cards:
  - name: DBS Altitude
    liability_account: liabilities:creditcard:dbs
    parser: parsers/dbs.py
  - name: OCBC 90N
    liability_account: liabilities:creditcard:ocbc
    parser: parsers/ocbc.py

currency: SGD

claude:
  model: claude-sonnet-4-20250514
  confidence_threshold: 0.85    # below this, always ask even if merchant seen before
```

---

## State & Persistence Files

All of these live in the same git repo as your hledger journal:

| File | Purpose |
|---|---|
| `journal.hledger` | Your main ledger |
| `merchant_map.json` | Learned merchant→account mappings |
| `state.json` | Last processed date per card |

Everything is committed together so the state is never out of sync with the journal.

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| PDF can't be parsed | Bot replies with error and asks you to check the format |
| No new transactions found | Bot says "Nothing new since {last_date}" |
| Claude API down | Bot falls back to asking you to categorise everything manually |
| Git push fails | Bot warns you; journal is still written and committed locally |
| Duplicate detection | If a transaction with same date+amount+description already exists in journal, it is skipped with a warning |

---

## Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | Best ecosystem for PDF parsing + Telegram + hledger tooling |
| Telegram | `python-telegram-bot` v20+ | Async, well-maintained |
| PDF parsing | `pdfplumber` | Reliable table/text extraction from bank PDFs |
| AI categorisation | Anthropic Claude API (claude-sonnet-4-20250514) | Accurate, easy to prompt with account context |
| hledger output | String templating | Simple and transparent |
| Git | `subprocess` / `gitpython` | Straightforward for commit+push |

---

## Deployment

### Infrastructure
- **VPS**: Ubuntu/Debian
- **Runtime**: Docker container (single container, no orchestration needed)

### Repository Layout

```
hledger-bot/
├── bot/
│   ├── main.py               # Telegram bot entrypoint
│   ├── parser/
│   │   ├── dbs.py
│   │   └── ocbc.py
│   ├── categoriser.py        # Claude API calls
│   ├── writer.py             # hledger journal writer
│   ├── git_ops.py            # commit + push
│   └── state.py              # state.json read/write
├── config.yaml
├── Dockerfile
├── docker-compose.yml
└── .env                      # secrets (never committed)
```

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY config.yaml .

CMD ["python", "-m", "bot.main"]
```

### docker-compose.yml

```yaml
services:
  hledger-bot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ~/finance:/finance          # your hledger git repo, mounted into container
      - ~/.ssh:/root/.ssh:ro        # SSH key for git push
```

The `~/finance` volume mount means the bot reads and writes your actual journal files directly on the VPS. Git push uses your existing SSH key.

### .env (secrets, never committed)

```
TELEGRAM_BOT_TOKEN=...
ANTHROPIC_API_KEY=...
```

### Setup Steps (one-time)

```bash
# On your VPS
git clone <your-hledger-repo> ~/finance
git clone <this-bot-repo> ~/hledger-bot
cd ~/hledger-bot
cp .env.example .env          # fill in your tokens
docker compose up -d          # starts and stays running
docker compose logs -f        # verify it's working
```

### Updates

```bash
cd ~/hledger-bot
git pull
docker compose up -d --build  # rebuilds and restarts
```

---

## Out of Scope (v1)

- Automatic PDF fetching from email / bank portal (you drop it manually into Telegram)
- Multi-user support
- Web dashboard / reporting
- Bank API / OFX/QIF integration
- Expense splitting

---

## Open Questions / Future Enhancements

- **Receipt photo support**: Send a photo of a receipt instead of a PDF for ad-hoc entries.
- **Weekly reminder**: Bot pings you every Sunday: "Time to log your transactions!"
- **Fuzzy merchant matching**: Handle slight variations in merchant names (e.g. `GRAB* SG` vs `GRAB*12345`).
- **Category corrections**: A `/recategorise MERCHANT ACCOUNT` command to bulk-update past entries.
