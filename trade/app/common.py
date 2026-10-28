"""Shared by the engine and API Lambdas: Schwab access (via the published access-token object),
DynamoDB helpers, budget accounting.

Token contract: see projectknowledge.md ("Token contract"). We only ever READ
s3://TOKEN_BUCKET/schwab/access.json. We never hold a refresh token, never call the Schwab token
endpoint, never authorize. Stale tokens => raise TokenStale and let the UI say so.
"""
import os, json, time, math, datetime as dt, urllib.request, urllib.parse, urllib.error
from decimal import Decimal
from zoneinfo import ZoneInfo
import boto3

ET = ZoneInfo("America/New_York")
TABLE = os.environ.get("TABLE", "trade-demo")
TOKEN_BUCKET = os.environ.get("TOKEN_BUCKET", "")
TOKEN_KEY = os.environ.get("TOKEN_KEY", "schwab/access.json")
ACCOUNT_NUMBER = os.environ.get("ACCOUNT_NUMBER", "")
EXPIRATION = os.environ.get("EXPIRATION", "2026-11-06")
MAX_ORDER_USD = float(os.environ.get("MAX_ORDER_USD", "500"))
MAX_TOTAL_USD = float(os.environ.get("MAX_TOTAL_USD", "1500"))
# Optional: students see the bankroll shifted up by this much (so the real balance isn't revealed).
# Display only - every cap and size is computed from the real MAX_TOTAL_USD.
DISPLAY_OFFSET = float(os.environ.get("DISPLAY_OFFSET", "0"))
MAX_ACTIVE_ORDERS = int(os.environ.get("MAX_ACTIVE_ORDERS", "3"))
MAX_ORDERS_PER_DAY = int(os.environ.get("MAX_ORDERS_PER_DAY", "20"))

ddb = boto3.resource("dynamodb")
table = ddb.Table(TABLE)
ddbc = boto3.client("dynamodb")
s3 = boto3.client("s3")

# Schwab statuses after which an order can no longer fill (FILLED is "done" too, but keeps its cost).
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}
UNFILLABLE = {"CANCELED", "REJECTED", "EXPIRED", "REPLACED"}
CANCELABLE = {"WORKING", "QUEUED", "ACCEPTED", "PENDING_ACTIVATION", "AWAITING_PARENT_ORDER",
              "AWAITING_CONDITION", "AWAITING_MANUAL_REVIEW", "AWAITING_UR_OUT", "NEW",
              "AWAITING_STOP_CONDITION", "AWAITING_RELEASE_TIME", "PENDING_ACKNOWLEDGEMENT",
              "PENDING_RECALL", "UNKNOWN"}


# Static exclusions on top of busy_underlyings(), e.g. a symbol whose short option expires soon and whose
# assignment hasn't settled yet: {"XYZ": dt.datetime(2026, 11, 2, 9, 30, tzinfo=ET)}
EXCLUDE_UNTIL = {}


def excluded(sym):
    t = EXCLUDE_UNTIL.get(sym)
    return bool(t and now_et() < t)


class TokenStale(Exception):
    pass


class SchwabError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Schwab HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


# ---------------------------------------------------------------- dynamo helpers
def todb(o):
    return json.loads(json.dumps(o), parse_float=Decimal)


def fromdb(o):
    if isinstance(o, list):
        return [fromdb(x) for x in o]
    if isinstance(o, dict):
        return {k: fromdb(v) for k, v in o.items()}
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    return o


