# Teleclaude

A Telegram bot that connects you to Claude. Send a message on Telegram, get a response from Claude.

## Setup

### 1. Create a Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token you receive

### 2. Get an Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com/)
2. Create an API key

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in your tokens:

```
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
ANTHROPIC_API_KEY=sk-ant-...
```

Optional settings in `.env`:
- `CLAUDE_MODEL` — which Claude model to use (default: `claude-sonnet-4-20250514`)
- `SYSTEM_PROMPT` — customize Claude's behavior
- `ALLOWED_USER_IDS` — comma-separated Telegram user IDs to restrict access (empty = allow all)

### 4. Run Locally

```bash
pip install -r requirements.txt
python bot.py
```

## Deploy to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) and create a new project from your repo
3. Add environment variables in the Railway dashboard:
   - `TELEGRAM_BOT_TOKEN`
   - `ANTHROPIC_API_KEY`
   - (and any optional ones from `.env.example`)
4. Railway will auto-detect the `Procfile` and deploy

## Usage

- Send any text message to your bot — it forwards to Claude and returns the response
- `/new` — clear conversation history and start fresh
- `/model` — show which Claude model is active
- `/help` — show available commands

## Security

If you're running this on a public server, set `ALLOWED_USER_IDS` in `.env` to restrict who can use the bot. You can find your Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot).
