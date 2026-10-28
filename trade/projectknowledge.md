# Project knowledge: AI Trade Desk (Schwab + Amazon Bedrock)

Give this file to your coding assistant (Claude Code, Cursor, GitHub Copilot, ...) together with this repository.
It covers everything needed to build and run your own copy: registering a Schwab developer app, getting OAuth
tokens, setting up AWS, and how the application works and why. Steps marked **(you)** need a human at a browser
or a decision only you can make. Your assistant can do the rest with you.

> **Real money.** This app places real orders in a real brokerage account. Use a small bankroll you can afford
> to lose, start in demo mode (limit prices 10% below market, so orders rarely fill), and read the guard rails
> section before you go live. Nothing here is investment advice.

---

## 0. What you are building

A public, no-login web page. Every minute it shows:

- **Top 5 stocks** to buy now and sell at a profit later this week.
- **Top 5 bull call debit spreads** for one chosen option expiration (a Friday).
- The app's own **placed / filled orders** (with a Cancel button), and the list of **rejected** ideas.

Each idea has **Accept** and **Reject**:

- **Reject** removes it permanently for everyone; the next-ranked idea moves up.
- **Accept** re-prices it from a fresh quote, sizes the quantity as close to the per-order cap as possible
  without going over, and places a real Schwab **limit** order.
- If two people click the same card, the first click wins. A stale click is refused with "refresh the page".

```
 Your laptop (token publisher)                     AWS us-west-2
 -----------------------------                     -------------
 refresh token (7-day life)  -- never leaves -->   x
   | every 5 min: refresh -> 30-min access token
   v
 s3://schwab-demo-tokens-<acct>/schwab/access.json (SSE-KMS) --> Lambda "engine" (EventBridge, every minute)
                                                                   - Schwab quotes, price history, option chains
                                                                   - quant shortlist -> Claude on Bedrock ranks it
                                                                   - writes recommendations to DynamoDB
                                                               --> Lambda "api" (API Gateway HTTP API)
                                                                   - serves portal.html and /api/*
                                                                   - Accept -> Schwab order; Reject; Cancel
```

Repository layout:

| Path | What |
|---|---|
| `config.example.py` | all names and limits; copy to `config.py` (git-ignored) |
| `deploy.py` | idempotent boto3 deploy: table, role, Lambdas, schedule, HTTP API, optional domain |
| `teardown.py` | removes what deploy.py created; leaves the token side and your orders alone |
| `set_mode.py` | `demo` (limit 10% under) / `live` (limit at the current price) |
| `app/common.py` | token reader, Schwab HTTP helper, budget accounting, order bookkeeping, safety checks |
| `app/engine.py` | the minute job: sync orders, compute features, build spreads, ask Claude, publish rankings |
| `app/api.py` | the public API and the accept / reject / cancel logic |
| `app/portal.html` | the single-page UI (vanilla JS, light theme, Methodology dialog) |

---

## 1. Prerequisites

- A **Charles Schwab brokerage account** (a cash account is simplest; a Roth IRA works too but cannot
  short or use margin). Approval for **options trading** is needed for the spreads. Spreads need at least
  Level 2 at Schwab; check your account's options approval level.
- An **AWS account** with permission to create IAM roles, Lambda, DynamoDB, API Gateway, EventBridge,
  S3, KMS (and optionally ACM and Route 53).
- **Python 3.11+**, `pip install boto3 requests`, and the AWS CLI configured (`aws configure`).
- Optional: a domain in a Route 53 hosted zone for a friendly URL. Without one, the app is served at the
  API Gateway URL that `deploy.py` prints.

---

## 2. Register a Schwab developer app (you)

Schwab's developer portal is separate from your brokerage login. Wording on the site changes from time to time;
if a button name differs, look for the closest match.

1. Go to **https://developer.schwab.com** and click **Register**. Create a developer account with your
   email address, then confirm the verification email. (This is *not* your schwab.com login, although you may use
   the same email.)
2. Sign in. Open your **profile / dashboard** and make sure your role is **Individual Developer**.
3. Go to **API Products** and request access to **Trader API – Individual**. This covers both
   *Accounts and Trading* and *Market Data*. Approval can take one to several business days. Start early.
