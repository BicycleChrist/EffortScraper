"""3D pitch-flight viewer for the props window's SP FORM panel.

Pops out from the movement scatter (click a pitch's mean dot) and animates
the starter's REAL arsenal — each pitch type's average Hawk-Eye kinematics
from the season pitch-detail cache. "Fly Arsenal" sequences every pitch and
keeps the colored trails overlaid: the tunnel and the break separation the
way the batter sees it.

Rendering is homerunwidget's UmpireView3D — same sky shader, lighting,
ball/trail/shadow pipeline and coordinate world as the HR widget — minus
the stadium: the OBJ load is stubbed out and paintGL draws a minimal
mound-to-plate scene (ground, dirt circles, plate, strike zone) instead of
the ballpark. Cameras are preset physics-coordinate views run through
physics_to_model, plus free mouse orbit (drag) and dolly (wheel).
"""

import json
import math
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QPushButton, QSlider,
    QLabel, QComboBox, QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer
from OpenGL.GL import *
from OpenGL.GLU import *

from pitch_sim import savant_pitch_trajectory
from homerunwidget import UmpireView3D

FT_TO_M = 1 / 3.28084


def _hex_to_gl(color_hex: str):
    h = color_hex.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _norm(v):
    m = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / m, v[1] / m, v[2] / m)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _lerp3(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


def _smoothstep(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _smootherstep(t):
    """Quintic ease — C2, where `_smoothstep` is only C1.

    Used where a blend WINDOW opens or closes inside the swing: the probe reads
    |d2p/ds2|, so a C1 join shows up as a spike at the window edge even though
    the motion looks continuous. Blending the follow-through's two grip
    reconciliations with the cubic put a 216 bump exactly at the window end."""
    t = max(0.0, min(1.0, t))
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)



# ---------------------------------------------------------------------------
# The barrel path — a cubic Bezier, parameterised by ARC LENGTH.
#
# Arc length is the whole point: Savant's swing length is a distance travelled,
# and its bat speed is a speed along that distance. Parameterise by arc length
# and both become exact by construction instead of by search — the shape solve
# only has to hit the total length, and the speed profile is then the kinematic
# sequence applied to it.
#
# The inversion (arc length -> Bezier parameter) is a table built once per
# solve. It is interpolated with a CUBIC Hermite in sigma, not linearly: the
# slope du/dsigma = 1/|P'(u)| is known exactly at every node, so the inverse is
# C1 and the barrel does not pick up a kink at every table node. A linear
# inversion shows up directly in the |d2p/ds2| the probe measures.
_ARC_N = 96
_GL3 = ((-0.7745966692414834, 0.5555555555555556),
        (0.0, 0.8888888888888888),
        (0.7745966692414834, 0.5555555555555556))


def _bez(Q, u):
    a = 1.0 - u
    w0, w1, w2, w3 = a * a * a, 3.0 * a * a * u, 3.0 * a * u * u, u * u * u
    return (w0 * Q[0][0] + w1 * Q[1][0] + w2 * Q[2][0] + w3 * Q[3][0],
            w0 * Q[0][1] + w1 * Q[1][1] + w2 * Q[2][1] + w3 * Q[3][1],
            w0 * Q[0][2] + w1 * Q[1][2] + w2 * Q[2][2] + w3 * Q[3][2])


def _bez_d(Q, u):
    a = 1.0 - u
    w0, w1, w2 = 3.0 * a * a, 6.0 * a * u, 3.0 * u * u
    return (w0 * (Q[1][0] - Q[0][0]) + w1 * (Q[2][0] - Q[1][0])
            + w2 * (Q[3][0] - Q[2][0]),
            w0 * (Q[1][1] - Q[0][1]) + w1 * (Q[2][1] - Q[1][1])
            + w2 * (Q[3][1] - Q[2][1]),
            w0 * (Q[1][2] - Q[0][2]) + w1 * (Q[2][2] - Q[1][2])
            + w2 * (Q[3][2] - Q[2][2]))


def _bez_arc_table(sg):
    """Cumulative arc length at _ARC_N+1 nodes, by 3-point Gauss-Legendre per
    segment. Returns the total length, which is the quantity the shape solve
    bisects against the measured swing length."""
    Q = (sg["Q0"], sg["Q1"], sg["Q2"], sg["Q3"])
    n, h = _ARC_N, 1.0 / _ARC_N
    us = [k * h for k in range(n + 1)]
    sp = []
    for u in us:
        d = _bez_d(Q, u)
        sp.append(math.sqrt(_dot(d, d)))
    cum = [0.0] * (n + 1)
    for k in range(n):
        tot = 0.0
        for x, w in _GL3:
            d = _bez_d(Q, us[k] + h * 0.5 * (x + 1.0))
            tot += w * math.sqrt(_dot(d, d))
        cum[k + 1] = cum[k] + tot * h * 0.5
    sg["arc_u"], sg["arc_s"], sg["arc_sp"] = us, cum, sp
    sg["L"] = cum[-1]
    return cum[-1]


# ---------------------------------------------------------------------------
# Swing timing — the kinematic sequence.
#
# A competitive downswing is ~150 ms from the start of the barrel's move to
# contact, with ~120 ms of follow-through after it. Within that window the
# body fires in a fixed order: the pelvis peaks first (~650 deg/s), then the
# torso (~800), then the arm, then the hands (~1500), and each segment
# decelerates in the same order so energy passes up the chain. We model each
# segment as a logistic whose midpoint IS its peak-velocity instant, so the
# ordering below literally is the kinematic sequence. The wrists go last —
# the "late hit" that makes the barrel accelerate into the zone instead of
# sweeping at a constant rate (the old linear phase, which is what read as
# robotic).
_DOWNSWING_S = 0.140      # reference only — solved per hitter
_FOLLOW_S = 0.120
_CONTACT_P = _DOWNSWING_S / (_DOWNSWING_S + _FOLLOW_S)

_SEQ_PELVIS = 0.34
_SEQ_TORSO = 0.46
_SEQ_WRIST = 0.86
_SEQ_BARREL = 0.78      # solved, not picked — see the timing note below
_SEQ_K = 7.0

# Sanity anchor for the whole timing model. RE-DERIVED TWICE, and the second
# time against real swings rather than against prose.
#
# Savant's note that bat tracking begins "generally around 150 ms" before impact
# was read as meaning that window IS the downswing, and `_SEQ_BARREL` was set to
# 0.95 to make `t_down` land there. Driveline's landmark data says otherwise:
# the time a real hitter takes to cover the 1.841 m of sweet-spot path that
# Savant reports as 7.2 ft of bat head is **92 ms** (p10 78, p90 150), because
# tracking starts while the bat is barely moving and little path accumulates
# early. The window is a duration of DATA, not of swing.
#
# So the ramp is much less extreme than the 0.95 version: mean speed over the
# window is 1.841/0.092 = 20 m/s against ~32 at contact, a 63% ratio, where
# 0.95 implies 38%. `_SEQ_BARREL` = 0.78 puts `t_down` at 96 ms median against
# the measured 92. The barrel is still ACCELERATING at contact — the
# pelvis/torso/arm have peaked and are decelerating, the hands peak last, at
# the ball — just not as violently as the misreading implied.
_MPH = 0.44704


def _seq(tau, c, k=_SEQ_K):
    """Normalized logistic on tau in [0,1]: 0 at tau=0, exactly 1 at tau=1,
    peak rate at tau=c. One segment of the kinematic sequence."""
    tau = max(0.0, min(1.0, tau))
    f = lambda x: 1.0 / (1.0 + math.exp(-k * (x - c)))
    f0, f1 = f(0.0), f(1.0)
    return (f(tau) - f0) / max(1e-9, f1 - f0)


def _seq_d(tau, c, k=_SEQ_K):
    """d/dtau of `_seq` — needed at tau=1 to match the barrel's tangent to the
    measured attack angle."""
    f = lambda x: 1.0 / (1.0 + math.exp(-k * (x - c)))
    f0, f1 = f(0.0), f(1.0)
    ft = f(max(0.0, min(1.0, tau)))
    return k * ft * (1.0 - ft) / max(1e-9, f1 - f0)


# ---------------------------------------------------------------------------
# Per-moment batting-stance foot data (was batting_stance_data.py).
#
# Reads complete_batting_stances.json (produced by savant_stance_scraper.py)
# and serves it in this viewer's physics frame, so the batter overlay can
# animate a hitter's REAL stride instead of a generic one. Savant tracks the
# feet at three moments — in the stance, at pitch release, and at bat-ball
# intercept. Release matters on its own: for ~27% of hitters it sits well off
# the straight line between stance and intercept (big leg-kick guys have the
# front foot in the air, swung back toward the catcher), so it cannot be
# reconstructed by interpolating the endpoints.
#
# Scrape frame : (off_plate, depth) in inches inside the batter's box, plus the
#                cleat image's SVG rotation.
# Physics frame: X toward the pitcher, Y up, Z toward 1B — the same mapping the
#                overlay already uses for depth_in_box / dist_off_plate.
_STANCE_IN = 0.0254
_STANCE_DATA_PATH = Path(__file__).resolve().parent / "complete_batting_stances.json"

# ---------------------------------------------------------------------------
# Reference swing — MOTION WARPING.
#
# `swing_reference.json` is CMU Graphics Lab mocap trial 124_07 ("Baseball
# Swing"), run through forward kinematics, normalised (pelvis at the origin,
# feet axis onto +X, scaled to this figure's hip/shoulder heights) and
# resampled onto swing phase with contact pinned at 0.538. 61 samples.
#
# Why this and not more solving: three separate attempts to place the body
# analytically all put the bat through the hitter, because the barrel arc is
# solved from Savant's measurements and the body was placed independently —
# nothing tied them together. A recorded swing is natural by construction, so
# the body and the hand PATH come from it, and the measured arc keeps the
# barrel. The two meet at the one constraint that actually links them: the bat
# is rigid, so the grip must sit BAT_SWEET from the sweet spot. The mocap hand
# is projected onto that sphere — it keeps the real path's shape and stays out
# of the body, while the barrel stays exactly where the measurements put it.
#
# Measured against the model it replaces: the mocap hand path runs 46-58 cm
# from the spine; the analytic one ran 17-20 cm.
_SWING_REF_PATH = Path(__file__).resolve().parent / "swing_reference.json"
_SWING_REF = None


def swing_reference():
    """Lazy-load the reference swing; None if the file is missing (the figure
    then falls back to the analytic pose)."""
    global _SWING_REF
    if _SWING_REF is None:
        try:
            import json as _json
            with open(_SWING_REF_PATH) as fh:
                _SWING_REF = _json.load(fh)
        except Exception as e:
            print(f"sp_flight_viewer: reference swing unavailable: {e}")
            _SWING_REF = False
    return _SWING_REF or None


def swing_ref_at(s):
    """Reference joints at swing phase `s`, linearly interpolated.

    Returns a dict of (x, y, z) in the reference frame: +X toward the pitcher,
    +Y up, +Z toward the PLATE, origin at the hitter's pelvis on the ground."""
    ref = swing_reference()
    if not ref:
        return None
    fr = ref["frames"]
    n = len(fr)
    t = max(0.0, min(1.0, s)) * (n - 1)
    i1 = int(t)
    i2 = min(n - 1, i1 + 1)
    i0 = max(0, i1 - 1)
    i3 = min(n - 1, i2 + 1)
    a = t - i1
    # CATMULL-ROM, not linear. Linear interpolation of the samples is only C0,
    # so every one of the 61 sample boundaries is a kink in velocity — that
    # alone tripled the figure's median |d2p/ds2| (10 -> 30) when the
    # reference was first wired in. A cubic through the neighbouring samples
    # is C1 and costs nothing.
    a2 = a * a
    a3 = a2 * a
    c0 = -0.5 * a3 + a2 - 0.5 * a
    c1 = 1.5 * a3 - 2.5 * a2 + 1.0
    c2 = -1.5 * a3 + 2.0 * a2 + 0.5 * a
    c3 = 0.5 * a3 - 0.5 * a2
    out = {}
    for k in fr[i1]:
        if k == "s":
            continue
        p0, p1, p2, p3 = fr[i0][k], fr[i1][k], fr[i2][k], fr[i3][k]
        out[k] = tuple(c0 * p0[d] + c1 * p1[d] + c2 * p2[d] + c3 * p3[d]
                       for d in range(3))
    return out


# ---------------------------------------------------------------------------
# The barrel path — MEASURED, from 673 real swings.
#
# Source: Driveline OpenBiomechanics (`hitting_landmarks.zip`, dataset-v1),
# CC BY-NC-SA 4.0. The `sweet_spot_*` channel is the barrel itself, so the path
# does not have to be invented: for every swing, walk back from contact until
# 1.841 m of sweet-spot path has accumulated (= the 7.2 ft of BAT HEAD that
# Savant reports), express it in cylindrical coordinates about that hitter's own
# spine axis, and take the median shape.
#
# What comes out is a SPIRAL, and it is nothing like the fixed-radius circle
# this model used to swing. Over Savant's window the barrel goes from 36% of its
# contact radius and 71 cm ABOVE contact, winding out and down, bottoming out
# 3 cm below the ball at u≈0.87 and rising into it — that last dip is the attack
# angle, and it falls out of the data rather than being imposed.
#
# The circle was the whole reason the bat swept through the hitter. A real
# barrel NEVER comes near the body: minimum distance from the sweet spot to the
# thorax axis is p10 33.9 cm / median 39.7, and 0 of 675 swings come inside
# 17 cm. Ours ran a median 6.9 cm, min 0.6. In these coordinates clearance is
# just `rho >= CLEAR_MIN` and the profile satisfies it by construction — the
# path can no longer pass through the hitter no matter what the solve does.
#
# Sweep over the window is 165.1 deg (p10 137, p90 197) against the 92 deg the
# circle needed for the same path length, i.e. the old swing radius was far too
# big. Coordinates are the SWING PLANE's: the axis is the plane normal, not
# vertical. A first pass used a vertical axis and had to bolt the tilt on
# afterwards through an out-of-plane correction that grew to 21 cm and buckled
# the path. Savant's `swing_tilt` IS this plane, and the measured plane tilt
# (median 31.9 deg) sits right inside the board's range, which is the check
# that the two definitions are the same thing.
_OBP_U = 33
_OBP_R = (
    +0.3827, +0.3880, +0.3900, +0.3912, +0.3971, +0.4017,
    +0.4093, +0.4169, +0.4249, +0.4377, +0.4510, +0.4644,
    +0.4784, +0.4942, +0.5101, +0.5277, +0.5482, +0.5710,
    +0.5910, +0.6149, +0.6384, +0.6627, +0.6910, +0.7188,
    +0.7466, +0.7763, +0.8068, +0.8395, +0.8716, +0.9037,
    +0.9361, +0.9686, +1.0000)
# OUT-OF-PLANE drift, metres. The swing is not planar early — the barrel sits
# 28 cm out of the plane it will finish in — but it converges to the plane with
# zero slope by contact, which is exactly why Savant can define swing tilt over
# the last 40 ms. The old code faked this with a `roll` that stood the early
# plane up; here it is measured.
_OBP_H = (
    +0.2840, +0.2637, +0.2475, +0.2319, +0.2166, +0.2055,
    +0.1942, +0.1846, +0.1725, +0.1618, +0.1496, +0.1398,
    +0.1275, +0.1166, +0.1074, +0.0961, +0.0859, +0.0753,
    +0.0657, +0.0566, +0.0475, +0.0403, +0.0338, +0.0272,
    +0.0214, +0.0159, +0.0108, +0.0073, +0.0044, +0.0018,
    +0.0005, +0.0001, +0.0000)
# Measured contact radius about the swing axis (m): median 0.969, p10 0.855,
# p90 1.056. Not used as an input — the solve derives it — but it is the number
# the derived value is checked against.
_OBP_RC = 0.969
# Measured horizontal offset from the hitter's THORAX at contact to the contact
# point: 0.528 m toward the pitcher (and 0.704 toward the plate, 0.420 below the
# thorax midpoint). Used to reconcile Savant's stance-referenced batter position
# with where the hitter's body actually is when he hits the ball.
_OBP_CONTACT_FWD = 0.528
_OBP_SWEEP = math.radians(165.1)
CLEAR_MIN = 0.34


def _obp_at(tab, u):
    """Sample a measured profile at normalised sweep `u` (Catmull-Rom)."""
    n = len(tab)
    t = max(0.0, min(1.0, u)) * (n - 1)
    i1 = int(t)
    i2 = min(n - 1, i1 + 1)
    i0 = max(0, i1 - 1)
    i3 = min(n - 1, i2 + 1)
    a = t - i1
    a2, a3 = a * a, a * a * a
    return (tab[i0] * (-0.5 * a3 + a2 - 0.5 * a)
            + tab[i1] * (1.5 * a3 - 2.5 * a2 + 1.0)
            + tab[i2] * (-1.5 * a3 + 2.0 * a2 + 0.5 * a)
            + tab[i3] * (0.5 * a3 - 0.5 * a2))


_REF_YAW = None


def swing_ref_yaw():
    """Rotation PROGRESS of the pelvis and the shoulders over the reference
    swing, each normalised to 0 at the load and 1 at the finish.

    Only the timing is taken, not the amplitude or the direction: those are
    this figure's own (validated handedness, validated load/finish poses), and
    swapping both at once would make a regression impossible to attribute. What
    the reference supplies is the SHAPE of the two curves — a real hitter's
    pelvis and shoulders do not follow tidy logistics, and the gap between them
    is the X-factor. Measured on the reference: the shoulders turn 177 deg and
    the hips 142 deg, separated by ~20 deg at the load.

    Returns (pelvis, shoulder) lists of `n` samples on uniform phase, or None."""
    global _REF_YAW
    if _REF_YAW is None:
        ref = swing_reference()
        if not ref:
            _REF_YAW = False
        else:
            def curve(a, b):
                out, prev = [], None
                for fr in ref["frames"]:
                    p, q = fr[a], fr[b]
                    th = math.atan2(p[2] - q[2], p[0] - q[0])
                    if prev is not None:            # unwrap
                        while th - prev > math.pi:
                            th -= 2 * math.pi
                        while th - prev < -math.pi:
                            th += 2 * math.pi
                    prev = th
                    out.append(th)
                span = out[-1] - out[0]
                if abs(span) < 1e-6:
                    return [k / (len(out) - 1) for k in range(len(out))]
                return [(t - out[0]) / span for t in out]
            _REF_YAW = (curve("hip_l", "hip_r"), curve("sh_l", "sh_r"))
    return _REF_YAW or None


def _ref_curve_at(curve, s):
    """Sample a normalised reference curve at phase `s` (Catmull-Rom, to match
    `swing_ref_at` — a linear read is only C0 and kinks the yaw rate)."""
    n = len(curve)
    t = max(0.0, min(1.0, s)) * (n - 1)
    i1 = int(t)
    i2 = min(n - 1, i1 + 1)
    i0 = max(0, i1 - 1)
    i3 = min(n - 1, i2 + 1)
    a = t - i1
    a2, a3 = a * a, a * a * a
    return (curve[i0] * (-0.5 * a3 + a2 - 0.5 * a)
            + curve[i1] * (1.5 * a3 - 2.5 * a2 + 1.0)
            + curve[i2] * (-1.5 * a3 + 2.0 * a2 + 0.5 * a)
            + curve[i3] * (0.5 * a3 - 0.5 * a2))


def _stance_foot_to_physics(f, bside):
    """One scraped foot → (x, z, axis) in physics metres.

    The cleat image's long axis points +y at rot=0 and SVG rotate(a) sends
    (0,1) → (-sin a, cos a). Carrying that through the panel mirroring and the
    bside flip, both hands collapse to the same expression. A foot is drawn as a
    symmetric segment, so only the axis matters, not its sign.
    """
    a = math.radians(f["rot_deg"] or 0.0)
    return (0.22 - f["depth_in"] * _STANCE_IN,        # X: toward the pitcher
            bside * f["off_plate_in"] * _STANCE_IN,   # Z: toward 1B
            (-math.cos(a), -math.sin(a)))             # foot axis in (X, Z)


class _StanceMoments:
    """Lazy singleton-ish reader over the scraped stance file."""

    def __init__(self, path: Path = _STANCE_DATA_PATH):
        self._path = path
        self._players: Optional[dict] = None

    def _load(self) -> dict:
        if self._players is None:
            try:
                self._players = json.loads(self._path.read_text())["players"]
            except Exception as e:
                print(f"sp_flight_viewer: no stance file ({e}) — the batter "
                      f"overlay will fall back to a generic stride")
                self._players = {}
        return self._players

    def get(self, mlbam: int, side: str) -> Optional[dict]:
        """Foot moments for one batter-side, already in physics coords.

        Returns {moment: {"front": (x, z, axis), "back": (x, z, axis)}} for
        whichever moments were captured, or None when the hitter isn't in the
        scrape. Switch hitters have a record per side; everyone else has one, so
        we fall back to the other side rather than dropping the hitter.
        """
        players = self._load()
        if not players:
            return None
        side = (side or "R").upper()[:1]
        rec = players.get(f"{mlbam}_{side}")
        if rec is None:                    # single-sided hitter, side mismatch
            other = "L" if side == "R" else "R"
            rec = players.get(f"{mlbam}_{other}")
        if rec is None:
            return None

        bside = -1.0 if side == "R" else 1.0    # RHB → 3B (−Z), LHB → 1B (+Z)
        out = {}
        for moment, feet in (rec.get("moments") or {}).items():
            if len(feet) != 2:
                continue
            # front foot is the one nearer the pitcher = smaller depth in box
            near, far = sorted(feet, key=lambda f: f["depth_in"])
            out[moment] = {"front": _stance_foot_to_physics(near, bside),
                           "back": _stance_foot_to_physics(far, bside)}
        return out or None


_STANCE_STORE = _StanceMoments()


def get_stance_moments(mlbam, side):
    """Per-moment foot positions (stance / pitch release / bat-ball intercept)
    for one batter, in this viewer's physics frame. None when the hitter isn't
    in the scrape — callers fall back to a generic stride."""
    try:
        return _STANCE_STORE.get(int(mlbam), side)
    except (TypeError, ValueError):
        return None


class PitchFlightView(UmpireView3D):
    """UmpireView3D without the stadium: minimal plate/mound scene tuned
    for pitch flight, per-dot colored trails for arsenal overlays, preset +
    orbit cameras. All rendering constants (ball, shadow, sky, lighting)
    are the HR widget's own."""

    # Camera presets in PHYSICS coordinates (meters): X toward the mound,
    # Y up, Z toward 1B. Transformed through physics_to_model at use time.
    CAMERA_PRESETS = {
        # Pulled back + slightly higher so the batter in the box is framed
        "Umpire":    {"pos": (-3.1, 1.95, 0.0), "target": (17.0, 1.35, 0.0),
                      "fov": 55},
        "Batter":    {"pos": (-0.3, 1.70, -0.85), "target": (17.5, 1.7, 0.0),
                      "fov": 62},
        "Pitcher":   {"pos": (19.6, 1.9, 0.0), "target": (0.0, 0.9, 0.0),
                      "fov": 50},
        # +Z is toward 1B (same convention as bside / the stance scrape), so
        # these two were labelled backwards: picking "3B Side" put the camera
        # on the FIRST-base side. With a left-handed hitter — who stands on the
        # 1B side — that reads as the batter being in the wrong box entirely.
        "1B Side":   {"pos": (9.2, 3.2, 11.0), "target": (9.2, 1.6, 0.0),
                      "fov": 42},
        "3B Side":   {"pos": (9.2, 3.2, -11.0), "target": (9.2, 1.6, 0.0),
                      "fov": 42},
        "Broadcast": {"pos": (23.0, 4.5, -4.0), "target": (3.0, 1.2, 0.0),
                      "fov": 30},
        "Overhead":  {"pos": (9.2, 14.0, 0.0), "target": (9.2, 0.0, 0.0),
                      "fov": 45, "up": (1.0, 0.0, 0.0)},
    }

    def _load_model_bg(self):
        # No ballpark OBJ in the flight viewer — the stadium (and its
        # display-list path in our paintGL) never exists
        self._model_load_pending = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._night_mode = False
        self.show_zone = True
        # Release-point overlay: per-pitch release markers + arm-slot vector.
        # Populated by SPFlightWindow after construction.
        self.show_release = True
        self.release_pitches = []
        self.release_colors = {}
        self.release_abbrev = {}
        self.arm_angle = None
        # Batter-in-the-box overlay (combo pitcher-vs-batter view)
        self.show_batter = False
        self.batter = None
        # Swing animation progress 0..1 (None = loaded, pre-swing). The barrel
        # reaches the ball at _CONTACT_P; SPFlightWindow drives it.
        self._swing_phase = None
        self._recover = None        # 0..1 ease from the finish back to load
        self._sg_cache = None       # (key, solved swing) — the solve iterates
        # Foot progress runs on the PITCH clock, not the swing clock: the
        # scraped moments are keyed to pitch release and bat-ball intercept.
        # None = show the batting stance.
        self._foot_phase = None
        self.sz_top_m = 3.4 * FT_TO_M
        self.sz_bot_m = 1.6 * FT_TO_M
        # Persistent-arsenal trail: spacing in model units (~0.3 ≈ 13cm of
        # flight ≈ ~140 dots per pitch at full frame rate)
        self.trail_min_dist = 0.3
        self._drag_last = None
        self.set_camera_preset("Umpire")
        self.setMinimumSize(720, 460)

    # ------------------------------------------------------------- cameras

    def _pm_dir(self, dx, dy, dz):
        """Rotate a physics-space direction into model space (no offset)."""
        return ((dx * self._model_cos - dz * self._model_sin),
                dy,
                (dx * self._model_sin + dz * self._model_cos))

    def set_camera_preset(self, name):
        p = self.CAMERA_PRESETS.get(name)
        if not p:
            return
        self.camera = {
            "pos": list(self.physics_to_model(*p["pos"])),
            "target": list(self.physics_to_model(*p["target"])),
            "up": list(self._pm_dir(*p.get("up", (0.0, 1.0, 0.0)))),
            "fov": p["fov"],
        }
        self.update()

    # Free-look: drag orbits the camera around its target, wheel dollies
    def mousePressEvent(self, ev):
        self._drag_last = ev.position()

    def mouseMoveEvent(self, ev):
        if self._drag_last is None:
            return
        d = ev.position() - self._drag_last
        self._drag_last = ev.position()
        px, py, pz = self.camera["pos"]
        tx, ty, tz = self.camera["target"]
        vx, vy, vz = px - tx, py - ty, pz - tz
        # Yaw around Y
        yaw = -d.x() * 0.006
        c, s = math.cos(yaw), math.sin(yaw)
        vx, vz = vx * c - vz * s, vx * s + vz * c
        # Pitch (clamped so the camera never flips over the pole)
        r_xz = math.hypot(vx, vz)
        pitch = math.atan2(vy, r_xz) + d.y() * 0.006
        pitch = max(-1.35, min(1.45, pitch))
        r = math.sqrt(vx * vx + vy * vy + vz * vz)
        vy = r * math.sin(pitch)
        scale_xz = (r * math.cos(pitch)) / max(r_xz, 1e-6)
        vx, vz = vx * scale_xz, vz * scale_xz
        self.camera["pos"] = [tx + vx, ty + vy, tz + vz]
        self.update()

    def mouseReleaseEvent(self, ev):
        self._drag_last = None

    def wheelEvent(self, ev):
        factor = 0.9 if ev.angleDelta().y() > 0 else 1.12
        px, py, pz = self.camera["pos"]
        tx, ty, tz = self.camera["target"]
        self.camera["pos"] = [tx + (px - tx) * factor,
                              ty + (py - ty) * factor,
                              tz + (pz - tz) * factor]
        self.update()

    # ------------------------------------------------------------ painting

    def clear_trails(self):
        self.ball_trail = []
        self.prev_ball_pos = None
        self.ball_pos = None
        self.ball_vel = None
        self._swing_phase = None
        self.update()

    # Tron scene palette
    _GRID_RGB = (0.16, 0.82, 0.95)   # cyan grid + mound contours
    _PLATE_RGB = (0.92, 0.95, 1.0)   # cool white plate/rubber

    def _p2m(self, px, py, pz):
        return self.physics_to_model(px, py, pz)

    def _draw_tron_grid(self):
        """Neon ground grid on black — receding into darkness. Lines fade
        with distance from the plate→mound corridor so the far field dissolves
        instead of hard-clipping."""
        r, g, b = self._GRID_RGB
        x0, x1, z0, z1 = -4.0, 23.0, -10.0, 10.0
        step = 1.524   # 5-ft cells
        maxd = 26.0

        def vtx(px, pz):
            d = math.hypot(px - 9.5, pz)       # from mid-corridor
            a = max(0.0, 0.40 * (1.0 - d / maxd))
            glColor4f(r, g, b, a)
            glVertex3f(*self._p2m(px, 0.0, pz))

        glPushAttrib(GL_LIGHTING_BIT | GL_ENABLE_BIT | GL_LINE_BIT
                     | GL_CURRENT_BIT)
        glDisable(GL_LIGHTING)
        glEnable(GL_BLEND)
        glLineWidth(1.1)
        glBegin(GL_LINES)
        z = z0
        while z <= z1 + 1e-6:
            vtx(x0, z)
            vtx(x1, z)
            z += step
        x = x0
        while x <= x1 + 1e-6:
            vtx(x, z0)
            vtx(x, z1)
            x += step
        glEnd()
        # Brighter center line down the plate→mound axis
        glLineWidth(1.6)
        glBegin(GL_LINES)
        glColor4f(r, g, b, 0.5)
        glVertex3f(*self._p2m(x0, 0.005, 0.0))
        glColor4f(r, g, b, 0.10)
        glVertex3f(*self._p2m(x1, 0.005, 0.0))
        glEnd()
        glPopAttrib()

    def _draw_mound(self):
        """Pitcher's mound as a solid translucent dome (dark teal, brighter
        toward the crown) with glowing contour rings riding on the surface, a
        bright footprint rim, and a glowing white rubber. Real geometry: 18-ft
        circle, 10-in peak, 5-ft flat table, 1-in/ft slope."""
        r, g, b = self._GRID_RGB
        cx = 18.0            # mound center (~59 ft from plate)
        R = 2.74            # 9-ft radius
        peak = 0.254        # 10 in
        plateau = 0.76      # 2.5-ft table radius
        N = 48

        def h_at(rr):
            if rr <= plateau:
                return peak
            return max(0.0, peak - (rr - plateau) * (0.0254 / 0.3048))

        def ring(rr, lift=0.0):
            h = h_at(rr) + lift
            return [self._p2m(cx + rr * math.cos(2 * math.pi * k / N), h,
                              rr * math.sin(2 * math.pi * k / N))
                    for k in range(N + 1)]

        radii = [0.0, 0.4, plateau, 1.15, 1.6, 2.05, 2.45, R]
        rings = [ring(rr) for rr in radii]

        glPushAttrib(GL_LIGHTING_BIT | GL_ENABLE_BIT | GL_LINE_BIT
                     | GL_CURRENT_BIT)
        glDisable(GL_LIGHTING)
        glEnable(GL_BLEND)

        # 1) Solid dome surface — dark teal, alpha fades from crown to rim so
        #    it dissolves into the grid at the base instead of a hard disk
        fr, fg, fb = 0.05, 0.17, 0.20
        fa = lambda rr: 0.10 + 0.36 * (1.0 - rr / R)
        for i in range(len(radii) - 1):
            a0, a1 = fa(radii[i]), fa(radii[i + 1])
            r0, r1 = rings[i], rings[i + 1]
            glBegin(GL_TRIANGLE_STRIP)
            for k in range(N + 1):
                glColor4f(fr, fg, fb, a0)
                glVertex3f(*r0[k])
                glColor4f(fr, fg, fb, a1)
                glVertex3f(*r1[k])
            glEnd()

        # 2) Glowing contour rings, lifted a hair so they sit ON the surface
        glLineWidth(1.3)
        for rr in radii[1:]:
            glColor4f(r, g, b, max(0.14, 0.5 * (1.0 - rr / (R * 1.4))))
            glBegin(GL_LINE_STRIP)
            for p in ring(rr, lift=0.004):
                glVertex3f(*p)
            glEnd()

        # 3) Bright footprint rim where the mound meets the field
        glLineWidth(2.2)
        glColor4f(r, g, b, 0.72)
        glBegin(GL_LINE_STRIP)
        for p in ring(R, lift=0.004):
            glVertex3f(*p)
        glEnd()

        # 4) Rubber: 24" x 6" white slab + glowing outline atop the table
        pr, pg, pb = self._PLATE_RGB
        rub = [self._p2m(px, peak + 0.014, pz) for px, pz in (
            (18.44 - 0.076, 0.305), (18.44 + 0.076, 0.305),
            (18.44 + 0.076, -0.305), (18.44 - 0.076, -0.305))]
        glColor4f(pr, pg, pb, 0.95)
        glBegin(GL_POLYGON)
        for v in rub:
            glVertex3f(*v)
        glEnd()
        glLineWidth(1.6)
        glColor4f(1.0, 1.0, 1.0, 1.0)
        glBegin(GL_LINE_LOOP)
        for v in rub:
            glVertex3f(*v)
        glEnd()
        glPopAttrib()

    def _draw_home_plate(self):
        """Real home-plate pentagon (17" front edge toward the mound, point
        toward the catcher), filled cool-white with a brighter glowing rim."""
        pr, pg, pb = self._PLATE_RGB
        h = self._model_hp_lift if hasattr(self, "_model_hp_lift") else 0.012
        # physics coords (m): X toward mound, Z toward 1B; plate at origin
        pts = [(0.216, -0.216), (0.216, 0.216), (0.0, 0.216),
               (-0.216, 0.0), (0.0, -0.216)]
        verts = [self._p2m(px, h, pz) for px, pz in pts]
        glPushAttrib(GL_LIGHTING_BIT | GL_ENABLE_BIT | GL_LINE_BIT
                     | GL_CURRENT_BIT)
        glDisable(GL_LIGHTING)
        glEnable(GL_BLEND)
        glColor4f(pr, pg, pb, 0.9)
        glBegin(GL_POLYGON)
        for v in verts:
            glVertex3f(*v)
        glEnd()
        glLineWidth(2.0)
        glColor4f(1.0, 1.0, 1.0, 1.0)
        glBegin(GL_LINE_LOOP)
        for v in verts:
            glVertex3f(*v)
        glEnd()
        glPopAttrib()

    def _release_model_pt(self, kin):
        """Savant release_pos (ft) → model coords. Savant y=dist-from-plate →
        physics X, z=height → physics Y, x=lateral → physics Z."""
        return self.physics_to_model(kin["release_pos_y"] * FT_TO_M,
                                     kin["release_pos_z"] * FT_TO_M,
                                     kin["release_pos_x"] * FT_TO_M)

    def _draw_release_markers(self):
        """Per-pitch release-point cluster (glowing spheres in pitch color +
        a faint drop line to the ground) and one arm-slot vector at the mean
        release point, drawn at the pitcher's Statcast arm angle."""
        pitches = [p for p in self.release_pitches if p.get("kin")]
        if not pitches:
            return
        pts = []   # (model_pt, phys_lat) per pitch, for the arm-slot mean

        # Glowing spheres (lit + emissive, like the trail dots)
        for p in pitches:
            kin = p["kin"]
            code = self.release_abbrev.get(p["pitch"], p["pitch"])
            r, g, b = _hex_to_gl(self.release_colors.get(code, "#95A5A6"))
            mx, my, mz = self._release_model_pt(kin)
            pts.append(((mx, my, mz), kin["release_pos_x"]))
            glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [r, g, b, 1.0])
            glMaterialfv(GL_FRONT, GL_SPECULAR, [1.0, 1.0, 1.0, 1.0])
            glMaterialf(GL_FRONT, GL_SHININESS, 70.0)
            glMaterialfv(GL_FRONT, GL_EMISSION,
                         [r * 0.5, g * 0.5, b * 0.5, 1.0])
            glPushMatrix()
            glTranslatef(mx, my, mz)
            sph = gluNewQuadric()
            gluQuadricNormals(sph, GLU_SMOOTH)
            gluSphere(sph, 0.075, 12, 12)
            gluDeleteQuadric(sph)
            glPopMatrix()
        glMaterialfv(GL_FRONT, GL_EMISSION, [0.0, 0.0, 0.0, 1.0])

        # Drop lines + arm-slot vector: unlit
        glPushAttrib(GL_LIGHTING_BIT | GL_ENABLE_BIT | GL_LINE_BIT
                     | GL_CURRENT_BIT)
        glDisable(GL_LIGHTING)
        glEnable(GL_BLEND)
        glLineWidth(1.0)
        glColor4f(0.55, 0.62, 0.70, 0.35)
        glBegin(GL_LINES)
        for p in pitches:
            kin = p["kin"]
            mx, my, mz = self._release_model_pt(kin)
            gx, gy, gz = self.physics_to_model(
                kin["release_pos_y"] * FT_TO_M, 0.0,
                kin["release_pos_x"] * FT_TO_M)
            glVertex3f(mx, my, mz)
            glVertex3f(gx, gy, gz)
        glEnd()

        # Arm-slot vector at the mean release point (frontal plane: Y up,
        # Z lateral). Arm angle: 0°=sidearm (horizontal), 90°=over the top.
        if self.arm_angle is not None:
            mean_phys = [
                _avg([p["kin"]["release_pos_y"] for p in pitches]) * FT_TO_M,
                _avg([p["kin"]["release_pos_z"] for p in pitches]) * FT_TO_M,
                _avg([p["kin"]["release_pos_x"] for p in pitches]) * FT_TO_M,
            ]
            th = math.radians(self.arm_angle)
            side = 1.0 if mean_phys[2] >= 0 else -1.0   # arm side (lateral)
            L = 0.62   # ~forearm length in meters
            # shoulder = release, back down the arm toward the body/center
            shoulder = (mean_phys[0],
                        mean_phys[1] - math.sin(th) * L,
                        mean_phys[2] - side * math.cos(th) * L)
            a = self.physics_to_model(*shoulder)
            b = self.physics_to_model(*mean_phys)
            glLineWidth(3.0)
            glColor4f(0.90, 0.62, 0.20, 0.95)   # amber arm-slot line
            glBegin(GL_LINES)
            glVertex3f(*a)
            glVertex3f(*b)
            glEnd()
        glPopAttrib()

    def set_swing_phase(self, p):
        """Swing progress 0..1 (None = loaded, pre-swing)."""
        self._swing_phase = p

    def set_recover(self, r):
        """0..1 blend from the finished follow-through back to the load pose.

        Without this the figure SNAPS: the swing holds at phase 1.0, then the
        next pitch in the queue calls reset_swing() and the pose jumps
        straight from the wrapped finish to the cocked load — the arms
        visibly yanked back across the body in a single frame. It is not a
        defect of the swing solve, which is why it never showed up in the
        pose-continuity numbers: both poses are fine, it is the CUT between
        them that reads as the jerk."""
        self._recover = r

    def reset_swing(self):
        self._swing_phase = None
        self._foot_phase = None
        self._recover = None

    def set_foot_phase(self, p):
        """Pitch progress 0..1 from release to bat-ball intercept (None = the
        batting stance). Separate from the swing phase because Savant's foot
        moments are keyed to the pitch, not to the swing."""
        self._foot_phase = p

    def _feet_at_phase(self):
        """The batter's REAL feet at the current pitch phase, interpolated
        across the scraped moments. Returns (front, back) as (x, z, axis) in
        physics metres, or None when this hitter wasn't scraped — in which case
        the caller falls back to a generic stride.

        Release is a genuine waypoint, not a midpoint: for a big leg-kick
        hitter the front foot at release is airborne and swung back toward the
        catcher, nowhere near the stance->intercept line."""
        sm = (self.batter or {}).get("stance_moments")
        if not sm or "stance" not in sm:
            return None
        fp = self._foot_phase
        have_rel, have_int = "release" in sm, "intercept" in sm
        if fp is None:
            a = b = "stance"
            t = 0.0
        elif fp < 0.15 and have_rel:
            # the kick is already up by release; ease in so it doesn't pop
            a, b, t = "stance", "release", fp / 0.15
        elif have_rel and have_int:
            a, b, t = "release", "intercept", (fp - 0.15) / 0.85
        elif have_int:
            a, b, t = "stance", "intercept", fp
        else:
            a, b, t = "stance", "release", min(1.0, fp / 0.15)
        t = max(0.0, min(1.0, t))
        t = t * t * (3.0 - 2.0 * t)
        A, B = sm.get(a) or sm["stance"], sm.get(b) or sm["stance"]
        out = []
        for which in ("front", "back"):
            ax, az, aa = A[which]
            bx, bz, ba = B[which]
            out.append((ax + (bx - ax) * t, az + (bz - az) * t,
                        (aa[0] + (ba[0] - aa[0]) * t,
                         aa[1] + (ba[1] - aa[1]) * t)))
        return out[0], out[1]

    def swing_seconds(self):
        """This hitter's downswing duration (s) — solved from his bat speed and
        swing length, so a short quick swing really is shorter on the clock.
        Falls back to the league reference when he has no tracked swing."""
        b = self.batter
        if not b:
            return _DOWNSWING_S
        bside = -1.0 if b.get("side", "R") == "R" else 1.0
        IN = 0.0254
        bx = 0.22 - (b.get("depth_in_box") or 27.0) * IN
        bz = bside * (b.get("dist_off_plate") or 27.0) * IN
        sg = self._swing_geometry(b, bx, bz, bside)
        return sg["t_down"] if sg else _DOWNSWING_S

    def contact_phase(self):
        """Swing progress 0..1 at which the barrel reaches the ball. Now a
        TIME split, not a geometry one: the downswing lasts ~150 ms and the
        follow-through ~120 ms for every hitter. Swing length changes how far
        the barrel travels in that window (i.e. how fast it moves), not how
        long the swing takes."""
        return _CONTACT_P

    def _seg(self, p, q):
        """Emit one physics-space line segment (call inside a GL_LINES begin)."""
        glVertex3f(*self._p2m(*p))
        glVertex3f(*self._p2m(*q))

    @staticmethod
    def _elbow(S, H, arm=0.74, bend_ref=(0.0, -1.0, 0.0), min_flex=0.10):
        """Elbow of an equal-bone 2-bone arm from shoulder S to hand H.

        The elbow sits on the S→H midplane, pushed off the S→H line along the
        component of `bend_ref` ⟂ that line, so the arm reads as a jointed limb.
        `min_flex` keeps a floor under the bend even when the hand is near full
        reach, so the arm never snaps dead-straight mid-swing (only true
        extension at contact fully straightens it).

        When the hand is beyond the arm's reach the elbow is the true midpoint
        (a single straight arm) — NOT a stub near the shoulder with a long,
        ballooned forearm, which is what made the extended arm look broken."""
        dx, dy, dz = H[0] - S[0], H[1] - S[1], H[2] - S[2]
        d = math.sqrt(dx * dx + dy * dy + dz * dz) or 1e-6
        u = (dx / d, dy / d, dz / d)
        if d >= arm:                                    # beyond reach → straight
            return (S[0] + dx * 0.5, S[1] + dy * 0.5, S[2] + dz * 0.5)
        # geometric bend from the slack in the two equal bones, floored so the
        # limb keeps a little articulation until it truly extends
        bend = math.sqrt(max(0.0, (arm * 0.5) ** 2 - (d * 0.5) ** 2))
        bend = max(bend, min_flex * (1.0 - d / arm))
        ref = bend_ref
        dot = ref[0] * u[0] + ref[1] * u[1] + ref[2] * u[2]
        bx_, by_, bz_ = (ref[0] - dot * u[0], ref[1] - dot * u[1],
                         ref[2] - dot * u[2])
        bl = math.sqrt(bx_ * bx_ + by_ * by_ + bz_ * bz_)
        if bl < 1e-6:                                   # ref parallel to the arm
            bx_, by_, bz_ = u[2], 0.0, -u[0]
            bl = math.hypot(u[2], u[0]) or 1.0
        return (S[0] + u[0] * d * 0.5 + bx_ / bl * bend,
                S[1] + u[1] * d * 0.5 + by_ / bl * bend,
                S[2] + u[2] * d * 0.5 + bz_ / bl * bend)


    # Torso cylinder the grip is warped out of, and the softness of that warp.
    # 0.32 is MEASURED, not guessed: CMU mocap trial 124_07 ("Baseball Swing")
    # run through forward kinematics says a real hitter's hands never come
    # closer than 36cm to his own spine axis through the swing. This model had
    # them at 17-20cm — its hand path was about half as wide as a real one,
    # which is the actual reason the bat kept ending up inside the hitter.
    SPINE_R = 0.32
    SPINE_SOFT = 0.05

    def _spine_warp(self, p, a, b, hint=None):
        """Smoothly map the inside of the torso cylinder to the outside.

        This is the standard animation fix for limbs penetrating the body, and
        the important part is that it is a CONTINUOUS SPACE WARP applied to
        every point unconditionally — not a collision test with a corrective
        push. A test has a threshold, the threshold is crossed between frames,
        and the correction switches on and off: that is what made both earlier
        attempts here jitter (peak |d2p/ds2| of 48,000 and 8,478 against a
        median of ~10). A softplus radial profile has no branch at all:

            r' = R + k*ln(1 + exp((r - R)/k))

        r' -> r for r >> R, r' -> R for r << R, and it is C-infinity in
        between, so nothing can pop."""
        d = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        L2 = _dot(d, d)
        t = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, (
            (p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]
            + (p[2] - a[2]) * d[2]) / L2))
        q = (a[0] + d[0] * t, a[1] + d[1] * t, a[2] + d[2] * t)
        w = (p[0] - q[0], p[1] - q[1], p[2] - q[2])
        r = math.sqrt(_dot(w, w))
        R, k = self.SPINE_R, self.SPINE_SOFT
        x = (r - R) / k
        # ln(1+e^x) without overflowing for large x
        sp = x + math.log1p(math.exp(-x)) if x > 0 else math.log1p(math.exp(x))
        r2 = R + k * sp
        if r < 1e-6:
            u = hint or (0.0, 1.0, 0.0)
        else:
            u = (w[0] / r, w[1] / r, w[2] / r)
        return (q[0] + u[0] * r2, q[1] + u[1] * r2, q[2] + u[2] * r2)

    @staticmethod
    def _reach_clamp(p, S, R, k=0.06):
        """Pull point `p` inside radius `R` of `S`, softly.

        The mirror image of `_spine_warp`: that one maps the inside of a
        cylinder out, this maps the outside of a sphere in. Same softplus, same
        reason — a hard clamp has a threshold and the threshold gets crossed
        between frames."""
        d = (p[0] - S[0], p[1] - S[1], p[2] - S[2])
        r = math.sqrt(_dot(d, d))
        if r < 1e-6:
            return p
        x = (R - r) / k
        sp = x + math.log1p(math.exp(-x)) if x > 0 else math.log1p(math.exp(x))
        r2 = R - k * sp                         # -> r below R, -> R above it
        return (S[0] + d[0] / r * r2, S[1] + d[1] / r * r2,
                S[2] + d[2] / r * r2)

    def _reach_cap(self, sweet, bd, S, Ls, R_arm, k=0.12):
        """Rotate the bat about its SWEET SPOT until the grip is inside the
        lead arm's reach of shoulder `S`, and return the new bat direction.

        This is the lever the body used to pull instead. The old code, when the
        grip came out further from the shoulder than an arm is long, TRANSLATED
        the whole upper body to it — which is why the figure's head travelled
        109 cm through a swing that should move it about 9. But the grip is the
        FREE end: the barrel is what Savant measures, the bat is rigid, so any
        rotation about the sweet spot costs none of the measured quantities and
        moves only the hands. Rotate the bat, do not walk the hitter.

        The reachable set of bat directions is a spherical cap about the
        direction to the shoulder, with half-angle `a_max` from the law of
        cosines on the triangle (sweet, grip, shoulder). The clamp into it is a
        SOFTPLUS on the angle, not a threshold, for the same reason
        `_spine_warp` is: a hard clamp switches on between frames and pops."""
        v = (S[0] - sweet[0], S[1] - sweet[1], S[2] - sweet[2])
        d0 = math.sqrt(_dot(v, v))
        if d0 < 1e-6:
            return bd
        u = (v[0] / d0, v[1] / d0, v[2] / d0)
        cos_max = (Ls * Ls + d0 * d0 - R_arm * R_arm) / (2.0 * Ls * d0)
        if cos_max <= -1.0:                     # every direction is reachable
            return bd
        a_max = math.acos(min(1.0, cos_max))
        a = math.acos(max(-1.0, min(1.0, _dot(bd, u))))
        x = (a_max - a) / k
        sp = x + math.log1p(math.exp(-x)) if x > 0 else math.log1p(math.exp(x))
        a2 = a_max - k * sp                     # -> a when slack, -> a_max when not
        turn = a - a2
        if turn <= 1e-6 or a < 1e-6:
            return bd
        ax = _cross(bd, u)
        al = math.sqrt(_dot(ax, ax))
        if al < 1e-9:
            return bd
        ax = (ax[0] / al, ax[1] / al, ax[2] / al)
        c, s_ = math.cos(turn), math.sin(turn)  # Rodrigues about ax
        adb = _dot(ax, bd)
        return _norm((bd[0] * c + (ax[1] * bd[2] - ax[2] * bd[1]) * s_
                      + ax[0] * adb * (1 - c),
                      bd[1] * c + (ax[2] * bd[0] - ax[0] * bd[2]) * s_
                      + ax[1] * adb * (1 - c),
                      bd[2] * c + (ax[0] * bd[1] - ax[1] * bd[0]) * s_
                      + ax[2] * adb * (1 - c)))

    # Bat geometry. Savant measures its swing metrics at TWO different points
    # on the bat and the difference matters more than it looks:
    #
    #   bat speed     "measured at the sweet-spot of the bat" — 6" in from the
    #                 head. Attack angle, attack direction and swing tilt are
    #                 all derived off the same point's velocity, so the sweet
    #                 spot is what the solve pins to the ball.
    #   swing length  "the total (sum) distance in feet traveled of the HEAD of
    #                 the bat in X/Y/Z space, from start of tracking data until
    #                 impact point" — the TIP, not the sweet spot.
    #
    # This model fitted the sweet spot's path to swing length until 2026-08-10.
    # The head rides 6" further out, so its arc is ~19% longer at any pivot
    # radius: pinning the sweet spot to 7.2 ft made the head travel 8.59 ft
    # (measured, all 210 board hitters). That is a swing that starts a fifth of
    # an arc too far back, which is exactly what the over-the-head loop in the
    # path overlay was. See `_head_path_len`.
    BAT_LEN = 0.84
    BAT_SWEET = BAT_LEN - 0.152     # the definition's 6", exactly
    ARM_REACH = 0.76        # shoulder → hands, before the torso has to lean
    LEAN_MAX = 0.12         # hard ceiling on that lean — see the note in
                            # `_body_pose`; uncapped, it cost 109 cm of head
    PIVOT_SHIFT = 0.55      # how far the pivot rides in toward the body at load
    # Barrel radius at contact. FREE — tangency is exact for any value, since
    # the pivot is placed perpendicular to the attack vector — so this is the
    # parameter that decides where the swing circle sits relative to the
    # hitter, and it was one constant for all 201 tracked hitters.
    R_CONTACT = 1.05
    # Bat-to-torso angle at the top of the downswing — Blast Motion's "Early
    # Connection", measured on 677 real swings by 98 hitters in Driveline's
    # open OpenBiomechanics dataset (`bat_torso_angle_ds`): p10 92.9, median
    # 108.1, p90 126.0 deg. LEAGUE-GENERIC on purpose: across those swings the
    # load angle correlates with nothing we hold for an MLB hitter.
    BAT_TORSO_LOAD = math.radians(108.1)
    # Where the GRIP is at the top of the downswing. `lag0` is derived by
    # aiming the bat at this point, so it sets the bat's angle for the whole
    # early swing.
    GRIP_LOAD_BACK = 0.18       # behind the box point, toward the catcher
    GRIP_LOAD_Y = 1.30
    GRIP_LOAD_OUT = 0.04        # away from the plate

    def _swing_geometry(self, b, bx, bz, bside):
        """Solved swing for this batter at this box position (cached — the
        solve iterates, so it must not run per frame)."""
        key = (id(b), round(bx, 4), round(bz, 4), bside)
        if self._sg_cache and self._sg_cache[0] == key:
            return self._sg_cache[1]
        sg = self._solve_swing(b, bx, bz, bside)
        self._sg_cache = (key, sg)
        return sg

    def _solve_swing(self, b, bx, bz, bside):
        """Fit the barrel's swing to Savant's swing-path board.

        The bat is modelled the way the literature does (Cross, *Mechanics of
        swinging a bat*): two links, the arm turning about the body and the bat
        turning about the hands with a wrist lag held early and released late.

        Solved against the measured data, in this order:
          · the swing PLANE, from swing tilt, containing the attack vector;
          · the pivot, placed so the barrel is tangent to the measured attack
            angle and direction at the intercept point — exactly, by
            construction;
          · `dphi`, bisected so the sweet spot's PATH LENGTH over the downswing
            equals the measured swing length;
          · `t_down`, from the measured bat speed;
          · `lag0`, aiming the grip back at the rear shoulder at load.

                Returns None when the batter has no tracked swing data."""
        IN = 0.0254
        aa = b.get("attack_angle")
        tilt = b.get("swing_tilt")
        ad = b.get("attack_dir") or 0.0
        ivp = b.get("intercept_vs_plate")
        if aa is None or tilt is None or ivp is None:
            return None
        adr, av, tr = math.radians(ad), math.radians(aa), math.radians(tilt)
        Ls = self.BAT_SWEET

        # Contact point. Savant's two intercept fields are the SAME depth
        # measured from two origins — batter_y_position + intercept_y_vs_plate
        # == intercept_y_vs_batter. So intercept_vs_batter is a DEPTH in front
        # of the hitter, not a lateral reach out over the plate.
        C = (0.216 + ivp * IN, 0.90, bz * 0.10)

        a_h = _norm((math.cos(adr), 0.0, -bside * math.sin(adr)))
        atk = _norm((a_h[0] * math.cos(av), math.sin(av), a_h[2] * math.cos(av)))

        # Swing-plane normal: contains the attack vector and sits at swing_tilt
        # from horizontal, i.e. its NORMAL sits at swing_tilt from vertical.
        # NOTE: this used to finish with n = cross(atk, N), which takes a vector
        # lying IN the plane and treats it as the normal — 90 deg out.
        u1 = _norm((-atk[1] * atk[0], 1.0 - atk[1] * atk[1], -atk[1] * atk[2]))
        w = _norm(_cross(atk, (0.0, 1.0, 0.0)))
        cos_a = max(-1.0, min(1.0, math.cos(tr) / max(1e-6, math.cos(av))))
        sin_a = math.sqrt(max(0.0, 1.0 - cos_a * cos_a))
        best = None
        for sgn in (sin_a, -sin_a):
            n_c = _norm((cos_a * u1[0] + sgn * w[0], cos_a * u1[1] + sgn * w[1],
                         cos_a * u1[2] + sgn * w[2]))
            if n_c[1] < 0.0:
                n_c = (-n_c[0], -n_c[1], -n_c[2])
            score = -n_c[2] * bside                    # high on his side
            if best is None or score > best[0]:
                best = (score, n_c)
        n_c = best[1]

        e2 = atk                                   # in-plane, = attack vector
        e1 = _norm(_cross(n_c, e2))                # in-plane, ⟂ attack vector

        tp = -bside                                # +1 = toward the plate
        f2 = atk                                   # tangent at contact

        # SWING AXIS — the hitter's own spine, a vertical line through the box
        # point. The whole path lives in cylindrical coordinates about it, which
        # is what makes the barrel's distance from the hitter an explicit
        # coordinate instead of an emergent accident. The old pivot construction
        # put the centre of a fixed-radius circle wherever tangency happened to
        # place it, and the barrel then cut straight through the torso.
        h_c = C[1]

        # Tangency at contact, exactly, as two slope corrections. Decompose the
        # measured attack vector's horizontal part in (rhat, phat): the ratio of
        # those two components IS the ratio of radial growth to angular rate, so
        # once the sweep is known both slopes are forced.
        sg = {"C": C, "atk": atk, "n": n_c, "e1": e1, "e2": e2, "f2": f2,
              "BX": bx, "BZ": bz, "h_c": h_c,
              "A": (bx, 1.24, bz), "rc": _OBP_RC, "axis_off": 0.0, "h0": 0.0,
              "rhat": (1.0, 0.0, 0.0), "phat": (0.0, 0.0, 1.0),
              "alpha": 0.0, "beta": 1.0,
              "swdir": 1.0, "kH": 1.0, "dRs": 0.0, "dHs": 0.0, "c2": 0.0,
              "Ls": Ls, "BAT": self.BAT_LEN, "s_c": _CONTACT_P,
              "lag0": math.radians(-62.0), "wrap": math.radians(150.0),
              "h_contact": C, "h_finish": C,
              "dphi": _OBP_SWEEP, "gn0": 0.0, "k_barrel": _SEQ_K,
              "v_in": (0.0, 0.0, 0.0), "w_in": 0.0,
              "t_down": _DOWNSWING_S}

        sg["fwd"] = max(0.0, min(0.60, (b["depth_in_box"] + ivp) * IN
                                 - _OBP_CONTACT_FWD)) if b.get("depth_in_box") else 0.0
        target = (b.get("swing_length") or 7.2) * FT_TO_M
        v_target = max(1.0, (b.get("bat_speed") or 72.0) * _MPH)

        grip_load = (bx - self.GRIP_LOAD_BACK, self.GRIP_LOAD_Y,
                     bz - tp * self.GRIP_LOAD_OUT)
        # Bisect the sweep against the measured swing length, on the BAT HEAD —
        # the point Savant sums that distance over.
        lo, hi = 0.5, 5.0
        for _ in range(34):
            mid = 0.5 * (lo + hi)
            sg["dphi"] = mid
            self._solve_tangency(sg)
            if self._head_path_len(sg) < target:
                lo = mid
            else:
                hi = mid
        sg["dphi"] = 0.5 * (lo + hi)
        self._solve_tangency(sg)
        sg["head_len"] = self._head_path_len(sg)
        # Duration, from the bat speed — which is measured at the SWEET SPOT,
        # so this must not use the head's arc.
        #
        # `|dP/du|` at contact is taken from the path itself now. The old code
        # could use the closed form `R_c*dphi` because the path was a circle of
        # known radius; the spiral's speed at contact also carries the radial
        # and vertical growth, and a closed form for it would just be this
        # derivative written out.
        sg["sweet_len"] = self._sweet_path_len(sg)
        hh = 1e-5
        pa = self._barrel_at(sg, 1.0 - hh)[0]
        pb = self._barrel_at(sg, 1.0)[0]
        dPdu = math.dist(pa, pb) / hh
        B1 = _seq_d(1.0, _SEQ_BARREL, sg["k_barrel"])
        sg["t_down"] = max(0.060, min(0.260, dPdu * B1 / v_target))

        # Where the GRIP starts: aim the bat at load so the grip sits back
        # over the rear shoulder. The lag unwinds to zero at contact, where the
        # bat points straight out along the radius, so this costs the measured
        # quantities nothing.
        sweet0, er0, et0 = self._barrel_at(sg, 0.0)
        d = (sweet0[0] - grip_load[0], sweet0[1] - grip_load[1],
             sweet0[2] - grip_load[2])
        a1 = _dot(d, er0)
        a2 = _dot(d, et0)
        an = _dot(d, n_c)
        sg["lag0"] = max(math.radians(-150.0),
                         min(math.radians(20.0), math.atan2(a2, a1)))
        sg["gn0"] = max(-2.0, min(2.0, an / max(1e-6, math.hypot(a1, a2))))

        sg["tangent_err"] = self._tangent_err(sg)
        sg["contact_mph"] = self._contact_speed(sg) / _MPH
        sg["h_contact"] = self._pose_at(sg, sg["s_c"])["hands"]
        # Follow-through finish: the REFERENCE swing's contact->finish
        # DISPLACEMENT, not its absolute finish position.
        _rf1, _rfc = swing_ref_at(1.0), swing_ref_at(sg["s_c"])
        hc0 = sg["h_contact"]
        if _rf1 is not None and _rfc is not None:
            sg["h_finish"] = (
                hc0[0] + (_rf1["hands"][0] - _rfc["hands"][0]),
                hc0[1] + (_rf1["hands"][1] - _rfc["hands"][1]),
                hc0[2] + tp * (_rf1["hands"][2] - _rfc["hands"][2]))
        else:
            sg["h_finish"] = (bx + 0.34, 1.50, bz + tp * 0.34)
        # The bat keeps turning in the SWING PLANE it was already in, and the
        # axis MUST come from a cross product rather than being `n_c` directly.
        #
        # This was a real handedness bug and it is worth understanding, because
        # nothing else in the file trips it. Under the mirror that takes a RHB
        # to a LHB, `M Rot(a, t) M^-1 == Rot(Ma, -t)`: rotating by the same
        # positive `wrap` about the mirrored normal turns the bat the WRONG WAY
        # round for a left-hander. `_reach_cap` is immune because its axis is
        # `cross(bd, u)`, and a cross product picks up the compensating sign
        # flip under a reflection; a plain vector does not.
        #
        # `cross(rhat, phat)` is +n or -n depending on which way this hitter's
        # swing turns, so it both selects the correct direction and mirrors
        # correctly. Measured before: the bat tip was up to 168 cm from where
        # the mirrored right-handed solve puts it, on every LHB, from contact
        # to the finish. After: 0.00 cm.
        sg["wrap_axis"] = _norm(_cross(sg["rhat"], sg["phat"]))
        sg["wrap"] = math.radians(115.0)
        sg["bd_contact"] = sg["rhat"]
        # Start tangents for the follow-through Hermite, measured off the
        # pre-contact branch — the only definition that makes the halves agree.
        h = 1e-4
        s_c = sg["s_c"]
        pm = self._pose_at(sg, s_c - h)
        hc = sg["h_contact"]
        scale = (1.0 - s_c) / h
        v_in = ((hc[0] - pm["hands"][0]) * scale,
                (hc[1] - pm["hands"][1]) * scale,
                (hc[2] - pm["hands"][2]) * scale)
        # NO clamp on the tangent: clamping TRUNCATES the incoming velocity,
        # which is a speed discontinuity at contact, and that is the louder
        # artefact by far.
        sg["v_in"] = v_in

        # Angle of the bat inside the swing plane, measured in the frame the
        # bat is actually built in now: radially out at contact, turning toward
        # the sweep direction.
        _pr, _pp = sg["rhat"], sg["phat"]

        def _plane_ang(dv):
            return math.atan2(_dot(dv, _pp), _dot(dv, _pr))
        w_in = (_plane_ang(self._pose_at(sg, s_c)["dir"])
                - _plane_ang(pm["dir"]))
        while w_in > math.pi:
            w_in -= 2 * math.pi
        while w_in < -math.pi:
            w_in += 2 * math.pi
        w_in *= scale
        sg["w_in"] = max(-abs(sg["wrap"]) * 1.6,
                         min(abs(sg["wrap"]) * 1.6, w_in))
        return sg

    # Width of the corrections that enforce tangency and swing tilt at contact.
    # They are bumps that vanish at u=1 in position, so nothing they do can move
    # the contact point; they only shape the approach to it.
    _CORR_SIGMA = 0.28

    @staticmethod
    def _corr_w(u, sigma=_CORR_SIGMA):
        """Slope bump: 0 at contact, unit derivative there, decaying backwards."""
        d = 1.0 - u
        return -d * math.exp(-(d / sigma) ** 2)

    def _barrel_at(self, sg, u):
        """Sweet spot, radial direction and tangential direction at normalised
        sweep `u` (0 = the start of Savant's measured window, 1 = contact).

        Cylindrical about the SWING PLANE's normal. Radius and out-of-plane
        drift are the measured profile; the sweep is this hitter's own solved
        angle. Clearance is the radius itself, floored at `CLEAR_MIN`, so the
        barrel cannot enter the hitter whatever the solve does."""
        u = max(0.0, min(1.0, u))
        R = _obp_at(_OBP_R, u) + sg["dRs"] * self._corr_w(u)
        rho = sg["rc"] * R
        if rho < CLEAR_MIN:                        # softplus floor, never a step
            k = 0.06
            x = (rho - CLEAR_MIN) / k
            sp = x + math.log1p(math.exp(-x)) if x > 0 else math.log1p(math.exp(x))
            rho = CLEAR_MIN + k * sp
        Hn = (_obp_at(_OBP_H, u) * sg["kH"] + sg["dHs"] * self._corr_w(u)
              + sg["h0"])
        th = -sg["dphi"] * (1.0 - u)
        ct, st = math.cos(th), math.sin(th)
        rh, ps, n = sg["rhat"], sg["phat"], sg["n"]
        er = (ct * rh[0] + st * ps[0], ct * rh[1] + st * ps[1],
              ct * rh[2] + st * ps[2])
        et = (-st * rh[0] + ct * ps[0], -st * rh[1] + ct * ps[1],
              -st * rh[2] + ct * ps[2])
        A = sg["A"]
        sweet = (A[0] + rho * er[0] + Hn * n[0],
                 A[1] + rho * er[1] + Hn * n[1],
                 A[2] + rho * er[2] + Hn * n[2])
        return sweet, er, et

    def _solve_tangency(self, sg):
        """Place the swing axis so the measured attack vector is consistent with
        the measured spiral, then read the residual corrections. Closed form.

        The AXIS is solved, not assumed. The profile says the barrel is still
        growing radially at contact at a known rate (`Rp/dphi`), and the
        measured attack vector says which way it is travelling; those two agree
        for exactly one axis direction in the plane. Pinning the axis at the
        hitter's stance point instead leaves the mismatch to be absorbed by
        correction bumps, which is what buckled the first attempt.

        Because the plane normal comes from `swing_tilt` and the attack vector
        lies IN that plane, swing tilt and attack angle/direction are all exact
        by construction and nothing has to be corrected for them."""
        h = 1e-4
        Rp = (_obp_at(_OBP_R, 1.0) - _obp_at(_OBP_R, 1.0 - h)) / h
        atk, C, n = sg["atk"], sg["C"], sg["n"]
        kap = Rp / max(1e-6, sg["dphi"])           # radial growth per radian
        # In-plane basis (p, q) with p along the attack vector.
        p = _norm((atk[0] - _dot(atk, n) * n[0], atk[1] - _dot(atk, n) * n[1],
                   atk[2] - _dot(atk, n) * n[2]))
        q = _cross(n, p)
        # rhat at angle psi off p, with cos(psi)/|sin(psi)| = kap.
        psi0 = math.atan2(1.0, kap)
        #
        # `rc`, the contact radius about the swing axis, is MEASURED — median
        # 0.969 m over the 675 landmark swings — so it is not a free parameter.
        rc = _OBP_RC
        bxc = sg["BX"] + sg.get("fwd", 0.0)
        torso_a = (bxc, 0.98, sg["BZ"])
        torso_b = (bxc, 1.50, sg["BZ"])
        # AXIS PLACEMENT IS THE ONE PIECE STILL UNRESOLVED — read this before
        # touching it. Four approaches have been tried and MEASURED.
        #
        # Real swings put the barrel's closest approach to the thorax (39.7 cm)
        # within a centimetre of the profile's own minimum radius (0.383 x
        # 0.969 = 37 cm). That can only be true if the swing axis and the spine
        # are essentially the same line. Getting both that AND in-plane tangency
        # out of one axis has not worked yet:
        #
        #   direction from tangency, positioned nearest the stance point
        #       -> barrel 9.7 cm off the torso; cuts through him
        #   same, but `rc` scanned for clearance
        #       -> degenerate; every hitter at the floor with a 220 deg sweep
        #   direction from tangency, branch chosen by clearance
        #       -> 0/200 through the torso, BUT the axis is off the body, so
        #          the whole swing goes with it and the grip ends up 93 cm
        #          beyond the arms. A bat the hitter cannot hold is worse than
        #          one that clips him.
        #   axis anchored ON the thorax (what is here)
        #       -> figure coherent, arms reach, `dRs` ~1.2 deforms the radius
        #          near contact by ~10 cm, clearance 15.1 cm
        #
        # The trap that cost the most time: the in-plane angle this
        # construction needs and the horizontal angle the landmark data reports
        # are NOT the same angle, which made them look reconciled at 64.3 vs
        # 65.1 deg when they were not. Compare them in the same frame.
        A = (bxc, 1.24, sg["BZ"])
        w = (C[0] - A[0], C[1] - A[1], C[2] - A[2])
        wn = _dot(w, n)
        wp = (w[0] - wn * n[0], w[1] - wn * n[1], w[2] - wn * n[2])
        rc = math.sqrt(_dot(wp, wp))
        if rc < 1e-6:
            rc, wp = _OBP_RC, p
        rh = (wp[0] / rc, wp[1] / rc, wp[2] / rc)
        sg["h0"] = wn
        ps = _cross(n, rh)
        if _dot(atk, ps) < 0.0:                    # tangential, forward
            ps = (-ps[0], -ps[1], -ps[2])
        sg["rhat"], sg["phat"], sg["rc"], sg["A"] = rh, ps, rc, A
        sg["axis_off"] = math.hypot(A[0] - sg["BX"], A[2] - sg["BZ"])
        alpha = _dot(atk, rh)
        beta = abs(_dot(atk, ps)) or 1e-6
        sg["alpha"], sg["beta"], sg["swdir"] = alpha, beta, 1.0
        sg["kH"] = 1.0
        # Residual only — zero when the axis solve was not clamped.
        sg["dRs"] = sg["dphi"] * alpha / beta - Rp
        # The profile's out-of-plane drift lands with a slope of about -0.003
        # rather than a clean zero (it is a median of 675 noisy swings). Left
        # alone that tips the barrel very slightly out of the measured plane at
        # contact and shows up as 0.30 deg of tangency error, so it is zeroed.
        Hp = (_obp_at(_OBP_H, 1.0) - _obp_at(_OBP_H, 1.0 - h)) / h
        sg["dHs"] = -Hp

    def _contact_speed(self, sg):
        """Sweet-spot speed at contact (m/s). Phase maps linearly to time
        inside the downswing, so ds → dt is that hitter's own swing duration."""
        h = 1e-4
        a = self._pose_at(sg, sg["s_c"] - h)["sweet"]
        c = self._pose_at(sg, sg["s_c"])["sweet"]
        return math.dist(a, c) / (h * sg["t_down"] / sg["s_c"])

    def _tangent_err(self, sg):
        """Angle (deg) between the barrel's actual path at contact and the
        measured attack vector — 0 means the swing crosses the ball exactly as
        Savant measured it. Kept on the solution as a diagnostic."""
        h = 1e-4
        a = self._pose_at(sg, sg["s_c"] - h)["sweet"]
        c = self._pose_at(sg, sg["s_c"])["sweet"]
        v = _norm((c[0] - a[0], c[1] - a[1], c[2] - a[2]))
        return math.degrees(math.acos(max(-1.0, min(1.0, _dot(v, sg["atk"])))))

    def _path_len(self, sg, key, steps=28):
        """Length of `key`'s path over the downswing (m)."""
        prev, tot = None, 0.0
        for k in range(steps + 1):
            q = self._pose_at(sg, sg["s_c"] * k / steps)[key]
            if prev is not None:
                tot += math.dist(prev, q)
            prev = q
        return tot

    def _head_path_len(self, sg, steps=28):
        """Length of the BAT HEAD's path over the downswing (m) — the thing
        Savant's swing length actually measures. The head is the tip, 6" beyond
        the sweet spot; `_pose_at` already carries it as "tip"."""
        return self._path_len(sg, "tip", steps)

    def _sweet_path_len(self, sg, steps=28):
        """Length of the SWEET SPOT's path over the downswing (m). NOT what
        swing length measures — kept because bat speed lives on this point, so
        the timing solve needs its arc."""
        return self._path_len(sg, "sweet", steps)

    def _pose_at(self, sg, s):
        """Hands / sweet spot / bat tip at swing progress `s` (0..1).

        The barrel rides its solved arc, its radius growing into contact as the
        wrists release. The bat TRAILS the radius by a lag angle that unwinds
        late (the "late hit"), and the hands are the other end of that same
        rigid bat, so the grip and the barrel can never disagree. Both the
        radius growth and the lag are squared/eased to zero at contact, which
        leaves the barrel exactly tangent to the measured attack vector."""
        s_c = sg["s_c"]
        if s <= s_c:
            tau = s / s_c if s_c else 1.0
            B = _seq(tau, _SEQ_BARREL, sg["k_barrel"])
            W = _seq(tau, _SEQ_WRIST)
            sweet, er, et = self._barrel_at(sg, B)
            # The bat TRAILS the radius by a lag that unwinds late (the "late
            # hit"), so at contact it points straight out along the radius —
            # arms extended, which is what the measured pose is. The grip is
            # the other end of the same rigid bat, so the two can never
            # disagree, and the lag being zero at contact means none of this
            # touches the measured quantities.
            psi = sg["lag0"] * (1.0 - W)
            gn = sg["gn0"] * (1.0 - _smoothstep(tau / 0.72))
            cb, sb = math.cos(psi), math.sin(psi)
            n = sg["n"]
            bd = _norm((cb * er[0] + sb * et[0] + gn * n[0],
                        cb * er[1] + sb * et[1] + gn * n[1],
                        cb * er[2] + sb * et[2] + gn * n[2]))
            hands = (sweet[0] - sg["Ls"] * bd[0], sweet[1] - sg["Ls"] * bd[1],
                     sweet[2] - sg["Ls"] * bd[2])
            tip = (hands[0] + sg["BAT"] * bd[0], hands[1] + sg["BAT"] * bd[1],
                   hands[2] + sg["BAT"] * bd[2])
            return {"hands": hands, "sweet": sweet, "tip": tip, "dir": bd}
        else:
            # Past contact the HANDS lead, not the barrel: they decelerate and
            # pull back in toward the lead shoulder while the bat keeps turning
            # about them and wraps over that shoulder. Driving the wrap off the
            # barrel's arc instead flips the bat end-for-end and flings the grip
            # ~2 m off the body, which no amount of torso lean can absorb.
            t2 = min(1.0, (s - s_c) / max(1e-6, 1.0 - s_c))
            # CUBIC HERMITE, not an eased lerp. `1-(1-t)^2` leaves the follow-
            # through with a nonzero initial rate that has nothing to do with
            # how fast the hands were actually travelling at contact, so the
            # figure changed speed instantly on the contact frame — measured at
            # 1.42 -> 3.31 m per unit phase, a 90-110x spike in |d2p/ds2| and
            # the single loudest source of the jerk. Hermite with the incoming
            # velocity as its start tangent is continuous in BOTH position and
            # speed, and still arrives at rest.
            t3 = t2 * t2
            t4 = t3 * t2
            h00 = 2.0 * t4 - 3.0 * t3 + 1.0
            h10 = t4 - 2.0 * t3 + t2
            h01 = -2.0 * t4 + 3.0 * t3
            v_in = sg["v_in"]
            hc, hf = sg["h_contact"], sg["h_finish"]
            hands = (h00 * hc[0] + h10 * v_in[0] + h01 * hf[0],
                     h00 * hc[1] + h10 * v_in[1] + h01 * hf[1],
                     h00 * hc[2] + h10 * v_in[2] + h01 * hf[2])
            settle = 1.0
            # The bat's WRAP gets the same treatment — it is an angle on the
            # same clock, and starting it from rest made the barrel visibly
            # hesitate at the ball before the wrap took over.
            wr = h00 * 0.0 + h10 * sg["w_in"] + h01 * sg["wrap"]
            # Slerp the bat from its contact direction to the named finish
            # pose about the shortest-arc axis (Rodrigues).
            ax = sg["wrap_axis"]
            bc = sg["bd_contact"]
            cw, sw = math.cos(wr), math.sin(wr)
            adb = _dot(ax, bc)
            bd = _norm((bc[0] * cw + (ax[1] * bc[2] - ax[2] * bc[1]) * sw
                        + ax[0] * adb * (1 - cw),
                        bc[1] * cw + (ax[2] * bc[0] - ax[0] * bc[2]) * sw
                        + ax[1] * adb * (1 - cw),
                        bc[2] * cw + (ax[0] * bc[1] - ax[1] * bc[0]) * sw
                        + ax[2] * adb * (1 - cw)))
            sweet = (hands[0] + sg["Ls"] * bd[0], hands[1] + sg["Ls"] * bd[1],
                     hands[2] + sg["Ls"] * bd[2])
            tip = (hands[0] + sg["BAT"] * bd[0], hands[1] + sg["BAT"] * bd[1],
                   hands[2] + sg["BAT"] * bd[2])
            return {"hands": hands, "sweet": sweet, "tip": tip, "dir": bd}

    def _draw_batter(self):
        """3-D batter reconstruction from the Savant swing-path + stance data.
        The figure stands at his real box position with his real foot
        separation and open/closed stance; the torso coils and unwinds and the
        bat head sweeps the tilted swing plane, driven by `_swing_phase`. The
        path already swept trails behind the bat, color-graded load→extension.
        """
        b = self.batter
        if not b:
            return
        IN = 0.0254
        side = b.get("side", "R")
        bside = -1.0 if side == "R" else 1.0    # RHB→3B(-Z), LHB→1B(+Z)
        off = (b.get("dist_off_plate") or 27.0) * IN
        depth = (b.get("depth_in_box") or 27.0) * IN
        bx = 0.22 - depth                        # toward the catcher (−X)
        bz = bside * off

        p = self._swing_phase
        pp = p if p is not None else 0.0
        sg = self._swing_geometry(b, bx, bz, bside)

        glPushAttrib(GL_LIGHTING_BIT | GL_ENABLE_BIT | GL_LINE_BIT
                     | GL_CURRENT_BIT)
        glDisable(GL_LIGHTING)
        glEnable(GL_BLEND)
        self._draw_batter_box(bside)
        self._draw_batter_figure(b, bx, bz, bside, sg, pp, p is None)
        if sg is not None:
            self._draw_swing_path(sg, p, pp)
        glPopAttrib()

    def _draw_batter_box(self, bside):
        """Batter's-box outline on the correct side of the plate."""
        inner, outer = 0.37 * bside, (0.37 + 1.22) * bside
        glLineWidth(1.5)
        glColor4f(0.70, 0.78, 0.88, 0.40)
        glBegin(GL_LINE_LOOP)
        for px, pz in ((0.95, inner), (0.95, outer),
                       (-1.05, outer), (-1.05, inner)):
            glVertex3f(*self._p2m(px, 0.006, pz))
        glEnd()

    def _body_pose(self, b, bx, bz, bside, sg, pp, setup):
        """Every joint of the figure at swing progress `pp`, as plain numbers.

        Split out of the drawing so the pose can be MEASURED (bat-vs-torso
        clearance, frame-to-frame jerk) and so the swing solve can react to
        the body instead of the body being hung off the barrel and hoped for.
        `_draw_batter_figure` is now only the GL calls.

        Batter driven by ONE kinematic chain, feet → pelvis → torso →
        shoulders → arms → bat, in kinematic-sequence order.

        Nothing is pinned after the fact: the hands come out of the solved
        double pendulum and the bat is the second link off them, so the bat is
        rigid at every instant and setup flows straight into the swing (they
        are the same chain at s=0, not two poses lerped together). The pelvis
        opens first and the shoulders lag it — the X-factor — then close that
        gap through contact. Legs and arms are two-bone IK, and when the
        measured intercept is further out than the arms can reach the TORSO
        leans to it rather than the arm stretching."""
        IN = 0.0254
        # Savant's batter reference is NOT the hitter's torso at contact, and
        # the gap is big enough to have looked like a barrel-path defect.
        #
        # Savant's own identity puts contact `batter_y_position + intercept_
        # y_vs_plate` in front of the batter's tracked position — 0.955 m for
        # the median hitter. Driveline's landmarks put contact 0.528 m in front
        # of the THORAX at contact. Both are right: the tracked position is a
        # stance reference and the hitter has strode and rotated away from it by
        # the time he hits the ball. Standing the figure at the stance point
        # left the barrel a median 5.4 cm off his torso; carrying him to his
        # contact-time position puts it at 37.5 cm, against the 39.7 real
        # swings hold. Per hitter, so a deep-in-the-box hitter carries further.
        ivp_ = b.get("intercept_vs_plate")
        dep_ = b.get("depth_in_box")
        fwd = 0.0
        if ivp_ is not None and dep_ is not None:
            fwd = max(0.0, min(0.60, (dep_ + ivp_) * IN - _OBP_CONTACT_FWD))
        bx = bx + fwd
        half = (b.get("foot_sep") or 30.0) * IN / 2.0
        st = math.radians(-(b.get("stance_angle") or 0.0))   # +open
        fc, fs = math.cos(st), math.sin(st)

        knee_y, hip_y, sh_y, head_dy = 0.50, 0.98, 1.50, 0.22
        HIP_HALF, SH_HALF = 0.16, 0.19
        tp = -bside                                 # +1 = toward the plate

        s_c = sg["s_c"] if sg else _CONTACT_P
        if setup or sg is None:
            tau, post = 0.0, 0.0
        else:
            tau = min(1.0, pp / s_c) if s_c else 1.0
            post = max(0.0, (pp - s_c) / max(1e-6, 1.0 - s_c))
        fpost = 1.0 - (1.0 - min(1.0, post)) ** 2
        # Phase the reference swing is read at — clamped to the load while the
        # figure is in setup. Needed by the weight shift, the yaw and the head.
        s_now = 0.0 if (setup or sg is None) else pp
        seq_p = _seq(tau, _SEQ_PELVIS)
        seq_t = _seq(tau, _SEQ_TORSO)

        # Stride. When this hitter is in the Savant stance scrape we drive the
        # feet from his REAL tracked positions and foot angles at pitch release
        # and bat-ball intercept — so a leg-kick hitter visibly lifts, swings
        # the front foot back and replants, while a no-stride hitter barely
        # shifts. Otherwise fall back to a generic stride off his stance line.
        # The lift runs on the PITCH clock like the feet themselves; running it
        # on the swing clock (as it used to) made the foot pop mid-stride.
        cx, cz = bx, bz
        fp = 0.0 if setup else (self._foot_phase or 0.0)
        lift = 0.055 * math.sin(math.pi * min(1.0, fp))
        real = self._feet_at_phase()
        if real:
            (fx, fz, faxis), (bkx, bkz, baxis) = real
            front_f = (fx, lift, fz)
            back_f = (bkx, 0.0, bkz)
            foot_axes = (faxis, baxis)
        else:
            STRIDE = 0.13
            front_f = (cx + half * fc + STRIDE * seq_p, lift,
                       cz + bside * half * fs)
            back_f = (cx - half * fc, 0.0, cz - bside * half * fs)
            foot_axes = ((fc, bside * fs), (fc, bside * fs))

        # Stable weight point — the pelvis, shifting off the rear side onto the
        # feet centre through the stride.
        #
        # This is MEASURED off the reference now, and the amount matters more
        # than it looks: the old rule put the pelvis 75% of the way from the
        # feet centre to the BACK FOOT at load (the `0.25` below) and then
        # walked it all the way in, which is 35 cm of pelvis travel. The
        # reference hitter's pelvis moves 20.6 cm. Everything above the pelvis
        # inherits that error, and it was the largest single contributor to the
        # figure's head travel once the reach lunge was capped.
        feet_cx = (front_f[0] + back_f[0]) / 2.0
        feet_cz = (front_f[2] + back_f[2]) / 2.0
        rp_, rp1_ = swing_ref_at(s_now), swing_ref_at(1.0)
        if rp_ is not None and rp1_ is not None:
            ax_ = feet_cx + (rp_["pelvis"][0] - rp1_["pelvis"][0])
            az_ = feet_cz + tp * (rp_["pelvis"][2] - rp1_["pelvis"][2])
        else:
            rear_hip_x = back_f[0] + (feet_cx - back_f[0]) * 0.25
            rear_hip_z = back_f[2] + (feet_cz - back_f[2]) * 0.25
            ax_ = rear_hip_x + (feet_cx - rear_hip_x) * seq_p
            az_ = rear_hip_z + (feet_cz - rear_hip_z) * seq_p

        # Feet. The scrape's foot axis is a LINE, not a direction — it comes
        # from a symmetric cleat glyph, so its sign is meaningless. Resolve it
        # into a real toe direction by pointing it at the plate, which is where
        # a hitter's toes point. Everything downstream that needs front-vs-back
        # (the knees, the toe marks) depends on this, and taking the raw sign
        # instead is what had the knees bending away from the plate — the
        # single loudest "this hitter is facing backwards" cue in the figure.
        toes = []
        for axf in foot_axes:
            t = (axf[0], axf[1])
            if t[1] * tp < 0.0:
                t = (-t[0], -t[1])
            toes.append(t)

        # Pelvis and shoulders turn on SEPARATE clocks: the pelvis fires first
        # and the shoulders lag behind it at load (the X-factor), then close
        # that gap and pass it through contact.
        #
        # The two progress curves are the RECORDED ones (`swing_ref_yaw`), not
        # a pair of logistics — the amplitudes and directions below are still
        # this figure's own, only the shape of the turn comes from the mocap.
        # The old `_seq` pair is the fallback when the reference is missing.
        ryaw = swing_ref_yaw()
        if ryaw is not None:
            prog_h = _ref_curve_at(ryaw[0], s_now)
            prog_s = _ref_curve_at(ryaw[1], s_now)
            yaw_h = bside * (-0.10 + 1.65 * prog_h)
            yaw_s = bside * (-0.42 + 2.27 * prog_s)
        else:
            yaw_h = bside * (-0.10 + 1.40 * seq_p + 0.25 * fpost)
            yaw_s = bside * (-0.42 + 1.97 * seq_t + 0.30 * fpost)

        def rot(px, pz, yaw):
            cy_, sy_ = math.cos(yaw), math.sin(yaw)
            dx, dz = px - ax_, pz - az_
            return (ax_ + dx * cy_ - dz * sy_, az_ + dx * sy_ + dz * cy_)

        lh_x, lh_z = rot(ax_ + HIP_HALF, az_, yaw_h)     # lead hip (mound side)
        rh_x, rh_z = rot(ax_ - HIP_HALF, az_, yaw_h)
        spine_off = -0.05
        sc_x, sc_z = rot(ax_ + spine_off, az_, yaw_s)
        ls_x, ls_z = rot(ax_ + spine_off + SH_HALF, az_, yaw_s)
        rs_x, rs_z = rot(ax_ + spine_off - SH_HALF, az_, yaw_s)

        # Hands + bat straight off the solved chain.
        if sg is not None:
            pose = self._pose_at(sg, 0.0 if setup else pp)
            hands, bat_tip, sweet = pose["hands"], pose["tip"], pose["sweet"]
            # Warp the GRIP out of the torso and rebuild the bat rigidly from
            # it. The barrel is the measured half of the swing and is left
            # exactly where the solve put it; the grip is the free half (the
            # bat only has to stay BAT_SWEET long), so it is the end that
            # moves. Two fixed passes — a fixed count keeps the whole thing a
            # smooth function of the phase.
            if sweet is not None:
                torso_a = ((lh_x + rh_x) / 2.0, hip_y, (lh_z + rh_z) / 2.0)
                torso_b = (sc_x, sh_y, sc_z)
                Ls = self.BAT_SWEET
                s_now2 = 0.0 if setup else pp
                if s_now2 <= s_c:
                    # DOWNSWING: the barrel is the measured half, so pin the
                    # sweet spot and let the warp rotate the bat about it.
                    # Two constraints share that one rotation — the grip has to
                    # stay OUT of the torso and INSIDE the lead arm's reach —
                    # and both are softplus-smoothed, so alternating them a
                    # fixed number of times stays a smooth function of phase.
                    lead0 = (ls_x, sh_y, ls_z)
                    # The cap FADES OUT into contact. It has to: past contact
                    # the follow-through owns the grip and cannot apply the
                    # same rotation, so a cap still biting at s_c is a step in
                    # the grip — measured at 6.0 cm median, 18.6 cm worst, and
                    # it put the hands' |d2p/ds2| up to 7139 from 228. It costs
                    # nothing to give up: the reach overshoot at contact is
                    # already -16 cm, i.e. the arms are comfortably inside
                    # their reach exactly where the measurements live.
                    tau_ = (s_now2 / s_c) if s_c else 1.0
                    w_cap = 1.0 - _smootherstep((tau_ - 0.55) / 0.45)
                    for _ in range(3):
                        hands = self._spine_warp(hands, torso_a, torso_b,
                                                 hint=(0.0, 0.0, -tp))
                        bd = _norm((hands[0] - sweet[0], hands[1] - sweet[1],
                                    hands[2] - sweet[2]))
                        if w_cap > 1e-4:
                            bc = self._reach_cap(sweet, bd, lead0, Ls,
                                                 self.ARM_REACH)
                            bd = _norm(_lerp3(bd, bc, w_cap))
                        hands = (sweet[0] + Ls * bd[0], sweet[1] + Ls * bd[1],
                                 sweet[2] + Ls * bd[2])
                    bd = _norm((sweet[0] - hands[0], sweet[1] - hands[1],
                                sweet[2] - hands[2]))
                else:
                    # FOLLOW-THROUGH: nothing is measured here and the bat's
                    # direction is the deliberate wrap pose, so TRANSLATE the
                    # bat with the warped grip instead of rotating it about a
                    # sweet spot that is itself derived from the grip — doing
                    # that spun the bat about a meaningless anchor and is what
                    # stood it up across the chest at the finish.
                    # ...and pulled back inside the arm, radially about the
                    # lead shoulder. Without this the wrap drifted the grip up
                    # to 1.37 m off the spine — a distance the old code hid by
                    # walking the whole torso after it.
                    #
                    # Two reconciliations are blended here, and the blend is
                    # what makes the contact seam vanish. TRANSLATING the bat
                    # with the warped grip is the right thing at the finish
                    # (rotating it about a sweet spot that is itself derived
                    # from the grip stands the bat up across the chest), but it
                    # is NOT what the downswing does one sample earlier, and
                    # that mismatch was a 2 cm step in the grip. So the first
                    # 15% of the follow-through eases out of the downswing's
                    # own operation — pin the sweet spot, warp, re-rigidify —
                    # into the translation. At t2=0 the two branches are
                    # identical by construction.
                    lead0 = (ls_x, sh_y, ls_z)
                    bdB = pose["dir"]
                    hB = self._reach_clamp(
                        self._spine_warp(hands, torso_a, torso_b,
                                         hint=(0.0, 0.0, -tp)),
                        lead0, self.ARM_REACH)
                    sweetA = (hands[0] + Ls * bdB[0], hands[1] + Ls * bdB[1],
                              hands[2] + Ls * bdB[2])
                    hA = hands
                    for _ in range(3):
                        hA = self._spine_warp(hA, torso_a, torso_b,
                                              hint=(0.0, 0.0, -tp))
                        d_ = _norm((hA[0] - sweetA[0], hA[1] - sweetA[1],
                                    hA[2] - sweetA[2]))
                        hA = (sweetA[0] + Ls * d_[0], sweetA[1] + Ls * d_[1],
                              sweetA[2] + Ls * d_[2])
                    bdA = _norm((sweetA[0] - hA[0], sweetA[1] - hA[1],
                                 sweetA[2] - hA[2]))
                    wB = _smootherstep(post / 0.15)
                    hands = _lerp3(hA, hB, wB)
                    bd = _norm(_lerp3(bdA, bdB, wB))
                    sweet = (hands[0] + Ls * bd[0], hands[1] + Ls * bd[1],
                             hands[2] + Ls * bd[2])
                bat_tip = (hands[0] + self.BAT_LEN * bd[0],
                           hands[1] + self.BAT_LEN * bd[1],
                           hands[2] + self.BAT_LEN * bd[2])
        else:
            # No tracked swing: a static cocked stance, bat up over the rear
            # shoulder, so the overlay still reads as a hitter in the box.
            hx, hz = rot(ax_ - 0.24, az_ + tp * 0.10, yaw_s)
            hands = (hx, sh_y + 0.05, hz)
            c60, s60 = math.cos(math.radians(60.0)), math.sin(math.radians(60.0))
            d = _norm((-1.0 * c60, s60, 0.0))
            bat_tip = (hands[0] + self.BAT_LEN * d[0],
                       hands[1] + self.BAT_LEN * d[1],
                       hands[2] + self.BAT_LEN * d[2])
            sweet = None

        # NOTE: the bat currently sweeps THROUGH the torso mid-downswing for
        # every tracked hitter — measured across all 201 on the board, median
        # 23% of the swing. A per-frame
        # collision correction was tried here and removed: rotating the bat
        # about its sweet spot costs none of the measured quantities, but the
        # pose it is correcting is off by ~17cm (the shaft crosses the spine
        # axis itself), so the rotation needed is ~120 deg and the result
        # jerks far worse than the penetration it fixes. The fix belongs in
        # the path solve — see the shape note in `_solve_swing`.

        # Reach. This used to translate the WHOLE upper body onto the hands by
        # the full overshoot, uncapped, which is where the figure's 109 cm of
        # head travel came from (a real hitter moves his head ~9 cm, and even
        # the reference swing only moves it 44). The grip is now pulled into
        # reach by rotating the bat about its sweet spot instead — see
        # `_reach_cap` — so all that is left here is a small, BOUNDED lean for
        # whatever the rotation could not absorb, which is a real thing hitters
        # do to cover the outer third. Softplus-bounded, so it never pops.
        lead_sh = (ls_x, sh_y, ls_z)
        need = math.dist(hands, lead_sh) - self.ARM_REACH
        if need > 0.0:
            k = self.LEAN_MAX * 0.5
            lean = self.LEAN_MAX * (1.0 - math.exp(-need / max(1e-6, k)))
            u = _norm((hands[0] - lead_sh[0], hands[1] - lead_sh[1],
                       hands[2] - lead_sh[2]))
            for dxu, w_ in ((lean, 1.0),):
                sc_x += u[0] * dxu * w_
                sc_z += u[2] * dxu * w_
                ls_x += u[0] * dxu * w_
                ls_z += u[2] * dxu * w_
                rs_x += u[0] * dxu * w_
                rs_z += u[2] * dxu * w_
            ax_ += u[0] * lean * 0.45
            az_ += u[2] * lean * 0.45
            lh_x += u[0] * lean * 0.45
            lh_z += u[2] * lean * 0.45
            rh_x += u[0] * lean * 0.45
            rh_z += u[2] * lean * 0.45

        # Head. Taken from the reference as an offset off the shoulder centre,
        # in WORLD axes and deliberately NOT rotated by the shoulder yaw: the
        # head staying still while the shoulders turn under it is the whole
        # point. The reference's own head moves 3.3 cm vertically across the
        # entire swing, which is what "head still" actually looks like.
        head_c = (sc_x, sh_y + head_dy, sc_z)
        rh_ = swing_ref_at(s_now)
        if rh_ is not None:
            # Anchored to the SHOULDER CENTRE. Anchoring to the pelvis was
            # tried and is worse (58.5 cm of head travel against 53.5): our
            # pelvis carries the weight shift forward and the reference head
            # moves forward too, so the two ADD, where the shoulder centre's
            # yaw swing partly cancels them.
            #
            # What is taken is the head's MOTION relative to the shoulders, not
            # its absolute offset: the reference's head marker sits ~35 cm above
            # sh_c (it is the top of the skull, and mocap marker placement is
            # not this figure's proportions), so using the raw offset stretched
            # the neck to 29 cm and left the head visibly floating. Subtracting
            # the offset at the load makes s=0 identical to the pose this figure
            # already had, and everything after it is the mocap's.
            r0_ = swing_ref_at(0.0)
            dx_ = ((rh_["head"][0] - rh_["sh_c"][0])
                   - (r0_["head"][0] - r0_["sh_c"][0]))
            dy_ = ((rh_["head"][1] - rh_["sh_c"][1])
                   - (r0_["head"][1] - r0_["sh_c"][1]))
            dz_ = ((rh_["head"][2] - rh_["sh_c"][2])
                   - (r0_["head"][2] - r0_["sh_c"][2]))
            head_c = (sc_x + dx_, sh_y + head_dy + dy_, sc_z + tp * dz_)

        return {
            "front_f": front_f, "back_f": back_f, "toes": toes,
            "lead_hip": (lh_x, hip_y, lh_z), "rear_hip": (rh_x, hip_y, rh_z),
            "pelvis_c": ((lh_x + rh_x) / 2.0, hip_y, (lh_z + rh_z) / 2.0),
            "shoulder_c": (sc_x, sh_y, sc_z),
            "lead_sh": (ls_x, sh_y, ls_z), "rear_sh": (rs_x, sh_y, rs_z),
            "head_c": head_c,
            "hands": hands, "bat_tip": bat_tip, "sweet": sweet,
            "sh_y": sh_y, "hip_y": hip_y, "head_dy": head_dy,
        }

    def _draw_batter_figure(self, b, bx, bz, bside, sg, pp, setup):
        """Draw the figure from `_body_pose` — GL only, no geometry."""
        rec = getattr(self, "_recover", None)
        if rec is not None and sg is not None and 0.0 < rec < 1.0:
            # Ease the whole pose from the finish back to the load. Lerping
            # the JOINTS (rather than the phase) is what makes this possible —
            # phase 1.0 -> 0.0 would replay the swing backwards.
            a = self._body_pose(b, bx, bz, bside, sg, 1.0, False)
            c = self._body_pose(b, bx, bz, bside, sg, 0.0, True)
            w = _smoothstep(rec)
            j = {}
            for k, va in a.items():
                vc = c.get(k)
                if isinstance(va, tuple) and vc is not None and len(va) == 3 \
                        and all(isinstance(x, (int, float)) for x in va):
                    j[k] = _lerp3(va, vc, w)
                elif isinstance(va, (int, float)) and isinstance(vc, (int, float)):
                    j[k] = va + (vc - va) * w
                else:
                    j[k] = vc if w > 0.5 else va
        else:
            j = self._body_pose(b, bx, bz, bside, sg, pp, setup)
        front_f, back_f, toes = j["front_f"], j["back_f"], j["toes"]
        lead_hip, rear_hip = j["lead_hip"], j["rear_hip"]
        pelvis_c, shoulder_c = j["pelvis_c"], j["shoulder_c"]
        lead_sh, rear_sh = j["lead_sh"], j["rear_sh"]
        hands, bat_tip, sweet = j["hands"], j["bat_tip"], j["sweet"]
        ls_x, ls_z = lead_sh[0], lead_sh[2]
        rs_x, rs_z = rear_sh[0], rear_sh[2]
        sc_x, sc_z = shoulder_c[0], shoulder_c[2]
        sh_y, head_dy = j["sh_y"], j["head_dy"]

        glLineWidth(3.4)
        glColor4f(0.40, 0.88, 0.98, 0.95)
        glBegin(GL_LINES)
        for f, t in zip((front_f, back_f), toes):
            heel = (f[0] - 0.07 * t[0], f[1] + 0.01, f[2] - 0.07 * t[1])
            toe = (f[0] + 0.15 * t[0], f[1] + 0.01, f[2] + 0.15 * t[1])
            self._seg(heel, toe)
            # toe splay — an asymmetric foot so which way he stands is legible
            for sgn in (1.0, -1.0):
                self._seg(toe, (toe[0] - 0.05 * t[0] - sgn * 0.045 * t[1],
                                toe[1],
                                toe[2] - 0.05 * t[1] + sgn * 0.045 * t[0]))
        glEnd()

        glLineWidth(2.6)
        glColor4f(0.55, 0.90, 0.98, 0.95)
        glBegin(GL_LINES)
        # Legs: two-bone IK per side, the knee tracking the way that foot
        # points, so the legs never cross or scissor through each other.
        for hip_pt, foot, t in ((lead_hip, front_f, toes[0]),
                                (rear_hip, back_f, toes[1])):
            kdir = _norm((t[0], 0.22, t[1]))
            k = self._elbow(hip_pt, foot, arm=1.14, bend_ref=kdir,
                            min_flex=0.10)
            self._seg(hip_pt, k)
            self._seg(k, foot)
        self._seg(lead_hip, rear_hip)               # pelvis line
        self._seg(pelvis_c, shoulder_c)             # spine
        self._seg(lead_sh, rear_sh)                 # shoulder line
        # Arms: both shoulders IK to the shared grip, elbows splayed to their
        # own side so the two arms + the shoulder line read as a diamond.
        sax = (ls_x - rs_x, 0.0, ls_z - rs_z)
        sl = math.hypot(sax[0], sax[2]) or 1.0
        sax = (sax[0] / sl, 0.0, sax[2] / sl)
        for shoulder, ref in ((lead_sh, (sax[0], -0.35, sax[2])),
                              (rear_sh, (-sax[0] * 1.1, -0.35, -sax[2] * 1.1))):
            elbow = self._elbow(shoulder, hands, bend_ref=ref)
            self._seg(shoulder, elbow)
            self._seg(elbow, hands)
        glEnd()

        # Bat — one rigid link from the grip, with the knob behind the hands
        glLineWidth(4.2)
        glColor4f(0.86, 0.73, 0.46, 0.98)
        glBegin(GL_LINES)
        self._seg(hands, bat_tip)
        bd = _norm((bat_tip[0] - hands[0], bat_tip[1] - hands[1],
                    bat_tip[2] - hands[2]))
        self._seg(hands, (hands[0] - bd[0] * 0.06, hands[1] - bd[1] * 0.06,
                          hands[2] - bd[2] * 0.06))
        glEnd()
        if sweet is not None:                       # sweet spot on the barrel
            glPointSize(5.0)
            glColor4f(0.98, 0.86, 0.55, 0.9)
            glBegin(GL_POINTS)
            glVertex3f(*self._p2m(*sweet))
            glEnd()

        # Head — from the pose, which now carries it off the reference swing
        # rather than welding it to the top of the spine.
        hc_ = j["head_c"]
        glLineWidth(2.2)                            # cap bill — he looks out at
        glColor4f(0.55, 0.90, 0.98, 0.9)            # the pitcher, so this reads
        glBegin(GL_LINES)                           # as which way he is turned
        self._seg((hc_[0] + 0.07, hc_[1], hc_[2]),
                  (hc_[0] + 0.20, hc_[1] - 0.02, hc_[2]))
        glEnd()
        glLineWidth(2.2)                            # neck
        glColor4f(0.55, 0.90, 0.98, 0.9)
        glBegin(GL_LINES)
        self._seg(shoulder_c, (hc_[0], hc_[1] - 0.10, hc_[2]))
        glEnd()
        glLineWidth(1.8)                            # head
        glBegin(GL_LINE_LOOP)
        for k in range(18):
            a = 2 * math.pi * k / 18
            glVertex3f(*self._p2m(hc_[0] + 0.10 * math.cos(a),
                                  hc_[1] + 0.10 * math.sin(a), hc_[2]))
        glEnd()

    def _draw_swing_path(self, sg, p, pp):
        """The swing PATH as a trailing arc, sampled from the SAME pendulum the
        bat is drawn from — so the marker rides the drawn barrel instead of
        drifting off it, which is what the old separate-circle overlay did.
        A faint full guide, the bright color-graded portion already swept, the
        bat-head marker, and the intercept dot + attack-angle tangent that
        lights up at contact."""
        STEPS = 56

        # Faint full-swing guide
        glLineWidth(1.4)
        glColor4f(0.45, 0.60, 0.80, 0.16)
        glBegin(GL_LINE_STRIP)
        for k in range(STEPS + 1):
            glVertex3f(*self._p2m(*self._pose_at(sg, k / STEPS)["sweet"]))
        glEnd()

        # Swept path so far — bright, color-graded (blue load → amber/red
        # extension), the trail building behind the barrel as it swings
        if pp > 0.001:
            glLineWidth(4.0)
            glBegin(GL_LINE_STRIP)
            SW = max(2, int(STEPS * pp))
            for k in range(SW + 1):
                f = pp * k / SW
                glColor4f(0.25 + 0.72 * f, 0.55 + 0.25 * (1 - abs(f - 0.5) * 2),
                          0.95 - 0.72 * f, 0.95)
                glVertex3f(*self._p2m(*self._pose_at(sg, f)["sweet"]))
            glEnd()

        # Glowing barrel marker at the current pose
        glPointSize(9.0)
        glColor4f(1.0, 0.96, 0.72, 0.98)
        glBegin(GL_POINTS)
        glVertex3f(*self._p2m(*self._pose_at(sg, pp)["sweet"]))
        glEnd()

        # Intercept dot (always) + attack-angle tangent (once contact reached)
        C, atk = sg["C"], sg["atk"]
        glLineWidth(1.6)
        glColor4f(1.0, 0.95, 0.55, 0.90)
        glBegin(GL_LINE_LOOP)
        for k in range(16):
            a = 2 * math.pi * k / 16
            glVertex3f(*self._p2m(C[0] + 0.05 * math.cos(a),
                                  C[1] + 0.05 * math.sin(a), C[2]))
        glEnd()
        if p is not None and p >= sg["s_c"] - 0.02:
            glLineWidth(3.4)
            glColor4f(0.98, 0.32, 0.30, 0.98)
            glBegin(GL_LINES)
            glVertex3f(*self._p2m(*C))
            glVertex3f(*self._p2m(C[0] + atk[0] * 0.5, C[1] + atk[1] * 0.5,
                                  C[2] + atk[2] * 0.5))
            glEnd()

    def _draw_strike_zone(self):
        half_w = 0.216   # meters (17" plate width / 2)
        corners = [self.physics_to_model(0.0, y, z)
                   for y, z in ((self.sz_bot_m, -half_w),
                                (self.sz_bot_m, half_w),
                                (self.sz_top_m, half_w),
                                (self.sz_top_m, -half_w))]
        glPushAttrib(GL_LIGHTING_BIT | GL_CURRENT_BIT | GL_ENABLE_BIT
                     | GL_LINE_BIT)
        glDisable(GL_LIGHTING)
        glLineWidth(2.0)
        glColor4f(0.9, 0.92, 0.95, 0.85)
        glBegin(GL_LINE_LOOP)
        for cx, cy, cz in corners:
            glVertex3f(cx, cy, cz)
        glEnd()
        # Faint thirds grid
        glLineWidth(1.0)
        glColor4f(0.9, 0.92, 0.95, 0.28)
        glBegin(GL_LINES)
        for f in (1 / 3, 2 / 3):
            y = self.sz_bot_m + (self.sz_top_m - self.sz_bot_m) * f
            a = self.physics_to_model(0.0, y, -half_w)
            b = self.physics_to_model(0.0, y, half_w)
            glVertex3f(*a)
            glVertex3f(*b)
            z = -half_w + 2 * half_w * f
            a = self.physics_to_model(0.0, self.sz_bot_m, z)
            b = self.physics_to_model(0.0, self.sz_top_m, z)
            glVertex3f(*a)
            glVertex3f(*b)
        glEnd()
        glPopAttrib()

    def paintGL(self):
        # Black backdrop instead of the HR widget's bright sky — a Tron-style
        # neon grid + contour mound + glowing plate carry the whole scene.
        glClearColor(0.015, 0.02, 0.03, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()
        gluPerspective(self.camera['fov'],
                       self.width() / max(self.height(), 1), 0.1, 500)
        gluLookAt(*self.camera['pos'], *self.camera['target'],
                  *self.camera['up'])

        glDisable(GL_COLOR_MATERIAL)
        self._draw_tron_grid()
        self._draw_mound()
        self._draw_home_plate()
        if self.show_zone:
            self._draw_strike_zone()
        if self.show_release:
            self._draw_release_markers()
        if self.show_batter:
            self._draw_batter()

        # Trails — HR widget's dot pipeline, but each dot keeps the color
        # of the pitch that laid it so overlaid arsenals stay readable
        for i, entry in enumerate(self.ball_trail):
            (x, y, z), alpha = entry[0], entry[1] * 0.9
            r, g, b = (entry[2] if len(entry) == 3
                       else (self.pitch_trail_color or (1.0, 1.0, 1.0)))
            glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [r, g, b, alpha])
            glMaterialfv(GL_FRONT, GL_SPECULAR,
                         [r * 0.5, g * 0.5, b * 0.5, alpha])
            glMaterialf(GL_FRONT, GL_SHININESS, 80.0)
            # Mild emission keeps the pitch color saturated against the
            # bright sky instead of washing out under the scene lighting
            glMaterialfv(GL_FRONT, GL_EMISSION,
                         [r * 0.45, g * 0.45, b * 0.45, alpha])
            glPushMatrix()
            glTranslatef(x, y, z)
            sphere = gluNewQuadric()
            gluQuadricDrawStyle(sphere, GLU_FILL)
            gluQuadricNormals(sphere, GLU_SMOOTH)
            gluSphere(sphere, 0.09, 8, 8)
            gluDeleteQuadric(sphere)
            glPopMatrix()
        if self.ball_trail:
            glMaterialfv(GL_FRONT, GL_EMISSION, [0.0, 0.0, 0.0, 1.0])

        # Ball + shadow — verbatim UmpireView3D look (speed tint, velocity
        # stretch, ground shadow), trail append carries the pitch color
        if self.ball_pos is not None:
            x, y, z = self.ball_pos
            ball_speed = 0
            if self.ball_vel is not None:
                vx, vy, vz = self.ball_vel
                ball_speed = (vx**2 + vy**2 + vz**2)**0.5
                if self.prev_ball_pos is not None:
                    px, py, pz = self.prev_ball_pos
                    dist = ((x-px)**2 + (y-py)**2 + (z-pz)**2)**0.5
                    if dist > self.trail_min_dist:
                        self.ball_trail.insert(0, ((x, y, z), 0.7,
                                               self.pitch_trail_color
                                               or (1.0, 1.0, 1.0)))
            self.prev_ball_pos = (x, y, z)

            red = min(1.0, 0.8 + ball_speed / 30)
            green = max(0.7, 1.0 - ball_speed / 20)
            blue = max(0.7, 1.0 - ball_speed / 20)
            glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE,
                         [red, green, blue, 1.0])
            glMaterialfv(GL_FRONT, GL_SPECULAR, [1.0, 1.0, 1.0, 1.0])
            glMaterialf(GL_FRONT, GL_SHININESS, 80.0)
            glPushMatrix()
            glTranslatef(x, y, z)
            if self.ball_vel is not None and ball_speed > 5:
                vx, vy, vz = self.ball_vel
                norm = ball_speed
                vx, vy, vz = vx / norm, vy / norm, vz / norm
                stretch = min(1.5, 1.0 + ball_speed / 30)
                if ball_speed > 10:
                    axis_x, axis_y = -vy, vx
                    axis_len = (axis_x**2 + axis_y**2) ** 0.5
                    if axis_len > 0.001:
                        axis_x, axis_y = axis_x / axis_len, axis_y / axis_len
                        angle = math.degrees(
                            math.acos(max(-1.0, min(1.0, vz))))
                        glRotatef(angle, axis_x, axis_y, 0)
                        glScalef(1.0, 1.0, stretch)
            sphere = gluNewQuadric()
            gluQuadricDrawStyle(sphere, GLU_FILL)
            gluQuadricNormals(sphere, GLU_SMOOTH)
            gluSphere(sphere, 0.2, 16, 16)
            gluDeleteQuadric(sphere)
            glPopMatrix()

            glPushMatrix()
            glTranslatef(x, self._model_hp[1] + 0.02, z)
            height = y - self._model_hp[1]
            shadow_alpha = max(0.1, min(0.6, 0.6 - height / 50))
            glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE,
                         [0.0, 0.0, 0.0, shadow_alpha])
            glMaterialfv(GL_FRONT, GL_SPECULAR, [0.0, 0.0, 0.0, 0.0])
            glMaterialf(GL_FRONT, GL_SHININESS, 0.0)
            glRotatef(-90, 1, 0, 0)
            shadow = gluNewQuadric()
            gluDisk(shadow, 0, 0.28, 16, 1)
            gluDeleteQuadric(shadow)
            glPopMatrix()


