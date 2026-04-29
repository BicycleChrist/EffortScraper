"""
Pitch Simulation Test — prototyping pitch trajectory for UmpireView3D

Two approaches:

1. KINEMATIC (preferred): Use Savant's vx0/vy0/vz0 + ax/ay/az directly.
   Hawk-Eye already solved the physics — constant-acceleration kinematic
   equations reconstruct the exact trajectory. No Magnus modeling needed.

2. ARCHETYPE (fallback): For slider-mode / what-if simulations where we
   don't have a specific Savant row, use pitch archetypes with the ODE solver.

Both output the same trajectory dict format that update_animation() consumes.

Usage:
    python test_pitch_simulation.py
    python test_pitch_simulation.py --player_id 592789   # fetch live from Savant
"""

import numpy as np
import argparse

# ── Savant coordinate system ─────────────────────────────────────────────
# Savant's Hawk-Eye data uses a coordinate system where:
#   x  = horizontal (catcher POV: positive toward 1B/RHP arm side)
#   y  = distance from plate (positive toward pitcher, ~50-55 ft at release)
#   z  = vertical (positive up)
#
# vx0, vy0, vz0 = velocity components at y=50 ft from plate (ft/s)
# ax, ay, az     = constant acceleration (ft/s²), bakes in gravity + drag + Magnus
#
# Our 3D view coordinate system (homerunwidget.py):
#   X  = toward center field (positive away from batter)
#   Y  = vertical (positive up)
#   Z  = toward 1B (positive) / toward 3B (negative)
#
# Mapping: Savant y → our X, Savant z → our Y, Savant x → our Z
# (Savant x positive = toward 1B / catcher's LEFT, same as our +Z)

MEASUREMENT_Y_FT = 50.0  # Savant measures vx0/vy0/vz0 at 50 ft from plate


def savant_pitch_trajectory(vx0, vy0, vz0, ax, ay, az,
                            release_pos_x, release_pos_y, release_pos_z,
                            num_points=120):
    """Reconstruct a pitch trajectory from Savant's Hawk-Eye kinematic data.

    Savant provides constant-acceleration parameters measured at y=50ft from
    plate. We integrate from the release point to home plate using simple
    kinematics: pos(t) = pos0 + v0*t + 0.5*a*t²

    Parameters (all in Savant's coordinate system, ft and ft/s):
        vx0, vy0, vz0       : velocity at y=50ft from plate
        ax, ay, az           : constant acceleration (ft/s²)
        release_pos_x/y/z   : release point coordinates
        num_points           : trajectory resolution

    Returns:
        dict compatible with update_animation():
        {time, x, y, z, vx, vy, vz, distance, start_x, start_y, start_z,
         plate_time, plate_x_ft, plate_z_ft}
        All positions/velocities in our 3D view coordinate system (feet, ft/s).
    """
    # ── Step 1: Find time offset between release and measurement point ──
    # Savant velocities are at y_savant=50 ft from plate.
    # Release is at y_savant=release_pos_y (~54-56 ft from plate).
    # We need to back-calculate to find the velocity at release.
    #
    # In Savant coords: y(t) = y_meas + vy0*t + 0.5*ay*t²
    # At release: y_savant = release_pos_y
    # At measurement: y_savant = 50
    # vy0 is negative (ball moving toward plate, y decreasing)
    #
    # Time from measurement to release (going backward):
    # release_pos_y = 50 + vy0*t_back + 0.5*ay*t_back²
    # Solve quadratic for t_back (will be negative since release is before measurement)

    dy = release_pos_y - MEASUREMENT_Y_FT  # positive (release is farther from plate)
    # 0.5*ay*t² + vy0*t - dy = 0
    a_coeff = 0.5 * ay
    b_coeff = vy0
    c_coeff = -dy

    disc = b_coeff**2 - 4 * a_coeff * c_coeff
    if disc < 0:
        disc = 0  # shouldn't happen with real data

    # We want the negative root (going backward in time from measurement point)
    t_back = (-b_coeff - np.sqrt(disc)) / (2 * a_coeff) if a_coeff != 0 else -dy / vy0

    # Velocity at release = v_meas + a * t_back
    vx_release = vx0 + ax * t_back
    vy_release = vy0 + ay * t_back
    vz_release = vz0 + az * t_back

    # ── Step 2: Integrate forward from release to plate ──
    # Find total flight time: when does y_savant reach 0 (home plate)?
    # y(t) = release_pos_y + vy_release*t + 0.5*ay*t²  = 0
    a2 = 0.5 * ay
    b2 = vy_release
    c2 = release_pos_y

    disc2 = b2**2 - 4 * a2 * c2
    if disc2 < 0:
        disc2 = 0

    # Positive root = time for ball to reach plate
    if a2 != 0:
        t_plate = (-b2 - np.sqrt(disc2)) / (2 * a2)
        if t_plate < 0:
            t_plate = (-b2 + np.sqrt(disc2)) / (2 * a2)
    else:
        t_plate = -release_pos_y / vy_release

    # Generate time array from release to just past plate
    t = np.linspace(0, t_plate, num_points)

    # Savant coordinate positions
    sx = release_pos_x + vx_release * t + 0.5 * ax * t**2
    sy = release_pos_y + vy_release * t + 0.5 * ay * t**2
    sz = release_pos_z + vz_release * t + 0.5 * az * t**2

    # Savant coordinate velocities
    svx = vx_release + ax * t
    svy = vy_release + ay * t
    svz = vz_release + az * t

    # ── Step 3: Convert to our 3D view coordinate system ──
    # Savant y (distance from plate, positive toward pitcher) → our X (toward CF)
    # Savant z (vertical, positive up) → our Y (up)
    # Savant x (horizontal, positive toward 1B from catcher) → our Z (positive toward 1B)
    #   So: our_Z = savant_x (same direction, no flip needed)
    out_x = sy          # distance from plate
    out_y = sz          # height
    out_z = sx          # horizontal (1B positive in both systems)

    out_vx = svy
    out_vy = svz
    out_vz = svx

    # Plate crossing values (last point)
    plate_x_ft = sx[-1]   # horizontal location at plate (Savant convention, for zone overlay)
    plate_z_ft = sz[-1]   # height at plate

    return {
        "time": t,
        "x": out_x,
        "y": out_y,
        "z": out_z,
        "vx": out_vx,
        "vy": out_vy,
        "vz": out_vz,
        "distance": float(release_pos_y),
        "start_x": float(out_x[0]),
        "start_y": float(out_y[0]),
        "start_z": float(out_z[0]),
        # Pitch-specific extras
        "plate_time": float(t_plate),
        "plate_x_ft": float(plate_x_ft),   # Savant plate_x (horizontal, for zone)
        "plate_z_ft": float(plate_z_ft),    # Savant plate_z (height, for zone)
        "plate_speed_mph": float(np.sqrt(svx[-1]**2 + svy[-1]**2 + svz[-1]**2) * 0.681818),
    }