4. When access is granted, go to **Dashboard → Apps → Create App** and fill in:
   - **API Product:** select both **Accounts and Trading Production** and **Market Data Production**.
   - **App name:** anything, e.g. `my-trade-desk`.
   - **Callback URL:** `https://127.0.0.1` (must be `https`; it must match *exactly*, character for character,
     what you send later. No trailing slash unless you register one).
   - **Order limit:** the default (e.g. 120 orders/minute) is fine.
5. Submit. The app's status shows **Approved – Pending** at first. Wait until it says **Ready For Use**
   (often a few hours to a few days). Tokens requested before that will fail.
6. Open the app and copy the **App Key** (this is the OAuth `client_id`) and the **Secret** (the
   `client_secret`). Store them in a local file that is never committed, e.g. `~/.schwab/schwab.env`:

   ```
   SCHWAB_APP_KEY=...
   SCHWAB_APP_SECRET=...
   SCHWAB_CALLBACK_URL=https://127.0.0.1
   ```
   `chmod 600 ~/.schwab/schwab.env`

One app with both products is enough. This repo's token file has two slots (`marketdata` and `trading`) because
the original deployment used two registrations to spread rate limits; with one app, publish the same access token
in both slots.

---

## 3. Get the bearer (access) and refresh tokens (you, about 2 minutes)

Schwab uses OAuth 2 authorization-code flow:

| Token | Lifetime | How you get a new one |
|---|---|---|
| **Access token** (the "bearer" token sent as `Authorization: Bearer ...`) | **30 minutes** | refresh with the refresh token; fully scriptable |
| **Refresh token** | **7 days**, counted from the browser login; refreshing does **not** extend it | a human logs in through the browser again, at least weekly |

### 3.1 Authorize in the browser

Open this URL (replace `APP_KEY`):

```
https://api.schwabapi.com/v1/oauth/authorize?client_id=APP_KEY&redirect_uri=https://127.0.0.1&response_type=code
```

1. Log in with your **schwab.com brokerage** username and password (not the developer-portal login).
2. Accept the terms and **select the account(s)** the app may access. Select the one you will trade in.
3. Schwab redirects to `https://127.0.0.1/?code=C0.b2F...%40&session=...`. The browser shows **"This site
   can't be reached"**. That is expected. **Copy the full URL from the address bar.**
4. The `code` is valid for only about **30 seconds**, so do step 3.2 right away. If it fails, simply repeat 3.1.

### 3.2 Exchange the code for tokens

The code in the URL is URL-encoded (it ends in `%40`, an `@`). Decode it before sending.

```bash
CODE=$(python3 -c 'import sys,urllib.parse as u; print(u.parse_qs(u.urlparse(sys.argv[1]).query)["code"][0])' "PASTE_FULL_URL")
curl -s -X POST https://api.schwabapi.com/v1/oauth/token \
  -u "$SCHWAB_APP_KEY:$SCHWAB_APP_SECRET" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode grant_type=authorization_code \
  --data-urlencode "code=$CODE" \
  --data-urlencode redirect_uri=https://127.0.0.1 > ~/.schwab/tokens.json
chmod 600 ~/.schwab/tokens.json
```

(`-u` sends `Authorization: Basic base64(APP_KEY:APP_SECRET)`.) The response contains `access_token`,
`refresh_token`, `expires_in` (1800), `token_type` ("Bearer"), `scope` ("api") and `id_token`.

### 3.3 Refresh the access token (scriptable)

```bash
curl -s -X POST https://api.schwabapi.com/v1/oauth/token \
  -u "$SCHWAB_APP_KEY:$SCHWAB_APP_SECRET" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode grant_type=refresh_token \
  --data-urlencode "refresh_token=$(jq -r .refresh_token ~/.schwab/tokens.json)"
```

### 3.4 Smoke test

```bash
AT=$(jq -r .access_token ~/.schwab/tokens.json)
curl -s -H "Authorization: Bearer $AT" "https://api.schwabapi.com/marketdata/v1/quotes?symbols=SPY" | jq '.SPY.quote.lastPrice'
curl -s -H "Authorization: Bearer $AT" https://api.schwabapi.com/trader/v1/accounts/accountNumbers
```
The second call returns `[{"accountNumber": "...", "hashValue": "..."}]`. Every `/trader/v1/accounts/...` call
uses the **hashValue**, never the plain account number.

