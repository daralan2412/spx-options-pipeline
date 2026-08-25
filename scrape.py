#!/usr/bin/env python3
"""
scrape.py — Roadmap step 1: scrape the SPX options chain by hand.

Fetches the full SPX chain from CBOE's free delayed-quotes JSON endpoint,
stamps it, filters to near-the-money strikes on the nearest expiries,
flattens one row per contract, and appends to a daily CSV.

Usage:
    python scrape.py                    # filtered snapshot -> data/YYYY-MM-DD.csv
    python scrape.py --pct 8 --expiries 4
    python scrape.py --no-filter       # keep the entire chain (big!)
    python scrape.py --dry-run         # print the summary, write nothing

Only dependency: requests   (pip install requests)
"""

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json"

# One row per contract, in this fixed order — this is the schema.
COLUMNS = [
    "fetched_utc",      # when THIS script fetched the feed
    "quote_utc",        # timestamp the feed itself reports
    "root",             # SPX (monthly, AM-settled) or SPXW (weekly, PM-settled)
    "expiry",           # YYYY-MM-DD
    "type",             # C or P
    "strike",
    "spot",             # SPX level reported alongside the chain
    "bid", "bid_size",
    "ask", "ask_size",
    "last",
    "volume",
    "open_interest",
    "iv",
    "delta", "gamma", "theta", "vega", "rho",
]

# CBOE option symbol, e.g. "SPXW260918C05500000":
#   root + yymmdd + C/P + strike*1000 zero-padded to 8 digits
SYMBOL_RE = re.compile(r"^(?P<root>[A-Z]+?)(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


def parse_symbol(sym: str):
    m = SYMBOL_RE.match(sym)
    if not m:
        return None
    d = m.groupdict()
    expiry = f"20{d['date'][0:2]}-{d['date'][2:4]}-{d['date'][4:6]}"
    return d["root"], expiry, d["cp"], int(d["strike"]) / 1000.0


def fetch_chain():
    resp = requests.get(
        CBOE_URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (personal research script)"},
    )
    resp.raise_for_status()
    return resp.json()


def flatten(payload, fetched_utc, pct, n_expiries, apply_filter=True):
    data = payload["data"]
    spot = data.get("current_price")
    quote_utc = payload.get("timestamp") or data.get("last_trade_time") or ""
    options = data.get("options", [])

    parsed = []
    skipped = 0
    for o in options:
        info = parse_symbol(o.get("option", ""))
        if info is None:
            skipped += 1
            continue
        root, expiry, cp, strike = info
        parsed.append((root, expiry, cp, strike, o))

    if apply_filter:
        lo, hi = spot * (1 - pct / 100.0), spot * (1 + pct / 100.0)
        today = fetched_utc[:10]
        future_expiries = sorted({e for _, e, _, _, _ in parsed if e >= today})
        keep_expiries = set(future_expiries[:n_expiries])
        parsed = [p for p in parsed
                  if p[1] in keep_expiries and lo <= p[3] <= hi]

    rows = []
    for root, expiry, cp, strike, o in parsed:
        rows.append([
            fetched_utc, quote_utc, root, expiry, cp, strike, spot,
            o.get("bid"), o.get("bid_size"),
            o.get("ask"), o.get("ask_size"),
            o.get("last_trade_price"),
            o.get("volume"), o.get("open_interest"),
            o.get("iv"),
            o.get("delta"), o.get("gamma"), o.get("theta"),
            o.get("vega"), o.get("rho"),
        ])
    return rows, spot, len(options), skipped


def summarize(rows, spot, total, skipped):
    print(f"spot (delayed):     {spot}")
    print(f"contracts in feed:  {total}  (unparseable symbols: {skipped})")
    print(f"rows kept:          {len(rows)}")
    if not rows:
        return
    expiries = sorted({r[3] for r in rows})
    strikes = sorted({r[5] for r in rows})
    print(f"expiries kept:      {', '.join(expiries)}")
    print(f"strike range kept:  {strikes[0]} … {strikes[-1]}")
    print("\nsample rows (first 3 / last 3):")
    print("  " + " | ".join(COLUMNS))
    for r in rows[:3] + (rows[-3:] if len(rows) > 6 else []):
        print("  " + " | ".join(str(v) for v in r))


def main():
    ap = argparse.ArgumentParser(description="Snapshot the SPX options chain (CBOE delayed).")
    ap.add_argument("--pct", type=float, default=10.0,
                    help="keep strikes within ±PCT%% of spot (default 10)")
    ap.add_argument("--expiries", type=int, default=6,
                    help="keep the N nearest expiries (default 6)")
    ap.add_argument("--no-filter", action="store_true", help="keep the entire chain")
    ap.add_argument("--dry-run", action="store_true", help="summarize only, write nothing")
    ap.add_argument("--out-dir", default="data", help="output directory (default ./data)")
    args = ap.parse_args()

    fetched_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        payload = fetch_chain()
    except requests.RequestException as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        rows, spot, total, skipped = flatten(
            payload, fetched_utc, args.pct, args.expiries,
            apply_filter=not args.no_filter,
        )
    except (KeyError, TypeError) as e:
        # Feed shape changed — dump it so the parser can be fixed.
        Path("last_payload.json").write_text(json.dumps(payload)[:2_000_000])
        print(f"unexpected feed shape ({e}); raw payload saved to last_payload.json",
              file=sys.stderr)
        sys.exit(2)

    summarize(rows, spot, total, skipped)

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{fetched_utc[:10]}.csv"
    new_file = not out_path.exists()
    with out_path.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(COLUMNS)
        w.writerows(rows)
    print(f"\nappended {len(rows)} rows -> {out_path}")


if __name__ == "__main__":
    main()
