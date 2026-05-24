# BOM→YYZ Flight Monitor

Tracks Saudia & Etihad (EY205+EY22) round-trip flight prices from Mumbai to Toronto on [offers.reward360.in](https://offers.reward360.in).

## How it works

- Runs every 2 hours via GitHub Actions
- Checks prices for the preferred dates (28 Jul → 27 Sep) and ±7 day flex
- Compares with previous state — sends Telegram alert when prices change
- Stores state in `prices.json` (committed back to repo)

## Etihad filter

Only tracks the EY205 (BOM→AUH 23:10) + EY22 (YYZ→AUH 13:40) combination.

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/botfather) and get its token
2. Find your chat ID (send a message to the bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`)
3. Add repo secrets:
   - `TELEGRAM_BOT_TOKEN` — your bot token
   - `TELEGRAM_CHAT_ID` — your chat ID

## Manual run

```bash
gh workflow run "BOM→YYZ Flight Monitor"
```
