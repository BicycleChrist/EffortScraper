#!/usr/bin/env python
"""Parity + benchmark harness: native QuickieParse.KalshiLiveBook vs the pure
-Python KalshiClient.KalshiLiveBook.

Two things are proven here:
  1. PARITY — replaying an identical frame corpus through both books yields
     identical state()/ladder() at EVERY step. This is the go/no-go gate: we do
     not swap the live trading path until outputs match exactly.
  2. PERF — wall-clock throughput and per-frame p50/p99 for three paths:
       a) stdlib json.loads + Python apply   (the CURRENT Kalshi _on_message path)
       b) orjson.loads     + Python apply   (the free, zero-C++ win)
       c) native ingest(raw_str)            (simdjson parse + book update, GIL off)

Run:  python cppparser/bench_kalshi.py
"""
import json
import os
import random
import sys
import time
from statistics import quantiles

import orjson

# Native module lives next to this file; the Python book is one dir up.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import QuickieParse  # noqa: E402  (the built .so)
from KalshiClient import KalshiLiveBook as PyBook  # noqa: E402

TICKER = "KXMLBGAME-25JUN20PHINYM-PHI"


def gen_corpus(n_frames=60000, seed=7):
    """A realistic Kalshi stream: an initial snapshot, then deltas (dollar-string
    `*_fp` schema AND the integer-cent fallback, to exercise every _to_cents
    branch), periodic trades, occasional sequence gaps and resnapshots."""
    rnd = random.Random(seed)
    frames = []
    sid = 1001
    seq = 1

    def snapshot():
        nonlocal seq
        # ~14 levels each side around a 72c market, dollar-string fp schema.
        yes = [[f"0.{p:02d}00", f"{rnd.randint(50, 150000)}.0"]
               for p in range(60, 74)]
        no = [[f"0.{p:02d}00", f"{rnd.randint(50, 150000)}.0"]
              for p in range(20, 34)]
        seq = 1
        return json.dumps({"type": "orderbook_snapshot", "sid": sid, "seq": seq,
                           "msg": {"market_ticker": TICKER,
                                   "yes_dollars_fp": yes, "no_dollars_fp": no}})

    frames.append(snapshot())
    for i in range(n_frames - 1):
        r = rnd.random()
        if r < 0.015:
            # Resnapshot (new sid binding happens server-side; keep sid here).
            frames.append(snapshot())
            continue
        if r < 0.045:
            # Trade print.
            yp = rnd.randint(64, 80) / 100.0
            frames.append(json.dumps({"type": "trade",
                "msg": {"market_ticker": TICKER, "yes_price_dollars": f"{yp:.4f}"}}))
            continue
        # Delta. Every ~30th one skips a seq to exercise the gap path.
        seq += 1
        if i % 30 == 0:
            seq += 1  # induce a gap
        side = "yes" if rnd.random() < 0.5 else "no"
        price = rnd.randint(60, 73) if side == "yes" else rnd.randint(20, 33)
        dlt = rnd.choice([-1, 1]) * rnd.randint(1, 5000)
        if i % 3 == 0:
            # Integer-cent fallback form: bare `price` int + `delta` number.
            inner = {"market_ticker": TICKER, "side": side,
                     "price": price, "delta": float(dlt)}
        else:
            # Live dollar-string form.
            inner = {"market_ticker": TICKER, "side": side,
                     "price_dollars": f"0.{price:02d}00", "delta_fp": f"{float(dlt)}"}
        frames.append(json.dumps({"type": "orderbook_delta", "sid": sid,
                                  "seq": seq, "msg": inner}))
    return frames


def norm_state(s):
    if s is None:
        return None
    return (s["best_bid"], s["best_ask"],
            None if s["mid"] is None else round(s["mid"], 6),
            s["last_trade"], s["stale"])


def norm_ladder(l):
    if l is None:
        return None
    return (tuple(tuple(x) for x in l["bids"]),
            tuple(tuple(x) for x in l["asks"]),
            l["best_bid"], l["best_ask"], l["last_trade"], l["stale"])


def check_parity(frames):
    py = PyBook()
    cpp = QuickieParse.KalshiLiveBook()
    mismatches = 0
    first = None
    for i, f in enumerate(frames):
        py.apply(json.loads(f))
        cpp.ingest(f)
        ps, cs = norm_state(py.state(TICKER)), norm_state(cpp.state(TICKER))
        pl, cl = norm_ladder(py.ladder(TICKER, 8)), norm_ladder(cpp.ladder(TICKER, 8))
        if ps != cs or pl != cl:
            mismatches += 1
            if first is None:
                first = (i, ps, cs, pl, cl)
    return mismatches, first


def time_path(frames, fn, repeats=3):
    """Return (frames_per_sec, p50_ns, p99_ns) for fn(frame) over the corpus."""
    best_total = None
    lat = []
    for r in range(repeats):
        t0 = time.perf_counter()
        if r == repeats - 1:
            for f in frames:
                a = time.perf_counter_ns()
                fn(f)
                lat.append(time.perf_counter_ns() - a)
        else:
            for f in frames:
                fn(f)
        dt = time.perf_counter() - t0
        best_total = dt if best_total is None else min(best_total, dt)
    fps = len(frames) / best_total
    qs = quantiles(lat, n=100)
    return fps, qs[49], qs[98]


def main():
    frames = gen_corpus()
    print(f"corpus: {len(frames):,} frames\n")

    mism, first = check_parity(frames)
    if mism == 0:
        print("PARITY: ✅ identical state+ladder at every step")
    else:
        print(f"PARITY: ❌ {mism} mismatched steps; first = {first}")
        return

    # a) current path: stdlib json + Python apply
    pyb = PyBook()
    fps_a, p50_a, p99_a = time_path(frames, lambda f: pyb.apply(json.loads(f)))
    # b) orjson + Python apply
    pyb2 = PyBook()
    fps_b, p50_b, p99_b = time_path(frames, lambda f: pyb2.apply(orjson.loads(f)))
    # c) native ingest
    cppb = QuickieParse.KalshiLiveBook()
    fps_c, p50_c, p99_c = time_path(frames, lambda f: cppb.ingest(f))

    print("\n               frames/sec     p50/frame     p99/frame")
    print(f"  json+py     {fps_a:>10,.0f}   {p50_a:>8.0f} ns   {p99_a:>8.0f} ns")
    print(f"  orjson+py   {fps_b:>10,.0f}   {p50_b:>8.0f} ns   {p99_b:>8.0f} ns")
    print(f"  native C++  {fps_c:>10,.0f}   {p50_c:>8.0f} ns   {p99_c:>8.0f} ns")
    print(f"\nspeedup vs current (json+py): {fps_c / fps_a:.1f}x")
    print(f"speedup vs orjson+py        : {fps_c / fps_b:.1f}x")


if __name__ == "__main__":
    main()
