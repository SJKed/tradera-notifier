# Tradera notifier – AoS Bargains

Checks Tradera every 15 minutes for cheap Warhammer listings (by default **Helsmiths of Hashut**, **Maggotkin of Nurgle** and **Kruleboyz**) and shows them in a small installable web app, **AoS Bargains**, with links straight to each Tradera listing. It can send a push notification to your phone for each new find, and you can add, change and remove searches from inside the app.

Everything runs free on GitHub: Actions runs the bot, Pages hosts the app. The bot uses Tradera's official API and only reads search results. It never bids or buys.

## How it fits together

- `bot.py` searches Tradera using the searches in `config.json` and writes `docs/listings.json`.
- `docs/` is the app (PWA). It reads `listings.json` and shows the listings.
- `.github/workflows/watch.yml` runs the bot every 15 minutes and on every push, then publishes `docs/` to GitHub Pages.
- When you save searches in the app, it commits `config.json` to this repo. That push reruns the bot, so new results appear within a couple of minutes.
- When you turn on notifications, the app commits this device's push subscription to `push/subscriptions.json`. The bot signs pushes with a key it creates on its first run and keeps in the Actions cache, so the key is never in the repo.

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

Under Options, **Always exclude** holds words that apply to every search. It comes pre-filled to skip paints, White Dwarf and other magazines, Blood Bowl, novels, battletomes, card packs and dice. Exclude words match whole words, so `paint` hides "Citadel Paint" but not "Painted Kruleboyz". End a word with `*` to also match longer words (`paint*` hides both). When you add an exclude word, listings already in the app that match it disappear on the next run.

You can also turn auctions off, set a stricter price cap for auctions, or change how many result pages to scan. The app shows your estimated daily API usage; Tradera allows 10,000 calls a day.

## Notifications

Open the magnifier button → **Notifications** → turn on *Notify this device about new bargains* (it uses the same GitHub token as the search editor). Within a few minutes you get a "Notifications are on" test ping. After that, each new bargain sends one notification, and tapping it opens the Tradera listing.

- **iPhone/iPad:** push only works in the installed app (iOS 16.4 or newer). Add it to your Home Screen first, open it from there, then turn notifications on.
- **No floods:** nothing is sent on the bot's very first run, or for listings a brand-new search finds on its first run. If more than 5 new listings turn up at once, you get one summary instead.
- Turn notifications on separately on each device you want pinged. Turning them off removes that device.
- If the Actions cache is ever cleared, the bot makes a new signing key. The app notices and re-subscribes the next time you open it.

## Bargains

The **Bargains** tab lists auctions whose current bid is under half of the kit's MSRP, whatever your price limits are. A 1,500 kr Great Unclean One at 450 kr shows up here even if your Nurgle limit is 200 kr. You get a notification for each new bargain ("Bargain: 450 kr (30% of MSRP)").

- **Where MSRP comes from:** Games Workshop's site can't be read by a bot, so once a week the bot downloads GW's UK RRP and product code for every Age of Sigmar kit from a UK retailer's public price feed. It converts pounds to kronor with a factor, 14 kr/£ by default, which you can change under Options along with the 50% threshold. Any army you add a search for is covered automatically.
- **Matching:** the bot finds the kit from GW's product code when a title includes it (e.g. 99120211003), otherwise from the kit name in the title plus the army. Sprues, bits, and single models out of a multi-model box ("1 st Plaguebearer") are never counted as bargains. Bundles are priced as their single most expensive matched kit, so they're under- rather than over-valued.
- **It's a heuristic:** each card shows which kit it matched and that kit's MSRP. If one is wrong, tap **Hide** to remove it from Bargains on that device.
- Auction prices rise. A bargain drops out once bids pass the threshold.

## In the app

- **NEW** marks listings found since you last opened the app. On Android, the app icon also shows a badge with the count.
- Filter by army, sort by newest, cheapest or ending soonest, or show buy-now listings only.
- Every run, listings are re-checked against your current settings. Anything over your price limit (a lowered limit, or bids that went past it), matching an exclude word, or found only by a search word you've removed disappears. Change the setting back and they return.
- Ended listings, and listings older than 30 days, disappear automatically.

## Running the bot locally

```bash
export TRADERA_APP_ID=... TRADERA_APP_KEY=...
python3 bot.py --dry-run     # print matches without saving
python3 bot.py               # write docs/listings.json
python3 -m http.server -d docs 8000   # view the app at localhost:8000
```

Needs Python 3.11 or newer and no extra packages.
