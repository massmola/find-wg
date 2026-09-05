# Apartment Notification Aggregator

This bot combines two kinds of research:

- email notifications from housing sites and page monitors;
- direct, read-only searches of public Willhaben, WG-Gesucht and ÖH housing pages.

It removes duplicates, filters out clearly over-budget offers, assigns a location priority and forwards each match to one Telegram chat. On its first direct scan it reports all matching current listings it can see. Later scans report only new listings or listings whose advertised price changed.

It does **not** log into housing sites, automatically contact landlords or transfer money. Public page formats can change, so source errors are printed clearly in the bot log.

## Prerequisites

- Python 3.11 or newer;
- an email mailbox with IMAP enabled;
- WG-Gesucht or other housing alerts delivered to that mailbox (recommended as a backup);
- a Telegram bot token and chat ID.

## Configure it

1. Copy the sample configuration:

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

2. Enter the mailbox's IMAP details. Use an app-specific password if the provider supports one. Do not put your normal email password into source control.

3. In Telegram, message `@BotFather`, create a bot and copy its token into `.env`.

4. Send any message to the new bot, then open:

   ```text
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```

   Copy `message.chat.id` into `TELEGRAM_CHAT_ID`.

5. Adjust `ALERT_MATCH_TERMS` if the notification sender uses a different name.

6. Direct research is enabled by default. These settings can be adjusted in `.env`:

   ```dotenv
   ENABLE_DIRECT_SEARCH=true
   DIRECT_SCAN_SECONDS=900
   DIRECT_SOURCE_TIMEOUT=20
   DIRECT_POSTCODES="1020,1040,1050,1100"
   DIRECT_MAX_DISTANCE_KM=4.0
   WILLHABEN_MAX_PAGES=2
   MIN_CONTRACT_MONTHS=6
   MAX_CONTRACT_MONTHS=24
   IDEAL_CONTRACT_MONTHS=12
   ```

   The postcode list deliberately follows the proximity-first search. A listing in one of these postcodes is still only a candidate: the exact 08:00 door-to-door route must be verified before applying.

## Test it safely

Run the complete synthetic self-test after initial setup or whenever credentials and filters change:

```bash
python notification_bot.py --self-test
```

It performs four checks:

1. validates the required configuration;
2. authenticates to Gmail and opens the configured folder read-only;
3. passes a synthetic €650 listing in 1040 Wien through the real email parser, budget filter, location priority and link extractor;
4. sends the formatted result to Telegram with an `APARTMENT BOT SELF-TEST` heading.

The synthetic message is created locally. It does not email anyone, contact a landlord or alter the deduplication database. A successful run ends with `SELF-TEST PASSED` and produces one clearly labeled Telegram message.

The unit tests do not use Gmail or Telegram and can be run separately:

```bash
python -m unittest -v
```

Run a one-time dry test. Messages are printed, not forwarded:

```bash
temporary_state=$(mktemp /tmp/apartment-bot-test-XXXXXX.sqlite3)
rm "$temporary_state"
python notification_bot.py --dry-run --state-file "$temporary_state"
```

Using a temporary state file prevents the test from marking real listings as already notified. It is safe to remove that temporary database afterward.

Then test Telegram delivery:

```bash
python notification_bot.py --once
```

Finally, leave the bot running:

```bash
python notification_bot.py
```

It checks email every 60 seconds and public housing pages every 15 minutes by default. Stop it with `Ctrl+C`.

To test only the existing email pipeline and skip website access:

```bash
python notification_bot.py --dry-run --email-only
```

## How filtering works

- Email text must contain one configured alert term and an identifiable fixed contract duration.
- Only fixed contracts from 6 through 24 months are accepted. Unlimited contracts, terms shorter than 6 months, terms longer than 24 months, and listings whose duration cannot be established are rejected.
- A 12-month contract is marked as ideal and used as a tie-breaker after location and straight-line proximity. It never causes a farther apartment to outrank a closer one solely because of contract length.
- If the bot finds prices, the smallest plausible amount must be at most €700. This avoids mistaking a larger deposit for the monthly rent.
- Alerts without a detectable price are forwarded for manual review rather than silently discarded.
- Willhaben, WG-Gesucht and ÖH results at or below `MAX_RENT_EUR` are collected directly when their postcode is in `DIRECT_POSTCODES`. Willhaben results beyond the coarse `DIRECT_MAX_DISTANCE_KM` radius are discarded.
- Three-room-or-larger whole apartments are collected up to `GROUP_3_MAX_RENT_EUR` (€1,500 total by default) for three people.
- Two-room-or-larger whole apartments that do not match the three-person profile are collected up to `GROUP_2_MAX_RENT_EUR` (€1,600 total by default) for two people.
- Group-apartment prices are total advertised monthly prices, not per-person budgets. Room count does not prove that every bedroom is separately accessible, so the alert explicitly requires checking the floor plan and whether a WG is permitted.
- A normal apartment needs one private room per person plus a living room (four rooms for three people, three rooms for two). One fewer room is accepted only when the advert explicitly says it is suitable for a two- or three-person WG.
- Reserved adverts, daily rates below €200, and municipal/cooperative transfers that explicitly require eligibility documents are discarded as noise.
- A public listing is notified once. If its advertised price changes, it is notified again.
- When a public search card omits the contract, the bot reads the listing's public detail page. Results are cached for six hours by default (`CONTRACT_CACHE_SECONDS=21600`) to avoid repeatedly requesting the same pages.
- `1040`, Wieden and the main TU Wien streets receive priority A.
- `1050`, `1100` and direct-U1 terms receive priority B.
- `1020` and unknown locations receive priority C and must be checked manually.

The direct collectors use only public search results and need no housing-site credentials. The bot cannot reliably calculate door-to-door travel time when an advert hides the exact address. Always apply the acceptance checks in `accommodation-guide.md` before responding or paying.

## Run it permanently on a server

For a DigitalOcean Droplet or another always-on Linux server, use the included
Docker Compose deployment. It restarts the bot after failures and server reboots,
persists the deduplication database, and rotates logs. Follow
[`DEPLOYMENT.md`](DEPLOYMENT.md) for setup, verification, operation, and backup
instructions.

It can share the existing Venvi Droplet: Venvi retains its web port, while this
bot exposes no ports and has explicit CPU and memory limits.

The old `@reboot` crontab approach is not recommended because it does not restart
the bot after a process crash and does not manage log growth.

## Files containing private data

Never share or commit these files:

- `.env`
- `.notification-bot.sqlite3`
- `notification-bot.log`
