#!/usr/bin/env python
"""Parity + benchmark: native QuickieParse.PolymarketLiveBook vs the pure-Python
polymarketquery.PolymarketLiveBook.

Replays an identical market-channel corpus (book snapshots + price_change bundles
+ last_trade_price, some delivered as arrays) through both books, asserting
identical state()/ladder() at every message, then times:
  a) stdlib json.loads + per-event Python apply (current PM _on_message path)
  b) orjson.loads     + per-event Python apply
  c) native ingest(raw_str)            (simdjson, GIL off, handles the array)
"""
import json
import os
import random
import sys
import time
from statistics import quantiles

import orjson

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import QuickieParse  # noqa: E402
from polymarketquery import PolymarketLiveBook as PyBook  # noqa: E402

A1 = "71321009198143209022193431619886180400191769412377414"  # phillies token
A2 = "98765432109876543210987654321098765432109876543210000"  # mets token
ASSETS = [A1, A2]


def book_event(aid, rnd):
    bids = [{"price": f"0.{p:03d}", "size": f"{rnd.randint(50, 150000)}"}
            for p in range(600, 740, 10)]
    asks = [{"price": f"0.{p:03d}", "size": f"{rnd.randint(50, 150000)}"}
            for p in range(740, 880, 10)]
    return {"event_type": "book", "asset_id": aid, "bids": bids, "asks": asks}


def gen_corpus(n_msgs=60000, seed=11):
    rnd = random.Random(seed)
    msgs = []
    # Initial book burst as an ARRAY (both assets at once) — exercises the
    # array-frame path in the native ingest.
    msgs.append(json.dumps([book_event(A1, rnd), book_event(A2, rnd)]))
    for i in range(n_msgs - 1):
        r = rnd.random()
        aid = rnd.choice(ASSETS)
        if r < 0.02:
            msgs.append(json.dumps(book_event(aid, rnd)))
        elif r < 0.06:
            price = rnd.randint(640, 800) / 1000.0
            msgs.append(json.dumps({"event_type": "last_trade_price",
                                    "asset_id": aid, "price": f"{price:.3f}"}))
        else:
            # price_change bundling 1-3 level updates (sometimes across both assets)
            n = rnd.randint(1, 3)
            changes = []
            for _ in range(n):
                a = rnd.choice(ASSETS)
                # Mixed case on purpose: the real feed's side isn't upper-case
                # (Python does .upper()), so the corpus must exercise that.
                side = rnd.choice(["buy", "BUY", "Buy"]) if rnd.random() < 0.5 \
                    else rnd.choice(["sell", "SELL", "Sell"])
                p = rnd.randint(600, 740) if side == "BUY" else rnd.randint(740, 880)
                size = rnd.choice([0, rnd.randint(1, 200000)])  # 0 => remove level
                changes.append({"asset_id": a, "side": side,
                                "price": f"0.{p:03d}", "size": f"{size}"})
            msgs.append(json.dumps({"event_type": "price_change",
                                    "price_changes": changes}))
    return msgs


def norm_state(s):
    if s is None:
        return None
    return (None if s["best_bid"] is None else round(s["best_bid"], 1),
            None if s["best_ask"] is None else round(s["best_ask"], 1),
            None if s["mid"] is None else round(s["mid"], 6),
            None if s["last_trade"] is None else round(s["last_trade"], 1))


def norm_ladder(l):
    if l is None:
        return None
    return (tuple((round(p, 1), q) for p, q in l["bids"]),
            tuple((round(p, 1), q) for p, q in l["asks"]),
            None if l["best_bid"] is None else round(l["best_bid"], 1),
            None if l["best_ask"] is None else round(l["best_ask"], 1))


def py_apply_msg(book, raw, loader):
    msg = loader(raw)
    for ev in (msg if isinstance(msg, list) else [msg]):
        if isinstance(ev, dict):
            book.apply(ev)


def check_parity(msgs):
    py = PyBook()
    cpp = QuickieParse.PolymarketLiveBook()
    mismatches = 0
    first = None
    for i, raw in enumerate(msgs):
        py_apply_msg(py, raw, json.loads)
        cpp.ingest(raw)
        for aid in ASSETS:
            ps, cs = norm_state(py.state(aid)), norm_state(cpp.state(aid))
            pl, cl = norm_ladder(py.ladder(aid, 8)), norm_ladder(cpp.ladder(aid, 8))
            if ps != cs or pl != cl:
                mismatches += 1
                if first is None:
                    first = (i, aid, ps, cs, pl, cl)
    return mismatches, first


def time_path(msgs, fn, repeats=3):
    best = None
    lat = []
    for r in range(repeats):
        t0 = time.perf_counter()
        if r == repeats - 1:
            for m in msgs:
                a = time.perf_counter_ns()
                fn(m)
                lat.append(time.perf_counter_ns() - a)
        else:
            for m in msgs:
                fn(m)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    qs = quantiles(lat, n=100)
    return len(msgs) / best, qs[49], qs[98]


def main():
    msgs = gen_corpus()
    print(f"corpus: {len(msgs):,} messages\n")

    mism, first = check_parity(msgs)
    if mism == 0:
        print("PARITY: ✅ identical state+ladder at every message")
    else:
        print(f"PARITY: ❌ {mism} mismatches; first = {first}")
        return

    b1 = PyBook()
    fps_a, p50_a, p99_a = time_path(msgs, lambda m: py_apply_msg(b1, m, json.loads))
    b2 = PyBook()
    fps_b, p50_b, p99_b = time_path(msgs, lambda m: py_apply_msg(b2, m, orjson.loads))
    bc = QuickieParse.PolymarketLiveBook()
    fps_c, p50_c, p99_c = time_path(msgs, lambda m: bc.ingest(m))

    print("\n               msgs/sec      p50/msg       p99/msg")
    print(f"  json+py     {fps_a:>10,.0f}   {p50_a:>8.0f} ns   {p99_a:>8.0f} ns")
    print(f"  orjson+py   {fps_b:>10,.0f}   {p50_b:>8.0f} ns   {p99_b:>8.0f} ns")
    print(f"  native C++  {fps_c:>10,.0f}   {p50_c:>8.0f} ns   {p99_c:>8.0f} ns")
    print(f"\nspeedup vs current (json+py): {fps_c / fps_a:.1f}x")
    print(f"speedup vs orjson+py        : {fps_c / fps_b:.1f}x")


if __name__ == "__main__":
    main()
