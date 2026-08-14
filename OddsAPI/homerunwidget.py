# TODO: Train lightweight XGBoost/LightGBM model on BBE data (~15-18 features: launch_speed,
#       launch_angle, hla, bb_type, release_speed, spin_rate, spin_axis, pfx_x/z, altitude,
#       temp, humidity, wind_speed/dir, baro_pressure, park_id) to predict hit distance.
#       Compare ML prediction vs physics engine vs actual hit_distance_sc per BBE event.
#       Inference is microseconds so no perf hit. Filter training to FB/LD with distance > 150ft.

import pathlib

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QWidget,
    QPushButton, QSlider, QLabel, QComboBox, QGroupBox, QSpinBox, QCheckBox,
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsPathItem,
    QGraphicsItemGroup, QGraphicsLineItem, QGraphicsRectItem, QGridLayout, QListWidget, QDoubleSpinBox, QTabWidget,
    QSizePolicy, QProgressBar, QStackedWidget
)
# Import QOpenGLWidget from the correct module
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtOpenGL import QOpenGLShaderProgram, QOpenGLShader
from PyQt6.QtGui import QMatrix4x4
from OpenGL.GL import *
from OpenGL.GLU import *
from OpenGL.GLUT import *
from OpenGL.GLUT.fonts import *
from PyQt6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush, QPainterPath, QSurfaceFormat, QIcon, QLinearGradient, QRadialGradient, QPolygonF
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, QSizeF, QPoint, pyqtSignal, QPropertyAnimation, QEasingCurve, QThread, QObject
import sys
import random
import time
import numpy as np
import math
from scipy.integrate import solve_ivp
from weatherman import WeatherService, STADIUM_DATA, get_stadium_wall_distance, get_stadium_wall_height, WindVectorWidget, get_park_for_team, get_park_azimuth, effective_wind, resolve_park_name
from player_overlay_widget import PlayerBBEOverlay
from weatherman import open_weather_key
from pathlib import Path
from pywavefront import Wavefront
import csv
from pitch_sim import savant_row_to_pitch_trajectory

LOG_BALL_PHYSICS = False

# ==============================================
# ML Distance Residual Predictor
# Loads model_data/distance_residual_model.lgb (trained by train_distance_model.py).
# Adds a learned residual to the physics distance to match Statcast hit_distance_sc.
# ==============================================
_ML_MODEL = None
_ML_MODEL_TRIED = False

def _load_ml_distance_model():
    global _ML_MODEL, _ML_MODEL_TRIED
    if _ML_MODEL_TRIED:
        return _ML_MODEL
    _ML_MODEL_TRIED = True
    try:
        import lightgbm as lgb
        path = Path(__file__).parent / "model_data" / "distance_residual_model.lgb"
        if not path.exists():
            print(f"[ml] distance model not found at {path}")
            return None
        _ML_MODEL = lgb.Booster(model_file=str(path))
        print(f"[ml] loaded distance residual model ({len(_ML_MODEL.feature_name())} features)")
    except Exception as e:
        print(f"[ml] failed to load distance model: {e}")
        _ML_MODEL = None
    return _ML_MODEL


def predict_distance_residual(record: dict, park: str):
    """Predict the residual (ft) to add to physics distance for a BBE.

    Returns None when the model is unavailable, the park is unknown to the
    model, or required features are missing.
    """
    model = _load_ml_distance_model()
    if model is None or not park:
        return None
    try:
        import pandas as pd
        feat_names = model.feature_name()
        row = {}
        for name in feat_names:
            if name == "park":
                row[name] = park
            elif name == "pitch_type":
                row[name] = record.get("pitch_type") or "UNK"
            elif name == "spray_angle":
                hc_x = record.get("hc_x")
                hc_y = record.get("hc_y")
                if hc_x is None or hc_y is None:
                    return None
                hc_x, hc_y = float(hc_x), float(hc_y)
                if math.isnan(hc_x) or math.isnan(hc_y):
                    return None
                dx = hc_x - 125.42
                dy = 198.27 - hc_y
                row[name] = math.degrees(math.atan2(dx, dy)) if dy > 0 else 0.0
            else:
                v = record.get(name)
                try:
                    row[name] = float(v) if v is not None else float("nan")
                except (TypeError, ValueError):
                    row[name] = float("nan")
        df = pd.DataFrame([row], columns=feat_names)
        for cat_col in ("park", "pitch_type"):
            if cat_col in df.columns:
                df[cat_col] = df[cat_col].astype("category")
        return float(model.predict(df)[0])
    except Exception as e:
        print(f"[ml] residual prediction failed: {e}")
        return None


# Foul balls without spray cords are sim'd as 0 deg HLA and therefore end up in CF within BBE events
# Pitch trail colors by pitch type (matches test_pitch_viewer.py)
PITCH_TRAIL_COLORS = {
    "4-Seam Fastball": (1.0, 0.3, 0.3),
    "Sinker":          (1.0, 0.5, 0.2),
    "Cutter":          (0.9, 0.2, 0.5),
    "Slider":          (0.3, 0.7, 1.0),
    "Sweeper":         (0.2, 0.5, 1.0),
    "Curveball":       (0.4, 1.0, 0.4),
    "Knuckle Curve":   (0.3, 0.9, 0.5),
    "Changeup":        (1.0, 0.8, 0.2),
    "Splitter":        (0.8, 0.6, 1.0),
}


# Ballpark model in baseballfield.obj file is at 'pos': [-515.808441, 41.099228, -760.366211]
# Load the materials as well if possible from baseballfield.mtl
#TODO: Implement animated spray chart with controls in player_overlay widget
fmt = QSurfaceFormat()
fmt.setSamples(4)
fmt.setDepthBufferSize(24)
fmt.setStencilBufferSize(8)
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)



# ==============================================
# Ball Flight Physics Component
# ==============================================
class BallFlightSimulator:
    # ------------------------------------------------------------------
    # Barometric pressure & altitude handling
    #
    # Air density (rho) is the key atmospheric input to both the drag and
    # Magnus force terms in the ODE.  It is computed via calculate_air_density
    # and fed into baseball_ode as a callable (rho_fn) rather than a fixed
    # scalar, so density correctly decreases as the ball gains height during
    # each solver step.
    #
    # Pressure source priority:
    #   1. OpenWeather grnd_level field (actual station pressure) — preferred
    #   2. OpenWeather pressure field (sea-level normalised / SLP) — fallback
    #   3. ISA standard atmosphere derived from stadium altitude — last resort
    #
    # At low-altitude parks the difference between SLP and station pressure is
    # small (~1-2 %) and has negligible effect on simulated carry.  Above
    # HIGH_ALTITUDE_THRESHOLD_FT the divergence becomes material — at Coors
    # Field (~5280 ft) SLP overstates station pressure by ~20 %, which would
    # meaningfully underestimate how far the ball carries.  Above the threshold
    # we therefore derive station pressure from the ISA formula and blend in
    # the day's weather anomaly (SLP deviation from ISA standard) so the
    # actual atmospheric conditions are still reflected in the simulation.
    # ------------------------------------------------------------------

    def __init__(self):
        # Constants
        self.g = 9.81  # m/s^2, gravity
        self.m = 0.145  # kg, baseball mass
        self.d = 0.074  # m, baseball diameter
        self.C_d = 0.40  # drag coefficient (calibrated 2026 to zero physics bias on 5k BBE sample;
                         # high end of Nathan's 0.30–0.45 literature range, absorbs missing drag-crisis effect)
        self.C_l = 0.2  # lift coefficient (for Magnus effect)
        self.omega = 1800  # rpm, typical spin rate

    # Stadiums at or above this elevation trigger the high-altitude pressure
    # correction described above.  1500 ft is the point where the SLP error
    # (~4 % density impact) starts producing a noticeable difference in carry.
    HIGH_ALTITUDE_THRESHOLD_FT = 1500

    def calculate_trajectory(self, exit_velocity, vlaunch_angle, hlaunch_angle, wind_speed, wind_direction,
                             temp, humidity, altitude, start_x=0, start_y=0.91, start_z=0,
                             pressure_pa=None, cd_override=None, cl_override=None, omega_override=None,
                             park_azimuth=None, pressure_is_station=False, wind_profile=None,
                             tol=None):
        """Calculate ball trajectory based on initial conditions and environment

        Standard coordinate system:
        - X axis: From home plate toward pitcher's mound/center field (positive)
        - Y axis: Vertical (up is positive)
        - Z axis: From home plate toward third base/left field (negative) or first base/right field (positive)

        wind_direction is the direction the wind blows FROM, and it is
        interpreted in the FIELD frame: 0 = in from centre field, 90 = from
        the first-base side, 180 = out to centre field.

        pressure_pa is assumed SEA-LEVEL normalised, because that is what
        OpenWeather's `main.pressure` is.  Pass pressure_is_station=True for a
        reading already taken at field level — Open-Meteo's surface_pressure,
        or OpenWeather's grnd_level — otherwise the high-altitude branch will
        "correct" an already-correct number into a badly wrong one.

        Pass `park_azimuth` (the home-plate -> centre-field compass bearing,
        weatherman.get_park_azimuth) whenever wind_direction came from a
        weather feed, which reports bearings from true north.  Without it a
        forecast bearing is silently read as a field angle, so the same
        reading blows out to centre in every park regardless of which way the
        park actually faces.  Omit it only when the caller has already
        rotated, or when there is no wind to rotate.
        """
        # Convert inputs to SI units
        v0 = exit_velocity * 0.44704  # mph to m/s
        vertical_angle = np.radians(vlaunch_angle)
        horizontal_angle = np.radians(hlaunch_angle)
        wind = wind_speed * 0.44704  # mph to m/s
        # Compass -> field frame.  Both conventions are "blows from" and both
        # run clockwise seen from above, so this is a plain subtraction.
        if park_azimuth is not None:
            wind_direction = (float(wind_direction) - float(park_azimuth)) % 360.0
        wind_rad = np.radians(wind_direction)

        # ----------------------------------------------------------------
        # Height-varying wind.
        # A fly ball spends most of its flight between 10 and 45 m, but a
        # forecast reports 10 m.  Wind grows with height, so a single surface
        # value understates what the ball actually flies through — and it is
        # the one environmental input still applied as a flat constant, while
        # air density has varied with height for years (rho_fn above).
        #
        # `wind_profile` is a callable h_m -> speed_mph, normally built by
        # weatherman.wind_profile() from the 10/80/120 m levels.  It is scaled
        # so that profile(10) matches the wind_speed passed in, which keeps any
        # receptivity factor applied by the caller intact.
        # ----------------------------------------------------------------
        if wind_profile is not None:
            ref = wind_profile(10.0)
            scale = (wind / (ref * 0.44704)) if ref and ref > 0.1 else 0.0

            def wind_fn(y_m):
                return wind_profile(max(y_m, 0.5)) * 0.44704 * scale
        else:
            def wind_fn(y_m):
                return wind

        # ----------------------------------------------------------------
        # High-altitude correction
        # OpenWeather's main.pressure field is sea-level-normalised (SLP).
        # At stadiums above the threshold this diverges significantly from
        # actual station pressure, so we derive station pressure from the
        # ISA formula instead — it is more accurate than SLP for physics.
        # ----------------------------------------------------------------
        corrected_pressure_pa = pressure_pa
        if pressure_is_station and pressure_pa:
            # Already the pressure at field level — the reconstruction below
            # exists only to undo sea-level normalisation, and running it on a
            # station reading is destructive: 840 hPa at Coors comes out as
            # 664, a 21 % density error worth ~27 ft of carry.  Open-Meteo's
            # surface_pressure and OpenWeather's grnd_level are both station.
            pass
        elif altitude >= self.HIGH_ALTITUDE_THRESHOLD_FT:
            # Derive station pressure from ISA standard atmosphere
            altitude_m = altitude * 0.3048
            p0_isa = 101325.0
            T0_isa = 288.15
            g_isa  = 9.81
            L_isa  = 0.0065
            R_isa  = 8.31447
            M_isa  = 0.0289644
            station_pressure_pa = p0_isa * (1 - L_isa * altitude_m / T0_isa) ** ((g_isa * M_isa) / (R_isa * L_isa))
            if pressure_pa is not None and pressure_pa > 0:
                # Blend: trust the SLP-derived *anomaly* (today's weather deviation
                # from ISA standard) but anchor it to the correct station level.
                # anomaly = SLP - standard SLP; apply to station pressure.
                slp_standard_pa = p0_isa  # ISA sea-level standard
                anomaly_pa = pressure_pa - slp_standard_pa  # e.g. +800 Pa if high pressure day
                corrected_pressure_pa = station_pressure_pa + anomaly_pa
                print(f"[High-altitude correction] Stadium altitude {altitude} ft "
                      f"| SLP={pressure_pa/100:.1f} hPa "
                      f"| Station ISA={station_pressure_pa/100:.1f} hPa "
                      f"| Corrected={corrected_pressure_pa/100:.1f} hPa")
            else:
                corrected_pressure_pa = station_pressure_pa
                print(f"[High-altitude correction] Stadium altitude {altitude} ft "
                      f"| No API pressure — using ISA station pressure "
                      f"{station_pressure_pa/100:.1f} hPa")

        # ----------------------------------------------------------------
        # Build a height-aware rho function.
        # The ODE calls rho_fn(y_metres_above_field) at every solver step
        # so air density correctly decreases as the ball climbs.
        # ----------------------------------------------------------------
        def rho_fn(y_m):
            return self.calculate_air_density(
                temp, humidity, altitude,
                pressure_pa=corrected_pressure_pa,
                extra_altitude_m=y_m
            )

        # Initial conditions [x, y, z, vx, vy, vz]
        initial_state = [
            start_x, start_y, start_z,
            v0 * np.cos(vertical_angle) * np.cos(horizontal_angle),
            v0 * np.sin(vertical_angle),
            v0 * np.cos(vertical_angle) * np.sin(horizontal_angle)
        ]

        t_span = (0, 10)  # 10 seconds covers any realistic ball flight

        # Optional aerodynamic-coefficient overrides (used by calibration scripts).
        # Save & restore so this stays a per-call override, not a side-effect.
        _saved_cd, _saved_cl, _saved_omega = self.C_d, self.C_l, self.omega
        if cd_override    is not None: self.C_d   = cd_override
        if cl_override    is not None: self.C_l   = cl_override
        if omega_override is not None: self.omega = omega_override
        try:
            # `tol` loosens the solver for display-quality work. The default
            # 1e-6 with a 0.05 s cap is calibration precision; a carry figure
            # rounded to the foot does not need it, and a slate of parks is
            # hundreds of solves.
            _rtol, _atol, _mstep = (1e-6, 1e-6, 0.05) if tol is None else tol
            solution = solve_ivp(
                lambda t, y: self.baseball_ode(t, y, wind_fn, wind_rad, rho_fn),
                t_span,
                initial_state,
                method='RK45',
                max_step=_mstep,
                rtol=_rtol,
                atol=_atol,
                first_step=0.01
            )
        finally:
            self.C_d, self.C_l, self.omega = _saved_cd, _saved_cl, _saved_omega

        # Convert position to imperial units for display
        x = solution.y[0] * 3.28084  # m to ft (distance toward center field)
        y = solution.y[1] * 3.28084  # m to ft (height)
        z = solution.y[2] * 3.28084  # m to ft (left/right field distance)

        # Also extract velocities from the solution
        vx = solution.y[3] * 3.28084  # m/s to ft/s
        vy = solution.y[4] * 3.28084  # m/s to ft/s
        vz = solution.y[5] * 3.28084  # m/s to ft/s

        # Find landing point (where y reaches field level)
        field_level = start_y * 3.28084
        landing_idx = np.argmax(y < field_level)
        if landing_idx == 0 and y[-1] > field_level:
            landing_idx = len(y) - 1

        # INTERPOLATE the crossing rather than taking the first sample below
        # it. The solver's steps do not land on the fence-height crossing, so
        # reading the sample after it reports the ball wherever the integrator
        # happened to stop -- measured at 2.98, 0.22, -2.59 and -11.30 ft below
        # contact height for the same batted ball at different step caps, i.e.
        # several feet of purely numerical noise on every distance, and a
        # distance that moved when the tolerance did.
        if landing_idx > 0 and y[landing_idx] < field_level <= y[landing_idx - 1]:
            y0, y1 = y[landing_idx - 1], y[landing_idx]
            frac = (y0 - field_level) / (y0 - y1) if y0 != y1 else 0.0
            xi = x[landing_idx - 1] + frac * (x[landing_idx] - x[landing_idx - 1])
            zi = z[landing_idx - 1] + frac * (z[landing_idx] - z[landing_idx - 1])
        else:
            xi, zi = x[landing_idx], z[landing_idx]

        # Calculate total horizontal distance
        distance = np.sqrt(xi**2 + zi**2)

        return {
            "time": solution.t[:landing_idx+1],
            "x": x[:landing_idx+1],  # Center field direction
            "y": y[:landing_idx+1],  # Height
            "z": z[:landing_idx+1],  # Left/right field direction
            "vx": vx[:landing_idx+1],  # Velocity in x direction
            "vy": vy[:landing_idx+1],  # Velocity in y direction
            "vz": vz[:landing_idx+1],  # Velocity in z direction
            "distance": distance,
            "start_x": start_x * 3.28084,
            "start_y": start_y * 3.28084,
            "start_z": start_z * 3.28084
        }

    def baseball_ode(self, t, state, wind_fn, wind_direction, rho_fn):
        """ODE system for baseball flight with height-varying air density.

        Parameters:
        t (float): Time variable for time-dependent forces
        state (array): Current state [x, y, z, vx, vy, vz]
        wind_fn (callable): wind_fn(y_m) -> wind speed m/s at height y_m above
                            the field.  Evaluated every solver step so the wind
                            grows with height the way the real profile does;
                            a constant function reproduces the old behaviour.
        wind_direction (float): Wind direction in radians
        rho_fn (callable): Function rho_fn(y_m) -> air density kg/m³ at height y_m
                           above field level.  Evaluated at every solver step so that
                           density correctly decreases as the ball climbs.

        Returns:
        array: Derivatives [dx/dt, dy/dt, dz/dt, dvx/dt, dvy/dt, dvz/dt]
        """
        x, y, z, vx, vy, vz = state

        # Air density at the ball's current height above field level
        air_density = rho_fn(max(y, 0.0))  # clamp to 0 — no negative heights in density calc

        # Wind at the ball's current height, then a time-dependent wobble.
        wind_speed = wind_fn(max(y, 0.0))
        wind_variation = 0.1 * np.sin(2 * np.pi * t)  # 10% variation with 1 Hz frequency
        current_wind_speed = wind_speed * (1 + wind_variation)

        # Wind direction can also vary with time
        dir_variation = np.radians(5) * np.sin(np.pi * t)  # ±5 degrees variation
        current_wind_direction = wind_direction + dir_variation

        # Calculate wind components
        wind_x = current_wind_speed * np.cos(current_wind_direction)
        wind_z = current_wind_speed * np.sin(current_wind_direction)

        # Relative velocity (ball velocity minus wind velocity)
        v_rel_x = vx + wind_x
        v_rel_y = vy  # no vertical wind component
        v_rel_z = vz + wind_z

        v_rel = np.sqrt(v_rel_x**2 + v_rel_y**2 + v_rel_z**2)

        # Drag force — slight time-based increase simulates ball absorbing moisture/wear
        drag_time_factor = 1.0 + 0.05 * min(t, 5.0)  # max 25 % increase over 5 s

        A = np.pi * (self.d / 2) ** 2  # cross-sectional area m²
        F_drag = 0.5 * air_density * v_rel**2 * self.C_d * A * drag_time_factor

        # Magnus force — spin decays exponentially
        spin_decay = np.exp(-0.1 * t)
        current_omega = self.omega * spin_decay
        omega_rad = current_omega * 2 * np.pi / 60  # rpm → rad/s

        F_magnus = 0.5 * air_density * v_rel * self.d**3 * self.C_l * omega_rad

        # Unit vector of relative velocity
        if v_rel > 0:
            v_rel_unit_x = v_rel_x / v_rel
            v_rel_unit_y = v_rel_y / v_rel
            v_rel_unit_z = v_rel_z / v_rel
        else:
            v_rel_unit_x, v_rel_unit_y, v_rel_unit_z = 0, 0, 0

        # Drag acceleration components
        ax_drag = -F_drag * v_rel_unit_x / self.m
        ay_drag = -F_drag * v_rel_unit_y / self.m
        az_drag = -F_drag * v_rel_unit_z / self.m

        # Magnus acceleration (vertical lift)
        ay_magnus = F_magnus / self.m

        ax = ax_drag
        ay = ay_drag + ay_magnus - self.g
        az = az_drag

        return [vx, vy, vz, ax, ay, az]

    def calculate_air_density(self, temp_f, humidity, altitude_ft, pressure_pa=None,
                              extra_altitude_m=0.0):
        """Calculate air density based on temperature, humidity, altitude, and optionally
        a measured barometric pressure.

        Parameters
        ----------
        temp_f          : float  – surface temperature in °F
        humidity        : float  – relative humidity 0-100
        altitude_ft     : float  – stadium field elevation above sea level in feet
        pressure_pa     : float  – station-level barometric pressure in Pa (preferred);
                                   if None, the ISA formula is used instead
        extra_altitude_m: float  – additional height above field level in metres
                                   (used during simulation to vary rho with ball height)

        When pressure_pa is supplied it is treated as the station pressure at field level.
        The ISA lapse rate is then used to adjust that pressure upward as the ball climbs,
        giving a physically correct height-varying air density throughout the trajectory.
        When pressure_pa is None we fall back entirely to the standard atmosphere formula.
        """
        # ISA constants
        p0 = 101325   # sea level pressure Pa
        T0 = 288.15   # sea level standard temperature K
        g  = 9.81     # gravity m/s²
        L  = 0.0065   # temperature lapse rate K/m
        R  = 8.31447  # universal gas constant J/(mol·K)
        M  = 0.0289644  # molar mass of dry air kg/mol
        Rd = 287.05   # specific gas constant dry air J/(kg·K)
        Rv = 461.495  # specific gas constant water vapor J/(kg·K)

        # Convert temperature from Fahrenheit to Celsius; assume lapse with height
        temp_c = (temp_f - 32) * 5 / 9
        # Adjust temperature for ball height above field (ISA lapse rate)
        temp_c_at_height = temp_c - L * extra_altitude_m
        T = temp_c_at_height + 273.15  # Kelvin

        # Total altitude above sea level at the ball's current position
        field_altitude_m = altitude_ft * 0.3048
        total_altitude_m = field_altitude_m + extra_altitude_m

        if pressure_pa is not None and pressure_pa > 0:
            # pressure_pa is station pressure at field level.
            # Scale it upward using the ISA ratio between total altitude and field altitude
            # so that pressure decreases correctly as the ball climbs.
            p_field = float(pressure_pa)
            T_field = temp_c + 273.15
            # ISA pressure ratio: p(h) / p(field) = (T(h)/T(field))^(gM/RL)
            exponent = (g * M) / (R * L)
            T_at_height = T0 - L * total_altitude_m
            T_at_field  = T0 - L * field_altitude_m
            if T_at_field > 0 and T_at_height > 0:
                p = p_field * (T_at_height / T_at_field) ** exponent
            else:
                p = p_field  # safety fallback
        else:
            # Full ISA formula from sea level to current ball altitude
            p = p0 * (1 - L * total_altitude_m / T0) ** ((g * M) / (R * L))

        # Saturation vapor pressure (Tetens formula) at surface temp — Pa
        e_s = 6.1078 * 10 ** ((7.5 * temp_c) / (237.3 + temp_c)) * 100.0
        e = (humidity / 100.0) * e_s

        # Density of moist air: ρ = (p_dry)/(Rd·T) + e/(Rv·T)
        rho = (p - e) / (Rd * T) + e / (Rv * T)

        return rho

    def log_trajectory_physics(self, trajectory_data, filename="ball_physics_log.csv"):
        """
        Create a detailed CSV log of the ball's physics at each timestep

        Parameters:
        trajectory_data (dict): The trajectory data dictionary from calculate_trajectory
        filename (str): Output filename for the CSV log
        """
        if not LOG_BALL_PHYSICS: return;
        # Open file for writing
        with open(filename, 'w', newline='') as csvfile:
            # Define CSV header
            fieldnames = [
                'time',
                'x_pos_ft', 'y_pos_ft', 'z_pos_ft',
                'x_vel_ft_s', 'y_vel_ft_s', 'z_vel_ft_s',
                'speed_mph',
                'x_accel_ft_s2', 'y_accel_ft_s2', 'z_accel_ft_s2',
                'accel_magnitude_ft_s2',
                'height_change_rate_ft_s',
                'distance_from_start_ft'
            ]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            # Retrieve trajectory data
            times = trajectory_data['time']
            x_pos = trajectory_data['x']
            y_pos = trajectory_data['y']
            z_pos = trajectory_data['z']
            x_vel = trajectory_data['vx']
            y_vel = trajectory_data['vy']
            z_vel = trajectory_data['vz']

            # Calculate derivatives for acceleration
            x_accel = np.zeros_like(x_vel)
            y_accel = np.zeros_like(y_vel)
            z_accel = np.zeros_like(z_vel)

            # Calculate accelerations using finite differences
            for i in range(1, len(times)-1):
                dt1 = times[i] - times[i-1]
                dt2 = times[i+1] - times[i]
                dt_avg = (dt1 + dt2) / 2

                # Central difference for better accuracy
                x_accel[i] = (x_vel[i+1] - x_vel[i-1]) / (dt1 + dt2)
                y_accel[i] = (y_vel[i+1] - y_vel[i-1]) / (dt1 + dt2)
                z_accel[i] = (z_vel[i+1] - z_vel[i-1]) / (dt1 + dt2)

            # Handle endpoints (forward/backward difference)
            if len(times) > 1:
                dt = times[1] - times[0]
                x_accel[0] = (x_vel[1] - x_vel[0]) / dt
                y_accel[0] = (y_vel[1] - y_vel[0]) / dt
                z_accel[0] = (z_vel[1] - z_vel[0]) / dt

                dt = times[-1] - times[-2]
                x_accel[-1] = (x_vel[-1] - x_vel[-2]) / dt
                y_accel[-1] = (y_vel[-1] - y_vel[-2]) / dt
                z_accel[-1] = (z_vel[-1] - z_vel[-2]) / dt

            # Write data row by row
            for i in range(len(times)):
                # Calculate derived metrics
                speed_mph = np.sqrt(x_vel[i]**2 + y_vel[i]**2 + z_vel[i]**2) * 0.681818  # ft/s to mph
                accel_magnitude = np.sqrt(x_accel[i]**2 + y_accel[i]**2 + z_accel[i]**2)
                distance = np.sqrt((x_pos[i] - x_pos[0])**2 + (z_pos[i] - z_pos[0])**2)

                # Write row
                writer.writerow({
                    'time': f"{times[i]:.4f}",
                    'x_pos_ft': f"{x_pos[i]:.4f}",
                    'y_pos_ft': f"{y_pos[i]:.4f}",
                    'z_pos_ft': f"{z_pos[i]:.4f}",
                    'x_vel_ft_s': f"{x_vel[i]:.4f}",
                    'y_vel_ft_s': f"{y_vel[i]:.4f}",
                    'z_vel_ft_s': f"{z_vel[i]:.4f}",
                    'speed_mph': f"{speed_mph:.4f}",
                    'x_accel_ft_s2': f"{x_accel[i]:.4f}",
                    'y_accel_ft_s2': f"{y_accel[i]:.4f}",
                    'z_accel_ft_s2': f"{z_accel[i]:.4f}",
                    'accel_magnitude_ft_s2': f"{accel_magnitude:.4f}",
                    'height_change_rate_ft_s': f"{y_vel[i]:.4f}",
                    'distance_from_start_ft': f"{distance:.4f}"
                })

        print(f"Physics log written to {filename}")
        return filename



# Add a method to print a physics summary to the console
def print_physics_summary(trajectory_data):
    """Print a summary of physics data to help debug acceleration issues"""
    # Get the number of timesteps
    n_steps = len(trajectory_data['time'])

    if n_steps < 2:
        print("Not enough data points for physics summary")
        return

    # Print header
    print("\n--- BALL PHYSICS SUMMARY ---")

    # Sample points (start, 25%, 50%, 75%, end)
    # sample_points = [0, n_steps//4, n_steps//2, 3*n_steps//4, n_steps-1]
    sample_points = [I for I in range(n_steps)]

    print("time | x_pos_ft | y_pos_ft | z_pos_ft | x_vel_ft_s | y_vel_ft_s | z_vel_ft_s | speed_mph | x_accel_ft_s2 | y_accel_ft_s2 | z_accel_ft_s2 | accel_magnitude_ft_s2")
    print("---------+-------------------------+---------------------------+-------------+-----------------")

    # Calculate y-acceleration using finite differences
    y_accel = []
    for i in range(1, n_steps-1):
        dt1 = trajectory_data['time'][i] - trajectory_data['time'][i-1]
        dt2 = trajectory_data['time'][i+1] - trajectory_data['time'][i]
        dt_avg = (dt1 + dt2) / 2
        y_accel.append((trajectory_data['vy'][i+1] - trajectory_data['vy'][i-1]) / (dt1 + dt2))

    # Handle endpoints
    if n_steps > 1:
        dt = trajectory_data['time'][1] - trajectory_data['time'][0]
        y_accel.insert(0, (trajectory_data['vy'][1] - trajectory_data['vy'][0]) / dt)

        dt = trajectory_data['time'][-1] - trajectory_data['time'][-2]
        y_accel.append((trajectory_data['vy'][-1] - trajectory_data['vy'][-2]) / dt)

    # Print sample points
    for idx in sample_points:
        time = trajectory_data['time'][idx]
        pos_x = trajectory_data['x'][idx]
        pos_y = trajectory_data['y'][idx]
        pos_z = trajectory_data['z'][idx]
        vel_x = trajectory_data['vx'][idx]
        vel_y = trajectory_data['vy'][idx]
        vel_z = trajectory_data['vz'][idx]
        speed = (vel_x**2 + vel_y**2 + vel_z**2)**0.5 * 0.681818  # Convert ft/s to mph

        # Format string for output
        pos_str = f"({pos_x:7.2f}, {pos_y:6.2f}, {pos_z:6.2f})"
        vel_str = f"({vel_x:7.2f}, {vel_y:6.2f}, {vel_z:6.2f})"

        # Print the row
        accel = y_accel[idx] if idx < len(y_accel) else "N/A"
        print(f"{time:7.2f} | {pos_str:23} | {vel_str:25} | {speed:11.2f} | {accel if isinstance(accel, str) else accel:7.2f}")

    # Calculate and print key metrics
    max_height = max(trajectory_data['y'])
    max_height_idx = trajectory_data['y'].tolist().index(max_height)
    max_height_time = trajectory_data['time'][max_height_idx]

    # Find where y velocity changes from positive to negative (peak of trajectory)
    peak_idx = None
    for i in range(1, n_steps):
        if trajectory_data['vy'][i-1] > 0 and trajectory_data['vy'][i] <= 0:
            peak_idx = i
            break

    print("\n--- KEY METRICS ---")
    print(f"Initial Y-Velocity: {trajectory_data['vy'][0]:.2f} ft/s")
    print(f"Maximum Height: {max_height:.2f} ft at time {max_height_time:.2f} s")

    if peak_idx is not None:
        peak_time = trajectory_data['time'][peak_idx]
        print(f"Trajectory Peak: at time {peak_time:.2f} s")

        # Calculate average y-acceleration during ascent
        avg_y_accel_ascent = (trajectory_data['vy'][peak_idx] - trajectory_data['vy'][0]) / peak_time
        print(f"Average Y-Acceleration (ascent): {avg_y_accel_ascent:.2f} ft/s²")

        # For descent, use the last point
        if peak_idx < n_steps - 1:
            descent_time = trajectory_data['time'][-1] - peak_time
            avg_y_accel_descent = (trajectory_data['vy'][-1] - trajectory_data['vy'][peak_idx]) / descent_time
            print(f"Average Y-Acceleration (descent): {avg_y_accel_descent:.2f} ft/s²")

    # Check for expected gravitational acceleration (should be around -32 ft/s²)
    grav_accel_approx = sum(y_accel) / len(y_accel) if y_accel else 0
    print(f"Average Y-Acceleration (overall): {grav_accel_approx:.2f} ft/s²")
    print(f"Expected gravitational acceleration: -32.2 ft/s²")

    # Verdict
    if abs(grav_accel_approx + 32.2) > 5:  # More than 5 ft/s² different from expected
        print("\nVERDICT: Gravity acceleration appears INCORRECT")
    else:
        print("\nVERDICT: Gravity acceleration appears correct")

    print("------------------------\n")






