# QuickieParse — native order-book parsers (simdjson + nanobind)

Native reimplementations of the sub-second feed parsers, ingesting the **raw
websocket frame string** (simdjson parse + book update in one call, GIL released
during parse) instead of a pre-parsed Python dict. This removes Python's
`json.loads` + the per-frame intermediate dict allocation from the hot path.

## Status — both books wired into HistoricalOddsWidget (native by default)
- `KalshiLiveBook` — parity with `KalshiClient.KalshiLiveBook`. `ingest(raw_str)`
  returns the affected market's state dict, plus a `"gap": True` flag on a seq gap
  (the caller resubscribes — no held Python callback, so no shutdown leak). Fed by
  `KalshiStreamClient.raw_frame`. A/B: `EFFORTODDS_K_NATIVE=0` forces Python.
- `PolymarketLiveBook` — parity with `polymarketquery.PolymarketLiveBook`.
  `ingest(raw_str)` handles object-or-array, returns touched asset_ids. Fed by
  `PolymarketStreamClient.raw_frame`. PM lacks WS snapshot-on-resubscribe, so the
  widget also REST-seeds the book on every subscribe (`_seed_pm_books_rest`). A/B:
  `EFFORTODDS_PM_NATIVE=0` forces Python.
- Both guarded by `NATIVE_PARSERS` (try/except import) with transparent fallback.

## Build
Requires: g++/clang, cmake ≥3.18, `nanobind` (`pip install nanobind`), and system
`simdjson` (header + lib; resolved via pkg-config).

```bash
cd cppparser
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
# -> QuickieParse.cpython-3xx-*.so is dropped in cppparser/
```

## Parity + benchmark
```bash
python cppparser/bench_kalshi.py   # Kalshi book
python cppparser/bench_pm.py       # Polymarket book
```
Each replays an identical 60k-frame corpus through both books, asserting
identical `state()`/`ladder()` at **every** step, then times three paths.
Representative run (Python 3.14, GCC 16):

Kalshi:
| path                         | frames/sec | p50/frame | p99/frame |
|------------------------------|-----------:|----------:|----------:|
| stdlib json + Python apply (current) | 356k | 2690 ns | 12870 ns |
| orjson + Python apply (free win)     | 536k | 1780 ns | 11309 ns |
| native C++ ingest                    | 1.15M | 870 ns | 4800 ns |

→ **3.2× vs current**, **2.1× vs orjson**, p99 tail cut ~2.7×. Parity exact.

Polymarket (messages can be event arrays):
| path                         | msgs/sec | p50/msg | p99/msg |
|------------------------------|---------:|--------:|--------:|
| stdlib json + Python apply (current) | 296k | 3270 ns | 16650 ns |
| orjson + Python apply                | 429k | 2180 ns | 13650 ns |
| native C++ ingest                    | 1.19M | 830 ns | 5240 ns |

→ **4.0× vs current**, **2.8× vs orjson**, p99 tail cut ~3.2×. Parity exact.

## Parity-critical semantics (mirrored from the Python book)
- `_to_cents`: JSON **integer** is taken as-is (already cents); JSON **double/
  string** uses the `≤1.0 → dollars×100, else cents` rule. Must branch on the
  JSON value type, not just the numeric value.
- `int(round(x))` is **round-half-to-even** (Python `round`); native uses
  `std::nearbyint` under the default FE_TONEAREST mode.
- **`seq` is validated per-SID, not per-market.** Kalshi serves only ONE active
  `orderbook_delta` subscription per connection, so all of a view's markets ride a
  single `sid` and their deltas **interleave on one shared `seq` counter**.
  Consecutive deltas for the *same* market are therefore NOT consecutive in seq.
  The book keeps a `sid -> last_seq` map (`_sid_seq` / `sid_seq_`): a snapshot
  baselines it, and a delta is a real gap only when it breaks the SID's own
  sequence. Checking seq per-market_ticker instead falsely flags every
  interleaving as a gap and freezes all but one side (the dual-side `·resync`
  freeze bug). Subscribing each market separately is NOT a fix — the later sub
  silently starves the earlier one's orderbook feed.
- A real sequence gap (a break in the SID's seq) sets `stale=True`, notifies, and
  **still applies** the delta best-effort.
- Snapshot preserves the prior `last_trade`; `trade` setdefaults a blank book.

## Widget integration (Polymarket trial)
`HistoricalOddsClient.py` imports `QuickieParse` with a try/except → `NATIVE_PARSERS`
flag. When available, the widget's `pm_live_book` is the **native** book, fed the
raw frame string via the new `PolymarketStreamClient.raw_frame` signal
(`_on_pm_raw_frame` → `ingest(raw)`); the parsed-dict `_on_pm_orderbook` /
`_on_pm_trade` handlers early-return in native mode to avoid double-applying. The
Kalshi book is still the Python one (next candidate once the PM trial is proven
live). Both PM `_on_message` and Kalshi `_on_message` now decode with `orjson`.

## Next
- Wire the native Kalshi book in (same raw-frame pattern; KalshiStreamClient would
  need a raw_frame str signal — currently it emits parsed dicts).
- Optional: have `ingest` skip building the state dict (return None) and read
  `state()` only when the UI needs it — removes the per-frame dict-build that
  currently caps the speedup.