### 3.5 Token rules that will save you days

- **One Schwab app registration supports exactly one live refresh token.** Every browser authorization
  mints a new refresh token and **revokes** the previous one. If two programs (or two classmates) share an App
  Key and each logs in, they keep killing each other's tokens. Give every program that does its own login
  its own app registration.
- **Refreshing is harmless**: it returns the same refresh token and does not consume it. Re-*authorizing*
  is what revokes.
- **The refresh token expires 7 days after the browser login**, no matter how often you refresh. Put a
  weekly reminder on your calendar to repeat step 3.
- **Never commit** the App Secret or tokens. This repo's `.gitignore` excludes `*.env`, `.secrets/`,
  `schwab_tokens*.json` and `access.json`, but keep them outside the repo anyway (`~/.schwab/`).

---

## 4. Token publisher and the AWS "token side"

The cloud app **never holds a refresh token**. A small script on your own machine refreshes the 30-minute access
token and publishes **only the access token** to one encrypted S3 object every 5 minutes. If your laptop is off,
the app stops trading within 30 minutes and says so. That is a safety feature: you can cut the cloud off at any
time, and a leaked cloud credential is worth 30 minutes at most.

### 4.1 Create the token side in AWS (once)

```bash
REGION=us-west-2; ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=schwab-demo-tokens-$ACCT
KEY_ID=$(aws kms create-key --region $REGION --description "schwab demo tokens" --query KeyMetadata.KeyId --output text)
aws kms create-alias --region $REGION --alias-name alias/schwab-demo --target-key-id $KEY_ID
aws s3api create-bucket --region $REGION --bucket $BUCKET --create-bucket-configuration LocationConstraint=$REGION
aws s3api put-public-access-block --bucket $BUCKET --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket $BUCKET --server-side-encryption-configuration \
  "{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"aws:kms\",\"KMSMasterKeyID\":\"$KEY_ID\"}}]}"
```
Leave bucket versioning **off** (the object is overwritten every 5 minutes).

Create a **publisher IAM user** whose only power is writing that one object:

```json
{ "Version": "2012-10-17", "Statement": [
  { "Effect": "Allow", "Action": "s3:PutObject",
    "Resource": "arn:aws:s3:::schwab-demo-tokens-<ACCT>/schwab/access.json" },
  { "Effect": "Allow", "Action": ["kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"],
    "Resource": "arn:aws:kms:us-west-2:<ACCT>:key/<KEY_ID>" } ] }
```
```bash
aws iam create-user --user-name schwab-publisher
aws iam put-user-policy --user-name schwab-publisher --policy-name publish --policy-document file://publisher-policy.json
aws iam create-access-key --user-name schwab-publisher   # save into a local profile: aws configure --profile schwab-publisher
```

`deploy.py` gives the app's Lambda role the mirror-image permission: `s3:GetObject` on that one object and
`kms:Decrypt` on that one key.

### 4.2 The token contract (what the app reads)

`s3://schwab-demo-tokens-<ACCT>/schwab/access.json`:

```json
{
  "published_at": 1793125563,
  "api_base": "https://api.schwabapi.com",
  "apps": {
    "marketdata": { "access_token": "...", "token_type": "Bearer", "expires_at": 1793127156 },
    "trading":    { "access_token": "...", "token_type": "Bearer", "expires_at": 1793127127 }
  }
}
```
`expires_at` is Unix seconds (UTC). The app re-reads the object when a token has under 2 minutes left, and once
after any HTTP 401. If a token is already expired it **fails loudly** ("token publisher appears to be down");
it never tries to mint its own.

### 4.3 Publisher (reference; have your assistant adapt and review it)

`~/.schwab/publish.py`, run by cron every 5 minutes (`*/5 * * * * python3 ~/.schwab/publish.py`):

