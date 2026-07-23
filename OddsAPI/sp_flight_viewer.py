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

import math

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


class PitchFlightView(UmpireView3D):
    """UmpireView3D without the stadium: minimal plate/mound scene tuned
    for pitch flight, per-dot colored trails for arsenal overlays, preset +
    orbit cameras. All rendering constants (ball, shadow, sky, lighting)
    are the HR widget's own."""

    # Camera presets in PHYSICS coordinates (meters): X toward the mound,
    # Y up, Z toward 1B. Transformed through physics_to_model at use time.
    CAMERA_PRESETS = {
        "Umpire":    {"pos": (-1.6, 1.75, 0.0), "target": (17.0, 1.3, 0.0),
                      "fov": 55},
        "Batter":    {"pos": (-0.3, 1.70, -0.85), "target": (17.5, 1.7, 0.0),
                      "fov": 62},
        "Pitcher":   {"pos": (19.6, 1.9, 0.0), "target": (0.0, 0.9, 0.0),
                      "fov": 50},
        "1B Side":   {"pos": (9.2, 3.2, -11.0), "target": (9.2, 1.6, 0.0),
                      "fov": 42},
        "3B Side":   {"pos": (9.2, 3.2, 11.0), "target": (9.2, 1.6, 0.0),
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
        """Pitcher's mound as glowing contour rings (a topographic look that
        reads as a simulator) + a white rubber bar. Real geometry: 18-ft
        circle, 10-in peak, 5-ft flat table, 1-in/ft slope."""
        r, g, b = self._GRID_RGB
        cx = 18.0            # mound center (~59 ft from plate)
        R = 2.74            # 9-ft radius
        peak = 0.254        # 10 in
        plateau = 0.76      # 2.5-ft table radius

        def h_at(rr):
            if rr <= plateau:
                return peak
            return max(0.0, peak - (rr - plateau) * (0.0254 / 0.3048))

        glPushAttrib(GL_LIGHTING_BIT | GL_ENABLE_BIT | GL_LINE_BIT
                     | GL_CURRENT_BIT)
        glDisable(GL_LIGHTING)
        glEnable(GL_BLEND)
        glLineWidth(1.4)
        for rr in (0.4, plateau, 1.2, 1.7, 2.2, R):
            h = h_at(rr)
            glColor4f(r, g, b, max(0.12, 0.5 * (1.0 - rr / (R * 1.35))))
            glBegin(GL_LINE_LOOP)
            for k in range(56):
                th = 2 * math.pi * k / 56
                glVertex3f(*self._p2m(cx + rr * math.cos(th), h,
                                      rr * math.sin(th)))
            glEnd()
        # Radial contour spokes (follow the slope profile)
        glColor4f(r, g, b, 0.16)
        glBegin(GL_LINES)
        for k in range(12):
            th = 2 * math.pi * k / 12
            prev = None
            for rr in (0.0, plateau, 1.4, 2.0, R):
                pt = self._p2m(cx + rr * math.cos(th), h_at(rr),
                               rr * math.sin(th))
                if prev is not None:
                    glVertex3f(*prev)
                    glVertex3f(*pt)
                prev = pt
        glEnd()
        # Rubber: 24" x 6" white slab atop the table
        pr, pg, pb = self._PLATE_RGB
        glColor4f(pr, pg, pb, 0.95)
        glBegin(GL_POLYGON)
        for px, pz in ((18.44 - 0.076, 0.305), (18.44 + 0.076, 0.305),
                       (18.44 + 0.076, -0.305), (18.44 - 0.076, -0.305)):
            glVertex3f(*self._p2m(px, peak + 0.012, pz))
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
                 start_pitch: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{pitcher_name} — Arsenal Flight")
        self.setMinimumSize(940, 620)
        self._pitches = [p for p in pitches if p.get("kin")]
        self._colors = colors
        self._abbrev = abbrev
        self._queue: list = []
        self.trajectory_data = None
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
        lay.addLayout(bar)

        self.info_label = QLabel("Click a pitch chip, or fly the arsenal · "
                                 "drag to orbit, wheel to zoom")
        self.info_label.setObjectName("flightInfo")
        self.info_label.setFixedHeight(16)
        lay.addWidget(self.info_label)

        self.view = PitchFlightView()
        self.view.sz_top_m = sz_top * FT_TO_M
        self.view.sz_bot_m = sz_bot * FT_TO_M
        lay.addWidget(self.view, stretch=1)
        self.cam_combo.currentTextChanged.connect(self.view.set_camera_preset)
        self.zone_check.toggled.connect(
            lambda v: (setattr(self.view, "show_zone", v),
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
        frames_per_tick = n / (td["plate_time"] * 60 * self.slowdown)
        self.frame_accumulator += frames_per_tick
        while self.frame_accumulator >= 1.0 and self.current_frame < n:
            self.frame_accumulator -= 1.0
            self.current_frame += 1
        if self.current_frame >= n:
            self.animation_timer.stop()
            self.current_frame = 0
            self.view.ball_pos = None
            self.view.update()
            if self._queue:
                QTimer.singleShot(300, self._next_in_queue)
            return
        i = self.current_frame
        v = self.view
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

    def _next_in_queue(self):
        if self._queue:
            self._launch(self._queue.pop(0))

    def closeEvent(self, ev):
        self.animation_timer.stop()
        super().closeEvent(ev)
