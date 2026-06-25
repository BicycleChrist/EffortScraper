// Native order-book parsers for the EffortOdds sub-second feeds.
//
// First target: a drop-in-parity reimplementation of KalshiClient.KalshiLiveBook.
// It ingests the RAW websocket frame string (simdjson parse + book update in one
// native call, GIL released during parse) instead of taking a pre-parsed Python
// dict, so the per-tick cost no longer includes Python's json.loads + the large
// intermediate dict allocation.
//
// Parity is the contract: state()/ladder() must match the Python implementation
// byte-for-byte over a replayed frame corpus. The helpers below replicate the
// Python semantics exactly, including the int-vs-string _to_cents branch and
// round-half-to-even (Python's round()).

#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <simdjson.h>

#include <algorithm>
#include <cctype>
#include <cfenv>
#include <cmath>
#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace nb = nanobind;
namespace sj = simdjson;

namespace {

// Python int(round(x)): round-half-to-even. std::nearbyint honours the current
// FP rounding mode, whose default (FE_TONEAREST) is round-half-to-even.
inline long py_round_to_long(double x) { return static_cast<long>(std::nearbyint(x)); }

// Replicates KalshiLiveBook._to_cents. Critically: a JSON *integer* is taken
// as-is (already cents), whereas a JSON *double* or *string* uses the
// "<=1.0 -> dollars*100, else cents" rule. That distinction is real in the feed
// (snapshot integer-cent fallback vs dollar strings like "0.2300").
inline std::optional<long> to_cents(sj::dom::element el) {
    switch (el.type()) {
        case sj::dom::element_type::BOOL:
            return std::nullopt;  // Python: isinstance(p, bool) -> None
        case sj::dom::element_type::INT64:
            return static_cast<long>(int64_t(el));
        case sj::dom::element_type::UINT64:
            return static_cast<long>(uint64_t(el));
        case sj::dom::element_type::DOUBLE: {
            double f = double(el);
            return f <= 1.0 ? py_round_to_long(f * 100.0) : py_round_to_long(f);
        }
        case sj::dom::element_type::STRING: {
            std::string_view sv;
            if (el.get_string().get(sv)) return std::nullopt;
            try {
                double f = std::stod(std::string(sv));
                return f <= 1.0 ? py_round_to_long(f * 100.0) : py_round_to_long(f);
            } catch (...) {
                return std::nullopt;
            }
        }
        default:
            return std::nullopt;
    }
}

// Replicates _to_qty: float(q) for number/string, else 0.0.
inline double to_qty(sj::dom::element el) {
    switch (el.type()) {
        case sj::dom::element_type::INT64:  return double(int64_t(el));
        case sj::dom::element_type::UINT64: return double(uint64_t(el));
        case sj::dom::element_type::DOUBLE: return double(el);
        case sj::dom::element_type::STRING: {
            std::string_view sv;
            if (el.get_string().get(sv)) return 0.0;
            try { return std::stod(std::string(sv)); }
            catch (...) { return 0.0; }
        }
        default: return 0.0;
    }
}

struct Book {
    std::unordered_map<long, double> yes;
    std::unordered_map<long, double> no;
    std::optional<int64_t> sid;
    std::optional<int64_t> seq;
    std::optional<long> last_trade;
    bool stale = false;
};

class KalshiLiveBook {
public:
    KalshiLiveBook() = default;

    // Ingest one raw frame string. Returns the normalized state dict for the
    // affected market (or None when the frame was ignored). On a sequence gap the
    // returned dict carries "gap": True so the CALLER can resubscribe — we don't
    // hold a Python callback in C++ (that leaks at shutdown and forces a GIL
    // reacquire mid-parse).
    nb::object ingest(const std::string &frame) {
        std::string ticker;
        bool produced = false, gap = false;
        {
            // Parse + mutate the book off the GIL; only touch Python objects after.
            nb::gil_scoped_release release;
            produced = apply_locked(frame, ticker, gap);
        }
        if (!produced && !gap) return nb::none();
        nb::dict d;
        if (produced) {
            nb::object st = state(ticker);
            if (st.is_none()) { d["market_ticker"] = ticker; }
            else d = nb::cast<nb::dict>(st);
        } else {
            d["market_ticker"] = ticker;  // gap on a market with no book yet
        }
        d["gap"] = gap;
        return d;
    }

