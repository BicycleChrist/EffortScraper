import pathlib

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QWidget,
    QPushButton, QSlider, QLabel, QComboBox, QGroupBox, QSpinBox, QCheckBox,
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsPathItem,
    QGraphicsItemGroup, QGraphicsLineItem, QGraphicsRectItem, QGridLayout, QListWidget, QDoubleSpinBox, QTabWidget,
    QSizePolicy
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
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, QSizeF, QPoint, pyqtSignal, QPropertyAnimation, QEasingCurve
import sys
import random
import numpy as np
import math
from scipy.integrate import solve_ivp
from weatherman import WeatherService, STADIUM_DATA, get_stadium_wall_distance, WindVectorWidget
from player_overlay_widget import PlayerBBEOverlay
from weatherman import open_weather_key
from pathlib import Path
from pywavefront import Wavefront
import csv
from test_pitch_simulation import savant_row_to_pitch_trajectory

LOG_BALL_PHYSICS = False

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
        self.C_d = 0.3  # drag coefficient
        self.C_l = 0.2  # lift coefficient (for Magnus effect)
        self.omega = 1800  # rpm, typical spin rate

    # Stadiums at or above this elevation trigger the high-altitude pressure
    # correction described above.  1500 ft is the point where the SLP error
    # (~4 % density impact) starts producing a noticeable difference in carry.
    HIGH_ALTITUDE_THRESHOLD_FT = 1500

    def calculate_trajectory(self, exit_velocity, vlaunch_angle, hlaunch_angle, wind_speed, wind_direction,
                             temp, humidity, altitude, start_x=0, start_y=0.91, start_z=0,
                             pressure_pa=None):
        """Calculate ball trajectory based on initial conditions and environment

        Standard coordinate system:
        - X axis: From home plate toward pitcher's mound/center field (positive)
        - Y axis: Vertical (up is positive)
        - Z axis: From home plate toward third base/left field (negative) or first base/right field (positive)
        """
        # Convert inputs to SI units
        v0 = exit_velocity * 0.44704  # mph to m/s
        vertical_angle = np.radians(vlaunch_angle)
        horizontal_angle = np.radians(hlaunch_angle)
        wind = wind_speed * 0.44704  # mph to m/s
        wind_rad = np.radians(wind_direction)

        # ----------------------------------------------------------------
        # High-altitude correction
        # OpenWeather's main.pressure field is sea-level-normalised (SLP).
        # At stadiums above the threshold this diverges significantly from
        # actual station pressure, so we derive station pressure from the
        # ISA formula instead — it is more accurate than SLP for physics.
        # ----------------------------------------------------------------
        corrected_pressure_pa = pressure_pa
        if altitude >= self.HIGH_ALTITUDE_THRESHOLD_FT:
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

        solution = solve_ivp(
            lambda t, y: self.baseball_ode(t, y, wind, wind_rad, rho_fn),
            t_span,
            initial_state,
            method='RK45',
            max_step=0.05,
            rtol=1e-6,
            atol=1e-6,
            first_step=0.01
        )

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

        # Calculate total horizontal distance
        distance = np.sqrt(x[landing_idx]**2 + z[landing_idx]**2)

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

    def baseball_ode(self, t, state, wind_speed, wind_direction, rho_fn):
        """ODE system for baseball flight with height-varying air density.

        Parameters:
        t (float): Time variable for time-dependent forces
        state (array): Current state [x, y, z, vx, vy, vz]
        wind_speed (float): Wind speed in m/s
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

        # Wind components with time-dependent variation (like gusts)
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
                boundary_item.setBrush(QBrush())
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
                field_boundary = QPainterPath()
                home_plate_point = QPointF(home_plate_x, home_plate_y)

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
            # Hand the model to the main thread.  The GIL makes the reference
            # assignment atomic.  paintGL will lazy-compile the display list
            # on its next frame since it's already in a valid GL context.
            self.ballpark_model = model
            self._model_load_pending = False
            self.update()   # schedule a repaint
        except Exception as e:
            print(f"[model] error loading OBJ: {e}")
            self._model_load_pending = False

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

        # Initialize stadium display list (compiled when model finishes loading)
        self.stadium_display_list = None
        if self.ballpark_model:
            self.compile_stadium_display_list()

        # Set up procedural sky shader
        self._setup_sky_shader()

        # Set up per-pixel stadium lighting shader (used in night mode)
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

        self._draw_outfield_geometry()

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
        if not self.ballpark_model:
            return

        # Generate a new display list
        self.stadium_display_list = glGenLists(1)
        glNewList(self.stadium_display_list, GL_COMPILE)

        # Basic model transformations
        glPushMatrix()
        glScalef(1.0, 1.0, 1.0)
        vertices = self.ballpark_model.vertices

        # Process each mesh with its own material based on new names
        for mesh_index, mesh in enumerate(self.ballpark_model.mesh_list):
            # Get material name for this mesh
            material_name = None
            if hasattr(mesh, 'materials') and mesh.materials and len(mesh.materials) > 0:
                material = mesh.materials[0]
                if isinstance(material, str):
                    material_name = material
                elif hasattr(material, 'name'):
                    material_name = material.name

            # Get mesh name if available (should be available with new Blender export)
            mesh_name = ""
            if hasattr(mesh, 'name'):
                mesh_name = mesh.name

            print(f"Processing mesh {mesh_index}: {mesh_name} with material: {material_name}")

            # Apply materials based on mesh name
            if "Infield" in mesh_name or mesh_name == "Infield":
                # Dirt infield
                glMaterialfv(GL_FRONT, GL_AMBIENT, [0.2, 0.15, 0.1, 1.0])
                glMaterialfv(GL_FRONT, GL_DIFFUSE, [0.76, 0.46, 0.25, 1.0])
                glMaterialfv(GL_FRONT, GL_SPECULAR, [0.4, 0.3, 0.2, 1.0])
                glMaterialf(GL_FRONT, GL_SHININESS, 64.0)
                print(f"Applied dirt material to infield")

            elif "outfield" in mesh_name or mesh_name == "outfield":
                # Green grass outfield
                glMaterialfv(GL_FRONT, GL_AMBIENT, [0.05, 0.2, 0.05, 1.0])
                glMaterialfv(GL_FRONT, GL_DIFFUSE, [0.1, 0.6, 0.1, 1.0])
                glMaterialfv(GL_FRONT, GL_SPECULAR, [0.1, 0.4, 0.1, 1.0])
                glMaterialf(GL_FRONT, GL_SHININESS, 12.0)
                print(f"Applied grass material to outfield")

            elif "EffortText" in mesh_name or mesh_name == "EffortText":
                # Nearly transparent material for EffortText
                glMaterialfv(GL_FRONT, GL_AMBIENT, [1.0, 1.0, 1.0, 0.1])
                glMaterialfv(GL_FRONT, GL_DIFFUSE, [1.0, 1.0, 1.0, 0.1])
                glMaterialfv(GL_FRONT, GL_SPECULAR, [1.0, 1.0, 1.0, 0.1])
                glMaterialfv(GL_FRONT, GL_EMISSION, [0.15, 0.05, 0.05, 0.05])  # No emission
                glMaterialf(GL_FRONT, GL_SHININESS, 128.0)
                # glDisable(GL_BLEND)

            elif "homeplate" in mesh_name:
                # White for home plate
                glMaterialfv(GL_FRONT, GL_AMBIENT, [0.3, 0.3, 0.3, 1.0])
                glMaterialfv(GL_FRONT, GL_DIFFUSE, [0.95, 0.95, 0.95, 1.0])
                glMaterialfv(GL_FRONT, GL_SPECULAR, [0.8, 0.8, 0.8, 1.0])
                glMaterialf(GL_FRONT, GL_SHININESS, 96.0)
                print(f"Applied white material to homeplate")

            elif "pitchersmound" in mesh_name:
                # Slightly different dirt color for pitcher's mound
                glMaterialfv(GL_FRONT, GL_AMBIENT, [0.22, 0.17, 0.12, 1.0])
                glMaterialfv(GL_FRONT, GL_DIFFUSE, [0.7, 0.4, 0.2, 1.0])
                glMaterialfv(GL_FRONT, GL_SPECULAR, [0.4, 0.3, 0.2, 1.0])
                glMaterialf(GL_FRONT, GL_SHININESS, 32.0)
                print(f"Applied mound material to pitchersmound")

            elif "Dugout" in mesh_name or "dugout" in mesh_name:
                # Gray concrete for dugouts
                glMaterialfv(GL_FRONT, GL_AMBIENT, [0.2, 0.2, 0.2, 1.0])
                glMaterialfv(GL_FRONT, GL_DIFFUSE, [0.6, 0.6, 0.6, 1.0])
                glMaterialfv(GL_FRONT, GL_SPECULAR, [0.3, 0.3, 0.3, 1.0])
                glMaterialf(GL_FRONT, GL_SHININESS, 48.0)
                print(f"Applied concrete material to dugout")

            elif "Graffiti" in mesh_name:
                # Graffiti wall - you could use a texture here in the future
                glMaterialfv(GL_FRONT, GL_AMBIENT, [0.2, 0.2, 0.2, 1.0])
                glMaterialfv(GL_FRONT, GL_DIFFUSE, [0.8, 0.8, 0.8, 1.0])
                glMaterialfv(GL_FRONT, GL_SPECULAR, [0.3, 0.3, 0.3, 1.0])
                glMaterialf(GL_FRONT, GL_SHININESS, 8.0)
                print(f"Applied wall material to Graffiti Wall")

            elif "Cylinder" in mesh_name or "Box" in mesh_name:
                # Stadium structures - light blue/gray
                glMaterialfv(GL_FRONT, GL_AMBIENT, [0.2, 0.2, 0.25, 1.0])
                glMaterialfv(GL_FRONT, GL_DIFFUSE, [0.5, 0.5, 0.6, 1.0])
                glMaterialfv(GL_FRONT, GL_SPECULAR, [0.3, 0.3, 0.4, 1.0])
                glMaterialf(GL_FRONT, GL_SHININESS, 32.0)
                print(f"Applied structure material to {mesh_name}")

            else:
                # Default white material for unrecognized meshes
                glMaterialfv(GL_FRONT, GL_AMBIENT, [0.2, 0.2, 0.2, 1.0])
                glMaterialfv(GL_FRONT, GL_DIFFUSE, [0.8, 0.8, 0.8, 1.0])
                glMaterialfv(GL_FRONT, GL_SPECULAR, [0.5, 0.5, 0.5, 1.0])
                glMaterialf(GL_FRONT, GL_SHININESS, 32.0)
                print(f"Applied default material to {mesh_name}")

            # Draw the triangles for this mesh with per-face normals
            glBegin(GL_TRIANGLES)
            for face in mesh.faces:
                if len(face) >= 3:
                    v0, v1, v2 = vertices[face[0]], vertices[face[1]], vertices[face[2]]
                    e1 = (v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2])
                    e2 = (v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2])
                    nx = e1[1]*e2[2] - e1[2]*e2[1]
                    ny = e1[2]*e2[0] - e1[0]*e2[2]
                    nz = e1[0]*e2[1] - e1[1]*e2[0]
                    if nx or ny or nz:
                        glNormal3f(nx, ny, nz)
                    glVertex3f(*v0); glVertex3f(*v1); glVertex3f(*v2)
            glEnd()

        glPopMatrix()
        glEndList()
        print("✅ Stadium model compiled into display list with materials based on mesh names")

    def _draw_outfield_geometry(self):
        """Draw procedural outfield geometry: ground plane, wall, warning track, foul lines, foul poles.
        All coordinates derived from STADIUM_DATA wall distances via physics_to_model().
        """
        if not self._stadium_name:
            return

        # --- shared constants ---
        WALL_HEIGHT_FT = 8.0          # default outfield wall height (feet)
        POLE_HEIGHT_FT = 30.0         # foul pole height (feet)
        POLE_RADIUS_M  = 0.07         # foul pole radius in meters (~3 inches)
        WARN_TRACK_FT  = 20.0         # warning track depth (feet in front of wall)
        GROUND_Y_M     = 0.0          # physics Y of ground level
        WALL_H_M       = WALL_HEIGHT_FT / 3.28084
        POLE_H_M       = POLE_HEIGHT_FT / 3.28084
        FT_TO_M        = 1.0 / 3.28084

        # ------------------------------------------------------------------ #
        # Build wall point list: one entry per integer polar angle 0..90      #
        # Each entry: (bx,by,bz, tx,ty,tz, dist_ft, phys_x_m, phys_z_m)    #
        #             or None if no data                                      #
        # ------------------------------------------------------------------ #
        polar_angles = list(range(0, 91))   # 0..90 inclusive, 1° steps
        wall_pts = []

        for theta in polar_angles:
            dist_ft = get_stadium_wall_distance(self._stadium_name, theta)
            if dist_ft is None or dist_ft <= 0:
                wall_pts.append(None)
                continue
            phys_angle = math.radians(theta + 45)
            x_m = dist_ft * math.sin(phys_angle) * FT_TO_M
            z_m = dist_ft * math.cos(phys_angle) * FT_TO_M
            bx, by, bz = self.physics_to_model(x_m, GROUND_Y_M, z_m)
            tx, ty, tz = self.physics_to_model(x_m, WALL_H_M,   z_m)
            wall_pts.append((bx, by, bz, tx, ty, tz, dist_ft, x_m, z_m))

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
            pa0 = math.radians(theta0 + 45);  pa1 = math.radians(theta1 + 45)
            ox0_m = (dist0 + CAP_DEPTH_FT) * math.sin(pa0) * FT_TO_M
            oz0_m = (dist0 + CAP_DEPTH_FT) * math.cos(pa0) * FT_TO_M
            ox1_m = (dist1 + CAP_DEPTH_FT) * math.sin(pa1) * FT_TO_M
            oz1_m = (dist1 + CAP_DEPTH_FT) * math.cos(pa1) * FT_TO_M
            otx0, oty0, otz0 = self.physics_to_model(ox0_m, WALL_H_M, oz0_m)
            otx1, oty1, otz1 = self.physics_to_model(ox1_m, WALL_H_M, oz1_m)

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
        self.stadium_pixmap = QPixmap(stadium_image_path)
        self.lat = lat
        self.lon = lon
        self.altitude = altitude
        self.dimensions = None
        self.stadium_name = ""

        # Weather and simulation data
        self.weather_data = None
        self.trajectory_data = None
        self.current_frame = 0

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

        # Fetch initial weather data
        self.fetch_weather_data()






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

            # 3D View — pass stadium name then repaint
            self.umpire_view._stadium_name = stadium_name
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

    def set_custom_weather(self, wind_speed, wind_direction):
        """Set custom weather data for simulation"""
        self.weather_data = {
            "wind_speed": wind_speed,
            "wind_direction": wind_direction,
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
            pressure_pa=self.weather_data.get("pressure_pa", None)
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

        # Update the flight info label
        self.flight_info_label.setText(
            f"Distance: {distance:.1f} ft | Max Height: {max_height:.1f} ft | "
            f"Exit Vel: {exit_velocity} mph | Launch: {vlaunch_angle}°/{hlaunch_angle}° | Spin: {spin_rate} rpm"
            f" | {hit_result}"
        )

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


        # Typical MLB wall height is ~8 ft; use that if we found a crossing
        wall_height = 8
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

        return distance >= wall_distance and final_height > 8


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


if __name__ == "__main__":
    main()