def savant_row_to_pitch_trajectory(row, num_points=120):
    """Convenience: extract Savant columns from a BBE row dict/Series and build trajectory.

    Works with both a pandas Series (from the BBE DataFrame) or a plain dict.
    Falls back gracefully if kinematic fields are missing.
    """
    required = ["vx0", "vy0", "vz0", "ax", "ay", "az",
                "release_pos_x", "release_pos_y", "release_pos_z"]

    # Check for missing data
    for col in required:
        val = row.get(col) if isinstance(row, dict) else getattr(row, col, None)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return None  # can't build trajectory without kinematic data

    def g(key):
        return float(row.get(key) if isinstance(row, dict) else getattr(row, key))

    return savant_pitch_trajectory(
        vx0=g("vx0"), vy0=g("vy0"), vz0=g("vz0"),
        ax=g("ax"), ay=g("ay"), az=g("az"),
        release_pos_x=g("release_pos_x"),
        release_pos_y=g("release_pos_y"),
        release_pos_z=g("release_pos_z"),
        num_points=num_points,
    )


# ── Test with hardcoded Savant data ──────────────────────────────────────
# Real pitch from a Savant CSV row (typical RHP 4-seam fastball)

SAMPLE_PITCHES = {
    "FF_sample": {
        "pitch_name": "4-Seam Fastball",
        "release_speed": 95.1,
        "release_pos_x": -2.14,
        "release_pos_y": 54.12,
        "release_pos_z": 5.72,
        "vx0": 7.92,
        "vy0": -138.42,
        "vz0": -5.36,
        "ax": -14.12,
        "ay": 31.58,
        "az": -15.18,
        "pfx_x": -8.2,
        "pfx_z": 15.8,
        "plate_x": -0.42,
        "plate_z": 2.85,
        "release_spin_rate": 2312,
        "spin_axis": 213,
    },
    "SL_sample": {
        "pitch_name": "Slider",
        "release_speed": 86.3,
        "release_pos_x": -1.89,
        "release_pos_y": 54.45,
        "release_pos_z": 5.68,
        "vx0": 3.15,
        "vy0": -125.82,
        "vz0": -2.45,
        "ax": 3.62,
        "ay": 27.42,
        "az": -28.85,
        "pfx_x": 3.8,
        "pfx_z": 2.1,
        "plate_x": 0.35,
        "plate_z": 1.82,
        "release_spin_rate": 2485,
        "spin_axis": 52,
    },
    "CU_sample": {
        "pitch_name": "Curveball",
        "release_speed": 80.2,
        "release_pos_x": -1.75,
        "release_pos_y": 54.89,
        "release_pos_z": 5.91,
        "vx0": 1.82,
        "vy0": -116.85,
        "vz0": -1.15,
        "ax": 5.42,
        "ay": 24.18,
        "az": -38.52,
        "pfx_x": 5.2,
        "pfx_z": -7.5,
        "plate_x": 0.18,
        "plate_z": 1.25,
        "release_spin_rate": 2680,
        "spin_axis": 28,
    },
    "CH_sample": {
        "pitch_name": "Changeup",
        "release_speed": 84.8,
        "release_pos_x": -2.25,
        "release_pos_y": 53.95,
        "release_pos_z": 5.45,
        "vx0": 12.35,
        "vy0": -123.42,
        "vz0": -7.82,
        "ax": -22.15,
        "ay": 28.85,
        "az": -20.52,
        "pfx_x": -13.8,
        "pfx_z": 9.2,
        "plate_x": -0.85,
        "plate_z": 2.15,
        "release_spin_rate": 1720,
        "spin_axis": 238,
    },
}