    nb::object state(const std::string &ticker) {
        auto it = books_.find(ticker);
        if (it == books_.end()) return nb::none();
        const Book &b = it->second;

        std::optional<long> best_bid = max_key(b.yes);
        std::optional<long> best_ask;
        if (auto mk = max_key(b.no)) best_ask = 100 - *mk;

        std::optional<double> mid;
        if (best_bid && best_ask) mid = (*best_bid + *best_ask) / 2.0;
        else if (best_bid) mid = static_cast<double>(*best_bid);
        else if (best_ask) mid = static_cast<double>(*best_ask);

        nb::dict d;
        d["market_ticker"] = ticker;
        d["best_bid"] = opt_long(best_bid);
        d["best_ask"] = opt_long(best_ask);
        d["mid"] = mid ? nb::cast(*mid) : nb::none();
        d["last_trade"] = opt_long(b.last_trade);
        d["stale"] = b.stale;
        return d;
    }

    nb::object ladder(const std::string &ticker, int depth = 10) {
        auto it = books_.find(ticker);
        if (it == books_.end()) return nb::none();
        const Book &b = it->second;

        // bids: yes levels, highest price first.
        std::vector<std::pair<long, double>> bids(b.yes.begin(), b.yes.end());
        std::sort(bids.begin(), bids.end(),
                  [](auto &a, auto &c) { return a.first > c.first; });
        if ((int)bids.size() > depth) bids.resize(depth);

        // asks: no levels as (100 - price, qty), lowest ask first.
        std::vector<std::pair<long, double>> asks;
        asks.reserve(b.no.size());
        for (auto &kv : b.no) asks.emplace_back(100 - kv.first, kv.second);
        std::sort(asks.begin(), asks.end(),
                  [](auto &a, auto &c) { return a.first < c.first; });
        if ((int)asks.size() > depth) asks.resize(depth);

        nb::list bid_list, ask_list;
        for (auto &kv : bids)
            bid_list.append(nb::make_tuple(kv.first, py_round_to_long(kv.second)));
        for (auto &kv : asks)
            ask_list.append(nb::make_tuple(kv.first, py_round_to_long(kv.second)));

        nb::dict d;
        d["bids"] = bid_list;
        d["asks"] = ask_list;
        d["best_bid"] = bids.empty() ? nb::none() : nb::cast(bids.front().first);
        d["best_ask"] = asks.empty() ? nb::none() : nb::cast(asks.front().first);
        d["last_trade"] = opt_long(b.last_trade);
        d["stale"] = b.stale;
        return d;
    }

    nb::object current_sid(const std::string &ticker) {
        auto it = books_.find(ticker);
        if (it != books_.end() && it->second.sid) return nb::cast(*it->second.sid);
        return nb::none();  // match Python: None when unknown
    }

    void reset(nb::object ticker = nb::none()) {
        if (ticker.is_none()) { books_.clear(); sid_seq_.clear(); return; }
        auto it = books_.find(nb::cast<std::string>(ticker));
        std::optional<int64_t> sid = (it != books_.end()) ? it->second.sid : std::nullopt;
        if (it != books_.end()) books_.erase(it);
        // Drop the per-sid baseline only if no remaining book rides this sid.
        if (sid) {
            bool still_used = false;
            for (auto &kv : books_) if (kv.second.sid && *kv.second.sid == *sid) { still_used = true; break; }
            if (!still_used) sid_seq_.erase(*sid);
        }
    }

private:
    static std::optional<long> max_key(const std::unordered_map<long, double> &m) {
        if (m.empty()) return std::nullopt;
        long best = m.begin()->first;
        for (auto &kv : m) best = std::max(best, kv.first);
        return best;
    }

    static nb::object opt_long(std::optional<long> v) {
        return v ? nb::cast(*v) : nb::none();
    }

