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

# Surface-specific tour averages, computed from Sackmann-schema ATP match data
# (2024+2025 seasons, ~925k service points): serve dominance differs enough by
# surface to change hold rates (grass 84.0%, hard 80.8%, clay 76.6%), which is
# what drives set length, tiebreak frequency and therefore games totals/spreads.
# Using one flat baseline made grass sets too break-heavy and clay too clean.
SURFACE_SERVE_AVG = {
    "Hard": 0.6453,
    "Clay": 0.6204,
    "Grass": 0.6630,
}

# WTA equivalents (same Sackmann-schema computation, 2024+2025, ~750k service
# points). Serve dominance is far lower on the women's tour — holds are 66.1%
# hard / 61.9% clay / 70.0% grass vs ATP's 80.8/76.6/84.0 — so running a WTA
# match on ATP baselines badly overprices holds, totals and tiebreak rates.
WTA_SERVE_AVG = 0.5660          # svpt-weighted mean of the three surfaces
WTA_SURFACE_SERVE_AVG = {
    "Hard": 0.5691,
    "Clay": 0.5499,
    "Grass": 0.5857,
}


def surface_serve_avg(surface: Optional[str], tour: str = "ATP") -> float:
    """Tour-average SPW for a surface, falling back to the tour's overall
    average. `tour` is "ATP" (default) or "WTA"."""
    if str(tour).upper() == "WTA":
        return WTA_SURFACE_SERVE_AVG.get(surface or "", WTA_SERVE_AVG)
    return SURFACE_SERVE_AVG.get(surface or "", ATP_SERVE_AVG)


def tour_serve_avg(tour: str = "ATP") -> float:
    """Overall tour-average SPW."""
    return WTA_SERVE_AVG if str(tour).upper() == "WTA" else ATP_SERVE_AVG


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
def serve_point_prob(spw_server: float, rpw_returner: float,
                     serve_avg: float = ATP_SERVE_AVG) -> float:
    """P(server wins a point) given server SPW% and returner RPW% (0-1 each).

    Deviation-from-tour-average blend (O'Malley / Barnett style):
        p = SERVE_AVG + (SPW - SERVE_AVG) - (RPW - RETURN_AVG)
    with SERVE_AVG the tour average for the surface being played (see
    SURFACE_SERVE_AVG) so two tour-average players hold at the surface's true
    rate. Clamped to a sane range.
    """
    p = serve_avg + (spw_server - serve_avg) - (rpw_returner - (1.0 - serve_avg))
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


# Per-match latent "relative form" spread, in logit(point-on-serve) space.
# i.i.d. sets systematically understate straight-set finishes: a 75% Bo5
# favourite can NEVER have 3-0 more likely than 3-1 under i.i.d. sets (that
# needs set-win > 2/3, i.e. match win > ~79%), yet books price 3-0 as the
# modal scoreline for such favourites. Real matches have day-to-day form: the
# player who is better *today* tends to be better in every set. A single
# zero-mean gaussian shift drawn once per simulated match (A up, B down)
# induces exactly that inter-set correlation and fattens both 3-0 tails.
# 0.15 calibrated against 983 completed Bo5 matches (Sackmann ATP data,
# 2024-25): empirical set-count distribution is 43% / 34% / 22.5% for 3/4/5
# sets, which a favourite-strength mixture around 65-80% reproduces at sigma
# ~0.12-0.15 (0.20 overshot to ~50%+ three-setters, shorting games totals by
# ~2). Conditional games-per-set-count validate the within-set model (sim
# 3-setters avg ~29 games vs 28.7 empirical; 5-setters ~51 vs 50.3), so the
# latent-form draw only has to fix the set-count mixture. Market set-betting
# boards for a ~75-80% Bo5 favourite (3-0 ≈ 39% devigged) agree independently.
# Full-corpus backtest (backtest_model.py, td_matches 2017-26, ~24k graded
# matches/tour bucketed by market favourite prob) confirmed 0.15 for ATP
# (Bo5 straight-set/games/margin all within ~1pp) and showed WTA runs
# streakier: empirical straight-set shares sit between the 0.15 and 0.20
# columns in every bucket, best fit ~0.17.
MATCH_FORM_SIGMA = 0.15
WTA_MATCH_FORM_SIGMA = 0.17


def tour_form_sigma(tour: str = "ATP") -> float:
    return WTA_MATCH_FORM_SIGMA if str(tour).upper() == "WTA" else MATCH_FORM_SIGMA

# 7-point Gauss-Hermite rule (physicists'), for averaging the analytic match
# win probability over the latent-form gaussian when solving the anchor.
_GH_NODES = (
    (0.0, 0.8102646175568073),
    (0.8162878828589647, 0.4256072526101278),
    (-0.8162878828589647, 0.4256072526101278),
    (1.6735516287674714, 0.05451558281912703),
    (-1.6735516287674714, 0.05451558281912703),
    (2.6519613568352334, 0.0009717812450995192),
    (-2.6519613568352334, 0.0009717812450995192),
)
_GH_NORM = math.sqrt(math.pi)


