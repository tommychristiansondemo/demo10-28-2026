"""Runs every minute (EventBridge). 1) sync status of the app's own orders, 2) re-rank opportunities.

Ranking idea (horizon: buy now, sell at a profit by Friday):
  * Short-horizon edges that are well documented for liquid US equities: buying short pullbacks
    inside an established uptrend (RSI(2) mean reversion), and buying breakouts to 20-day highs on
    above-normal volume with relative strength vs SPY. Names need enough daily range (ATR) to
    reach a target in ~3 sessions, but not so much that it's a coin flip.
  * Options: bull call debit verticals expiring EXPIRATION on the strongest names (no short puts and no
    ex-dividend date before expiry, to avoid early assignment), sized so one spread costs < $500,
    scored by probability-weighted payoff.
  * Claude (Bedrock) then reads the market tape + the quant shortlist and produces the final
    ranking with a thesis, target and stop for each pick.
Anything a student has rejected (or accepted) is excluded from every future list.
"""
import os, json, time, math, statistics, traceback, concurrent.futures as cf
import boto3, botocore
import common as C

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")
LLM_EVERY_CLOSED = 30 * 60           # outside market hours data is static; don't pay for a re-read every minute
bedrock = boto3.client("bedrock-runtime", config=botocore.config.Config(read_timeout=75, retries={"max_attempts": 1}))