    void parse_levels(sj::dom::object inner, const char *side,
                      std::unordered_map<long, double> &out) {
        out.clear();
        sj::dom::element arr;
        std::string fp = std::string(side) + "_dollars_fp";
        std::string dl = std::string(side) + "_dollars";
        if (inner.at_key(fp).get(arr) && inner.at_key(side).get(arr) &&
            inner.at_key(dl).get(arr)) {
            return;  // none present
        }
        sj::dom::array a;
        if (arr.get_array().get(a)) return;
        for (sj::dom::element entry : a) {
            sj::dom::array pair;
            if (entry.get_array().get(pair)) continue;
            auto itp = pair.begin();
            if (itp == pair.end()) continue;
            sj::dom::element price_el = *itp;
            ++itp;
            if (itp == pair.end()) continue;
            sj::dom::element qty_el = *itp;
            if (auto c = to_cents(price_el)) out[*c] = to_qty(qty_el);
        }
    }

    // Returns true if a state should be produced for `ticker`. Sets gap_out=true
    // on a sequence gap so ingest() can surface it to the caller.
    bool apply_locked(const std::string &frame, std::string &ticker_out, bool &gap_out) {
        sj::dom::element root;
        auto err = parser_.parse(frame).get(root);
        if (err) return false;
        sj::dom::object msg;
        if (root.get_object().get(msg)) return false;

        std::string_view mtype;
        if (msg.at_key("type").get(mtype)) return false;

        sj::dom::object inner;
        if (msg.at_key("msg").get_object().get(inner)) return false;

        std::string_view ticker_sv;
        if (inner.at_key("market_ticker").get(ticker_sv)) return false;
        std::string ticker(ticker_sv);
        ticker_out = ticker;

        if (mtype == "orderbook_snapshot") {
            std::optional<long> prev_last;
            auto pit = books_.find(ticker);
            if (pit != books_.end()) prev_last = pit->second.last_trade;
            Book b;
            b.last_trade = prev_last;
            parse_levels(inner, "yes", b.yes);
            parse_levels(inner, "no", b.no);
            int64_t sid;
            if (!msg.at_key("sid").get(sid)) b.sid = sid;
            int64_t seq;
            if (!msg.at_key("seq").get(seq)) b.seq = seq;
            // Baseline the SID-level seq from this snapshot (its seq is part of the
            // subscription's single sequence stream shared by all its markets).
            if (b.sid && b.seq) sid_seq_[*b.sid] = *b.seq;
            books_[ticker] = std::move(b);
            return true;
        }

        if (mtype == "orderbook_delta") {
            auto it = books_.find(ticker);
            if (it == books_.end() || !it->second.seq) {
                gap_out = true;  // can't apply a delta with no snapshot -> resync
                return false;
            }
            Book &b = it->second;

            int64_t seq;
            bool have_seq = !msg.at_key("seq").get(seq);
            int64_t sid;
            bool have_sid = !msg.at_key("sid").get(sid);

            // Ignore deltas from a different sid than our bound snapshot.
            if (b.sid && have_sid && sid != *b.sid) return false;

            // Sequence gap is evaluated at the SID level: one subscription's seq
            // counter is shared across every market it carries, so consecutive deltas
            // for the SAME market are NOT consecutive in seq (a sibling market's deltas
            // fall between). Checking per-market would flag those interleavings as gaps
            // and freeze the book. Only a break in the SID's own sequence is real.
            if (have_sid && have_seq) {
                auto sit = sid_seq_.find(sid);
                if (sit != sid_seq_.end() && seq != sit->second + 1) {
                    b.stale = true;
                    gap_out = true;
                }
                sid_seq_[sid] = seq;
            } else if (b.seq && have_seq && seq != *b.seq + 1) {
                // No sid on the frame: fall back to the per-market check.
                b.stale = true;
                gap_out = true;
            }
            if (have_seq) b.seq = seq;

            std::string_view side;
            bool have_side = !inner.at_key("side").get(side);

            // price = inner.price ?? inner.price_dollars
            std::optional<long> price;
            sj::dom::element price_el;
            if (!inner.at_key("price").get(price_el)) price = to_cents(price_el);
            else if (!inner.at_key("price_dollars").get(price_el)) price = to_cents(price_el);

            // delta = inner.delta_fp ?? inner.delta (live schema uses delta_fp)
            sj::dom::element delta_el;
            bool have_delta = false;
            if (!inner.at_key("delta_fp").get(delta_el)) have_delta = true;
            else if (!inner.at_key("delta").get(delta_el)) have_delta = true;

            bool side_ok = have_side && (side == "yes" || side == "no");
            if (side_ok && price && have_delta) {
                auto &levels = (side == "yes") ? b.yes : b.no;
                double new_qty = 0.0;
                auto lit = levels.find(*price);
                if (lit != levels.end()) new_qty = lit->second;
                new_qty += to_qty(delta_el);
                if (new_qty <= 1e-9) levels.erase(*price);
                else levels[*price] = new_qty;
            }
            return true;
        }

        if (mtype == "trade") {
            Book &b = books_[ticker];  // setdefault(blank)
            sj::dom::element yp;
            if (!inner.at_key("yes_price_dollars").get(yp)) {
                double f;
                bool ok = false;
                switch (yp.type()) {
                    case sj::dom::element_type::INT64:  f = double(int64_t(yp)); ok = true; break;
                    case sj::dom::element_type::UINT64: f = double(uint64_t(yp)); ok = true; break;
                    case sj::dom::element_type::DOUBLE: f = double(yp); ok = true; break;
                    case sj::dom::element_type::STRING: {
                        std::string_view sv;
                        if (!yp.get_string().get(sv)) {
                            try { f = std::stod(std::string(sv)); ok = true; }
                            catch (...) { ok = false; }
                        }
                        break;
                    }
                    default: ok = false;
                }
                if (ok) b.last_trade = py_round_to_long(f * 100.0);
            }
            return true;
        }

        return false;
    }