# ── Tests ─────────────────────────────────────────────────────────────────

def test_kinematic_trajectories():
    """Build trajectories from sample Savant kinematic data and verify sanity."""
    print("=" * 100)
    print("KINEMATIC PITCH TRAJECTORIES (from Savant Hawk-Eye data)")
    print("=" * 100)
    print(f"{'Pitch':<18} {'Velo':>5} {'Spin':>5} │ {'Time':>6} {'PlateV':>7} "
          f"{'Ht@Plt':>7} {'Side@P':>7} │ {'pfx_x':>6} {'pfx_z':>6} │ {'Points':>6}")
    print("-" * 100)

    for key, row in SAMPLE_PITCHES.items():
        traj = savant_row_to_pitch_trajectory(row)
        if traj is None:
            print(f"{key:<18} FAILED — missing kinematic data")
            continue

        print(f"{row['pitch_name']:<18} {row['release_speed']:>5.1f} {row['release_spin_rate']:>5d} │ "
              f"{traj['plate_time']:>6.3f} {traj['plate_speed_mph']:>7.1f} "
              f"{traj['plate_z_ft']:>7.2f} {traj['plate_x_ft']:>7.2f} │ "
              f"{row['pfx_x']:>6.1f} {row['pfx_z']:>6.1f} │ "
              f"{len(traj['time']):>6d}")

        # Sanity checks
        errors = []
        if not (0.35 < traj["plate_time"] < 0.55):
            errors.append(f"plate_time={traj['plate_time']:.3f}s (expected 0.35-0.55)")
        if not (70 < traj["plate_speed_mph"] < 100):
            errors.append(f"plate_speed={traj['plate_speed_mph']:.1f}mph")
        if not (0.5 < traj["plate_z_ft"] < 5.0):
            errors.append(f"plate_height={traj['plate_z_ft']:.2f}ft (expected 0.5-5.0)")

        # Check plate location matches Savant's plate_x/plate_z
        plate_x_err = abs(traj["plate_x_ft"] - row["plate_x"])
        plate_z_err = abs(traj["plate_z_ft"] - row["plate_z"])
        if plate_x_err > 0.5:
            errors.append(f"plate_x off by {plate_x_err:.2f}ft")
        if plate_z_err > 0.5:
            errors.append(f"plate_z off by {plate_z_err:.2f}ft")

        if errors:
            print(f"  WARN: {', '.join(errors)}")

    print("=" * 100)