```python
import json, time, base64, pathlib, requests, boto3
D = pathlib.Path.home() / ".schwab"
env = dict(l.split("=", 1) for l in (D / "schwab.env").read_text().split() if "=" in l)
tok = json.loads((D / "tokens.json").read_text())

if time.time() > tok.get("access_expires_at", 0) - 10 * 60:          # refresh when < 10 min left
    r = requests.post("https://api.schwabapi.com/v1/oauth/token", timeout=30,
                      auth=(env["SCHWAB_APP_KEY"], env["SCHWAB_APP_SECRET"]),
                      data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    r.raise_for_status()                    # 400/401 here = refresh token expired: redo section 3 in a browser
    new = r.json()
    tok.update(access_token=new["access_token"], access_expires_at=int(time.time()) + new["expires_in"] - 30)
    if new.get("refresh_token"):
        tok["refresh_token"] = new["refresh_token"]
    (D / "tokens.json").write_text(json.dumps(tok)); (D / "tokens.json").chmod(0o600)

a = {"access_token": tok["access_token"], "token_type": "Bearer", "expires_at": tok["access_expires_at"]}
doc = {"published_at": int(time.time()), "api_base": "https://api.schwabapi.com",
       "apps": {"marketdata": a, "trading": a}}
body = json.dumps(doc)
for secret in (tok["refresh_token"], env["SCHWAB_APP_KEY"], env["SCHWAB_APP_SECRET"]):
    assert secret not in body, "refusing to publish a long-lived secret"
boto3.Session(profile_name="schwab-publisher").client("s3", region_name="us-west-2").put_object(
    Bucket=f"schwab-demo-tokens-{env['AWS_ACCOUNT_ID']}", Key="schwab/access.json", Body=body,
    ServerSideEncryption="aws:kms", SSEKMSKeyId="alias/schwab-demo", ContentType="application/json")
```
(Add `AWS_ACCOUNT_ID=...` to `schwab.env`, and after step 3.2 add `access_expires_at` to tokens.json, or just
let the first run refresh.) Every published token has at least 20 minutes of life.

**Kill switch:** remove `kms:Decrypt` from the app role (or stop the cron). The app goes dark within 30 minutes
without touching Schwab.

*Simpler alternative, with a tradeoff:* store the refresh token in AWS Secrets Manager and refresh from a Lambda.
It works, but then a cloud compromise exposes a 7-day credential that can trade, and Secrets Manager is a poor fit
for a value that changes every few minutes. The publisher design above is recommended.

---

## 5. Amazon Bedrock (you, once)

1. In the AWS console (us-west-2) open **Amazon Bedrock → Model access** and enable an Anthropic Claude model.
2. Use its **cross-region inference profile id**, e.g. `us.anthropic.claude-sonnet-4-6`, as `MODEL_ID` in
   `config.py`. Check that it works:
   ```bash
   aws bedrock-runtime converse --region us-west-2 --model-id us.anthropic.claude-sonnet-4-6 \
     --messages '[{"role":"user","content":[{"text":"say ok"}]}]'
   ```
   An `AccessDeniedException` means that model isn't enabled for your account; pick one that is.
3. Cost: the engine calls Claude every minute during market hours (~390 calls/day, roughly $10–20/day with
   Sonnet; a Haiku model is far cheaper) and every 30 minutes otherwise. **Run `teardown.py` after class.**

---

## 6. Configure, deploy, operate

```bash
cp config.example.py config.py
```
Fill in `ACCOUNT_ID`, `SCHWAB_ACCOUNT_NUMBER` (the plain number from `accountNumbers`), `EXPIRATION` (an
upcoming Friday with listed options), the money limits, and optionally `DOMAIN` + `HOSTED_ZONE_ID`. Then:

```bash
python deploy.py            # first time: everything (about 2 minutes, plus certificate validation if DOMAIN set)
python deploy.py --code     # after editing anything under app/
python set_mode.py          # shows demo/live; new deployments start in DEMO mode
python set_mode.py live     # only when you are ready to trade at real prices
python teardown.py          # cancel your open app orders in the portal first
```

Logs: CloudWatch log groups `/aws/lambda/trade-demo-engine` and `/aws/lambda/trade-demo-api`.

---

## 7. How the app works

### 7.1 Engine (every minute)

1. **Sync orders.** For every order the app placed, `GET /trader/v1/accounts/{hash}/orders/{id}`, store
   status / filled quantity / average fill price, and release budget for orders that can no longer fill.
