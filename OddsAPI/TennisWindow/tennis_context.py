"""Context / situational adjustments for the tennis match model.

The base rating (surface-blended Elo) captures a player's underlying skill from
results against quality opposition, but it is *static* on the day of a match: it
does not know that a player is coming off a five-hour clay quarterfinal, is
playing their first grass match in eleven months, has been idle for six weeks,
or has quietly been serving below their career norm all season. The betting
market prices these in; a static rating does not, which is a big part of why the
model and the line disagree.

This module turns the Tennis Abstract match log into a handful of quantified,
literature-motivated adjustments, each expressed as a signed **Elo-point delta**
so they compose additively with the rating before it is handed to the Monte
Carlo as an anchor target. Everything is pure-Python and Qt-free.

Factors (see the module-level constants for the tunable magnitudes):

  * rest / rust      - days idle before the match; non-monotonic (fatigue when
                       too short, rust when too long). UTS uses a logistic
                       inactivity penalty proportional to idle days.
  * fatigue          - exponentially recency-discounted minutes played over a
                       trailing window (Sipko-style load feature).
  * surface adapt    - same-surface reps in the recent past + a season-transition
                       flag; a first grass match after the clay swing carries
                       transition noise that shouldn't read as true grass level.
  * serve form       - trailing-52-week serve/return dominance vs the career
                       baseline: rust (or a surge) on the *rates* that Elo, which
                       only sees wins and losses, is blind to.
  * importance       - tournament tier x round. Real but "individually variable
                       noise" in the literature, so it is off by default
                       (IMPORTANCE_WEIGHT = 0.0) and shown for context only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional, Tuple

import tennis_sim

# --------------------------------------------------------------------------- #
# Tunable magnitudes (Elo points). Deliberately conservative; these are soft
# situational nudges, not skill. Adjust here to re-weight the whole layer.
# --------------------------------------------------------------------------- #
REST_RUST_CAP = 70.0        # max rust penalty for a long layoff
REST_RUST_MID = 75.0        # idle-days at which rust reaches half its cap
REST_RUST_STEEP = 25.0      # logistic steepness (days)
SHORT_REST_PEN = 6.0        # per-day penalty for playing on <2 days rest

FATIGUE_CAP = 40.0          # max fatigue penalty
FATIGUE_TAU = 6.0           # recency discount time-constant (days)
FATIGUE_WINDOW = 12         # trailing days considered
FATIGUE_FREE_MIN = 150.0    # minutes of recent load treated as "free"
FATIGUE_PER_MIN = 1.0 / 12  # Elo penalty per discounted minute over the free band

SURFACE_COLD_PEN = 22.0     # penalty for zero recent reps on the match surface
SURFACE_WINDOW = 45         # trailing days that count as "recent" reps
SURFACE_TRANSITION_PEN = 6.0  # extra when the last match was a different surface

FORM_CAP = 30.0             # max serve-form adjustment (either direction)
FORM_GAIN = 400.0           # Elo per unit of (recent - career) combined dominance

IMPORTANCE_WEIGHT = 0.0     # global multiplier on the importance factor (off)
IMPORTANCE_CAP = 15.0

# Tennis Abstract level codes considered tour-level (vs challenger/ITF).
_TOUR_LEVELS = {'G': 5, 'F': 5, 'M': 4, 'A': 3, 'O': 4, 'D': 3, 'P': 3}
_ROUND_WEIGHT = {'F': 1.0, 'SF': 0.8, 'QF': 0.6, 'R16': 0.4,
                 'R32': 0.25, 'R64': 0.15, 'R128': 0.1, 'RR': 0.5}

_SURFACE_ALIASES = {'hard': 'Hard', 'clay': 'Clay', 'grass': 'Grass',
                    'carpet': 'Carpet', 'i.hard': 'Hard', 'indoor': 'Hard'}


# --------------------------------------------------------------------------- #
# Parsing helpers (Tennis Abstract match-log formats)
# --------------------------------------------------------------------------- #
def _parse_date(s) -> Optional[date]:
    s = str(s or '').strip()
    if not s:
        return None
    for fmt in ("%Y%m%d", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_minutes(s) -> Optional[float]:
    """'1:05' -> 65, '2:44' -> 164, '164' -> 164, '' -> None."""
    s = str(s or '').strip()
    if not s:
        return None
    try:
        if ':' in s:
            h, m = s.split(':')[:2]
            return int(h) * 60 + int(m)
        return float(s)
    except (ValueError, TypeError):
        return None


def _games_in_score(score: str) -> int:
    """Approximate total games from a score string like '6-4 3-6 7-6(3)'."""
    total = 0
    for tok in str(score or '').split():
        tok = tok.split('(')[0]           # drop tiebreak detail
        if '-' in tok:
            a, _, b = tok.partition('-')
            try:
                total += int(a) + int(b)
            except ValueError:
                pass
    return total


def _norm_surface(s) -> str:
    return _SURFACE_ALIASES.get(str(s or '').strip().lower(), str(s or '').strip().title())


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #
@dataclass
class Factor:
    key: str
    label: str
    delta: float            # signed Elo-point contribution
    detail: str             # short human-readable explanation


@dataclass
class ContextReport:
    factors: List[Factor] = field(default_factory=list)
    net: float = 0.0        # sum of factor deltas (Elo points)
    last_match: Optional[date] = None
    n_matches: int = 0

    def by_key(self, key: str) -> Optional[Factor]:
        return next((f for f in self.factors if f.key == key), None)


# --------------------------------------------------------------------------- #
# Individual factors
# --------------------------------------------------------------------------- #
def _rest_rust(days_idle: Optional[int]) -> Factor:
    if days_idle is None:
        return Factor('rest', 'Rest / rust', 0.0, 'no schedule data')
    d = days_idle
    if d < 2:
        pen = -SHORT_REST_PEN * (2 - d)
        return Factor('rest', 'Rest / rust', pen, f'{d}d rest (short — fatigue)')
    if d <= 14:
        return Factor('rest', 'Rest / rust', 0.0, f'{d}d rest (fresh)')
    # Logistic rust penalty that grows with idle days.
    pen = -REST_RUST_CAP / (1.0 + math.exp(-(d - REST_RUST_MID) / REST_RUST_STEEP))
    tag = 'rusty' if d < 60 else 'long layoff'
    return Factor('rest', 'Rest / rust', pen, f'{d}d idle ({tag})')


def _fatigue(matches: List[dict], as_of: date) -> Factor:
    load = 0.0
    recent = 0
    for m in matches:
        dt = _parse_date(m.get('date'))
        if not dt:
            continue
        ago = (as_of - dt).days
        if ago < 0 or ago > FATIGUE_WINDOW:
            continue
        mins = _parse_minutes(m.get('match_time'))
        if mins is None:                       # fall back to games as a proxy
            mins = _games_in_score(m.get('score')) * 4.5
        load += mins * math.exp(-ago / FATIGUE_TAU)
        recent += 1
    over = max(0.0, load - FATIGUE_FREE_MIN)
    pen = -min(FATIGUE_CAP, over * FATIGUE_PER_MIN)
    if recent == 0:
        return Factor('fatigue', 'Fatigue', 0.0, 'no recent load')
    return Factor('fatigue', 'Fatigue', pen,
                  f'{recent} match{"es" if recent != 1 else ""} / '
                  f'{load:.0f} disc. min ({FATIGUE_WINDOW}d)')


def _surface_adapt(matches: List[dict], surface: str, as_of: date) -> Factor:
    surface = _norm_surface(surface)
    reps = 0
    last_surface = None
    last_dt = None
    n_dated = 0
    for m in matches:
        dt = _parse_date(m.get('date'))
        if not dt:
            continue
        n_dated += 1
        if last_dt is None or dt > last_dt:
            last_dt, last_surface = dt, _norm_surface(m.get('surface'))
        ago = (as_of - dt).days
        if 0 <= ago <= SURFACE_WINDOW and _norm_surface(m.get('surface')) == surface:
            reps += 1
    if n_dated == 0:                       # no schedule evidence -> neutral
        return Factor('surface', 'Surface adapt', 0.0, 'no schedule data')
    # Ramp the cold penalty down as recent same-surface reps accumulate.
    ramp = {0: 1.0, 1: 0.55, 2: 0.22}.get(reps, 0.0)
    pen = -SURFACE_COLD_PEN * ramp
    trans = ''
    if last_surface and last_surface != surface and reps <= 1:
        pen -= SURFACE_TRANSITION_PEN
        trans = f', off {last_surface}'
    detail = (f'{reps} {surface} match{"es" if reps != 1 else ""} in '
              f'{SURFACE_WINDOW}d{trans}')
    return Factor('surface', 'Surface adapt', pen, detail)


def _serve_form(surf52: dict, surf: dict, surface: str) -> Factor:
    """Trailing-52-week serve+return dominance vs career, on the match surface
    (falling back to Total). Positive = serving/returning above career norm."""
    surface = _norm_surface(surface)

    def dom(splits):
        row = splits.get(surface) or splits.get('Total')
        if not row or row.get('spw') is None or row.get('rpw') is None:
            return None, 0
        return (row['spw'] + row['rpw']), (row.get('m') or 0)

    d52, n52 = dom(surf52 or {})
    dcar, _ = dom(surf or {})
    if d52 is None or dcar is None:
        return Factor('form', 'Serve form', 0.0, 'no recent split')
    delta = d52 - dcar
    # Down-weight tiny recent samples toward zero adjustment.
    conf = min(1.0, n52 / 15.0)
    adj = max(-FORM_CAP, min(FORM_CAP, delta * FORM_GAIN)) * conf
    arrow = 'above' if delta >= 0 else 'below'
    return Factor('form', 'Serve form', adj,
                  f'52wk {arrow} career ({delta * 100:+.1f} pts, n={int(n52)})')


def _importance(level: Optional[str], rnd: Optional[str]) -> Factor:
    if not IMPORTANCE_WEIGHT:
        return Factor('importance', 'Importance', 0.0, 'off')
    if not level:
        return Factor('importance', 'Importance', 0.0, 'no draw data')
    lw = _TOUR_LEVELS.get(str(level).strip(), 2)
    rw = _ROUND_WEIGHT.get(str(rnd or '').strip().upper(), 0.3)
    mag = min(IMPORTANCE_CAP, IMPORTANCE_WEIGHT * lw * rw * 4.0)
    return Factor('importance', 'Importance', mag,
                  f'{level} {rnd or ""}'.strip())


# --------------------------------------------------------------------------- #
# Top-level entry
# --------------------------------------------------------------------------- #
def compute_context(payload: dict, surface: str,
                    as_of: Optional[date] = None,
                    target_level: Optional[str] = None,
                    target_round: Optional[str] = None) -> ContextReport:
    """Build the situational-adjustment report for one player.

    `payload` is a parse_player_payload() dict (needs 'historical', 'surf',
    'surf52'). `surface` is the upcoming-match surface. `as_of` defaults to
    today, so 'days idle' and the fatigue/surface windows are measured up to the
    present."""
    as_of = as_of or date.today()
    matches = payload.get('historical', []) or []

    # Most recent completed match date -> idle days.
    dates = sorted((d for d in (_parse_date(m.get('date')) for m in matches) if d),
                   reverse=True)
    last = dates[0] if dates else None
    days_idle = (as_of - last).days if last else None

    factors = [
        _rest_rust(days_idle),
        _fatigue(matches, as_of),
        _surface_adapt(matches, surface, as_of),
        _serve_form(payload.get('surf52', {}), payload.get('surf', {}), surface),
        _importance(target_level, target_round),
    ]
    net = sum(f.delta for f in factors)
    return ContextReport(factors=factors, net=net, last_match=last,
                         n_matches=len(matches))


def adjusted_win_prob(elo_a: float, elo_b: float,
                      ctx_a: ContextReport, ctx_b: ContextReport):
    """Elo win prob for A after applying each player's net context delta.

    Returns (prob_a, eff_elo_a, eff_elo_b)."""
    ea = (elo_a or 1500.0) + ctx_a.net
    eb = (elo_b or 1500.0) + ctx_b.net
    return tennis_sim.elo_win_prob(ea, eb), ea, eb