def test_trajectory_detail():
    """Print detailed trajectory for the fastball sample."""
    row = SAMPLE_PITCHES["FF_sample"]
    traj = savant_row_to_pitch_trajectory(row)

    print(f"\n{row['pitch_name']} — Detailed Kinematic Trajectory")
    print("=" * 75)
    print(f"Release: ({traj['start_x']:.1f}, {traj['start_y']:.1f}, {traj['start_z']:.1f}) ft [our coords]")
    print(f"Savant release: ({row['release_pos_x']:.2f}, {row['release_pos_y']:.2f}, {row['release_pos_z']:.2f}) ft")
    print(f"Plate time:   {traj['plate_time']:.4f} s")
    print(f"Plate speed:  {traj['plate_speed_mph']:.1f} mph (release: {row['release_speed']:.1f})")
    print(f"Plate loc:    x={traj['plate_x_ft']:.2f} ft, z={traj['plate_z_ft']:.2f} ft")
    print(f"  (Savant:    x={row['plate_x']:.2f} ft, z={row['plate_z']:.2f} ft)")
    print(f"Movement:     pfx_x={row['pfx_x']:.1f} in, pfx_z={row['pfx_z']:.1f} in")
    print(f"Points:       {len(traj['time'])}")

    # Print every ~8th point
    step = max(1, len(traj['time']) // 15)
    print(f"\n{'t':>6} {'X(CF)':>7} {'Y(up)':>7} {'Z(1B)':>7} {'Speed':>6}")
    print(f"{'s':>6} {'ft':>7} {'ft':>7} {'ft':>7} {'mph':>6}")
    print("-" * 40)
    for i in range(0, len(traj['time']), step):
        speed = np.sqrt(traj['vx'][i]**2 + traj['vy'][i]**2 + traj['vz'][i]**2) * 0.681818
        print(f"{traj['time'][i]:>6.4f} {traj['x'][i]:>7.1f} {traj['y'][i]:>7.2f} "
              f"{traj['z'][i]:>7.2f} {speed:>6.1f}")


def test_format_compat():
    """Verify output dict has all keys that update_animation() expects."""
    row = SAMPLE_PITCHES["FF_sample"]
    traj = savant_row_to_pitch_trajectory(row)

    required_keys = ["time", "x", "y", "z", "vx", "vy", "vz",
                     "distance", "start_x", "start_y", "start_z"]
    missing = [k for k in required_keys if k not in traj]
    if missing:
        print(f"FAIL — missing keys: {missing}")
    else:
        print("PASS — trajectory dict compatible with update_animation()")

    lengths = {k: len(traj[k]) for k in required_keys if hasattr(traj[k], '__len__')}
    unique_lengths = set(lengths.values())
    if len(unique_lengths) == 1:
        print(f"PASS — all arrays length {unique_lengths.pop()}")
    else:
        print(f"FAIL — array length mismatch: {lengths}")

    # Verify trajectory goes from ~55ft to ~0ft in X (distance from plate)
    print(f"PASS — X range: {traj['x'][0]:.1f} → {traj['x'][-1]:.1f} ft" if traj['x'][0] > 40 and traj['x'][-1] < 2
          else f"WARN — X range unexpected: {traj['x'][0]:.1f} → {traj['x'][-1]:.1f}")


def test_live_fetch():
    """Fetch real BBE data from Savant for a player and build pitch trajectories.
    Requires the expanded BBE_KEEP_COLS in savant_bbe_fetch.py."""
    import io
    import requests

    parser = argparse.ArgumentParser()
    parser.add_argument("--player_id", type=int, default=None)
    args, _ = parser.parse_known_args()

    if args.player_id is None:
        print("\nSkipping live fetch test (pass --player_id to enable)")
        return

    print(f"\nFetching live Savant data for player {args.player_id}...")

    url = (
        f"https://baseballsavant.mlb.com/statcast_search/csv"
        f"?hfPT=&hfAB=&hfGT=R%7C&hfPR=&hfZ=&hfStadium=&hfBBL=&hfNewZones=&hfPull="
        f"&hfC=&hfSea=2025%7C&hfSit=&player_type=batter"
        f"&hfOuts=&hfOpponent=&pitcher_throws=&batter_stands=&hfSA=&min_pitches=0"
        f"&min_results=0&group_by=name&sort_col=pitches&player_event_sort=api_p_release_speed"
        f"&sort_order=desc&min_abs=0&type=details&player_id={args.player_id}"
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Referer": "https://baseballsavant.mlb.com/",
    }

    import pandas as pd
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))

    # Filter to pitches with kinematic data
    kinematic_cols = ["vx0", "vy0", "vz0", "ax", "ay", "az",
                      "release_pos_x", "release_pos_y", "release_pos_z"]
    has_data = df.dropna(subset=kinematic_cols)

    print(f"Total rows: {len(df)}, with kinematic data: {len(has_data)}")

    if has_data.empty:
        print("No kinematic data available")
        return

    # Show a few pitch trajectories
    print(f"\n{'Date':<12} {'Pitch':<15} {'Velo':>5} {'pfx_x':>6} {'pfx_z':>6} │ "
          f"{'Time':>6} {'PlateV':>7} {'Plt_x':>6} {'Plt_z':>6}")
    print("-" * 90)

    for _, row in has_data.head(10).iterrows():
        traj = savant_row_to_pitch_trajectory(row)
        if traj is None:
            continue

        pitch_name = str(row.get("pitch_name", "?"))[:14]
        pfx_x = row.get("pfx_x", 0) or 0
        pfx_z = row.get("pfx_z", 0) or 0

        print(f"{str(row.get('game_date', '')):<12} {pitch_name:<15} "
              f"{row.get('release_speed', 0):>5.1f} {pfx_x:>6.1f} {pfx_z:>6.1f} │ "
              f"{traj['plate_time']:>6.3f} {traj['plate_speed_mph']:>7.1f} "
              f"{traj['plate_x_ft']:>6.2f} {traj['plate_z_ft']:>6.2f}")


if __name__ == "__main__":
    test_format_compat()
    test_kinematic_trajectories()
    test_trajectory_detail()
    test_live_fetch()