class StadiumView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)

        # Set up the graphics scene
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)

        # Enable antialiasing for smoother graphics
        self.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Layers for organization
        self.stadium_layer = QGraphicsItemGroup()
        self.weather_layer = QGraphicsItemGroup()
        self.spray_layer = QGraphicsItemGroup()    # Spray chart dots
        self.trail_layer = QGraphicsItemGroup()    # Persistent shot trails
        self.ball_layer = QGraphicsItemGroup()

        self.scene.addItem(self.stadium_layer)
        self.scene.addItem(self.weather_layer)
        self.scene.addItem(self.spray_layer)
        self.scene.addItem(self.trail_layer)
        self.scene.addItem(self.ball_layer)

        # z-ordering: spray between stadium and trails
        self.spray_layer.setZValue(25)

        # Ball and trajectory items
        self.ball_item = None
        self.shadow_item = None
        self.trajectory_path = None

        # Set background color
        self.setBackgroundBrush(QBrush(QColor(0, 0, 0)))

        # Enable mouse tracking for interactive elements
        self.setMouseTracking(True)

        # Set the scene rect to a much larger size initially
        self.scene.setSceneRect(-500, -500, 1000, 1000)

        # Shared drawing constants - all methods must use these
        self.field_scale = 5.5
        self.home_plate_x = 0
        self.home_plate_y = 250

        # Fit the view to the scene
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        """Handle resize events to maintain proper view scaling"""
        super().resizeEvent(event)
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    # ---- Spray chart --------------------------------------------------- #
    # Savant hc_x/hc_y pixel grid constants
    SAVANT_HP_X, SAVANT_HP_Y = 125.42, 198.27
    SAVANT_SCALE = 2.51  # feet per Savant pixel

    _SPRAY_COLOUR_MAP = {
        "home_run":   QColor(255, 200, 50),     # gold
        "single":     QColor(100, 210, 100),     # green
        "double":     QColor(100, 180, 255),     # blue
        "triple":     QColor(100, 220, 255),     # cyan
        "foul":       QColor(140, 140, 140),     # grey
    }
    _SPRAY_OUT_COLOUR = QColor(200, 70, 70)      # red for outs

    def _make_spray_dot(self, hc_x: float, hc_y: float, event_type: str):
        """Create a spray dot QGraphicsEllipseItem at the correct scene position."""
        scale = self.field_scale
        dx_ft = (hc_x - self.SAVANT_HP_X) * self.SAVANT_SCALE
        dy_ft = (self.SAVANT_HP_Y - hc_y) * self.SAVANT_SCALE
        sx = self.home_plate_x + dx_ft * scale
        sy = self.home_plate_y - dy_ft * scale

        colour = self._SPRAY_COLOUR_MAP.get(event_type, self._SPRAY_OUT_COLOUR)
        dot_r = 6  # scene-unit radius — visible at typical zoom
        dot = QGraphicsEllipseItem(sx - dot_r, sy - dot_r, dot_r * 2, dot_r * 2)
        outline = QPen(colour.darker(140))
        outline.setWidthF(1.0)
        dot.setPen(outline)
        dot.setBrush(QBrush(colour))
        dot.setOpacity(0.75)
        return dot

    def _parse_hc(self, ev: dict):
        """Return (hc_x, hc_y) as floats or None if invalid."""
        hc_x = ev.get("hc_x")
        hc_y = ev.get("hc_y")
        if hc_x is None or hc_y is None:
            return None
        try:
            hc_x, hc_y = float(hc_x), float(hc_y)
        except (ValueError, TypeError):
            return None
        if math.isnan(hc_x) or math.isnan(hc_y):
            return None
        return hc_x, hc_y

    def set_spray_chart(self, events: list):
        """Draw spray chart dots from BBE events with hc_x/hc_y."""
        self.clear_spray_chart()
        for ev in events:
            parsed = self._parse_hc(ev)
            if parsed is None:
                continue
            dot = self._make_spray_dot(parsed[0], parsed[1], str(ev.get("events", "")))
            self.spray_layer.addToGroup(dot)

    def clear_spray_chart(self):
        """Remove all spray chart dots."""
        self.scene.removeItem(self.spray_layer)
        self.spray_layer = QGraphicsItemGroup()
        self.spray_layer.setZValue(25)
        self.scene.addItem(self.spray_layer)

    def add_spray_dot(self, ev: dict):
        """Add a single spray chart dot (used for animated playback)."""
        parsed = self._parse_hc(ev)
        if parsed is None:
            return
        dot = self._make_spray_dot(parsed[0], parsed[1], str(ev.get("events", "")))
        self.spray_layer.addToGroup(dot)

    def draw_stadium_polar(self, stadium_name, dimensions):
        """Draw stadium outline using polar coordinate data from weatherman.py"""
        # Delete all stadium items and create a new layer
        self.scene.removeItem(self.stadium_layer)
        self.stadium_layer = QGraphicsItemGroup()
        self.scene.addItem(self.stadium_layer)

        print(f"Drawing stadium using polar coordinates: {stadium_name}")

        # Clear persistent trails and spray chart when stadium changes
        self.scene.removeItem(self.trail_layer)
        self.trail_layer = QGraphicsItemGroup()
        self.scene.addItem(self.trail_layer)
        self.clear_spray_chart()

        # Scale factor - increased for better space usage and visibility
        scale_factor = self.field_scale

        # Home plate position - centered horizontally, positioned to show full field
        home_plate_x = self.home_plate_x
        home_plate_y = self.home_plate_y

        # Check if stadium has Cartesian wall data (more accurate for complex shapes)
        stadium_data = STADIUM_DATA.get(stadium_name, {})

        if "cartesian_wall" in stadium_data:
            # Use Cartesian wall points directly
            # Convert (x, z) in feet to scene coordinates using same transform as ball path
            wall_points = []
            for (wx, wz) in stadium_data["cartesian_wall"]:
                horiz_dist = math.sqrt(wx**2 + wz**2)
                adjusted_angle = math.atan2(wx, wz)
                field_x = horiz_dist * math.cos(adjusted_angle) * scale_factor
                field_y = -horiz_dist * math.sin(adjusted_angle) * scale_factor
                wall_points.append(QPointF(home_plate_x + field_x, home_plate_y + field_y))

            if wall_points:
                field_boundary = QPainterPath()
                home_plate_point = QPointF(home_plate_x, home_plate_y)
                field_boundary.moveTo(home_plate_point)
                field_boundary.lineTo(wall_points[0])
                for point in wall_points[1:]:
                    field_boundary.lineTo(point)
                field_boundary.lineTo(home_plate_point)

                boundary_item = QGraphicsPathItem(field_boundary)
                boundary_item.setPen(QPen(QColor(139, 69, 19), 4))
                boundary_item.setBrush(QBrush(QColor(34, 90, 34, 180)))
                self.stadium_layer.addToGroup(boundary_item)

        else:
            # Fall back to polar coordinate system
            wall_points = []
            angle_step = 1.0

            for angle in np.arange(0, 91, angle_step):
                distance = get_stadium_wall_distance(stadium_name, angle)

                if distance is None or distance <= 0 or distance == float('inf'):
                    wall_points.append(None)
                    continue

                angle_rad = math.radians(angle)
                adjusted_angle = angle_rad + math.pi / 4
                field_x = distance * math.cos(adjusted_angle) * scale_factor
                field_y = -distance * math.sin(adjusted_angle) * scale_factor
                wall_points.append(QPointF(home_plate_x + field_x, home_plate_y + field_y))

            JUMP_THRESHOLD = 130
            for i in range(1, len(wall_points)):
                if wall_points[i] is None or wall_points[i-1] is None:
                    continue
                dx = wall_points[i].x() - wall_points[i-1].x()
                dy = wall_points[i].y() - wall_points[i-1].y()
                if math.sqrt(dx**2 + dy**2) > JUMP_THRESHOLD:
                    wall_points[i] = None

            if any(p is not None for p in wall_points):
                home_plate_point = QPointF(home_plate_x, home_plate_y)
                valid_seq = [p for p in wall_points if p is not None]

                # Filled outfield polygon (home → walls → home) drawn first
                fill_path = QPainterPath()
                fill_path.moveTo(home_plate_point)
                for p in valid_seq:
                    fill_path.lineTo(p)
                fill_path.lineTo(home_plate_point)

                fill_item = QGraphicsPathItem(fill_path)
                fill_item.setPen(QPen(Qt.PenStyle.NoPen))
                fill_item.setBrush(QBrush(QColor(34, 90, 34, 180)))
                self.stadium_layer.addToGroup(fill_item)

                # Outline path preserves gap handling so broken segments don't draw bogus lines
                field_boundary = QPainterPath()

                in_segment = False
                first_valid = next((p for p in wall_points if p is not None), None)

                field_boundary.moveTo(home_plate_point)
                if first_valid:
                    field_boundary.lineTo(first_valid)

                for point in wall_points:
                    if point is None:
                        in_segment = False
                    else:
                        if not in_segment:
                            field_boundary.moveTo(point)
                            in_segment = True
                        else:
                            field_boundary.lineTo(point)

                last_valid = next((p for p in reversed(wall_points) if p is not None), None)
                if last_valid:
                    field_boundary.moveTo(last_valid)
                    field_boundary.lineTo(home_plate_point)

                boundary_item = QGraphicsPathItem(field_boundary)
                boundary_item.setPen(QPen(QColor(139, 69, 19), 4))
                boundary_item.setBrush(QBrush())
                self.stadium_layer.addToGroup(boundary_item)

        # Draw the infield (basic diamond shape)
        self.draw_infield(home_plate_x, home_plate_y, scale_factor)

        # Compute tight scene rect from actual wall point bounds
        valid_points = [p for p in wall_points if p is not None]
        margin = 40
        home_plate_padding = 125  # Clearance for infield diamond below home plate
        if valid_points:
            min_x = min(p.x() for p in valid_points)
            max_x = max(p.x() for p in valid_points)
            min_y = min(p.y() for p in valid_points)
            # Enforce symmetric left/right bounds around home plate center
            half_width = max(abs(min_x - home_plate_x), abs(max_x - home_plate_x)) + margin
            min_x = home_plate_x - half_width
            max_x = home_plate_x + half_width
            min_y = min(min_y, home_plate_y) - margin
            max_y = home_plate_y + home_plate_padding
        else:
            max_distance = max(
                dimensions["left_field"], dimensions["center_field"], dimensions["right_field"]
            ) * scale_factor
            min_x = -max_distance - margin
            max_x =  max_distance + margin
            min_y = home_plate_y - max_distance - margin
            max_y = home_plate_y + home_plate_padding

        self.scene.setSceneRect(min_x, min_y, max_x - min_x, max_y - min_y)

        # Force view update
        self.resetCachedContent()
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.update()

        print(f"Successfully drew stadium {stadium_name} using polar coordinates")

    def draw_infield(self, home_x, home_y, scale_factor):
        """Draw basic infield diamond"""
        # Base distances (90 feet between bases)
        base_distance = 90 * scale_factor

        # Base positions (clockwise from home)
        first_base = QPointF(home_x + base_distance * 0.707, home_y - base_distance * 0.707)
        second_base = QPointF(home_x, home_y - base_distance * 1.414)
        third_base = QPointF(home_x - base_distance * 0.707, home_y - base_distance * 0.707)
        home_plate = QPointF(home_x, home_y)

        # Draw base paths
        infield_path = QPainterPath()
        infield_path.moveTo(home_plate)
        infield_path.lineTo(first_base)
        infield_path.lineTo(second_base)
        infield_path.lineTo(third_base)
        infield_path.lineTo(home_plate)

        infield_item = QGraphicsPathItem(infield_path)
        infield_item.setPen(QPen(QColor(255, 82, 45), 10))  # Brown infield lines
        infield_item.setBrush(QBrush(QColor(139, 69, 19, 50)))  # Light brown fill

        self.stadium_layer.addToGroup(infield_item)

        # Draw pitcher's mound
        mound_distance = 60.5 * scale_factor  # Distance from home to pitcher's mound
        mound_x = home_x
        mound_y = home_y - mound_distance
        mound_radius = 9 * scale_factor  # Pitcher's mound radius

        mound_item = QGraphicsEllipseItem(
            mound_x - mound_radius,
            mound_y - mound_radius,
            mound_radius * 2,
            mound_radius * 2
        )
        mound_item.setPen(QPen(QColor(255, 82, 45), 10))
        mound_item.setBrush(QBrush(QColor(255, 69, 19, 100)))

        self.stadium_layer.addToGroup(mound_item)



    def draw_starting_position(self, start_x, start_y, start_z):
        """Draw a visual indicator for the ball starting position"""
        # Clear any previous starting position indicator
        for item in self.ball_layer.childItems():
            if hasattr(item, 'is_start_indicator') and item.is_start_indicator:
                self.scene.removeItem(item)

        # Scale factor - must match the one used in draw_stadium_polar
        scale_factor = self.field_scale

        # Set a fixed home plate position - same as in start_ball_trajectory
        fixed_home_x = self.home_plate_x
        fixed_home_y = self.home_plate_y

        # Create the start position indicator (a larger circle with crosshairs)
        indicator_size = 15
        self.start_indicator = QGraphicsEllipseItem(-indicator_size/2, -indicator_size/2, indicator_size, indicator_size)
        self.start_indicator.setBrush(QBrush(QColor(255, 140, 0, 180)))  # Semi-transparent orange
        self.start_indicator.setPen(QPen(QColor(255, 140, 0), 2))

        # Add crosshairs
        line_h = QGraphicsLineItem(-indicator_size/2, 0, indicator_size/2, 0)
        line_v = QGraphicsLineItem(0, -indicator_size/2, 0, indicator_size/2)
        line_h.setPen(QPen(QColor(255, 140, 0), 2))
        line_v.setPen(QPen(QColor(255, 140, 0), 2))

        # Create a group to hold all indicators
        self.start_position_group = QGraphicsItemGroup()
        self.start_position_group.addToGroup(self.start_indicator)
        self.start_position_group.addToGroup(line_h)
        self.start_position_group.addToGroup(line_v)

        # Mark this as a start indicator for easier identification
        self.start_position_group.is_start_indicator = True

        # Convert using the same polar transform as draw_stadium_polar
        # start_x = feet toward center field, start_z = feet toward right(+)/left(-) field
        horiz_dist = math.sqrt(start_x**2 + start_z**2)
        if horiz_dist == 0:
            scene_x = fixed_home_x
            scene_y = fixed_home_y
        else:
            adjusted_angle = math.atan2(start_x, start_z)
            scene_x = fixed_home_x + horiz_dist * math.cos(adjusted_angle) * scale_factor
            scene_y = fixed_home_y - horiz_dist * math.sin(adjusted_angle) * scale_factor

        # Position the indicator
        self.start_position_group.setPos(scene_x, scene_y)

        # Add to the ball layer with high z-value to be on top
        self.ball_layer.addToGroup(self.start_position_group)
        self.start_position_group.setZValue(150)  # Above ball trajectory

        # Add a text label with the coordinates
        text = f"Start: ({start_x:.1f}, {start_y:.1f}, {start_z:.1f})"
        label = self.scene.addSimpleText(text)
        label.setBrush(QBrush(QColor(255, 140, 0)))
        label.setPos(scene_x + 15, scene_y - 15)
        label.is_start_indicator = True
        self.ball_layer.addToGroup(label)

        return self.start_position_group


    def draw_wind_indicators(self, speed, direction):
        """Draw wind vector indicators on the field with animation"""
        # Clear previous wind indicators
        while self.weather_layer.childItems():
            item = self.weather_layer.childItems()[0]
            self.scene.removeItem(item)

        # Store current wind speed for animation
        self.current_wind_speed = speed

        # Convert meteorological to mathematical angle
        math_angle = (270 - direction) % 360
        rad_angle = math.radians(math_angle)

        # Create three large prominent arrows at the top of the screen
        arrow_positions = [
            (-200, -900),  # Left top
            (0, -900),     # Center top
            (200, -900)    # Right top
        ]

        # Scale based on wind speed
        scale_factor = 18  # Large scale for visibility
        length = scale_factor * max(2, speed)  # Minimum size for visibility

        for center_x, center_y in arrow_positions:
            # Calculate endpoint
            end_x = center_x + length * math.cos(rad_angle)
            end_y = center_y + length * math.sin(rad_angle)

            # Create the arrow shaft with thicker line
            shaft = QGraphicsLineItem(center_x, center_y, end_x, end_y)

            # Set color based on wind speed - vibrant colors
            if speed < 5:
                color = QColor(80, 200, 255)  # Bright blue for light wind
            elif speed < 10:
                color = QColor(50, 255, 120)  # Bright green for moderate wind
            else:
                color = QColor(255, 60, 60)  # Bright red for strong wind

            # Use thicker line
            shaft.setPen(QPen(color, 8))  # Increased from 6 to 8
            self.weather_layer.addToGroup(shaft)

            # Add larger arrowhead
            self.add_arrowhead(end_x, end_y, rad_angle, 30, color, 8)  # Passing line thickness

        # Add wind speed text label (only once, in the center)
        wind_text = self.scene.addSimpleText(f"{speed} mph")
        wind_text.setBrush(QBrush(color))

        # Make text larger
        font = wind_text.font()
        font.setPointSize(16)
        wind_text.setFont(font)

        # Position text near the center arrow
        text_width = wind_text.boundingRect().width()
        text_height = wind_text.boundingRect().height()
        center_arrow_end_x = arrow_positions[1][0] + length * math.cos(rad_angle)
        center_arrow_end_y = arrow_positions[1][1] + length * math.sin(rad_angle)
        wind_text.setPos(center_arrow_end_x + 10, center_arrow_end_y - text_height/2)

        self.weather_layer.addToGroup(wind_text)

        # Start animation for the wind indicators
        self.start_wind_animation()

    def add_arrowhead(self, x, y, angle, size, color, thickness=4):
        """Add an arrowhead to a wind vector"""
        angle1 = angle + math.radians(150)
        angle2 = angle + math.radians(210)

        arrow1_x = x + size * math.cos(angle1)
        arrow1_y = y + size * math.sin(angle1)
        arrow2_x = x + size * math.cos(angle2)
        arrow2_y = y + size * math.sin(angle2)

        line1 = QGraphicsLineItem(x, y, arrow1_x, arrow1_y)
        line2 = QGraphicsLineItem(x, y, arrow2_x, arrow2_y)

        # Use the specified thickness
        line1.setPen(QPen(color, thickness))
        line2.setPen(QPen(color, thickness))

        self.weather_layer.addToGroup(line1)
        self.weather_layer.addToGroup(line2)

    def start_wind_animation(self):
        """Start a simple pulsing animation for the wind vectors"""
        # Create a timer for the animation
        if not hasattr(self, 'wind_animation_timer'):
            self.wind_animation_timer = QTimer(self)
            self.wind_animation_timer.timeout.connect(self.pulse_wind_vectors)
            self.wind_animation_state = 0

        # Start the timer if not already running
        if not self.wind_animation_timer.isActive():
            self.wind_animation_timer.start(500)  # 500ms interval for pulse

    def pulse_wind_vectors(self):
        """Create a pulsing effect for wind vectors"""
        self.wind_animation_state = (self.wind_animation_state + 1) % 3

        # Set opacity based on animation state for pulsing effect
        opacity = 0.6 + (self.wind_animation_state * 0.2)  # Oscillate between 0.6 and 1.0

        # Apply to all wind vector items
        for item in self.weather_layer.childItems():
            item.setOpacity(opacity)

    def start_ball_trajectory(self, trajectory_data, hit_result=None):
        """Initialize the ball trajectory visualization in the 2D view with custom starting position"""
        # Clear previous live ball items only (not persistent trails)
        for item in self.ball_layer.childItems():
            if not (hasattr(item, 'is_start_indicator') and item.is_start_indicator):
                self.scene.removeItem(item)

        # Scale factor - must match the one used in draw_stadium_polar
        scale_factor = self.field_scale

        # Set a fixed home plate position - this is the key point of alignment
        fixed_home_x = self.home_plate_x
        fixed_home_y = self.home_plate_y

        # Create the ball
        ball_size = 10
        self.ball_item = QGraphicsEllipseItem(-ball_size/2, -ball_size/2, ball_size, ball_size)
        self.ball_item.setBrush(QBrush(QColor(255, 255, 255)))
        self.ball_item.setPen(QPen(Qt.GlobalColor.black, 1))

        # Create the shadow
        shadow_size = ball_size * 0.8
        self.shadow_item = QGraphicsEllipseItem(-shadow_size/2, -shadow_size/2, shadow_size, shadow_size)
        self.shadow_item.setBrush(QBrush(QColor(0, 0, 0, 150)))
        self.shadow_item.setPen(QPen(Qt.PenStyle.NoPen))

        # Subtract the 3D starting offset so 2D coords are relative to home plate
        start_x = trajectory_data.get('start_x', 0)
        start_z = trajectory_data.get('start_z', 0)

        # 2D always starts at home plate
        scene_start_x = fixed_home_x
        scene_start_y = fixed_home_y

        # Build scene path points (subtract start offset to keep relative to home plate)
        path_points = [(scene_start_x, scene_start_y)]
        for i in range(0, len(trajectory_data["x"]), 5):
            ball_x = trajectory_data["x"][i] - start_x
            ball_z = trajectory_data["z"][i] - start_z
            horiz_dist = math.sqrt(ball_x**2 + ball_z**2)
            # atan2(X,Z) already gives angles π/4 higher than stadium polar convention
            # (RF foul line = atan2 π/4 vs polar 0°), so no extra offset needed
            adjusted_angle = math.atan2(ball_x, ball_z)
            scene_x = fixed_home_x + horiz_dist * math.cos(adjusted_angle) * scale_factor
            scene_y = fixed_home_y - horiz_dist * math.sin(adjusted_angle) * scale_factor
            path_points.append((scene_x, scene_y))

        # --- Persistent trail in trail_layer ---
        # Color by hit result
        result_colors = {
            "HOME RUN":    QColor(255, 215, 0),    # Gold
            "OFF THE WALL":QColor(255, 140, 0),    # Orange
            "WARNING TRACK":QColor(255, 200, 60),  # Yellow
            "IN PLAY":     QColor(160, 220, 100),  # Green
            "FOUL BALL":   QColor(180, 180, 180),  # Grey
        }
        trail_color = result_colors.get(hit_result, QColor(255, 140, 0))

        trail_path = QPainterPath()
        trail_path.moveTo(path_points[0][0], path_points[0][1])
        for px, py in path_points[1:]:
            trail_path.lineTo(px, py)

        trail_item = QGraphicsPathItem(trail_path)
        trail_pen = QPen(trail_color, 2, Qt.PenStyle.SolidLine)
        trail_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        trail_item.setPen(trail_pen)
        trail_item.setOpacity(0.75)

        # Tag with hit result for potential future filtering
        trail_item.hit_result = hit_result
        self.trail_layer.addToGroup(trail_item)

        # Landing dot at end of trail
        land_x, land_y = path_points[-1]
        dot_size = 7
        land_dot = QGraphicsEllipseItem(-dot_size/2, -dot_size/2, dot_size, dot_size)
        land_dot.setBrush(QBrush(trail_color))
        land_dot.setPen(QPen(Qt.PenStyle.NoPen))
        land_dot.setPos(land_x, land_y)
        self.trail_layer.addToGroup(land_dot)

        # --- Live dashed path for animation (ball_layer, gets cleared next sim) ---
        live_path = QPainterPath()
        live_path.moveTo(path_points[0][0], path_points[0][1])
        for px, py in path_points[1:]:
            live_path.lineTo(px, py)

        self.trajectory_path = QGraphicsPathItem(live_path)
        self.trajectory_path.setPen(QPen(QColor(255, 255, 255, 80), 1, Qt.PenStyle.DashLine))

        # Add live items to ball_layer
        self.ball_layer.addToGroup(self.shadow_item)
        self.ball_layer.addToGroup(self.trajectory_path)
        self.ball_layer.addToGroup(self.ball_item)

        self.ball_item.setPos(scene_start_x, scene_start_y)
        self.shadow_item.setPos(scene_start_x, scene_start_y)

        self.start_x = scene_start_x
        self.start_y = scene_start_y

        self.ball_layer.setVisible(True)
        self.ball_layer.setZValue(100)
        self.trail_layer.setZValue(50)

        return True

    def clear_trails(self):
        """Clear all persistent shot trails"""
        self.scene.removeItem(self.trail_layer)
        self.trail_layer = QGraphicsItemGroup()
        self.scene.addItem(self.trail_layer)

    def update_ball_position(self, trajectory_data, frame):
        """Update the ball position for animation with velocity-based visual effects"""
        if frame >= len(trajectory_data["x"]):
            return False

        # Scale factor - must match the one used in draw_stadium_polar
        scale_factor = self.field_scale

        # Convert ball position using same polar transform as stadium drawing
        # Subtract start offset so 2D coords are relative to home plate
        start_x = trajectory_data.get('start_x', 0)
        start_z = trajectory_data.get('start_z', 0)
        ball_x = trajectory_data["x"][frame] - start_x
        ball_z = trajectory_data["z"][frame] - start_z
        horiz_dist = math.sqrt(ball_x**2 + ball_z**2)
        adjusted_angle = math.atan2(ball_x, ball_z)
        x = self.home_plate_x + horiz_dist * math.cos(adjusted_angle) * scale_factor
        y = self.home_plate_y - horiz_dist * math.sin(adjusted_angle) * scale_factor
        height = trajectory_data["y"][frame]

        # Get velocity data for visual effects
        vx = trajectory_data["vx"][frame]
        vy = trajectory_data["vy"][frame]
        vz = trajectory_data["vz"][frame]

        # Calculate speed magnitude for scaling effects
        speed_magnitude = np.sqrt(vx**2 + vy**2 + vz**2)

        # Set positions
        self.ball_item.setPos(x, y)
        self.shadow_item.setPos(x, y)

        # Scale ball based on height and speed
        height_factor = max(0.8, min(1.5, 1 + height/100))
        speed_factor = max(0.9, min(1.2, 1 + speed_magnitude/300))
        self.ball_item.setScale(height_factor * speed_factor)

        # Add speed-based color effect to the ball
        speed_threshold = 25
        if speed_magnitude > speed_threshold:
            speed_color = QColor(255, 255 - min(100, int(speed_magnitude - speed_threshold)), 255 - min(150, int(speed_magnitude - speed_threshold)))
            self.ball_item.setBrush(QBrush(speed_color))
        else:
            self.ball_item.setBrush(QBrush(QColor(255, 255, 255)))

        # Make shadow more transparent based on height
        opacity = max(0.2, 1.0 - height/200)
        self.shadow_item.setOpacity(opacity)

        # Scale shadow size inversely proportional to height
        shadow_scale = max(0.5, 1.0 - height/300)
        self.shadow_item.setScale(shadow_scale)

        return True



    def stop_wind_animation(self):
        """Stop the wind vector animation"""
        if hasattr(self, 'wind_animation_timer') and self.wind_animation_timer.isActive():
            self.wind_animation_timer.stop()


    def get_wind_color(self, speed, animation_state):
        """Get color for wind vector based on speed and animation state"""
        base_colors = {
            'light': QColor(100, 200, 255),    # Light wind - blue
            'moderate': QColor(50, 255, 150),  # Moderate wind - green
            'strong': QColor(255, 50, 50)      # Strong wind - red
        }

        # Select base color based on speed
        if speed < 5:
            base = base_colors['light']
        elif speed < 10:
            base = base_colors['moderate']
        else:
            base = base_colors['strong']

        # Make color slightly brighter during pulse peak
        if animation_state == 1:
            # Brighten the color by 20%
            return QColor(
                min(255, int(base.red() * 1.2)),
                min(255, int(base.green() * 1.2)),
                min(255, int(base.blue() * 1.2))
            )

        return base