    std::unordered_map<std::string, Book> books_;
    // Kalshi `seq` is per-SID, not per market_ticker: one subscription can carry
    // several markets whose deltas interleave on a single shared seq counter.
    // Validate seq at the SID level so interleaving isn't mistaken for per-market
    // gaps. sid -> last seq seen on that subscription.
    std::unordered_map<int64_t, int64_t> sid_seq_;
    sj::dom::parser parser_;
};

// --------------------------------------------------------------------------
// Polymarket live book — parity with polymarketquery.PolymarketLiveBook.
//
// Differences from Kalshi: prices are dollar floats 0-1 normalized to CENTS at
// 0.1c resolution (PM tick = 0.001 dollars); a token's own bids/asks are the
// YES book directly (no NO inversion); there are NO sequence numbers (a `book`
// event is an authoritative snapshot, `price_change` sets each level's new
// ABSOLUTE size). A single frame may be an ARRAY of events touching several
// assets. Internally levels are keyed by tenths-of-a-cent (long) to dodge
// float-key hashing; reported prices are tenths/10.0 (e.g. 72.5).

struct PmBook {
    std::unordered_map<long, double> bids;   // key: tenths-of-cent
    std::unordered_map<long, double> asks;
    std::optional<long> last_trade;          // tenths-of-cent
};

// Python _to_cents rounded to 0.1c -> tenths-of-cent. round(price*1000) with
// round-half-to-even (matches Python round at the 0.1c grid PM ticks live on).
inline std::optional<long> price_to_tenths(sj::dom::element el) {
    double f;
    switch (el.type()) {
        case sj::dom::element_type::INT64:  f = double(int64_t(el)); break;
        case sj::dom::element_type::UINT64: f = double(uint64_t(el)); break;
        case sj::dom::element_type::DOUBLE: f = double(el); break;
        case sj::dom::element_type::STRING: {
            std::string_view sv;
            if (el.get_string().get(sv)) return std::nullopt;
            try { f = std::stod(std::string(sv)); } catch (...) { return std::nullopt; }
            break;
        }
        default: return std::nullopt;
    }
    return py_round_to_long(f * 1000.0);
}

class PolymarketLiveBook {
public:
    nb::object ingest(const std::string &frame) {
        std::vector<std::string> touched;
        {
            nb::gil_scoped_release release;
            apply_locked(frame, touched);
        }
        nb::list out;
        for (auto &a : touched) out.append(a);
        return out;
    }

