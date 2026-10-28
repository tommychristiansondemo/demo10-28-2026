"""Public (anonymous) API + portal page for the AI Trade Desk.

GET  /             portal HTML
GET  /api/state    recommendations (top 5 each, rejected/accepted removed), app orders, rejections
POST /api/reject   {"key", "sig"}
POST /api/accept   {"key", "sig"}
POST /api/cancel   {"order_id"}

Guard rails enforced here, server-side, whatever the browser sends:
  * only orders this app created are ever shown, refreshed or cancelled (looked up in our table by id)
  * each order < MAX_ORDER_USD at its limit price; total reserved <= MAX_TOTAL_USD;
    <= MAX_ACTIVE_ORDERS live-or-filled orders; <= MAX_ORDERS_PER_DAY placements per day
  * a recommendation is claimed with a conditional write before any order is sent, so two
    students can never both accept, and an accept after another student's reject is refused
"""
import base64, json, math, time, pathlib, traceback
import common as C

HTML = (pathlib.Path(__file__).parent / "portal.html").read_text()
VISIBLE = 5


def resp(code, body, ctype="application/json"):
    return {"statusCode": code, "headers": {"Content-Type": ctype, "Cache-Control": "no-store"},
            "body": body if isinstance(body, str) else json.dumps(body)}


# ---------------------------------------------------------------- state
def visible(kind, decided):
    r = C.get("RECS", kind) or {"items": [], "generated_at": None}
    items = [i for i in r["items"] if i["key"] not in decided]
    return items[:VISIBLE], r.get("generated_at"), len(items)


def state():
    decisions = C.query("DECISION")
    decided = {d["sk"] for d in decisions}
    stocks, gen_s, n_s = visible("stock", decided)
    spreads, gen_p, n_p = visible("spread", decided)
    st = C.get("STATUS", "engine") or {}
    cfg = C.config()
    b = C.budget()
    cap = C.order_cap(b)
    disc = cfg["demo_discount"] if cfg["demo_mode"] else 0.0
    for r in stocks + spreads:
        mult = 1 if r["kind"] == "stock" else 100
        lim = limit_for(r["kind"], float(r["price"]), disc)
        q = size(lim, mult, cap)
        r["suggest"] = {"qty": q, "limit": lim, "cost": round(q * lim * mult, 2)}
    orders = sorted(C.query("ORDER"), key=lambda o: -o["placed_at"])
    keep = ("order_id", "key", "kind", "title", "legs", "qty", "limit", "ref_price", "cost", "placed_at",
            "status", "filled_qty", "avg_fill", "demo_mode", "status_desc", "synced_at")
    return {
        "now": int(time.time()),
        "mode": {"demo": cfg["demo_mode"], "discount": cfg["demo_discount"]},
        "limits": {"max_order": C.MAX_ORDER_USD, "max_total": round(C.MAX_TOTAL_USD + C.DISPLAY_OFFSET, 2), "max_active": C.MAX_ACTIVE_ORDERS,
                   },
        "budget": {"committed": round(float(b["committed"]), 2), "active": int(b["active"]),
                   "remaining": round(C.MAX_TOTAL_USD + C.DISPLAY_OFFSET - float(b["committed"]), 2)},
        "engine": {k: st.get(k) for k in ("ok", "generated_at", "market_open", "tape", "market_view", "llm_at",
                                          "llm_error", "model", "error", "error_at", "n_universe", "took")},
        "expiration": C.EXPIRATION,
        "stocks": stocks, "spreads": spreads, "remaining": {"stocks": n_s, "spreads": n_p},
        "generated_at": gen_s or gen_p,
        "orders": [dict({k: o.get(k) for k in keep}, cancelable=o.get("status") in C.CANCELABLE) for o in orders],
        "rejected": sorted([{"key": d["sk"], "title": d.get("title"), "at": d.get("at")}
                            for d in decisions if d.get("status") == "REJECTED"], key=lambda d: -(d["at"] or 0)),
    }


# ---------------------------------------------------------------- reject / accept
def find_rec(key):
    kind = "stock" if key.startswith("STK:") else "spread"
    r = C.get("RECS", kind) or {"items": []}
    return next((i for i in r["items"] if i["key"] == key), None)