2. **Universe.** About 75 liquid stocks and ETFs, minus anything rejected or already accepted, minus every
   symbol the account already holds or has a working order in (`busy_underlyings()`, see 7.4).
3. **Features** from daily price history (cached in DynamoDB per day) plus live quotes: SMA20/SMA50 trend,
   RSI(2) pullback, RSI(14), ATR % (enough range to move in 3 days, not a coin flip), relative volume,
   relative strength vs SPY, 20-day high breakout. These give a **bull score** per name.
4. **Spreads.** For the strongest ~14 names (skipping any with an ex-dividend date before expiration + 7 days),
   pull the option chain for `EXPIRATION` and pick the best **bull call vertical**: long leg delta 0.38–0.68,
   debit 25–62% of the strike width, total cost ≤ $480, tight bid/ask and open interest filters. Score by
   probability (from deltas) × payoff, i.e. expected value.
5. **Claude** (Bedrock Converse) receives the market tape (SPY, QQQ, VIX, sector ETFs) and the quant shortlist
   and returns strict JSON: a market view, then ranked stocks and spreads, each with a thesis, target, stop and
   a HIGH/MEDIUM/LOW confidence. If Claude fails or returns bad JSON, the quant ranking is used unchanged.
6. Write `RECS/stock`, `RECS/spread` and `STATUS/engine`. The UI shows the top 5 of each that nobody has
   decided on yet, so a reject immediately promotes #6.

### 7.2 API

- `GET /` → portal.html. `GET /api/state` → recommendations (each with a suggested quantity and limit), the
  app's own orders, rejected list, budget, mode, engine status.
- `POST /api/reject {key, sig}` → conditional put of `DECISION/<key>`; fails if someone already decided it.
- `POST /api/accept {key, sig}`:
  1. The card's `sig` must match the current recommendation (otherwise: "this changed, refresh").
  2. Re-check safety live: not busy, not excluded, spreads must be CALL spreads with no ex-dividend before expiry.
  3. `cap = min(per-order cap, MAX_TOTAL − committed, Schwab projectedBalances.availableFunds)`.
  4. Fresh quote → limit price (demo mode: 10% under) → quantity = floor(cap / unit cost).
  5. One DynamoDB transaction claims the decision **and** reserves budget (optimistic `ver` check).
  6. `POST /trader/v1/accounts/{hash}/orders`; the new order id is the last segment of the `Location`
     response header. If placement fails, the reservation is rolled back.
- `POST /api/cancel {order_id}` → only ids the app itself created (looked up in DynamoDB).

### 7.3 DynamoDB single table (`pk`, `sk`)

| Item | Holds |
|---|---|
| `CONFIG/main` | `demo_mode`, `demo_discount` |
| `BUDGET/main` | `committed`, `active`, `placed_today`, `day`, `ver` (optimistic lock) |
| `RECS/stock`, `RECS/spread` | ranked lists from the engine |
| `DECISION/<key>` | accepted / rejected, so it never comes back |
| `ORDER/<schwab id>` | the app's orders: qty, limit, cost, status, filled qty, avg fill |
| `STATUS/engine`, `LLM/last`, `HIST/<sym>` | heartbeat, last Claude output, daily bars cache |

### 7.4 Guard rails (keep them)

- **Limit orders only**, `duration: DAY`, `session: NORMAL`. Never MARKET, not even in error paths.
- **Only touch orders the app created.** Never cancel-all; never cancel-and-replace to reprice.
- **Never send account data to the browser**: no account number, hash, balance, or other positions/orders.
  The browser only sees the app's own orders and the configured budget (plus optional `DISPLAY_OFFSET`).
- **Budget**: per order ≤ `MAX_ORDER_USD`; the total of open + filled ≤ `MAX_TOTAL_USD`; at most
  `MAX_ACTIVE_ORDERS` at once and `MAX_ORDERS_PER_DAY`. Use `projectedBalances.availableFunds`, which nets
  Schwab's own holds for open orders. `currentBalances.availableFunds` does not, and using it caused
  "not enough buying power" rejections.
- **Stay out of busy symbols.** If the account holds a short call in XYZ (from any other strategy) and it is
  assigned, Schwab delivers XYZ shares from whatever is in the account, including shares this app bought.
  The app therefore excludes any symbol with any position or working order, checked every minute and again at
  click time.
