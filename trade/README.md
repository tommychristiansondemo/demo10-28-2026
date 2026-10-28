# AI Trade Desk (class demo)

An anonymous web portal that, every minute, ranks the top 5 stock buys and the top 5 bull call debit spreads
(for one chosen expiration) using live Schwab market data and Claude on Amazon Bedrock. Students can accept a
recommendation, which places a **real** Schwab limit order, or reject it, which excludes it permanently.

> **This places real orders with real money.** It is a teaching demo, not investment advice. Use a small,
> separate bankroll you are prepared to lose.

To build your own copy (Schwab developer app, OAuth tokens, AWS setup), give **[projectknowledge.md](projectknowledge.md)**
to your coding assistant (Claude Code, Cursor, GitHub Copilot, ...) and work through it together.

| | |
|---|---|
| Portal | API Gateway HTTP API (optionally on a custom domain), us-west-2 |
| Engine | Lambda `trade-demo-engine`, EventBridge `rate(1 minute)`: syncs the app's orders, re-ranks |
| API | Lambda `trade-demo-api`: `/`, `/api/state`, `/api/accept`, `/api/reject`, `/api/cancel` |
| State | DynamoDB `trade-demo` (RECS, DECISION, ORDER, BUDGET, CONFIG, STATUS, HIST, LLM) |
| Schwab auth | reads 30-minute access tokens from one SSE-KMS S3 object written by a token publisher you run. The cloud never holds a refresh token. |

## Setup
```
cp config.example.py config.py      # then fill in your values (config.py is git-ignored)
pip install boto3
python deploy.py                    # everything (needs your token publisher running first)
```

## Commands
```
python set_mode.py            # show mode
python set_mode.py live       # limit = current price
python set_mode.py demo       # limit = 10% under (default)
python deploy.py --code       # redeploy code only
python teardown.py            # remove everything (token side is left alone; cancel open orders first)
```

## Guard rails (server-side)
- Each order is at most `MAX_ORDER_USD`, or whatever is left of `MAX_TOTAL_USD` after open and filled orders if
  that is less. Cards suggest the quantity that comes closest to that cap without going over.
- At most `MAX_ACTIVE_ORDERS` orders open or filled at once, and `MAX_ORDERS_PER_DAY` placed per day.
- Limit orders only. Only orders this app created are shown or cancelled.
- The app stays out of every symbol the account already holds or has a working order in, so it can't
  interact with your other positions. Spreads are bull calls only, with no ex-dividend date before expiry,
  to avoid early assignment.
- Accept and reject are claimed with DynamoDB conditional writes, so if a student accepts something another
  student already rejected, the order is refused with a "refresh" message.

## Cost
Claude runs every minute during market hours (~390 calls/day, roughly $10–20/day with Sonnet) and every
30 minutes otherwise. The rest is pennies. Run `teardown.py` when the class is over.