def decided_msg(d, action):
    who = "rejected" if d.get("status") == "REJECTED" else "accepted"
    if action == "accept" and who == "rejected":
        return (f"Another student already REJECTED {d.get('title') or 'this trade'}, so your order will NOT be "
                f"submitted. You're looking at a stale list - please refresh the page.")
    if who == "accepted" or d.get("status") == "PENDING":
        return (f"Another student already ACCEPTED {d.get('title') or 'this trade'} - the order was placed once and "
                f"won't be duplicated. Please refresh the page.")
    return f"{d.get('title') or 'This trade'} was already rejected by another student. Please refresh the page."


def reject(body):
    key, sig = body.get("key", ""), body.get("sig", "")
    rec = find_rec(key)
    title = rec["title"] if rec else key
    try:
        C.table.put_item(Item=C.todb({"pk": "DECISION", "sk": key, "status": "REJECTED", "title": title,
                                      "at": int(time.time())}),
                         ConditionExpression="attribute_not_exists(sk)")
    except C.ddbc.exceptions.ConditionalCheckFailedException:
        return resp(409, {"ok": False, "stale": True, "message": decided_msg(C.get("DECISION", key), "reject")})
    return resp(200, {"ok": True, "message": f"Rejected {title}. The next-ranked idea moved up.", "state": state()})


def round_tick(x, tick, down=False):
    n = x / tick
    return round((math.floor(n + 1e-9) if down else round(n)) * tick, 2)


def limit_for(kind, ref, disc):
    """Limit price for a reference (current) price: as-is in live mode, `disc` under it in demo mode."""
    if kind == "stock":
        return round_tick(ref * (1 - disc), 0.01, down=True) if disc else round(ref, 2)
    tick = 0.05 if ref < 3 else 0.10
    return max(0.05, round_tick(ref * (1 - disc), tick, down=True)) if disc else max(0.05, round_tick(ref, tick))


def size(limit, mult, cap):
    """Largest quantity whose cost is as close to `cap` as possible without going over."""
    return math.floor((cap + 1e-9) / (limit * mult))


def price_order(rec, cfg, cap):
    """Fresh quote at click time -> (limit, qty, multiplier, ref_price, order json)."""
    disc = cfg["demo_discount"] if cfg["demo_mode"] else 0.0
    if rec["kind"] == "stock":
        q = C.quotes([rec["symbol"]], fields="quote")[rec["symbol"]]["quote"]
        ref = float(q.get("lastPrice") or q.get("mark"))
        mult = 1
    else:
        legs = rec["legs"]
        q = C.quotes([l["symbol"] for l in legs], fields="quote")
        mids = {}
        for l in legs:
            qq = q[l["symbol"]]["quote"]
            b, a = qq.get("bidPrice") or 0, qq.get("askPrice") or 0
            if b <= 0 or a <= 0:
                raise ValueError(f"no two-sided market right now for {l['symbol'].strip()}")
            mids[l["symbol"]] = (b + a) / 2
        ref = mids[legs[0]["symbol"]] - mids[legs[1]["symbol"]]
        if ref <= 0.05:
            raise ValueError("spread has no meaningful debit right now")
        mult = 100
    limit = limit_for(rec["kind"], ref, disc)
    qty = size(limit, mult, cap)
    if qty < 1:
        raise ValueError(f"one unit at ${limit * mult:,.2f} is more than the ${cap:,.2f} this order may use")
    if rec["kind"] == "stock":
        order = {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE",
                 "price": f"{limit:.2f}",
                 "orderLegCollection": [{"instruction": "BUY", "quantity": qty,
                                         "instrument": {"symbol": rec["symbol"], "assetType": "EQUITY"}}]}
    else:
        order = {"orderType": "NET_DEBIT", "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE",
                 "complexOrderStrategyType": "VERTICAL", "price": f"{limit:.2f}",
                 "orderLegCollection": [{"instruction": l["action"], "quantity": qty,
                                         "instrument": {"symbol": l["symbol"], "assetType": "OPTION"}}
                                        for l in rec["legs"]]}
    return limit, qty, mult, round(ref, 4), order