    nb::object state(const std::string &asset_id) {
        auto it = books_.find(asset_id);
        if (it == books_.end()) return nb::none();
        const PmBook &b = it->second;
        std::optional<long> bb = max_key(b.bids);
        std::optional<long> ba = min_key(b.asks);
        std::optional<double> mid;
        std::optional<double> best_bid = bb ? std::optional<double>(*bb / 10.0) : std::nullopt;
        std::optional<double> best_ask = ba ? std::optional<double>(*ba / 10.0) : std::nullopt;
        if (best_bid && best_ask) mid = (*best_bid + *best_ask) / 2.0;
        else if (best_bid) mid = *best_bid;
        else if (best_ask) mid = *best_ask;

        nb::dict d;
        d["asset_id"] = asset_id;
        d["best_bid"] = best_bid ? nb::cast(*best_bid) : nb::none();
        d["best_ask"] = best_ask ? nb::cast(*best_ask) : nb::none();
        d["mid"] = mid ? nb::cast(*mid) : nb::none();
        d["last_trade"] = b.last_trade ? nb::cast(*b.last_trade / 10.0) : nb::none();
        d["stale"] = false;  // no seq stream to fall behind
        return d;
    }

    nb::object ladder(const std::string &asset_id, int depth = 10) {
        auto it = books_.find(asset_id);
        if (it == books_.end()) return nb::none();
        const PmBook &b = it->second;

        std::vector<std::pair<long, double>> bids(b.bids.begin(), b.bids.end());
        std::sort(bids.begin(), bids.end(),
                  [](auto &a, auto &c) { return a.first > c.first; });
        if ((int)bids.size() > depth) bids.resize(depth);
        std::vector<std::pair<long, double>> asks(b.asks.begin(), b.asks.end());
        std::sort(asks.begin(), asks.end(),
                  [](auto &a, auto &c) { return a.first < c.first; });
        if ((int)asks.size() > depth) asks.resize(depth);

        nb::list bid_list, ask_list;
        for (auto &kv : bids)
            bid_list.append(nb::make_tuple(kv.first / 10.0, py_round_to_long(kv.second)));
        for (auto &kv : asks)
            ask_list.append(nb::make_tuple(kv.first / 10.0, py_round_to_long(kv.second)));

        nb::dict d;
        d["bids"] = bid_list;
        d["asks"] = ask_list;
        d["best_bid"] = bids.empty() ? nb::none() : nb::cast(bids.front().first / 10.0);
        d["best_ask"] = asks.empty() ? nb::none() : nb::cast(asks.front().first / 10.0);
        d["last_trade"] = b.last_trade ? nb::cast(*b.last_trade / 10.0) : nb::none();
        d["stale"] = false;
        return d;
    }

    void reset(nb::object asset_id = nb::none()) {
        if (asset_id.is_none()) { books_.clear(); return; }
        books_.erase(nb::cast<std::string>(asset_id));
    }

private:
    static std::optional<long> max_key(const std::unordered_map<long, double> &m) {
        if (m.empty()) return std::nullopt;
        long best = m.begin()->first;
        for (auto &kv : m) best = std::max(best, kv.first);
        return best;
    }
    static std::optional<long> min_key(const std::unordered_map<long, double> &m) {
        if (m.empty()) return std::nullopt;
        long best = m.begin()->first;
        for (auto &kv : m) best = std::min(best, kv.first);
        return best;
    }