def solve_anchor_delta(pa: float, pb: float, target: float,
                       best_of: int = 3,
                       form_sigma: float = 0.0) -> float:
    """Find the symmetric logit shift delta such that, with the two serve
    point probs shifted (pa up, pb down by delta in logit space), the analytic
    match win probability for A equals `target`. Bisection; delta may be <0.

    If `form_sigma` > 0 the Monte Carlo will additionally jitter the logits by
    a per-match N(0, sigma) draw, which pulls the *expected* win prob toward
    0.5; the solve averages the analytic probability over that gaussian
    (Gauss-Hermite) so the anchored headline still lands on `target`."""
    target = min(0.999, max(0.001, target))
    la, lb = _logit(pa), _logit(pb)

    def wp_at(delta: float, eps: float) -> float:
        na = min(0.92, max(0.30, _sigmoid(la + delta + eps)))
        nb = min(0.92, max(0.30, _sigmoid(lb - delta - eps)))
        return match_win_prob(na, nb, best_of)

    def wp(delta: float) -> float:
        if form_sigma <= 0.0:
            return wp_at(delta, 0.0)
        scale = form_sigma * math.sqrt(2.0)
        return sum(w * wp_at(delta, scale * t) for t, w in _GH_NODES) / _GH_NORM

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
    margin_hist: Dict[int, float] = field(default_factory=dict)  # A games - B games -> prob
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

    def spread_probs(self, line: float):
        """P(A covers a games handicap of `line`) and P(B covers).

        `line` is A's handicap in the usual book convention: -4.5 means A must
        win by 5+ games; +2.5 means A covers unless beaten by 3+. Half-point
        lines only (no push mass)."""
        cover = sum(p for m, p in self.margin_hist.items() if m + line > 0)
        return cover, 1.0 - cover

    def fair_spread(self):
        """Half-point games handicap for A closest to a 50/50 cover split."""
        if not self.margin_hist:
            return 0.0, 0.5
        best_line, best_gap, best_cover = 0.5, 1.0, 0.5
        # A covers `line` iff margin > -line, so candidate lines span the
        # negated margin support.
        lo = -max(self.margin_hist) - 0.5
        hi = -min(self.margin_hist) + 0.5
        line = lo
        while line <= hi:
            cover, _ = self.spread_probs(line)
            gap = abs(cover - 0.5)
            if gap < best_gap:
                best_line, best_gap, best_cover = line, gap, cover
            line += 1.0
        return best_line, best_cover


def simulate_match(spw_a: float, rpw_a: float, spw_b: float, rpw_b: float,
                   best_of: int = 3, n: int = 20000,
                   seed: Optional[int] = None,
                   anchor_p: Optional[float] = None,
                   form_sigma: float = MATCH_FORM_SIGMA,
                   serve_avg: float = ATP_SERVE_AVG) -> MatchSimResult:
    """Monte Carlo a match from surface service/return rates (all 0-1).

    If `anchor_p` is given, the two players' per-point serve probabilities are
    shifted by a symmetric logit delta so the simulated match win probability
    for A matches `anchor_p`. This keeps the serve/return rates as the source of
    scoreline/games *texture* while pinning the headline probability to a
    calibrated target (e.g. surface Elo, optionally context-adjusted). Raw,
    un-opponent-adjusted point rates otherwise let a big-serving small-sample
    player run away with a match the ratings say they should lose.

    `form_sigma` adds a per-match latent form draw (see MATCH_FORM_SIGMA):
    sets within one simulated match share the same form shift, giving the
    inter-set correlation that i.i.d. sets lack. The anchor solve integrates
    over the same gaussian, so the headline win prob still hits `anchor_p`."""
    rng = random.Random(seed)
    pa = serve_point_prob(spw_a, rpw_b, serve_avg)
    pb = serve_point_prob(spw_b, rpw_a, serve_avg)
    sets_to_win = 3 if best_of == 5 else 2
    anchor_delta = 0.0
    if anchor_p is not None:
        # Deterministic analytic solve (exact game/set/tiebreak model). Matches
        # the sampled headline to within Monte Carlo noise, with no probe-stream
        # jitter of its own.
        anchor_delta = solve_anchor_delta(pa, pb, anchor_p, best_of,
                                          form_sigma=form_sigma)
        pa = min(0.92, max(0.30, _sigmoid(_logit(pa) + anchor_delta)))
        pb = min(0.92, max(0.30, _sigmoid(_logit(pb) - anchor_delta)))
    la, lb = _logit(pa), _logit(pb)

    a_wins = 0
    total_games = 0
    straights = 0
    decider = 0
    scores: Dict[str, int] = {}
    oriented: Dict[str, int] = {}
    games_hist: Dict[int, int] = {}
    margin_hist: Dict[int, int] = {}

    for _ in range(n):
        if form_sigma > 0.0:
            eps = rng.gauss(0.0, form_sigma)
            pa_m = min(0.92, max(0.30, _sigmoid(la + eps)))
            pb_m = min(0.92, max(0.30, _sigmoid(lb - eps)))
        else:
            pa_m, pb_m = pa, pb
        sa = sb = 0
        a_first = True
        games = 0
        a_games = b_games = 0
        while sa < sets_to_win and sb < sets_to_win:
            w, ga, gb, _nxt = _sim_set(pa_m, pb_m, a_first, rng)
            games += ga + gb
            a_games += ga
            b_games += gb
            if w == "A":
                sa += 1
            else:
                sb += 1
            a_first = not a_first
        total_games += games
        games_hist[games] = games_hist.get(games, 0) + 1
        margin = a_games - b_games
        margin_hist[margin] = margin_hist.get(margin, 0) + 1
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
        margin_hist={k: v / n for k, v in sorted(margin_hist.items())},
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