def get(pk, sk):
    return fromdb(table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item"))


def put(pk, sk, **fields):
    table.put_item(Item=todb({"pk": pk, "sk": sk, **fields}))


def query(pk):
    out, kw = [], {"KeyConditionExpression": "pk = :p", "ExpressionAttributeValues": {":p": pk}}
    while True:
        r = table.query(**kw)
        out += r["Items"]
        if "LastEvaluatedKey" not in r:
            return fromdb(out)
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def config():
    c = get("CONFIG", "main") or {}
    return {"demo_mode": c.get("demo_mode", True), "demo_discount": c.get("demo_discount", 0.10)}


def now_et():
    return dt.datetime.now(ET)


def market_open(t=None):
    t = t or now_et()
    if t.weekday() >= 5:
        return False
    m = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= m < 16 * 60


# ---------------------------------------------------------------- Schwab
_tok = {"doc": None}


def _tokens(force=False):
    d, now = _tok["doc"], time.time()
    if force or not d or min(a["expires_at"] for a in d["apps"].values()) - now < 120:
        try:
            body = s3.get_object(Bucket=TOKEN_BUCKET, Key=TOKEN_KEY)["Body"].read()
        except Exception as e:
            raise TokenStale(f"cannot read published Schwab access tokens ({type(e).__name__})")
        d = _tok["doc"] = json.loads(body)
    for app, a in d["apps"].items():
        if a["expires_at"] - now < 30:
            raise TokenStale(f"published Schwab '{app}' access token expired "
                             f"{int(now - a['expires_at'])}s ago - the token publisher appears to be down")
    return d


def schwab(method, path, app="marketdata", params=None, body=None, _retry=True):
    """Returns (headers, json). path starts with /marketdata/v1 or /trader/v1."""
    d = _tokens()
    url = d["api_base"] + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Authorization": "Bearer " + d["apps"][app]["access_token"], "Accept": "application/json"}
    if data is not None:
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode()
            return dict(r.headers), (json.loads(txt) if txt.strip() else None)
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        if e.code == 401 and _retry:      # publisher may have just rotated the access token
            _tokens(force=True)
            return schwab(method, path, app, params, body, _retry=False)
        raise SchwabError(e.code, txt)


def quotes(symbols, fields="quote,fundamental"):
    out = {}
    syms = list(dict.fromkeys(symbols))
    for i in range(0, len(syms), 150):
        _, j = schwab("GET", "/marketdata/v1/quotes",
                      params={"symbols": ",".join(syms[i:i + 150]), "fields": fields, "indicative": "false"})
        out.update(j or {})
    return out


_hash = {"v": None}


def account_hash():
    if not _hash["v"]:
        _, j = schwab("GET", "/trader/v1/accounts/accountNumbers", app="trading")
        for a in j:
            if a["accountNumber"] == ACCOUNT_NUMBER:
                _hash["v"] = a["hashValue"]
        if not _hash["v"]:
            raise RuntimeError("configured Schwab account not visible to the trading token")
    return _hash["v"]


def get_order(order_id):
    _, j = schwab("GET", f"/trader/v1/accounts/{account_hash()}/orders/{order_id}", app="trading")
    return j


def place_order(order):
    h, _ = schwab("POST", f"/trader/v1/accounts/{account_hash()}/orders", app="trading", body=order)
    loc = h.get("Location") or h.get("location") or ""
    oid = loc.rstrip("/").split("/")[-1]
    if not oid.isdigit():
        raise RuntimeError("Schwab accepted the order but returned no order id")
    return oid


def spendable_cash():
    """Schwab availableFunds minus the unfilled part of OUR working orders (availableFunds does not net
    out open orders). Only the result is used; the balance is never sent to the browser."""
    _, j = schwab("GET", f"/trader/v1/accounts/{account_hash()}", app="trading")
    acct = j["securitiesAccount"]
    avail = float(acct["currentBalances"]["availableFunds"])
    working = 0.0
    for o in query("ORDER"):
        if o.get("status") not in TERMINAL and float(o["qty"]):
            working += float(o["cost"]) * max(0.0, float(o["qty"]) - float(o.get("filled_qty") or 0)) / float(o["qty"])
    # projectedBalances already nets out ALL open orders (with Schwab's own hold amounts, which run higher than
    # limit x qty) and is what Schwab checks at placement - 10/28 XOM/SOFI were rejected when only the
    # current-minus-ours figure was used. Take whichever is tighter.
    projected = (acct.get("projectedBalances") or {}).get("availableFunds")
    return min(avail - working, float(projected)) if projected is not None else avail - working


def busy_underlyings():
    """Every symbol the account already touches: any position (shares or options, long or short) and any
    non-terminal order, by equity symbol or option underlying. The demo stays out of all of them, so another
    position's short option can never be assigned against shares or legs the demo owns, and vice versa.
    Includes the demo's own positions/orders. Used internally only; never sent to the browser."""
    h = account_hash()
    _, j = schwab("GET", f"/trader/v1/accounts/{h}", app="trading", params={"fields": "positions"})
    out = set()

    def add(inst):
        out.add(inst.get("underlyingSymbol") or inst.get("symbol", "").split()[0])
    for p in j["securitiesAccount"].get("positions") or []:
        add(p["instrument"])
    now = dt.datetime.now(dt.timezone.utc)
    _, orders = schwab("GET", f"/trader/v1/accounts/{h}/orders", app="trading", params={
        "fromEnteredTime": (now - dt.timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "toEnteredTime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "maxResults": 3000})
    for o in orders or []:
        if o.get("status") not in TERMINAL:
            for leg in o.get("orderLegCollection") or []:
                add(leg["instrument"])
    out.discard("")
    return out


def div_before(fundamental, expiration, buffer_days=7):
    """True if an ex-dividend date (declared, or Schwab's estimate) falls between today and expiration + buffer.
    That's the situation in which a short call gets exercised early; the buffer covers estimate drift."""
    today = now_et().date()
    limit = dt.date.fromisoformat(expiration) + dt.timedelta(days=buffer_days)
    for k in ("divExDate", "nextDivExDate"):
        v = (fundamental or {}).get(k)
        if v:
            d = dt.date.fromisoformat(v[:10])
            if today <= d <= limit:
                return True
    return False


def cancel_order(order_id):
    schwab("DELETE", f"/trader/v1/accounts/{account_hash()}/orders/{order_id}", app="trading")


# ---------------------------------------------------------------- orders bookkeeping
def summarize_schwab_order(o):
    """Only the fields we show. Never copy account numbers or anything else from the payload."""
    # Net average fill = sum(sign * price * qty) over execution legs / filled quantity. Works for a
    # single stock leg and for a vertical (each filled spread fills one unit of each leg).
    sign = {l.get("legId"): (1 if l.get("instruction", "").startswith("BUY") else -1)
            for l in o.get("orderLegCollection") or []}
    value = 0.0
    for act in o.get("orderActivityCollection") or []:
        for leg in act.get("executionLegs") or []:
            value += sign.get(leg.get("legId"), 1) * float(leg.get("price") or 0) * float(leg.get("quantity") or 0)
    filled = float(o.get("filledQuantity") or 0)
    return {"status": o.get("status", "UNKNOWN"),
            "filled_qty": filled,
            "avg_fill": round(value / filled, 4) if filled else None,
            "remaining_qty": float(o.get("remainingQuantity") or 0),
            "close_time": o.get("closeTime"),
            "status_desc": (o.get("statusDescription") or "")[:200]}


def refresh_order(item):
    """Pull status of one of OUR orders from Schwab, persist it, release budget if it can no longer fill."""
    s = summarize_schwab_order(get_order(item["order_id"]))
    table.update_item(Key={"pk": "ORDER", "sk": item["sk"]},
                      UpdateExpression="SET #s=:s, filled_qty=:f, avg_fill=:a, remaining_qty=:r, status_desc=:d, synced_at=:t",
                      ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues=todb({":s": s["status"], ":f": s["filled_qty"], ":a": s["avg_fill"],
                                                      ":r": s["remaining_qty"], ":d": s["status_desc"],
                                                      ":t": int(time.time())}))
    item.update(s)
    if s["status"] in UNFILLABLE:
        release(item)
    return item


def release(item):
    """Give back the unfilled part of an order's reserved cash, exactly once."""
    qty = float(item["qty"])
    unfilled = max(0.0, qty - float(item.get("filled_qty") or 0))
    give = round(float(item["cost"]) * unfilled / qty, 2) if qty else 0
    try:
        ddbc.transact_write_items(TransactItems=[
            {"Update": {"TableName": TABLE, "Key": {"pk": {"S": "ORDER"}, "sk": {"S": item["sk"]}},
                        "UpdateExpression": "SET released = :t",
                        "ConditionExpression": "attribute_not_exists(released) OR released = :f",
                        "ExpressionAttributeValues": {":t": {"BOOL": True}, ":f": {"BOOL": False}}}},
            {"Update": {"TableName": TABLE, "Key": {"pk": {"S": "BUDGET"}, "sk": {"S": "main"}},
                        "UpdateExpression": "SET committed = committed - :g, active = active - :a, ver = ver + :one",
                        "ExpressionAttributeValues": {":g": {"N": str(give)},
                                                      ":a": {"N": "1" if unfilled == qty else "0"},
                                                      ":one": {"N": "1"}}}},
        ])
    except ddbc.exceptions.TransactionCanceledException:
        pass   # already released


def order_cap(b):
    """Most one new order may cost: $500, or less once open/filled app orders leave under $500 of the bankroll."""
    return round(max(0.0, min(MAX_ORDER_USD, MAX_TOTAL_USD - float(b["committed"]))), 2)


def budget():
    b = get("BUDGET", "main")
    if not b:
        try:
            table.put_item(Item={"pk": "BUDGET", "sk": "main", "committed": Decimal(0), "active": 0,
                                 "ver": 0, "day": "", "placed_today": 0},
                           ConditionExpression="attribute_not_exists(pk)")
        except ddbc.exceptions.ConditionalCheckFailedException:
            pass
        b = get("BUDGET", "main")
    return b