    void apply_event(sj::dom::object ev, std::vector<std::string> &touched) {
        std::string_view et;
        if (ev.at_key("event_type").get(et)) return;

        if (et == "book") {
            std::string_view aid;
            if (ev.at_key("asset_id").get(aid)) return;
            std::string asset(aid);
            std::optional<long> prev_last;
            auto pit = books_.find(asset);
            if (pit != books_.end()) prev_last = pit->second.last_trade;
            PmBook nb;
            nb.last_trade = prev_last;
            sj::dom::array arr;
            if (!ev.at_key("bids").get_array().get(arr))
                for (sj::dom::element e : arr) add_level(e, nb.bids);
            if (!ev.at_key("asks").get_array().get(arr))
                for (sj::dom::element e : arr) add_level(e, nb.asks);
            books_[asset] = std::move(nb);
            touched.push_back(asset);
            return;
        }

        if (et == "price_change") {
            sj::dom::array changes;
            if (ev.at_key("price_changes").get_array().get(changes)) return;
            for (sj::dom::element ch_el : changes) {
                sj::dom::object ch;
                if (ch_el.get_object().get(ch)) continue;
                std::string_view aid;
                if (ch.at_key("asset_id").get(aid)) continue;
                std::string asset(aid);
                std::string_view side_sv;
                if (ch.at_key("side").get(side_sv)) continue;
                // Match Python's (side or "").upper() == "BUY"/"SELL": the feed's
                // case is not guaranteed, so compare case-insensitively.
                std::string side(side_sv);
                for (auto &c : side) c = static_cast<char>(std::toupper((unsigned char)c));
                PmBook &b = books_[asset];
                std::unordered_map<long, double> *levels = nullptr;
                if (side == "BUY") levels = &b.bids;
                else if (side == "SELL") levels = &b.asks;
                else continue;
                sj::dom::element price_el;
                if (ch.at_key("price").get(price_el)) continue;
                auto t = price_to_tenths(price_el);
                if (!t) continue;
                sj::dom::element size_el;
                double size = 0.0;
                if (!ch.at_key("size").get(size_el)) size = to_qty(size_el);
                if (size <= 1e-9) levels->erase(*t);
                else (*levels)[*t] = size;
                touched.push_back(asset);
            }
            return;
        }

        if (et == "last_trade_price") {
            std::string_view aid;
            if (ev.at_key("asset_id").get(aid)) return;
            std::string asset(aid);
            sj::dom::element price_el;
            if (!ev.at_key("price").get(price_el)) {
                if (auto t = price_to_tenths(price_el)) books_[asset].last_trade = *t;
            }
            touched.push_back(asset);
            return;
        }
    }

    void add_level(sj::dom::element entry,
                   std::unordered_map<long, double> &levels) {
        sj::dom::object o;
        if (entry.get_object().get(o)) return;
        sj::dom::element price_el;
        if (o.at_key("price").get(price_el)) return;
        auto t = price_to_tenths(price_el);
        if (!t) return;
        sj::dom::element size_el;
        double size = 0.0;
        if (!o.at_key("size").get(size_el)) size = to_qty(size_el);
        levels[*t] = size;
    }

    void apply_locked(const std::string &frame, std::vector<std::string> &touched) {
        sj::dom::element root;
        if (parser_.parse(frame).get(root)) return;
        // Frame may be a single event object OR an array of events.
        sj::dom::array arr;
        if (!root.get_array().get(arr)) {
            for (sj::dom::element e : arr) {
                sj::dom::object ev;
                if (!e.get_object().get(ev)) apply_event(ev, touched);
            }
        } else {
            sj::dom::object ev;
            if (!root.get_object().get(ev)) apply_event(ev, touched);
        }
    }

    std::unordered_map<std::string, PmBook> books_;
    sj::dom::parser parser_;
};

}  // namespace

NB_MODULE(QuickieParse, m) {
    m.doc() = "Native order-book parsers for EffortOdds sub-second feeds (simdjson).";

    nb::class_<KalshiLiveBook>(m, "KalshiLiveBook")
        .def(nb::init<>())
        .def("ingest", &KalshiLiveBook::ingest, nb::arg("frame"),
             "Parse a raw websocket frame string and apply it; returns the "
             "normalized state dict or None.")
        .def("state", &KalshiLiveBook::state, nb::arg("ticker"))
        .def("ladder", &KalshiLiveBook::ladder, nb::arg("ticker"), nb::arg("depth") = 10)
        .def("current_sid", &KalshiLiveBook::current_sid, nb::arg("ticker"))
        .def("reset", &KalshiLiveBook::reset, nb::arg("ticker") = nb::none());

    nb::class_<PolymarketLiveBook>(m, "PolymarketLiveBook")
        .def(nb::init<>())
        .def("ingest", &PolymarketLiveBook::ingest, nb::arg("frame"),
             "Parse a raw market-channel frame (object or array) and apply every "
             "event; returns the list of touched asset_ids.")
        .def("state", &PolymarketLiveBook::state, nb::arg("asset_id"))
        .def("ladder", &PolymarketLiveBook::ladder, nb::arg("asset_id"), nb::arg("depth") = 10)
        .def("reset", &PolymarketLiveBook::reset, nb::arg("asset_id") = nb::none());
}