class SPFlightWindow(QMainWindow):
    """Pop-out arsenal flight viewer: slim chrome, colored pitch chips
    (click = fly, trails persist), Fly Arsenal sequencing, HR-widget
    rendering underneath."""

    def __init__(self, pitcher_name: str, pitches: list, sz_top: float,
                 sz_bot: float, colors: dict, abbrev: dict,
                 start_pitch: str = "", arm_angle=None, batter=None,
                 parent=None):
        super().__init__(parent)
        title = f"{pitcher_name} — Arsenal Flight"
        if batter and batter.get("name"):
            title += f"  vs  {batter['name']}"
        self.setWindowTitle(title)
        self.setMinimumSize(940, 620)
        self._pitches = [p for p in pitches if p.get("kin")]
        self._colors = colors
        self._abbrev = abbrev
        self._arm_angle = arm_angle
        self._batter = batter
        self._queue: list = []
        self.trajectory_data = None
        self._contact_frame = None       # ball-reaches-intercept frame, or None
        self._swing_start_frame = 0      # downswing start (contact − 150 ms)
        self._follow_frames = 1          # follow-through length in frames
        self._extra_frames = 0           # ticks past the last ball frame
        self.current_frame = 0
        self.slowdown = 4.0
        self.frame_accumulator = 0.0

        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self._update_animation)

        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)

        # One slim toolbar: name · pitch chips · transport · camera · speed
        bar = QHBoxLayout()
        bar.setSpacing(6)
        name_lbl = QLabel(pitcher_name)
        name_lbl.setObjectName("flightName")
        bar.addWidget(name_lbl)
        total = sum(p["n"] for p in self._pitches) or 1
        for p in self._pitches:
            code = self._abbrev.get(p["pitch"], p["pitch"])
            color = self._colors.get(code, "#95A5A6")
            velo = f" {p['velo']:.0f}" if p.get("velo") else ""
            btn = QPushButton(f"{code}{velo}")
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"{p['pitch']} · {p['n'] / total:.0%} usage · "
                           f"HB {p['mean_hb']:+.1f}\" iVB {p['mean_ivb']:+.1f}\"")
            btn.setStyleSheet(
                f"QPushButton {{ color: {color}; background: transparent;"
                f" border: 1px solid #2C3E50; border-radius: 3px;"
                f" font-size: 9pt; font-weight: bold; padding: 2px 7px; }}"
                f"QPushButton:hover {{ border-color: {color}; }}")
            btn.clicked.connect(lambda _, pp=p: self._launch(pp))
            bar.addWidget(btn)
        bar.addSpacing(8)
        fly_all = QPushButton("Fly Arsenal")
        fly_all.setObjectName("flightAction")
        fly_all.clicked.connect(self._fly_arsenal)
        bar.addWidget(fly_all)
        clear = QPushButton("Clear")
        clear.setObjectName("flightAction")
        clear.clicked.connect(self._clear)
        bar.addWidget(clear)
        bar.addStretch()

        self.cam_combo = QComboBox()
        for name in PitchFlightView.CAMERA_PRESETS:
            self.cam_combo.addItem(name)
        bar.addWidget(self.cam_combo)
        bar.addWidget(QLabel("Slow"))
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(1, 12)
        self.speed_slider.setValue(4)
        self.speed_slider.setFixedWidth(80)
        self.speed_slider.valueChanged.connect(
            lambda v: setattr(self, "slowdown", float(v)))
        bar.addWidget(self.speed_slider)
        self.zone_check = QCheckBox("Zone")
        self.zone_check.setChecked(True)
        bar.addWidget(self.zone_check)
        self.rel_check = QCheckBox("Release")
        self.rel_check.setChecked(True)
        self.rel_check.setToolTip("Release-point markers (per pitch) + arm-slot"
                                  " vector")
        bar.addWidget(self.rel_check)
        self.bat_check = QCheckBox("Batter")
        self.bat_check.setChecked(bool(batter))
        self.bat_check.setEnabled(bool(batter))
        self.bat_check.setToolTip("Batter in the box: stance position, swing"
                                  " plane (attack angle/direction), intercept")
        bar.addWidget(self.bat_check)
        lay.addLayout(bar)

        arm_txt = (f"  ·  arm slot {arm_angle:.0f}°"
                   if arm_angle is not None else "")
        bat_txt = ""
        if batter and batter.get("attack_angle") is not None:
            ad = batter.get("attack_dir") or 0.0
            bat_txt = (f"  ·  {batter.get('name', 'batter')}: attack "
                       f"{batter['attack_angle']:.0f}° "
                       f"{abs(ad):.0f}° {'pull' if ad < 0 else 'oppo'}")
        self.info_label = QLabel("Click a pitch chip, or fly the arsenal · "
                                 "drag to orbit, wheel to zoom"
                                 + arm_txt + bat_txt)
        self.info_label.setObjectName("flightInfo")
        self.info_label.setFixedHeight(16)
        lay.addWidget(self.info_label)

        self.view = PitchFlightView()
        self.view.sz_top_m = sz_top * FT_TO_M
        self.view.sz_bot_m = sz_bot * FT_TO_M
        # Feed the release-point overlay (per-pitch release markers + arm slot)
        self.view.release_pitches = self._pitches
        self.view.release_colors = colors
        self.view.release_abbrev = abbrev
        self.view.arm_angle = arm_angle
        self.view.batter = batter
        self.view.show_batter = bool(batter)
        lay.addWidget(self.view, stretch=1)
        self.cam_combo.currentTextChanged.connect(self.view.set_camera_preset)
        self.zone_check.toggled.connect(
            lambda v: (setattr(self.view, "show_zone", v),
                       self.view.update()))
        self.rel_check.toggled.connect(
            lambda v: (setattr(self.view, "show_release", v),
                       self.view.update()))
        self.bat_check.toggled.connect(
            lambda v: (setattr(self.view, "show_batter", v),
                       self.view.update()))

        self.setStyleSheet("""
            QMainWindow { background-color: #10151c; }
            QLabel { color: #95A5A6; font-size: 9pt; }
            #flightName { color: #dc9437; font-size: 10pt; font-weight: bold; }
            #flightInfo { color: #7F8C8D; font-size: 8pt; }
            QComboBox { background: #1E2A38; color: #ddd;
                        border: 1px solid #34495E; padding: 2px 6px; }
            QCheckBox { color: #BDC3C7; font-size: 9pt; }
            #flightAction { background: #1E2A38; color: #BDC3C7;
                            border: 1px solid #34495E; border-radius: 3px;
                            padding: 2px 10px; font-size: 9pt; }
            #flightAction:hover { border-color: #dc9437; color: #ECF0F1; }
        """)

        if start_pitch:
            p = next((p for p in self._pitches if p["pitch"] == start_pitch),
                     None)
            if p is not None:
                QTimer.singleShot(150, lambda: self._launch(p))

    # ------------------------------------------------------------ playback

    def _clear(self):
        self.animation_timer.stop()
        self._queue = []
        self.view.clear_trails()

    def _fly_arsenal(self):
        if not self._pitches:
            return
        self.view.clear_trails()
        self._queue = list(self._pitches[1:])
        self._launch(self._pitches[0])

    def _launch(self, p):
        kin = p["kin"]
        self.trajectory_data = savant_pitch_trajectory(
            vx0=kin["vx0"], vy0=kin["vy0"], vz0=kin["vz0"],
            ax=kin["ax"], ay=kin["ay"], az=kin["az"],
            release_pos_x=kin["release_pos_x"],
            release_pos_y=kin["release_pos_y"],
            release_pos_z=kin["release_pos_z"],
            num_points=220,
        )
        code = self._abbrev.get(p["pitch"], p["pitch"])
        self.view.pitch_trail_color = _hex_to_gl(
            self._colors.get(code, "#95A5A6"))
        # New pitch = fresh segment: no streak from the last plate crossing
        self.view.prev_ball_pos = None

        # Batter swing timing: find the frame the ball reaches the intercept
        # depth so the bat sweeps through contact exactly as the ball arrives.
        self._contact_frame = None
        self._extra_frames = 0
        bat = self.view.batter
        if (self.view.show_batter and bat
                and bat.get("attack_angle") is not None):
            ivp = bat.get("intercept_vs_plate") or 0.0
            cx_ft = (0.216 + ivp * 0.0254) * 3.28084   # intercept depth (ft)
            xs = self.trajectory_data["x"]
            self._contact_frame = next(
                (i for i in range(len(xs)) if xs[i] <= cx_ft),
                int(len(xs) * 0.85))
            # The swing runs on a REAL clock: this hitter's own solved
            # downswing (~130-160 ms) ending on the contact frame, plus a
            # proportional follow-through, whatever the pitch's velocity.
            # Anchoring it to a fraction of the flight (what it used to do)
            # made the swing stretch and shrink with velo.
            ts = self.trajectory_data["time"]
            dt = (ts[-1] - ts[0]) / max(1, len(ts) - 1)
            t_down = self.view.swing_seconds()
            t_follow = t_down * (_FOLLOW_S / _DOWNSWING_S)
            self._swing_start_frame = max(
                0, self._contact_frame - int(round(t_down / max(dt, 1e-6))))
            self._follow_frames = max(1, int(round(t_follow / max(dt, 1e-6))))
            self._extra_frames = max(
                0, self._follow_frames - (len(xs) - self._contact_frame))
        self.view.reset_swing()
        velo = f" · {p['velo']:.1f} mph" if p.get("velo") else ""
        self.info_label.setText(
            f"{p['pitch']}{velo} · HB {p['mean_hb']:+.1f}\" · "
            f"iVB {p['mean_ivb']:+.1f}\" · plate "
            f"{self.trajectory_data['plate_speed_mph']:.1f} mph")
        self.current_frame = 0
        self.frame_accumulator = 0.0
        self.animation_timer.start(16)

    def _update_animation(self):
        td = self.trajectory_data
        if td is None:
            return
        n = len(td["time"])
        # The pitch ends at the plate but the swing does not — the barrel still
        # owes ~110 ms of follow-through. Keep the clock running past the last
        # ball frame for however many frames that takes, with the ball hidden,
        # so the finish plays at its real speed instead of being snapped to.
        n_end = n + self._extra_frames
        frames_per_tick = n / (td["plate_time"] * 60 * self.slowdown)
        self.frame_accumulator += frames_per_tick
        while self.frame_accumulator >= 1.0 and self.current_frame < n_end:
            self.frame_accumulator -= 1.0
            self.current_frame += 1
        if self.current_frame >= n_end:
            self.animation_timer.stop()
            self.current_frame = 0
            self.view.ball_pos = None
            if self._contact_frame is not None:
                self.view.set_swing_phase(1.0)   # hold the finished follow-through
                self.view.set_foot_phase(1.0)    # ...on the planted front foot
                self._start_recover()
            self.view.update()
            if self._queue and self._contact_frame is None:
                QTimer.singleShot(300, self._next_in_queue)
            return
        i = self.current_frame
        v = self.view
        if self._contact_frame is not None:
            v.set_swing_phase(self._swing_phase_for(i, n))
            # Feet run on the pitch clock: frame 0 IS pitch release, and the
            # scraped intercept lands on the contact frame.
            v.set_foot_phase(min(1.0, i / max(1, self._contact_frame)))
        if i >= n:                          # ball is gone; finish the swing
            v.ball_pos = None
            v.update()
            return
        # ft → m → model coords (positions through the full transform,
        # velocity through rotation+scale only)
        v.ball_pos = v.physics_to_model(td["x"][i] * FT_TO_M,
                                        td["y"][i] * FT_TO_M,
                                        td["z"][i] * FT_TO_M)
        dvx, dvy, dvz = v._pm_dir(td["vx"][i] * FT_TO_M,
                                  td["vy"][i] * FT_TO_M,
                                  td["vz"][i] * FT_TO_M)
        s = v._model_scale
        v.ball_vel = (dvx * s, dvy * s, dvz * s)
        v.update()

    def _swing_phase_for(self, i, n):
        """Map the pitch frame `i` to swing progress on the real clock: still
        until the downswing's start frame, linear to contact at the intercept
        frame, then follow through over the next ~120 ms. The phase→pose curve
        (which is where the acceleration lives) is the view's kinematic
        sequence, so this stays a plain time map."""
        cf, sf = self._contact_frame, self._swing_start_frame
        pc = self.view.contact_phase()
        if i <= sf:
            return 0.0
        if i <= cf:
            return pc * (i - sf) / max(1, cf - sf)
        return min(1.0, pc + (1 - pc) * (i - cf) / max(1, self._follow_frames))

    # Hold the finish, then EASE back to the load pose. The next pitch only
    # goes once that is done, so a queued arsenal flyby never cuts from a
    # wrapped follow-through straight to a cocked stance.
    _RECOVER_HOLD_MS = 260
    _RECOVER_MS = 420

    def _start_recover(self):
        self._recover_t0 = None
        if getattr(self, "_recover_timer", None) is None:
            self._recover_timer = QTimer(self)
            self._recover_timer.timeout.connect(self._tick_recover)
        QTimer.singleShot(self._RECOVER_HOLD_MS, self._begin_recover)

    def _begin_recover(self):
        import time as _t
        self._recover_t0 = _t.monotonic()
        self._recover_timer.start(16)

    def _tick_recover(self):
        import time as _t
        if self._recover_t0 is None:
            return
        r = (_t.monotonic() - self._recover_t0) * 1000.0 / self._RECOVER_MS
        if r >= 1.0:
            self._recover_timer.stop()
            self._recover_t0 = None
            self.view.reset_swing()
            self.view.update()
            if self._queue:
                QTimer.singleShot(120, self._next_in_queue)
            return
        self.view.set_recover(r)
        self.view.update()

    def _next_in_queue(self):
        if self._queue:
            self._launch(self._queue.pop(0))

    def closeEvent(self, ev):
        self.animation_timer.stop()
        super().closeEvent(ev)
