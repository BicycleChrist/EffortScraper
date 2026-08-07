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
_SEQ_BARREL = 0.86
_SEQ_K = 7.0

# Sanity anchor for the whole timing model: a ~7.2 ft sweet-spot path covered
# in ~140 ms ending at ~72 mph is what the league-median row on Savant's board
# implies, and a ramp whose mean speed is half its peak reproduces all three at
# once. That is why the barrel is still ACCELERATING at contact here — the
# pelvis/torso/arm have already peaked and are decelerating (the sequence), but
# the hands peak last, at the ball.
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

    def reset_swing(self):
        self._swing_phase = None
        self._foot_phase = None

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

    # Bat geometry. Every Savant swing metric (attack angle/direction, tilt,
    # swing length) is measured on the SWEET SPOT, ~6" in from the tip, so
    # that is the point the solve pins to the ball; the tip is drawn beyond.
    BAT_LEN = 0.84
    BAT_SWEET = 0.66
    ARM_REACH = 0.76        # shoulder → hands, before the torso has to lean
    PIVOT_SHIFT = 0.55      # how far the pivot rides in toward the body at load

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
        What that buys over the fixed-radius circle this replaced is an
        effective radius that GROWS through the swing, a barrel that
        accelerates into the zone instead of sweeping at a constant rate, a
        path that starts steep and flattens into the tilt plane, and a bat that
        is rigid at every instant — the grip is the other end of the same link,
        not a point pinned onto the barrel after the fact.

        Solved against the measured data, in this order:
          · the swing PLANE, from swing tilt, containing the attack vector;
          · the pivot, placed so the barrel is tangent to the measured attack
            angle and direction at the intercept point — exactly, by
            construction;
          · `dphi`, bisected so the sweet spot's PATH LENGTH over the downswing
            equals the measured swing length;
          · `t_down`, from the measured bat speed — how long the downswing
            takes, rather than a constant for every hitter;
          · `lag0`, aiming the grip back at the rear shoulder at load (the one
            piece the data says nothing about, and it moves none of the above).

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
        # measured from two origins — league-wide, batter_y_position +
        # intercept_y_vs_plate == intercept_y_vs_batter to within a third of an
        # inch (median 28.6 + 2.8 vs 31.0). So intercept_vs_batter is a DEPTH
        # in front of the hitter, not a lateral reach out over the plate, and
        # using it as one (as this did) pushed contact ~2 ft too far toward the
        # plate and ~1.5 ft too far out front. Depth comes from vs_plate;
        # laterally the board has no coordinate, and contact averages out
        # essentially over the plate, a touch to the batter's side.
        C = (0.216 + ivp * IN, 0.90, bz * 0.10)

        # Sweet-spot velocity at contact: forward (+X toward the pitcher), up by
        # the attack angle, azimuth off centre by the attack direction.
        a_h = _norm((math.cos(adr), 0.0, -bside * math.sin(adr)))
        atk = _norm((a_h[0] * math.cos(av), math.sin(av), a_h[2] * math.cos(av)))

        # Swing-plane normal. The plane must (a) contain the attack vector (the
        # barrel is tangent to it at contact) and (b) sit at swing_tilt from
        # horizontal, i.e. its NORMAL sits at swing_tilt from vertical — a flat
        # swing is a horizontal plane with a vertical normal. Among the
        # perpendiculars to atk, n = cos·u1 + sin·w with u1 the up-most ⟂-atk
        # direction (itself `attack_angle` off vertical) and w horizontal ⟂-atk;
        # n·up = cos(a)·cos(attack), so cos(a) = cos(tilt)/cos(attack) leans it
        # exactly swing_tilt off vertical. Two solutions (the plane leans either
        # way) — keep the one that rides HIGH on the hitter's side and low out
        # over the plate, which is the way a swing plane actually leans.
        #
        # NOTE: this used to finish with n = cross(atk, N), which takes a vector
        # lying IN the plane and treats it as the normal — 90° out. Every swing
        # came out sweeping a near-VERTICAL plane (a loop up over the hitter's
        # head) no matter what his tilt actually was, which is most of why the
        # old overlay looked wrong from every camera.
        u1 = _norm((-atk[1] * atk[0], 1.0 - atk[1] * atk[1], -atk[1] * atk[2]))
        w = _norm(_cross(atk, (0.0, 1.0, 0.0)))
        cos_a = max(-1.0, min(1.0, math.cos(tr) / max(1e-6, math.cos(av))))
        sin_a = math.sqrt(max(0.0, 1.0 - cos_a * cos_a))
        best = None
        for s in (sin_a, -sin_a):
            n_c = _norm((cos_a * u1[0] + s * w[0], cos_a * u1[1] + s * w[1],
                         cos_a * u1[2] + s * w[2]))
            if n_c[1] < 0.0:
                n_c = (-n_c[0], -n_c[1], -n_c[2])
            score = -n_c[2] * bside                    # high on his side
            if best is None or score > best[0]:
                best = (score, n_c)
        n_c = best[1]

        # The swing is a rotation about ONE axis: `n`, the normal of the tilt
        # plane, running through the hitter's own body. Both the hands and the
        # barrel sweep circles about that axis — parallel planes, the barrel's
        # offset from the hands' by however far the bat sticks out along the
        # axis. That offset is the whole reason this cannot be modelled as a
        # single plane through the contact point: the hands are ~0.7 m off the
        # barrel's plane, so projecting the body into it (as a plane-through-C
        # model must) puts the pivot out over the plate and the swing comes out
        # rotating the wrong way entirely.
        e2 = atk                                   # in-plane, = attack vector
        e1 = _norm(_cross(n_c, e2))                # in-plane, ⟂ attack vector

        # Barrel pivot. Every quantity Savant measures is a property of the
        # BARREL — the plane it sweeps, the direction and angle it crosses the
        # ball at, how far it travels, how fast it is going. So the barrel's
        # path is what gets solved, and the body is hung off it afterwards.
        #
        # (A body-first solve was tried: hands on a circle about the spine, bat
        # as a second link. It cannot be made to fit. Savant puts contact ~31"
        # in FRONT of the hitter, and at that depth every rotation about his
        # spine carries the barrel ~40 deg to the pull side of the measured
        # attack direction — the barrel at contact is extending, not orbiting
        # the spine, so its instantaneous centre is not the spine at all.)
        #
        # The pivot therefore sits perpendicular to the attack vector inside the
        # swing plane, up and behind the ball, which makes the barrel tangent to
        # the measured attack angle at contact by construction.
        tp = -bside                                # +1 = toward the plate
        m = e1 if e1[1] >= 0.0 else (-e1[0], -e1[1], -e1[2])   # up-and-back
        f1 = (-m[0], -m[1], -m[2])                 # pivot → ball at contact
        f2 = atk                                   # tangent at contact
        R_c = 1.05                                 # barrel radius at contact
        O = (C[0] + R_c * m[0], C[1] + R_c * m[1], C[2] + R_c * m[2])

        sg = {"C": C, "atk": atk, "n": n_c, "e1": e1, "e2": e2, "m": m,
              "f1": f1, "f2": f2, "O": O, "R_c": R_c, "dR": 0.30,
              "O_load": O, "Ls": Ls, "BAT": self.BAT_LEN, "s_c": _CONTACT_P,
              "lag0": math.radians(-62.0), "wrap": math.radians(150.0),
              "h_contact": C, "h_finish": C, "roll": 0.0,
              "dphi": 2.2, "gn0": 0.0, "k_barrel": _SEQ_K,
              "t_down": _DOWNSWING_S}

        # Axis roll. Savant's swing tilt is defined over the 40 ms BEFORE
        # contact only, so it is honoured exactly there and the earlier path is
        # allowed to stand up steeper — the shape a real barrel traces, and it
        # keeps the load from being tipped through the hitter's own head.
        th = math.radians(16.0)
        sg["roll"] = max((th, -th),
                         key=lambda t: -abs(n_c[1] * math.cos(t)
                                            - f1[1] * math.sin(t)))

        # The two measured scalars then fix the rest:
        #   dphi   — how far the barrel swings, bisected so its PATH LENGTH over
        #            the downswing equals the measured swing length;
        #   t_down — how long that takes, from the measured bat speed. The
        #            barrel's radius is momentarily stationary at contact (the
        #            growth term is squared), so its speed there is simply
        #            R_c·dphi·B'(1)/t_down and this is a division, not a search.
        target = (b.get("swing_length") or 7.2) * FT_TO_M
        v_target = max(1.0, (b.get("bat_speed") or 72.0) * _MPH)

        # The pivot MIGRATES. Held fixed, a 130 deg sweep back from a pivot
        # that sits ~0.3 m outside the hitter parks the barrel at load about
        # 0.7 m off his back side — sticking out sideways rather than cocked
        # over the rear shoulder, which is what read as the hitter facing the
        # wrong way. A real barrel starts at the shoulder and the centre of its
        # curvature travels out as the arms extend. So the pivot eases from a
        # load position (the one that puts the barrel over the rear shoulder)
        # to the solved contact pivot, arriving by the 40 ms window where the
        # measurements live — value AND rate settled, so tangency, attack angle
        # and bat speed at contact are untouched.
        # Anchor the load by the GRIP and the bat's angle out of it — hands
        # back at the rear shoulder, barrel up and behind it — and let the
        # barrel's own start follow from that, rather than placing the barrel
        # and hoping the grip lands somewhere human.
        # Pull the pivot toward the hitter's rear shoulder at load, capped, so
        # the barrel starts near his body instead of ~0.7 m off his back side.
        # Pinning the load barrel to an exact pose instead makes the pivot
        # travel so far that most of the measured swing length is covered by
        # that translation, which flattens the speed profile and squeezes the
        # downswing under 110 ms — well short of the ~140 ms a real one takes.
        rear_sh = (bx - 0.19, 1.45, bz)
        shift = (rear_sh[0] - O[0], rear_sh[1] - O[1], rear_sh[2] - O[2])
        slen = math.sqrt(_dot(shift, shift)) or 1.0
        k = min(1.0, self.PIVOT_SHIFT / slen)
        sg["O_load"] = (O[0] + shift[0] * k, O[1] + shift[1] * k,
                        O[2] + shift[2] * k)
        grip_load = (bx - 0.18, 1.30, bz - tp * 0.04)
        if True:
            lo, hi = 0.5, 4.0
            for _ in range(30):
                mid = 0.5 * (lo + hi)
                sg["dphi"] = mid
                if self._sweet_path_len(sg) < target:
                    lo = mid
                else:
                    hi = mid
            sg["dphi"] = 0.5 * (lo + hi)
        B1 = _seq_d(1.0, _SEQ_BARREL, sg["k_barrel"])
        sg["t_down"] = max(0.100, min(0.220, R_c * sg["dphi"] * B1 / v_target))
        # Where the GRIP starts. The barrel's path is measured, but nothing in
        # the data says where the hands are on it, and letting the lag angle
        # default left them out over the plate at load instead of back at the
        # rear shoulder. So aim the bat at load: the barrel is wherever the
        # solved arc starts, and the lag is set so the grip points back at the
        # rear shoulder from there. The lag then unwinds to zero at contact, so
        # this costs the measured quantities nothing — the barrel path, the
        # attack angle and the bat speed are all untouched by it.
        p0 = self._pose_at(sg, 0.0)
        d = (p0["sweet"][0] - grip_load[0], p0["sweet"][1] - grip_load[1],
             p0["sweet"][2] - grip_load[2])
        roll0 = sg["roll"]
        cr, sr = math.cos(roll0), math.sin(roll0)
        f1r = (f1[0] * cr + n_c[0] * sr, f1[1] * cr + n_c[1] * sr,
               f1[2] * cr + n_c[2] * sr)
        nr = (n_c[0] * cr - f1[0] * sr, n_c[1] * cr - f1[1] * sr,
              n_c[2] * cr - f1[2] * sr)
        a1, a2, an = _dot(d, f1r), _dot(d, f2), _dot(d, nr)
        ang = math.atan2(a2, a1)
        lag = ang - (-sg["dphi"])
        while lag > math.pi:
            lag -= 2 * math.pi
        while lag < -math.pi:
            lag += 2 * math.pi
        sg["lag0"] = max(math.radians(-150.0), min(math.radians(20.0), lag))
        sg["gn0"] = max(-2.0, min(2.0, an / max(1e-6, math.hypot(a1, a2))))

        sg["tangent_err"] = self._tangent_err(sg)
        sg["contact_mph"] = self._contact_speed(sg) / _MPH
        # Follow-through anchors: where the grip is at contact, and where it
        # finishes — up and across in front of the lead shoulder.
        sg["h_contact"] = self._pose_at(sg, sg["s_c"])["hands"]
        sg["h_finish"] = (bx + 0.16, 1.42, bz + tp * 0.26)
        return sg

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

    def _sweet_path_len(self, sg, steps=28):
        """Length of the sweet spot's path over the downswing (m) — the thing
        Savant's swing length measures."""
        prev, tot = None, 0.0
        for k in range(steps + 1):
            q = self._pose_at(sg, sg["s_c"] * k / steps)["sweet"]
            if prev is not None:
                tot += math.dist(prev, q)
            prev = q
        return tot

    def _pose_at(self, sg, s):
        """Hands / sweet spot / bat tip at swing progress `s` (0..1).

        The barrel rides its solved arc, its radius growing into contact as the
        wrists release — that growth is what makes the barrel accelerate into
        the zone rather than sweep at a constant rate. The bat TRAILS the radius
        by a lag angle that unwinds late (the "late hit"), and the hands are the
        other end of that same rigid bat, so the grip and the barrel can never
        disagree. Both the radius growth and the lag are squared/eased to zero
        at contact, which leaves the barrel exactly tangent to the measured
        attack vector there."""
        s_c = sg["s_c"]
        if s <= s_c:
            tau = s / s_c if s_c else 1.0
            B = _seq(tau, _SEQ_BARREL, sg["k_barrel"])
            W = _seq(tau, _SEQ_WRIST)
            phi = -sg["dphi"] * (1.0 - B)
            R = sg["R_c"] - sg["dR"] * (1.0 - B) ** 2
            psi = sg["lag0"] * (1.0 - W)
            settle = _smoothstep(tau / 0.72)
            roll = sg["roll"] * (1.0 - settle)
            O = _lerp3(sg["O_load"], sg["O"], settle)
        else:
            # Past contact the HANDS lead, not the barrel: they decelerate and
            # pull back in toward the lead shoulder while the bat keeps turning
            # about them and wraps over that shoulder. Driving the wrap off the
            # barrel's arc instead flips the bat end-for-end and flings the grip
            # ~2 m off the body, which no amount of torso lean can absorb.
            t2 = (s - s_c) / max(1e-6, 1.0 - s_c)
            F = 1.0 - (1.0 - min(1.0, t2)) ** 2          # decelerating
            settle = 1.0
            hands = _lerp3(sg["h_contact"], sg["h_finish"], F)
            wr = sg["wrap"] * F
            cb, sb = math.cos(wr), math.sin(wr)
            f1, f2 = sg["f1"], sg["f2"]
            bd = (cb * f1[0] + sb * f2[0], cb * f1[1] + sb * f2[1],
                  cb * f1[2] + sb * f2[2])
            sweet = (hands[0] + sg["Ls"] * bd[0], hands[1] + sg["Ls"] * bd[1],
                     hands[2] + sg["Ls"] * bd[2])
            tip = (hands[0] + sg["BAT"] * bd[0], hands[1] + sg["BAT"] * bd[1],
                   hands[2] + sg["BAT"] * bd[2])
            return {"hands": hands, "sweet": sweet, "tip": tip, "dir": bd}
        f1, f2, n = sg["f1"], sg["f2"], sg["n"]
        if roll:                                    # stand the early plane up
            cr, sr = math.cos(roll), math.sin(roll)
            f1, n = (f1[0] * cr + n[0] * sr, f1[1] * cr + n[1] * sr,
                     f1[2] * cr + n[2] * sr), \
                    (n[0] * cr - sg["f1"][0] * sr, n[1] * cr - sg["f1"][1] * sr,
                     n[2] * cr - sg["f1"][2] * sr)
        cp, sp = math.cos(phi), math.sin(phi)
        sweet = (O[0] + R * (cp * f1[0] + sp * f2[0]),
                 O[1] + R * (cp * f1[1] + sp * f2[1]),
                 O[2] + R * (cp * f1[2] + sp * f2[2]))
        # The bat leans OUT of the swing plane early — a cocked bat sticks up
        # out of the plane its barrel will later sweep, and forcing it flat in
        # there is what left the grip stranded across the chest at load. The
        # lean is gone by the time the plane settles, so contact is unaffected.
        cb, sb = math.cos(phi + psi), math.sin(phi + psi)
        gn = sg["gn0"] * (1.0 - settle)
        bd = _norm((cb * f1[0] + sb * f2[0] + gn * n[0],
                    cb * f1[1] + sb * f2[1] + gn * n[1],
                    cb * f1[2] + sb * f2[2] + gn * n[2]))
        hands = (sweet[0] - sg["Ls"] * bd[0], sweet[1] - sg["Ls"] * bd[1],
                 sweet[2] - sg["Ls"] * bd[2])
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

    def _draw_batter_figure(self, b, bx, bz, bside, sg, pp, setup):
        """Batter driven by ONE kinematic chain, feet → pelvis → torso →
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

        # Stable weight point. The pelvis sits over the REAR hip at load and
        # eases toward the feet centre on the pelvis's own slot in the sequence.
        feet_cx = (front_f[0] + back_f[0]) / 2.0
        feet_cz = (front_f[2] + back_f[2]) / 2.0
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

        # Pelvis and shoulders turn on SEPARATE clocks: the pelvis fires first
        # and the shoulders lag ~25° behind it at load (the X-factor), then
        # close that gap and pass it through contact.
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

        # Reach: if the hands are further from the shoulder than an arm is
        # long, the TORSO goes to them. Leaning is what a hitter actually does
        # to cover the outer third; stretching the arm is what used to make the
        # figure snap into a straight, ballooned limb.
        lead_sh = (ls_x, sh_y, ls_z)
        need = math.dist(hands, lead_sh) - self.ARM_REACH
        if need > 0.0:
            u = _norm((hands[0] - lead_sh[0], hands[1] - lead_sh[1],
                       hands[2] - lead_sh[2]))
            for dxu, w_ in ((need, 1.0),):
                sc_x += u[0] * dxu * w_
                sc_z += u[2] * dxu * w_
                ls_x += u[0] * dxu * w_
                ls_z += u[2] * dxu * w_
                rs_x += u[0] * dxu * w_
                rs_z += u[2] * dxu * w_
            ax_ += u[0] * need * 0.45
            az_ += u[2] * need * 0.45
            lh_x += u[0] * need * 0.45
            lh_z += u[2] * need * 0.45
            rh_x += u[0] * need * 0.45
            rh_z += u[2] * need * 0.45

        lead_hip = (lh_x, hip_y, lh_z)
        rear_hip = (rh_x, hip_y, rh_z)
        pelvis_c = ((lh_x + rh_x) / 2.0, hip_y, (lh_z + rh_z) / 2.0)
        shoulder_c = (sc_x, sh_y, sc_z)
        lead_sh = (ls_x, sh_y, ls_z)
        rear_sh = (rs_x, sh_y, rs_z)

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

        glLineWidth(2.2)                            # cap bill — he looks out at
        glColor4f(0.55, 0.90, 0.98, 0.9)            # the pitcher, so this reads
        glBegin(GL_LINES)                           # as which way he is turned
        hy = sh_y + head_dy
        self._seg((sc_x + 0.07, hy, sc_z), (sc_x + 0.20, hy - 0.02, sc_z))
        glEnd()
        glLineWidth(2.2)                            # neck
        glColor4f(0.55, 0.90, 0.98, 0.9)
        glBegin(GL_LINES)
        self._seg(shoulder_c, (sc_x, sh_y + head_dy - 0.10, sc_z))
        glEnd()
        glLineWidth(1.8)                            # head
        glBegin(GL_LINE_LOOP)
        for k in range(18):
            a = 2 * math.pi * k / 18
            glVertex3f(*self._p2m(sc_x + 0.10 * math.cos(a),
                                  sh_y + head_dy + 0.10 * math.sin(a), sc_z))
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
            self.view.update()
            if self._queue:
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

    def _next_in_queue(self):
        if self._queue:
            self._launch(self._queue.pop(0))

    def closeEvent(self, ev):
        self.animation_timer.stop()
        super().closeEvent(ev)