- **Avoid early assignment of the app's own spreads**: bull call debit spreads only (a deep in-the-money short
  put can be assigned any day), and no ex-dividend date before expiration + 7 days (calls are exercised early
  almost only to capture a dividend). American-style options can never be made 100% assignment-proof. For that,
  use European-style cash-settled index options (XSP/SPX).
- **Concurrency**: accept/reject use conditional writes; budget uses a version number in a transaction.
- Engine Lambda has reserved concurrency 1 and no retries, so runs never overlap.

---

## 8. Schwab API cheat sheet

Base `https://api.schwabapi.com`, header `Authorization: Bearer <access_token>`.

| Purpose | Call |
|---|---|
| Quotes (≤ ~150 symbols per call) | `GET /marketdata/v1/quotes?symbols=AAPL,MSFT&fields=quote,fundamental` (`fundamental` has `divExDate`, `nextDivExDate`) |
| Daily bars | `GET /marketdata/v1/pricehistory?symbol=AAPL&periodType=month&period=3&frequencyType=daily&frequency=1` |
| Option chain | `GET /marketdata/v1/chains?symbol=AAPL&contractType=CALL&fromDate=2026-11-06&toDate=2026-11-06&strikeCount=20&includeUnderlyingQuote=true` |
| Account hashes | `GET /trader/v1/accounts/accountNumbers` |
| Balances / positions | `GET /trader/v1/accounts/{hash}?fields=positions` |
| Place order | `POST /trader/v1/accounts/{hash}/orders` → 201, id in `Location` header |
| Order status | `GET /trader/v1/accounts/{hash}/orders/{id}` |
| List orders | `GET /trader/v1/accounts/{hash}/orders?fromEnteredTime=...Z&toEnteredTime=...Z` (both required, ≤ 60 days) |
| Cancel | `DELETE /trader/v1/accounts/{hash}/orders/{id}` |
| Dry run | `POST /trader/v1/accounts/{hash}/previewOrder` |

Stock limit buy:
```json
{"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE", "price": "187.25",
 "orderLegCollection": [{"instruction": "BUY", "quantity": 2, "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}}]}
```
Bull call debit vertical (option symbols come straight from the chain, e.g. `AAPL  261106C00250000`, with the
root padded to 6 characters):
```json
{"orderType": "NET_DEBIT", "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE",
 "complexOrderStrategyType": "VERTICAL", "price": "1.85",
 "orderLegCollection": [
   {"instruction": "BUY_TO_OPEN",  "quantity": 2, "instrument": {"symbol": "AAPL  261106C00250000", "assetType": "OPTION"}},
   {"instruction": "SELL_TO_OPEN", "quantity": 2, "instrument": {"symbol": "AAPL  261106C00255000", "assetType": "OPTION"}}]}
```
Gotchas: prices are strings with 2 decimals (stocks under $1 and some options use other tick sizes); a
201 without a `Location` id means you cannot track the order, so treat it as an error; order statuses that can
no longer fill are `CANCELED`, `REJECTED`, `EXPIRED`, `REPLACED` (and `FILLED` is final); the trading API
accepts orders outside market hours but `session: NORMAL` + `DAY` orders wait for the open.

---

## 9. Instructions for the AI coding assistant

When helping a student with this project:

1. **Never automate the browser login** (section 3.1), and never call the Schwab token endpoint from cloud code
   on a timer. Only the student's local publisher refreshes tokens.
2. **Never put a refresh token, App Key or App Secret in AWS**, in the repo, in logs, or in the browser.
3. **Never place MARKET orders.** Only manage orders whose ids the app recorded. Never cancel-all.
4. Keep every guard rail in section 7.4. If a change would weaken one, say so and ask first.
5. Keep demo mode as the default for new deployments. Switch to live only when the student asks.
6. If tokens are stale, report it ("token publisher down") instead of inventing another way to authenticate.
7. Ask the student before running `deploy.py`, `teardown.py`, `set_mode.py live`, or anything that places,
   cancels or previews an order against their account.
8. Never delete a `CDKToolkit` stack or AWS resources the project didn't create.
