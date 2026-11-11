# Running turtlebot against an IBKR paper account

This guide takes you from a fresh clone to a bot trading on an Interactive Brokers **paper**
account. Plan about an hour, most of it in the TWS settings. If you only want to look around,
skip all of this and run the demo:

```bash
pip install -e .
turtlebot dashboard --demo        # http://127.0.0.1:8000, synthetic data, no account needed
```

> [!IMPORTANT]
> turtlebot only connects to paper accounts. It refuses any port other than the paper port 7497
> and any account id that does not start with the paper prefix (`D`). Keep it that way.

## 1. Paper account

1. Open an IBKR account (any entity). Every account comes with a paper-trading account; you find
   its login under *Settings → Account Settings → Paper Trading Account* in the client portal.
2. Optional but recommended: subscribe the live account to US equity market data. The paper
   account shares the live account's subscriptions. Without them TWS cannot deliver its own daily
   bars, and the bot then skips trailing a stop rather than trust a single data source (see
   *Checked price data* in the README).

## 2. TWS or IB Gateway

Install TWS (or the lighter IB Gateway) and log in with the **paper** credentials. Then, in TWS,
open *File → Global Configuration → API → Settings*:

| Setting | Value | Why |
|---|---|---|
| Enable ActiveX and Socket Clients | on | the bot talks to TWS over the socket API |
| Read-Only API | **off** | otherwise no orders can be placed (dry runs work either way) |
| Socket port | `7497` | the TWS paper default; on IB Gateway (default 4002) change it to 7497 |
| Allow connections from localhost only | on | the bot runs on the same machine |
| Trusted IPs | `127.0.0.1` | |
| Bypass Order Precautions for API Orders | on (*API → Precautions*) | otherwise a confirmation dialog blocks every order |

Under *Configure → Lock and Exit*, choose **auto restart** instead of auto log-off. TWS restarts
once a day; the bot treats the short disconnect as normal, reconnects with backoff and
reconciles before doing anything else.

## 3. Install and configure

```bash
git clone https://github.com/WenjaDebug/turtlebot.git && cd turtlebot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

`.env` is git-ignored. Every value in `.env.example` is the default, so you only change what
differs, typically:

```bash
TURTLE_BROKER__CLIENT_ID=11         # any id not used by another API client (21-29 are for dry runs)
TURTLE_TRADING_ENABLED=false        # leave false until the dry runs look right
```

The account size does not need configuring: positions are sized as a percentage of the net
liquidation that TWS reports.

## 4. Dry runs

Run these with TWS open, during or outside market hours:

```bash
turtlebot run --dry-run --reconcile-only   # read-only: compare the database with TWS
turtlebot run --dry-run --once             # plus one scan, still no orders
turtlebot report                           # what the bot believes it holds
```

A dry run uses its own client id (21-29) and its own database (`var/turtlebot_dryrun.db`), so it never
disturbs a running bot. Check that the reconciliation reports `ok` and that the scan finds
signals. `turtlebot audit-orders` lists every working order in the account that the bot does not
own, for example stops you placed by hand.

## 5. Go live on paper

```bash
TURTLE_TRADING_ENABLED=true turtlebot run
```

or set the flag in `.env`. Keep the process in its own terminal tab (on macOS, `caffeinate -i`
in front of the command stops the laptop from sleeping). The daily schedule, in New York time:

| Time | Job |
|---|---|
| 08:00 | build the universe (constituents, prices, liquidity and market-cap filters) |
| 09:00 | reconcile with TWS and trail the stops to the new 10-day low |
| 15:45 | scan and place market-on-close entries with attached stop orders |

The bot can be stopped at any time with Ctrl+C. Open positions stay protected by their GTC stops
at the broker. On the next start it reconciles first and catches up on a missed stop update.

```bash
turtlebot dashboard                  # http://127.0.0.1:8000, reads the same database
```

## 6. Exact exit prices

TWS only reports executions of the last few days through the API. If a stop fills while the bot is
offline for longer, the position is closed with an unknown exit price. To fill the gap, export the
trades from the IBKR portal, either as a Flex Query (*Reports → Flex Queries*, section *Trades*,
format CSV, with at least *Symbol, Buy/Sell, Quantity, TradePrice, DateTime*) or as an Activity
Statement in CSV, then:

```bash
turtlebot import-executions trades.csv          # preview the matches
turtlebot import-executions trades.csv --apply  # store the exit prices
```

Only positions whose sell executions add up to the full quantity are updated; everything else is
reported and left alone.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `connection refused` | TWS not running, API not enabled, or wrong port |
| `client id already in use` | another instance (or a dry run with the same id) is connected |
| Orders rejected with error 10349 or stuck behind a dialog | *Bypass Order Precautions* is off |
| Stop updates rejected with error 103 | the stop was placed by another client id; the bot reports it and leaves it alone |
| Reconciliation `degraded` after a restart | TWS was still syncing; nothing was changed, the next run usually clears it |