def reserve(key, title, cost):
    """Atomically claim the recommendation AND reserve budget. Returns None or an error response."""
    today = C.now_et().strftime("%Y-%m-%d")
    for _ in range(5):
        b = C.budget()
        placed = int(b["placed_today"]) if b.get("day") == today else 0
        if int(b["active"]) >= C.MAX_ACTIVE_ORDERS:
            return resp(409, {"ok": False, "message": f"The demo already has {C.MAX_ACTIVE_ORDERS} open or filled "
                              "orders (the class limit). Cancel an open order to free a slot."})
        if float(b["committed"]) + cost > C.MAX_TOTAL_USD + 1e-6:
            return resp(409, {"ok": False, "message": "Not enough of the class trading budget is left for this "
                              f"${cost:,.2f} order. Cancel an open order to free up money."})
        if placed >= C.MAX_ORDERS_PER_DAY:
            return resp(409, {"ok": False, "message": "Daily order limit for the demo reached."})
        try:
            C.ddbc.transact_write_items(TransactItems=[
                {"Put": {"TableName": C.TABLE, "ConditionExpression": "attribute_not_exists(sk)",
                         "Item": {"pk": {"S": "DECISION"}, "sk": {"S": key}, "status": {"S": "PENDING"},
                                  "title": {"S": title}, "at": {"N": str(int(time.time()))}}}},
                {"Update": {"TableName": C.TABLE, "Key": {"pk": {"S": "BUDGET"}, "sk": {"S": "main"}},
                            "ConditionExpression": "ver = :v",
                            "UpdateExpression": "SET committed = committed + :c, active = active + :one, "
                                                "ver = ver + :one, #d = :day, placed_today = :p",
                            "ExpressionAttributeNames": {"#d": "day"},
                            "ExpressionAttributeValues": {":v": {"N": str(b["ver"])}, ":c": {"N": f"{cost:.2f}"},
                                                          ":one": {"N": "1"}, ":day": {"S": today},
                                                          ":p": {"N": str(placed + 1)}}}},
            ])
            return None
        except C.ddbc.exceptions.TransactionCanceledException as e:
            reasons = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
            if reasons and reasons[0] == "ConditionalCheckFailed":
                return resp(409, {"ok": False, "stale": True,
                                  "message": decided_msg(C.get("DECISION", key) or {}, "accept")})
            time.sleep(0.1)   # budget version raced with another accept; re-read and retry
    return resp(503, {"ok": False, "message": "Busy - please try again."})


def unreserve(key, cost):
    C.ddbc.transact_write_items(TransactItems=[
        {"Delete": {"TableName": C.TABLE, "Key": {"pk": {"S": "DECISION"}, "sk": {"S": key}}}},
        {"Update": {"TableName": C.TABLE, "Key": {"pk": {"S": "BUDGET"}, "sk": {"S": "main"}},
                    "UpdateExpression": "SET committed = committed - :c, active = active - :one, ver = ver + :one",
                    "ExpressionAttributeValues": {":c": {"N": f"{cost:.2f}"}, ":one": {"N": "1"}}}},
    ])


