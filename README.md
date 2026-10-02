# Madina Electronics Stock Bot — Setup

Your dad messages this Telegram bot with a product name/brand/model and it
replies with quantity in stock, reading live from the Google Sheet
"Madina Electronics Stock Quantities". Replies are bilingual (English +
Roman Urdu).

Total setup time: ~15-20 minutes, one-time.

## 1. Create the Telegram bot (2 min)

1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot`, give it a name (e.g. "Madina Electronics Stock") and a
   username ending in `bot` (e.g. `madina_stock_bot`).
3. BotFather replies with a token like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.
   Save this — it's `TELEGRAM_BOT_TOKEN` below.

## 2. Create a Google service account (5 min)

This lets the bot read the sheet without needing your Google login.

1. Go to https://console.cloud.google.com/ (any Google account works).
2. Create a new project (top-left dropdown → New Project). Any name is fine.
3. In the search bar, go to **"Google Sheets API"** → click **Enable**.
4. Go to **APIs & Services → Credentials → Create Credentials → Service account**.
   Give it any name, click through, no roles needed.
5. Click into the service account you just made → **Keys** tab → **Add Key →
   Create new key → JSON**. This downloads a `.json` file — keep it safe,
   this is `GOOGLE_CREDENTIALS_JSON` below (you'll paste its full contents).
6. Open that JSON file, find the `"client_email"` field — it looks like
   `something@your-project.iam.gserviceaccount.com`.

## 3. Share the sheet with the service account (1 min)

1. Open **Madina Electronics Stock Quantities** in Google Sheets.
2. Click **Share**, paste in the `client_email` from step 2.6, give it
   **Viewer** access, send.
3. Copy the sheet's ID from its URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`
   That's `GOOGLE_SHEET_ID` below.

## 4. Deploy the bot (10 min)

The easiest free option is [Railway](https://railway.app):

1. Sign up at railway.app (GitHub login is easiest).
2. Push this folder to a new GitHub repo (or use Railway's "Deploy from
   local folder" if offered).
3. In Railway, **New Project → Deploy from GitHub repo**, pick the repo.
4. Go to the service's **Variables** tab and add three variables:
   - `TELEGRAM_BOT_TOKEN` — from step 1.3
   - `GOOGLE_SHEET_ID` — from step 3.3
   - `GOOGLE_CREDENTIALS_JSON` — paste the *entire* contents of the JSON
     file from step 2.5 as one value (Railway handles the newlines fine)
5. Railway will detect the `Procfile` and run `python bot.py` automatically.
   Check the **Deployments → Logs** tab for `Bot starting...` — if you see
   that with no errors, it's live.

(Render.com's free tier works the same way if you prefer it — same three
variables, same Procfile.)

## 5. Try it

Open Telegram, find your bot by the username you gave it in step 1.2, send
`/start`, then try:

- `PEL AC`
- `Metro geyser`
- `Dawlance 9160`

Send `/refresh` any time to force it to re-read the sheet immediately
(otherwise it re-reads automatically every 2 minutes, so edits to the
sheet show up within a couple of minutes on their own).

## Notes

- The bot searches Brand, Category, Model, and Variant across every tab
  of the sheet (skipping the Summary tab). Multiple words narrow the
  search — "PEL AC 18K" is more specific than "PEL".
- Add more people: just share the bot's Telegram username with anyone
  else who should be able to check stock. No extra setup needed per
  person.
- If you later add the Price Evaluation sheet to this bot too, duplicate
  the sheet ID handling — ask me and I'll extend it.
