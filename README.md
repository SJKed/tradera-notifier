# Tradera notifier – AoS Bargains

Checks Tradera every 15 minutes for cheap Warhammer listings (by default **Helsmiths of Hashut**, **Maggotkin of Nurgle** and **Kruleboyz**) and shows them in a small installable web app, **AoS Bargains**, with links straight to each Tradera listing. You can add, change and remove searches from inside the app.

Everything runs free on GitHub: Actions runs the bot, Pages hosts the app. The bot uses Tradera's official API and only reads search results. It never bids or buys.

## How it fits together

- `bot.py` searches Tradera using the searches in `config.json` and writes `docs/listings.json`.
- `docs/` is the app (PWA). It reads `listings.json` and shows the listings.
- `.github/workflows/watch.yml` runs the bot every 15 minutes and on every push, then publishes `docs/` to GitHub Pages.
- When you save searches in the app, it commits `config.json` to this repo. That push reruns the bot, so new results appear within a couple of minutes.

## Setup

1. **Tradera API key.** Register a developer account at <https://api.tradera.com/register>, create an application, and note its **App ID** and **App Key**.
2. **Secrets.** In this repo go to *Settings → Secrets and variables → Actions* and add `TRADERA_APP_ID` and `TRADERA_APP_KEY`.
3. **Pages.** Go to *Settings → Pages → Build and deployment → Source* and choose **GitHub Actions**. Free Pages needs a public repo. That's fine: only the listings are public, and your keys stay in secrets.
4. **First run.** Go to *Actions → Tradera watcher → Run workflow*. The app goes live at `https://sjked.github.io/tradera-notifier/`.
5. **Install on your phone.** Open that link. On iPhone, use Safari → Share → *Add to Home Screen*. On Android, use Chrome → menu → *Install app*.

## Editing searches from the app

Tap the magnifier button. The first time, the app asks for a GitHub token so it can save `config.json`:

1. Go to <https://github.com/settings/personal-access-tokens/new>.
2. Under **Repository access**, choose *Only select repositories* → `tradera-notifier`.
3. Under **Permissions → Repository permissions → Contents**, choose **Read and write**.
4. Generate the token and paste it into the app. It's stored only on that device.

Each search has:
- a **name**
- **search words**, each searched separately on Tradera
- a **max price** in kr
- **exclude words**: titles containing any of these are skipped

Under Options you can turn auctions off, set a stricter price cap for auctions, or change how many result pages to scan. The app shows your estimated daily API usage; Tradera allows 10,000 calls a day.

## In the app

- **NEW** marks listings found since you last opened the app. On Android, the app icon also shows a badge with the count.
- Filter by army, sort by newest, cheapest or ending soonest, or show buy-now listings only.
- Faded cards are listings whose price has risen above your limit, usually auctions with new bids.
- Ended listings, and listings older than 30 days, disappear automatically.

## Running the bot locally

```bash
export TRADERA_APP_ID=... TRADERA_APP_KEY=...
python3 bot.py --dry-run     # print matches without saving
python3 bot.py               # write docs/listings.json
python3 -m http.server -d docs 8000   # view the app at localhost:8000
```

Needs Python 3.11 or newer and no extra packages.