def accept(body):
    key, sig = body.get("key", ""), body.get("sig", "")
    d = C.get("DECISION", key)
    if d:
        return resp(409, {"ok": False, "stale": True, "message": decided_msg(d, "accept")})
    rec = find_rec(key)
    if not rec:
        return resp(409, {"ok": False, "stale": True, "message": "That recommendation dropped out of the latest "
                          "ranking (the market moved). Your order will NOT be submitted - please refresh the page."})
    if rec.get("sig") != sig:
        return resp(409, {"ok": False, "stale": True, "message": "That recommendation was re-priced to different "
                          "strikes in the last refresh. Your order will NOT be submitted - please refresh and review."})
    blocked = f"{rec['symbol']} is temporarily off-limits for the demo (it would overlap another position in the " \
              "account and create option-assignment risk). Your order will NOT be submitted - please refresh."
    if C.excluded(rec["symbol"]):
        return resp(409, {"ok": False, "stale": True, "message": blocked})
    try:   # re-check live at click time: positions/orders may have changed since the last ranking
        if rec["symbol"] in C.busy_underlyings():
            return resp(409, {"ok": False, "stale": True, "message": blocked})
        if rec["kind"] == "spread":
            if rec.get("side") != "CALL":
                return resp(409, {"ok": False, "stale": True, "message": "Only bull call spreads are allowed now. "
                                  "Your order will NOT be submitted - please refresh."})
            f = C.quotes([rec["symbol"]], fields="fundamental")[rec["symbol"]].get("fundamental")
            if C.div_before(f, C.EXPIRATION):
                return resp(409, {"ok": False, "stale": True, "message": f"{rec['symbol']} goes ex-dividend before the "
                                  "spread expires (early-assignment risk). Your order will NOT be submitted - please refresh."})
    except C.TokenStale as e:
        return resp(503, {"ok": False, "message": f"Live Schwab connection is down ({e}). No order was sent."})
    except Exception as e:
        return resp(503, {"ok": False, "message": f"Couldn't verify assignment safety right now ({type(e).__name__}). No order was sent."})
    cfg = C.config()
    try:
        cash = C.spendable_cash()
    except C.TokenStale as e:
        return resp(503, {"ok": False, "message": f"Live Schwab connection is down ({e}). No order was sent."})
    except Exception as e:
        return resp(503, {"ok": False, "message": f"Couldn't check the account's cash right now ({type(e).__name__}). No order was sent."})
    cap = round(max(0.0, min(C.order_cap(C.budget()), cash)), 2)
    try:
        limit, qty, mult, ref, order = price_order(rec, cfg, cap)
    except C.TokenStale as e:
        return resp(503, {"ok": False, "message": f"Live Schwab connection is down ({e}). No order was sent."})
    except (ValueError, KeyError) as e:
        return resp(409, {"ok": False, "message": f"Couldn't size this trade right now: {e}. No order was sent."})
    cost = round(limit * qty * mult, 2)

    err = reserve(key, rec["title"], cost)
    if err:
        return err
    try:
        oid = C.place_order(order)
    except Exception as e:
        unreserve(key, cost)
        msg = e.body[:300] if isinstance(e, C.SchwabError) else str(e)[:300]
        print("ORDER FAILED", key, json.dumps(order), msg)
        return resp(502, {"ok": False, "message": f"Schwab rejected the order: {msg}"})

    item = {"pk": "ORDER", "sk": oid, "order_id": oid, "key": key, "kind": rec["kind"], "title": rec["title"],
            "legs": rec.get("legs"), "symbol": rec["symbol"], "qty": qty, "limit": limit, "ref_price": ref,
            "multiplier": mult, "cost": cost, "placed_at": int(time.time()), "status": "SUBMITTED",
            "filled_qty": 0, "demo_mode": cfg["demo_mode"], "released": False}
    C.table.put_item(Item=C.todb(item))
    C.table.update_item(Key={"pk": "DECISION", "sk": key}, UpdateExpression="SET #s = :s, order_id = :o",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":s": "ACCEPTED", ":o": oid})
    try:
        C.refresh_order(C.fromdb(item))
    except Exception as e:
        print("post-place refresh failed", e)
    unit = "shares" if mult == 1 else "spreads"
    note = (f" Demo mode: limit is {cfg['demo_discount']*100:.0f}% below the current {ref:.2f}, so it will likely "
            f"rest as an open order." if cfg["demo_mode"] else "")
    return resp(200, {"ok": True, "message": f"Order placed: BUY {qty} {unit} {rec['title']} @ {limit:.2f} limit "
                      f"(≈${cost:,.2f}).{note}", "state": state()})


def cancel(body):
    oid = str(body.get("order_id", ""))
    item = C.get("ORDER", oid) if oid.isdigit() else None
    if not item:
        return resp(404, {"ok": False, "message": "Unknown order."})     # never touches orders we didn't place
    try:
        item = C.refresh_order(item)
        if item["status"] not in C.CANCELABLE:
            return resp(409, {"ok": False, "message": f"Order is {item['status']} and can't be cancelled.",
                              "state": state()})
        C.cancel_order(oid)
        time.sleep(0.8)
        item = C.refresh_order(C.get("ORDER", oid))
    except C.TokenStale as e:
        return resp(503, {"ok": False, "message": f"Live Schwab connection is down ({e})."})
    except C.SchwabError as e:
        return resp(502, {"ok": False, "message": f"Schwab: {e.body[:200]}"})
    return resp(200, {"ok": True, "message": f"Cancel sent for {item['title']} (status now {item['status']}).",
                      "state": state()})


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    try:
        if method == "GET" and path in ("/", "/index.html"):
            return resp(200, HTML, "text/html; charset=utf-8")
        if method == "GET" and path == "/api/state":
            return resp(200, state())
        if method == "POST" and path in ("/api/accept", "/api/reject", "/api/cancel"):
            try:
                raw = event.get("body") or "{}"
                if event.get("isBase64Encoded"):
                    raw = base64.b64decode(raw).decode()
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return resp(400, {"ok": False, "message": "bad json"})
            return {"/api/accept": accept, "/api/reject": reject, "/api/cancel": cancel}[path](body)
        return resp(404, {"ok": False, "message": "not found"})
    except Exception as e:
        traceback.print_exc()
        return resp(500, {"ok": False, "message": f"Server error: {type(e).__name__}"})