UNIVERSE = """AAPL MSFT NVDA AMD AMZN GOOGL META TSLA NFLX AVGO ORCL CRM ADBE INTC MU QCOM PLTR UBER
SHOP COIN SOFI PYPL HOOD BAC JPM WFC C SCHW XOM CVX OXY SLB F GM RIVN DIS NKE SBUX MCD WMT TGT HD LOW
PFE MRK JNJ ABBV UNH CVS BA CAT DE GE T VZ KO PEP DAL AAL CCL ARM SMCI MRVL ANET PANW CRWD SNOW DKNG
ROKU SNAP MARA XLF XLE SMH IWM""".split()
CONTEXT = ["SPY", "QQQ", "IWM", "$VIX", "XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "SMH", "TLT"]
MAX_STOCK_PRICE = 450                # must fit >=1 share under $500
N_STOCKS, N_SPREADS = 15, 12


# ---------------------------------------------------------------- daily history (cached once per ET day)
def history(sym, today):
    item = C.get("HIST", sym)
    if item and item.get("day") == today:
        return item["closes"], item["highs"], item["lows"], item["vols"]
    _, j = C.schwab("GET", "/marketdata/v1/pricehistory",
                    params={"symbol": sym, "periodType": "month", "period": 3, "frequencyType": "daily",
                            "frequency": 1, "needExtendedHoursData": "false"})
    cs = [c for c in (j or {}).get("candles", [])
          if C.dt.datetime.fromtimestamp(c["datetime"] / 1000, C.ET).strftime("%Y-%m-%d") < today]
    out = ([c["close"] for c in cs], [c["high"] for c in cs], [c["low"] for c in cs], [c["volume"] for c in cs])
    C.put("HIST", sym, day=today, closes=out[0], highs=out[1], lows=out[2], vols=out[3])
    return out


def rsi(closes, n=2):
    g = l = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        g, l = g + max(d, 0), l + max(-d, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def features(sym, q, hist, spy_chg, session_frac):
    closes, highs, lows, vols = hist
    qq = q.get("quote", {})
    p = qq.get("lastPrice") or qq.get("mark") or qq.get("closePrice")
    if not p or len(closes) < 50:
        return None
    cl = closes + [p]
    sma20, sma50 = statistics.fmean(cl[-20:]), statistics.fmean(cl[-50:])
    trs = [max(h, cp) - min(lo, cp) for h, lo, cp in zip(highs[-14:], lows[-14:], closes[-15:-1])]
    atr_pct = 100 * statistics.fmean(trs) / p
    day_chg = qq.get("netPercentChange") or (100 * (p / closes[-1] - 1))
    avgv = statistics.fmean(vols[-20:]) or 1
    rel_vol = (qq.get("totalVolume") or 0) / (avgv * max(session_frac, 0.05))
    hi20, lo20 = max(highs[-20:]), min(lows[-20:])
    f = dict(symbol=sym, price=round(p, 2), bid=qq.get("bidPrice"), ask=qq.get("askPrice"),
             day_chg=round(day_chg, 2), rel_strength=round(day_chg - spy_chg, 2), ret5=round(100 * (p / cl[-6] - 1), 2),
             rsi2=round(rsi(cl, 2), 1), rsi14=round(rsi(cl, 14), 1), above_sma20=round(100 * (p / sma20 - 1), 2),
             above_sma50=round(100 * (p / sma50 - 1), 2), atr_pct=round(atr_pct, 2), rel_vol=round(rel_vol, 2),
             from_20d_high=round(100 * (p / hi20 - 1), 2), from_20d_low=round(100 * (p / lo20 - 1), 2))
    uptrend, downtrend = p > sma50 and sma20 > sma50, p < sma50 and sma20 < sma50
    bull, sig = 0.0, []
    if uptrend:
        bull += 1.5; sig.append("uptrend (20>50 SMA)")
    if uptrend and f["rsi2"] < 25:
        bull += (25 - f["rsi2"]) / 10 + 0.5; sig.append(f"short pullback in uptrend (RSI2 {f['rsi2']})")
    if f["from_20d_high"] > -1.5 and f["rel_vol"] > 1.2 and day_chg > 0:
        bull += 1.5; sig.append(f"breaking out near 20d high on {f['rel_vol']}x volume")
    bull += max(-3, min(3, f["rel_strength"])) * 0.3
    if 1.5 <= atr_pct <= 5:
        bull += 0.5
    elif atr_pct > 7:
        bull -= 1; sig.append("very high volatility")
    if day_chg > 6:
        bull -= 1.5; sig.append("extended today (chase risk)")
    bear, bsig = 0.0, []
    if downtrend:
        bear += 1.5; bsig.append("downtrend (20<50 SMA)")
    if downtrend and f["rsi2"] > 75:
        bear += (f["rsi2"] - 75) / 10 + 0.5; bsig.append(f"overbought bounce in downtrend (RSI2 {f['rsi2']})")
    if f["from_20d_low"] < 1.5 and f["rel_vol"] > 1.2 and day_chg < 0:
        bear += 1.5; bsig.append(f"breaking down near 20d low on {f['rel_vol']}x volume")
    bear -= max(-3, min(3, f["rel_strength"])) * 0.3
    f.update(bull=round(bull, 3), bear=round(bear, 3), signals=sig, bear_signals=bsig)
    return f


# ---------------------------------------------------------------- vertical spreads
def _mid(o):
    b, a = o.get("bid") or 0, o.get("ask") or 0
    return (b + a) / 2 if b > 0 and a > 0 else None


def best_spread(sym, side, f):
    """side CALL = bull call debit spread, PUT = bear put debit spread. Returns the best candidate or None."""
    _, j = C.schwab("GET", "/marketdata/v1/chains",
                    params={"symbol": sym, "contractType": side, "fromDate": C.EXPIRATION, "toDate": C.EXPIRATION,
                            "strikeCount": 40, "includeUnderlyingQuote": "false"})
    m = (j or {}).get("callExpDateMap" if side == "CALL" else "putExpDateMap") or {}
    exp = next((v for k, v in m.items() if k.startswith(C.EXPIRATION)), None)
    if not exp:
        return None
    opts = sorted((o[0] for o in exp.values() if o), key=lambda o: o["strikePrice"])
    if side == "PUT":
        opts = opts[::-1]                 # long the higher strike, short a lower one
    S = f["price"]

    def liquid(o):
        m_ = _mid(o)
        return m_ and m_ > 0.05 and (o["ask"] - o["bid"]) / m_ <= 0.35 and (o.get("openInterest") or 0) >= 25

    def p_beyond(be):
        # |delta| at the breakeven strike ~ risk-neutral prob of finishing beyond it; interpolate
        pts = [(o["strikePrice"], abs(o.get("delta") or 0)) for o in opts if o.get("delta") not in (None, -999)]
        pts.sort()
        for (k1, d1), (k2, d2) in zip(pts, pts[1:]):
            if k1 <= be <= k2:
                return d1 + (d2 - d1) * (be - k1) / (k2 - k1) if k2 != k1 else d1
        return None

    best = None
    for i, lo in enumerate(opts):
        d = abs(lo.get("delta") or 0)
        if not (0.38 <= d <= 0.68) or not liquid(lo):
            continue
        for sh in opts[i + 1:i + 9]:
            if not liquid(sh):
                continue
            width = abs(sh["strikePrice"] - lo["strikePrice"])
            debit = _mid(lo) - _mid(sh)
            natural = lo["ask"] - sh["bid"]
            if debit <= 0.05 or debit * 100 > 480 or not (0.25 <= debit / width <= 0.62):
                continue
            be = lo["strikePrice"] + debit if side == "CALL" else lo["strikePrice"] - debit
            p = p_beyond(be)
            if p is None:
                continue
            maxp = width - debit
            ev = (p * maxp - (1 - p) * debit) / debit           # crude expected return per $ at expiry
            cand = dict(long=lo, short=sh, width=width, debit=round(debit, 2), natural=round(natural, 2),
                        max_profit=round(maxp, 2), breakeven=round(be, 2), prob=round(p, 3), ev=round(ev, 3),
                        be_move_pct=round(100 * (be / S - 1), 2))
            if not best or ev > best["ev"]:
                best = cand
    if not best:
        return None
    lo, sh = best["long"], best["short"]
    kind = "Bull call" if side == "CALL" else "Bear put"
    k1, k2 = lo["strikePrice"], sh["strikePrice"]
    under_score = f["bull"] if side == "CALL" else f["bear"]
    return dict(
        key=f"SPR:{sym}:{side}", kind="spread", symbol=sym, side=side,
        title=f"{sym} {kind} {k1:g}/{k2:g}", sig=f"{lo['symbol']}|{sh['symbol']}",
        legs=[{"symbol": lo["symbol"], "action": "BUY_TO_OPEN", "strike": k1},
              {"symbol": sh["symbol"], "action": "SELL_TO_OPEN", "strike": k2}],
        expiration=C.EXPIRATION, underlying=S, width=best["width"], debit=best["debit"], natural=best["natural"],
        max_profit=best["max_profit"], max_loss=best["debit"], breakeven=best["breakeven"],
        prob=best["prob"], ev=best["ev"], be_move_pct=best["be_move_pct"],
        price=best["debit"], score=round(under_score + 2 * best["ev"], 3),
        signals=(f["signals"] if side == "CALL" else f["bear_signals"]) +
                [f"breakeven {best['be_move_pct']:+.1f}% away, ~{best['prob']*100:.0f}% prob beyond it",
                 f"risk ${best['debit']*100:.0f} to make ${best['max_profit']*100:.0f} per spread"])


# ---------------------------------------------------------------- Claude
PROMPT = """You are the market analyst for a live classroom trading demo. Today is {now} ET; market {mstate}.
Holding horizon: buy now, sell at a profit later THIS WEEK (by Friday's close). Option spreads expire {exp}.

MARKET TAPE (price, % change today, plus VIX):
{tape}

STOCK SHORTLIST (quant pre-screen; fields: price, day_chg %, rel_strength vs SPY, ret5 %, rsi2, rsi14,
above_sma20 %, above_sma50 %, atr_pct, rel_vol (volume vs normal pace), from_20d_high %, signals):
{stocks}

DEBIT VERTICAL SPREAD SHORTLIST (id, structure, debit, max profit, breakeven distance, approx prob beyond breakeven, underlying signals):
{spreads}

Task: rank these for the best chance of a profitable exit by Friday. Weigh the market regime (index trend, VIX,
sector rotation), each name's setup quality, and risk/reward. Penalize names likely to report earnings this week
if you believe so, and say so. Use ONLY the symbols and ids given. Rank every stock and every spread.

Return ONLY JSON, no prose outside it:
{{"market_view": "<2-3 sentences on today's regime and what it favors>",
 "stocks": [{{"symbol": "...", "thesis": "<=25 words", "target": <price>, "stop": <price>, "confidence": "high|medium|low"}}, ...best first],
 "spreads": [{{"id": "...", "thesis": "<=25 words", "confidence": "high|medium|low"}}, ...best first]}}"""


def ask_claude(tape, stocks, spreads, mstate):
    def s_line(f):
        keep = ["price", "day_chg", "rel_strength", "ret5", "rsi2", "rsi14", "above_sma20", "above_sma50",
                "atr_pct", "rel_vol", "from_20d_high"]
        return f"{f['symbol']}: " + ", ".join(f"{k}={f[k]}" for k in keep) + f"; signals: {'; '.join(f['signals']) or 'none'}"

    def p_line(s):
        return (f"{s['key']}: {s['title']} exp {s['expiration']}, underlying {s['underlying']}, debit {s['debit']}, "
                f"max profit {s['max_profit']}, breakeven {s['be_move_pct']:+}% away, prob~{s['prob']}; "
                f"{'; '.join(s['signals'][:-2]) or 'no strong signals'}")
    prompt = PROMPT.format(now=C.now_et().strftime("%a %Y-%m-%d %H:%M"), mstate=mstate, exp=C.EXPIRATION,
                           tape="\n".join(tape), stocks="\n".join(map(s_line, stocks)),
                           spreads="\n".join(map(p_line, spreads)))
    r = bedrock.converse(modelId=MODEL_ID, messages=[{"role": "user", "content": [{"text": prompt}]}],
                         inferenceConfig={"maxTokens": 3500, "temperature": 0.2})
    txt = r["output"]["message"]["content"][0]["text"]
    return json.loads(txt[txt.index("{"): txt.rindex("}") + 1])


# ---------------------------------------------------------------- main
def sync_orders():
    n = 0
    for it in C.query("ORDER"):
        if it.get("status") in C.TERMINAL and it.get("released", it.get("status") == "FILLED"):
            continue
        try:
            C.refresh_order(it); n += 1
        except Exception as e:
            print("order sync failed", it["order_id"], e)
    return n


def evaluate():
    t0 = time.time()
    now = C.now_et()
    today = now.strftime("%Y-%m-%d")
    is_open = C.market_open(now)
    session_frac = min(1, max(0, (now.hour * 60 + now.minute - 570) / 390)) if is_open else 1
    decided = {d["sk"] for d in C.query("DECISION")}

    q = C.quotes(UNIVERSE + CONTEXT)
    spy_chg = q.get("SPY", {}).get("quote", {}).get("netPercentChange") or 0
    tape = []
    for s in CONTEXT:
        qq = q.get(s, {}).get("quote", {})
        if qq:
            tape.append(f"{s}: {qq.get('lastPrice')} ({qq.get('netPercentChange', 0):+.2f}%)")

    busy = C.busy_underlyings()          # raises -> whole run fails loudly rather than ranking blind
    syms = [s for s in UNIVERSE if s in q and not C.excluded(s) and s not in busy]
    with cf.ThreadPoolExecutor(12) as ex:
        hist = dict(zip(syms, ex.map(lambda s: _safe(history, s, today), syms)))
    feats = [f for s in syms if hist.get(s) for f in [features(s, q[s], hist[s], spy_chg, session_frac)] if f]

    stock_c = sorted((f for f in feats if f["price"] <= C.MAX_ORDER_USD * 0.9 and f["price"] <= MAX_STOCK_PRICE
                      and f"STK:{f['symbol']}" not in decided), key=lambda f: -f["bull"])[:N_STOCKS]

    bulls = [f for f in sorted(feats, key=lambda f: -f["bull"]) if f"SPR:{f['symbol']}:CALL" not in decided][:14]
    # Bull call debit spreads only: a short PUT that goes deep in the money can be exercised early at any time,
    # and a short CALL essentially only ahead of an ex-dividend date - so skip names with one before expiry.
    bulls = [f for f in bulls if not C.div_before(q[f["symbol"]].get("fundamental"), C.EXPIRATION)]
    jobs = [(f, "CALL") for f in bulls]
    with cf.ThreadPoolExecutor(8) as ex:
        spreads = [s for s in ex.map(lambda j: _safe(best_spread, j[0]["symbol"], j[1], j[0]), jobs) if s]
    spreads = sorted(spreads, key=lambda s: -s["score"])[:N_SPREADS]

    stock_recs = [dict(key=f"STK:{f['symbol']}", kind="stock", symbol=f["symbol"], title=f"{f['symbol']} shares",
                       sig=f["symbol"], price=f["price"], score=f["bull"], signals=f["signals"],
                       metrics={k: f[k] for k in ("day_chg", "rel_strength", "ret5", "rsi2", "atr_pct", "rel_vol")})
                  for f in stock_c]

    # Claude: every minute while the market is open, every 30 min otherwise (reusing its last read in between)
    prev = C.get("LLM", "last") or {}
    llm, llm_err = prev, None
    if is_open or time.time() - prev.get("at", 0) > LLM_EVERY_CLOSED or not prev.get("stocks"):
        try:
            out = ask_claude(tape, stock_c, spreads, "OPEN" if is_open else "CLOSED (last prices shown)")
            llm = dict(at=int(time.time()), model=MODEL_ID, market_view=out.get("market_view", ""),
                       stocks={s["symbol"]: dict(s, rank=i) for i, s in enumerate(out.get("stocks", []))},
                       spreads={s["id"]: dict(s, rank=i) for i, s in enumerate(out.get("spreads", []))})
            C.put("LLM", "last", **llm)
        except Exception as e:
            llm_err = f"{type(e).__name__}: {e}"[:300]
            print("LLM failed", llm_err)

    def apply(recs, table, idfield):
        ann = (llm or {}).get(table) or {}
        for r in recs:
            a = ann.get(r[idfield])
            if a:
                r.update(thesis=a.get("thesis"), confidence=a.get("confidence"), llm_rank=a["rank"])
                if table == "stocks":
                    r.update(target=a.get("target"), stop=a.get("stop"))
            else:
                r.update(thesis="Quant screen: " + ("; ".join(r["signals"][:2]) or "relative strength"),
                         confidence="low", llm_rank=999)
        recs.sort(key=lambda r: (r["llm_rank"], -r["score"]))
        for i, r in enumerate(recs):
            r["rank"] = i + 1
        return recs

    gen = int(time.time())
    stock_recs = apply(stock_recs, "stocks", "symbol")
    spreads = apply(spreads, "spreads", "key")
    C.put("RECS", "stock", items=stock_recs, generated_at=gen)
    C.put("RECS", "spread", items=spreads, generated_at=gen)
    C.put("STATUS", "engine", ok=True, generated_at=gen, market_open=is_open, tape=tape,
          market_view=(llm or {}).get("market_view", ""), llm_at=(llm or {}).get("at"), llm_error=llm_err,
          model=MODEL_ID, n_universe=len(feats), took=round(time.time() - t0, 1), error=None)
    return len(stock_recs), len(spreads)


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        print(f"{fn.__name__}{a[:2]} failed: {e}")
        return None


def handler(event, context):
    try:
        n = sync_orders()
        ns, np_ = evaluate()
        print(f"synced {n} orders; {ns} stock recs, {np_} spread recs")
        return {"ok": True}
    except C.TokenStale as e:
        msg = str(e)
    except Exception as e:
        traceback.print_exc()
        msg = f"{type(e).__name__}: {e}"[:400]
    st = C.get("STATUS", "engine") or {}
    st.pop("pk", None); st.pop("sk", None)
    st.update(ok=False, error=msg, error_at=int(time.time()))
    C.put("STATUS", "engine", **st)
    print("ENGINE ERROR:", msg)
    raise RuntimeError(msg)       # fail loudly (CloudWatch error metric)