# ==============================================
# 3D Umpire View
# ==============================================
class UmpireView3D(QOpenGLWidget):
    # ------------------------------------------------------------------ #
    # Static geometry (OBJ model + procedural outfield) is compiled into
    # GL display lists so paintGL just replays them with zero Python
    # overhead per frame.
    #
    # OBJ model:  loaded on a background thread → normals and materials
    #             pre-computed there → display list compiled lazily in
    #             paintGL on first frame after data arrives.
    #
    # Outfield:   ground plane, wall, warning track, foul lines/poles
    #             compiled into a separate display list on first paint
    #             after a stadium change.  Invalidated when the stadium
    #             name changes or the GL context is re-created.
    # ------------------------------------------------------------------ #
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ball_pos = None
        self.ball_vel = None  # Add velocity storage
        self.prev_ball_pos = None  # Track previous position for trail effect
        self.ball_trail = []  # Store recent ball positions for trail effect
        self.ballpark_model = None
        self.textures = {}
        self.spray_dots = []   # list of (x_m, z_m, r, g, b) for spray chart
        self.spray_trails = [] # list of (points_list, r, g, b) for 3D trajectory trails
        self._stadium_name: str = ""   # set by SplitView when stadium changes
        self._outfield_display_list = None  # compiled in paintGL, invalidated on stadium change
        self.pitch_trail_color = None  # when set (r,g,b), trail dots use this color
        self.trail_min_dist = 0.1      # minimum distance between trail points (meters)

        # Physics-to-model coordinate transform constants
        # Model home plate at (-7.38, 1.14, 0.90), CF direction at 45.2° in XZ plane
        # Model scale: ~2.32 model units per meter (HP-to-rubber = 42.8 units / 18.44m)
        self._model_hp = (-7.38, 1.14, 0.90)
        self._model_scale = 2.320
        _angle_rad = math.radians(45.2)
        self._model_cos = math.cos(_angle_rad)
        self._model_sin = math.sin(_angle_rad)

        # Set format for better rendering
        fmt = QSurfaceFormat()
        fmt.setSamples(4)  # 4x MSAA
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        self.setFormat(fmt)

        # Model loaded in background thread — display list compiled when it arrives
        self.ballpark_model = None
        self._precomputed_meshes = None
        self._effort_meshes = []
        self._effort_t0 = time.time()
        self._effort_timer = QTimer(self)
        self._effort_timer.timeout.connect(self.update)
        self._effort_timer.start(33)  # ~30fps repaint to drive logo pulse
        self._model_load_pending = True
        import threading
        threading.Thread(target=self._load_model_bg, daemon=True).start()

        # Camera setup
        self.camera = {
            'pos': [-10.6, 2.6, -5 ],
            'target': [13.9,-0.6,14.5],
            'up': [0, 1, 0],
            'fov': 50
        }

        self.control_mode = 'camera'

        self.light_params = [
            # Main field light 1 (positioned high like a stadium light on first base side)
            {
                'position': [15.0, 25.0, 20.0, 1.0],  # Higher position for stadium lights
                'diffuse': [1.0, 0.98, 0.9, 1.0],     # Slightly warm white (stadium lights)
                'ambient': [0.2, 0.2, 0.22, 1.0],     # Low ambient from this source
                'specular': [0.8, 0.8, 0.7, 1.0],     # Slightly reduced blue in specular
                'enabled': True
            },
            # Main field light 2 (positioned high like a stadium light on third base side)
            {
                'position': [15.0, 25.0, -20.0, 1.0],  # Opposite side of the field
                'diffuse': [1.0, 0.98, 0.9, 1.0],      # Same warm white
                'ambient': [0.2, 0.2, 0.22, 1.0],      # Low ambient
                'specular': [0.8, 0.8, 0.7, 1.0],      # Consistent specular
                'enabled': True
            },
            # Main field light 3 (positioned high behind home plate)
            {
                'position': [-10.0, 25.0, 0.0, 1.0],   # Behind home plate
                'diffuse': [1.0, 0.98, 0.9, 1.0],      # Same warm white
                'ambient': [0.2, 0.2, 0.22, 1.0],      # Low ambient
                'specular': [0.8, 0.8, 0.7, 1.0],      # Consistent specular
                'enabled': True
            },
            # Main field light 4 (positioned high in center field)
            {
                'position': [30.0, 25.0, 0.0, 1.0],    # Center field
                'diffuse': [1.0, 0.98, 0.9, 1.0],      # Same warm white
                'ambient': [0.2, 0.2, 0.22, 1.0],      # Low ambient
                'specular': [0.8, 0.8, 0.7, 1.0],      # Consistent specular
                'enabled': True
            },
            # Fill light (softer light from above to prevent harsh shadows)
            {
                'position': [0.0, 30.0, 0.0, 1.0],     # Directly above field
                'diffuse': [0.5, 0.5, 0.6, 1.0],       # Slightly bluish fill light
                'ambient': [0.1, 0.1, 0.15, 1.0],      # Very low ambient
                'specular': [0.3, 0.3, 0.4, 1.0],      # Reduced specular
                'enabled': True
            },
            # Ambient environment light (simulates bounce light)
            {
                'position': [0.0, 0.0, 0.0, 0.0],      # Set W=0 for directional light
                'diffuse': [0.3, 0.3, 0.35, 1.0],      # Soft blue-tinted indirect light
                'ambient': [0.15, 0.15, 0.2, 1.0],     # Global ambient
                'specular': [0.0, 0.0, 0.0, 1.0],      # No specular for ambient light
                'enabled': True
            }
        ]

        # Store a second camera configuration that will follow the ball
        self.tracking_camera = {
            'enabled': True,
            'pos': [-10.6, 2.6, -5],  # Same as default camera
            'target': [13.9, -0.6, 14.5],  # Same as default camera
            'up': [0, 1, 0],
            'fov': 50
        }

        # Store current camera to switch back and forth
        self.main_camera = self.camera.copy()

        # Flag to track when to update camera
        self.is_tracking_ball = False


    # Material definitions keyed by mesh name substring match.
    # Looked up once on the background thread during _precompute_mesh_data().
    _MESH_MATERIALS = {
        "Infield":        ([0.2,0.15,0.1,1.0], [0.76,0.46,0.25,1.0], [0.4,0.3,0.2,1.0], 64.0, None),
        "outfield":       ([0.05,0.2,0.05,1.0],[0.1,0.6,0.1,1.0],   [0.1,0.4,0.1,1.0], 12.0, None),
        "EffortText":     ([1.0,1.0,1.0,0.1], [1.0,1.0,1.0,0.1],   [1.0,1.0,1.0,0.1], 128.0,[0.15,0.05,0.05,0.05]),
        "homeplate":      ([0.3,0.3,0.3,1.0], [0.95,0.95,0.95,1.0],[0.8,0.8,0.8,1.0],  96.0, None),
        "pitchersmound":  ([0.22,0.17,0.12,1.0],[0.7,0.4,0.2,1.0], [0.4,0.3,0.2,1.0],  32.0, None),
        "Dugout":         ([0.2,0.2,0.2,1.0], [0.6,0.6,0.6,1.0],   [0.3,0.3,0.3,1.0],  48.0, None),
        "dugout":         ([0.2,0.2,0.2,1.0], [0.6,0.6,0.6,1.0],   [0.3,0.3,0.3,1.0],  48.0, None),
        "Graffiti":       ([0.2,0.2,0.2,1.0], [0.8,0.8,0.8,1.0],   [0.3,0.3,0.3,1.0],  8.0,  None),
        "Cylinder":       ([0.2,0.2,0.25,1.0],[0.5,0.5,0.6,1.0],   [0.3,0.3,0.4,1.0],  32.0, None),
        "Box":            ([0.2,0.2,0.25,1.0],[0.5,0.5,0.6,1.0],   [0.3,0.3,0.4,1.0],  32.0, None),
    }
    _DEFAULT_MATERIAL = ([0.2,0.2,0.2,1.0], [0.8,0.8,0.8,1.0], [0.5,0.5,0.5,1.0], 32.0, None)

    def _load_model_bg(self):
        """Load Wavefront OBJ on a background thread (no GL calls here)."""
        try:
            model = Wavefront(
                'baseballfield.obj',
                create_materials=True,
                collect_faces=True,
                strict=False,
                parse=True,
            )
            print(f"[model] loaded {len(model.materials)} materials (bg thread)")

            # Pre-compute normals and flatten vertex data on this thread
            # so the GL compile on the main thread is a fast memcpy-style loop.
            precomputed = self._precompute_mesh_data(model)

            # Hand results to the main thread.  GIL makes reference assignment atomic.
            self._precomputed_meshes = precomputed
            self.ballpark_model = model
            self._model_load_pending = False
            self.update()   # schedule a repaint
        except Exception as e:
            print(f"[model] error loading OBJ: {e}")
            self._model_load_pending = False

    def _precompute_mesh_data(self, model):
        """Pre-compute per-face normals and material assignments (pure Python, no GL).

        Returns a list of (material_tuple, gl_data) where gl_data is a flat list
        of (nx,ny,nz, v0,v1,v2, v3,v4,v5, v6,v7,v8) per triangle — ready to be
        blasted into glNormal3f/glVertex3f with minimal per-face Python overhead.
        """
        import array
        vertices = model.vertices
        result = []
        effort = []

        for mesh in model.mesh_list:
            mesh_name = getattr(mesh, 'name', '') or ''

            # Find matching material
            mat = self._DEFAULT_MATERIAL
            for key, mat_def in self._MESH_MATERIALS.items():
                if key in mesh_name:
                    mat = mat_def
                    break

            # Pre-compute flattened vertex+normal data for all faces
            gl_data = array.array('f')
            for face in mesh.faces:
                if len(face) < 3:
                    continue
                v0, v1, v2 = vertices[face[0]], vertices[face[1]], vertices[face[2]]
                e1x = v1[0]-v0[0]; e1y = v1[1]-v0[1]; e1z = v1[2]-v0[2]
                e2x = v2[0]-v0[0]; e2y = v2[1]-v0[1]; e2z = v2[2]-v0[2]
                nx = e1y*e2z - e1z*e2y
                ny = e1z*e2x - e1x*e2z
                nz = e1x*e2y - e1y*e2x
                gl_data.extend((nx, ny, nz,
                                v0[0], v0[1], v0[2],
                                v1[0], v1[1], v1[2],
                                v2[0], v2[1], v2[2]))

            # Effort logo mesh is rendered separately each frame so it can pulse
            if "Effort" in mesh_name:
                effort.append(gl_data)
            else:
                result.append((mat, gl_data))

        self._effort_meshes = effort
        print(f"[model] pre-computed {sum(len(d)//12 for _,d in result)} triangles "
              f"({sum(len(d)//12 for d in effort)} Effort) on bg thread")
        return result

    def update_ball_tracking(self):
        """Update the tracking camera to follow the ball"""
        # Only update if tracking is enabled and we have a ball position
        if not self.is_tracking_ball or self.ball_pos is None:
            return

        # Get the ball position
        x, y, z = self.ball_pos

        # Calculate camera position behind the ball
        offset_distance = 5  # Distance behind the ball
        height_offset = 1.5  # Position above the ball

        # If we have velocity information, position camera behind the ball's path
        if self.ball_vel is not None:
            vx, vy, vz = self.ball_vel
            speed = (vx**2 + vy**2 + vz**2)**0.5

            if speed > 0.1:  # Only use velocity if the ball is moving
                # Normalize velocity
                vx, vy, vz = vx/speed, vy/speed, vz/speed

                # Position camera behind and above
                self.camera['pos'] = [
                    x - vx * offset_distance,
                    y + height_offset,
                    z - vz * offset_distance
                ]

                # Update camera to point at ball
                self.camera['target'] = [x, y, z]
                return

        # Fallback if no velocity or ball not moving
        # Just position camera behind ball relative to home plate
        self.camera['pos'] = [x - offset_distance, y + height_offset, z]
        self.camera['target'] = [x, y, z]


    def set_spray_camera(self, enabled: bool):
        """Switch to a wide, centered camera for spray chart viewing."""
        if enabled:
            self._pre_spray_camera = self.camera.copy()
            self.camera = {
                'pos': [-20.0, 14.0, -12.0],
                'target': [42.0, 0.0, 50.0],
                'up': [0, 1, 0],
                'fov': 65
            }
        elif hasattr(self, '_pre_spray_camera'):
            self.camera = self._pre_spray_camera.copy()
        self.update()

    def toggle_ball_tracking(self):
        """Toggle between normal camera and ball tracking camera"""
        # Toggle tracking state
        self.is_tracking_ball = not self.is_tracking_ball
        print(f"Ball tracking toggled to: {self.is_tracking_ball}")

        if self.is_tracking_ball:
            # Save current camera settings to main_camera
            self.main_camera = self.camera.copy()
            print(f"Saved main camera: {self.main_camera}")
        else:
            # Restore main camera settings
            print(f"Restoring camera from {self.main_camera}")
            self.camera = self.main_camera.copy()

        self.update()
        return self.is_tracking_ball

    def initializeGL(self):
        glEnable(GL_DEPTH_TEST)
        glDepthFunc(GL_LEQUAL)
        glEnable(GL_LIGHTING)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        # Updated global ambient light for more realistic outdoor lighting
        global_ambient = [0.2, 0.2, 0.25, 1.0]  # Subtle bluish ambient for outdoor daylight
        glLightModelfv(GL_LIGHT_MODEL_AMBIENT, global_ambient)

        # Better lighting model settings
        glLightModeli(GL_LIGHT_MODEL_LOCAL_VIEWER, GL_TRUE)
        glLightModeli(GL_LIGHT_MODEL_TWO_SIDE, GL_TRUE)

        # Set a realistic sky blue background (instead of dark purple)
        glClearColor(0.529, 0.808, 0.922, 1.0)  # Sky blue (#87CEEB)

        # Enable normal vectors normalization for proper lighting
        glEnable(GL_NORMALIZE)

        # Set up lights based on parameters
        self.set_lighting(self.light_params)

        # GL context was (re-)created — previous display lists are invalid.
        self.stadium_display_list = None
        self._outfield_display_list = None
        if self.ballpark_model and self._precomputed_meshes:
            self.compile_stadium_display_list()

        # Shaders must be recompiled after a context (re-)init.
        self._setup_sky_shader()
        self._setup_stadium_lighting_shader()

    def _setup_stadium_lighting_shader(self):
        """Compile GLSL shader for per-pixel stadium lighting.

        Uses built-in gl_LightSource[i] and gl_FrontMaterial so the existing
        display list (glMaterialfv + glVertex calls) works unchanged — we just
        bind this program before glCallList and get real per-fragment spotlight
        evaluation instead of per-vertex interpolation.
        """
        self._stadium_shader = None

        vert_src = """
#version 130
out vec3 vPos;     // eye-space position
out vec3 vNormal;  // eye-space normal

void main() {
    vPos    = (gl_ModelViewMatrix * gl_Vertex).xyz;
    vNormal = normalize(gl_NormalMatrix * gl_Normal);
    gl_Position = gl_ModelViewProjectionMatrix * gl_Vertex;
}
"""
        frag_src = """
#version 130
in vec3 vPos;
in vec3 vNormal;
out vec4 fragColor;

void main() {
    vec3 N = normalize(vNormal);
    vec3 V = normalize(-vPos);  // eye at origin in eye space

    // Global ambient
    vec3 color = gl_LightModel.ambient.rgb * gl_FrontMaterial.ambient.rgb;

    // Evaluate all 6 lights per-pixel
    for (int i = 0; i < 6; i++) {
        vec3 lightDir;
        float attenuation = 1.0;

        if (gl_LightSource[i].position.w == 0.0) {
            // Directional light
            lightDir = normalize(gl_LightSource[i].position.xyz);
        } else {
            // Positional light
            vec3 toLight = gl_LightSource[i].position.xyz - vPos;
            float dist = length(toLight);
            lightDir = toLight / dist;
            attenuation = 1.0 / (gl_LightSource[i].constantAttenuation
                                + gl_LightSource[i].linearAttenuation * dist
                                + gl_LightSource[i].quadraticAttenuation * dist * dist);
        }

        // Spotlight factor
        float spotFactor = 1.0;
        if (gl_LightSource[i].spotCutoff < 180.0) {
            float spotCos = dot(-lightDir, normalize(gl_LightSource[i].spotDirection));
            if (spotCos < gl_LightSource[i].spotCosCutoff) {
                spotFactor = 0.0;
            } else {
                spotFactor = pow(max(spotCos, 0.0), gl_LightSource[i].spotExponent);
            }
        }

        float atten = attenuation * spotFactor;

        // Ambient
        color += gl_LightSource[i].ambient.rgb * gl_FrontMaterial.ambient.rgb * atten;

        // Diffuse
        float NdotL = max(dot(N, lightDir), 0.0);
        color += gl_LightSource[i].diffuse.rgb * gl_FrontMaterial.diffuse.rgb * NdotL * atten;

        // Specular (Blinn-Phong)
        if (NdotL > 0.0) {
            vec3 H = normalize(lightDir + V);
            float NdotH = max(dot(N, H), 0.0);
            float spec = pow(NdotH, gl_FrontMaterial.shininess);
            color += gl_LightSource[i].specular.rgb * gl_FrontMaterial.specular.rgb * spec * atten;
        }
    }

    fragColor = vec4(color, gl_FrontMaterial.diffuse.a);
}
"""
        try:
            prog = QOpenGLShaderProgram(self)
            ok_v = prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, vert_src)
            if not ok_v:
                print(f"[stadium-light] vertex shader compile failed: {prog.log()}")
                return
            ok_f = prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, frag_src)
            if not ok_f:
                print(f"[stadium-light] fragment shader compile failed: {prog.log()}")
                return
            if not prog.link():
                print(f"[stadium-light] shader link failed: {prog.log()}")
                return

            self._stadium_shader = prog
            print("[stadium-light] per-pixel lighting shader compiled OK")
        except Exception as e:
            print(f"[stadium-light] shader setup exception: {e}")
            self._stadium_shader = None

    def _setup_sky_shader(self):
        """Compile GLSL sky shader (fullscreen triangle, no VBO needed)."""
        self._sky_shader = None
        self._sky_vao = None

        vert_src = """
#version 130
out vec2 vUV;
void main() {
    // Fullscreen triangle from gl_VertexID (covers [-1,1] clip space)
    float x = float((gl_VertexID & 1) << 2) - 1.0;
    float y = float((gl_VertexID & 2) << 1) - 1.0;
    vUV = vec2(x, y);
    gl_Position = vec4(x, y, 0.999, 1.0);  // far depth
}
"""
        frag_src = """
#version 130
in vec2 vUV;
out vec4 fragColor;
uniform mat4 u_invViewProj;
uniform vec3 u_sunDir;
uniform float u_nightMix;  // 0.0 = day, 1.0 = night

// Pseudo-random hash for stars
float hash(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}

void main() {
    // Reconstruct view ray from clip-space UV
    vec4 clipNear = vec4(vUV, -1.0, 1.0);
    vec4 clipFar  = vec4(vUV,  1.0, 1.0);
    vec4 worldNear = u_invViewProj * clipNear;
    vec4 worldFar  = u_invViewProj * clipFar;
    worldNear /= worldNear.w;
    worldFar  /= worldFar.w;
    vec3 ray = normalize(worldFar.xyz - worldNear.xyz);

    float y = ray.y;

    // === DAY SKY ===
    // --- Zenith-to-horizon blue gradient ---
    vec3 zenith  = vec3(0.18, 0.30, 0.70);
    vec3 horizon = vec3(0.55, 0.70, 0.90);
    float t = pow(clamp(y, 0.0, 1.0), 0.6);
    vec3 daySky = mix(horizon, zenith, t);

    // --- Horizon haze (warm glow) ---
    float haze = exp(-abs(y) * 6.0);
    daySky += vec3(0.20, 0.15, 0.08) * haze;

    // --- Sun disk + corona ---
    float sunDot = max(dot(ray, u_sunDir), 0.0);
    float disk   = smoothstep(0.9994, 0.9998, sunDot);
    float corona = pow(sunDot, 256.0) * 0.6 + pow(sunDot, 32.0) * 0.15;
    daySky += vec3(1.0, 0.95, 0.85) * disk;
    daySky += vec3(1.0, 0.85, 0.55) * corona;

    // --- Below-horizon darkening (day) ---
    if (y < 0.0) {
        float dark = clamp(-y * 3.0, 0.0, 1.0);
        daySky = mix(daySky, vec3(0.25, 0.30, 0.35), dark);
    }

    // === NIGHT SKY ===
    vec3 nightZenith  = vec3(0.02, 0.02, 0.06);
    vec3 nightHorizon = vec3(0.05, 0.06, 0.12);
    float tn = pow(clamp(y, 0.0, 1.0), 0.5);
    vec3 nightSky = mix(nightHorizon, nightZenith, tn);

    // --- City-glow horizon tint ---
    float nightHaze = exp(-abs(y) * 4.0);
    nightSky += vec3(0.06, 0.06, 0.10) * nightHaze;

    // --- Stars (above horizon only) ---
    if (y > 0.3) {
        // Quantize ray direction for stable star positions
        vec2 starUV = ray.xz / (y + 0.001) * 80.0;
        vec2 starCell = floor(starUV);
        float starVal = hash(starCell);
        // Only ~3% of cells get a star
        if (starVal > 0.97) {
            // Brightness variation
            float brightness = 0.5 + 0.5 * hash(starCell + vec2(7.0, 13.0));
            // Twinkle based on slight offset
            float twinkle = 0.7 + 0.3 * sin(starVal * 100.0);
            // Size: tiny dot - check distance to cell center
            vec2 starPos = starCell + vec2(hash(starCell + vec2(1.0, 0.0)),
                                           hash(starCell + vec2(0.0, 1.0)));
            float dist = length(starUV - starPos);
            float starDot = smoothstep(0.15, 0.0, dist);
            nightSky += vec3(0.8, 0.85, 1.0) * starDot * brightness * twinkle;
        }
    }

    // --- Subtle moon (opposite sun direction) ---
    vec3 moonDir = normalize(vec3(-u_sunDir.x, abs(u_sunDir.y) + 0.3, -u_sunDir.z));
    float moonDot = max(dot(ray, moonDir), 0.0);
    float moonDisk = smoothstep(0.9990, 0.9997, moonDot);
    float moonGlow = pow(moonDot, 64.0) * 0.08;
    nightSky += vec3(0.7, 0.75, 0.9) * moonDisk * 0.6;
    nightSky += vec3(0.15, 0.15, 0.25) * moonGlow;

    // --- Below-horizon darkening (night) ---
    if (y < 0.0) {
        float dark = clamp(-y * 3.0, 0.0, 1.0);
        nightSky = mix(nightSky, vec3(0.01, 0.01, 0.02), dark);
    }

    // === BLEND ===
    vec3 sky = mix(daySky, nightSky, u_nightMix);

    fragColor = vec4(sky, 1.0);
}
"""
        try:
            prog = QOpenGLShaderProgram(self)
            ok_v = prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, vert_src)
            if not ok_v:
                print(f"[sky] vertex shader compile failed: {prog.log()}")
                return
            ok_f = prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, frag_src)
            if not ok_f:
                print(f"[sky] fragment shader compile failed: {prog.log()}")
                return
            if not prog.link():
                print(f"[sky] shader link failed: {prog.log()}")
                return

            self._sky_shader = prog
            self._sky_loc_invVP = prog.uniformLocation("u_invViewProj")
            self._sky_loc_sunDir = prog.uniformLocation("u_sunDir")
            self._sky_loc_nightMix = prog.uniformLocation("u_nightMix")
            self._night_mode = False

            # Create a dummy VAO in case we're in a core context
            from OpenGL.GL import glGenVertexArrays
            vao = glGenVertexArrays(1)
            self._sky_vao = vao

            print("[sky] procedural sky shader compiled OK")
        except Exception as e:
            print(f"[sky] shader setup exception: {e}")
            self._sky_shader = None

    def clear_ball(self):
        """Clear any displayed ball from the 3D view"""
        self.ball_pos = None
        self.ball_vel = None
        self.prev_ball_pos = None
        # self.ball_trail = []
        self.update()  # Request a redraw of the scene

    def _drawSkyShader(self):
        """Draw procedural sky via GLSL shader; falls back to dome if unavailable."""
        if self._sky_shader is None:
            self.drawSkyGradient()
            return

        # Read the matrices already set by gluPerspective / gluLookAt
        proj_raw = glGetFloatv(GL_PROJECTION_MATRIX)  # 4x4 column-major
        mv_raw   = glGetFloatv(GL_MODELVIEW_MATRIX)

        # QMatrix4x4 constructor takes row-major, GL returns column-major → transpose
        proj = QMatrix4x4([float(proj_raw[j][i]) for i in range(4) for j in range(4)])
        mv   = QMatrix4x4([float(mv_raw[j][i])   for i in range(4) for j in range(4)])

        vp = proj * mv
        inv_vp, invertible = vp.inverted()
        if not invertible:
            self.drawSkyGradient()
            return

        # Save GL state affected by the shader pass
        glPushAttrib(GL_ENABLE_BIT | GL_DEPTH_BUFFER_BIT)
        glDisable(GL_LIGHTING)
        glDisable(GL_DEPTH_TEST)
        glDepthMask(GL_FALSE)

        self._sky_shader.bind()
        self._sky_shader.setUniformValue(self._sky_loc_invVP, inv_vp)
        # Pleasant afternoon sun direction (normalized)
        self._sky_shader.setUniformValue(self._sky_loc_sunDir, 0.4, 0.6, 0.3)
        self._sky_shader.setUniformValue(self._sky_loc_nightMix, 1.0 if self._night_mode else 0.0)

        if self._sky_vao is not None:
            from OpenGL.GL import glBindVertexArray
            glBindVertexArray(self._sky_vao)

        glDrawArrays(GL_TRIANGLES, 0, 3)

        if self._sky_vao is not None:
            from OpenGL.GL import glBindVertexArray
            glBindVertexArray(0)

        self._sky_shader.release()

        glDepthMask(GL_TRUE)
        glPopAttrib()

    def drawSkyGradient(self):
        """Draw a distant sky dome that respects the depth buffer (fallback)"""
        # Save current states
        glPushAttrib(GL_ALL_ATTRIB_BITS)

        # Disable lighting for the sky
        glDisable(GL_LIGHTING)

        # Make sure depth testing is enabled but NEVER update the depth buffer for sky
        glEnable(GL_DEPTH_TEST)
        glDepthMask(GL_FALSE)  # Disable depth writing

        # Use a large radius dome
        radius = 400  # Just under the far clip plane (500)
        slices = 32
        stacks = 16

        # Save and set up matrices for the sky dome
        glMatrixMode(GL_MODELVIEW)
        glPushMatrix()

        # Position dome at camera target but at ground level
        # This keeps the sky centered on the scene
        target_x, target_y, target_z = self.camera['target']
        glTranslatef(target_x, 0, target_z)

        # Create the dome using GLU functions - only drawing the upper hemisphere
        dome = gluNewQuadric()
        gluQuadricDrawStyle(dome, GLU_FILL)

        # Draw with a gradient
        glShadeModel(GL_SMOOTH)

        # Clip the bottom half of the sphere to make a dome
        glPushMatrix()
        glClipPlane(GL_CLIP_PLANE0, [0, 1, 0, 0])  # Y >= 0
        glEnable(GL_CLIP_PLANE0)

        # Gradient drawing function for the dome
        def set_color_for_height(y_factor):
            # Map y-factor (-1 to 1) to appropriate color
            if y_factor > 0:
                # Interpolate between horizon color and zenith color
                # At horizon (y=0)
                horizon_color = [0.392, 0.584, 0.929, 1.0]  # Deeper blue
                # At zenith (y=1)
                zenith_color = [0.529, 0.808, 0.922, 1.0]   # Sky blue

                # Linear interpolation
                factor = y_factor
                r = horizon_color[0] + factor * (zenith_color[0] - horizon_color[0])
                g = horizon_color[1] + factor * (zenith_color[1] - horizon_color[1])
                b = horizon_color[2] + factor * (zenith_color[2] - horizon_color[2])
                glColor4f(r, g, b, 1.0)

        # We use a callback to set the colors for the dome
        def dome_callback(component, inner_radius, outer_radius, sweep, loops):
            glBegin(GL_QUADS)
            for i in range(loops):
                angle1 = (i / loops) * sweep
                angle2 = ((i + 1) / loops) * sweep

                y1 = math.sin(math.radians(angle1))
                y2 = math.sin(math.radians(angle2))

                # Set colors based on height
                set_color_for_height(y1)
                glVertex3d(0, inner_radius * y1, 0)
                glVertex3d(0, outer_radius * y1, 0)

                set_color_for_height(y2)
                glVertex3d(0, outer_radius * y2, 0)
                glVertex3d(0, inner_radius * y2, 0)
            glEnd()

        # Drawing a partial sphere to represent the sky dome
        # This is a simplified approach without a custom callback
        for i in range(stacks):
            y1 = math.cos(math.pi * i / stacks)
            y2 = math.cos(math.pi * (i + 1) / stacks)

            # Skip lower hemisphere
            if y1 < 0 and y2 < 0:
                continue

            glBegin(GL_QUAD_STRIP)
            for j in range(slices + 1):
                angle = 2 * math.pi * j / slices
                x = math.sin(angle)
                z = math.cos(angle)

                # Set color for first vertex
                set_color_for_height(y1)
                glVertex3f(x * radius * math.sin(math.acos(y1)),
                          y1 * radius,
                          z * radius * math.sin(math.acos(y1)))

                # Set color for second vertex
                set_color_for_height(y2)
                glVertex3f(x * radius * math.sin(math.acos(y2)),
                          y2 * radius,
                          z * radius * math.sin(math.acos(y2)))
            glEnd()

        # Clean up
        glDisable(GL_CLIP_PLANE0)
        glPopMatrix()

        gluDeleteQuadric(dome)

        # Restore matrix
        glPopMatrix()

        # Restore state
        glDepthMask(GL_TRUE)  # Re-enable depth writing
        glPopAttrib()



    def paintGL(self):
        """Override paintGL to update ball tracking"""
        if self.is_tracking_ball and self.ball_pos is not None: self.update_ball_tracking();

        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()

        # Set up perspective
        gluPerspective(self.camera['fov'], self.width()/self.height(), 0.1, 500)
        gluLookAt(*self.camera['pos'], *self.camera['target'], *self.camera['up'])

        # Draw procedural sky (falls back to dome gradient if shader unavailable)
        self._drawSkyShader()

        # Enable material properties
        glDisable(GL_COLOR_MATERIAL)
        glColorMaterial(GL_FRONT, GL_AMBIENT_AND_DIFFUSE)

        # Lazy-compile: if the bg thread delivered the model but the display
        # list hasn't been built yet (QTimer from a non-Qt thread can misfire),
        # compile it now — we're already in a valid GL context here.
        if self.ballpark_model and not self.stadium_display_list:
            self.compile_stadium_display_list()

        # Render stadium model using display list if available
        # In night mode, bind per-pixel lighting shader so spotlights are
        # evaluated per-fragment instead of per-vertex (which can't show
        # light pools on large triangles).
        _use_ppx = (self._night_mode
                     and self._stadium_shader is not None)
        if _use_ppx:
            self._stadium_shader.bind()

        if self.stadium_display_list:
            glCallList(self.stadium_display_list)

        if _use_ppx:
            self._stadium_shader.release()

        # Effort logo: gold neon-sign emissive pulse, redrawn each frame
        if self._effort_meshes:
            self._draw_effort_logo()

        # Outfield geometry (ground, wall, warning track, foul lines/poles)
        # is compiled into a display list that is rebuilt only when the stadium changes.
        if getattr(self, '_outfield_display_list', None):
            glCallList(self._outfield_display_list)
        elif self._stadium_name:
            self._compile_outfield_display_list()
            if self._outfield_display_list:
                glCallList(self._outfield_display_list)

        # Draw spray chart 3D trajectory trails
        if self.spray_trails:
            glPushAttrib(GL_LIGHTING_BIT | GL_CURRENT_BIT | GL_ENABLE_BIT | GL_LINE_BIT)
            glDisable(GL_LIGHTING)
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glLineWidth(2.0)
            for points, r, g, b in self.spray_trails:
                glColor4f(r, g, b, 0.7)
                glBegin(GL_LINE_STRIP)
                for px, py, pz in points:
                    glVertex3f(px, py, pz)
                glEnd()
                # Landing dot at final point
                if points:
                    lx, ly, lz = points[-1]
                    glPushMatrix()
                    glTranslatef(lx, max(ly, self._model_hp[1] + 0.05), lz)
                    glRotatef(-90, 1, 0, 0)
                    glColor4f(r, g, b, 0.85)
                    disk = gluNewQuadric()
                    gluDisk(disk, 0, 1.0, 12, 1)
                    gluDeleteQuadric(disk)
                    glPopMatrix()
            glPopAttrib()

        # Draw spray chart dots at ground level
        if self.spray_dots:
            glPushAttrib(GL_LIGHTING_BIT | GL_CURRENT_BIT | GL_ENABLE_BIT)
            glDisable(GL_LIGHTING)
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            for x_m, z_m, r, g, b in self.spray_dots:
                glPushMatrix()
                glTranslatef(x_m, self._model_hp[1] + 0.05, z_m)
                glRotatef(-90, 1, 0, 0)  # orient disk flat on ground
                glColor4f(r, g, b, 0.75)
                disk = gluNewQuadric()
                gluDisk(disk, 0, 1.2, 16, 1)  # ~2ft diameter disk (model scale)
                gluDeleteQuadric(disk)
                glPopMatrix()
            glPopAttrib()

        # Draw ball starting position indicator
        if hasattr(self, 'start_pos') and self.start_pos is not None:
            start_x, start_y, start_z = self.start_pos

            # Draw a larger orange sphere for the starting position
            glPushMatrix()
            glTranslatef(start_x, start_y, start_z)

            # Set orange material for the start indicator
            glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [1.0, 0.5, 0.0, 0.7])  # Orange, semi-transparent
            glMaterialfv(GL_FRONT, GL_SPECULAR, [1.0, 0.8, 0.2, 0.7])
            glMaterialf(GL_FRONT, GL_SHININESS, 50.0)

            # Draw a slightly larger sphere
            indicator = gluNewQuadric()
            gluQuadricDrawStyle(indicator, GLU_FILL)
            gluQuadricNormals(indicator, GLU_SMOOTH)
            gluSphere(indicator, 0.25, 16, 16)  # Slightly larger than the ball
            gluDeleteQuadric(indicator)
            glPopMatrix()

        # Draw ball trail (new code)
        if self.ball_trail:
            glPushMatrix()
            # Use a gradient of transparency for the trail
            for i, (trail_pos, alpha) in enumerate(self.ball_trail):
                x, y, z = trail_pos
                # Make trail segments increasingly transparent
                alpha = alpha * 0.9  # Further reduce alpha

                # Set material for trail segment
                if self.pitch_trail_color is not None:
                    r, g, b = self.pitch_trail_color
                    glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [r, g, b, alpha])
                    glMaterialfv(GL_FRONT, GL_SPECULAR, [r*0.5, g*0.5, b*0.5, alpha])
                else:
                    glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [1.0, 1.0, 1.0, alpha])
                    glMaterialfv(GL_FRONT, GL_SPECULAR, [1.0, 1.0, 1.0, alpha])
                glMaterialf(GL_FRONT, GL_SHININESS, 80.0)

                # Draw smaller spheres for trail
                glPushMatrix()
                glTranslatef(x, y, z)
                # Scale based on position in trail (smaller toward end)
                scale_factor = 0.8 - (i * 0.1)
                trail_size = max(0.05, 0.2 * scale_factor)
                sphere = gluNewQuadric()
                gluQuadricDrawStyle(sphere, GLU_FILL)
                gluQuadricNormals(sphere, GLU_SMOOTH)
                gluSphere(sphere, trail_size, 8, 8)  # Smaller, less detailed spheres for trail
                gluDeleteQuadric(sphere)
                glPopMatrix()

                # Update alpha for next segment
                # self.ball_trail[i] = (trail_pos, alpha)
            glPopMatrix()

        # Ball rendering (with velocity-based effects)
        if self.ball_pos is not None:
            x, y, z = self.ball_pos

            # Calculate ball speed if velocity data exists
            ball_speed = 0
            if self.ball_vel is not None:
                vx, vy, vz = self.ball_vel
                ball_speed = (vx**2 + vy**2 + vz**2)**0.5

                # Update ball trail
                if self.prev_ball_pos is not None:
                    # Only add to trail if ball has moved sufficiently
                    px, py, pz = self.prev_ball_pos
                    dist = ((x-px)**2 + (y-py)**2 + (z-pz)**2)**0.5
                    if dist > self.trail_min_dist:  # Minimum distance to add a trail point
                        # Add current position to trail with full opacity
                        self.ball_trail.insert(0, ((x, y, z), 0.7))

            # Store current position for next frame
            self.prev_ball_pos = (x, y, z)

            # Set white material for the ball (with velocity-based effects)
            # Use more red for faster balls
            red = min(1.0, 0.8 + ball_speed/30)
            green = max(0.7, 1.0 - ball_speed/20)
            blue = max(0.7, 1.0 - ball_speed/20)

            glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [red, green, blue, 1.0])
            glMaterialfv(GL_FRONT, GL_SPECULAR, [1.0, 1.0, 1.0, 1.0])
            glMaterialf(GL_FRONT, GL_SHININESS, 80.0)

            # Draw ball with size based slightly on velocity (motion blur effect)
            glPushMatrix()
            glTranslatef(x, y, z)

            # Add slight stretching in direction of motion for high velocities
            if self.ball_vel is not None and ball_speed > 5:
                vx, vy, vz = self.ball_vel
                # Normalize velocity vector
                norm = (vx**2 + vy**2 + vz**2)**0.5
                if norm > 0:
                    vx, vy, vz = vx/norm, vy/norm, vz/norm

                # Calculate rotation axis and angle to stretch ball along velocity
                stretch_factor = min(1.5, 1.0 + ball_speed/30)

                # Apply stretch transformation using a scaling matrix
                if ball_speed > 10:  # Only stretch for higher speeds
                    # Create rotation to align with velocity vector
                    # Find rotation axis (cross product of [0,0,1] and velocity)
                    axis_x = -vy
                    axis_y = vx
                    axis_z = 0
                    axis_len = (axis_x**2 + axis_y**2 + axis_z**2)**0.5

                    if axis_len > 0.001:  # Avoid division by near-zero
                        axis_x, axis_y, axis_z = axis_x/axis_len, axis_y/axis_len, axis_z/axis_len
                        # Calculate rotation angle
                        angle = math.degrees(math.acos(vz/norm))
                        # Apply rotation
                        glRotatef(angle, axis_x, axis_y, axis_z)
                        # Stretch along z-axis (now aligned with velocity)
                        glScalef(1.0, 1.0, stretch_factor)

            # Ball size
            ball_size = 0.2

            sphere = gluNewQuadric()
            gluQuadricDrawStyle(sphere, GLU_FILL)
            gluQuadricNormals(sphere, GLU_SMOOTH)
            gluSphere(sphere, ball_size, 16, 16)
            gluDeleteQuadric(sphere)
            glPopMatrix()

            # Draw shadow with proper physics
            glPushMatrix()
            glTranslatef(x, self._model_hp[1] + 0.02, z)  # Shadow at model ground level

            # Shadow darkness based on height above ground (in model units)
            height_above_ground = y - self._model_hp[1]
            shadow_alpha = max(0.1, min(0.6, 0.6 - height_above_ground/50))
            glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [0.0, 0.0, 0.0, shadow_alpha])
            glMaterialfv(GL_FRONT, GL_SPECULAR, [0.0, 0.0, 0.0, 0.0])
            glMaterialf(GL_FRONT, GL_SHININESS, 0.0)

            # Shadow size scales with height above ground
            shadow_scale = max(0.5, min(1.0, 0.8 + 0.2*(25-height_above_ground)/25))
            glScalef(shadow_scale, 0.1, shadow_scale)

            # Draw shadow
            shadow = gluNewQuadric()
            gluQuadricDrawStyle(shadow, GLU_FILL)
            gluQuadricNormals(shadow, GLU_SMOOTH)
            gluDisk(shadow, 0, 0.5, 16, 1)
            gluDeleteQuadric(shadow)
            glPopMatrix()

        # IMPORTANT: Draw light sources at the global level, outside of any object transformations
        # This ensures lights are drawn in world space coordinates, not relative to any object
        if hasattr(self, 'show_lights') and self.show_lights:
            self.draw_light_sources(True)



 # this function actually tries to load the mats from .mtl file

    def compile_stadium_display_list(self):
        """Compile the OBJ model into a GL display list.

        All heavy work (normal computation, material lookup, vertex flattening)
        was already done on the background thread in _precompute_mesh_data().
        This method just streams the pre-built arrays into GL calls — typically
        completes in <50ms for the ~9k triangle model.
        """
        if not self.ballpark_model:
            return
        precomputed = getattr(self, '_precomputed_meshes', None)
        if not precomputed:
            return

        self.stadium_display_list = glGenLists(1)
        glNewList(self.stadium_display_list, GL_COMPILE)
        glPushMatrix()

        for mat, gl_data in precomputed:
            ambient, diffuse, specular, shininess, emission = mat
            glMaterialfv(GL_FRONT, GL_AMBIENT, ambient)
            glMaterialfv(GL_FRONT, GL_DIFFUSE, diffuse)
            glMaterialfv(GL_FRONT, GL_SPECULAR, specular)
            glMaterialf(GL_FRONT, GL_SHININESS, shininess)
            if emission is not None:
                glMaterialfv(GL_FRONT, GL_EMISSION, emission)

            # Each triangle is 12 floats: nx,ny,nz, v0x,v0y,v0z, v1x,v1y,v1z, v2x,v2y,v2z
            n = len(gl_data)
            glBegin(GL_TRIANGLES)
            for i in range(0, n, 12):
                nx, ny, nz = gl_data[i], gl_data[i+1], gl_data[i+2]
                if nx or ny or nz:
                    glNormal3f(nx, ny, nz)
                glVertex3f(gl_data[i+3], gl_data[i+4], gl_data[i+5])
                glVertex3f(gl_data[i+6], gl_data[i+7], gl_data[i+8])
                glVertex3f(gl_data[i+9], gl_data[i+10], gl_data[i+11])
            glEnd()

            # Reset emission if it was set
            if emission is not None:
                glMaterialfv(GL_FRONT, GL_EMISSION, [0.0, 0.0, 0.0, 1.0])

        glPopMatrix()
        glEndList()
        print(f"[model] display list compiled ({sum(len(d)//12 for _,d in precomputed)} triangles)")

    def _draw_effort_logo(self):
        """Render the Effort logo mesh as an emissive sign with a sweeping
        light pulse that traces across the text every few seconds.

        Drawn outside the stadium display list so the sweep can animate.
        Pass 1: solid emissive core (warm gold, gentle breath pulse).
        Pass 2: additive sweep — per-vertex brightness driven by a Gaussian
        window centered on a moving x-position that crosses the bounding box.
        """
        # ------------------------------------------------------------------ #
        # One-time bbox compute along whichever axis has the largest extent.
        # That axis is the natural "left→right" direction of the text.
        # ------------------------------------------------------------------ #
        if not hasattr(self, '_effort_bbox') or self._effort_bbox is None:
            mins = [float('inf')] * 3
            maxs = [float('-inf')] * 3
            for gl_data in self._effort_meshes:
                n = len(gl_data)
                for i in range(0, n, 12):
                    for j in (3, 6, 9):
                        for a in range(3):
                            v = gl_data[i + j + a]
                            if v < mins[a]: mins[a] = v
                            if v > maxs[a]: maxs[a] = v
            spans = [maxs[a] - mins[a] for a in range(3)]
            sweep_axis = max(range(3), key=lambda a: spans[a])
            self._effort_bbox = (mins, maxs, sweep_axis, spans[sweep_axis])

        mins, maxs, axis, span = self._effort_bbox

        t = time.time() - self._effort_t0

        # Slow breath pulse for the base emission
        breath = 0.5 + 0.5 * math.sin(t * 1.6)
        base_r = 1.0
        base_g = 0.78 + 0.15 * breath
        base_b = 0.25 + 0.45 * breath

        # Sweep cycle: ~3.5s, sweep travels from one bbox edge past the other
        SWEEP_PERIOD = 3.5
        SWEEP_WIDTH  = max(span * 0.18, 0.05)   # gaussian sigma, in model units
        cycle = (t % SWEEP_PERIOD) / SWEEP_PERIOD
        # Travel slightly past both ends so the trail enters/exits cleanly
        sweep_pos = mins[axis] - SWEEP_WIDTH + cycle * (span + 2 * SWEEP_WIDTH)
        inv_two_sigma_sq = 1.0 / (2.0 * SWEEP_WIDTH * SWEEP_WIDTH)

        glPushAttrib(GL_LIGHTING_BIT | GL_CURRENT_BIT | GL_ENABLE_BIT |
                     GL_DEPTH_BUFFER_BIT | GL_COLOR_BUFFER_BIT | GL_POLYGON_BIT)

        # ------------------------------------------------------------------ #
        # Pass 1 — solid emissive core (writes depth, populates buffer)
        # ------------------------------------------------------------------ #
        glEnable(GL_LIGHTING)
        glEnable(GL_DEPTH_TEST)
        glDepthMask(GL_TRUE)
        glDisable(GL_BLEND)
        glMaterialfv(GL_FRONT_AND_BACK, GL_AMBIENT,  [0.10, 0.07, 0.02, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_DIFFUSE,  [0.30, 0.22, 0.05, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_SPECULAR, [1.0, 0.9, 0.5, 1.0])
        glMaterialf (GL_FRONT_AND_BACK, GL_SHININESS, 96.0)
        glMaterialfv(GL_FRONT_AND_BACK, GL_EMISSION, [base_r, base_g, base_b, 1.0])

        for gl_data in self._effort_meshes:
            n = len(gl_data)
            glBegin(GL_TRIANGLES)
            for i in range(0, n, 12):
                nx, ny, nz = gl_data[i], gl_data[i+1], gl_data[i+2]
                if nx or ny or nz:
                    glNormal3f(nx, ny, nz)
                glVertex3f(gl_data[i+3], gl_data[i+4], gl_data[i+5])
                glVertex3f(gl_data[i+6], gl_data[i+7], gl_data[i+8])
                glVertex3f(gl_data[i+9], gl_data[i+10], gl_data[i+11])
            glEnd()

        # ------------------------------------------------------------------ #
        # Pass 2 — additive sweep highlight (per-vertex Gaussian intensity)
        # ------------------------------------------------------------------ #
        glDisable(GL_LIGHTING)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)   # additive
        glDepthMask(GL_FALSE)
        # Bright white-hot core for the sweep
        sweep_r, sweep_g, sweep_b = 1.0, 0.95, 0.75
        # Vertex coord offset within gl_data (axis maps to slot 3+axis, 6+axis, 9+axis)
        a = axis

        for gl_data in self._effort_meshes:
            n = len(gl_data)
            glBegin(GL_TRIANGLES)
            for i in range(0, n, 12):
                for vbase in (3, 6, 9):
                    v_axis = gl_data[i + vbase + a]
                    d = v_axis - sweep_pos
                    intensity = math.exp(-(d * d) * inv_two_sigma_sq)
                    if intensity < 0.01:
                        glColor4f(0.0, 0.0, 0.0, 0.0)
                    else:
                        glColor4f(sweep_r, sweep_g, sweep_b, intensity)
                    glVertex3f(gl_data[i + vbase],
                               gl_data[i + vbase + 1],
                               gl_data[i + vbase + 2])
            glEnd()

        glPopAttrib()

    def _compile_outfield_display_list(self):
        """Compile procedural outfield geometry into a GL display list.

        Ground plane, wall, warning track, foul lines, foul poles — all derived
        from STADIUM_DATA wall distances via physics_to_model().  This is called
        once per stadium change; subsequent frames just glCallList.
        """
        if not self._stadium_name:
            return

        self._outfield_display_list = glGenLists(1)
        glNewList(self._outfield_display_list, GL_COMPILE)

        # --- shared constants ---
        POLE_HEIGHT_FT = 30.0         # foul pole height (feet)
        POLE_RADIUS_M  = 0.07         # foul pole radius in meters (~3 inches)
        WARN_TRACK_FT  = 20.0         # warning track depth (feet in front of wall)
        GROUND_Y_M     = 0.0          # physics Y of ground level
        POLE_H_M       = POLE_HEIGHT_FT / 3.28084
        FT_TO_M        = 1.0 / 3.28084

        # ------------------------------------------------------------------ #
        # Build wall point list: one entry per integer polar angle 0..90      #
        # Each entry: (bx,by,bz, tx,ty,tz, dist_ft, phys_x_m, phys_z_m,    #
        #              wall_h_m)  or None if no data                          #
        # ------------------------------------------------------------------ #
        polar_angles = list(range(0, 91))   # 0..90 inclusive, 1° steps
        wall_pts = []

        for theta in polar_angles:
            dist_ft = get_stadium_wall_distance(self._stadium_name, theta)
            if dist_ft is None or dist_ft <= 0:
                wall_pts.append(None)
                continue
            wall_h_ft = get_stadium_wall_height(self._stadium_name, theta)
            wall_h_m  = wall_h_ft * FT_TO_M
            phys_angle = math.radians(theta + 45)
            x_m = dist_ft * math.sin(phys_angle) * FT_TO_M
            z_m = dist_ft * math.cos(phys_angle) * FT_TO_M
            bx, by, bz = self.physics_to_model(x_m, GROUND_Y_M, z_m)
            tx, ty, tz = self.physics_to_model(x_m, wall_h_m,   z_m)
            wall_pts.append((bx, by, bz, tx, ty, tz, dist_ft, x_m, z_m, wall_h_m))

        # Pre-build list of valid adjacent pairs for segment drawing
        valid_pairs = [
            (i, wall_pts[i], wall_pts[i+1])
            for i in range(len(wall_pts) - 1)
            if wall_pts[i] is not None and wall_pts[i+1] is not None
        ]

        # Home plate model position (used as fan center and normal reference)
        origin_mx, origin_my, origin_mz = self.physics_to_model(0.0, 0.0, 0.0)
        hp_mx, _hp_my, hp_mz = self._model_hp

        # ------------------------------------------------------------------ #
        # Push shared OpenGL state                                            #
        # ------------------------------------------------------------------ #
        glPushAttrib(GL_LIGHTING_BIT | GL_CURRENT_BIT | GL_ENABLE_BIT |
                     GL_POLYGON_BIT | GL_DEPTH_BUFFER_BIT | GL_LINE_BIT)
        glEnable(GL_LIGHTING)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        # ================================================================== #
        # Component 1 — Outfield ground plane (full fan from HP)              #
        # Triangle fan from home plate to all wall base points.               #
        # GROUND_DROP = -0.08m puts the grass at model Y ≈ 0.95, safely     #
        # below the infield dirt (model Y ≈ 0.985) and infield grass         #
        # (model Y ≈ 1.14). The depth buffer naturally hides the procedural  #
        # grass wherever the OBJ model exists above it.                       #
        # ================================================================== #
        GROUND_DROP = -0.08  # ~8cm below HP → model Y ≈ 0.95 (below dirt at 0.985)
        RADIAL_STEPS = 6    # subdivide radially to avoid per-vertex lighting streaks

        glEnable(GL_POLYGON_OFFSET_FILL)
        glPolygonOffset(1.0, 1.0)

        glMaterialfv(GL_FRONT_AND_BACK, GL_AMBIENT,  [0.05, 0.20, 0.05, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_DIFFUSE,  [0.08, 0.30, 0.08, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_SPECULAR, [0.02, 0.05, 0.02, 1.0])
        glMaterialf (GL_FRONT_AND_BACK, GL_SHININESS, 4.0)
        glNormal3f(0.0, 1.0, 0.0)   # flat ground, normal straight up

        # Subdivided quad grid: RADIAL_STEPS rings × 90 angular slices
        # Eliminates per-vertex lighting streaks from giant triangles
        for i, pt0, pt1 in valid_pairs:
            dist0 = pt0[6]  # wall distance in ft for this angle
            dist1 = pt1[6]
            pa0 = math.radians(polar_angles[i] + 45)
            pa1 = math.radians(polar_angles[i + 1] + 45)
            for r in range(RADIAL_STEPS):
                frac0 = r / RADIAL_STEPS
                frac1 = (r + 1) / RADIAL_STEPS
                # Inner and outer radius for this ring segment
                r0_ft = frac0 * dist0
                r1_ft = frac1 * dist0
                r0b_ft = frac0 * dist1
                r1b_ft = frac1 * dist1
                # Four corners of the quad
                x00 = r0_ft * math.sin(pa0) * FT_TO_M
                z00 = r0_ft * math.cos(pa0) * FT_TO_M
                x10 = r1_ft * math.sin(pa0) * FT_TO_M
                z10 = r1_ft * math.cos(pa0) * FT_TO_M
                x01 = r0b_ft * math.sin(pa1) * FT_TO_M
                z01 = r0b_ft * math.cos(pa1) * FT_TO_M
                x11 = r1b_ft * math.sin(pa1) * FT_TO_M
                z11 = r1b_ft * math.cos(pa1) * FT_TO_M
                mx00, my00, mz00 = self.physics_to_model(x00, GROUND_DROP, z00)
                mx10, my10, mz10 = self.physics_to_model(x10, GROUND_DROP, z10)
                mx01, my01, mz01 = self.physics_to_model(x01, GROUND_DROP, z01)
                mx11, my11, mz11 = self.physics_to_model(x11, GROUND_DROP, z11)
                glBegin(GL_QUADS)
                glVertex3f(mx00, my00, mz00)
                glVertex3f(mx10, my10, mz10)
                glVertex3f(mx11, my11, mz11)
                glVertex3f(mx01, my01, mz01)
                glEnd()

        glDisable(GL_POLYGON_OFFSET_FILL)

        # ================================================================== #
        # Component 3 — Warning track                                         #
        # Brown/clay arc on the ground, 20 ft wide, immediately inside wall. #
        # ================================================================== #
        glMaterialfv(GL_FRONT_AND_BACK, GL_AMBIENT,  [0.25, 0.15, 0.05, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_DIFFUSE,  [0.60, 0.38, 0.18, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_SPECULAR, [0.05, 0.03, 0.01, 1.0])
        glMaterialf (GL_FRONT_AND_BACK, GL_SHININESS, 4.0)
        glNormal3f(0.0, 1.0, 0.0)

        for i, pt0, pt1 in valid_pairs:
            theta0  = polar_angles[i]
            theta1  = polar_angles[i + 1]
            dist0   = pt0[6]
            dist1   = pt1[6]
            inner0_ft = max(0.0, dist0 - WARN_TRACK_FT)
            inner1_ft = max(0.0, dist1 - WARN_TRACK_FT)
            pa0 = math.radians(theta0 + 45)
            pa1 = math.radians(theta1 + 45)
            ix0_m = inner0_ft * math.sin(pa0) * FT_TO_M
            iz0_m = inner0_ft * math.cos(pa0) * FT_TO_M
            ix1_m = inner1_ft * math.sin(pa1) * FT_TO_M
            iz1_m = inner1_ft * math.cos(pa1) * FT_TO_M
            imx0, imy0, imz0 = self.physics_to_model(ix0_m, GROUND_Y_M, iz0_m)
            imx1, imy1, imz1 = self.physics_to_model(ix1_m, GROUND_Y_M, iz1_m)

            glBegin(GL_QUADS)
            glVertex3f(imx0,    imy0,    imz0)
            glVertex3f(pt0[0],  pt0[1],  pt0[2])   # outer = bottom of wall pt0
            glVertex3f(pt1[0],  pt1[1],  pt1[2])   # outer = bottom of wall pt1
            glVertex3f(imx1,    imy1,    imz1)
            glEnd()

        # ================================================================== #
        # Component 2 — Outfield wall face + top cap                          #
        # Vertical green face; inward-facing normal per quad segment.         #
        # Grey horizontal cap strip on top.                                   #
        # ================================================================== #
        # Wall face (dark green)
        glMaterialfv(GL_FRONT_AND_BACK, GL_AMBIENT,  [0.05, 0.20, 0.05, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_DIFFUSE,  [0.10, 0.40, 0.10, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_SPECULAR, [0.05, 0.15, 0.05, 1.0])
        glMaterialf (GL_FRONT_AND_BACK, GL_SHININESS, 16.0)

        for i, pt0, pt1 in valid_pairs:
            bx0, by0, bz0 = pt0[0], pt0[1], pt0[2]
            tx0, ty0, tz0 = pt0[3], pt0[4], pt0[5]
            bx1, by1, bz1 = pt1[0], pt1[1], pt1[2]
            tx1, ty1, tz1 = pt1[3], pt1[4], pt1[5]

            # Normal: from wall midpoint toward home plate in XZ, Y=0
            mid_x = (bx0 + bx1) * 0.5
            mid_z = (bz0 + bz1) * 0.5
            nx = hp_mx - mid_x
            nz = hp_mz - mid_z
            n_len = math.sqrt(nx * nx + nz * nz)
            if n_len > 0:
                nx /= n_len;  nz /= n_len
            glNormal3f(nx, 0.0, nz)

            glBegin(GL_QUADS)
            glVertex3f(bx0, by0, bz0)
            glVertex3f(tx0, ty0, tz0)
            glVertex3f(tx1, ty1, tz1)
            glVertex3f(bx1, by1, bz1)
            glEnd()

        # Top cap (grey)
        glMaterialfv(GL_FRONT_AND_BACK, GL_AMBIENT,  [0.10, 0.10, 0.10, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_DIFFUSE,  [0.25, 0.25, 0.25, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_SPECULAR, [0.10, 0.10, 0.10, 1.0])
        glMaterialf (GL_FRONT_AND_BACK, GL_SHININESS, 8.0)
        glNormal3f(0.0, 1.0, 0.0)

        CAP_DEPTH_FT = 1.0   # 1 ft deep outward from wall top
        for i, pt0, pt1 in valid_pairs:
            tx0, ty0, tz0 = pt0[3], pt0[4], pt0[5]
            tx1, ty1, tz1 = pt1[3], pt1[4], pt1[5]
            theta0 = polar_angles[i];       theta1 = polar_angles[i + 1]
            dist0  = pt0[6];                dist1  = pt1[6]
            wh0_m  = pt0[9];                wh1_m  = pt1[9]
            pa0 = math.radians(theta0 + 45);  pa1 = math.radians(theta1 + 45)
            ox0_m = (dist0 + CAP_DEPTH_FT) * math.sin(pa0) * FT_TO_M
            oz0_m = (dist0 + CAP_DEPTH_FT) * math.cos(pa0) * FT_TO_M
            ox1_m = (dist1 + CAP_DEPTH_FT) * math.sin(pa1) * FT_TO_M
            oz1_m = (dist1 + CAP_DEPTH_FT) * math.cos(pa1) * FT_TO_M
            otx0, oty0, otz0 = self.physics_to_model(ox0_m, wh0_m, oz0_m)
            otx1, oty1, otz1 = self.physics_to_model(ox1_m, wh1_m, oz1_m)

            glBegin(GL_QUADS)
            glVertex3f(tx0,  ty0,  tz0)
            glVertex3f(otx0, oty0, otz0)
            glVertex3f(otx1, oty1, otz1)
            glVertex3f(tx1,  ty1,  tz1)
            glEnd()

        # ================================================================== #
        # Component 5 — Foul lines                                            #
        # White GL_LINES from home plate along each foul line to foul poles.  #
        # ================================================================== #
        glDisable(GL_LIGHTING)
        glLineWidth(2.5)
        glColor3f(1.0, 1.0, 1.0)

        rf_dist = get_stadium_wall_distance(self._stadium_name, 0.0)
        lf_dist = get_stadium_wall_distance(self._stadium_name, 90.0)

        if rf_dist and rf_dist > 0:
            pa = math.radians(45)
            rx_m = rf_dist * math.sin(pa) * FT_TO_M
            rz_m = rf_dist * math.cos(pa) * FT_TO_M
            rmx, rmy, rmz = self.physics_to_model(rx_m, GROUND_Y_M, rz_m)
            glBegin(GL_LINES)
            glVertex3f(origin_mx, origin_my + 0.02, origin_mz)
            glVertex3f(rmx,       rmy       + 0.02, rmz)
            glEnd()

        if lf_dist and lf_dist > 0:
            pa = math.radians(135)
            lx_m = lf_dist * math.sin(pa) * FT_TO_M
            lz_m = lf_dist * math.cos(pa) * FT_TO_M
            lmx, lmy, lmz = self.physics_to_model(lx_m, GROUND_Y_M, lz_m)
            glBegin(GL_LINES)
            glVertex3f(origin_mx, origin_my + 0.02, origin_mz)
            glVertex3f(lmx,       lmy       + 0.02, lmz)
            glEnd()

        # ================================================================== #
        # Component 4 — Foul poles                                            #
        # Yellow gluCylinder at RF (polar 0°) and LF (polar 90°) corners.    #
        # ================================================================== #
        glEnable(GL_LIGHTING)
        glMaterialfv(GL_FRONT_AND_BACK, GL_AMBIENT,  [0.30, 0.28, 0.00, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_DIFFUSE,  [1.00, 0.90, 0.00, 1.0])
        glMaterialfv(GL_FRONT_AND_BACK, GL_SPECULAR, [0.60, 0.55, 0.10, 1.0])
        glMaterialf (GL_FRONT_AND_BACK, GL_SHININESS, 40.0)

        pole_r_model = POLE_RADIUS_M * self._model_scale

        for polar_base, dist_ft in [(0.0, rf_dist), (90.0, lf_dist)]:
            if dist_ft is None or dist_ft <= 0:
                continue
            pa  = math.radians(polar_base + 45)
            x_m = dist_ft * math.sin(pa) * FT_TO_M
            z_m = dist_ft * math.cos(pa) * FT_TO_M
            base_mx, base_my, base_mz = self.physics_to_model(x_m, GROUND_Y_M, z_m)
            top_mx,  top_my,  top_mz  = self.physics_to_model(x_m, POLE_H_M,   z_m)

            pole_h_model = top_my - base_my

            glPushMatrix()
            glTranslatef(base_mx, base_my, base_mz)

            # Rotate gluCylinder (default draws along +Z) to point toward top in model space
            dx = top_mx - base_mx
            dy = top_my - base_my
            dz = top_mz - base_mz
            length = math.sqrt(dx*dx + dy*dy + dz*dz)
            if length > 1e-6:
                dx /= length;  dy /= length;  dz /= length
                # Rotation axis = cross(Z_hat, target) = (-dy, dx, 0)
                ax = -dy;  ay = dx
                a_len = math.sqrt(ax*ax + ay*ay)
                if a_len > 1e-4:
                    angle_deg = math.degrees(math.acos(max(-1.0, min(1.0, dz))))
                    glRotatef(angle_deg, ax / a_len, ay / a_len, 0.0)

            cyl = gluNewQuadric()
            gluQuadricDrawStyle(cyl, GLU_FILL)
            gluQuadricNormals(cyl, GLU_SMOOTH)
            gluCylinder(cyl, pole_r_model, pole_r_model, pole_h_model, 8, 1)
            gluDeleteQuadric(cyl)
            glPopMatrix()

        # ------------------------------------------------------------------ #
        glPopAttrib()
        glEndList()
        print(f"[outfield] display list compiled for {self._stadium_name}")

    def physics_to_model(self, x_m, y_m, z_m):
        """Convert physics coordinates (meters) to 3D model coordinates.

        Physics: X = toward CF, Y = up, Z = toward RF (+) / LF (-)
        Model:   CF direction is at 45° in XZ plane, scale ~2.32 units/m,
                 home plate at (-7.38, 1.14, 0.90).
        """
        # Rotate 45° around Y axis and scale
        mx = (x_m * self._model_cos - z_m * self._model_sin) * self._model_scale + self._model_hp[0]
        my = y_m * self._model_scale + self._model_hp[1]
        mz = (x_m * self._model_sin + z_m * self._model_cos) * self._model_scale + self._model_hp[2]
        return mx, my, mz

    def set_start_position(self, x, y, z):
        """Set the starting position indicator for the 3D view"""
        self.start_pos = self.physics_to_model(x, y, z)
        self.update()  # Request a redraw of the scene

    # ---- Spray chart --------------------------------------------------- #
    SAVANT_HP_X, SAVANT_HP_Y = 125.42, 198.27
    SAVANT_SCALE = 2.51  # feet per Savant pixel
    FT_TO_M = 1.0 / 3.28084

    _SPRAY_RGB = {
        "home_run": (1.0, 0.78, 0.2),
        "single":   (0.4, 0.82, 0.4),
        "double":   (0.4, 0.7, 1.0),
        "triple":   (0.4, 0.86, 1.0),
        "foul":     (0.55, 0.55, 0.55),
    }
    _SPRAY_OUT_RGB = (0.8, 0.27, 0.27)

    def set_spray_chart(self, events: list):
        """Populate spray dots from BBE events with hc_x/hc_y."""
        self.spray_dots = []
        for ev in events:
            hc_x = ev.get("hc_x")
            hc_y = ev.get("hc_y")
            if hc_x is None or hc_y is None:
                continue
            try:
                hc_x, hc_y = float(hc_x), float(hc_y)
            except (ValueError, TypeError):
                continue
            if math.isnan(hc_x) or math.isnan(hc_y):
                continue

            dx_ft = (hc_x - self.SAVANT_HP_X) * self.SAVANT_SCALE
            dy_ft = (self.SAVANT_HP_Y - hc_y) * self.SAVANT_SCALE

            # Physics coords: x toward CF, z toward RF (meters)
            x_phys = dy_ft * self.FT_TO_M
            z_phys = dx_ft * self.FT_TO_M
            # Transform to model coordinates
            mx, _, mz = self.physics_to_model(x_phys, 0, z_phys)

            event_type = str(ev.get("events", ""))
            r, g, b = self._SPRAY_RGB.get(event_type, self._SPRAY_OUT_RGB)
            self.spray_dots.append((mx, mz, r, g, b))
        self.update()

    def clear_spray_chart(self):
        self.spray_dots = []
        self.spray_trails = []
        self.update()

    def add_spray_trail(self, traj: dict, r: float, g: float, b: float):
        """Add a full trajectory as a persistent 3D trail line."""
        FT = self.FT_TO_M
        points = []
        step = max(1, len(traj["x"]) // 60)  # ~60 points per trail
        for i in range(0, len(traj["x"]), step):
            points.append(self.physics_to_model(
                traj["x"][i] * FT,
                traj["y"][i] * FT,
                traj["z"][i] * FT,
            ))
        if points:
            self.spray_trails.append((points, r, g, b))
            self.update()

    def add_spray_dot(self, ev: dict):
        """Add a single spray dot (used for animated playback)."""
        hc_x = ev.get("hc_x")
        hc_y = ev.get("hc_y")
        if hc_x is None or hc_y is None:
            return
        try:
            hc_x, hc_y = float(hc_x), float(hc_y)
        except (ValueError, TypeError):
            return
        if math.isnan(hc_x) or math.isnan(hc_y):
            return
        dx_ft = (hc_x - self.SAVANT_HP_X) * self.SAVANT_SCALE
        dy_ft = (self.SAVANT_HP_Y - hc_y) * self.SAVANT_SCALE
        x_phys = dy_ft * self.FT_TO_M
        z_phys = dx_ft * self.FT_TO_M
        mx, _, mz = self.physics_to_model(x_phys, 0, z_phys)
        event_type = str(ev.get("events", ""))
        r, g, b = self._SPRAY_RGB.get(event_type, self._SPRAY_OUT_RGB)
        self.spray_dots.append((mx, mz, r, g, b))
        self.update()

    def clear_ball(self):
        """Clear any displayed ball from the 3D view but keep the starting position"""
        self.ball_pos = None
        self.update()  # Request a redraw of the scene

    def set_lighting(self, light_params):
        """Update OpenGL lighting based on parameters"""
        # Store light parameters
        self.light_params = light_params

        # Make sure we're in a valid OpenGL context
        self.makeCurrent()

        # Update OpenGL light settings
        for i, light in enumerate(light_params):
            # OpenGL typically supports 8 lights (0-7)
            if i >= 8:
                print(f"Warning: Exceeded maximum number of OpenGL lights (8)")
                break

            # Map light index to OpenGL light constant
            light_constants = [GL_LIGHT0, GL_LIGHT1, GL_LIGHT2, GL_LIGHT3,
                               GL_LIGHT4, GL_LIGHT5, GL_LIGHT6, GL_LIGHT7]

            try:
                light_id = light_constants[i]
                if light['enabled']:
                    glEnable(light_id)
                    glLightfv(light_id, GL_POSITION, light['position'])
                    glLightfv(light_id, GL_AMBIENT, light['ambient'])
                    glLightfv(light_id, GL_DIFFUSE, light['diffuse'])
                    glLightfv(light_id, GL_SPECULAR, light['specular'])

                    # Add light attenuation for more realism
                    # Only apply to positional lights (w=1)
                    if light['position'][3] == 1.0:
                        glLightf(light_id, GL_CONSTANT_ATTENUATION, 1.0)
                        glLightf(light_id, GL_LINEAR_ATTENUATION, 0.0)
                        quad_atten = light.get('quadratic_attenuation', 0.0005)
                        glLightf(light_id, GL_QUADRATIC_ATTENUATION, quad_atten)

                    # Spotlight parameters (optional)
                    spot_cutoff = light.get('spot_cutoff', 180.0)
                    glLightf(light_id, GL_SPOT_CUTOFF, spot_cutoff)
                    if spot_cutoff < 180.0:
                        glLightfv(light_id, GL_SPOT_DIRECTION, light.get('spot_direction', [0.0, -1.0, 0.0]))
                        glLightf(light_id, GL_SPOT_EXPONENT, light.get('spot_exponent', 0.0))
                else:
                    glDisable(light_id)
            except Exception as e:
                print(f"Error setting light {i}: {e}")

        # Request a redraw
        self.update()

    def set_night_mode(self, enabled):
        """Toggle between day and night lighting modes.

        Night mode converts lights 0-3 into spotlights aimed at the field,
        disables the fill light, dims the ambient light to moonlight levels,
        and switches the sky shader to a dark starry sky.
        """
        import math as _math
        self.makeCurrent()

        if enabled:
            # Save day params for restoration (deep copy)
            self._day_light_params = []
            for lp in self.light_params:
                self._day_light_params.append({k: (v.copy() if isinstance(v, list) else v) for k, v in lp.items()})

            # Field center in model coords (approximate center of the diamond)
            field_center = [10.0, 0.0, 10.0]

            # Configure lights 0-3 as stadium flood spotlights
            # Real stadium floods are huge banks covering the whole field — use very
            # wide cones with minimal exponent so the coverage is even, not a hotspot.
            for i in range(4):
                lp = self.light_params[i]
                lp['enabled'] = True
                # Compute spot direction: from light position toward field center
                dx = field_center[0] - lp['position'][0]
                dy = field_center[1] - lp['position'][1]
                dz = field_center[2] - lp['position'][2]
                mag = _math.sqrt(dx*dx + dy*dy + dz*dz)
                if mag > 0.001:
                    lp['spot_direction'] = [dx/mag, dy/mag, dz/mag]
                else:
                    lp['spot_direction'] = [0.0, -1.0, 0.0]
                lp['spot_cutoff'] = 80.0           # very wide flood cone
                lp['spot_exponent'] = 1.5           # nearly flat — no visible hotspot
                lp['diffuse'] = [1.1, 1.1, 1.05, 1.0]  # neutral white, slight warm tint
                lp['ambient'] = [0.03, 0.03, 0.03, 1.0]  # tiny per-light ambient fill
                lp['specular'] = [0.9, 0.9, 0.85, 1.0]
                lp['quadratic_attenuation'] = 0.00008  # very low — lights are far away

            # Light 4 (fill): keep enabled but dim — simulates scatter/bounce light
            # that fills the whole field evenly (what the banks of floods create in sum)
            self.light_params[4]['enabled'] = True
            self.light_params[4]['diffuse'] = [0.18, 0.18, 0.20, 1.0]
            self.light_params[4]['ambient'] = [0.05, 0.05, 0.07, 1.0]
            self.light_params[4]['specular'] = [0.0, 0.0, 0.0, 1.0]

            # Light 5 (directional ambient): dim blue-ish moonlight/skylight
            self.light_params[5]['enabled'] = True
            self.light_params[5]['diffuse'] = [0.06, 0.06, 0.10, 1.0]
            self.light_params[5]['ambient'] = [0.03, 0.03, 0.05, 1.0]
            self.light_params[5]['specular'] = [0.0, 0.0, 0.0, 1.0]

            # Apply the modified params
            self.set_lighting(self.light_params)

            # Global ambient: low but not pitch-black — simulates ambient sky scatter
            glLightModelfv(GL_LIGHT_MODEL_AMBIENT, [0.06, 0.06, 0.08, 1.0])

            # Dark navy clear color
            glClearColor(0.01, 0.01, 0.03, 1.0)

            self._night_mode = True

        else:
            # Restore day params
            if hasattr(self, '_day_light_params') and self._day_light_params:
                self.light_params = self._day_light_params
                self.set_lighting(self.light_params)

                # Reset spotlight cutoff to 180 (point light) on lights 0-3
                light_constants = [GL_LIGHT0, GL_LIGHT1, GL_LIGHT2, GL_LIGHT3]
                for light_id in light_constants:
                    glLightf(light_id, GL_SPOT_CUTOFF, 180.0)

            # Restore day global ambient
            glLightModelfv(GL_LIGHT_MODEL_AMBIENT, [0.2, 0.2, 0.25, 1.0])

            # Restore sky blue clear color
            glClearColor(0.529, 0.808, 0.922, 1.0)

            self._night_mode = False

        self.update()

    def draw_light_sources(self, show_lights):
        """Draw spheres to visualize light positions"""
        if not show_lights or not hasattr(self, 'light_params'):
            return

        # Save current material and lighting state
        glPushAttrib(GL_LIGHTING_BIT | GL_CURRENT_BIT | GL_ENABLE_BIT)

        # Temporarily disable lighting for the light source indicators
        glDisable(GL_LIGHTING)

        for i, light in enumerate(self.light_params):
            if not light['enabled']:
                continue

            # Extract position
            x, y, z = light['position'][0:3]

            # Use light's own color for the sphere, but make it brighter
            r = min(1.0, light['diffuse'][0] * 1.5)
            g = min(1.0, light['diffuse'][1] * 1.5)
            b = min(1.0, light['diffuse'][2] * 1.5)

            # Draw a larger sphere to represent the light
            glPushMatrix()
            glTranslatef(x, y, z)

            # Draw sphere with flat shading for better visibility
            glColor4f(r, g, b, 0.8)
            sphere = gluNewQuadric()
            gluQuadricDrawStyle(sphere, GLU_FILL)
            gluSphere(sphere, 0.5, 16, 16)  # Larger sphere (0.5 instead of 0.3)
            gluDeleteQuadric(sphere)

            # Draw coordinate axes to show light position better
            # X axis (red)
            glBegin(GL_LINES)
            glColor3f(1.0, 0.0, 0.0)
            glVertex3f(0, 0, 0)
            glVertex3f(1.0, 0, 0)

            # Y axis (green)
            glColor3f(0.0, 1.0, 0.0)
            glVertex3f(0, 0, 0)
            glVertex3f(0, 1.0, 0)

            # Z axis (blue)
            glColor3f(0.0, 0.0, 1.0)
            glVertex3f(0, 0, 0)
            glVertex3f(0, 0, 1.0)
            glEnd()

            # Draw text label with light number
            glRasterPos3f(0.6, 0.6, 0.6)

            glutInit()
            for C in f"Light {i}":# noinspection PyUnresolvedReferences
                glutBitmapCharacter(GLUT_BITMAP_TIMES_ROMAN_24, ord(C))

            glPopMatrix()

        # Re-enable lighting
        glEnable(GL_LIGHTING)

        # Restore previous state
        glPopAttrib()




# Weather animation widget and particle classes live in weatherman.py




class SplitView(QWidget):
    """Widget that contains both top-down and umpire views"""

    # Signal used to safely deliver weather data from the background thread
    # to the main thread. Qt signals are inherently thread-safe.
    _weather_ready  = pyqtSignal(dict)
    _weather_failed = pyqtSignal(str)

    def __init__(self, stadium_image_path, lat, lon, altitude, parent=None, api_key=open_weather_key):
        super().__init__(parent)

        # Connect thread-safe weather signals to their handlers on the main thread
        self._weather_ready.connect(self._apply_weather)
        self._weather_failed.connect(self._on_weather_error)

        self.weather_service = WeatherService(api_key)

        # Initialize physics simulator
        self.ball_simulator = BallFlightSimulator()

        # Stadium and location information
        self.stadium_image_path = stadium_image_path  # deferred — loaded only if needed
        self.lat = lat
        self.lon = lon
        self.altitude = altitude
        self.dimensions = None
        self.stadium_name = ""

        # Weather and simulation data
        self.weather_data = None
        self.trajectory_data = None
        self.current_frame = 0

        # ML residual correction toggle (driven by the ML correction button)
        self.ml_correction_enabled = True
        self._ml_residual_ft = None
        self._ml_actual_ft = None

        # Pitch animation state
        self.pitch_trajectory_data = None
        self.animation_phase = "idle"  # "idle" | "pitch" | "flight"
        self.pitch_slowdown = 1.0      # slowdown multiplier for pitch phase (1.0 = real speed)
        self.pitch_frame_accumulator = 0.0
        self.pending_bbe_record = None  # stashed BBE record to fire after pitch




        # Setup UI
        self.setup_ui()

        # Animation timer
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.update_animation)

        # Seed default weather so the sim is usable immediately.
        # The actual API fetch is triggered by change_stadium() after the
        # event loop starts (via QTimer.singleShot in MLBWeatherApp.__init__),
        # so we don't fire a duplicate request here.
        self.weather_data = dict(self._DEFAULT_WEATHER)
        self.update_weather_label()
        self.update_weather_visualization()






    # In SplitView class
    def update_stadium(self, stadium_name):
        """Update the stadium when selection changes"""
        if stadium_name in STADIUM_DATA:
            # Update stadium properties
            self.dimensions = STADIUM_DATA[stadium_name]["dimensions"]
            self.stadium_name = stadium_name

            # IMPORTANT: Update the latitude and longitude
            self.lat = STADIUM_DATA[stadium_name]["lat"]
            self.lon = STADIUM_DATA[stadium_name]["lon"]
            self.altitude = STADIUM_DATA[stadium_name]["altitude"]

            print(f"Updated stadium coordinates: lat={self.lat}, lon={self.lon}")

            # Update the stadium info label
            stadium_info = f"{stadium_name}\nAlt: {self.altitude} ft"
            self.info_label.setText(stadium_info)
            self.info_label.adjustSize()
            self.info_label.move(self.stadium_view.width() - self.info_label.width() - 8, 8)

            # 2D View - now using polar coordinates directly
            self.stadium_view.draw_stadium_polar(stadium_name, self.dimensions)

            # 3D View — pass stadium name, invalidate outfield cache, repaint
            self.umpire_view._stadium_name = stadium_name
            self.umpire_view._outfield_display_list = None
            self.umpire_view.update()
        else:
            print(f"Stadium {stadium_name} not found in STADIUM_DATA")

    def setup_ui(self):
        """Set up the split view UI layout"""
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        # Create top section with only the wind vector widget taking full width
        top_layout = QVBoxLayout()
        top_layout.setSpacing(0)
        top_layout.setContentsMargins(0, 0, 0, 0)

        # Add the wind vector widget taking full width
        self.wind_vector_widget = WindVectorWidget()
        top_layout.addWidget(self.wind_vector_widget)

        # Create a horizontal layout for organized stats display
        stats_layout = QHBoxLayout()
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(0)

        # Stadium selector (compact, inside stats bar)
        stadium_combo_label = QLabel("Stadium:")
        stadium_combo_label.setStyleSheet("color: #ccc; font-size: 11px; padding: 0 2px;")
        stats_layout.addWidget(stadium_combo_label, 0)
        self.stadium_combo = QComboBox()
        self.stadium_combo.addItems(STADIUM_DATA.keys())
        self.stadium_combo.setMinimumWidth(180)
        self.stadium_combo.setStyleSheet(
            "background: rgba(0,0,0,120); color: white; border: 1px solid #444; "
            "padding: 2px 6px; font-size: 11px;"
        )
        stats_layout.addWidget(self.stadium_combo, 0)

        # Weather info panel
        self.weather_label = QLabel("Weather data: Not loaded")
        self.weather_label.setStyleSheet("color: white; background-color: rgba(0, 0, 0, 120); padding: 8px; margin: 2px;")
        stats_layout.addWidget(self.weather_label, 1)

        # Flight stats panel
        self.flight_info_label = QLabel("Flight data: No simulation")
        self.flight_info_label.setStyleSheet("color: white; background-color: rgba(0, 0, 100, 120); padding: 8px; margin: 2px;")
        stats_layout.addWidget(self.flight_info_label, 1)

        top_layout.addLayout(stats_layout)

        self.layout.addLayout(top_layout)

        # Main views container - side by side
        views_layout = QHBoxLayout()
        views_layout.setSpacing(0)
        views_layout.setContentsMargins(0, 0, 0, 0)

        # Container for top-down stadium view with overlay elements
        stadium_view_container = QWidget()
        stadium_view_layout = QVBoxLayout(stadium_view_container)
        stadium_view_layout.setContentsMargins(0, 0, 0, 0)

        # Top-down stadium view
        self.stadium_view = StadiumView()
        self.stadium_view.setMinimumSize(300, 300)
        stadium_view_layout.addWidget(self.stadium_view)

        # Stadium info label positioned at top-right of stadium view
        self.info_label = QLabel("Stadium Info")
        self.info_label.setStyleSheet("color: white; background-color: rgba(0, 0, 0, 120); padding: 5px;")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.info_label.setMinimumSize(160, 45)
        self.info_label.setMaximumWidth(220)
        self.info_label.setWordWrap(True)
        self.info_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.info_label.setParent(self.stadium_view)
        self.info_label.adjustSize()
        self.info_label.move(self.stadium_view.width() - self.info_label.width() - 8, 8)
        self.info_label.show()

        # Connect resize event to reposition the label
        self.stadium_view.resizeEvent = lambda event: (
            self.info_label.move(self.stadium_view.width() - self.info_label.width() - 8, 8),
            type(self.stadium_view).resizeEvent(self.stadium_view, event)
        )

        # Add flight stats list widget positioned at bottom right of stadium view
        self.flight_stats_list = QListWidget(self.stadium_view)
        self.flight_stats_list.setMinimumWidth(400)
        self.flight_stats_list.setMaximumHeight(150)
        self.flight_stats_list.setStyleSheet("background-color: rgba(0, 0, 0, 150); color: white;")
        self.flight_stats_list.move(
            self.stadium_view.width() - 420,
            self.stadium_view.height() - 160
        )
        self.flight_stats_list.hide()  # Hidden by default, show with flight history

        # Update flight_stats_list position when stadium_view is resized
        original_resize_event = self.stadium_view.resizeEvent

        def new_resize_event(event):
            original_resize_event(event)
            self.flight_stats_list.move(
                self.stadium_view.width() - 420,
                self.stadium_view.height() - 160
            )
        self.stadium_view.resizeEvent = new_resize_event

        views_layout.addWidget(stadium_view_container, 30)

        # 3D umpire view on the right
        self.umpire_view = UmpireView3D()
        self.umpire_view.setMinimumSize(600, 300)
        views_layout.addWidget(self.umpire_view, 70)

        # ---- Player BBE sidebar (inline, next to 3D view) ----
        import os
        self.player_overlay = PlayerBBEOverlay()
        self.player_overlay.simulate_bbe.connect(self._on_simulate_bbe)
        self.player_overlay.player_selected_with_data.connect(self._on_player_spray)
        self.player_overlay.play_spray.connect(self._on_play_spray)
        self.player_overlay.clear_spray.connect(self._clear_spray)
        views_layout.addWidget(self.player_overlay, 0)

        # Spray chart animation timer
        from PyQt6.QtCore import QTimer
        self._spray_timer = QTimer()
        self._spray_timer.timeout.connect(self._spray_tick)
        self._spray_queue: list = []
        # Auto-detect the most recent season with BBE data on disk
        import glob as _glob, re as _re
        _years = sorted({
            int(m.group(1))
            for f in _glob.glob("savant_bbe_*.csv")
            if (m := _re.search(r"savant_bbe_(\d{4})\.csv$", f))
        }) or [2024]
        _latest = _years[-1]
        bbe_path = os.environ.get('BBE_CSV', f'savant_bbe_{_latest}.csv')
        lb_path  = os.environ.get('LB_CSV',  f'savant_lb_{_latest}.csv')
        lb_path  = lb_path if os.path.exists(lb_path) else None
        self.player_overlay.load_data(bbe_path, lb_path, year=_latest)

        self.layout.addLayout(views_layout)


    def update_starting_position(self):
        """Update the visual indicator when position controls change"""
        if hasattr(self, 'x_pos_spin') and hasattr(self, 'stadium_view'):
            x = self.x_pos_spin.value()
            y = self.y_pos_spin.value()
            z = self.z_pos_spin.value()

            # 2D view always anchors the start indicator at home plate
            self.stadium_view.draw_starting_position(0, 0, 0)

            # Update the 3D view starting position
            # Convert feet to the 3D view's units (approximately meters)
            x_3d = x / 3.28084
            y_3d = y / 3.28084
            z_3d = z / 3.28084

            # Set the starting position in the 3D view
            if hasattr(self, 'umpire_view'):
                self.umpire_view.set_start_position(x_3d, y_3d, z_3d)


    # ---- Default weather so the sim is usable before the API responds ---- #
    _DEFAULT_WEATHER = {
        "wind_speed": 8,
        "wind_direction": 180,
        "temperature": 72,
        "humidity": 50,
        "pressure_hpa": 1013.25,
        "pressure_pa": 101325.0,
        "condition": "Clear",
        "description": "loading…",
        "precipitation": 0,
        # 180 here means "out to centre", not "from the south" — this seed is
        # a pleasant default to sim against, not an observation.
        "wind_frame": "field",
    }

    def fetch_weather_data(self):
        """Fetch weather in a background thread so the UI is never blocked."""
        # If first call, seed with defaults so the sim works immediately
        if self.weather_data is None:
            self.weather_data = dict(self._DEFAULT_WEATHER)
            self.update_weather_label()
            self.update_weather_visualization()

        current_text = self.weather_label.text().removesuffix(" (updating…)")
        self.weather_label.setText(current_text + " (updating…)")

        import threading
        lat, lon = self.lat, self.lon
        svc = self.weather_service

        def _fetch():
            try:
                print(f"[weather] fetching for lat={lat}, lon={lon}")
                weather_json = svc.get_weather_by_location(lat, lon)
                data = svc.extract_weather_data(weather_json)
                # Emit signal — crosses thread boundary safely via Qt's queued connection
                self._weather_ready.emit(data)
            except Exception as e:
                print(f"[weather] error: {e}")
                self._weather_failed.emit(str(e))

        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_weather(self, data):
        """Called on main thread when background weather fetch completes."""
        self.weather_data = data
        print(f"[weather] ready: {data.get('description', '')}  {data.get('temperature', '')}°F")
        self.update_weather_label()
        self.update_weather_visualization()

    def _on_weather_error(self, error_msg):
        """Called on main thread when the background weather fetch fails."""
        current_text = self.weather_label.text().removesuffix(" (updating…)")
        self.weather_label.setText(current_text + f" (update failed: {error_msg})")

    def _wind_azimuth(self):
        """Park azimuth to hand BallFlightSimulator, or None when the wind
        angle we hold is already a field angle.

        weather_data carries a `wind_frame` tag because two different things
        land in the same `wind_direction` slot: a forecast bearing from true
        north, and a hand-dialled "blowing out to centre" angle from the
        drawer.  Rotating the second one would move the manual control's
        meaning under the user, so only feed data gets rotated.
        """
        if not self.weather_data:
            return None
        if self.weather_data.get("wind_frame", "compass") != "compass":
            return None
        return get_park_azimuth(self.stadium_name)

    def set_custom_weather(self, wind_speed, wind_direction):
        """Set custom weather data for simulation.

        The drawer's dial is field-relative — 0 blows in from centre, 180 out
        to centre — and stays that way regardless of which park is loaded.
        """
        self.weather_data = {
            "wind_speed": wind_speed,
            "wind_direction": wind_direction,
            "wind_frame": "field",
            "temperature": 75,  # Default temperature
            "humidity": 50,     # Default humidity
            "pressure_hpa": 1013.25,          # Standard atmosphere
            "pressure_pa":  101325.0,
            "condition": "Custom",
            "description": "custom weather settings",
            "precipitation": 0,
        }
        self.update_weather_label()
        self.update_weather_visualization()

    def update_weather_label(self):
        """Update the weather information display"""
        if self.weather_data:
            wind_info  = f"Wind: {self.weather_data['wind_speed']} mph at {self.weather_data['wind_direction']}°"
            temp_info  = f"Temp: {self.weather_data['temperature']}°F"
            cond_info  = f"Conditions: {self.weather_data['description']}"
            press_hpa  = self.weather_data.get("pressure_hpa", None)
            press_info = f"Pressure: {press_hpa:.1f} hPa" if press_hpa is not None else ""
            parts = [wind_info, temp_info, cond_info]
            if press_info:
                parts.append(press_info)
            self.weather_label.setText(" | ".join(parts))

    def update_weather_visualization(self):
        """Update the visual representation of weather conditions"""
        if self.weather_data:
            self.wind_vector_widget.set_wind_data(
                self.weather_data["wind_speed"],
                self.weather_data["wind_direction"],
                self.weather_data.get("condition", "Clear"),
                self.weather_data.get("description", "clear sky")
            )

    def _on_player_spray(self, player_id: int, events):
        """Show or clear spray chart dots when a player is selected/deselected."""
        self._spray_timer.stop()
        self._spray_queue.clear()
        if not events or player_id == 0:
            self.stadium_view.clear_spray_chart()
            self.umpire_view.clear_spray_chart()
            self.umpire_view.set_spray_camera(False)
        else:
            self.stadium_view.set_spray_chart(events)
            self.umpire_view.set_spray_chart(events)
            self.umpire_view.set_spray_camera(True)

    def _on_play_spray(self, events, include_fouls: bool):
        """Animate spray chart: compute each BBE's trajectory and draw trails."""
        self._spray_timer.stop()
        self.animation_timer.stop()
        self.stadium_view.clear_spray_chart()
        self.stadium_view.clear_trails()
        self.umpire_view.clear_spray_chart()
        self.umpire_view.set_spray_camera(True)

        # Filter to events with valid launch data and spray coords
        queue = []
        for ev in events:
            hc_x = ev.get("hc_x")
            hc_y = ev.get("hc_y")
            ev_speed = ev.get("launch_speed")
            la = ev.get("launch_angle")
            if hc_x is None or hc_y is None or ev_speed is None or la is None:
                continue
            try:
                hc_x, hc_y = float(hc_x), float(hc_y)
                ev_speed, la = float(ev_speed), float(la)
            except (ValueError, TypeError):
                continue
            if any(math.isnan(v) for v in (hc_x, hc_y, ev_speed, la)):
                continue
            if not include_fouls and ev.get("events", "") == "foul":
                continue
            queue.append(ev)

        queue.sort(key=lambda e: e.get("game_date", ""))

        if not queue:
            return

        self._spray_queue = queue
        interval_ms = max(10, min(80, int(8000 / max(len(queue), 1))))
        self._spray_timer.start(interval_ms)

    def _spray_tick(self):
        """Pop one BBE, compute its trajectory, and draw trail on 2D + dot on 3D."""
        if not self._spray_queue:
            self._spray_timer.stop()
            return

        ev = self._spray_queue.pop(0)
        hc_x = float(ev["hc_x"])
        hc_y = float(ev["hc_y"])
        ev_speed = float(ev["launch_speed"])
        la = float(ev["launch_angle"])

        # Derive horizontal launch angle from Savant spray coords
        # Savant: hc_x increases toward right field, hc_y decreases toward CF
        SAVANT_HP_X, SAVANT_HP_Y = 125.42, 198.27
        dx = hc_x - SAVANT_HP_X   # + toward right field
        dy = SAVANT_HP_Y - hc_y    # + toward center field
        # atan2(dx, dy) gives angle from CF toward RF (positive = RF side)
        hla_deg = math.degrees(math.atan2(dx, dy))

        # Estimate batted ball spin from launch angle (not pitch spin)
        spin = self._estimate_batted_ball_spin(la)

        if not self.weather_data:
            return

        # Compute trajectory without animating
        self.ball_simulator.omega = spin
        start_x = self.x_pos_spin.value() / 3.28084
        start_y = self.y_pos_spin.value() / 3.28084
        start_z = self.z_pos_spin.value() / 3.28084

        traj = self.ball_simulator.calculate_trajectory(
            ev_speed, la, hla_deg,
            self.weather_data["wind_speed"],
            self.weather_data["wind_direction"],
            self.weather_data["temperature"],
            self.weather_data["humidity"],
            self.altitude,
            start_x, start_y, start_z,
            pressure_pa=self.weather_data.get("pressure_pa", None),
            pressure_is_station=self.weather_data.get("pressure_frame") == "station",
            park_azimuth=self._wind_azimuth(),
        )

        # Classify and pick trail color
        hit_result = self.classify_hit_result(traj)
        _TRAIL_RGB = {
            "HOME RUN":     (1.0, 0.84, 0.0),
            "OFF THE WALL": (1.0, 0.55, 0.0),
            "WARNING TRACK":(1.0, 0.78, 0.24),
            "IN PLAY":      (0.63, 0.86, 0.39),
            "FOUL BALL":    (0.7, 0.7, 0.7),
        }
        tr, tg, tb = _TRAIL_RGB.get(hit_result, (1.0, 0.55, 0.0))

        # Draw persistent trail on 2D view (no ball animation)
        self.stadium_view.start_ball_trajectory(traj, hit_result=hit_result)
        self.stadium_view.ball_layer.setVisible(False)

        # Draw persistent trail on 3D view
        self.umpire_view.add_spray_trail(traj, tr, tg, tb)

    def _clear_spray(self):
        """Clear all spray chart dots and trails from both views."""
        self._spray_timer.stop()
        self._spray_queue.clear()
        self.stadium_view.clear_spray_chart()
        self.stadium_view.clear_trails()
        self.umpire_view.clear_spray_chart()
        self.umpire_view.set_spray_camera(False)

    def _on_simulate_bbe(self, record: dict):
        """Called when user clicks a BBE row in the overlay.  Fires the sim
        with real EV, LA, and horizontal angle derived from spray coordinates."""
        try:
            ev = float(record.get("launch_speed", 0))
            la = float(record.get("launch_angle", 0))
        except (ValueError, TypeError):
            return

        # If not already in flight phase, try to play pitch animation first
        if self.animation_phase != "flight":
            pitch_traj = savant_row_to_pitch_trajectory(record)
            if pitch_traj is not None:
                self.pitch_trajectory_data = pitch_traj
                self.pending_bbe_record = record
                self._start_pitch_animation(record)
                return  # don't fire batted ball yet — pitch plays first

        # Derive horizontal launch angle from Savant spray coords if available
        hc_x = record.get("hc_x")
        hc_y = record.get("hc_y")
        hla = 0.0  # default: straight-away center
        if hc_x is not None and hc_y is not None:
            try:
                hc_x, hc_y = float(hc_x), float(hc_y)
                if not (math.isnan(hc_x) or math.isnan(hc_y)):
                    dx = hc_x - 125.42   # + toward right field
                    dy = 198.27 - hc_y   # + toward center field
                    hla = math.degrees(math.atan2(dx, dy))
            except (ValueError, TypeError):
                pass

        # Estimate batted ball spin from launch angle
        # (pitch spin ≠ batted ball spin; Savant doesn't publish batted ball spin)
        spin_rate = self._estimate_batted_ball_spin(la)

        # ML residual: park feature uses the BBE's actual home park when known,
        # falling back to the currently loaded stadium so altitude in physics
        # lines up with what the model saw at training time.
        if self.ml_correction_enabled:
            park = get_park_for_team(record.get("home_team")) or self.stadium_name
            self._ml_residual_ft = predict_distance_residual(record, park)
        else:
            self._ml_residual_ft = None
        actual = record.get("hit_distance_sc")
        try:
            self._ml_actual_ft = float(actual) if actual is not None else None
        except (TypeError, ValueError):
            self._ml_actual_ft = None

        self.simulate_ball_flight(
            exit_velocity=int(round(ev)),
            vlaunch_angle=int(round(la)),
            hlaunch_angle=round(hla, 1),
            spin_rate=spin_rate,
        )

    @staticmethod
    def _estimate_batted_ball_spin(launch_angle: float) -> int:
        """Estimate batted ball backspin (rpm) from vertical launch angle.

        Fly balls carry more backspin; grounders have topspin/less backspin.
        This is a rough heuristic — Savant doesn't publish per-event batted ball spin.
        """
        if launch_angle >= 30:
            return 2500     # high fly balls — strong backspin
        elif launch_angle >= 20:
            return 2100     # medium fly balls
        elif launch_angle >= 10:
            return 1800     # line drives
        elif launch_angle >= 0:
            return 1200     # low liners / soft grounders
        else:
            return 700      # topped grounders — topspin dominant

    def _start_pitch_animation(self, record: dict):
        """Begin pitch phase animation — ball flies from mound to plate in 3D only."""
        self.animation_timer.stop()
        self.animation_phase = "pitch"
        self.current_frame = 0
        self.pitch_frame_accumulator = 0.0

        # Clear 3D ball state
        self.umpire_view.clear_ball()
        self.umpire_view.ball_trail = []

        # Anchor pitch trajectory so the plate-crossing ends at the bat position.
        # physics_to_model() in _update_pitch_animation handles the 45° rotation,
        # scale, and translation to model coordinates.
        traj = self.pitch_trajectory_data
        bat_x = self.x_pos_spin.value()  # feet — batted ball start X (toward CF)
        bat_y = self.y_pos_spin.value()  # feet — batted ball start Y (height)
        bat_z = self.z_pos_spin.value()  # feet — batted ball start Z (1B/3B)

        # Offset so the last trajectory point lands at the bat position (physics space)
        offset_x = bat_x - traj["x"][-1]
        offset_y = bat_y - traj["y"][-1]
        offset_z = bat_z - traj["z"][-1]
        traj["x"] = traj["x"] + offset_x
        traj["y"] = traj["y"] + offset_y
        traj["z"] = traj["z"] + offset_z

        # Set pitch trail color and tighter trail spacing
        pitch_name = record.get("pitch_name", "")
        self.umpire_view.pitch_trail_color = PITCH_TRAIL_COLORS.get(pitch_name, (0.7, 0.7, 1.0))
        self.umpire_view.trail_min_dist = 0.03

        # Update info label with pitch info
        velo = record.get("release_speed", 0)
        spin = record.get("release_spin_rate", 0)
        pfx_x = record.get("pfx_x", 0) or 0
        pfx_z = record.get("pfx_z", 0) or 0
        self.flight_info_label.setText(
            f"Pitch: {pitch_name} | {velo:.1f} mph | Spin: {spin} rpm | "
            f"HB: {pfx_x:.1f}\" | IVB: {pfx_z:.1f}\""
        )

        # Start at 60fps for smooth pitch rendering
        self.animation_timer.start(16)

    def _update_pitch_animation(self):
        """Advance pitch animation by one tick (called at ~60fps)."""
        traj = self.pitch_trajectory_data
        if traj is None:
            self.animation_timer.stop()
            self.animation_phase = "idle"
            return

        n = len(traj["time"])
        plate_time = traj.get("plate_time", traj["time"][-1])

        # Fractional frame stepping: how many trajectory frames per timer tick
        # At 60fps with slowdown, we want the pitch to take plate_time * slowdown seconds
        frames_per_tick = n / (plate_time * 60.0 * self.pitch_slowdown)
        self.pitch_frame_accumulator += frames_per_tick

        while self.pitch_frame_accumulator >= 1.0 and self.current_frame < n:
            self.pitch_frame_accumulator -= 1.0
            self.current_frame += 1

        if self.current_frame >= n:
            # Pitch phase complete — fire the batted ball
            self.animation_timer.stop()
            self.umpire_view.pitch_trail_color = None
            self.umpire_view.trail_min_dist = 0.1
            self.umpire_view.ball_trail = []  # clear pitch trail before batted ball

            # Fire batted ball from stashed record
            record = self.pending_bbe_record
            self.pending_bbe_record = None
            self.pitch_trajectory_data = None
            if record is not None:
                self.animation_phase = "flight"  # skip pitch phase on re-entry
                self._on_simulate_bbe(record)
            else:
                self.animation_phase = "idle"
            return

        frame = min(self.current_frame, n - 1)

        # Convert feet → meters then to model coordinates
        x_m = traj["x"][frame] / 3.28084
        y_m = traj["y"][frame] / 3.28084
        z_m = traj["z"][frame] / 3.28084
        vx = traj["vx"][frame] / 3.28084
        vy = traj["vy"][frame] / 3.28084
        vz = traj["vz"][frame] / 3.28084

        self.umpire_view.ball_vel = (vx, vy, vz)
        self.umpire_view.ball_pos = self.umpire_view.physics_to_model(x_m, y_m, z_m)
        self.umpire_view.update()

    def simulate_ball_flight(self, exit_velocity, vlaunch_angle, hlaunch_angle, spin_rate=1800):
        """Simulate ball flight with current weather conditions and custom starting position"""
        if not self.weather_data:
            print("Weather data not available")
            return

        # Update spin rate
        self.ball_simulator.omega = spin_rate

        # Get starting position in meters (convert from feet)
        start_x = self.x_pos_spin.value() / 3.28084
        start_y = self.y_pos_spin.value() / 3.28084
        start_z = self.z_pos_spin.value() / 3.28084

        # Calculate trajectory with starting position
        self.trajectory_data = self.ball_simulator.calculate_trajectory(
            exit_velocity,
            vlaunch_angle,
            hlaunch_angle,
            self.weather_data["wind_speed"],
            self.weather_data["wind_direction"],
            self.weather_data["temperature"],
            self.weather_data["humidity"],
            self.altitude,
            start_x,
            start_y,
            start_z,
            pressure_pa=self.weather_data.get("pressure_pa", None),
            pressure_is_station=self.weather_data.get("pressure_frame") == "station",
            park_azimuth=self._wind_azimuth(),
        )

        if LOG_BALL_PHYSICS:
            # Generate physics log
            log_filename = f"ball_physics_ev{exit_velocity}_vla{vlaunch_angle}_hla{hlaunch_angle}_sr{spin_rate}.csv"
            self.ball_simulator.log_trajectory_physics(self.trajectory_data, log_filename)

            # Add log message to flight stats
            self.flight_stats_list.addItem(f"Physics log written to: {log_filename}")

        # Log trajectory data for debugging
        print(f"Starting point: ({self.trajectory_data['start_x']:.1f}, {self.trajectory_data['start_y']:.1f}, {self.trajectory_data['start_z']:.1f})")
        print(f"Ball will travel: {self.trajectory_data['distance']:.1f} feet")

        if LOG_BALL_PHYSICS: print_physics_summary(self.trajectory_data);

        # Initialize ball visualization in top-down view
        # Classify result first so trail gets correct color immediately
        hit_result = self.classify_hit_result(self.trajectory_data)

        success = self.stadium_view.start_ball_trajectory(self.trajectory_data, hit_result=hit_result)
        if not success:
            print("Warning: Failed to visualize trajectory in 2D view")

        # Clear any existing ball in umpire view
        self.umpire_view.clear_ball()

        # Start animation
        self.current_frame = 0

        # Calculate stats
        distance = self.trajectory_data["distance"]
        max_height = max(self.trajectory_data["y"])

        # Create stats text
        stats_text = f"Exit Vel: {exit_velocity} mph | Launch: {vlaunch_angle}°/{hlaunch_angle}° | Spin: {spin_rate} rpm | Dist: {distance:.1f} ft | Height: {max_height:.1f} ft | {hit_result}"

        # Add to flight stats list
        self.flight_stats_list.addItem(stats_text)
        self.flight_stats_list.scrollToBottom()

        # Update the flight info label — show ML-corrected distance alongside
        # physics whenever a residual prediction was stashed by _on_simulate_bbe.
        ml_residual = getattr(self, "_ml_residual_ft", None)
        ml_actual = getattr(self, "_ml_actual_ft", None)
        if ml_residual is not None:
            ml_dist = distance + ml_residual
            dist_str = f"Phys: {distance:.1f} ft | ML: {ml_dist:.1f} ft (Δ {ml_residual:+.1f})"
            if ml_actual is not None:
                dist_str += f" | Actual: {ml_actual:.0f} ft"
        else:
            dist_str = f"Distance: {distance:.1f} ft"
        self.flight_info_label.setText(
            f"{dist_str} | Max Height: {max_height:.1f} ft | "
            f"Exit Vel: {exit_velocity} mph | Launch: {vlaunch_angle}°/{hlaunch_angle}° | Spin: {spin_rate} rpm"
            f" | {hit_result}"
        )
        # One-shot: clear so subsequent manual sims don't reuse stale ML data
        self._ml_residual_ft = None
        self._ml_actual_ft = None

        # Start animation timer
        self.animation_phase = "flight"
        self.animation_timer.start(30)  # 30ms per frame (~33fps)

    def update_animation(self):
        """Update animation frame — dispatches to pitch or flight phase."""
        if self.animation_phase == "pitch":
            self._update_pitch_animation()
            return

        # Flight phase (existing batted ball animation)
        if not self.trajectory_data:
            return

        self.current_frame += 1

        if self.current_frame >= len(self.trajectory_data["x"]):
            self.animation_timer.stop()
            self.current_frame = 0
            self.animation_phase = "idle"
            return

        # Update ball position in top-down view
        self.stadium_view.update_ball_position(
            self.trajectory_data,
            self.current_frame
        )

        # Update ball position in 3D umpire view
        # Convert from feet to meters then to model coordinates
        x_m = self.trajectory_data["x"][self.current_frame] / 3.28084
        y_m = self.trajectory_data["y"][self.current_frame] / 3.28084
        z_m = self.trajectory_data["z"][self.current_frame] / 3.28084

        # Get velocities for the current frame
        vx = self.trajectory_data["vx"][self.current_frame] / 3.28084  # Convert ft/s to m/s
        vy = self.trajectory_data["vy"][self.current_frame] / 3.28084
        vz = self.trajectory_data["vz"][self.current_frame] / 3.28084

        # Store velocity in the umpire view for visual effects
        self.umpire_view.ball_vel = (vx, vy, vz)

        # Apply physics-to-model coordinate transform for 3D view
        self.umpire_view.ball_pos = self.umpire_view.physics_to_model(x_m, y_m, z_m)
        self.umpire_view.update()
        self.update()

    def classify_hit_result(self, trajectory_data):
        """Classify the hit as HOME RUN, OFF THE WALL, WARNING TRACK, IN PLAY, or FOUL BALL"""
        if not self.stadium_name or not trajectory_data:
            return "IN PLAY"

        final_x = trajectory_data["x"][-1]
        final_z = trajectory_data["z"][-1]

        distance = np.sqrt(final_x**2 + final_z**2)

        # atan2(X, Z) gives angle from +Z (RF direction) counterclockwise:
        #   RF foul line ≈ 45°, dead CF = 90°, LF foul line ≈ 135°
        # Stadium polar coords use 0° = RF foul line, 90° = LF foul line,
        # so subtract 45° to convert.
        angle_rad = math.atan2(final_x, final_z)
        atan2_deg = math.degrees(angle_rad)
        if atan2_deg < 0:
            atan2_deg += 360
        stadium_angle = atan2_deg - 45.0

        # Outside fair territory (stadium polar 0°–90° = RF line to LF line)
        if stadium_angle < 0 or stadium_angle > 90:
            return "FOUL BALL"

        wall_distance = get_stadium_wall_distance(self.stadium_name, stadium_angle)
        if wall_distance is None:
            return "IN PLAY"

        warning_track_start = wall_distance - 20

        # Find the height of the ball when it reaches wall_distance
        # Walk trajectory to find the frame where horizontal distance crosses wall_distance
        height_at_wall = None
        xs = trajectory_data["x"]
        zs = trajectory_data["z"]
        ys = trajectory_data["y"]
        for i in range(1, len(xs)):
            d_prev = math.sqrt(xs[i-1]**2 + zs[i-1]**2)
            d_curr = math.sqrt(xs[i]**2 + zs[i]**2)
            if d_prev <= wall_distance <= d_curr and d_curr > d_prev:
                # Linearly interpolate height at exact wall crossing
                frac = (wall_distance - d_prev) / (d_curr - d_prev)
                height_at_wall = ys[i-1] + frac * (ys[i] - ys[i-1])
                break


        wall_height = get_stadium_wall_height(self.stadium_name, stadium_angle)
        cleared_wall_height = (height_at_wall is not None and height_at_wall >= wall_height)

        if distance >= wall_distance:
            if cleared_wall_height:
                return "HOME RUN"
            else:
                return "OFF THE WALL"
        elif distance >= warning_track_start:
            return "WARNING TRACK"
        else:
            return "IN PLAY"

    def check_if_home_run(self, trajectory_data):
        """Check if the trajectory results in a home run using precise polar coordinate data"""
        return self.classify_hit_result(trajectory_data) == "HOME RUN"

    def check_if_home_run_fallback(self, final_x, final_z, final_height):
        """Fallback homerun detection using basic dimensions"""
        if not self.dimensions:
            return False

        distance = np.sqrt(final_x**2 + final_z**2)
        horizontal_angle = np.degrees(np.arctan2(final_z, final_x))

        # Determine wall distance based on angle ranges
        if horizontal_angle < -15:
            wall_distance = self.dimensions["left_field"]
        elif -15 <= horizontal_angle < 0:
            wall_distance = self.dimensions["left_center"]
        elif 0 <= horizontal_angle < 15:
            wall_distance = self.dimensions["center_field"]
        elif 15 <= horizontal_angle < 45:
            wall_distance = self.dimensions["right_center"]
        else:
            wall_distance = self.dimensions["right_field"]

        # Convert horizontal angle to approximate stadium polar angle for wall height lookup
        # horizontal_angle here is from arctan2(z, x) in physics coords — rough mapping
        polar_approx = max(0, min(90, 45 - horizontal_angle))
        wall_ht = get_stadium_wall_height(self.stadium_name, polar_approx) if self.stadium_name else 8
        return distance >= wall_distance and final_height > wall_ht


# ==============================================
# BBE Update Worker (off-thread Savant scrape)
# Runs the incremental update from savant_bbe_fetch in a QThread so the
# Qt event loop keeps painting while the scrape progresses.
# ==============================================
class BBEUpdateWorker(QObject):
    progress = pyqtSignal(int, int)   # done, total
    status   = pyqtSignal(str)
    finished = pyqtSignal(dict)       # combined result dict
    error    = pyqtSignal(str)

    def __init__(self, year: int, jobs: list,
                 delay: float = 1.2, workers: int = 10):
        """jobs is a list of (player_type, csv_path) tuples — one update per."""
        super().__init__()
        self.year = year
        self.jobs = jobs
        self.delay = delay
        self.workers = workers

    def run(self):
        try:
            import savant_bbe_fetch
            n_jobs = len(self.jobs) or 1
            combined_results = []
            for i, (player_type, csv_path) in enumerate(self.jobs):
                self.status.emit(f"{player_type.capitalize()}s:")
                # Map this job's 0..total progress into the global bar's slice.
                base_pct = int(round(100.0 * i / n_jobs))
                slice_pct = int(round(100.0 / n_jobs))
                def _progress_cb(done, total, base=base_pct, sl=slice_pct):
                    if total <= 0:
                        return
                    pct = base + int(round(sl * done / total))
                    self.progress.emit(pct, 100)
                def _status_cb(msg, pt=player_type):
                    self.status.emit(f"{pt.capitalize()}s: {msg}")
                result = savant_bbe_fetch.update(
                    year=self.year,
                    delay=self.delay,
                    workers=self.workers,
                    output_path=csv_path,
                    player_type=player_type,
                    progress_cb=_progress_cb,
                    status_cb=_status_cb,
                )
                combined_results.append((player_type, result or {}))

            # Build a one-line summary for the HUD label
            notes = []
            for pt, r in combined_results:
                added = r.get("added")
                if added is None:
                    notes.append(f"{pt}s: full pull")
                else:
                    notes.append(f"{pt}s: +{added:,}")
            self.finished.emit({"note": " | ".join(notes), "jobs": combined_results})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error.emit(str(e))


class MLBWeatherApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MLB Weather & Ball Flight Simulator")
        self.setMinimumSize(1200, 700)
        _screen = QApplication.primaryScreen()
        if _screen:
            self.setMaximumHeight(_screen.availableGeometry().height())

        # Create central widget and layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Create split view widget with default stadium
        default_stadium = list(STADIUM_DATA.keys())[0]
        self.stadium_widget = SplitView(
            STADIUM_DATA[default_stadium]["image_path"],
            STADIUM_DATA[default_stadium]["lat"],
            STADIUM_DATA[default_stadium]["lon"],
            STADIUM_DATA[default_stadium]["altitude"]
        )
        # Set dimensions for the default stadium
        self.stadium_widget.dimensions = STADIUM_DATA[default_stadium]["dimensions"]
        self.stadium_widget.stadium_name = default_stadium

        # Connect the stadium combo (now owned by SplitView)
        self.stadium_combo = self.stadium_widget.stadium_combo
        self.stadium_combo.currentTextChanged.connect(self.change_stadium)
        # Draw the default stadium once the event loop (and OpenGL context) is ready
        QTimer.singleShot(0, lambda: self.change_stadium(default_stadium))

        # Add the split view with stretching to take most of the space
        main_layout.addWidget(self.stadium_widget, 1)  # Use stretch factor

        # ============================================================
        # Compact always-visible toolbar
        # ============================================================
        toolbar_css = """
            QWidget#CompactToolbar {
                background: #1a1a2e;
                border-top: 1px solid #333345;
                border-bottom: 1px solid #333345;
            }
            QWidget#CompactToolbar QLabel {
                color: #aaa; font-size: 10px; padding: 0 2px;
            }
            QWidget#CompactToolbar QSpinBox, QWidget#CompactToolbar QDoubleSpinBox {
                background: #22223a; color: white; border: 1px solid #444;
                padding: 2px 4px; font-size: 11px; min-width: 50px; max-width: 65px;
            }
            QWidget#CompactToolbar QPushButton {
                background: #22223a; color: white; border: 1px solid #444;
                padding: 4px 10px; font-size: 11px;
            }
            QWidget#CompactToolbar QPushButton:hover { background: #2a2a4a; }
            QWidget#CompactToolbar QPushButton#SimBtn {
                background: #1a5276; font-weight: bold;
            }
            QWidget#CompactToolbar QPushButton#SimBtn:hover { background: #21618c; }
        """
        compact_toolbar = QWidget()
        compact_toolbar.setObjectName("CompactToolbar")
        compact_toolbar.setStyleSheet(toolbar_css)
        compact_toolbar.setFixedHeight(32)
        tb_layout = QHBoxLayout(compact_toolbar)
        tb_layout.setContentsMargins(4, 2, 4, 2)
        tb_layout.setSpacing(6)

        # -- Simulate button --
        self.simulate_btn = QPushButton("Simulate")
        self.simulate_btn.setObjectName("SimBtn")
        self.simulate_btn.clicked.connect(self.simulate_flight)
        tb_layout.addWidget(self.simulate_btn)

        # -- Track Ball button --
        self.track_ball_btn = QPushButton("Track")
        def ToggleBallTracking():
            enabled_css = "QPushButton { background-color: #00FF00; color: white; border: 1px solid #444; padding: 4px 10px; font-size: 11px; }"
            disabled_css = "QPushButton { background-color: #FF0000; color: white; border: 1px solid #444; padding: 4px 10px; font-size: 11px; }"
            isEnabled = self.stadium_widget.umpire_view.toggle_ball_tracking()
            self.track_ball_btn.setStyleSheet(enabled_css if isEnabled else disabled_css)
        self.track_ball_btn.clicked.connect(ToggleBallTracking)
        tb_layout.addWidget(self.track_ball_btn)
        ToggleBallTracking(); ToggleBallTracking()  # toggle twice to set initial css

        # -- ML correction toggle --
        self.ml_correction_btn = QPushButton("ML correction")
        ml_on_css  = "QPushButton { background-color: #00FF00; color: white; border: 1px solid #444; padding: 4px 10px; font-size: 11px; }"
        ml_off_css = "QPushButton { background-color: #FF0000; color: white; border: 1px solid #444; padding: 4px 10px; font-size: 11px; }"
        def ToggleMLCorrection():
            sw = self.stadium_widget
            sw.ml_correction_enabled = not sw.ml_correction_enabled
            self.ml_correction_btn.setStyleSheet(ml_on_css if sw.ml_correction_enabled else ml_off_css)
        self.ml_correction_btn.clicked.connect(ToggleMLCorrection)
        tb_layout.addWidget(self.ml_correction_btn)
        # Sync initial CSS to the SplitView's default state (enabled).
        self.ml_correction_btn.setStyleSheet(
            ml_on_css if self.stadium_widget.ml_correction_enabled else ml_off_css
        )

        tb_layout.addWidget(self._tb_separator())

        # -- EV spinbox --
        tb_layout.addWidget(QLabel("EV"))
        self._tb_ev_spin = QSpinBox()
        self._tb_ev_spin.setRange(0, 160)
        self._tb_ev_spin.setValue(100)
        self._tb_ev_spin.setSuffix(" mph")
        tb_layout.addWidget(self._tb_ev_spin)

        # -- VLA spinbox --
        tb_layout.addWidget(QLabel("VLA"))
        self._tb_vla_spin = QSpinBox()
        self._tb_vla_spin.setRange(0, 90)
        self._tb_vla_spin.setValue(25)
        self._tb_vla_spin.setSuffix("°")
        tb_layout.addWidget(self._tb_vla_spin)

        # -- HLA spinbox --
        tb_layout.addWidget(QLabel("HLA"))
        self._tb_hla_spin = QSpinBox()
        self._tb_hla_spin.setRange(0, 90)
        self._tb_hla_spin.setValue(45)
        self._tb_hla_spin.setSuffix("°")
        tb_layout.addWidget(self._tb_hla_spin)

        # -- Spin spinbox --
        tb_layout.addWidget(QLabel("Spin"))
        self._tb_spin_spin = QSpinBox()
        self._tb_spin_spin.setRange(1000, 3000)
        self._tb_spin_spin.setValue(1800)
        self._tb_spin_spin.setSingleStep(50)
        self._tb_spin_spin.setSuffix(" rpm")
        tb_layout.addWidget(self._tb_spin_spin)

        # -- Pitch speed spinbox --
        tb_layout.addWidget(QLabel("Speed"))
        self._tb_pitch_speed_spin = QDoubleSpinBox()
        self._tb_pitch_speed_spin.setRange(0.1, 2.0)
        self._tb_pitch_speed_spin.setValue(1.0)
        self._tb_pitch_speed_spin.setSingleStep(0.1)
        self._tb_pitch_speed_spin.setDecimals(1)
        self._tb_pitch_speed_spin.setSuffix("x")
        tb_layout.addWidget(self._tb_pitch_speed_spin)

        tb_layout.addStretch()

        # -- Drawer toggle --
        self._drawer_toggle = QPushButton("▼ More")
        self._drawer_toggle.setFixedWidth(70)
        self._drawer_toggle.clicked.connect(self._toggle_drawer)
        tb_layout.addWidget(self._drawer_toggle)

        main_layout.addWidget(compact_toolbar)

        # ============================================================
        # Full controls drawer (collapsible, hidden by default)
        # ============================================================
        self._drawer_expanded = False
        controls_container = QWidget()
        controls_container.setObjectName("ControlsDrawer")
        controls_main_layout = QVBoxLayout(controls_container)
        controls_main_layout.setContentsMargins(5, 5, 5, 5)

        # Top row of controls (sliders)
        top_controls = QHBoxLayout()

        # Exit Velocity control
        ev_group = QGroupBox("Exit Velocity (mph)")
        ev_layout = QVBoxLayout()
        self.ev_slider = QSlider(Qt.Orientation.Horizontal)
        self.ev_slider.setRange(0, 160)
        self.ev_slider.setValue(100)
        self.ev_value = QLabel("100")
        self.ev_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ev_slider.valueChanged.connect(lambda v: self.ev_value.setText(str(v)))
        ev_layout.addWidget(self.ev_slider)
        ev_layout.addWidget(self.ev_value)
        ev_group.setLayout(ev_layout)
        top_controls.addWidget(ev_group)

        # Launch Angle control
        la_group = QGroupBox("Launch Angles")
        la_layout = QGridLayout()

        self.vla_slider = QSlider(Qt.Orientation.Horizontal)
        self.vla_slider.setRange(0, 90)
        self.vla_slider.setValue(25)
        self.vla_value = QLabel(f"25°")
        self.vla_label = QLabel('V')
        self.vla_slider.valueChanged.connect(lambda v: self.vla_value.setText(f"{v}°"))
        la_layout.addWidget(self.vla_label, 0, 0)
        la_layout.addWidget(self.vla_slider, 0, 1, 3, 1, Qt.AlignmentFlag.AlignTop)
        la_layout.addWidget(self.vla_value, 0, 2)

        self.hla_slider = QSlider(Qt.Orientation.Horizontal)
        self.hla_slider.setRange(0, 90)
        self.hla_slider.setValue(45)
        self.hla_value = QLabel("45°")
        self.hla_label = QLabel('H')
        self.hla_slider.valueChanged.connect(lambda v: self.hla_value.setText(f"{v}°"))
        la_layout.addWidget(self.hla_label, 1, 0)
        la_layout.addWidget(self.hla_slider, 1, 1, 3, 1, Qt.AlignmentFlag.AlignTop)
        la_layout.addWidget(self.hla_value, 1, 2)
        la_layout.setColumnStretch(1, 1)
        la_layout.setRowStretch(0, 1)

        la_group.setLayout(la_layout)
        top_controls.addWidget(la_group)

        # Spin rate + Pitch speed controls (side by side)
        spin_pitch_container = QHBoxLayout()

        spin_group = QGroupBox("Spin Rate (rpm)")
        spin_layout = QVBoxLayout()
        self.spin_slider = QSlider(Qt.Orientation.Horizontal)
        self.spin_slider.setRange(1000, 3000)
        self.spin_slider.setValue(1800)
        self.spin_value = QLabel("1800")
        self.spin_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spin_slider.valueChanged.connect(lambda v: self.spin_value.setText(str(v)))
        spin_layout.addWidget(self.spin_slider)
        spin_layout.addWidget(self.spin_value)
        spin_group.setLayout(spin_layout)
        spin_pitch_container.addWidget(spin_group)

        pitch_speed_group = QGroupBox("Pitch Speed")
        pitch_speed_layout = QVBoxLayout()
        self.pitch_speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.pitch_speed_slider.setRange(1, 20)  # 0.1x to 2.0x in tenths
        self.pitch_speed_slider.setValue(10)      # 1.0x = real speed
        self.pitch_speed_value = QLabel("1.0x")
        self.pitch_speed_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        def _update_pitch_speed(v):
            speed = v / 10.0
            self.pitch_speed_value.setText(f"{speed:.1f}x")
            self.stadium_widget.pitch_slowdown = 1.0 / speed if speed > 0 else 10.0
        self.pitch_speed_slider.valueChanged.connect(_update_pitch_speed)
        pitch_speed_layout.addWidget(self.pitch_speed_slider)
        pitch_speed_layout.addWidget(self.pitch_speed_value)
        pitch_speed_group.setLayout(pitch_speed_layout)
        spin_pitch_container.addWidget(pitch_speed_group)

        top_controls.addLayout(spin_pitch_container)
        controls_main_layout.addLayout(top_controls)

        # Bottom row of controls
        bottom_controls = QHBoxLayout()

        # Wind override controls
        wind_group = QGroupBox("Override Weather")
        wind_layout = QGridLayout()
        wind_layout.setHorizontalSpacing(6)

        wind_layout.addWidget(QLabel("Wind Speed (mph):"), 0, 0)
        self.wind_speed_spin = QSpinBox()
        self.wind_speed_spin.setRange(0, 100)
        self.wind_speed_spin.setValue(10)
        wind_layout.addWidget(self.wind_speed_spin, 0, 1)

        wind_layout.addWidget(QLabel("Wind Direction (°):"), 1, 0)
        self.wind_dir_spin = QSpinBox()
        self.wind_dir_spin.setRange(0, 360)
        self.wind_dir_spin.setValue(0)
        wind_layout.addWidget(self.wind_dir_spin, 1, 1)

        self.override_weather = QCheckBox("Override Weather Data")
        wind_layout.addWidget(self.override_weather, 2, 0, 1, 2)

        # Ball position controls
        position_group = QGroupBox("Ball Starting Position (feet)")
        position_layout = QGridLayout()
        position_layout.setHorizontalSpacing(6)

        position_layout.addWidget(QLabel("X (center field):"), 0, 0)
        self.x_pos_spin = QDoubleSpinBox()
        self.x_pos_spin.setRange(-50, 100)
        self.x_pos_spin.setValue(-3.0)
        self.x_pos_spin.setSingleStep(0.5)
        self.x_pos_spin.valueChanged.connect(lambda: self.stadium_widget.update_starting_position())
        position_layout.addWidget(self.x_pos_spin, 0, 1)

        position_layout.addWidget(QLabel("Y (height):"), 1, 0)
        self.y_pos_spin = QDoubleSpinBox()
        self.y_pos_spin.setRange(-5, 20)
        self.y_pos_spin.setValue(4.5)
        self.y_pos_spin.setSingleStep(0.5)
        self.y_pos_spin.valueChanged.connect(lambda: self.stadium_widget.update_starting_position())
        position_layout.addWidget(self.y_pos_spin, 1, 1)

        position_layout.addWidget(QLabel("Z (left/right):"), 2, 0)
        self.z_pos_spin = QDoubleSpinBox()
        self.z_pos_spin.setRange(-50, 50)
        self.z_pos_spin.setValue(0.0)
        self.z_pos_spin.setSingleStep(0.5)
        self.z_pos_spin.valueChanged.connect(lambda: self.stadium_widget.update_starting_position())
        position_layout.addWidget(self.z_pos_spin, 2, 1)

        position_group.setLayout(position_layout)
        bottom_controls.addWidget(position_group)

        self.stadium_widget.x_pos_spin = self.x_pos_spin
        self.stadium_widget.y_pos_spin = self.y_pos_spin
        self.stadium_widget.z_pos_spin = self.z_pos_spin

        wind_group.setLayout(wind_layout)
        bottom_controls.addWidget(wind_group)

        # Cap each spinbox in these groups so the grid's column 1 doesn't
        # stretch wide and leave a big empty gap between label and input.
        for sb in (self.x_pos_spin, self.y_pos_spin, self.z_pos_spin,
                   self.wind_speed_spin, self.wind_dir_spin):
            sb.setMaximumWidth(70)

        # Action buttons (in drawer: lighting + weather update)
        button_layout = QVBoxLayout()

        self.lighting_btn = QPushButton("Lighting Controls")
        self.lighting_btn.clicked.connect(self.show_lighting_controls)
        button_layout.addWidget(self.lighting_btn)

        self.lighting_control = LightingControlWidget(self.stadium_widget.umpire_view.light_params)
        self.lighting_control.lightChanged.connect(self.update_lighting)
        self.lighting_control.nightModeChanged.connect(self._on_night_mode_changed)

        self.update_weather_btn = QPushButton("Update Weather Data")
        self.update_weather_btn.clicked.connect(self.update_weather)
        button_layout.addWidget(self.update_weather_btn)

        # -- Update BBE events: button swaps for a progress bar while scraping --
        self.update_bbe_stack = QStackedWidget()
        self.update_bbe_btn = QPushButton("Update BBE Events")
        self.update_bbe_btn.clicked.connect(self._start_bbe_update)
        self.update_bbe_progress = QProgressBar()
        self.update_bbe_progress.setRange(0, 100)
        self.update_bbe_progress.setFormat("Preparing… %p%")
        self.update_bbe_progress.setTextVisible(True)
        # QProgressBar defaults to Expanding horizontally, which made the whole
        # button column grow to fill the bottom_controls row. Match the button's
        # Minimum/Fixed policy so the column collapses back near its old width.
        for w in (self.update_bbe_progress, self.update_bbe_btn, self.update_bbe_stack):
            w.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.update_bbe_stack.addWidget(self.update_bbe_btn)        # index 0
        self.update_bbe_stack.addWidget(self.update_bbe_progress)   # index 1
        self.update_bbe_stack.setFixedHeight(self.update_weather_btn.sizeHint().height())
        button_layout.addWidget(self.update_bbe_stack)

        # Worker plumbing — held on self to survive past the start handler
        self._bbe_thread = None
        self._bbe_worker = None

        button_layout.addStretch()
        bottom_controls.addLayout(button_layout)

        controls_main_layout.addLayout(bottom_controls)

        # --- Drawer starts collapsed ---
        self._controls_container = controls_container
        self._controls_container.setMaximumHeight(0)
        self._controls_container.setMinimumHeight(0)
        main_layout.addWidget(controls_container)

        # Measure natural height after layout settles
        controls_container.adjustSize()
        self._drawer_full_height = controls_container.sizeHint().height()

        # Drawer animation
        self._drawer_anim = QPropertyAnimation(controls_container, b"maximumHeight")
        self._drawer_anim.setDuration(250)
        self._drawer_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._drawer_anim_min = QPropertyAnimation(controls_container, b"minimumHeight")
        self._drawer_anim_min.setDuration(250)
        self._drawer_anim_min.setEasingCurve(QEasingCurve.Type.OutCubic)

        # ============================================================
        # Bidirectional sync: toolbar spinboxes <-> drawer sliders
        # ============================================================
        self._syncing = False  # guard against feedback loops

        def _sync_ev_to_slider(v):
            if not self._syncing:
                self._syncing = True
                self.ev_slider.setValue(v)
                self._syncing = False

        def _sync_ev_to_spin(v):
            if not self._syncing:
                self._syncing = True
                self._tb_ev_spin.setValue(v)
                self._syncing = False

        self._tb_ev_spin.valueChanged.connect(_sync_ev_to_slider)
        self.ev_slider.valueChanged.connect(_sync_ev_to_spin)

        def _sync_vla_to_slider(v):
            if not self._syncing:
                self._syncing = True
                self.vla_slider.setValue(v)
                self._syncing = False

        def _sync_vla_to_spin(v):
            if not self._syncing:
                self._syncing = True
                self._tb_vla_spin.setValue(v)
                self._syncing = False

        self._tb_vla_spin.valueChanged.connect(_sync_vla_to_slider)
        self.vla_slider.valueChanged.connect(_sync_vla_to_spin)

        def _sync_hla_to_slider(v):
            if not self._syncing:
                self._syncing = True
                self.hla_slider.setValue(v)
                self._syncing = False

        def _sync_hla_to_spin(v):
            if not self._syncing:
                self._syncing = True
                self._tb_hla_spin.setValue(v)
                self._syncing = False

        self._tb_hla_spin.valueChanged.connect(_sync_hla_to_slider)
        self.hla_slider.valueChanged.connect(_sync_hla_to_spin)

        def _sync_spin_to_slider(v):
            if not self._syncing:
                self._syncing = True
                self.spin_slider.setValue(v)
                self._syncing = False

        def _sync_spin_to_spin(v):
            if not self._syncing:
                self._syncing = True
                self._tb_spin_spin.setValue(v)
                self._syncing = False

        self._tb_spin_spin.valueChanged.connect(_sync_spin_to_slider)
        self.spin_slider.valueChanged.connect(_sync_spin_to_spin)

        def _sync_pitch_speed_to_slider(v):
            if not self._syncing:
                self._syncing = True
                slider_val = int(round(v * 10))
                self.pitch_speed_slider.setValue(slider_val)
                self._syncing = False

        def _sync_pitch_speed_to_spin(v):
            if not self._syncing:
                self._syncing = True
                self._tb_pitch_speed_spin.setValue(v / 10.0)
                self._syncing = False

        self._tb_pitch_speed_spin.valueChanged.connect(_sync_pitch_speed_to_slider)
        self.pitch_speed_slider.valueChanged.connect(_sync_pitch_speed_to_spin)
        self.control_target = 'pos'
        self.control_index = 2
        self.control_value = self.stadium_widget.umpire_view.camera[self.control_target][self.control_index]

    # def keyPressEvent(self, a0):
    #     self.clearFocus()
    #     print(f"Keypress: {a0.key()}")
    #     if (a0.key() == Qt.Key.Key_C):
    #         print(self.stadium_widget.umpire_view.control_mode)
    #
    #     if (a0.key() in (Qt.Key.Key_Comma, Qt.Key.Key_Period)):
    #         if (a0.key() == Qt.Key.Key_Comma):  self.control_value -= 0.1;
    #         if (a0.key() == Qt.Key.Key_Period): self.control_value += 0.1;
    #         self.stadium_widget.umpire_view.camera[self.control_target][self.control_index] = self.control_value
    #         print(f"control value: {self.control_value}")
    #         self.stadium_widget.umpire_view.update()
    #
    #     for I in range(3):
    #         if (a0.key() == eval(f"Qt.Key.Key_{I+1}")):
    #             print(f"control index: {I}")
    #             self.control_index = I
    #             self.control_value = self.stadium_widget.umpire_view.camera[self.control_target][self.control_index]
    #             print(f"control value: {self.control_value}")
    #
    #     super().keyPressEvent(a0) # delegate back to base keybind handling
    #     return

    # ------------------------------------------------------------------ #
    # Compact toolbar / drawer helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _tb_separator():
        sep = QWidget()
        sep.setFixedWidth(1)
        sep.setFixedHeight(20)
        sep.setStyleSheet("background: #444;")
        return sep

    def _toggle_drawer(self):
        if self._drawer_expanded:
            # collapse
            self._drawer_expanded = False
            self._drawer_toggle.setText("▼ More")
            cur = self._controls_container.height()
            self._drawer_anim.setStartValue(cur)
            self._drawer_anim.setEndValue(0)
            self._drawer_anim_min.setStartValue(cur)
            self._drawer_anim_min.setEndValue(0)
        else:
            # expand
            self._drawer_expanded = True
            self._drawer_toggle.setText("▲ Less")
            # Recalculate in case font/dpi changed
            self._drawer_full_height = self._controls_container.sizeHint().height()
            if self._drawer_full_height < 50:
                self._drawer_full_height = 220  # sane fallback
            self._drawer_anim.setStartValue(0)
            self._drawer_anim.setEndValue(self._drawer_full_height)
            self._drawer_anim_min.setStartValue(0)
            self._drawer_anim_min.setEndValue(self._drawer_full_height)
        self._drawer_anim.start()
        self._drawer_anim_min.start()

    def keyPressEvent(self, event):
        """Handle key press events for camera movement"""
        camera_step = 1
        target_step = camera_step * 5
        self.clearFocus()

        # Get the currently active camera
        if self.stadium_widget.umpire_view.is_tracking_ball:
            # When tracking is enabled, we still modify the main camera
            # because we want manual camera controls to override tracking
            self.stadium_widget.umpire_view.is_tracking_ball = False
            active_camera = self.stadium_widget.umpire_view.camera
        else:
            active_camera = self.stadium_widget.umpire_view.camera

        # Position controls
        if event.key() == Qt.Key.Key_W:  # Move forward
            active_camera['pos'][0] += camera_step
            active_camera['target'][0] += camera_step
        elif event.key() == Qt.Key.Key_S:  # Move backward
            active_camera['pos'][0] -= camera_step
            active_camera['target'][0] -= camera_step
        elif event.key() == Qt.Key.Key_A:  # Move left
            active_camera['pos'][2] -= camera_step
            active_camera['target'][2] -= camera_step
        elif event.key() == Qt.Key.Key_D:  # Move right
            active_camera['pos'][2] += camera_step
            active_camera['target'][2] += camera_step
        elif event.key() == Qt.Key.Key_E:  # Move up
            active_camera['pos'][1] += camera_step
            active_camera['target'][1] += camera_step
        elif event.key() == Qt.Key.Key_Q:  # Move down
            active_camera['pos'][1] -= camera_step
            active_camera['target'][1] -= camera_step

        # Target controls
        elif event.key() == Qt.Key.Key_I:  # Target forward
            active_camera['target'][0] += target_step
        elif event.key() == Qt.Key.Key_K:  # Target backward
            active_camera['target'][0] -= target_step
        elif event.key() == Qt.Key.Key_J:  # Target left
            active_camera['target'][2] -= target_step
        elif event.key() == Qt.Key.Key_L:  # Target right
            active_camera['target'][2] += target_step
        elif event.key() == Qt.Key.Key_O:  # Target up
            active_camera['target'][1] += target_step
        elif event.key() == Qt.Key.Key_U:  # Target down
            active_camera['target'][1] -= target_step

        # Field of view controls
        elif event.key() == Qt.Key.Key_Plus:  # Zoom in
            active_camera['fov'] = max(20, active_camera['fov'] - 5)
        elif event.key() == Qt.Key.Key_Minus:  # Zoom out
            active_camera['fov'] = min(120, active_camera['fov'] + 5)

        # Print current camera settings
        elif event.key() == Qt.Key.Key_P:
            print("Camera settings:")
            print(f"  Position: {active_camera['pos']}")
            print(f"  Target: {active_camera['target']}")
            print(f"  FOV: {active_camera['fov']}")

        self.stadium_widget.umpire_view.update()
        super().keyPressEvent(event)

    def moveEvent(self, event):
        """Re-fit and re-cap when dragged to a different monitor."""
        super().moveEvent(event)
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.setMaximumHeight(available.height())
        cur_w, cur_h = self.width(), self.height()
        new_w = max(1200, available.width() - 40)
        new_h = max(700, available.height() - 40)
        if abs(new_w - cur_w) > 50 or abs(new_h - cur_h) > 50:
            self.resize(new_w, new_h)

    def change_stadium(self, stadium_name):
        """Update the stadium when selection changes"""
        # Update the stadium view
        self.stadium_widget.update_stadium(stadium_name)

        # Also update the weather data for the new stadium location
        print(f"Requesting weather update for new stadium: {stadium_name}")
        self.stadium_widget.fetch_weather_data()

    def simulate_flight(self):
        exit_velocity = self.ev_slider.value()
        vlaunch_angle = self.vla_slider.value()
        spin_rate = self.spin_slider.value()

        # Slider uses stadium polar convention (0=RF foul line, 45=CF, 90=LF
        # foul line).  Physics expects 0=CF, +45=RF foul line, -45=LF foul line.
        hlaunch_angle = 45 - self.hla_slider.value()

        # Check if we should override weather
        if self.override_weather.isChecked():
            wind_speed = self.wind_speed_spin.value()
            wind_direction = self.wind_dir_spin.value()
            self.stadium_widget.set_custom_weather(wind_speed, wind_direction)

        self.stadium_widget.simulate_ball_flight(exit_velocity, vlaunch_angle, hlaunch_angle, spin_rate)

    def update_weather(self):
        self.stadium_widget.fetch_weather_data()

    # ---------------- BBE incremental update (off-thread) ------------------ #
    def _start_bbe_update(self):
        """Kick off a Savant BBE incremental scrape in a background QThread."""
        if self._bbe_thread is not None and self._bbe_thread.isRunning():
            return  # already running

        # Resolve CSV paths next to this script — one job per player type so
        # both batter and pitcher CSVs stay current.
        from datetime import datetime as _dt
        year = _dt.now().year
        here = Path(__file__).parent
        jobs = [
            ("batter",  str(here / f"savant_bbe_{year}.csv")),
            ("pitcher", str(here / f"savant_pitcher-bbe_{year}.csv")),
        ]

        # Swap button for progress bar
        self.update_bbe_progress.setValue(0)
        self.update_bbe_progress.setFormat("Fetching leaderboard… %p%")
        self.update_bbe_stack.setCurrentIndex(1)

        self._bbe_thread = QThread(self)
        self._bbe_worker = BBEUpdateWorker(year=year, jobs=jobs)
        self._bbe_worker.moveToThread(self._bbe_thread)

        # Wire signals (queued connections cross thread boundaries automatically)
        self._bbe_thread.started.connect(self._bbe_worker.run)
        self._bbe_worker.progress.connect(self._on_bbe_progress)
        self._bbe_worker.status.connect(self._on_bbe_status)
        self._bbe_worker.finished.connect(self._on_bbe_finished)
        self._bbe_worker.error.connect(self._on_bbe_error)

        # Cleanup chain
        self._bbe_worker.finished.connect(self._bbe_thread.quit)
        self._bbe_worker.error.connect(self._bbe_thread.quit)
        self._bbe_thread.finished.connect(self._bbe_worker.deleteLater)
        self._bbe_thread.finished.connect(self._bbe_thread.deleteLater)
        self._bbe_thread.finished.connect(self._bbe_cleanup_refs)

        self._bbe_thread.start()

    def _on_bbe_status(self, msg: str):
        self.update_bbe_progress.setFormat(f"{msg} %p%")

    def _on_bbe_progress(self, done: int, total: int):
        if total <= 0:
            return
        # Worker now emits (global_pct, 100) already normalised across batter
        # and pitcher passes — just drive the bar value, leave format text to
        # the status signal so the player-type prefix stays visible.
        pct = int(round(100.0 * done / total))
        self.update_bbe_progress.setValue(pct)

    def _on_bbe_finished(self, result: dict):
        self.update_bbe_progress.setValue(100)
        note = result.get("note") if isinstance(result, dict) else ""
        if note:
            self.update_bbe_progress.setFormat(f"Done — {note}")
        else:
            self.update_bbe_progress.setFormat("Done")
        # Swap back to button after a short visible-completion beat
        QTimer.singleShot(2000, lambda: self.update_bbe_stack.setCurrentIndex(0))

    def _on_bbe_error(self, msg: str):
        self.update_bbe_progress.setFormat(f"Error: {msg[:40]}")
        QTimer.singleShot(3500, lambda: self.update_bbe_stack.setCurrentIndex(0))

    def _bbe_cleanup_refs(self):
        self._bbe_worker = None
        self._bbe_thread = None


    def show_lighting_controls(self):
        """Show the lighting control dialog"""
        self.lighting_control.show()

    def update_lighting(self):
        """Update 3D view lighting based on control widget settings"""
        light_params = self.lighting_control.get_light_params()
        self.stadium_widget.umpire_view.set_lighting(light_params)
        # Make sure to set the show_lights attribute
        self.stadium_widget.umpire_view.show_lights = self.lighting_control.show_lights()
        self.stadium_widget.umpire_view.update()

    def _on_night_mode_changed(self, enabled):
        """Toggle night game lighting on the 3D view."""
        self.stadium_widget.umpire_view.set_night_mode(enabled)
        if not enabled:
            # Sync the lighting control widget's params back from the restored day params
            self.lighting_control.lights = self.stadium_widget.umpire_view.light_params


class LightingControlWidget(QWidget):
    """Widget to control 3D scene lighting parameters"""

    lightChanged = pyqtSignal()  # Signal emitted when light parameters change
    nightModeChanged = pyqtSignal(bool)  # Signal emitted when night mode is toggled

    def __init__(self, lights, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lighting Controls")

        # Store the original lights with a deep copy to preserve all nested structures
        self.lights = lights
        self.default_lights = self.create_deep_copy(lights)

        # Dictionary to store RGB value labels for updates
        self.rgb_labels = {}

        # Store slider references for direct value updates
        self.sliders = {}

        self.setup_ui()

    def create_deep_copy(self, lights):
        """Create a deep copy of the lights list with all nested structures"""
        copied_lights = []
        for light in lights:
            # Create a new dictionary for each light
            light_copy = {}
            for key, value in light.items():
                # For list values (position, colors), create new lists with copied values
                if isinstance(value, list):
                    light_copy[key] = value.copy()
                else:
                    light_copy[key] = value
            copied_lights.append(light_copy)
        return copied_lights

    def setup_ui(self):
        # Main layout
        main_layout = QVBoxLayout(self)

        # Create tabs for each light source
        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)

        # Add visualization checkbox
        self.show_lights_check = QCheckBox("Show Light Sources")
        self.show_lights_check.setChecked(True)
        self.show_lights_check.stateChanged.connect(self.emit_light_changed)
        main_layout.addWidget(self.show_lights_check)

        # Night game mode checkbox
        self.night_game_check = QCheckBox("Night Game")
        self.night_game_check.setChecked(False)
        self.night_game_check.stateChanged.connect(self._on_night_game_toggled)
        main_layout.addWidget(self.night_game_check)

        # Create tabs for lights
        for i, light in enumerate(self.lights):
            light_tab = QWidget()
            tab_layout = QVBoxLayout(light_tab)

            # Initialize slider dictionary for this light
            if i not in self.sliders:
                self.sliders[i] = {}

            # Enable/disable light
            enable_check = QCheckBox(f"Enable Light {i}")
            enable_check.setChecked(light['enabled'])
            enable_check.stateChanged.connect(lambda state, idx=i: self.toggle_light(idx, state))
            tab_layout.addWidget(enable_check)

            # Light position controls
            pos_group = QGroupBox("Position")
            pos_layout = QGridLayout()

            # Initialize position sliders in dictionary
            self.sliders[i]['position'] = []

            # Create sliders for X, Y, Z position
            pos_labels = []

            for j, axis in enumerate(['X', 'Y', 'Z', 'W']):
                pos_layout.addWidget(QLabel(f"{axis}:"), j, 0)

                slider = QSlider(Qt.Orientation.Horizontal)

                # Special handling for W component which should be 0 or 1
                if axis == 'W':
                    slider.setRange(0, 1)  # W can be 0 (directional) or 1 (positional)
                    slider.setValue(int(light['position'][3]))
                    slider.valueChanged.connect(lambda val, idx=i, axis_idx=3:
                                              self.update_light_position(idx, axis_idx, float(val)))
                else:
                    # For X, Y, Z components
                    slider.setRange(-1000, 1000)
                    slider.setValue(int(light['position'][j] * 10))
                    slider.valueChanged.connect(lambda val, idx=i, axis_idx=j:
                                              self.update_light_position(idx, axis_idx, val/10))

                # Store slider reference for reset
                self.sliders[i]['position'].append(slider)

                label = QLabel(f"{light['position'][j]:.1f}" if axis != 'W' else f"{light['position'][3]:.0f}")

                # Different function for updating W label
                if axis == 'W':
                    slider.valueChanged.connect(lambda val, lbl=label: lbl.setText(f"{float(val):.0f}"))
                else:
                    slider.valueChanged.connect(lambda val, lbl=label: lbl.setText(f"{val/10:.1f}"))

                pos_layout.addWidget(slider, j, 1)
                pos_layout.addWidget(label, j, 2)

                pos_labels.append(label)

            pos_group.setLayout(pos_layout)
            tab_layout.addWidget(pos_group)

            # Light intensity controls
            intensity_group = QGroupBox("Intensity")
            intensity_layout = QGridLayout()

            # Initialize RGB labels dictionary for this light if not exists
            if i not in self.rgb_labels:
                self.rgb_labels[i] = {}

            # Create sliders for ambient, diffuse, specular
            for j, comp_name in enumerate(['Ambient', 'Diffuse', 'Specular']):
                intensity_layout.addWidget(QLabel(f"{comp_name}:"), j, 0)

                comp_key = comp_name.lower()

                # Initialize component in RGB labels dictionary
                if comp_key not in self.rgb_labels[i]:
                    self.rgb_labels[i][comp_key] = None

                # Initialize component sliders in dictionary
                if comp_key not in self.sliders[i]:
                    self.sliders[i][comp_key] = []

                # Create RGB sliders for each component
                rgb_layout = QHBoxLayout()
                rgb_values = []

                for k, color in enumerate(['R', 'G', 'B']):
                    color_value = light[comp_key][k]

                    slider = QSlider(Qt.Orientation.Horizontal)
                    slider.setRange(0, 100)
                    slider.setValue(int(color_value * 100))

                    # Store slider reference for reset
                    self.sliders[i][comp_key].append(slider)

                    # Connect color slider to update function
                    slider.valueChanged.connect(
                        lambda val, idx=i, comp=comp_key, color_idx=k:
                        self.update_light_component(idx, comp, color_idx, val/100)
                    )

                    rgb_layout.addWidget(QLabel(color))
                    rgb_layout.addWidget(slider)
                    rgb_values.append(color_value)

                # Add RGB value display and store reference
                rgb_label = QLabel(f"({rgb_values[0]:.1f}, {rgb_values[1]:.1f}, {rgb_values[2]:.1f})")
                self.rgb_labels[i][comp_key] = rgb_label

                intensity_layout.addLayout(rgb_layout, j, 1)
                intensity_layout.addWidget(rgb_label, j, 2)

            intensity_group.setLayout(intensity_layout)
            tab_layout.addWidget(intensity_group)

            # Add reset button
            reset_button = QPushButton(f"Reset Light {i} to Default")
            reset_button.clicked.connect(self.create_reset_function(i))
            tab_layout.addWidget(reset_button)

            # Add the tab
            self.tab_widget.addTab(light_tab, f"Light {i}")

    def create_reset_function(self, idx):
        """Create a proper reset function for a specific light index"""
        def reset_function():
            self.reset_light(idx)
        return reset_function

    def toggle_light(self, light_idx, enabled):
        """Enable or disable a light"""
        self.lights[light_idx]['enabled'] = enabled
        self.emit_light_changed()

    def update_light_position(self, light_idx, axis_idx, value):
        """Update a light's position on a specific axis"""
        self.lights[light_idx]['position'][axis_idx] = value
        self.emit_light_changed()

    def update_light_component(self, light_idx, component, color_idx, value):
        """Update a specific color component of a light's property"""
        # Update the light value
        self.lights[light_idx][component][color_idx] = value

        # Update the RGB label to show the new values
        if light_idx in self.rgb_labels and component in self.rgb_labels[light_idx]:
            rgb_values = self.lights[light_idx][component]
            self.rgb_labels[light_idx][component].setText(
                f"({rgb_values[0]:.1f}, {rgb_values[1]:.1f}, {rgb_values[2]:.1f})"
            )

        self.emit_light_changed()

    def reset_light(self, light_idx):
        """Reset a light to its default values"""
        print(f'RESETTING LIGHT: {light_idx}')

        # Create a deep copy of default light values
        default_light = self.default_lights[light_idx]
        reset_light = {}

        # Proper deep copy of each component
        for key, value in default_light.items():
            if isinstance(value, list):
                reset_light[key] = value.copy()
            else:
                reset_light[key] = value

        # Update light with reset values
        self.lights[light_idx] = reset_light

        # Directly update slider positions to match the reset values
        # Position sliders (X, Y, Z, W)
        if 'position' in self.sliders[light_idx]:
            for i, slider in enumerate(self.sliders[light_idx]['position']):
                if i < 3:  # X, Y, Z components (scale by 10)
                    slider.setValue(int(reset_light['position'][i] * 10))
                else:  # W component (0 or 1 directly)
                    slider.setValue(int(reset_light['position'][i]))

        # Color component sliders (ambient, diffuse, specular)
        for comp in ['ambient', 'diffuse', 'specular']:
            if comp in self.sliders[light_idx]:
                for i, slider in enumerate(self.sliders[light_idx][comp]):
                    slider.setValue(int(reset_light[comp][i] * 100))

        # Update RGB labels
        if light_idx in self.rgb_labels:
            for comp in ['ambient', 'diffuse', 'specular']:
                if comp in self.rgb_labels[light_idx]:
                    rgb_values = reset_light[comp]
                    self.rgb_labels[light_idx][comp].setText(
                        f"({rgb_values[0]:.1f}, {rgb_values[1]:.1f}, {rgb_values[2]:.1f})"
                    )

        print(f"Light {light_idx} reset complete")
        self.emit_light_changed()

    def emit_light_changed(self):
        """Emit signal when light parameters change"""
        self.lightChanged.emit()

    def show_lights(self):
        """Return whether to show light source visualizations"""
        return self.show_lights_check.isChecked()

    def get_light_params(self):
        """Return current light parameters"""
        return self.lights

    def _on_night_game_toggled(self, state):
        """Handle night game checkbox toggle."""
        enabled = bool(state)
        # Disable individual light tabs when night mode overrides them
        self.tab_widget.setEnabled(not enabled)
        self.nightModeChanged.emit(enabled)

    def is_night_mode(self):
        """Return whether night game mode is active."""
        return self.night_game_check.isChecked()







def main():
    app = QApplication(sys.argv)
    window = MLBWeatherApp()
    screen = app.primaryScreen()
    if screen:
        available = screen.availableGeometry()
        w = max(1200, available.width() - 40)
        h = max(700, available.height() - 40)
        window.resize(w, h)
        window.move(
            available.x() + (available.width()  - w) // 2,
            available.y() + (available.height() - h) // 2,
        )
    window.show()
    sys.exit(app.exec())


# ============================================================================
# Offline tools — calibration and model training
# ----------------------------------------------------------------------------
# Batch jobs that calibrate or train against BallFlightSimulator, which lives
# in this module.  Not runtime code: nothing here is imported by the widgets,
# and every heavy dependency (lightgbm, sklearn, pandas, multiprocessing) is
# imported lazily inside the functions so the GUI import path is unchanged.
#
#   python homerunwidget.py calibrate-cd [--cpw]
#   python homerunwidget.py calibrate-wind [--global] [--workers N]
#   python homerunwidget.py train-cpw
#   python homerunwidget.py train-runenv
#   python homerunwidget.py            # no args -> the widget, as before
#
# The multiprocessing workers pickle by qualified name, so the per-row physics
# helpers below must stay at module level.
# ============================================================================

import sys as _sys
import json
from multiprocessing import Pool, cpu_count

HERE = str(Path(__file__).resolve().parent)
# Shared with weatherman so both halves of the pipeline read and write the same
# place; regenerable, so gitignored.
DATA_DIR = os.path.join(HERE, "model_data")
os.makedirs(DATA_DIR, exist_ok=True)
CAL_YEARS = (2024, 2025, 2026)
CD_SAMPLE_N = 5000
SEED = 42

# Analysis dependencies, bound on first tool invocation.  Kept out of the
# module import so the widget never pays for lightgbm/sklearn, and out of the
# Pool workers' path because the per-row physics helpers below need none of
# them — workers only touch BallFlightSimulator, _num and _hla_from_hc.
pd = None
lgb = None
minimize_scalar = None
mean_absolute_error = None
log_loss = None


def _load_analysis_deps():
    global pd, lgb, minimize_scalar, mean_absolute_error, log_loss
    if pd is not None:
        return
    import pandas as _pd
    import lightgbm as _lgb
    from scipy.optimize import minimize_scalar as _ms
    from sklearn.metrics import mean_absolute_error as _mae, log_loss as _ll
    pd, lgb, minimize_scalar = _pd, _lgb, _ms
    mean_absolute_error, log_loss = _mae, _ll


def _hla_from_hc(hc_x: float, hc_y: float) -> float:
    """Savant spray pixel coords -> horizontal launch angle (degrees).

    Home plate at (125.42, 198.27); +HLA is toward right field.  Defined here
    rather than imported from train_distance_model, which imports THIS module
    and would make the dependency circular.
    """
    dx = hc_x - 125.42
    dy = 198.27 - hc_y
    if dy <= 0:
        return 0.0
    return math.degrees(math.atan2(dx, dy))


def _filter_dataframe(df):
    """Fly balls and liners with realistic distance and complete features."""
    needed = ["launch_speed", "launch_angle", "hit_distance_sc",
              "hc_x", "hc_y", "home_team", "bb_type"]
    df = df.dropna(subset=needed).copy()
    df = df[df["bb_type"].isin(["fly_ball", "line_drive"])]
    df = df[(df["launch_speed"] >= 80) & (df["hit_distance_sc"] >= 150)]
    from weatherman import TEAM_TO_PARK
    df["park"] = df["home_team"].map(TEAM_TO_PARK)
    df = df[df["park"].isin(STADIUM_DATA.keys())]
    return df.reset_index(drop=True)



# Constants that lived above the strip points when these tools were merged in
# from their standalone scripts.  NEUTRAL's absence made every C-Only physics
# value come back nan through _sim_one's except-clause, silently.
NEUTRAL = dict(temp=60.0, humidity=50.0, altitude=0.0, wind_speed=0.0,
               wind_direction=0.0, pressure_pa=None)

# calibrate_wind sizing, against ~960 trajectories/sec on 23 workers.
GLOBAL_SAMPLE = 4000
PARK_SAMPLE = 1500
PARK_TOTAL = 60000
MIN_PARK_ROWS = 400
ROUNDS = 2


def _tool_imports():
    """Heavy deps, resolved on first use rather than at import."""
    import numpy as np, pandas as pd
    return np, pd


def _num(value, default):
    """float(value) with None and NaN both falling back — NaN is truthy."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) else out


def _phys_with_cd(args):
    """Worker: run physics for one row at a given Cd."""
    row, cd = args
    sim = BallFlightSimulator()
    try:
        hla = _hla_from_hc(row["hc_x"], row["hc_y"])
        if row.get("_cpw"):
            press = _num(row.get("met_pressure_hpa"), None)
            env = dict(
                temp=_num(row.get("met_temp_f"), 60.0),
                humidity=_num(row.get("met_humidity"), 50.0),
                altitude=_num(row.get("park_elevation_ft"), 0.0),
                wind_speed=_num(row.get("met_wind_mph"), 0.0),
                # Already field-frame; do NOT pass park_azimuth as well.
                wind_direction=_num(row.get("met_wind_field_deg"), 0.0),
                pressure_pa=press * 100.0 if press is not None else None,
            )
            station = press is not None
        else:
            env = dict(temp=60.0, humidity=50.0,
                       altitude=float(STADIUM_DATA.get(row["park"], {})
                                      .get("altitude", 0.0)),
                       wind_speed=0.0, wind_direction=0.0, pressure_pa=None)
            station = False
        traj = sim.calculate_trajectory(
            exit_velocity=float(row["launch_speed"]),
            vlaunch_angle=float(row["launch_angle"]),
            hlaunch_angle=hla,
            cd_override=cd,
            pressure_is_station=station,
            **env,
        )
        return float(traj["distance"])
    except Exception:
        return float("nan")


def evaluate_cd(rows, cd, pool, label="", return_preds=False):
    preds = np.array(pool.map(_phys_with_cd, [(r, cd) for r in rows], chunksize=64))
    actual = np.array([r["hit_distance_sc"] for r in rows])
    mask = ~np.isnan(preds)
    bias = float(np.mean(preds[mask] - actual[mask]))
    mae = float(np.mean(np.abs(preds[mask] - actual[mask])))
    print(f"  Cd={cd:.4f}  N={mask.sum():5d}  bias={bias:+7.2f} ft  "
          f"MAE={mae:6.2f} ft  {label}")
    if return_preds:
        return bias, mae, preds, actual, mask
    return bias, mae


def load_frame(cpw):
    """Filtered BBE rows, with weather columns joined when cpw is set."""
    parts = []
    for year in CAL_YEARS:
        csv = os.path.join(HERE, f"savant_bbe_{year}.csv")
        if not os.path.exists(csv):
            continue
        print(f"[load] {csv}")
        df = pd.read_csv(csv, low_memory=False)
        if cpw:
            pq = os.path.join(DATA_DIR, f"weather_backfill_{year}.parquet")
            if not os.path.exists(pq):
                raise SystemExit(f"--cpw needs {pq}; run "
                             f"`python weatherman.py backfill` first")
            wx = pd.read_parquet(pq)
            if len(wx) != len(df):
                raise SystemExit(f"{year}: backfill/csv row mismatch")
            # Join BEFORE filtering — the parquet is in raw csv order.
            df = pd.concat([df.reset_index(drop=True),
                            wx.reset_index(drop=True)], axis=1)
        df = _filter_dataframe(df)
        if cpw:
            # The venue actually played at, not where the club plays today.
            df["park"] = df["venue_name"].fillna(df["park"])
            df = df.dropna(subset=["met_temp_f", "met_wind_mph"])
        parts.append(df)
    df = pd.concat(parts, ignore_index=True)
    print(f"[load] total filtered: {len(df)}")
    return df


def response_check(rows, preds, actual, mask):
    """Does the sim respond to the environment with the right SENSITIVITY?

    Getting mean bias to zero only fixes the intercept.  For park factors the
    slope is what matters: if carry gains too much per degree, every warm park
    is overrated and every cold one underrated, no matter how good the average
    looks.  Each slope below should be ~0 if the response is right.
    """
    err = preds[mask] - actual[mask]
    print("\n=== Phase C: environmental response (slope of error, want ~0) ===")
    for name, key, unit in (("temperature", "met_temp_f", "ft/F"),
                            ("altitude", "park_elevation_ft", "ft/1000ft"),
                            ("wind speed", "met_wind_mph", "ft/mph")):
        x = np.array([_num(r.get(key), np.nan) for r in rows])[mask]
        ok = ~np.isnan(x)
        if ok.sum() < 100 or np.nanstd(x[ok]) == 0:
            continue
        slope, intercept = np.polyfit(x[ok], err[ok], 1)
        r = float(np.corrcoef(x[ok], err[ok])[0, 1])
        shown = slope * 1000 if key == "park_elevation_ft" else slope
        print(f"  {name:12} slope {shown:+7.3f} {unit:10} r={r:+.3f}")

    # Wind decomposed along the ball's own line — the term park factors lean on
    fd = np.array([_num(r.get("met_wind_field_deg"), np.nan) for r in rows])[mask]
    sp = np.array([_num(r.get("met_wind_mph"), np.nan) for r in rows])[mask]
    sa = np.array([_hla_from_hc(r["hc_x"], r["hc_y"]) for r in rows])[mask]
    ok = ~(np.isnan(fd) | np.isnan(sp))
    if ok.sum() > 100:
        help_ = -sp[ok] * np.cos(np.radians(fd[ok] - sa[ok]))
        slope = np.polyfit(help_, err[ok], 1)[0]
        r = float(np.corrcoef(help_, err[ok])[0, 1])
        print(f"  {'wind_help':12} slope {slope:+7.3f} {'ft/mph':10} r={r:+.3f}"
              f"   <- the park-factor term")


def calibrate_cd_main():
    _load_analysis_deps()
    cpw = "--cpw" in sys.argv
    mode = "CPW (actual weather)" if cpw else "neutral (60F, sea level, calm)"
    print(f"=== calibrating C_d in {mode} ===\n")

    df = load_frame(cpw)
    cols = ["park", "hc_x", "hc_y", "launch_speed", "launch_angle", "hit_distance_sc"]
    if cpw:
        cols += ["met_temp_f", "met_humidity", "met_pressure_hpa", "met_wind_mph",
                 "met_wind_field_deg", "park_elevation_ft"]
    rng = np.random.default_rng(SEED)
    idx = rng.choice(len(df), size=min(CD_SAMPLE_N, len(df)), replace=False)
    sample = df.iloc[idx][cols].to_dict("records")
    for r in sample:
        r["_cpw"] = cpw
    print(f"[sample] using {len(sample)} events for calibration")

    with Pool(max(1, cpu_count() - 1)) as pool:
        print("\n=== Phase A: literature/code Cd checks ===")
        for cd in (0.30, 0.34, 0.38, 0.40, 0.42, 0.44):
            evaluate_cd(sample, cd, pool, label="(grid)")

        print("\n=== Phase B: scipy minimize_scalar on |bias| ===")
        n_evals = [0]

        def objective(cd):
            n_evals[0] += 1
            bias, _ = evaluate_cd(sample, cd, pool, label=f"(opt {n_evals[0]})")
            return abs(bias)

        t0 = time.time()
        result = minimize_scalar(objective, bounds=(0.28, 0.55), method="bounded",
                                 options={"xatol": 0.002, "maxiter": 25})
        cd_opt = result.x
        print(f"\n[optimize] {n_evals[0]} evals, {time.time() - t0:.1f}s")
        print(f"  Optimal Cd (zero-bias): {cd_opt:.4f}")

        print("\n=== Final verification at optimum ===")
        for cd in (cd_opt - 0.01, cd_opt, cd_opt + 0.01):
            evaluate_cd(sample, cd, pool, label="(verify)")
        _, _, preds, actual, mask = evaluate_cd(sample, cd_opt, pool,
                                                label="(response)",
                                                return_preds=True)
    if cpw:
        response_check(sample, preds, actual, mask)

    out = os.path.join(DATA_DIR, "cd_calibration_cpw.txt" if cpw else "cd_calibration.txt")
    with open(out, "w") as fh:
        fh.write(f"{cd_opt:.4f}\n")
    print(f"\n[save] optimal Cd -> {out}")




def _sim(args):
    """One trajectory under real weather, with wind scaled by `mult`."""
    row, cd, mult = args
    sim = BallFlightSimulator()
    try:
        press = _num(row.get("met_pressure_hpa"), None)
        traj = sim.calculate_trajectory(
            exit_velocity=float(row["launch_speed"]),
            vlaunch_angle=float(row["launch_angle"]),
            hlaunch_angle=_hla_from_hc(row["hc_x"], row["hc_y"]),
            temp=_num(row.get("met_temp_f"), 60.0),
            humidity=_num(row.get("met_humidity"), 50.0),
            altitude=_num(row.get("park_elevation_ft"), 0.0),
            wind_speed=_num(row.get("met_wind_mph"), 0.0) * mult,
            wind_direction=_num(row.get("met_wind_field_deg"), 0.0),
            pressure_pa=press * 100.0 if press is not None else None,
            pressure_is_station=press is not None,
            cd_override=cd,
        )
        return float(traj["distance"])
    except Exception:
        return float("nan")


def wind_help(rows):
    """Tailwind component along each ball's own direction, mph."""
    fd = np.array([_num(r.get("met_wind_field_deg"), np.nan) for r in rows])
    sp = np.array([_num(r.get("met_wind_mph"), np.nan) for r in rows])
    sa = np.array([_hla_from_hc(r["hc_x"], r["hc_y"]) for r in rows])
    return -sp * np.cos(np.radians(fd - sa))


def score(rows, cd, mult, pool, helps=None):
    """(mean bias, slope of residual on wind_help, MAE)."""
    preds = np.array(pool.map(_sim, [(r, cd, mult) for r in rows], chunksize=64))
    actual = np.array([r["hit_distance_sc"] for r in rows])
    h = wind_help(rows) if helps is None else helps
    ok = ~(np.isnan(preds) | np.isnan(h))
    err = preds[ok] - actual[ok]          # already masked — do not mask again
    slope = float(np.polyfit(h[ok], err, 1)[0]) if ok.sum() > 50 else float("nan")
    return float(err.mean()), slope, float(np.abs(err).mean())


def _solve_zero(xs, ys, lo, hi):
    """x where y crosses zero, from a quadratic through (xs, ys).

    Both responses here are smooth and near-linear, so three probes pin the
    curve exactly.  A bounded scalar optimiser needs ~12 evaluations to do the
    same job, and each evaluation is a few thousand ODE solves — the difference
    is minutes of a saturated CPU per fit.

    Returns NaN when the response never crosses zero in range.  It previously
    fell back to a clamp, which silently reported a boundary value (1.000) as
    though it were a fitted one — indistinguishable in the output from a park
    that genuinely wants full wind.
    """
    coeffs = np.polyfit(xs, ys, 2 if len(xs) > 2 else 1)
    roots = [r.real for r in np.roots(coeffs)
             if abs(r.imag) < 1e-9 and lo <= r.real <= hi]
    if roots:
        # Nearest root to the probed range's centre — the far one is spurious
        # curvature, not a second physical solution.
        return min(roots, key=lambda r: abs(r - np.mean(xs)))
    return float("nan")


def fit_global(rows, pool, cd0):
    """Alternate: multiplier kills the slope, C_d kills the bias."""
    cd, mult = cd0, 1.0
    helps = wind_help(rows)
    for rnd in range(1, ROUNDS + 1):
        probes = [0.0, 0.5, 1.0]
        slopes = []
        for m in probes:
            _, s, _ = score(rows, cd, m, pool, helps)
            slopes.append(s)
            print(f"    mult={m:.2f}  slope={s:+7.3f} ft/mph")
        mult = _solve_zero(np.array(probes), np.array(slopes), 0.0, 1.5)

        probes_cd = [cd - 0.03, cd, cd + 0.03]
        biases = []
        for c in probes_cd:
            b, _, _ = score(rows, c, mult, pool, helps)
            biases.append(b)
            print(f"    cd={c:.4f}  bias={b:+7.2f} ft")
        cd = _solve_zero(np.array(probes_cd), np.array(biases), 0.30, 0.55)

        b, s, mae = score(rows, cd, mult, pool, helps)
        print(f"  [round {rnd}] cd={cd:.4f} mult={mult:.4f} -> "
              f"bias={b:+.2f} ft  slope={s:+.3f} ft/mph  MAE={mae:.2f} ft")
    return cd, mult


def _slope(x, y):
    return float(np.polyfit(x, y, 1)[0]) if len(x) > 30 else float("nan")


def _partial_slope(h, y, controls):
    """Coefficient on `h` in a regression of y on h plus `controls`.

    A simple slope of the residual on wind_help is CONFOUNDED, and badly.
    wind_help = -speed * cos(field_dir - spray), so at a park with a prevailing
    wind it is largely a function of SPRAY ANGLE.  Our physics has real
    spray-dependent error — Magnus is vertical-only, so the extra carry a
    pulled fly ball gets from side spin is unmodelled — and that error then
    masquerades as a wind effect, with a sign set by which way the park's
    prevailing wind happens to blow.  Fitted naively, a third of the parks came
    out with NEGATIVE wind response (tailwind shortening the ball), Wrigley
    ranked 4th, and Angel Stadium ranked 1st despite its forecast carrying
    almost no information about field wind.

    Controlling for spray (and launch angle / exit velocity, which also shape
    the residual) isolates the part of the response that is actually wind.
    """
    X = np.column_stack([h] + list(controls) + [np.ones(len(h))])
    ok = np.all(np.isfinite(X), axis=1) & np.isfinite(y)
    if ok.sum() < 200:
        return float("nan")
    coef, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
    return float(coef[0])


def fit_all_parks(rows, pool, cd, shrink_n=2500):
    """Every park's effective-wind factor from TWO whole-dataset passes.

    The residual slope is linear in the multiplier — the simulator's wind term
    scales with it — so

        slope(m) = slope(0) + m * S,      S = slope(1) - slope(0)

    and the factor that zeroes it is just -slope(0)/S.  Running the ODE at
    m=0 and m=1 once over everything therefore yields every park at once, by
    grouping.  The previous version re-ran three probes PER PARK, which is
    ~45 passes over a subsample instead of 2 over the whole thing, and gave
    each park a smaller sample into the bargain.

    Per-park estimates are then shrunk toward the global fit by n/(n+shrink_n).
    They have to be: the underlying effect is small, so a park's own slope is
    dominated by noise, and unshrunk values swing from 0.02 to 2.0 in ways no
    stadium geometry explains.
    """
    helps = wind_help(rows)
    actual = np.array([r["hit_distance_sc"] for r in rows])
    parks = np.array([r["park"] for r in rows])
    # Controls: the residual's known structure, so the wind coefficient is not
    # asked to explain it.  spray enters quadratically (pull and oppo differ,
    # and both differ from centre).
    spray = np.array([_hla_from_hc(r["hc_x"], r["hc_y"]) for r in rows])
    la = np.array([_num(r.get("launch_angle"), np.nan) for r in rows])
    ev = np.array([_num(r.get("launch_speed"), np.nan) for r in rows])

    print("  pass 1/2: wind off ...", flush=True)
    p0 = np.array(pool.map(_sim, [(r, cd, 0.0) for r in rows], chunksize=64))
    print("  pass 2/2: wind full ...", flush=True)
    p1 = np.array(pool.map(_sim, [(r, cd, 1.0) for r in rows], chunksize=64))

    ok = ~(np.isnan(p0) | np.isnan(p1) | np.isnan(helps))
    e0, e1, h, pk = p0[ok] - actual[ok], p1[ok] - actual[ok], helps[ok], parks[ok]
    sp, la, ev = spray[ok], la[ok], ev[ok]
    ctl = [sp, sp ** 2, la, ev]

    s0_g = _partial_slope(h, e0, ctl)
    s1_g = _partial_slope(h, e1, ctl)
    g_mult = -s0_g / (s1_g - s0_g)
    print(f"\n  GLOBAL: slope(wind off)={s0_g:+.3f}  slope(wind full)={s1_g:+.3f}"
          f"  -> sim sensitivity {s1_g - s0_g:+.3f} ft/mph, factor {g_mult:.3f}")
    print(f"  (uncontrolled, for comparison: {_slope(h, e0):+.3f} / "
          f"{_slope(h, e1):+.3f})")

    out = {}
    for park in sorted(set(pk)):
        m = pk == park
        if m.sum() < MIN_PARK_ROWS:
            continue
        c = [x[m] for x in ctl]
        s0 = _partial_slope(h[m], e0[m], c)
        s1 = _partial_slope(h[m], e1[m], c)
        S = s1 - s0
        raw = -s0 / S if S > 0.5 else float("nan")
        n = int(m.sum())
        w = n / (n + shrink_n)
        shrunk = (w * raw + (1 - w) * g_mult) if not math.isnan(raw) else g_mult
        out[park] = {"n": n, "raw": None if math.isnan(raw) else round(raw, 3),
                     "wind_mult": round(float(np.clip(shrunk, 0.0, 1.0)), 3),
                     "sim_sensitivity": round(S, 3),
                     "real_response": round(-s0, 3)}
    return float(g_mult), out


def _workers():
    """Half the cores by default — this runs on the user's desktop, and a
    scalar optimiser saturating every core to fit one coefficient is a poor
    trade.  Override with --workers N."""
    for i, a in enumerate(sys.argv):
        if a == "--workers" and i + 1 < len(sys.argv):
            return max(1, int(sys.argv[i + 1]))
    return max(1, cpu_count() // 2)


def calibrate_wind_main():
    _load_analysis_deps()
    global_only = "--global" in sys.argv
    df = load_frame(cpw=True)
    # A wind multiplier is only identifiable where there IS wind, and roofed
    # games carry none by construction.
    df = df[(df.met_wind_mph.fillna(0) >= 3) & (~df.roof_closed.fillna(False))]
    # Collapse sponsor renames, or a park splits across two rows with half the
    # sample each: "Guaranteed Rate Field" (2024) and "Rate Field" (2025-26)
    # are one venue, as are Minute Maid/Daikin and Camden's two spellings.
    df["park"] = [resolve_park_name(p) or p for p in df["park"]]
    print(f"[filter] {len(df)} rows with usable wind, "
          f"{df.park.nunique()} distinct parks")

    cols = ["park", "hc_x", "hc_y", "launch_speed", "launch_angle", "hit_distance_sc",
            "met_temp_f", "met_humidity", "met_pressure_hpa", "met_wind_mph",
            "met_wind_field_deg", "park_elevation_ft"]
    rng = np.random.default_rng(SEED)
    idx = rng.choice(len(df), size=min(GLOBAL_SAMPLE, len(df)), replace=False)
    sample = df.iloc[idx][cols].to_dict("records")

    cd0 = 0.4166
    try:
        cd0 = float(open(os.path.join(DATA_DIR, "cd_calibration_cpw.txt")).read().strip())
    except (OSError, ValueError):
        pass

    nw = _workers()
    print(f"[cpu] {nw} workers of {cpu_count()} cores")
    with Pool(nw) as pool:
        print(f"\n=== global fit (start cd={cd0:.4f}, {len(sample)} rows) ===")
        b, s, mae = score(sample, cd0, 1.0, pool)
        print(f"  BEFORE: mult=1.00  bias={b:+.2f} ft  slope={s:+.3f} ft/mph  MAE={mae:.2f}")
        t0 = time.time()
        cd, mult = fit_global(sample, pool, cd0)
        print(f"\n  GLOBAL: cd={cd:.4f}  wind multiplier={mult:.3f}  "
              f"({time.time() - t0:.0f}s)")

        out = {"_global": {"cd": round(cd, 4), "wind_mult": round(mult, 3)}}
        if not global_only:
            print(f"\n=== per-park effective-wind factors (cd={cd:.4f}) ===")
            rows = df[cols].to_dict("records")
            if len(rows) > PARK_TOTAL:
                sub = rng.choice(len(rows), PARK_TOTAL, replace=False)
                rows = [rows[i] for i in sub]
            g_mult, parks = fit_all_parks(rows, pool, cd)
            out["_global"]["wind_mult_2pass"] = round(g_mult, 3)
            print(f"\n  {'park':32}{'n':>6}{'raw':>7}{'shrunk':>8}{'real ft/mph':>13}")
            for park in sorted(parks, key=lambda k: parks[k]["wind_mult"]):
                v = parks[park]
                raw = "  --  " if v["raw"] is None else f"{v['raw']:6.3f}"
                print(f"  {park:32}{v['n']:6}{raw:>7}{v['wind_mult']:8.3f}"
                      f"{v['real_response']:13.3f}")
                out[park] = v

    path = os.path.join(HERE, "wind_receptivity.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    print(f"\n[save] -> {path}")





def _load_cd(filename, fallback):
    """Drag coefficient from a calibration file, or the built-in default."""
    path = os.path.join(HERE, "model_data", filename)
    try:
        with open(path) as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        print(f"[cd] {filename} unreadable — falling back to {fallback}")
        return fallback


# The two configurations need DIFFERENT drag coefficients, and this is the
# whole reason the CPW physics was worse than neutral before.  A C_d fitted
# with the environment held at 60F/sea-level/calm has the average real
# environment absorbed into it; re-using it while ALSO supplying real
# conditions counts that environment twice, and the ball flies ~7 ft too far.
CD_NEUTRAL = _load_cd("cd_calibration.txt", 0.40)
CD_CPW = _load_cd("cd_calibration_cpw.txt", CD_NEUTRAL)

# Weather columns the CPW model is allowed to see.  obs_* is not among them.
MET_COLS = ["met_temp_f", "met_humidity", "met_pressure_hpa", "met_wind_mph",
            "met_wind_field_deg", "met_wind_gust_mph", "met_precip_in",
            "met_cloud_pct"]


# ------------------------------- physics ------------------------------------

def _unused_num_cpw(value, default):
    """float(value), with None AND NaN falling back to `default`.

    `float(nan or 0.0)` is nan, not 0.0 — NaN is truthy — so the obvious
    `or`-chain silently poisons the environment and the solver returns nan for
    the whole row.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) else out


def _sim_one(row):
    """One trajectory.  `row` carries a `_cpw` flag choosing the environment."""
    sim = BallFlightSimulator()
    try:
        hla = _hla_from_hc(row["hc_x"], row["hc_y"])
        if not row["_cpw"]:
            env = dict(NEUTRAL)
            station = False
        else:
            wind_dir = _num(row.get("met_wind_field_deg"), None)
            wind_mph = _num(row.get("met_wind_mph"), 0.0)
            if wind_dir is None:          # unknown bearing -> treat as calm
                wind_dir, wind_mph = 0.0, 0.0
            press = _num(row.get("met_pressure_hpa"), None)
            env = dict(
                temp=_num(row.get("met_temp_f"), 60.0),
                humidity=_num(row.get("met_humidity"), 50.0),
                altitude=_num(row.get("park_elevation_ft"), 0.0),
                # The uniform-equivalent wind, not the raw forecast: see
                # weatherman.effective_wind.  A reported 12 mph is not 12 mph
                # of steady flow over the whole trajectory, and feeding it as
                # though it were overstates carry by ~8x per mph.
                wind_speed=effective_wind(wind_mph, row.get("park")),
                # ALREADY in the field frame — park_azimuth must stay unset or
                # the rotation is applied twice.
                wind_direction=wind_dir,
                pressure_pa=press * 100.0 if press is not None else None,
            )
            station = env["pressure_pa"] is not None
        traj = sim.calculate_trajectory(
            exit_velocity=float(row["launch_speed"]),
            vlaunch_angle=float(row["launch_angle"]),
            hlaunch_angle=hla,
            pressure_is_station=station,
            cd_override=CD_CPW if row["_cpw"] else CD_NEUTRAL,
            **env,
        )
        return float(traj["distance"])
    except Exception:
        return float("nan")


def run_physics(df, cpw, n_workers=None):
    n_workers = n_workers or max(1, cpu_count() - 1)
    cols = ["hc_x", "hc_y", "launch_speed", "launch_angle", "park"]
    if cpw:
        cols += [c for c in MET_COLS + ["park_elevation_ft"] if c in df.columns]
    rows = df[cols].to_dict("records")
    for r in rows:
        r["_cpw"] = cpw
    label = "CPW" if cpw else "C-Only"
    print(f"[physics:{label}] {len(rows)} sims on {n_workers} workers...", flush=True)
    t0 = time.time()
    with Pool(n_workers) as pool:
        # map, not imap_unordered — order must line up with the frame.
        out = np.array(pool.map(_sim_one, rows, chunksize=64))
    print(f"[physics:{label}] {time.time() - t0:.1f}s")
    return out


# ------------------------------- features -----------------------------------

def _cpw_contact_features(df):
    """Everything about how the ball left the bat, and nothing else."""
    f = pd.DataFrame(index=df.index)
    f["launch_speed"] = df["launch_speed"]
    f["launch_angle"] = df["launch_angle"]
    f["spray_angle"] = df["spray_angle"]
    for c in ("release_speed", "release_spin_rate", "spin_axis",
              "pfx_x", "pfx_z", "plate_x", "plate_z", "effective_speed"):
        if c in df.columns:
            f[c] = df[c]
    if "pitch_type" in df.columns:
        f["pitch_type"] = df["pitch_type"].fillna("UNK").astype("category")
    return f


def _cpw_features(df):
    """Contact, plus the park and the day's air.

    Wind is handed over decomposed along the ball's own direction rather than
    as a bare bearing.  A 15 mph wind is a tailwind for a ball hit into it and
    a crosswind for one hit across it, and the raw angle makes the model
    rediscover that from scratch for every spray direction.
    """
    f = _cpw_contact_features(df)
    f["park"] = df["park"].astype("category")
    f["altitude"] = df["park_elevation_ft"] if "park_elevation_ft" in df else np.nan
    f["roof_closed"] = (df["roof_closed"].fillna(False).astype(int)
                        if "roof_closed" in df else 0)
    for c in MET_COLS:
        if c in df.columns and c != "met_wind_field_deg":
            f[c] = df[c]

    delta = np.radians(df["met_wind_field_deg"].astype(float) - df["spray_angle"].astype(float))
    # Effective wind here too, so the feature and the physics agree on what a
    # "mph" means.  The park factor is then read off a consistent pair.
    speed = np.array([effective_wind(v, p) or 0.0
                      for v, p in zip(df["met_wind_mph"], df["park"])])
    # wind_help > 0 pushes the ball out; wind_cross > 0 pushes it toward RF.
    f["wind_help"] = -speed * np.cos(delta)
    f["wind_cross"] = -speed * np.sin(delta)
    gust = np.array([effective_wind(v, p) or 0.0
                     for v, p in zip(df["met_wind_gust_mph"], df["park"])])
    f["gust_help"] = -gust * np.cos(delta)
    return f


# --------------------------------- data -------------------------------------

def _cpw_load_year(year):
    """BBE rows for a season with the weather columns joined on.

    The backfill parquet is written in the RAW csv's row order, so it is
    concatenated BEFORE any filtering — filtering first would misalign the two
    frames silently.
    """
    csv = os.path.join(HERE, f"savant_bbe_{year}.csv")
    pq = os.path.join(DATA_DIR, f"weather_backfill_{year}.parquet")
    if not os.path.exists(csv):
        return None
    df = pd.read_csv(csv, low_memory=False)
    if os.path.exists(pq):
        wx = pd.read_parquet(pq)
        if len(wx) != len(df):
            raise SystemExit(f"{year}: backfill has {len(wx)} rows, csv has {len(df)} "
                             "— rerun `python weatherman.py backfill`")
        df = pd.concat([df.reset_index(drop=True), wx.reset_index(drop=True)], axis=1)
    else:
        print(f"[warn] {pq} missing — {year} has no weather")
    df = _filter_dataframe(df)

    # _filter_dataframe labels the park from TEAM_TO_PARK, which is a snapshot
    # of where clubs play NOW.  Applied to history it mislabels whole seasons —
    # 2024 Oakland becomes Sacramento, 2025 Tampa Bay becomes Tropicana.  The
    # backfill carries the venue the game was actually played at, so it wins.
    # Former and neutral venues stay as their own categories rather than being
    # dropped: CPW wants park identity and elevation, not wall geometry.
    if "venue_name" in df.columns:
        real = df["venue_name"].fillna(df["park"])
        moved = int((real != df["park"]).sum())
        if moved:
            print(f"[{year}] repointed {moved} rows to the venue actually played at "
                  f"({sorted(set(real[real != df['park']]))[:4]})")
        df["park"] = real

    df["spray_angle"] = [
        _hla_from_hc(x, y) for x, y in zip(df["hc_x"], df["hc_y"])
    ]
    df["season"] = year
    return df


def _fit(X_tr, y_tr, X_te, y_te, label, rounds=2000):
    for col in X_tr.select_dtypes("category").columns:
        cats = X_tr[col].cat.categories.union(X_te[col].cat.categories)
        X_tr[col] = X_tr[col].cat.set_categories(cats)
        X_te[col] = X_te[col].cat.set_categories(cats)
    cats = list(X_tr.select_dtypes("category").columns)
    print(f"\n[lgb:{label}] {len(X_tr)} rows x {X_tr.shape[1]} features")
    ds_tr = lgb.Dataset(X_tr, label=y_tr, categorical_feature=cats, free_raw_data=False)
    ds_te = lgb.Dataset(X_te, label=y_te, categorical_feature=cats,
                        reference=ds_tr, free_raw_data=False)
    params = dict(objective="regression_l1", metric="mae", learning_rate=0.05,
                  num_leaves=63, min_data_in_leaf=200, feature_fraction=0.9,
                  bagging_fraction=0.9, bagging_freq=5, verbose=-1)
    model = lgb.train(params, ds_tr, num_boost_round=rounds,
                      valid_sets=[ds_te], valid_names=["test"],
                      callbacks=[lgb.early_stopping(50), lgb.log_evaluation(200)])
    return model


def train_cpw_main():
    _load_analysis_deps()
    train = pd.concat([d for d in (_cpw_load_year(2024), _cpw_load_year(2025)) if d is not None],
                      ignore_index=True)
    test = _cpw_load_year(2026)
    if test is None:
        raise SystemExit("no 2026 csv to test on")
    print(f"\n[load] train={len(train)} test={len(test)}")
    have = train["met_temp_f"].notna().mean() if "met_temp_f" in train else 0.0
    print(f"[load] train rows with weather: {100 * have:.1f}%")

    # Weather is the point; rows without it cannot be scored by CPW.
    for name, d in (("train", train), ("test", test)):
        before = len(d)
        d.dropna(subset=["met_temp_f", "met_wind_mph"], inplace=True)
        d.reset_index(drop=True, inplace=True)
        print(f"[filter] {name}: {before} -> {len(d)} with weather")

    for d in (train, test):
        d["phys_c"] = run_physics(d, cpw=False)
        d["phys_cpw"] = run_physics(d, cpw=True)
    train = train.dropna(subset=["phys_c", "phys_cpw"]).reset_index(drop=True)
    test = test.dropna(subset=["phys_c", "phys_cpw"]).reset_index(drop=True)

    actual_tr = train["hit_distance_sc"].values
    actual_te = test["hit_distance_sc"].values

    print("\n" + "=" * 66)
    print("PHYSICS ALONE (no learned residual)")
    for label, col in (("C-Only  (neutral air)", "phys_c"),
                       ("CPW     (actual air)", "phys_cpw")):
        p = test[col].values
        print(f"  {label:24} MAE {mean_absolute_error(actual_te, p):6.2f} ft"
              f"   bias {np.mean(p - actual_te):+6.2f} ft")

    models, preds = {}, {}
    for label, builder, physcol in (("c_only", _cpw_contact_features, "phys_c"),
                                    ("cpw", _cpw_features, "phys_cpw")):
        X_tr, X_te = builder(train), builder(test)
        y_tr = actual_tr - train[physcol].values
        y_te = actual_te - test[physcol].values
        m = _fit(X_tr, y_tr, X_te, y_te, label)
        models[label] = m
        preds[label] = test[physcol].values + m.predict(
            X_te, num_iteration=m.best_iteration)

    print("\n" + "=" * 66)
    print("PHYSICS + LEARNED RESIDUAL (test = 2026)")
    for label in ("c_only", "cpw"):
        p = preds[label]
        print(f"  {label:8} MAE {mean_absolute_error(actual_te, p):6.2f} ft"
              f"   bias {np.mean(p - actual_te):+6.2f} ft")
    gain = (mean_absolute_error(actual_te, preds["c_only"])
            - mean_absolute_error(actual_te, preds["cpw"]))
    print(f"\n  park+weather is worth {gain:+.2f} ft of MAE")
    print("=" * 66)

    # What the park and the air did to each batted ball — the raw material for
    # park factors, before any aggregation to batter or game.
    test["park_weather_ft"] = preds["cpw"] - preds["c_only"]
    by_park = (test.groupby("park")["park_weather_ft"]
               .agg(["size", "mean", "std"])
               .sort_values("mean", ascending=False))
    print("\nCarry added by park & weather, 2026 (ft per batted ball):")
    print(by_park.to_string(float_format=lambda x: f"{x:7.2f}"))

    imp = pd.DataFrame({"feature": models["cpw"].feature_name(),
                        "gain": models["cpw"].feature_importance("gain")})
    print("\nCPW top features by gain:")
    print(imp.sort_values("gain", ascending=False).head(18).to_string(index=False))

    for label, m in models.items():
        out = os.path.join(DATA_DIR, f"{label}_distance_model.lgb")
        m.save_model(out)
        print(f"[save] {out}")




RUN_VALUE = {"out": -0.27, "1B": 0.47, "2B": 0.78, "3B": 1.09, "HR": 1.40}
CLASSES = ["out", "1B", "2B", "3B", "HR"]

# Everything that is not a hit is an out for these purposes, including reached
# -on-error and sacrifices: this models the BATTED BALL, not the defence's
# execution, which is the same reason expected-outcome metrics treat errors as
# outs.  Fouls are not balls in play at all and are dropped.
_HIT = {"single": "1B", "double": "2B", "triple": "3B", "home_run": "HR"}
_NOT_IN_PLAY = {"foul", "catcher_interf"}


def _outcome(event):
    if event in _NOT_IN_PLAY or not isinstance(event, str):
        return None
    return _HIT.get(event, "out")


def _re_load_year(year):
    csv = os.path.join(HERE, f"savant_bbe_{year}.csv")
    pq = os.path.join(DATA_DIR, f"weather_backfill_{year}.parquet")
    if not os.path.exists(csv):
        return None
    df = pd.read_csv(csv, low_memory=False)
    if os.path.exists(pq):
        wx = pd.read_parquet(pq)
        if len(wx) != len(df):
            raise SystemExit(f"{year}: backfill/csv row mismatch")
        df = pd.concat([df.reset_index(drop=True), wx.reset_index(drop=True)], axis=1)

    df["outcome"] = [_outcome(e) for e in df["events"]]
    df = df.dropna(subset=["outcome", "launch_speed", "launch_angle",
                           "hc_x", "hc_y", "met_temp_f"])
    df["spray_angle"] = [_hla_from_hc(x, y) for x, y in zip(df.hc_x, df.hc_y)]
    df["park"] = [resolve_park_name(p) or p
                  for p in df["venue_name"].fillna("")]
    df["season"] = year
    if "game_pk" not in df.columns:
        df["game_pk"] = np.nan
    print(f"[{year}] {len(df)} balls in play  "
          f"({', '.join(f'{k} {100*(df.outcome==k).mean():.1f}%' for k in CLASSES)})")
    return df


def _wall(park, spray, fn, default):
    """Fence distance/height where the ball was actually hit.

    Spray is 0 at centre and positive toward right; weatherman's polar table
    runs 0 at the right-field line to 90 at left, hence 45 - spray.
    """
    if not park:
        return default
    try:
        v = fn(park, 45.0 - float(spray))
    except Exception:
        return default
    return default if v is None else float(v)


def _re_contact_features(df):
    f = pd.DataFrame(index=df.index)
    f["launch_speed"] = df["launch_speed"]
    f["launch_angle"] = df["launch_angle"]
    f["spray_angle"] = df["spray_angle"]
    f["abs_spray"] = df["spray_angle"].abs()
    if "bb_type" in df:
        f["bb_type"] = df["bb_type"].fillna("unknown").astype("category")
    return f


def _re_cpw_features(df):
    f = _re_contact_features(df)
    f["wall_distance"] = [_wall(p, s, get_stadium_wall_distance, np.nan)
                          for p, s in zip(df.park, df.spray_angle)]
    f["wall_height"] = [_wall(p, s, get_stadium_wall_height, 8.0)
                        for p, s in zip(df.park, df.spray_angle)]
    f["altitude"] = df["park_elevation_ft"]
    f["temp_f"] = df["met_temp_f"]
    f["humidity"] = df["met_humidity"]
    f["pressure_hpa"] = df["met_pressure_hpa"]
    f["roof_closed"] = df["roof_closed"].fillna(False).astype(int)

    speed = np.array([effective_wind(v, p) or 0.0
                      for v, p in zip(df["met_wind_mph"], df["park"])])
    delta = np.radians(df["met_wind_field_deg"].astype(float)
                       - df["spray_angle"].astype(float))
    f["wind_help"] = -speed * np.cos(delta)
    f["wind_cross"] = -speed * np.sin(delta)
    f["park"] = df["park"].astype("category")
    return f


def _re_fit(X_tr, y_tr, X_te, y_te, label):
    for col in X_tr.select_dtypes("category").columns:
        cats = X_tr[col].cat.categories.union(X_te[col].cat.categories)
        X_tr[col] = X_tr[col].cat.set_categories(cats)
        X_te[col] = X_te[col].cat.set_categories(cats)
    cats = list(X_tr.select_dtypes("category").columns)
    print(f"\n[lgb:{label}] {len(X_tr)} rows x {X_tr.shape[1]} features")
    params = dict(objective="multiclass", num_class=len(CLASSES),
                  metric="multi_logloss", learning_rate=0.05, num_leaves=63,
                  min_data_in_leaf=200, feature_fraction=0.9,
                  bagging_fraction=0.9, bagging_freq=5, verbose=-1)
    ds_tr = lgb.Dataset(X_tr, label=y_tr, categorical_feature=cats, free_raw_data=False)
    ds_te = lgb.Dataset(X_te, label=y_te, categorical_feature=cats,
                        reference=ds_tr, free_raw_data=False)
    return lgb.train(params, ds_tr, num_boost_round=1500,
                     valid_sets=[ds_te], valid_names=["test"],
                     callbacks=[lgb.early_stopping(50), lgb.log_evaluation(200)])


def train_run_env_main():
    _load_analysis_deps()
    # Which season to hold out.  2026 is only March 25 - May 2, so testing on
    # it yields COLD-WEATHER park factors: the rankings hold up but the levels
    # understate parks that play warm.  `--test-year 2025` evaluates on a full
    # season instead, which is what the published factors describe.
    test_year = 2026
    for i, a in enumerate(_sys.argv):
        if a == "--test-year" and i + 1 < len(_sys.argv):
            test_year = int(_sys.argv[i + 1])
    train_years = [y for y in CAL_YEARS if y != test_year]
    print(f"[split] train {train_years}  test {test_year}")
    train = pd.concat([d for d in (_re_load_year(y) for y in train_years)
                       if d is not None], ignore_index=True)
    test = _re_load_year(test_year)
    if test is None:
        raise SystemExit(f"no {test_year} data")

    idx = {c: i for i, c in enumerate(CLASSES)}
    y_tr = train.outcome.map(idx).values
    y_te = test.outcome.map(idx).values
    rv = np.array([RUN_VALUE[c] for c in CLASSES])

    models, exp_runs = {}, {}
    for label, builder in (("c_only", _re_contact_features), ("cpw", _re_cpw_features)):
        m = _re_fit(builder(train), y_tr, builder(test), y_te, label)
        p = m.predict(builder(test), num_iteration=m.best_iteration)
        models[label], exp_runs[label] = m, p @ rv
        ll = log_loss(y_te, p, labels=list(range(len(CLASSES))))
        print(f"  {label:8} multi-logloss {ll:.5f}")

    actual = test.outcome.map(RUN_VALUE).values
    print("\n" + "=" * 68)
    print(f"EXPECTED RUNS PER BALL IN PLAY (test = {test_year})")
    for label in ("c_only", "cpw"):
        e = exp_runs[label]
        print(f"  {label:8} mean {e.mean():+.4f}   MAE vs actual {np.abs(e - actual).mean():.4f}")
    print("=" * 68)

    test = test.assign(runs_added=exp_runs["cpw"] - exp_runs["c_only"])
    g = (test.groupby("park")["runs_added"]
         .agg(bip="size", runs_per_bip="mean", sd="std")
         .query("bip >= 150")
         .sort_values("runs_per_bip", ascending=False))
    # ~26 balls in play per team-game; league average runs/game ~4.4
    g["runs_per_game"] = g.runs_per_bip * 26.0
    g["index_100"] = 100 * (1 + g.runs_per_game / 4.4)
    print(f"\nRun environment by park, {test_year} (park + weather only):")
    print(g.to_string(float_format=lambda v: f"{v:9.3f}"))

    imp = pd.DataFrame({"feature": models["cpw"].feature_name(),
                        "gain": models["cpw"].feature_importance("gain")})
    imp["share"] = 100 * imp.gain / imp.gain.sum()
    print("\nCPW features by gain:")
    print(imp.sort_values("gain", ascending=False)
          .to_string(index=False, float_format=lambda v: f"{v:12.2f}"))

    test[["game_pk", "park", "runs_added"]].to_parquet(
        os.path.join(DATA_DIR, f"runs_added_{test_year}.parquet"), index=False)
    for label, m in models.items():
        m.save_model(os.path.join(DATA_DIR, f"{label}_runenv_model.lgb"))
    print(f"\n[save] *_runenv_model.lgb")






def backtest_runs_main():
    """Do our park+weather run adjustments predict ACTUAL runs scored?

    The park factors above are internal to the model: they are a difference of
    two of its own expectations.  This checks them against the scoreboard.

    For every game we sum `runs_added` over its balls in play — the model's
    claim about what the venue and that evening's air contributed — and then
    regress the game's real run total on it, controlling for how good the two
    clubs actually are.

        actual_runs ~ a + b * park_weather_runs + c * team_quality

    A calibrated model gives b ~ 1: one predicted run of park effect shows up
    as one real run.  b well under 1 means the adjustments are too aggressive,
    over 1 means too timid.

    Team quality uses each club's ROAD-only scoring and run-prevention rates.
    Season-long rates are contaminated for this purpose — half of them are
    earned in the very park whose effect we are trying to measure, which would
    launder the park factor into the control and hide it.
    """
    _load_analysis_deps()
    import urllib.request, json as _json
    import numpy as _np

    test_year = 2026
    for i, a in enumerate(_sys.argv):
        if a == "--test-year" and i + 1 < len(_sys.argv):
            test_year = int(_sys.argv[i + 1])

    # Contact-channel factors must come from a DIFFERENT season than the one
    # being tested, or the backtest grades the model on residuals it was fitted
    # to and the coefficient is meaningless.
    contact_season = None
    for i, a in enumerate(_sys.argv):
        if a == "--contact-season" and i + 1 < len(_sys.argv):
            contact_season = int(_sys.argv[i + 1])
    contact = None
    if contact_season:
        cf = os.path.join(DATA_DIR, f"contact_factors_{contact_season}.parquet")
        if not os.path.exists(cf):
            raise SystemExit(f"need {cf} — run `contact-factors --season "
                             f"{contact_season}` first")
        if contact_season == test_year:
            raise SystemExit("--contact-season must differ from --test-year "
                             "(in-sample factors make the backtest circular)")
        contact = pd.read_parquet(cf).set_index("park")["contact_runs_game"]
        print(f"[backtest] contact channel from {contact_season} "
              f"({len(contact)} parks), applied out of sample")

    pq = os.path.join(DATA_DIR, f"runs_added_{test_year}.parquet")
    if not os.path.exists(pq):
        raise SystemExit(f"need {pq} — run `train-runenv --test-year {test_year}` first")
    ra = pd.read_parquet(pq).dropna(subset=["game_pk"])
    per_game = ra.groupby("game_pk").agg(park_weather_runs=("runs_added", "sum"),
                                         bip=("runs_added", "size"),
                                         park=("park", "first")).reset_index()

    url = (f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&season={test_year}"
           "&gameType=R")
    with urllib.request.urlopen(url, timeout=90) as fh:
        sched = _json.load(fh)
    rows = []
    for d in sched["dates"]:
        for g in d["games"]:
            if g["status"]["abstractGameState"] != "Final":
                continue
            h, a = g["teams"]["home"], g["teams"]["away"]
            if "score" not in h or "score" not in a:
                continue
            rows.append({"game_pk": g["gamePk"], "home": h["team"]["id"],
                         "away": a["team"]["id"], "hr_": h["score"], "ar_": a["score"]})
    games = pd.DataFrame(rows)
    print(f"[backtest] {len(games)} final games with scores, "
          f"{len(per_game)} with modelled batted balls")

    # Road-only rates, so the control cannot absorb the home park's effect.
    road = games.groupby("away").agg(rs=("ar_", "mean")).rename(columns={"rs": "road_rs"})
    road_ra = games.groupby("home").agg(ra_=("ar_", "mean")).rename(columns={"ra_": "opp_rs_at_home"})
    road_allow = games.groupby("away").agg(ra_=("hr_", "mean")).rename(columns={"ra_": "road_ra"})
    q = road.join(road_allow)

    df = games.merge(per_game, on="game_pk", how="inner")
    df["actual_runs"] = df.hr_ + df.ar_
    df["quality"] = (
        df.home.map(q.road_rs) + df.away.map(q.road_rs)
        + df.home.map(q.road_ra) + df.away.map(q.road_ra)) / 2.0
    if contact is not None:
        df["contact_runs"] = df.park.map(contact).fillna(0.0)
        df["park_weather_runs"] = df.park_weather_runs + df.contact_runs
    df = df.dropna(subset=["actual_runs", "park_weather_runs", "quality"])
    # A game with few modelled batted balls carries little park signal and a
    # lot of noise; require a realistic count.
    df = df[df.bip >= 30]
    print(f"[backtest] {len(df)} games after filtering")

    y = df.actual_runs.values.astype(float)
    pw = df.park_weather_runs.values.astype(float)
    ql = df.quality.values.astype(float)

    def ols(X, y):
        X = _np.column_stack(X + [_np.ones(len(y))])
        coef, *_ = _np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
        r2 = 1 - resid.var() / y.var()
        se = _np.sqrt(_np.diag(_np.linalg.pinv(X.T @ X) * resid.var(ddof=X.shape[1])))
        return coef, r2, se

    c0, r2_0, _ = ols([ql], y)
    c1, r2_1, se1 = ols([pw, ql], y)
    b, t = c1[0], c1[0] / se1[0]
    print("\n" + "=" * 70)
    print(f"ACTUAL RUNS BACKTEST — {test_year}")
    print(f"  quality only            : R^2 {r2_0:.4f}")
    print(f"  + park/weather runs     : R^2 {r2_1:.4f}   (delta {r2_1 - r2_0:+.4f})")
    print(f"  coefficient on our term : b = {b:+.3f}  (t = {t:+.2f})")
    print(f"     b ~ 1 means calibrated; <1 too aggressive, >1 too timid")
    print(f"  spread of our term      : sd {pw.std():.3f} runs/game")
    print("=" * 70)

    # Park-level: our mean adjustment vs the park's actual run residual.
    df["resid"] = y - (c0[0] * ql + c0[1])
    g = df.groupby("park").agg(games=("resid", "size"), actual_resid=("resid", "mean"),
                               predicted=("park_weather_runs", "mean"))
    g = g[g.games >= 20].sort_values("predicted", ascending=False)
    if len(g) > 2:
        rho = _np.corrcoef(g.predicted, g.actual_resid)[0, 1]
        print(f"\nPer-park: corr(predicted park+weather runs, actual run residual) "
              f"= {rho:+.3f}  over {len(g)} parks")
        print(g.to_string(float_format=lambda v: f"{v:9.3f}"))




# Run values per plate-appearance event, for the contact channel.  Same scale
# as RUN_VALUE above (runs above average), so the two channels add.
PA_RUN_VALUE = {"K": -0.28, "BB": 0.33, "HBP": 0.36}


def team_game_logs(season, cache=True):
    """Per-team, per-game batting lines for a season — 30 requests, ~1 MB.

    The per-game boxscore carries the same numbers but costs 2,400 requests and
    ~50 MB, because `fields=` cannot strip its player-level block.
    """
    import urllib.request, json as _json
    path = os.path.join(HERE, "weather_cache", f"gamelogs_{season}.parquet")
    if cache and os.path.exists(path):
        return pd.read_parquet(path)
    teams = _json.loads(urllib.request.urlopen(
        f"https://statsapi.mlb.com/api/v1/teams?sportId=1&season={season}",
        timeout=60).read())["teams"]
    rows = []
    for t in teams:
        u = (f"https://statsapi.mlb.com/api/v1/teams/{t['id']}/stats?stats=gameLog"
             f"&group=hitting&season={season}&fields=stats,splits,date,isHome,game,"
             "gamePk,opponent,id,stat,plateAppearances,strikeOuts,baseOnBalls,"
             "hitByPitch,runs")
        try:
            d = _json.loads(urllib.request.urlopen(u, timeout=60).read())
        except Exception as e:
            print(f"  [warn] game log {t['id']}: {e}")
            continue
        for sp in (d.get("stats") or [{}])[0].get("splits", []):
            st = sp.get("stat", {})
            rows.append({
                "season": season, "team": t["id"],
                "opponent": (sp.get("opponent") or {}).get("id"),
                "game_pk": (sp.get("game") or {}).get("gamePk"),
                "is_home": bool(sp.get("isHome")),
                "pa": st.get("plateAppearances"), "k": st.get("strikeOuts"),
                "bb": st.get("baseOnBalls"), "hbp": st.get("hitByPitch"),
                "runs": st.get("runs"),
            })
    df = pd.DataFrame(rows).dropna(subset=["pa", "game_pk"])
    df = df[df.pa > 0]
    if cache:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        df.to_parquet(path, index=False)
    print(f"[gamelogs] {season}: {len(df)} team-games")
    return df


def contact_factors_main():
    """Park effects on CONTACT RATE — the channel the batted-ball model misses.

    train-runenv only sees balls already in play, so it cannot represent a park
    that changes whether the ball is put in play at all.  That is not a corner
    case: the actual-runs backtest understates Coors by 2.4x, and BallparkPal
    document the same failure for the same reason — thin air flattens breaking
    balls, hitters strike out less, and more contact happens before ball-flight
    physics is even involved.

    Method mirrors the runs backtest.  For each team-game, predict K and BB
    rates from the batting club's ROAD-only rates and the opposing club's
    road-only rates allowed, then read the park effect off the residual.  Road
    rates because a season rate is half-earned in the park being measured, and
    would launder the very effect we want.

    The residual rates are priced with linear weights and added to the
    batted-ball channel to give a combined run environment.
    """
    _load_analysis_deps()
    import numpy as _np

    season = 2025
    for i, a in enumerate(_sys.argv):
        if a == "--season" and i + 1 < len(_sys.argv):
            season = int(_sys.argv[i + 1])

    logs = team_game_logs(season)
    from weatherman import build_game_index, resolve_park_name
    games = build_game_index(season)[["game_pk", "venue_name"]]
    logs = logs.merge(games, on="game_pk", how="inner")
    logs["park"] = [resolve_park_name(v) or v for v in logs.venue_name]
    for c in ("k", "bb", "hbp"):
        logs[c] = logs[c].fillna(0)
    logs["k_rate"] = logs.k / logs.pa
    logs["bb_rate"] = (logs.bb + logs.hbp) / logs.pa

    # Talent, measured away from the park under test.
    off = logs[~logs.is_home].groupby("team")[["k_rate", "bb_rate"]].mean()
    off.columns = ["off_k", "off_bb"]
    # Opponent pitching: rows where the BATTING side is at home means the
    # opponent is on the road, so these are that staff's road numbers.
    pit = logs[logs.is_home].groupby("opponent")[["k_rate", "bb_rate"]].mean()
    pit.columns = ["pit_k", "pit_bb"]
    lg_k, lg_bb = logs.k_rate.mean(), logs.bb_rate.mean()

    d = logs.join(off, on="team").join(pit, on="opponent").dropna(
        subset=["off_k", "pit_k", "off_bb", "pit_bb"])
    d["exp_k"] = d.off_k + d.pit_k - lg_k
    d["exp_bb"] = d.off_bb + d.pit_bb - lg_bb
    d["res_k"] = d.k_rate - d.exp_k
    d["res_bb"] = d.bb_rate - d.exp_bb

    g = d.groupby("park").agg(team_games=("res_k", "size"),
                              k_res=("res_k", "mean"), bb_res=("res_bb", "mean"))
    g = g[g.team_games >= 100]
    # SHRINKAGE, per component, from measured year-over-year reliability
    # (2024 vs 2025, 28 parks): K residual r=+0.713, BB residual r=+0.318.
    # A park's strikeout effect largely repeats and is worth trusting; its walk
    # effect mostly does not and is nearly all sampling noise at ~160 team-games
    # a season.  Using the raw estimates overshoots — it moved the actual-runs
    # coefficient from 0.771 down to 0.696, i.e. further from calibrated.
    K_RELIABILITY = 0.71
    BB_RELIABILITY = 0.32
    g["k_res_shrunk"] = g.k_res * K_RELIABILITY
    g["bb_res_shrunk"] = g.bb_res * BB_RELIABILITY

    # Per PA: a strikeout replaces an average ball in play, a walk likewise.
    bip_rv = 0.0387          # league mean E[runs | ball in play], from train-runenv
    g["contact_runs_pa"] = (g.k_res_shrunk * (PA_RUN_VALUE["K"] - bip_rv)
                            + g.bb_res_shrunk * (PA_RUN_VALUE["BB"] - bip_rv))
    g["contact_runs_game"] = g.contact_runs_pa * 76.0   # ~38 PA per side
    g = g.sort_values("contact_runs_game", ascending=False)

    print("\n" + "=" * 74)
    print(f"CONTACT-RATE PARK EFFECTS — {season}")
    print("  k_res/bb_res are rate residuals vs road-talent expectation")
    print("=" * 74)
    print(g.to_string(float_format=lambda v: f"{v:10.4f}"))

    out = os.path.join(DATA_DIR, f"contact_factors_{season}.parquet")
    g.reset_index().to_parquet(out, index=False)
    print(f"\n[save] {out}")

    # Combine with the batted-ball channel and check against real runs.
    pq = os.path.join(DATA_DIR, f"runs_added_{season}.parquet")
    if os.path.exists(pq):
        ra = pd.read_parquet(pq).dropna(subset=["game_pk"])
        bip = ra.groupby("park")["runs_added"].sum() / \
            ra.groupby("park")["game_pk"].nunique()
        comb = pd.DataFrame({"bip_runs_game": bip}).join(g[["contact_runs_game"]])
        comb["total"] = comb.bip_runs_game + comb.contact_runs_game
        comb = comb.dropna().sort_values("total", ascending=False)
        print("\nCombined run environment (runs/game vs average):")
        print(comb.to_string(float_format=lambda v: f"{v:9.3f}"))
        print(f"\n  spread: batted-ball {comb.bip_runs_game.max()-comb.bip_runs_game.min():.2f}"
              f"  contact {comb.contact_runs_game.max()-comb.contact_runs_game.min():.2f}"
              f"  combined {comb.total.max()-comb.total.min():.2f} runs/game")
        comb.reset_index().to_parquet(
            os.path.join(DATA_DIR, f"run_env_combined_{season}.parquet"), index=False)


def _tool_main(argv):
    """Dispatch the offline tools; no subcommand means launch the widget."""
    cmds = {
        "calibrate-cd": calibrate_cd_main,
        "calibrate-wind": calibrate_wind_main,
        "train-cpw": train_cpw_main,
        "train-runenv": train_run_env_main,
        "backtest-runs": backtest_runs_main,
        "contact-factors": contact_factors_main,
    }
    if len(argv) > 1 and argv[1] in cmds:
        cmds[argv[1]]()
        return True
    return False


if __name__ == "__main__":
    if not _tool_main(_sys.argv):
        main()
