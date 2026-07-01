"""Lightweight tennis match models for the comparison window.

Two complementary, deliberately-simple models:

  * `elo_win_prob`  - surface (or overall) Elo -> match win probability.
  * `simulate_match` - a point->game->set->match Monte Carlo driven by each
    player's surface-specific service / return points-won rates. It yields the
    win probability *and* the scoreline texture (straight-sets %, decider %,
    average games, set-score distribution) that a single Elo number can't give.

Everything is pure-Python (stdlib `random`/`math`) so it has no extra deps and
runs comfortably off the UI thread.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from functools import lru_cache
from math import comb
from typing import Dict, Optional, Tuple

# ATP tour-average share of service points won. A point is won by either the
# server or the returner, so the return average is the complement.
ATP_SERVE_AVG = 0.642
ATP_RETURN_AVG = 1.0 - ATP_SERVE_AVG


# --------------------------------------------------------------------------- #
# Elo
# --------------------------------------------------------------------------- #
def elo_win_prob(elo_a: float, elo_b: float) -> float:
    """Logistic Elo expectation that A beats B."""
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def blend_elo(overall: Optional[float], surface: Optional[float],
              w_surface: float = 0.5) -> Optional[float]:
    """Blend overall and surface-specific Elo.

    FiveThirtyEight / Tennis Abstract tested a wide range of mixes and found a
    50/50 surface+overall blend is close to optimal on all three surfaces and
    beats surface-only ratings (surface samples are noisier). `w_surface` is the
    weight on the surface rating; the remainder goes to the overall rating.
    Missing inputs degrade gracefully to whichever rating is available.
    """
    if surface is None and overall is None:
        return None
    if surface is None:
        return overall
    if overall is None:
        return surface
    return w_surface * surface + (1.0 - w_surface) * overall


def prob_to_american(p: float) -> str:
    """Fair American odds for a probability (no vig)."""
    if p <= 0:
        return "+∞"
    if p >= 1:
        return "-∞"
    if p >= 0.5:
        return f"-{round(100 * p / (1 - p))}"
    return f"+{round(100 * (1 - p) / p)}"


# --------------------------------------------------------------------------- #
# Serve/return point model
# --------------------------------------------------------------------------- #
def serve_point_prob(spw_server: float, rpw_returner: float) -> float:
    """P(server wins a point) given server SPW% and returner RPW% (0-1 each).

    Deviation-from-tour-average blend (O'Malley / Barnett style):
        p = SERVE_AVG + (SPW - SERVE_AVG) - (RPW - RETURN_AVG)
    Clamped to a sane range.
    """
    p = ATP_SERVE_AVG + (spw_server - ATP_SERVE_AVG) - (rpw_returner - ATP_RETURN_AVG)
    return min(0.92, max(0.30, p))


def shrink_rate(rate: Optional[float], n: Optional[float], prior: float,
                pseudo: float = 25.0) -> Optional[float]:
    """Empirical-Bayes shrink a rate estimated from `n` matches toward `prior`.

    A surface split built from a handful of matches (e.g. a 9-match grass line)
    is mostly noise; blend it toward a stable baseline with a pseudo-count so
    small samples lean on the prior and large samples stand on their own::

        shrunk = (n*rate + pseudo*prior) / (n + pseudo)
    """
    if rate is None:
        return prior
    nn = max(0.0, float(n or 0))
    return (nn * rate + pseudo * prior) / (nn + pseudo)


# --------------------------------------------------------------------------- #
# Analytic game/set/match win probability (used to anchor the Monte Carlo).
# --------------------------------------------------------------------------- #
def game_win_prob(p: float) -> float:
    """P(server holds) given per-point serve win prob `p` (deuce-aware)."""
    p = min(0.999, max(0.001, p))
    q = 1.0 - p
    # win to love/15/30, plus reaching deuce (3-3) then winning it.
    base = p**4 * (1 + 4 * q + 10 * q * q)
    deuce = 20 * p**3 * q**3 * (p * p / (p * p + q * q))
    return base + deuce


def _tiebreak_win_prob(pa: float, pb: float) -> float:
    """Exact P(A wins a 7-point tiebreak) given per-point serve win probs, with
    the standard A,B,B,A,A,... serving order (A serves first). Deuce is resolved
    by a bounded DP (mass beyond ~20-20 is negligible)."""

    @lru_cache(maxsize=None)
    def T(a: int, b: int) -> float:
        if a >= 7 and a - b >= 2:
            return 1.0
        if b >= 7 and b - a >= 2:
            return 0.0
        if a + b >= 40:                        # negligible tail; break the tie
            return 1.0 if a > b else (0.0 if b > a else 0.5)
        server_a = _tiebreak_server_is_a(a + b + 1, True)
        pa_pt = pa if server_a else (1.0 - pb)
        return pa_pt * T(a + 1, b) + (1.0 - pa_pt) * T(a, b + 1)

    val = T(0, 0)
    T.cache_clear()
    return val


def _set_win_prob(pa: float, pb: float) -> float:
    """P(A wins a set) given both point-on-serve probs, via an exact games Markov
    chain with an exact tiebreak. A serves first (symmetric enough for the anchor
    solve; the Monte Carlo carries the full serving order for the texture)."""
    ga = game_win_prob(pa)          # A holds
    gb = game_win_prob(pb)          # B holds
    tb = _tiebreak_win_prob(pa, pb)  # A wins a 6-6 tiebreak

    @lru_cache(maxsize=None)
    def P(a: int, b: int, a_serving: bool) -> float:
        if a >= 6 and a - b >= 2:
            return 1.0
        if b >= 6 and b - a >= 2:
            return 0.0
        if a == 6 and b == 6:
            return tb
        hold = ga if a_serving else gb
        # game won by A if server holds & A serving, or server broken & B serving
        pa_game = hold if a_serving else (1.0 - hold)
        return pa_game * P(a + 1, b, not a_serving) + \
            (1.0 - pa_game) * P(a, b + 1, not a_serving)

    val = P(0, 0, True)
    P.cache_clear()
    return val


def match_win_prob(pa: float, pb: float, best_of: int = 3) -> float:
    """Analytic P(A wins the match) from per-point serve win probs."""
    s = _set_win_prob(pa, pb)
    sets_to_win = 3 if best_of == 5 else 2
    # P(A wins the race to `sets_to_win` sets), sets i.i.d. with prob s.
    total = 0.0
    for b_sets in range(sets_to_win):           # B's sets when A wins
        n = sets_to_win - 1 + b_sets
        total += comb(n, b_sets) * s**sets_to_win * (1 - s)**b_sets
    return total


def _logit(p: float) -> float:
    p = min(0.999999, max(1e-6, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def solve_anchor_delta(pa: float, pb: float, target: float,
                       best_of: int = 3) -> float:
    """Find the symmetric logit shift delta such that, with the two serve
    point probs shifted (pa up, pb down by delta in logit space), the analytic
    match win probability for A equals `target`. Bisection; delta may be <0."""
    target = min(0.999, max(0.001, target))
    la, lb = _logit(pa), _logit(pb)

    def wp(delta: float) -> float:
        na = min(0.92, max(0.30, _sigmoid(la + delta)))
        nb = min(0.92, max(0.30, _sigmoid(lb - delta)))
        return match_win_prob(na, nb, best_of)

    lo, hi = -2.5, 2.5
    # Ensure the target is bracketed (clamps handle extreme asks gracefully).
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if wp(mid) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _sim_game(p: float, rng: random.Random) -> bool:
    """One service game. Returns True if the server holds."""
    s = r = 0
    while True:
        if rng.random() < p:
            s += 1
        else:
            r += 1
        if s >= 4 and s - r >= 2:
            return True
        if r >= 4 and r - s >= 2:
            return False


def _tiebreak_server_is_a(point_idx: int, a_first: bool) -> bool:
    """Which player serves point `point_idx` (1-indexed) of a tiebreak."""
    # Serving order: 1, then alternating pairs -> A,B,B,A,A,B,B,...
    block = point_idx // 2  # 0 for pt1, 1 for pts2-3, 2 for pts4-5, ...
    a_serves = (block % 2 == 0)
    return a_serves if a_first else not a_serves


def _sim_tiebreak(pa: float, pb: float, a_first: bool, rng: random.Random) -> str:
    a = b = 0
    pt = 1
    while True:
        server_a = _tiebreak_server_is_a(pt, a_first)
        p = pa if server_a else pb
        server_wins = rng.random() < p
        if (server_a and server_wins) or (not server_a and not server_wins):
            a += 1
        else:
            b += 1
        if a >= 7 and a - b >= 2:
            return "A"
        if b >= 7 and b - a >= 2:
            return "B"
        pt += 1


def _sim_set(pa: float, pb: float, a_serves_first: bool,
             rng: random.Random) -> Tuple[str, int, int, bool]:
    """Simulate a set. Returns (winner, a_games, b_games, next_server_is_a)."""
    ga = gb = 0
    a_serving = a_serves_first
    while True:
        if a_serving:
            if _sim_game(pa, rng):
                ga += 1
            else:
                gb += 1
        else:
            if _sim_game(pb, rng):
                gb += 1
            else:
                ga += 1
        a_serving = not a_serving
        if ga >= 6 and ga - gb >= 2:
            return "A", ga, gb, a_serving
        if gb >= 6 and gb - ga >= 2:
            return "B", ga, gb, a_serving
        if ga == 6 and gb == 6:
            tb = _sim_tiebreak(pa, pb, a_serving, rng)
            if tb == "A":
                return "A", 7, 6, a_serving
            return "B", 6, 7, a_serving


@dataclass
class MatchSimResult:
    p_a: float                      # A match win probability
    p_b: float
    avg_games: float                # average total games in the match
    p_straights_winner: float       # P(match ends in straight sets, either player)
    p_decider: float                # P(match goes the distance)
    set_scores: Dict[str, float] = field(default_factory=dict)  # "2-1" -> prob
    set_scores_oriented: Dict[str, float] = field(default_factory=dict)  # "A 2-1" -> prob
    games_hist: Dict[int, float] = field(default_factory=dict)  # total games -> prob
    pa_serve: float = 0.0           # derived A point-on-serve prob (post-anchor)
    pb_serve: float = 0.0
    anchor_delta: float = 0.0       # logit shift applied to hit the anchor (0 = none)
    n: int = 0

    def games_line_probs(self, line: float):
        """P(total games over `line`) and P(under) for a totals line like 22.5."""
        over = sum(p for g, p in self.games_hist.items() if g > line)
        return over, 1.0 - over

    def games_quantile(self, q: float):
        """Approximate q-quantile (0-1) of the total-games distribution."""
        cum = 0.0
        for g in sorted(self.games_hist):
            cum += self.games_hist[g]
            if cum >= q:
                return g
        return max(self.games_hist) if self.games_hist else 0


def simulate_match(spw_a: float, rpw_a: float, spw_b: float, rpw_b: float,
                   best_of: int = 3, n: int = 20000,
                   seed: Optional[int] = None,
                   anchor_p: Optional[float] = None) -> MatchSimResult:
    """Monte Carlo a match from surface service/return rates (all 0-1).

    If `anchor_p` is given, the two players' per-point serve probabilities are
    shifted by a symmetric logit delta so the simulated match win probability
    for A matches `anchor_p`. This keeps the serve/return rates as the source of
    scoreline/games *texture* while pinning the headline probability to a
    calibrated target (e.g. surface Elo, optionally context-adjusted). Raw,
    un-opponent-adjusted point rates otherwise let a big-serving small-sample
    player run away with a match the ratings say they should lose."""
    rng = random.Random(seed)
    pa = serve_point_prob(spw_a, rpw_b)
    pb = serve_point_prob(spw_b, rpw_a)
    sets_to_win = 3 if best_of == 5 else 2
    anchor_delta = 0.0
    if anchor_p is not None:
        # Deterministic analytic solve (exact game/set/tiebreak model). Matches
        # the sampled headline to within Monte Carlo noise, with no probe-stream
        # jitter of its own.
        anchor_delta = solve_anchor_delta(pa, pb, anchor_p, best_of)
        pa = min(0.92, max(0.30, _sigmoid(_logit(pa) + anchor_delta)))
        pb = min(0.92, max(0.30, _sigmoid(_logit(pb) - anchor_delta)))

    a_wins = 0
    total_games = 0
    straights = 0
    decider = 0
    scores: Dict[str, int] = {}
    oriented: Dict[str, int] = {}
    games_hist: Dict[int, int] = {}

    for _ in range(n):
        sa = sb = 0
        a_first = True
        games = 0
        while sa < sets_to_win and sb < sets_to_win:
            w, ga, gb, _nxt = _sim_set(pa, pb, a_first, rng)
            games += ga + gb
            if w == "A":
                sa += 1
            else:
                sb += 1
            a_first = not a_first
        total_games += games
        games_hist[games] = games_hist.get(games, 0) + 1
        if sa > sb:
            a_wins += 1
        hi, lo = (sa, sb) if sa > sb else (sb, sa)
        key = f"{hi}-{lo}"            # winner-first, player-agnostic
        scores[key] = scores.get(key, 0) + 1
        okey = f"{'A' if sa > sb else 'B'} {hi}-{lo}"  # oriented to a player
        oriented[okey] = oriented.get(okey, 0) + 1
        if (sa == sets_to_win and sb == 0) or (sb == sets_to_win and sa == 0):
            straights += 1
        max_sets = best_of
        if sa + sb == max_sets:
            decider += 1

    return MatchSimResult(
        p_a=a_wins / n,
        p_b=1 - a_wins / n,
        avg_games=total_games / n,
        p_straights_winner=straights / n,
        p_decider=decider / n,
        set_scores={k: v / n for k, v in sorted(scores.items(),
                                                key=lambda kv: -kv[1])},
        set_scores_oriented={k: v / n for k, v in sorted(oriented.items(),
                                                         key=lambda kv: -kv[1])},
        games_hist={k: v / n for k, v in sorted(games_hist.items())},
        pa_serve=pa,
        pb_serve=pb,
        anchor_delta=anchor_delta,
        n=n,
    )


def blended_win_prob(elo_p: Optional[float], mc_p: Optional[float],
                     w_elo: float = 0.5) -> Optional[float]:
    """Blend the Elo and Monte Carlo win probabilities."""
    parts = []
    if elo_p is not None:
        parts.append((w_elo, elo_p))
    if mc_p is not None:
        parts.append((1 - w_elo, mc_p))
    if not parts:
        return None
    tw = sum(w for w, _ in parts)
    return sum(w * p for w, p in parts) / tw


if __name__ == "__main__":
    # quick sanity check
    print("Elo 1983 vs 1798:", round(elo_win_prob(1983, 1798), 3),
          prob_to_american(elo_win_prob(1983, 1798)))
    # Ruud (clay) vs an average opponent
    res = simulate_match(0.656, 0.400, 0.642, 0.358, best_of=3, n=20000, seed=1)
    print(f"MC p_a={res.p_a:.3f} avg_games={res.avg_games:.1f} "
          f"straights={res.p_straights_winner:.2f} decider={res.p_decider:.2f}")
    print("  serve pts:", round(res.pa_serve, 3), round(res.pb_serve, 3))
    print("  scorelines:", {k: round(v, 3) for k, v in res.set_scores.items()})
    # equal players -> ~50/50 and realistic holds
    eq = simulate_match(0.642, 0.358, 0.642, 0.358, n=20000, seed=2)
    print(f"equal players p_a={eq.p_a:.3f} avg_games={eq.avg_games:.1f}")
