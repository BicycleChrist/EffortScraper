from PyQt6.QtGui import (QPixmap, QPainter, QColor, QPen, QBrush, QPainterPath,
                         QLinearGradient, QRadialGradient, QPolygonF)
from PyQt6.QtCore import Qt, QTimer, QPointF
from PyQt6.QtWidgets import QWidget
import datetime as _dt
import hashlib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import random
import time
import numpy as np
import requests
from Creds import open_weather_key

# Helper function for polar coordinate equations
def polar_equation(angle_deg, coefficients):
    """
    Evaluate polar coordinate equation for stadium outfield wall
    angle_deg: angle in degrees from right field foul line
    coefficients: Can be one of:
        - (numerator, sin_coeff, cos_coeff) for simple equations
        - (numerator, sin_coeff, cos_coeff, constant) for equations with constant term
        - A string describing a complex equation
        - A dictionary with 'type' key for special equations
    """
    theta = math.radians(angle_deg)

    # Handle string descriptions of complex equations
    if isinstance(coefficients, str):
        if "Complex" in coefficients:
            # For now, return a default distance for complex equations
            # These would need individual implementations
            return 400  # Default distance
        return None

    # Handle dictionary format for special equations
    if isinstance(coefficients, dict):
        eq_type = coefficients.get('type')

        if eq_type == 'cos_only':
            # r = numerator / cos(θ)
            cos_val = math.cos(theta)
            if abs(cos_val) < 1e-10:
                return float('inf')
            return coefficients['numerator'] / cos_val

        elif eq_type == 'sin_only':
            # r = numerator / sin(θ)
            sin_val = math.sin(theta)
            if abs(sin_val) < 1e-10:
                return float('inf')
            return coefficients['numerator'] / sin_val

        elif eq_type == 'special_cos':
            # r = numerator * cos(θ) / denominator
            return coefficients['numerator'] * math.cos(theta) / coefficients['denominator']


        # Complex equation for Great American Ball Park (0-44.7 degrees)
        elif eq_type == 'complex_great_american':
            num1 = 11951552.5 * math.cos(theta - math.radians(25.2)) - (8986447.5 * math.cos(theta - math.radians(164.8)))
            num2 = 19212.09 * math.sqrt(41212.25 - (30987.75 * math.cos(2*theta - math.radians(190))) - 168200 * (math.sin(theta - math.radians(25.2))**2))
            numerator = num1 + num2

            denominator = 41212.25 - (30987.75 * math.cos(2*theta - math.radians(190)))
            return numerator / denominator

        # Complex equation for Minute Maid Park (34-49.6 degrees)
        # Current function for Minute Maid was made when Tals Hill was still a part of the field
        elif eq_type == 'complex_minute_maid':
            numerator1 = (3820575 * math.cos(theta - math.radians(42.5))) - (263175* math.cos(theta + math.radians(42.5)))
            numerator2 = 7424.6 * math.sqrt(10525 - (725 * math.cos(2 * theta)) - 263538* math.sin(theta - math.radians(42.5)) ** 2)
            denominator = 10525 - 725*math.cos(2*theta)
            return (numerator1+numerator2) / denominator

        # Complex equation for Kauffman Stadium (0-10.9 degrees) - from Image 3
        elif eq_type == 'complex_kauffman1':
            num1 = 1738857 * math.cos(theta - math.radians(10.1)) - 495945 * math.cos(theta - math.radians(169.9))
            num2 = 3671.3 * math.sqrt(5417 - 1545 * math.cos(2*theta - math.radians(180)) - (206082 * (math.sin(theta - math.radians(10.1)) ** 2)))
            numerator = num1 + num2

            denominator = 5417 - 1545 * math.cos(2*theta - math.radians(180))
            return numerator / denominator

        # (22.1-59) degrees
        elif eq_type == 'complex_kauffman2':
            num1 = 19759218 * math.cos(theta - math.radians(50.9)) - 1837968 * math.cos(theta + math.radians(76.9))
            num2 = 78594.9 * math.sqrt(111634 - 10384*math.cos(2*theta + math.radians(26)) - (62658 * (math.sin(theta - math.radians(50.9))**2)))
            numerator = num1 + num2
            denominator = 111634 - (10384 * math.cos(2*theta + math.radians(26)))
            return numerator / denominator

        # (59-76.9) degrees
        elif eq_type == 'complex_kauffman3':
            num1 = 5642864 * math.cos(theta - math.radians(68.7)) - 4885920 * math.cos(theta + math.radians(80.7))
            num2 = 5740.3 * math.sqrt(16218 - (14040 * math.cos(2*theta + math.radians(12))) - (242208*(math.sin(theta - math.radians(68.7))**2)))
            numerator = num1 + num2
            denominator = 16218 - (14040 * math.cos(2*theta + math.radians(12)))
            return numerator / denominator

        # (82.7-90) degrees
        elif eq_type == 'complex_kauffman4':
            num1 = 958907 * math.cos(theta - math.radians(82.6)) - 322725 * math.cos(theta + math.radians(44.6))
            num2 = 1929 * math.sqrt(2897 - (975*math.cos(2*theta - math.radians(38))) - (219122 * (math.sin(theta - math.radians(82.6)) ** 2)))
            numerator = num1 + num2
            denominator = 2897 - (975 * math.cos(2*theta - math.radians(38)))
            return numerator / denominator

        elif eq_type == 'complex_wrigley':
            # Common denominator
            common_denom = 33526.25 - 9105.75 * math.cos(2 * theta - math.radians(180))

            # First fraction numerator
            num1 = 9353823.75 * math.cos(theta - math.radians(33.2)) - 2540504.25 * math.cos(theta - math.radians(146.8))
            num2 = 22815.51 * math.sqrt(33526.25 - (9105.75 * math.cos(2 * theta - math.radians(180))) - (155682 * (math.sin(theta - math.radians(33.2)))** 2))
            return (num1 + num2)/common_denom

        elif eq_type == 'constant':
            # r = fixed value regardless of angle
            return coefficients['value']

        elif eq_type == 'interpolated':
            # Linear interpolation over a data table of (theta_deg, r) pairs
            data = coefficients['data']
            for i in range(len(data) - 1):
                t1, r1 = data[i]
                t2, r2 = data[i + 1]
                if t1 <= angle_deg <= t2:
                    frac = (angle_deg - t1) / (t2 - t1)
                    return r1 + frac * (r2 - r1)
            return None


    # Handle tuple format
    if len(coefficients) == 3:
        # Simple format: r = numerator / (sin(θ) + cos_coeff * cos(θ))
        numerator, sin_coeff, cos_coeff = coefficients
        denominator = sin_coeff * math.sin(theta) + cos_coeff * math.cos(theta)

        if abs(denominator) < 1e-10:  # Avoid division by zero
            return float('inf')

        return numerator / denominator

    elif len(coefficients) == 4:
        # Format with constant: r = numerator / (sin(θ) + cos_coeff * cos(θ) + constant)
        numerator, sin_coeff, cos_coeff, constant = coefficients
        denominator = sin_coeff * math.sin(theta) + cos_coeff * math.cos(theta) + constant

        if abs(denominator) < 1e-10:
            return float('inf')

        return numerator / denominator

    return None



def get_stadium_wall_distance(stadium_name, angle_deg):
    if stadium_name not in STADIUM_DATA:
        return None

    stadium = STADIUM_DATA[stadium_name]
    if "polar_coords" not in stadium:
        return None

    for angle_start, angle_end, coefficients in stadium["polar_coords"]:
        if angle_start <= angle_deg <= angle_end:
            result = polar_equation(angle_deg, coefficients)
            if result is None or result <= 0 or result == float('inf') or result > 600:
                return None
            return result

    # Gap in coverage — interpolate from boundary of nearest segments
    polar_coords = stadium["polar_coords"]
    best_below = None
    best_above = None

    for angle_start, angle_end, coefficients in polar_coords:
        if angle_end <= angle_deg:
            best_below = (angle_end, coefficients)
        if angle_start >= angle_deg and best_above is None:
            best_above = (angle_start, coefficients)

    if best_below and best_above:
        below_angle, below_coeff = best_below
        above_angle, above_coeff = best_above
        d_below = polar_equation(below_angle, below_coeff)
        d_above = polar_equation(above_angle, above_coeff)
        if d_below and d_above and 0 < d_below < 600 and 0 < d_above < 600:
            t = (angle_deg - below_angle) / (above_angle - below_angle)
            return d_below + t * (d_above - d_below)
    if best_below:
        d = polar_equation(best_below[0], best_below[1])
        return d if d and 0 < d < 600 else None
    if best_above:
        d = polar_equation(best_above[0], best_above[1])
        return d if d and 0 < d < 600 else None

    return None


def get_stadium_wall_height(stadium_name, angle_deg):
    """Return wall height in feet at the given stadium polar angle (0°=RF, 90°=LF).

    Walks the wall_heights segments for the stadium and returns the constant
    height for the segment containing angle_deg.  Falls back to 8.0 ft if the
    stadium has no wall_heights data or the angle falls in a gap.
    """
    if stadium_name not in STADIUM_DATA:
        return 8.0

    stadium = STADIUM_DATA[stadium_name]
    wall_heights = stadium.get("wall_heights")
    if not wall_heights:
        return 8.0

    # Find the segment containing this angle
    best_dist = None
    best_height = 8.0
    for angle_start, angle_end, height_ft in wall_heights:
        if angle_start <= angle_deg <= angle_end:
            return height_ft
        # Track nearest segment in case of gap
        dist = min(abs(angle_deg - angle_start), abs(angle_deg - angle_end))
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_height = height_ft

    return best_height


# ==============================================
# Stadium Data
# ==============================================
STADIUM_DATA = {
    "American Family Field": {  # Milwaukee Brewers
        "image_path": "MLBstadiumgraphics/AmericanFamilyField.gif",
        "lat": 43.0280,
        "lon": -87.9712,
        "altitude": 602,
        "dimensions": {
            "left_field": 344,
            "left_center": 371,
            "center_field": 400,
            "right_center": 374,
            "right_field": 345
        },
        "wall_heights": [(0, 90, 8)],
        "polar_coords": [
            (0, 16.5, (4068.1011, 1, 11.7916)),
            (16.5, 16.8, (-60.8626, 1, -0.47706)),
            (16.8, 23.3, (3834.475, 1, 10.73232)),
            (23.3, 35.5, (1042.985, 1, 2.60569)),
            (35.5, 37.7, (-1107.4106, 1, -4.237288)),
            (37.7, 52.3, (566.71123, 1, 1)),
            (52.3, 56.2, (287.52, 1, -0.130068)),
            (56.2, 74, (393.82239, 1, 0.374126)),
            (74, 85, (358.50448, 1, 0.027824)),
            (85, 90, (344, 1, -0.435742))
        ]
    },
    "Angel Stadium": {  # Los Angeles Angels
        "image_path": "MLBstadiumgraphics/AnaheimStadium.gif",
        "lat": 33.8003,
        "lon": -117.8827,
        "altitude": 157,
        "dimensions": {
            "left_field": 330,
            "left_center": 382,
            "center_field": 398,
            "right_center": 368,
            "right_field": 330
        },
        "wall_heights": [(0, 25, 8), (25, 65, 8), (65, 90, 5)],
        "polar_coords": [
            (0, 1.6, (-352.2388, 1, -1.06739)),
            (1.6, 3.2, (-496.7696, 1, -1.493901)),
            (3.2, 4.8, (-641.02615, 1, -1.9114787)),
            (4.8, 6.6, (-1020.3203, 1, -2.9928111)),
            (6.6, 11.2, (6919.533, 1, 19.3875915)),
            (11.2, 42.6, (1240.50705, 1, 3.314747)),
            (42.6, 68, (437.37565, 1, 0.5733725)),
            (68, 84, (351.0005, 1, -0.0286525)),
            (84, 85.6, (340.789, 1, -0.3046164)),
            (85.6, 87, (329.5441, 1, -0.72339596)),
            (87, 88.4, (324.50638, 1, -1.0040283)),
            (88.4, 90, (328, 1, -0.629411))
        ]
    },
    "Busch Stadium": {  # St. Louis Cardinals
        "image_path": "MLBstadiumgraphics/BuschStadium.gif",
        "lat": 38.6225,
        "lon": -90.1931,
        "altitude": 466,
        "dimensions": {
            "left_field": 336,
            "left_center": 375,
            "center_field": 400,
            "right_center": 375,
            "right_field": 335
        },
        "wall_heights": [(0, 90, 8)],
        "polar_coords": [
            (0, 3.3, (-436.689, 1, -1.3173)),
            (3.3, 25.6, {'type': 'cos_only', 'numerator': 346.303}),  # r = 346.303/cos θ
            (25.6, 39.9, (857.076, 1, 1.995805)),
            (39.9, 50, (569.534, 1, 1.04571)),
            (50, 64, (434.192, 1, 0.514)),
            (64, 88.4, {'type': 'sin_only', 'numerator': 346.76}),  # r = 346.76/sin θ
            (88.4, 90, (330, 1, -1.73033))
        ]
    },
    "Camden Yards": {  # Baltimore Orioles
        "image_path": "MLBstadiumgraphics/CamdenYards.gif",
        "lat": 39.2839,
        "lon": -76.6216,
        "altitude": 36,
        "dimensions": {
            "left_field": 333,
            "left_center": 386,
            "center_field": 410,
            "right_center": 373,
            "right_field": 318
        },
        "wall_heights": [(0, 15, 21), (15, 25, 21), (25, 65, 7), (65, 90, 8)],
        "polar_coords": [
            (0, 25.5, (-1786.977, 1, -5.61942)),
            (25.5, 49, (801.702, 1, 1.830)),
            (49, 69.7, (359.7761, 1, 0.187168)),
            (69.7, 90, {'type': 'interpolated', 'data': [
                (69.7, 363), (69.8, 364), (69.9, 366), (70.0, 368),
                (70.1, 370), (70.2, 372), (70.3, 374), (72.3, 373),
                (74.3, 372), (76.3, 371), (78.4, 370.75), (80.5, 370.5),
                (82.5, 371.25), (84.5, 372.25), (86.5, 373),
                (87.5, 358), (88.5, 345), (89.5, 333), (90.0, 331)
            ]})
        ]
    },
    "Chase Field": {  # Arizona Diamondbacks
        "image_path": "MLBstadiumgraphics/ChaseField.gif",
        "lat": 33.4453,
        "lon": -112.0667,
        "altitude": 1059,
        "dimensions": {
            "left_field": 330,
            "left_center": 376,
            "center_field": 407,
            "right_center": 376,
            "right_field": 334
        },
        "wall_heights": [(0, 25, 8), (25, 65, 25), (65, 90, 8)],
        "polar_coords": [
            (0, 4.9, (-389.4197, 1, -1.1624468)),
            (4.9, 6.6, (423.5471, 1, 1.085346)),
            (6.6, 31.7, (6211.3885, 1, 17.49789)),
            (31.7, 32.9, (427.9667, 1, 0.630552)),
            (32.9, 34, (1197.8397, 1, 2.9286229)),
            (34, 38.9, (559.10919, 1, 1.0073058)),
            (38.9, 39.1, (-91.557622, 1, -1.10398598)),
            (39.1, 50.5, (571.92441, 1, 1.0070058)),
            (50.5, 50.8, (114.59269, 1, -0.76826977)),
            (50.8, 55.7, (557.962, 1, 1.0031979)),
            (55.7, 56.7, (403.8808, 1, 0.3213439)),
            (56.7, 57.7, (755.17044, 1, 1.924966)),
            (57.7, 82.5, (353.793768, 1, 0.06108017)),
            (82.5, 84.2, (395.0241, 1, 0.9534313)),
            (84.2, 90, (327, 1, -0.9060869))
        ]
    },
    "Citi Field": {  # New York Mets
        "image_path": "MLBstadiumgraphics/CitiField.gif",
        "lat": 40.7571,
        "lon": -73.8458,
        "altitude": 10,
        "dimensions": {
            "left_field": 335,
            "left_center": 379,
            "center_field": 408,
            "right_center": 378,
            "right_field": 330
        },
        "wall_heights": [(0, 90, 8)],
        "polar_coords": [
            (0, 18.8, (-1967.79, 1, -5.963)),
            (18.8, 23, (667.7078, 1, 1.566132)),
            (23, 40.6, (1795.74, 1, 4.923)),
            (40.6, 49.1, (575.86589, 1, 0.9960149)),
            (49.1, 82.1, (358.6125, 1, 0.1847292)),
            (82.1, 90, (335, 1, -0.30194697))
        ]
    },
    "Citizens Bank Park": {  # Philadelphia Phillies
        "image_path": "MLBstadiumgraphics/CitizensBankPark.gif",
        "lat": 39.9061,
        "lon": -75.1665,
        "altitude": 13,
        "dimensions": {
            "left_field": 329,
            "left_center": 374,
            "center_field": 401,
            "right_center": 369,
            "right_field": 330
        },
        "wall_heights": [(0, 25, 13), (25, 65, 6), (65, 90, 11)],
        "polar_coords": [
            (0, 34.3, {'type': 'cos_only', 'numerator': 330}),
            (34.3, 50.7, (644.15, 1, 1.277017)),
            (50.7, 55.9, (308.591, 1, -0.02468)),
            (55.9, 59.3, (543.4657, 1, 1.08071)),
            (59.3, 88.3, {'type': 'sin_only', 'numerator': 331}),
            (88.3, 90, (325, 1, -0.596191))
        ],
    },
    "Comerica Park": {  # Detroit Tigers
        "image_path": "MLBstadiumgraphics/ComericaPark.gif",
        "lat": 42.3390,
        "lon": -83.0485,
        "altitude": 600,
        "dimensions": {
            "left_field": 342,
            "left_center": 379,
            "center_field": 420,
            "right_center": 388,
            "right_field": 345
        },
        "wall_heights": [(0, 25, 9), (25, 65, 9), (65, 90, 7)],
        "polar_coords": [
            (0, 1.25, (-405.584, 1, -1.23813)),
            (1.25, 22.5, {'type': 'cos_only', 'numerator': 337.21}),  # r = 337.21/cos θ
            (22.5, 24.6, (-430.4868, 1, -1.6908)),
            (24.6, 35.3, {'type': 'cos_only', 'numerator': 347.675}),  # r = 347.675/cos θ
            (35.3, 54, (593.97, 1, 1)),  # r = 593.97/(sin θ + cos θ)
            (54, 90, {'type': 'sin_only', 'numerator': 345})  # r = 345/sin θ
        ]
    },
    "Coors Field": {  # Colorado Rockies
        "image_path": "MLBstadiumgraphics/CoorsField.gif",
        "lat": 39.7559,
        "lon": -104.9942,
        # MLB's venue record says 5190, and the ERA5 grid cell agrees (1585 m
        # vs 1582). 5280 is the mile-high figure for the 20th row of the upper
        # deck, not for the field — and this number is a direct air-density
        # term, so it should be the field.
        "altitude": 5190,
        "dimensions": {
            "left_field": 347,
            "left_center": 390,
            "center_field": 415,
            "right_center": 387,
            "right_field": 350
        },
        "wall_heights": [(0, 15, 17), (15, 25, 17), (25, 65, 8), (65, 90, 8)],
        "polar_coords": [
            (0, 1.25, (-551.417, 1, -1.57548)),
            (1.25, 37.5, (4061.537, 1, 11.422)),
            (37.5, 60.2, (536.536, 1, 0.84288)),
            (60.2, 90, (345, 1, -0.08135))
        ]
    },
    "Dodger Stadium": {  # Los Angeles Dodgers
        "image_path": "MLBstadiumgraphics/DodgerStadium.gif",
        "lat": 34.0739,
        "lon": -118.2400,
        "altitude": 500,
        "dimensions": {
            "left_field": 330,
            "left_center": 375,
            "center_field": 400,
            "right_center": 375,
            "right_field": 330
        },
        "wall_heights": [(0, 25, 4), (25, 65, 8), (65, 90, 4)],
        "polar_coords": [
            (0, 4.2, (-443.8081, 1, -1.344873)),
            (4.2, 7.8, (-829.5118, 1, -2.44985)),
            (7.8, 9.5, (-10942.3745, 1, -30.646819)),
            (9.5, 25.1, (1719.756, 1, 4.622957)),
            (25.1, 31.1, (1115.073, 1, 2.83277)),
            (31.1, 42.6, (928.868, 1, 2.258998)),
            (42.6, 44.0, (742.26267, 1, 1.620443)),
            (44.0, 46.3, (562.6864, 1, 0.9947777)),
            (46.3, 49.2, (472.8006, 1, 0.66870534)),
            (49.2, 55.3, (423.6147, 1, 0.478618)),
            (55.3, 59, (395.11776, 1, 0.349269)),
            (59, 63.1, (392.2193, 1, 0.3344991)),
            (63.1, 69.2, (381.7462, 1, 0.2729345)),
            (69.2, 74.7, (372.8431, 1, 0.2051737)),
            (74.7, 80.5, (368.8506, 1, 0.163833)),
            (80.5, 82.1, (362.2344, 1, 0.053704)),
            (82.1, 83.3, (353.007, 1, -0.131245)),
            (83.3, 85.6, (334.774, 1, -0.564136)),
            (85.6, 87.2, (333.006, 1, -0.629807)),
            (87.2, 88.4, (328.317, 1, -0.90885)),
            (88.4, 90, (330, 1, -0.729958))
        ]
    },
    "Fenway Park": {  # Boston Red Sox
        "image_path": "MLBstadiumgraphics/FenwayPark.gif",
        "lat": 42.3467,
        "lon": -71.0972,
        "altitude": 20,
        "dimensions": {
            "left_field": 310,  # The Green Monster
            "left_center": 379,
            "center_field": 390,
            "right_center": 380,
            "right_field": 302
        },
        "wall_heights": [
            (0, 5, 3),        # Pesky Pole area — very short RF fence
            (5, 25, 5),       # RF bullpen area
            (25, 55, 17),     # CF triangle / batter's eye
            (55, 90, 37),     # Green Monster (LC through LF foul line)
        ],
        "polar_coords": [
            # Format: (angle_start, angle_end, (numerator, sin_coeff, cos_coeff)) where r = numerator/(sin_coeff*sin(θ) + cos_coeff*cos(θ))
            (0, 3.8, (-119.0423, 1, -0.3941798)),
            (3.8, 4.9, (-402.289, 1, -1.174046)),
            (4.9, 6, (-808.953, 1, -2.274195)),
            (6, 7.1, (-2332.79083, 1, -6.360156)),
            (7.1, 8.1, (-20759.85313, 1, -55.616)),
            (8.1, 31, (1129.33168, 1, 2.875435)),
            (31, 33.8, (-417.143116, 1, -1.8849057)),
            (33.8, 52.2, (431.2604, 1, 0.587157)),  # Fixed: positive numerator
            (52.2, 53.1, (2077.8716, 1, 7.7513156)),
            (53.1, 90, (306, 1, -0.00577087))
        ]
    },
    "Globe Life Field": {  # Texas Rangers
        "image_path": "MLBstadiumgraphics/GlobeLifeField.gif",
        "lat": 32.7473,
        "lon": -97.0835,
        "altitude": 544,
        "dimensions": {
            "left_field": 329,
            "left_center": 372,
            "center_field": 407,
            "right_center": 374,
            "right_field": 326
        },
        "wall_heights": [(0, 90, 8)],
        "polar_coords": [
            (0, 4, (-432.031, 1, -1.3252)),
            (4, 24, {'type': 'cos_only', 'numerator': 343.5}),  # r = 343.5/cos θ
            (24, 26.1, (543.706, 1, 1.1376)),
            (26.1, 34.3, {'type': 'cos_only', 'numerator': 336.22}),  # r = 336.22/cos θ
            (34.3, 53.1, (565.81, 1, 1)),  # r = 565.81/(sin θ + cos θ)
            (53.1, 64.3, (416.6997, 1, 0.38598)),
            (64.3, 84.2, {'type': 'sin_only', 'numerator': 349.203}),  # r = 349.203/sin θ
            (84.2, 90, (331, 1, -0.51319))
        ]
    },
    "Great American Ball Park": {  # Cincinnati Reds
        "image_path": "MLBstadiumgraphics/GreatAmericanBallPark.gif",
        "lat": 39.0979,
        "lon": -84.5082,
        "altitude": 490,
        "dimensions": {
            "left_field": 328,
            "left_center": 379,
            "center_field": 404,
            "right_center": 365,
            "right_field": 325
        },
        "wall_heights": [(0, 25, 8), (25, 65, 8), (65, 90, 12)],
        "polar_coords": [
            (0, 44.7, {'type': 'complex_great_american'}),  # Complex equation
            (44.7, 60.3, (436.311, 1, 0.52231577)),
            (60.3, 86.6, (336.435, 1, 0.0014347)),
            (86.6, 90, (326, 1, -0.5206991))
        ]
    },
    "Guaranteed Rate Field": {  # Chicago White Sox
        "image_path": "MLBstadiumgraphics/GuaranteedRateField.gif",
        "lat": 41.8299,
        "lon": -87.6338,
        "altitude": 590,
        "dimensions": {
            "left_field": 330,
            "left_center": 375,
            "center_field": 400,
            "right_center": 375,
            "right_field": 335
        },
        "wall_heights": [(0, 90, 8)],
        "polar_coords": [
            (0, 24.1, (-7014.6043, 1, -20.939117)),
            (24.1, 30.6, (1495.6997, 1, 3.92207)),
            (30.6, 36.6, (820.061, 1, 1.88324)),
            (36.6, 39.1, (1969.1459, 1, 5.562717)),
            (39.1, 50.6, (561.4969, 1, 1.00525)),
            (50.6, 54, (363.2118, 1, 0.2203438)),
            (54, 58.7, (426.18639, 1, 0.49718)),
            (58.7, 63.4, (378.8179, 1, 0.259128)),
            (63.4, 79, (340.82399, 1, 0.03285)),
            (79, 90, (327, 1, -0.221087))
        ]
    },
    "Kauffman Stadium": {  # Kansas City Royals
        "image_path": "MLBstadiumgraphics/KauffmanStadium.gif",
        "lat": 39.0516,
        "lon": -94.4803,
        "altitude": 856,  # was 750; MLB says 856 and the ERA5 grid cell 869 ft
        "dimensions": {
            "left_field": 330,
            "left_center": 375,
            "center_field": 410,
            "right_center": 375,
            "right_field": 330
        },
        "wall_heights": [(0, 90, 9)],
        "polar_coords": [
            (0, 5.9, {'type': 'complex_kauffman1'}),
            (5.9, 22.1, (25784.376, 1, 71.503534)),
            (22.1, 59, {'type': 'complex_kauffman2'}),
            (59, 76.9, {'type': 'complex_kauffman3'}),
            (76.9, 82.7, (361.884, 1, 0.01803985)),
            (82.7, 90, {'type': 'complex_kauffman4'})
        ]
    },
    "LoanDepot Park": {  # Miami Marlins
        "image_path": "MLBstadiumgraphics/LoanDepotPark.gif",
        "lat": 25.7781,
        "lon": -80.2197,
        "altitude": 7,
        "dimensions": {
            "left_field": 344,
            "left_center": 386,
            "center_field": 400,
            "right_center": 392,
            "right_field": 335
        },
        "wall_heights": [(0, 25, 12), (25, 65, 9), (65, 90, 12)],
        "polar_coords": [
            (0, 23.7, (-3285.092, 1, -9.80624)),
            (23.7, 59, {'type': 'interpolated', 'data': [
                (23.7, 383), (35.0, 391), (40.0, 397), (43.0, 399),
                (45.0, 400), (47.0, 399), (50.0, 397), (55.0, 391), (59.0, 387)
            ]}),
            (59, 60.8, (389.587, 1, 0.2903055)),
            (60.8, 63.6, (387.8902, 1, 0.281246)),
            (63.6, 68.2, (367.932, 1, 0.163124)),
            (68.2, 72.1, (360.9411, 1, 0.112519)),
            (72.1, 79.2, (349.332, 1, 0.0031917)),
            (79.2, 84.3, (339.562, 1, -0.137541)),
            (84.3, 90, (337, 1, -0.212114))
        ]
    },
    "Minute Maid Park": {  # Houston Astros
        "image_path": "MLBstadiumgraphics/MinuteMaidPark.gif",
        "lat": 29.7573,
        "lon": -95.3555,
        "altitude": 43,
        "dimensions": {
            "left_field": 315,
            "left_center": 362,
            "center_field": 409,
            "right_center": 373,
            "right_field": 326
        },
        "wall_heights": [(0, 25, 7), (25, 65, 9), (65, 90, 21)],
        "polar_coords": [
            (0, 23, (-2738.7177, 1, -8.400974)),
            (23, 24.1, (315.172, 1, 0.493462)),
            (24.1, 34, (-2943.702, 1, -9.23423)),
            (34, 50.2, (588.57, 1, 1.038)),
            (50.2, 67.7, (347.579, 1, 0.120385)),
            (67.7, 67.9, (42.673422, 1, -2.124119)),
            (67.9, 90, (315, 1, -0.0366002))
        ]
    },
    "Nationals Park": {  # Washington Nationals
        "image_path": "MLBstadiumgraphics/NationalsPark.gif",
        "lat": 38.8730,
        "lon": -77.0074,
        "altitude": 25,
        "dimensions": {
            "left_field": 336,
            "left_center": 377,
            "center_field": 402,
            "right_center": 370,
            "right_field": 335
        },
        "wall_heights": [(0, 25, 16), (25, 65, 10), (65, 90, 10)],
        "polar_coords": [
            (0, 13.1, (-1192.9, 1, -3.56091)),
            (13.1, 46.5, (1018.837, 1, 2.609847)),
            (46.5, 57.9, (372.8509, 1, 0.286983)),
            (57.9, 59, (1089.637, 1, 3.903208)),
            (59, 74.1, (383.87617, 1, 0.297133)),
            (74.1, 74.2, (163.401, 1, -1.88975)),
            (74.2, 76.5, (377.1893, 1, 0.261412)),
            (76.5, 90, (336, 1, -0.221087))
        ]
    },
    "Sutter Health Park": {  # Oakland Athletics - 2025-2027 temporary venue
        "image_path": "MLBstadiumgraphics/SutterHealthPark.gif",
        "lat": 38.6561,
        "lon": -121.5025,
        "altitude": 30,
        "dimensions": {
            "left_field": 325,
            "left_center": 362,
            "center_field": 400,
            "right_center": 365,
            "right_field": 320
        },
        "wall_heights": [(0, 90, 8)],
        "polar_coords": [
            (0, 90, {'type': 'interpolated', 'data': [
                (0.0, 325.0), (1.0, 327.3), (2.0, 329.7), (3.0, 332.2),
                (4.0, 334.8), (5.0, 337.5), (6.0, 340.3), (7.0, 343.2),
                (8.0, 346.2), (9.0, 349.3), (9.9, 352.5), (10.9, 355.8),
                (11.9, 359.2), (12.8, 362.7), (13.8, 366.3), (14.7, 370.0),
                (15.7, 370.2), (16.7, 370.5), (17.7, 370.9), (18.7, 371.4),
                (19.7, 372.0), (20.7, 372.7), (21.7, 373.5), (22.7, 374.4),
                (23.7, 375.4), (24.7, 376.5), (25.8, 377.7), (26.9, 379.0),
                (28.0, 380.4), (29.0, 381.9), (30.0, 383.5), (31.0, 385.2),
                (32.0, 387.0), (33.0, 388.9), (34.0, 390.9), (35.0, 393.0),
                (36.0, 395.2), (37.0, 397.5), (38.0, 399.9), (39.0, 402.4),
                (40.0, 404.5), (41.0, 404.0), (42.0, 403.6), (43.0, 403.3),
                (44.0, 403.1), (45.0, 403.0), (46.0, 403.1), (47.0, 403.3),
                (48.0, 403.6), (49.0, 404.0), (50.0, 404.5), (51.0, 405.0),
                (52.0, 403.9), (53.0, 402.8), (54.0, 401.8), (55.0, 400.7),
                (56.0, 399.6), (57.0, 398.5), (58.0, 397.4), (59.0, 396.2),
                (60.0, 395.0), (61.0, 393.8), (62.0, 392.5), (63.0, 391.2),
                (64.0, 389.8), (65.0, 388.3), (66.0, 386.8), (67.0, 385.2),
                (68.0, 383.6), (69.0, 381.8), (70.0, 380.0), (71.0, 378.1),
                (72.0, 376.1), (73.0, 374.0), (74.0, 371.9), (75.0, 369.8),
                (76.0, 367.8), (77.0, 365.7), (78.0, 363.7), (79.0, 361.8),
                (80.0, 360.0), (81.0, 358.3), (82.0, 356.5), (83.0, 354.7),
                (84.0, 352.5), (85.0, 350.0), (86.0, 347.0), (87.0, 343.6),
                (88.0, 340.0), (89.0, 336.0), (90.0, 330.0)
            ]})
        ]
    },
    "Oracle Park": {  # San Francisco Giants
        "image_path": "MLBstadiumgraphics/OraclePark.gif",
        "lat": 37.7786,
        "lon": -122.3893,
        "altitude": 0,  # At sea level
        "dimensions": {
            "left_field": 339,
            "left_center": 382,
            "center_field": 399,
            "right_center": 415,
            "right_field": 309
        },
        "wall_heights": [(0, 15, 25), (15, 25, 25), (25, 65, 8), (65, 90, 8)],
        "polar_coords": [
            (0, 15, (-697.339, 1, -2.25676)),
            (15, 18, (946.0859, 1, 2.4155)),
            (18, 25.6, (-712.5915, 1, -2.3890)),
            (25.6, 56.2, (552.5, 1, 1)),
            (56.2, 86.5, (347.526, 1, 0.07905)),
            (86.5, 90, (335, 1, -0.513097))
        ]
    },
    "Petco Park": {  # San Diego Padres
        "image_path": "MLBstadiumgraphics/PetcoPark.gif",
        "lat": 32.7073,
        "lon": -117.1566,
        "altitude": 16,
        "dimensions": {
            "left_field": 336,
            "left_center": 390,
            "center_field": 396,
            "right_center": 391,
            "right_field": 322
        },
        "wall_heights": [(0, 25, 10), (25, 65, 7), (65, 90, 4)],
        "polar_coords": [
            (0, 3.4, {'type': 'cos_only', 'numerator': 321.433}),  # r = 321.433/cos θ
            (3.4, 7.2, (-311.7359, 1, -1.029242)),
            (7.2, 27.8, {'type': 'cos_only', 'numerator': 345.87116}),  # r = 345.87116/cos θ
            (27.8, 31.8, (1425.7353, 1, 3.59492)),
            (31.8, 38.3, (740.2202, 1, 1.568308)),
            (38.3, 49.2, (543.05468, 1, 0.9402139)),
            (49.2, 50.4, (318.3662, 1, 0.0718681)),
            (50.4, 56.2, (539.44852, 1, 0.9611939)),
            (56.2, 63.5, (393.566469, 1, 0.2972904)),
            (63.5, 83.8, (344.316, 1, 0.0091906)),
            (83.8, 90, (336, 1, -0.2134522))
        ]
    },
    "PNC Park": {  # Pittsburgh Pirates
        "image_path": "MLBstadiumgraphics/PNCPark.gif",
        "lat": 40.4469,
        "lon": -80.0057,
        "altitude": 726,
        "dimensions": {
            "left_field": 325,
            "left_center": 389,
            "center_field": 399,
            "right_center": 375,
            "right_field": 320
        },
        "wall_heights": [(0, 15, 21), (15, 25, 21), (25, 65, 10), (65, 90, 6)],
        "polar_coords": [
            (0, 22.3, (-1759.947, 1, -5.4827)),
            (22.3, 34.1, (1120.149, 1, 2.8184)),
            (34.1, 44.3, (716.884, 1, 1.56)),
            (44.3, 58.5, (478.809, 1, 0.71785)),
            (58.5, 59.6, (-4560.837, 1, -24.0136)),
            (59.6, 81.5, (366.846, 1, 0.089958)),
            (81.5, 90, (321, 1, -0.75751))
        ]
    },
    "Progressive Field": {  # Cleveland Guardians
        "image_path": "MLBstadiumgraphics/ProgressiveField.gif",
        "lat": 41.4962,
        "lon": -81.6852,
        "altitude": 653,
        "dimensions": {
            "left_field": 325,
            "left_center": 370,
            "center_field": 405,
            "right_center": 375,
            "right_field": 325
        },
        "wall_heights": [(0, 25, 9), (25, 65, 9), (65, 90, 19)],
        "polar_coords": [
            (0, 20.3, (-1609.844, 1, -4.98404)),
            (20.3, 48.25, (906.183, 1, 2.2274)),
            (48.25, 78.25, (356.7465, 1, 0.197554)),
            (78.25, 90, (321, 1, -0.303978))
        ]
    },
    "Rogers Centre": {  # Toronto Blue Jays
        "image_path": "MLBstadiumgraphics/RogersCentre.gif",
        "lat": 43.6414,
        "lon": -79.3894,
        "altitude": 266,
        "dimensions": {
            "left_field": 328,
            "left_center": 375,
            "center_field": 400,
            "right_center": 375,
            "right_field": 328
        },
        "wall_heights": [(0, 90, 8)],
        "polar_coords": [
            (0, 20, (-1725.1974, 1, -5.2597)),
            (20, 32.5, (2160.354, 1, 5.7667)),
            (32.5, 57.5, {'type': 'constant', 'value': 400}),  # r = 400 ft constant
            (57.5, 70, (374.6529, 1, 0.17341)),
            (70, 90, (328, 1, -0.19012))
        ]
    },
    "T-Mobile Park": {  # Seattle Mariners
        "image_path": "MLBstadiumgraphics/TMobilePark.gif",
        "lat": 47.5915,
        "lon": -122.3326,
        "altitude": 15,
        "dimensions": {
            "left_field": 331,
            "left_center": 378,
            "center_field": 401,
            "right_center": 381,
            "right_field": 326
        },
        "wall_heights": [(0, 25, 7), (25, 65, 7), (65, 90, 15)],
        "polar_coords": [
            (0, 26.5, (-3502.437, 1, -10.74367)),
            (26.5, 47, (825.224, 1, 1.9153)),
            (47, 59.6, (414.271, 1, 0.427476)),
            (59.6, 66.5, (377.4922, 1, 0.2382)),
            (66.5, 88.5, (336.558, 1, -0.037016)),
            (88.5, 90, (331, 1, -0.6671))
        ]

    },
    "Tropicana Field": {  # Tampa Bay Rays - primary venue
        "image_path": "MLBstadiumgraphics/TropicanaField.gif",
        "lat": 27.7683,
        "lon": -82.6534,
        "altitude": 15,
        "dimensions": {
            "left_field": 315,
            "left_center": 370,
            "center_field": 404,
            "right_center": 370,
            "right_field": 322
        },
        "wall_heights": [(0, 25, 11), (25, 65, 9), (65, 90, 11)],
        "polar_coords": [
            (0, 1.7, (-357.101, 1, -1.109)),
            (1.7, 33.75, {'type': 'cos_only', 'numerator': 331}),
            (33.75, 36.2, (1678.156, 1, 4.403)),
            (36.2, 55, (596.756, 1, 1.09406)),
            (55, 56.4, (275.106, 1, -0.2654)),
            (56.4, 58, (477.013, 1, 0.6444)),
            (58, 86, (342.4305, 1, 0.011121)),
            (86, 90, (315, 1, -1.13533))
        ]
    },
    "Target Field": {  # Minnesota Twins
        "image_path": "MLBstadiumgraphics/TargetField.gif",
        "lat": 44.9817,
        "lon": -93.2776,
        "altitude": 840,
        "dimensions": {
            "left_field": 339,
            "left_center": 377,
            "center_field": 404,
            "right_center": 367,
            "right_field": 328
        },
        "wall_heights": [(0, 15, 23), (15, 25, 23), (25, 65, 8), (65, 90, 8)],
        "polar_coords": [
            (0, 20, (-2731.998, 1, -8.3292)),
            (20, 38.5, (1691.285, 1, 4.5671)),
            (38.5, 51.2, (629.3765, 1, 1.2001)),
            (51.2, 67, (382.741, 1, 0.24243)),
            (67, 90, (339, 1, -0.06451))
        ]
    },
    "George M. Steinbrenner Field": {  # Tampa Bay Rays - 2025 temporary venue
        "image_path": "MLBstadiumgraphics/SteinbrennerField.gif",
        "lat": 28.0647,
        "lon": -82.5069,
        "altitude": 15,
        "dimensions": {
            "left_field": 318,
            "left_center": 360,
            "center_field": 408,
            "right_center": 370,
            "right_field": 325
        },
        "wall_heights": [(0, 90, 8)],
        "polar_coords": [
            (0, 90, {'type': 'interpolated', 'data': [
                (0.0, 314), (1.8, 322), (3.6, 329), (5.4, 336),
                (6.6, 339), (7.4, 341), (8.4, 341.9), (9.4, 343),
                (10.4, 344.1), (11.4, 345.3), (12.4, 346.7), (13.4, 348.2),
                (14.4, 349.8), (15.4, 351.5), (16.4, 353.3), (17.4, 355.1),
                (18.4, 357), (19.4, 359), (20.4, 361.1), (21.4, 363.3),
                (22.4, 365.6), (23.4, 368), (24.4, 370.5), (25.4, 373.1),
                (26.4, 375.8), (27.4, 378.6), (28.4, 381.5), (29.4, 384.5),
                (30.4, 387.6), (32.8, 393), (35.2, 398), (37.2, 402),
                (39.2, 404), (42.0, 406), (45.0, 408), (46.0, 407.5),
                (48.0, 407), (51.0, 406), (53.2, 405), (55.2, 403.8),
                (57.2, 402.4), (59.2, 400.8), (61.2, 398.8), (63.0, 396.0),
                (64.0, 392.4), (65.0, 388.9), (66.0, 385.5), (67.0, 382.3),
                (68.0, 379.1), (69.0, 376.1), (70.0, 373.1), (71.0, 370.2),
                (72.0, 367.4), (73.0, 364.7), (74.0, 362.1), (75.0, 359.5),
                (76.0, 357.0), (77.0, 354.6), (78.0, 352.3), (79.0, 350.1),
                (80.0, 348.5), (82.0, 345), (84.0, 340), (86.0, 334),
                (88.0, 327), (90.0, 318)
            ]})
        ]
    },
    "Truist Park": {  # Atlanta Braves
        "image_path": "MLBstadiumgraphics/TruistPark.gif",
        "lat": 33.8907,
        "lon": -84.4677,
        "altitude": 1050,
        "dimensions": {
            "left_field": 335,
            "left_center": 385,
            "center_field": 400,
            "right_center": 375,
            "right_field": 325
        },
        "wall_heights": [(0, 25, 16), (25, 65, 9), (65, 90, 9)],
        "polar_coords": [
            (0, 23, (-2358.6904452, 1, -7.257509)),
            (23, 38, (1373.4019163, 1, 3.554217)),
            (38, 51, (569.8539117, 1, 1.017607)),
            (51, 66, (415.5627983, 1, 0.407729)),
            (66, 76, (392.8771560, 1, 0.262860)),
            (76, 90, (335, 1, -0.366717))
        ]
    },
    "Wrigley Field": {  # Chicago Cubs
        "image_path": "MLBstadiumgraphics/WrigleyField.gif",
        "lat": 41.9484,
        "lon": -87.6553,
        "altitude": 600,
        "dimensions": {
            "left_field": 355,
            "left_center": 368,
            "center_field": 400,
            "right_center": 368,
            "right_field": 353
        },
        "wall_heights": [(0, 90, 11)],
        "polar_coords": [
            (0, 10.9, (-4499.412, 1, -12.7462)),
            (10.9, 13.1, (297.1748, 1, 0.636566)),
            (13.1, 29.4, (18363.859, 1, 53.4839)),
            (29.4, 49.2, {'type': 'complex_wrigley'}),
            (49.2, 73.2, (357.8732, 1, 0.245827)),
            (73.2, 74.8, (496.86435, 1, 1.62768)),
            (74.8, 90, (355, 1, 0.112061))
        ]
    },
    "Yankee Stadium": {  # New York Yankees
        "image_path": "MLBstadiumgraphics/YankeeStadium.gif",
        "lat": 40.8296,
        "lon": -73.9262,
        "altitude": 14,
        "dimensions": {
            "left_field": 318,
            "left_center": 399,
            "center_field": 408,
            "right_center": 385,
            "right_field": 314
        },
        "wall_heights": [(0, 90, 8)],
        "polar_coords": [
            (0, 3.2, (-752.7415, 1, -2.397266)),
            (3.2, 4.9, (-1341.4764, 1, -4.22849)),
            (4.9, 30.6, {'type': 'cos_only', 'numerator': 323.639}),
            (30.6, 36.1, (2683.6147, 1, 7.700602)),
            (36.1, 40.4, (913.27186, 1, 2.139572)),
            (40.4, 44.4, (707.36801, 1, 1.4653105)),
            (44.4, 48.4, (600.6388, 1, 1.096466)),
            (48.4, 52.1, (496.311752, 1, 0.7103818)),
            (52.1, 56.7, (445.2994, 1, 0.5053365)),
            (56.7, 62.8, (390.30014, 1, 0.2548946)),
            (62.8, 80.6, (345.39856, 1, 0.001719809)),
            (80.6, 84.8, (324.4985, 1, -0.3638949)),
            (84.8, 90, (316, 1, -0.6421415))
        ]
    }
}


# ==============================================
# Park orientation & roof
# ==============================================
# `azimuth` is the TRUE COMPASS BEARING, in degrees clockwise from north, of
# the home-plate -> centre-field axis.  Values come straight from MLB's own
# venue record (statsapi /api/v1/venues/{id}?hydrate=location -> location.
# azimuthAngle), as does `roof` (fieldInfo.roofType).
#
# Without this the wind cannot be placed on the field at all.  The forecast
# gives a compass bearing; BallFlightSimulator wants an angle in the field
# frame (0 = from centre field).  Nothing else in this file records how a
# park is turned, so before these numbers existed every stadium was implicitly
# treated as though centre field pointed due north.
#
# Spot-checked against park landmarks rather than taken on faith, since a
# wrong azimuth is invisible — it just quietly moves the wind:
#   Wrigley 37    -> RF at 82 (Sheffield Ave, E), LF at 352 (Waveland, N)
#   PNC 116       -> outfield faces the downtown skyline, ESE
#   Great American 122 -> outfield faces the Ohio River, SE
#   Oracle 85     -> RF at 130, i.e. McCovey Cove to the SE
#   Coors 4       -> LF at 319, the Front Range to the NW
#   Petco 0       -> LF pole at 315, the Western Metal building
# The three parks reading exactly 0 (Chase, Petco, Progressive) are genuinely
# oriented near due north; 0 is not a missing-data default here.
PARK_ORIENTATION = {
    "American Family Field":        {"azimuth": 129.0,  "roof": "retractable"},
    "Angel Stadium":                {"azimuth":  43.61, "roof": "open"},
    "Busch Stadium":                {"azimuth":  62.0,  "roof": "open"},
    "Camden Yards":                 {"azimuth":  31.0,  "roof": "open"},
    "Chase Field":                  {"azimuth":   0.0,  "roof": "retractable"},
    "Citi Field":                   {"azimuth":  13.0,  "roof": "open"},
    "Citizens Bank Park":           {"azimuth":   9.0,  "roof": "open"},
    "Comerica Park":                {"azimuth": 150.0,  "roof": "open"},
    "Coors Field":                  {"azimuth":   4.0,  "roof": "open"},
    "Dodger Stadium":               {"azimuth":  26.0,  "roof": "open"},
    "Fenway Park":                  {"azimuth":  45.0,  "roof": "open"},
    "George M. Steinbrenner Field": {"azimuth":  60.0,  "roof": "open"},
    "Globe Life Field":             {"azimuth":  30.0,  "roof": "retractable"},
    "Great American Ball Park":     {"azimuth": 122.0,  "roof": "open"},
    "Guaranteed Rate Field":        {"azimuth": 127.0,  "roof": "open"},
    "Kauffman Stadium":             {"azimuth":  46.0,  "roof": "open"},
    "LoanDepot Park":               {"azimuth": 128.0,  "roof": "retractable"},
    "Minute Maid Park":             {"azimuth": 343.0,  "roof": "retractable"},
    "Nationals Park":               {"azimuth":  28.0,  "roof": "open"},
    "Oracle Park":                  {"azimuth":  85.0,  "roof": "open"},
    "PNC Park":                     {"azimuth": 116.0,  "roof": "open"},
    "Petco Park":                   {"azimuth":   0.0,  "roof": "open"},
    "Progressive Field":            {"azimuth":   0.0,  "roof": "open"},
    "Rogers Centre":                {"azimuth": 345.0,  "roof": "retractable"},
    "Sutter Health Park":           {"azimuth":  46.0,  "roof": "open"},
    "T-Mobile Park":                {"azimuth":  49.0,  "roof": "retractable"},
    "Target Field":                 {"azimuth": 129.0,  "roof": "open"},
    "Tropicana Field":              {"azimuth": 359.0,  "roof": "dome"},
    "Truist Park":                  {"azimuth": 145.0,  "roof": "open"},
    "Wrigley Field":                {"azimuth":  37.0,  "roof": "open"},
    "Yankee Stadium":               {"azimuth":  75.0,  "roof": "open"},
}

# Fold onto STADIUM_DATA so consumers that already hold a park dict can read
# `azimuth`/`roof` without a second lookup.  Kept as a separate table above
# because it comes from a different source than the polar wall equations and
# is refreshed independently.
for _park, _o in PARK_ORIENTATION.items():
    if _park in STADIUM_DATA:
        STADIUM_DATA[_park].update(_o)
del _park, _o


def get_park_azimuth(stadium_name):
    """Home-plate -> centre-field bearing in degrees clockwise from true
    north, or None if the park is unknown."""
    park = STADIUM_DATA.get(stadium_name) or PARK_ORIENTATION.get(stadium_name)
    return park.get("azimuth") if park else None


def get_park_roof(stadium_name):
    """'open', 'retractable' or 'dome'.  Unknown parks are assumed open."""
    park = STADIUM_DATA.get(stadium_name) or PARK_ORIENTATION.get(stadium_name)
    return (park or {}).get("roof", "open")


def wind_to_field_frame(met_direction_deg, stadium_name=None, azimuth=None):
    """Rotate a meteorological wind bearing into BallFlightSimulator's frame.

    Both conventions name the direction the wind blows FROM, and both are
    measured clockwise when seen from above, so the rotation is a plain
    subtraction:

        field_angle = (met_bearing - park_azimuth) mod 360

    In the returned frame 0 means the wind arrives from centre field (blowing
    in), 90 from the first-base/right-field side, 180 from behind the plate
    (blowing out to centre), 270 from the third-base/left-field side.

    Sanity check, Wrigley (azimuth 37): a south-westerly at 200 maps to 163 —
    from just off the plate, blowing out toward left-centre, which is the
    classic Wrigley slugfest wind.  A northerly at 0 maps to 323 — in off
    Waveland over the left-field wall.

    Returns None when the park's orientation is unknown, so callers can tell
    "no rotation applied" apart from "rotated by zero".
    """
    if azimuth is None and stadium_name is not None:
        azimuth = get_park_azimuth(stadium_name)
    if azimuth is None or met_direction_deg is None:
        return None
    return (float(met_direction_deg) - float(azimuth)) % 360.0


# Statcast home_team code -> STADIUM_DATA park name. Includes legacy and
# relocated codes (AZ/ARI, KC/KCR, SD/SDP, SF/SFG, TB/TBR, OAK/ATH, CWS/CHW).
TEAM_TO_PARK = {
    "ARI": "Chase Field", "AZ": "Chase Field",
    "ATL": "Truist Park",
    "BAL": "Camden Yards", "BOS": "Fenway Park",
    "CHC": "Wrigley Field", "CWS": "Guaranteed Rate Field", "CHW": "Guaranteed Rate Field",
    "CIN": "Great American Ball Park",
    "CLE": "Progressive Field", "COL": "Coors Field",
    "DET": "Comerica Park", "HOU": "Minute Maid Park",
    "KC": "Kauffman Stadium", "KCR": "Kauffman Stadium",
    "LAA": "Angel Stadium", "LAD": "Dodger Stadium",
    "MIA": "LoanDepot Park", "MIL": "American Family Field",
    "MIN": "Target Field", "NYM": "Citi Field", "NYY": "Yankee Stadium",
    "OAK": "Sutter Health Park", "ATH": "Sutter Health Park",
    "PHI": "Citizens Bank Park", "PIT": "PNC Park",
    "SD": "Petco Park", "SDP": "Petco Park",
    "SF": "Oracle Park", "SFG": "Oracle Park",
    "SEA": "T-Mobile Park", "STL": "Busch Stadium",
    "TB": "Tropicana Field", "TBR": "Tropicana Field",
    "TEX": "Globe Life Field", "TOR": "Rogers Centre",
    "WSH": "Nationals Park",
}


def get_park_for_team(team_code):
    """Return the STADIUM_DATA park name for a Statcast home_team code, or None."""
    if not team_code:
        return None
    return TEAM_TO_PARK.get(str(team_code).upper())


# ==============================================
# Weather Service Component
# ==============================================
class WeatherService:
    def __init__(self, api_key=open_weather_key):
        self.api_key = api_key or open_weather_key

        self.base_url = "https://api.openweathermap.org/data/2.5/weather"

    def get_weather_by_location(self, lat, lon):
        params = {
            "lat": lat,
            "lon": lon,
            "appid": self.api_key,
            "units": "imperial"  # Get data in imperial units (°F, mph)
        }

        response = requests.get(self.base_url, params=params)
        response.raise_for_status()   # surface HTTP errors (401, 429, 5xx, etc.)
        return response.json()

    def extract_weather_data(self, weather_json):
        """Extract relevant weather data from API response"""
        # OpenWeather's main.pressure is sea-level-normalised (SLP) — useful for display
        # but inaccurate for physics at altitude.  main.grnd_level is the actual station
        # pressure at ground level and is preferred when available.
        slp_hpa     = weather_json["main"].get("pressure", 1013.25)
        station_hpa = weather_json["main"].get("grnd_level")  # None if not in response
        pressure_hpa = station_hpa if station_hpa is not None else slp_hpa
        weather_data = {
            "wind_speed": weather_json["wind"]["speed"],
            "wind_direction": weather_json["wind"]["deg"],
            # Bearing from true north, so it must be rotated by the park's
            # azimuth before it means anything on a field.  Tagged rather
            # than assumed because hand-set wind lands in the same key.
            "wind_frame": "compass",
            # Gusts drive the spread of a carry estimate, not its centre.
            # Absent from the payload on calm days.
            "wind_gust": weather_json["wind"].get("gust"),
            "temperature": weather_json["main"]["temp"],
            "humidity": weather_json["main"]["humidity"],
            "pressure_hpa": pressure_hpa,           # hPa  – for display
            "pressure_pa": pressure_hpa * 100.0,    # Pa   – for physics
            # Which of the two we actually got.  grnd_level is measured at the
            # station and is already field level; main.pressure is sea-level
            # normalised and still needs the altitude reconstruction.  Feeding
            # the wrong one into that branch costs ~27 ft of carry at Coors.
            "pressure_frame": "station" if station_hpa is not None else "sealevel",
            "condition": weather_json["weather"][0]["main"],
            "description": weather_json["weather"][0]["description"],
            "precipitation": 0  # Default to 0
        }

        # Add precipitation data if available
        if "rain" in weather_json:
            # Rain data might be in 1h or 3h keys
            weather_data["precipitation"] = weather_json["rain"].get("1h", weather_json["rain"].get("3h", 0) / 3)
        elif "snow" in weather_json:
            # Snow data might also be relevant
            weather_data["precipitation"] = weather_json["snow"].get("1h", weather_json["snow"].get("3h", 0) / 3)
            weather_data["condition"] = "Snow"

        return weather_data


    # Use this at later date, see whos gonna miss FG wide right
    def draw_wind_vector(painter, x, y, speed, direction):
        """Draw a wind vector arrow with solid arrowhead"""
        # Convert wind direction from meteorological to mathematical angle
        math_angle = (270 - direction) % 360
        rad_angle = math.radians(math_angle)

        # Scale length based on wind speed
        max_length = 90
        min_length = 40
        scaled_length = min(max_length, max(min_length, speed * 4.5))

        # Calculate shaft end point (where arrowhead begins)
        shaft_end_x = x + (scaled_length * 0.7) * math.cos(rad_angle)
        shaft_end_y = y + (scaled_length * 0.7) * math.sin(rad_angle)

        # Calculate the true end point (tip of arrow)
        tip_x = x + scaled_length * math.cos(rad_angle)
        tip_y = y + scaled_length * math.sin(rad_angle)

        # Set color based on wind speed
        if speed < 5:
            color = QColor(0, 120, 255)  # Blue for light wind
        elif speed < 10:
            color = QColor(0, 200, 100)  # Green for moderate wind
        else:
            color = QColor(255, 40, 40)  # Red for strong wind

        # Save painter state
        painter.save()

        # Draw the shaft as a thick line
        pen = QPen(color, 6)  # Thicker line for visibility
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)  # Rounded ends
        painter.setPen(pen)
        painter.drawLine(QPointF(x, y), QPointF(shaft_end_x, shaft_end_y))

        # Create arrowhead as a polygon
        arrowhead_width = min(25, scaled_length * 0.4)  # Width proportional to length

        # Calculate the perpendicular direction for arrowhead width
        perp_angle = rad_angle + math.pi/2  # 90 degrees

        # Calculate the two base points of the arrowhead
        left_x = shaft_end_x + arrowhead_width * math.cos(perp_angle)
        left_y = shaft_end_y + arrowhead_width * math.sin(perp_angle)

        right_x = shaft_end_x - arrowhead_width * math.cos(perp_angle)
        right_y = shaft_end_y - arrowhead_width * math.sin(perp_angle)

        # Create the polygon
        arrowhead = QPolygonF()
        arrowhead.append(QPointF(tip_x, tip_y))  # Tip
        arrowhead.append(QPointF(left_x, left_y))  # Left corner
        arrowhead.append(QPointF(right_x, right_y))  # Right corner

        # Fill the arrowhead polygon
        painter.setPen(Qt.PenStyle.NoPen)  # No outline
        painter.setBrush(QBrush(color))  # Solid fill
        painter.drawPolygon(arrowhead)

        # Restore painter state
        painter.restore()

# ==============================================
# Hourly Weather Archive & Forecast
# ==============================================
# WeatherService above answers one question — "what is it doing at this park
# right now" — which is all the live HR widget ever needed.  Everything else
# needs an hour and a date: what the air was doing when a 2024 fly ball was
# struck, or what it will be doing at first pitch tonight.
#
# Open-Meteo serves all three regimes off one schema, which is why it is here
# rather than OpenWeather's paid history product:
#   * no API key, no quota to babysit
#   * hourly, back to 1940
#   * `surface_pressure` is STATION pressure, which is exactly the input
#     calculate_air_density wants.  OpenWeather hands back sea-level-normalised
#     pressure, which is why BallFlightSimulator carries that whole
#     high-altitude reconstruction for Coors.  Archive rows do not need it.
#   * every park in one request, each downscaled to its own field elevation.

OPEN_METEO_ARCHIVE  = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_HIRES    = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

# The high-resolution archive only reaches back to 2022; ERA5 covers earlier
# but on a ~25 km grid, which is coarse for a city-centre stadium.  They
# disagree — 2024-07-04 19:00 at Wrigley is 8.6mph@165 (ERA5) against
# 7.2mph@112 (hi-res), a 53 degree difference that would land in a different
# park-factor bucket.  Rows are tagged with the source they came from so the
# CPW training run can settle which one actually predicts carry, rather than
# us guessing now.
OPEN_METEO_HIRES_EPOCH = "2022-01-01"

_OM_HOURLY_VARS = (
    "temperature_2m", "relative_humidity_2m", "surface_pressure",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "precipitation", "cloud_cover", "weather_code",
)

# Upper levels, so the trajectory can use a real wind profile instead of one
# surface number.  ERA5 does not carry them (it returns nulls), so they are
# requested only from the model-based endpoints and their absence is normal.
_OM_WIND_LEVELS = ("wind_speed_80m", "wind_direction_80m",
                   "wind_speed_120m", "wind_direction_120m")

# WMO code -> the OpenWeather condition vocabulary WindVectorWidget's
# _CONDITION_MAP already speaks, so archive rows drive the same scenes as
# live ones with no branching at the call site.
_WMO_CONDITIONS = {
    0:  ("Clear", "clear sky"),
    1:  ("Clouds", "few clouds"),
    2:  ("Clouds", "scattered clouds"),
    3:  ("Clouds", "overcast clouds"),
    45: ("Fog", "fog"),              48: ("Fog", "depositing rime fog"),
    51: ("Drizzle", "light drizzle"), 53: ("Drizzle", "moderate drizzle"),
    55: ("Drizzle", "dense drizzle"), 56: ("Drizzle", "freezing drizzle"),
    57: ("Drizzle", "dense freezing drizzle"),
    61: ("Rain", "slight rain"),     63: ("Rain", "moderate rain"),
    65: ("Rain", "heavy rain"),      66: ("Rain", "freezing rain"),
    67: ("Rain", "heavy freezing rain"),
    71: ("Snow", "slight snowfall"), 73: ("Snow", "moderate snowfall"),
    75: ("Snow", "heavy snowfall"),  77: ("Snow", "snow grains"),
    80: ("Rain", "slight rain showers"), 81: ("Rain", "moderate rain showers"),
    82: ("Rain", "violent rain showers"),
    85: ("Snow", "slight snow showers"), 86: ("Snow", "heavy snow showers"),
    95: ("Thunderstorm", "thunderstorm"),
    96: ("Thunderstorm", "thunderstorm with slight hail"),
    99: ("Thunderstorm", "thunderstorm with heavy hail"),
}


# ----------------------------------------------------------------------------
# Roofs
# ----------------------------------------------------------------------------
# Measured, not assumed: 458 games at the eight roofed parks over 2026-04-01 to
# 2026-08-12, read off the StatsAPI live feed's gameData.weather block.
#
# `closed_rate` is how often the roof was actually shut, and it splits the group
# in two.  Houston (100%), Miami (95%), Texas (92%) and Arizona (73%) are indoor
# parks that occasionally open; Toronto (51%), Milwaukee (43%) and Seattle (12%)
# genuinely vary and are the only ones where predicting roof state is worth
# effort.  Use it as a prior when the roof state for a game is unknown.
#
# `indoor_temp_f` is a climate-control setpoint and is startlingly exact — Texas
# reported 74 degrees in all 54 closed games (sd 0.0), Miami 72 in all 54,
# Houston 73 in all 53.  Seattle is the exception and is deliberately None: its
# roof is an umbrella that covers without enclosing, so closing it does not heat
# the park (closed games average 54 degrees, simply because it closes when cold).
# Carry ambient temperature through there instead of overriding it.
#
# `wind_factor` is the share of outdoor wind still reaching the field with the
# roof shut.  Zero for the sealed parks.  Seattle again differs: of its closed
# games, MLB reported a live wind ("6 mph, Out To LF") in one of four, so 0.25
# is a weak point estimate off a small sample rather than a fitted number.
ROOF_BEHAVIOR = {
    "Minute Maid Park":      {"closed_rate": 1.00, "indoor_temp_f": 73.0, "wind_factor": 0.0},
    "Tropicana Field":       {"closed_rate": 1.00, "indoor_temp_f": 72.0, "wind_factor": 0.0},
    "LoanDepot Park":        {"closed_rate": 0.95, "indoor_temp_f": 72.0, "wind_factor": 0.0},
    "Globe Life Field":      {"closed_rate": 0.92, "indoor_temp_f": 74.0, "wind_factor": 0.0},
    "Chase Field":           {"closed_rate": 0.73, "indoor_temp_f": 75.6, "wind_factor": 0.0},
    "Rogers Centre":         {"closed_rate": 0.51, "indoor_temp_f": 68.1, "wind_factor": 0.0},
    "American Family Field": {"closed_rate": 0.43, "indoor_temp_f": 67.9, "wind_factor": 0.0},
    "T-Mobile Park":         {"closed_rate": 0.12, "indoor_temp_f": None, "wind_factor": 0.25},
}

# Tropicana is a fixed dome and reports "Dome"; the retractables report
# "Roof Closed".  Matching only the latter silently treats every Rays home game
# as open air.
_INDOOR_CONDITIONS = ("roof closed", "dome", "closed roof")

# MLB states observed wind in the FIELD frame, in eight buckets.  Values are the
# field-frame angle the wind blows FROM, matching wind_to_field_frame's output
# (0 = arriving from centre field).
MLB_WIND_LABELS = {
    "In From CF": 0.0,   "In From RF": 45.0,  "In From LF": 315.0,
    "Out To CF":  180.0, "Out To RF":  225.0, "Out To LF":  135.0,
    "R To L":     90.0,  "L To R":     270.0,
}


# MLB renames parks faster than this table does, and one differs only by case.
# Anything keyed on our names — ROOF_BEHAVIOR, PARK_ORIENTATION, STADIUM_DATA —
# must be looked up through resolve_park_name, or a renamed park silently falls
# through every lookup and is treated as an unknown open-air field.
PARK_NAME_ALIASES = {
    "oriole park at camden yards": "Camden Yards",
    "rate field": "Guaranteed Rate Field",          # renamed 2025
    "daikin park": "Minute Maid Park",              # renamed 2025
    "uniqlo field at dodger stadium": "Dodger Stadium",
    "loandepot park": "LoanDepot Park",             # case only
}


def resolve_park_name(name):
    """A venue name from anywhere -> the key this module uses, or None."""
    if not name:
        return None
    if name in STADIUM_DATA or name in PARK_ORIENTATION:
        return name
    low = name.lower()
    alias = PARK_NAME_ALIASES.get(low)
    if alias:
        return alias
    for key in STADIUM_DATA:
        if key.lower() == low:
            return key
    return None


def is_indoor_condition(condition):
    """True when a StatsAPI weather condition string means the roof is shut."""
    return any(tag in (condition or "").lower() for tag in _INDOOR_CONDITIONS)


def roof_closed_prior(park):
    """How often this park's roof is shut, 0-1.  Open-air parks give 0.0."""
    return ROOF_BEHAVIOR.get(park, {}).get("closed_rate", 0.0)


# Used when a roof is known to be shut over a park we have no measurements for
# — the Seoul and Tokyo series, or any new venue.  Sealed and around room
# temperature is much closer than the outdoor forecast, which otherwise reports
# things like 38F inside the Gocheok Sky Dome.
_GENERIC_INDOOR = {"closed_rate": 1.0, "indoor_temp_f": 72.0, "wind_factor": 0.0}


def apply_roof(row, park, closed=True):
    """Return a copy of an hourly row with the roof shut over it.

    Wind is scaled rather than zeroed because not every roof seals, and the
    temperature is replaced only where the park actually climate-controls.
    Leaves the row untouched when closed is false.

    `park` is matched through resolve_park_name, so a venue arriving under a
    sponsor's new name still finds its entry.
    """
    if not closed:
        return dict(row, roof_closed=False)
    behaviour = ROOF_BEHAVIOR.get(resolve_park_name(park) or park) or _GENERIC_INDOOR
    out = dict(row)
    factor = behaviour["wind_factor"]
    for key in ("wind_speed", "wind_gust"):
        if out.get(key) is not None:
            out[key] = out[key] * factor
    if behaviour["indoor_temp_f"] is not None:
        out["temperature"] = behaviour["indoor_temp_f"]
    out["roof_closed"] = True
    out["condition"] = "Dome" if park == "Tropicana Field" else "Roof Closed"
    out["description"] = "roof closed"
    out["precipitation"] = 0.0
    return out


def parse_gumbo_weather(weather, park=None):
    """StatsAPI gameData.weather -> a row in the shape everything else uses.

    This is the OBSERVED condition for a game that has been played, which beats
    any forecast for backfill.  Its wind direction is already field-relative, so
    the row carries `wind_frame: "field"` and must NOT be rotated again.
    Returns None if the block has nothing usable.
    """
    if not weather:
        return None
    condition = weather.get("condition") or ""
    indoor = is_indoor_condition(condition)
    try:
        temp = float(weather.get("temp"))
    except (TypeError, ValueError):
        temp = None
    speed, field_deg, label = None, None, None
    wind = weather.get("wind") or ""
    if "," in wind:
        speed_s, label = (part.strip() for part in wind.split(",", 1))
        try:
            speed = float(speed_s.lower().replace("mph", "").strip())
        except ValueError:
            speed = None
        field_deg = MLB_WIND_LABELS.get(label)
    return {
        "wind_speed": speed,
        "wind_direction": field_deg,
        "wind_frame": "field",       # already park-relative — do not rotate
        "wind_label": label,         # None/Calm/Varies survive here for callers
        "temperature": temp,
        "condition": condition,
        "description": condition.lower(),
        "roof_closed": indoor,
        "source": "statsapi",
    }


class WeatherArchive:
    """Hourly weather for ball parks — past, recent and forecast.

    Every method returns rows in the same shape WeatherService.
    extract_weather_data produces, so a row from 2024 and a row from tonight's
    forecast are interchangeable at the physics call site.  `wind_direction`
    is a compass bearing and is tagged `wind_frame: "compass"` accordingly —
    it still has to go through wind_to_field_frame (or calculate_trajectory's
    park_azimuth) before it means anything on a field.

    Responses are cached to disk because the CPW backfill re-reads the same
    park-seasons on every training run, and because a whole season for one
    park is a single request worth keeping.
    """

    def __init__(self, cache_dir=None, timeout=60):
        if cache_dir is None:
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "weather_cache")
        self.cache_dir = cache_dir
        self.timeout = timeout
        os.makedirs(self.cache_dir, exist_ok=True)

    # ---------------------------------------------------------------- fetch

    @staticmethod
    def pick_source(start_date, end_date):
        """'forecast', 'hires' or 'archive' for a date range.

        Both archives lag real time by a few days, so anything touching the
        last week has to come from the forecast endpoint (which serves past
        days too).  Below that, prefer the high-resolution archive and fall
        back to ERA5 only for dates it does not cover.
        """
        today = _dt.date.today()
        end = _dt.date.fromisoformat(str(end_date)[:10])
        if end >= today - _dt.timedelta(days=6):
            return "forecast"
        if str(start_date)[:10] >= OPEN_METEO_HIRES_EPOCH:
            return "hires"
        return "archive"

    def _cache_path(self, key):
        digest = hashlib.sha1(key.encode()).hexdigest()[:20]
        return os.path.join(self.cache_dir, f"om_{digest}.json")

    def _request(self, url, params, cache_key=None, retries=3):
        if cache_key:
            path = self._cache_path(cache_key)
            if os.path.exists(path):
                try:
                    with open(path) as fh:
                        return json.load(fh)
                except (OSError, ValueError):
                    pass  # corrupt cache entry — just refetch over it
        last = None
        for attempt in range(retries):
            try:
                r = requests.get(url, params=params, timeout=self.timeout)
                r.raise_for_status()
                payload = r.json()
                break
            except Exception as e:                       # network / 429 / 5xx
                last = e
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
        else:                                            # pragma: no cover
            raise last
        if cache_key:
            try:
                with open(self._cache_path(cache_key), "w") as fh:
                    json.dump(payload, fh)
            except OSError:
                pass                                     # cache is a nicety
        return payload

    # ----------------------------------------------------------- row shaping

    @staticmethod
    def _rows(block, source):
        """Open-Meteo's parallel arrays -> a list of per-hour dicts."""
        hourly = block.get("hourly") or {}
        times = hourly.get("time") or []
        out = []
        for i, stamp in enumerate(times):
            def val(name):
                seq = hourly.get(name) or []
                return seq[i] if i < len(seq) else None
            code = val("weather_code")
            cond, desc = _WMO_CONDITIONS.get(
                int(code) if code is not None else -1, ("Clear", "clear sky"))
            press_hpa = val("surface_pressure")
            out.append({
                "time": stamp,                       # ISO 8601, UTC
                "wind_speed": val("wind_speed_10m"),
                "wind_direction": val("wind_direction_10m"),
                "wind_frame": "compass",
                "wind_gust": val("wind_gusts_10m"),
                "temperature": val("temperature_2m"),
                "humidity": val("relative_humidity_2m"),
                "pressure_hpa": press_hpa,
                "pressure_pa": press_hpa * 100.0 if press_hpa is not None else None,
                # Taken at field level (we send each park's own elevation), so
                # it must NOT go through BallFlightSimulator's sea-level
                # reconstruction — see pressure_is_station there.
                "pressure_frame": "station",
                "precipitation": val("precipitation"),
                "cloud_cover": val("cloud_cover"),
                "wind_speed_80m": val("wind_speed_80m"),
                "wind_direction_80m": val("wind_direction_80m"),
                "wind_speed_120m": val("wind_speed_120m"),
                "condition": cond,
                "description": desc,
                "source": source,
                # What the provider actually resolved us to, so a bad park
                # coordinate shows up as a number rather than as a quietly
                # wrong air density.
                "grid_elevation_m": block.get("elevation"),
            })
        return out

    # -------------------------------------------------------------- history

    def hourly_points(self, points, start_date, end_date, source="auto"):
        """Hourly rows for several locations in ONE request.

        points: sequence of (name, lat, lon, elevation_ft).  Elevation is sent
        per location so surface_pressure comes back downscaled to the field
        rather than to whatever the grid cell happens to sit at — the two
        differ by 36 m at Kauffman, and pressure is a direct air-density term.

        Returns {name: [row, ...]}.
        """
        points = list(points)
        if not points:
            return {}
        if source == "auto":
            source = self.pick_source(start_date, end_date)
        url = {"archive": OPEN_METEO_ARCHIVE, "hires": OPEN_METEO_HIRES,
               "forecast": OPEN_METEO_FORECAST}[source]

        params = {
            "latitude":  ",".join(f"{p[1]:.4f}" for p in points),
            "longitude": ",".join(f"{p[2]:.4f}" for p in points),
            "elevation": ",".join(f"{(p[3] or 0) * 0.3048:.0f}" for p in points),
            "hourly": ",".join(_OM_HOURLY_VARS + (
                () if source == "archive" else _OM_WIND_LEVELS)),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "UTC",
            "start_date": str(start_date)[:10],
            "end_date": str(end_date)[:10],
        }
        # The variable list MUST be part of the key.  Without it, adding a
        # field (the 80/120 m wind levels) silently replays cached responses
        # that never contained it, and the new column comes back 100% null
        # while every other check still passes.
        cache_key = f"{source}|{params['latitude']}|{params['elevation']}|" \
                    f"{params['start_date']}|{params['end_date']}|{params['hourly']}"
        payload = self._request(url, params, cache_key=cache_key)
        # One location comes back as an object, several as a list.
        blocks = payload if isinstance(payload, list) else [payload]
        return {p[0]: self._rows(b, source)
                for p, b in zip(points, blocks)}

    def park_hours(self, park, start_date, end_date, source="auto"):
        """Hourly rows for one park over a date range."""
        info = STADIUM_DATA.get(park)
        if not info:
            return []
        got = self.hourly_points(
            [(park, info["lat"], info["lon"], info.get("altitude", 0))],
            start_date, end_date, source=source)
        return got.get(park, [])

    def all_parks_hours(self, start_date, end_date, source="auto"):
        """Every park in STADIUM_DATA over a date range, in one request."""
        pts = [(name, d["lat"], d["lon"], d.get("altitude", 0))
               for name, d in sorted(STADIUM_DATA.items())]
        return self.hourly_points(pts, start_date, end_date, source=source)

    # ------------------------------------------------------------- forecast

    def park_forecast(self, park, forecast_days=3, past_days=0):
        """Upcoming hourly rows for a park — this is what a game-time lookup
        wants when the game has not been played yet."""
        info = STADIUM_DATA.get(park)
        if not info:
            return []
        params = {
            "latitude": f"{info['lat']:.4f}",
            "longitude": f"{info['lon']:.4f}",
            "elevation": f"{info.get('altitude', 0) * 0.3048:.0f}",
            "hourly": ",".join(_OM_HOURLY_VARS + _OM_WIND_LEVELS),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "UTC",
            "forecast_days": int(forecast_days),
            "past_days": int(past_days),
        }
        # Deliberately uncached: a forecast that is an hour stale is worse
        # than no cache at all, and it is one cheap request.
        payload = self._request(OPEN_METEO_FORECAST, params)
        return self._rows(payload, "forecast")

    # ------------------------------------------------------- point lookup

    def at(self, park, when, source="auto"):
        """The single hour covering `when` (a UTC datetime) at `park`.

        Rounds to the nearest hour rather than truncating — a 7:10 pm first
        pitch is better served by the 7 pm row either way, but a 7:50 pm
        one is not.
        """
        if isinstance(when, str):
            when = _dt.datetime.fromisoformat(when)
        if when.tzinfo is not None:
            when = when.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        target = (when + _dt.timedelta(minutes=30)).replace(
            minute=0, second=0, microsecond=0)
        day = target.date()
        if source == "auto":
            source = self.pick_source(day, day)
        if source == "forecast":
            rows = self.park_forecast(park, forecast_days=7, past_days=5)
        else:
            rows = self.park_hours(park, day, day, source=source)
        stamp = target.strftime("%Y-%m-%dT%H:00")
        for row in rows:
            if row["time"][:13] == stamp[:13]:
                return row
        return None


# ==============================================
# Effective wind
# ==============================================
# BallFlightSimulator's `wind_speed` means a UNIFORM, STEADY vector applied
# over the ball's entire flight, at every height and across the whole field.
# A forecast (or a stadium anemometer) reports something quite different: a
# 10 m, open-terrain, hourly-average scalar at one point.  Inside a bowl those
# diverge — the grandstand blocks and separates the flow, the ball spends most
# of its flight well above the reference height, gusting means the hourly mean
# is not what any 5-second flight sees, and in a swirling park the mean bearing
# says little about any particular ball.
#
# Measured on 116k batted balls: real fly-ball distance responds to reported
# wind at ~0.30-0.41 ft/mph, while the simulator responds at 3.59 (literature
# for a genuinely uniform wind is 2.5-3, so the ODE itself is fine).  MLB's own
# on-field reading predicts distance NO BETTER than the grid forecast, so this
# is not a data-quality problem — the reported number simply is not the
# quantity the physics wants.
#
# The correction therefore lives HERE, at the boundary where a forecast becomes
# a model input, rather than inside the ODE.  The simulator stays physically
# honest for a wind you actually know; this function converts a reported wind
# into the uniform-equivalent that reproduces observed carry.
#
# Note what this is NOT: it is not the fraction of air that physically reaches
# the field.  It is the regression-calibrated effective wind given a surface
# observation, and it absorbs both genuine bowl attenuation and the fact that
# a point measurement is a noisy proxy for a whole flow field.
_WIND_RECEPTIVITY = None
EFFECTIVE_WIND_FALLBACK = 0.15


def wind_receptivity(park=None):
    """Per-park effective-wind factor, fitted by
    `python homerunwidget.py calibrate-wind`."""
    global _WIND_RECEPTIVITY
    if _WIND_RECEPTIVITY is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "model_data", "wind_receptivity.json")
        try:
            with open(path) as fh:
                _WIND_RECEPTIVITY = json.load(fh)
        except (OSError, ValueError):
            _WIND_RECEPTIVITY = {}
    if not _WIND_RECEPTIVITY:
        return EFFECTIVE_WIND_FALLBACK
    key = resolve_park_name(park) or park
    entry = _WIND_RECEPTIVITY.get(key)
    if entry and entry.get("wind_mult") is not None:
        return float(entry["wind_mult"])
    glob = _WIND_RECEPTIVITY.get("_global", {})
    return float(glob.get("wind_mult_2pass")
                 or glob.get("wind_mult") or EFFECTIVE_WIND_FALLBACK)


def wind_profile(levels, alpha_default=0.20):
    """Build a callable h_metres -> wind speed, from measured levels.

    `levels` is {height_m: speed_mph} — normally 10/80/120 m off Open-Meteo.
    Wind grows with height roughly as a power law, u(h) = u_ref * (h/h_ref)^a,
    and fitting `a` from two levels beats assuming one: over the corpus it runs
    well above the 0.14 of open flat terrain, which is what you would expect
    over a city with a stadium in it.

    Why this matters: the ball flies from 1 m to ~45 m, and the forecast is
    quoted at 10 m.  Applying the 10 m number flat across the whole flight
    understates the wind through most of it.  Air density has varied with
    height in this engine for years; wind was the one input still held
    constant.

    Falls back to a default exponent when only one level is present, and to a
    flat profile when none is.
    """
    pairs = sorted((float(h), float(s)) for h, s in levels.items()
                   if s is not None and h and float(s) >= 0)
    if not pairs:
        return lambda h: 0.0
    if len(pairs) == 1:
        h0, s0 = pairs[0]
        return lambda h: s0 * (max(h, 0.5) / h0) ** alpha_default

    # Least-squares power law through log(h), log(u); guard the zero-wind case
    # where the log is undefined.
    hs = np.array([p[0] for p in pairs])
    us = np.array([p[1] for p in pairs])
    good = us > 0.1
    if good.sum() < 2:
        h0, s0 = pairs[0]
        return lambda h: s0 * (max(h, 0.5) / h0) ** alpha_default
    alpha, log_u0 = np.polyfit(np.log(hs[good]), np.log(us[good]), 1)
    # A profile that falls off with height, or climbs absurdly, is a bad fit
    # rather than real weather — clamp to the physically sensible range.
    alpha_clamped = float(np.clip(alpha, 0.0, 0.45))
    if abs(alpha_clamped - alpha) < 1e-12:
        u0 = float(np.exp(log_u0))
    else:
        # Re-anchor to the lowest measured level.  Keeping the intercept from
        # the unclamped fit alongside a clamped exponent is incoherent — it
        # turned a 9 mph surface reading into 47 mph at 40 m.
        h_ref, u_ref = hs[good][0], us[good][0]
        u0 = float(u_ref / h_ref ** alpha_clamped)
    alpha = alpha_clamped
    return lambda h: u0 * max(h, 0.5) ** alpha


def profile_from_row(row):
    """wind_profile() built from an hourly row's multi-level wind columns."""
    return wind_profile({
        10.0: row.get("wind_speed"),
        80.0: row.get("wind_speed_80m"),
        120.0: row.get("wind_speed_120m"),
    })


def effective_wind(speed_mph, park=None):
    """Reported surface wind -> the uniform-equivalent the trajectory wants.

    Feed THIS to BallFlightSimulator, and keep the reported value for display:
    a fan reading "12 mph out to left" wants the forecast number, while the
    sim wants the much smaller wind that actually reproduces observed carry.
    """
    if speed_mph is None:
        return None
    return float(speed_mph) * wind_receptivity(park)


def game_conditions(park, first_pitch_utc, hours=3, roof_closed=None,
                    archive=None, source="auto"):
    """Hour-by-hour conditions over a game, ready for the physics engine.

    This is the call the weather tab is built on.  A ball game is not an
    instant — first pitch and the ninth inning are different environments, and
    a park factor computed off a single reading averages that away.  Returns
    `hours` consecutive rows starting at first pitch, which is where
    BallparkPal's Hour 1 / Hour 2 / Hour 3 columns come from.

    Each row gains `wind_field_deg`, the wind rotated into the field frame, so
    callers never have to remember to do it.  A row whose park has no azimuth
    gets None there rather than a plausible-looking wrong number.

    roof_closed: True/False to state it, or None to fall back to the park's
    measured closure rate — under which an always-indoor park like Minute Maid
    is treated as closed and a mostly-open one as open.  Pass the real state
    from StatsAPI whenever the game has been played.
    """
    archive = archive or WeatherArchive()
    if isinstance(first_pitch_utc, str):
        first_pitch_utc = _dt.datetime.fromisoformat(
            first_pitch_utc.replace("Z", "+00:00"))
    if first_pitch_utc.tzinfo is not None:
        first_pitch_utc = first_pitch_utc.astimezone(
            _dt.timezone.utc).replace(tzinfo=None)

    start = first_pitch_utc.replace(minute=0, second=0, microsecond=0)
    end = start + _dt.timedelta(hours=max(1, hours) - 1)
    if source == "auto":
        source = archive.pick_source(start.date(), end.date())
    if source == "forecast":
        rows = archive.park_forecast(park, forecast_days=7, past_days=5)
    else:
        rows = archive.park_hours(park, start.date(), end.date(), source=source)

    if roof_closed is None:
        roof_closed = roof_closed_prior(park) >= 0.5

    return _finish_hours(rows, park, start, hours, roof_closed)


def _finish_hours(rows, park, start, hours, roof_closed):
    """Slice `rows` to the game's hours and put them in the park's frame.

    Shared by game_conditions and slate_conditions so the single-park and
    batched paths cannot drift apart -- the roof, the rotation and the wind
    weighting all have to be applied identically or the tab and any one-off
    lookup would disagree.
    """
    azimuth = get_park_azimuth(park)
    wanted = [(start + _dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H")
              for i in range(max(1, hours))]
    by_hour = {r["time"][:13]: r for r in rows}
    out = []
    for stamp in wanted:
        row = by_hour.get(stamp)
        if row is None:
            continue
        row = apply_roof(row, park, closed=roof_closed)
        row["wind_field_deg"] = (
            None if row.get("roof_closed") and not row.get("wind_speed")
            else wind_to_field_frame(row["wind_direction"], azimuth=azimuth))
        # Both winds ride along: `wind_speed` is what the forecast says and
        # what a reader expects to see; `wind_effective_mph` is the weighted
        # value the trajectory model should actually be given.
        row["wind_effective_mph"] = effective_wind(row.get("wind_speed"), park)
        row["wind_receptivity"] = wind_receptivity(park)
        row["park"] = park
        out.append(row)
    return out


def slate_conditions(entries, hours=3, archive=None, forecast_days=3,
                     past_days=1):
    """game_conditions for a whole slate, in ONE request.

    `entries` is [(park, first_pitch_utc), ...]; returns {park: [row, ...]}.

    Per-park fetching cost 0.83s each and 11.7s for a fourteen-game card --
    79% of the tab's load, for data Open-Meteo will return in a single
    multi-location call in 1.4s. Same schema out; the per-row finishing is
    shared with game_conditions.
    """
    archive = archive or WeatherArchive()
    parks, when = [], {}
    for park, first_pitch in entries:
        if park not in STADIUM_DATA:
            continue
        t = first_pitch
        if isinstance(t, str):
            t = _dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        if t.tzinfo is not None:
            t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        if park not in when:
            parks.append(park)
        # A doubleheader lands two games on one park; the earlier start
        # bounds the window both of them need.
        when[park] = min(when.get(park, t), t)
    if not parks:
        return {}

    points = [(p, STADIUM_DATA[p]["lat"], STADIUM_DATA[p]["lon"],
               STADIUM_DATA[p].get("altitude", 0)) for p in parks]
    params = {
        "latitude": ",".join(f"{p[1]:.4f}" for p in points),
        "longitude": ",".join(f"{p[2]:.4f}" for p in points),
        "elevation": ",".join(f"{(p[3] or 0) * 0.3048:.0f}" for p in points),
        "hourly": ",".join(_OM_HOURLY_VARS + _OM_WIND_LEVELS),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "UTC",
        "forecast_days": int(forecast_days),
        "past_days": int(past_days),
    }
    # Uncached, like park_forecast: a stale forecast is worse than no cache.
    payload = archive._request(OPEN_METEO_FORECAST, params)
    blocks = payload if isinstance(payload, list) else [payload]

    out = {}
    for (park, *_), block in zip(points, blocks):
        rows = archive._rows(block, "forecast")
        start = when[park].replace(minute=0, second=0, microsecond=0)
        out[park] = _finish_hours(rows, park, start, hours,
                                  roof_closed_prior(park) >= 0.5)
    return out


def draw_precipitation(painter, precipitation, width, height, is_in_stadium_area_func):
    """Draw precipitation visualization"""
    # Simple rain visualization
    if precipitation > 0:
        painter.setPen(QPen(QColor(0, 0, 200, 100), 1))

        # Number of raindrops based on precipitation intensity
        num_drops = int(precipitation * 200)

        for _ in range(min(num_drops, 1000)):  # Cap at 1000 drops
            x = np.random.randint(0, width)
            y = np.random.randint(0, height)

            # Skip raindrops outside the stadium area
            if not is_in_stadium_area_func(x, y):
                continue

            # Draw a simple raindrop
            length = 5 + np.random.randint(0, 5)
            start_point = QPointF(float(x), float(y))
            end_point = QPointF(float(x), float(y + length))
            painter.drawLine(start_point, end_point)


# ═══════════════════════════════════════════════════════════════════════════════
# WEATHER ANIMATION WIDGET
# WindVectorWidget + particle classes (_Cloud, _RainDrop, _FogBank, _MistDrop,
# _LightningBolt, _Snowflake) — imported by homerunwidget.py
# ═══════════════════════════════════════════════════════════════════════════════
class WindVectorWidget(QWidget):
    """
    Animated wind-vector banner that shows:
      • A weather-condition-appropriate animated background (sky, clouds, rain, snow, etc.)
      • Flowing tapered arrows that travel in the actual wind direction at a speed
        proportional to wind speed — replacing the old pulsing static arrows.
    """

    # ── map OpenWeatherMap 'main' condition strings to our scene keys ──────────
    _CONDITION_MAP = {
        "Clear":         "clear",
        "Clouds":        "clouds",   # overcast or broken — refined by description
        "Rain":          "rain",
        "Drizzle":       "rain",
        "Thunderstorm":  "thunder",
        "Snow":          "snow",
        "Mist":          "mist",
        "Fog":           "fog",
        "Haze":          "haze",
        "Smoke":         "haze",
        "Dust":          "haze",
        "Sand":          "haze",
        "Ash":           "haze",
        "Squall":        "rain",
        "Tornado":       "thunder",
        "Custom":        "clear",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMaximumHeight(180)

        # Wind / weather state
        self.wind_speed     = 0.0
        self.wind_direction = 0.0
        self.condition      = "clear"   # resolved scene key

        # Arrow particles  [{'x','y','spd','alpha','growing'}]
        self._arrows        = []
        self._arrow_vx      = 1.0
        self._arrow_vy      = 0.0

        # Arrow appearance (recomputed on set_wind_data)
        self._arrow_len     = 54
        self._arrow_w       = 11
        self._tail_len      = 28
        self._px_per_frame  = 1.2
        self._arrow_count   = 5
        self._ar, self._ag, self._ab = 80, 210, 255

        # Scene particles for weather effects
        self._clouds   = []   # Cloud instances  (broken/overcast)
        self._drops    = []   # RainParticle / MistDrop instances
        self._flakes   = []   # SnowParticle instances
        self._bolt     = None # LightningState instance
        self._fog_banks= []   # FogBank instances

        # Scene time (milliseconds, driven by timer)
        self._t = 0.0

        # High-res animation timer  (~60 fps) — only runs while visible
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        # Don't start yet; showEvent will start it when the widget is mapped.

    def showEvent(self, event):
        super().showEvent(event)
        if not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._timer.isActive():
            self._timer.stop()

    # ── public API ────────────────────────────────────────────────────────────

    def set_wind_data(self, speed, direction, condition="Clear", description=""):
        self.wind_speed     = speed
        self.wind_direction = direction

        # Resolve scene key
        scene = self._CONDITION_MAP.get(condition, "clear")
        # Distinguish "few/scattered clouds" (broken) from "overcast"
        if condition == "Clouds":
            desc_lower = description.lower()
            if "overcast" in desc_lower:
                scene = "overcast"
            else:
                scene = "clouds"
        self.condition = scene

        # Wind vector  (met convention: FROM direction → going = dir+180)
        going = (direction + 180) % 360
        rad   = math.radians(90 - going)
        self._arrow_vx = math.cos(rad)
        self._arrow_vy = -math.sin(rad)

        # Arrow visual params scaled by speed
        spd = max(1.0, speed)
        self._arrow_count  = max(3, min(10, int(spd * 0.35 + 3)))
        self._arrow_len    = max(28, min(78, int(spd * 2.2 + 32)))
        self._arrow_w      = max(6,  min(18, int(spd * 0.45 + 7)))
        self._tail_len     = max(14, min(52, int(spd * 1.4 + 16)))
        self._px_per_frame = max(0.4, min(3.8, spd * 0.13 + 0.3))

        # Arrow colour by speed
        if   spd <  5: self._ar, self._ag, self._ab = 80, 210, 255
        elif spd < 12: self._ar, self._ag, self._ab = 80, 255, 160
        elif spd < 20: self._ar, self._ag, self._ab = 255, 205, 55
        else:          self._ar, self._ag, self._ab = 255, 75,  75

        # Rebuild particles for the new wind direction / count
        self._init_arrows()
        self._init_scene_particles()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _init_arrows(self):
        self._arrows = []
        W, H = self.width() or 600, self.height() or 180
        for _ in range(self._arrow_count):
            self._arrows.append(self._spawn_arrow(W, H, scatter=True))

    def _spawn_arrow(self, W, H, scatter=False):
        vx, vy = self._arrow_vx, self._arrow_vy
        L = self._arrow_len
        if scatter:
            x, y = random.random() * W, random.random() * H
        else:
            if abs(vx) >= abs(vy):
                x = -L if vx > 0 else W + L
                y = H * 0.12 + random.random() * H * 0.76
            else:
                x = W * 0.08 + random.random() * W * 0.84
                y = -L if vy > 0 else H + L
        spd_scale = max(0.5, self.wind_speed / 8.0)
        return {
            'x': x, 'y': y,
            'spd': (self._px_per_frame + random.random() * 0.5) * spd_scale,
            'alpha': random.uniform(0.15, 0.80) if scatter else 0.05,
            'growing': not scatter,
        }

    def _recycle_arrow(self, a, W, H):
        a['growing'] = True
        a['alpha']   = 0.05
        vx, vy = self._arrow_vx, self._arrow_vy
        L = self._arrow_len
        spd_scale = max(0.5, self.wind_speed / 8.0)
        a['spd'] = (self._px_per_frame + random.random() * 0.5) * spd_scale
        if abs(vx) >= abs(vy):
            a['x'] = -L if vx > 0 else W + L
            a['y'] = H * 0.12 + random.random() * H * 0.76
        else:
            a['x'] = W * 0.08 + random.random() * W * 0.84
            a['y'] = -L if vy > 0 else H + L

    def _init_scene_particles(self):
        W, H = self.width() or 600, self.height() or 180
        vx, vy = self._arrow_vx, self._arrow_vy
        scene = self.condition

        self._clouds    = []
        self._drops     = []
        self._flakes    = []
        self._bolt      = None
        self._fog_banks = []

        if scene in ("clouds",):
            for spd in (0.28, 0.18, 0.38, 0.24):
                self._clouds.append(_Cloud(W, H, spd, scatter=True))
        elif scene == "overcast":
            for spd in (0.20, 0.13, 0.30, 0.17, 0.25):
                self._clouds.append(_Cloud(W, H, spd, scatter=True, dark=True))
        elif scene == "mist":
            self._drops = [_MistDrop(W, H, vx, vy) for _ in range(220)]
        elif scene == "fog":
            self._fog_banks = [_FogBank(W, H) for _ in range(6)]
        elif scene == "rain":
            self._drops = [_RainDrop(W, H, vx, vy, heavy=False) for _ in range(100)]
        elif scene == "thunder":
            self._drops = [_RainDrop(W, H, vx, vy, heavy=True)  for _ in range(160)]
            self._bolt  = _LightningBolt(W, H)
        elif scene == "snow":
            self._flakes = [_Snowflake(W, H, vx) for _ in range(75)]

    # ── animation tick (called by QTimer) ────────────────────────────────────

    def _tick(self):
        self._t += 16.0   # ~16 ms per frame
        self.update()     # schedule a repaint

    # ── Qt paint ──────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # If particles haven't been sized for this widget size, reinit
        if not self._arrows:
            self._init_arrows()
            self._init_scene_particles()

        t = self._t
        scene = self.condition

        # ── 1. Draw background ────────────────────────────────────────────
        self._draw_background(painter, W, H, t, scene)

        # ── 2. Tick + draw scene particles ────────────────────────────────
        self._tick_and_draw_particles(painter, W, H, scene)

        # ── 3. Draw flowing wind arrows ───────────────────────────────────
        self._draw_arrows(painter, W, H)

        painter.end()

    # ── background scenes ─────────────────────────────────────────────────────

    def _draw_background(self, painter, W, H, t, scene):
        if scene == "clear":
            self._bg_clear(painter, W, H, t)
        elif scene == "clouds":
            self._bg_blue_sky(painter, W, H)        # sky behind clouds drawn separately
        elif scene == "overcast":
            self._bg_overcast(painter, W, H, t)
        elif scene == "rain":
            self._bg_rain(painter, W, H)
        elif scene == "mist":
            self._bg_mist(painter, W, H, t)
        elif scene == "fog":
            self._bg_fog(painter, W, H, t)
        elif scene == "thunder":
            self._bg_thunder(painter, W, H)
        elif scene == "snow":
            self._bg_snow(painter, W, H)
        elif scene == "haze":
            self._bg_haze(painter, W, H, t)
        else:
            self._bg_clear(painter, W, H, t)

    # ── individual background painters ───────────────────────────────────────

    def _bg_clear(self, painter, W, H, t):
        grad = QLinearGradient(0, 0, 0, H)
        grad.setColorAt(0.0, QColor(9,   30,  74))
        grad.setColorAt(0.5, QColor(18,  82, 160))
        grad.setColorAt(1.0, QColor(40, 120, 200))
        painter.fillRect(0, 0, W, H, QBrush(grad))

        # animated sun
        sx = int(W * 0.80 + math.sin(t * 0.00025) * 6)
        sy = int(H * 0.30 + math.cos(t * 0.00018) * 3)
        for radius, alpha in ((64, 28), (40, 55), (22, 110), (20, 220)):
            c = QColor(255, 225, 80, alpha)
            painter.setBrush(QBrush(c)); painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(sx, sy), radius, radius)

    def _bg_blue_sky(self, painter, W, H):
        grad = QLinearGradient(0, 0, 0, H)
        grad.setColorAt(0.0, QColor(18,  34,  64))
        grad.setColorAt(0.6, QColor(30,  72, 117))
        grad.setColorAt(1.0, QColor(46, 106, 170))
        painter.fillRect(0, 0, W, H, QBrush(grad))

    def _bg_overcast(self, painter, W, H, t):
        # Deep slate base
        grad = QLinearGradient(0, 0, 0, H)
        grad.setColorAt(0.0, QColor(28, 32, 38))
        grad.setColorAt(0.5, QColor(44, 50, 60))
        grad.setColorAt(1.0, QColor(58, 66, 76))
        painter.fillRect(0, 0, W, H, QBrush(grad))

        # Rows of cloud blobs, each row scrolling at its own speed — no wobble
        cloud_layers = [
            # (speed, y_frac, blob_spacing, blob_rw, blob_rh, color, alpha)
            (0.018, 0.12, 115, 82, 46, QColor(88,  98, 112), 200),
            (0.010, 0.30,  95, 72, 40, QColor(68,  78,  92), 225),
            (0.024, 0.50, 105, 70, 38, QColor(100,112, 126), 175),
            (0.007, 0.68, 125, 92, 50, QColor(76,  88, 102), 155),
        ]

        painter.setPen(Qt.PenStyle.NoPen)
        for spd, yf, spacing, rw, rh, color, max_alpha in cloud_layers:
            offset = (t * spd) % spacing
            cy = H * yf
            x = -offset - rw
            while x < W + rw:
                cg = QRadialGradient(x - rw * 0.15, cy - rh * 0.25, 0)
                cg.setCenter(x, cy)
                cg.setRadius(max(rw, rh))
                cg.setFocalPoint(x - rw * 0.15, cy - rh * 0.25)
                c_inner = QColor(color); c_inner.setAlpha(max_alpha)
                c_mid   = QColor(color); c_mid.setAlpha(int(max_alpha * 0.55))
                c_outer = QColor(color); c_outer.setAlpha(0)
                cg.setColorAt(0.0, c_inner)
                cg.setColorAt(0.5, c_mid)
                cg.setColorAt(1.0, c_outer)
                painter.save()
                painter.setBrush(QBrush(cg))
                painter.drawEllipse(QPointF(x, cy), float(rw), float(rh))
                painter.restore()
                x += spacing

        # Dark ceiling from top
        ceiling = QLinearGradient(0, 0, 0, H * 0.40)
        ceiling.setColorAt(0.0, QColor(18, 20, 26, 200))
        ceiling.setColorAt(1.0, QColor(18, 20, 26, 0))
        painter.fillRect(0, 0, W, int(H * 0.40), QBrush(ceiling))

    def _bg_rain(self, painter, W, H):
        grad = QLinearGradient(0, 0, 0, H)
        grad.setColorAt(0.0, QColor(12, 19, 24))
        grad.setColorAt(0.5, QColor(20, 30, 40))
        grad.setColorAt(1.0, QColor(28, 44, 58))
        painter.fillRect(0, 0, W, H, QBrush(grad))
        cloud_grad = QLinearGradient(0, 0, 0, int(H * 0.42))
        cloud_grad.setColorAt(0.0, QColor(14, 18, 26, 230))
        cloud_grad.setColorAt(1.0, QColor(22, 32, 44, 0))
        painter.fillRect(0, 0, W, int(H * 0.42), QBrush(cloud_grad))

    def _bg_mist(self, painter, W, H, t):
        # Cool blue-grey sky — muted and low visibility
        grad = QLinearGradient(0, 0, 0, H)
        grad.setColorAt(0.0,  QColor(62,  74,  84))
        grad.setColorAt(0.55, QColor(86, 100, 112))
        grad.setColorAt(1.0,  QColor(104, 118, 130))
        painter.fillRect(0, 0, W, H, QBrush(grad))

        # Thin gauzy veil across the whole frame — low visibility feeling
        veil = QLinearGradient(0, 0, 0, H)
        veil.setColorAt(0.0, QColor(160, 178, 190, 55))
        veil.setColorAt(1.0, QColor(172, 188, 198, 88))
        painter.fillRect(0, 0, W, H, QBrush(veil))

    def _bg_fog(self, painter, W, H, t):
        # Pale, washed-out sky — visibility near zero
        grad = QLinearGradient(0, 0, 0, H)
        grad.setColorAt(0.0,  QColor(148, 158, 165))
        grad.setColorAt(0.50, QColor(168, 178, 184))
        grad.setColorAt(1.0,  QColor(182, 192, 198))
        painter.fillRect(0, 0, W, H, QBrush(grad))

        # ── City skyline silhouette in lower ~40% of frame ───────────────────
        # Deterministic buildings derived from W so they don't jitter each frame
        painter.setPen(Qt.PenStyle.NoPen)
        building_color = QColor(88, 94, 102, 210)
        painter.setBrush(QBrush(building_color))

        # Use a fixed seed-like sequence based on W to get stable widths/heights
        bx = 0
        idx = 0
        while bx < W:
            # pseudo-random but stable: vary by position
            w  = 28 + (((bx * 7 + idx * 31) % 40))
            h  = int(H * (0.22 + ((bx * 13 + idx * 17) % 100) / 100.0 * 0.28))
            by = H - h
            painter.drawRect(bx, by, w - 2, h)

            # Some buildings get a small antenna/spire
            if (bx * 3 + idx) % 5 == 0:
                spire_w = 3
                spire_h = int(h * 0.18 + 6)
                painter.drawRect(bx + w // 2 - 1, by - spire_h, spire_w, spire_h)

            # Dim lit windows — small rectangles, fixed positions per building
            win_color = QColor(210, 200, 160, 55)
            painter.setBrush(QBrush(win_color))
            rows = max(1, h // 18)
            cols = max(1, (w - 4) // 10)
            for row in range(rows):
                for col in range(cols):
                    if (bx + row * 7 + col * 13) % 3 != 0:  # skip some — not all lit
                        wx2 = bx + 4 + col * 10
                        wy  = by + 6 + row * 16
                        if wy + 6 < H:
                            painter.drawRect(wx2, wy, 5, 6)
            painter.setBrush(QBrush(building_color))

            bx  += w
            idx += 1

        # Slightly darker ground strip below skyline
        ground = QLinearGradient(0, int(H * 0.78), 0, H)
        ground.setColorAt(0.0, QColor(70, 76, 84, 180))
        ground.setColorAt(1.0, QColor(55, 60, 68, 255))
        painter.fillRect(0, int(H * 0.78), W, int(H * 0.22), QBrush(ground))

    def _bg_thunder(self, painter, W, H):
        # Dark stormy sky — flashes brighter when a lightning bolt is active
        fl = 0
        if self._bolt and self._bolt.visible and self._bolt.alpha > 0:
            fl = self._bolt.alpha * 0.10
        r0 = int(10 + fl * 90); g0 = int(6  + fl * 40); b0 = int(18 + fl * 70)
        r1 = int(15 + fl * 60); g1 = int(10 + fl * 35); b1 = int(28 + fl * 55)
        r2 = int(20 + fl * 40); g2 = int(16 + fl * 30); b2 = int(36 + fl * 40)
        grad = QLinearGradient(0, 0, 0, H)
        grad.setColorAt(0.0, QColor(r0, g0, b0))
        grad.setColorAt(0.6, QColor(r1, g1, b1))
        grad.setColorAt(1.0, QColor(r2, g2, b2))
        painter.fillRect(0, 0, W, H, QBrush(grad))

    def _bg_snow(self, painter, W, H):
        grad = QLinearGradient(0, 0, 0, H)
        grad.setColorAt(0.0,  QColor(24, 32, 48))
        grad.setColorAt(0.55, QColor(38, 56, 72))
        grad.setColorAt(1.0,  QColor(52, 72, 88))
        painter.fillRect(0, 0, W, H, QBrush(grad))
        ceil_grad = QLinearGradient(0, 0, 0, int(H * 0.38))
        ceil_grad.setColorAt(0.0, QColor(72, 88, 108, 140))
        ceil_grad.setColorAt(1.0, QColor(72, 88, 108, 0))
        painter.fillRect(0, 0, W, int(H * 0.38), QBrush(ceil_grad))

    def _bg_haze(self, painter, W, H, t):
        grad = QLinearGradient(0, 0, 0, H)
        grad.setColorAt(0.0,  QColor(48, 40, 24))
        grad.setColorAt(0.55, QColor(80, 64, 48))
        grad.setColorAt(1.0,  QColor(112, 88, 72))
        painter.fillRect(0, 0, W, H, QBrush(grad))
        haze_layers = [(0.008, 0.55, 28), (0.005, 0.72, 20), (0.013, 0.38, 18)]
        for spd, yf, alpha in haze_layers:
            offset = (t * spd) % W
            cy = H * yf
            for rep in range(-1, 3):
                cx = -offset + rep * W + W * 0.5
                hg = QRadialGradient(cx, cy, W * 0.55)
                hg.setColorAt(0.0, QColor(215, 178, 88, alpha))
                hg.setColorAt(0.5, QColor(205, 168, 72, int(alpha * 0.4)))
                hg.setColorAt(1.0, QColor(205, 168, 72, 0))
                painter.save()
                painter.setBrush(QBrush(hg)); painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(QPointF(cx, cy), W * 0.55, H * 0.13)
                painter.restore()
        # diffuse sun
        sx, sy = W * 0.76, H * 0.24
        for radius, alpha in ((66, 20), (42, 50), (15, 165)):
            c = QColor(255, 210, 80, alpha)
            painter.setBrush(QBrush(c)); painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(sx, sy), radius, radius)

    # ── particle tick + draw ──────────────────────────────────────────────────

    def _tick_and_draw_particles(self, painter, W, H, scene):
        if scene in ("clouds", "overcast"):
            for c in self._clouds:
                c.tick(1.0)
                c.draw(painter)
        if scene in ("rain", "thunder", "mist"):
            for d in self._drops:
                d.tick(1.0)
                d.draw(painter)
        if scene == "fog":
            for fb in self._fog_banks:
                fb.tick(1.0)
                fb.draw(painter)
        if scene == "thunder" and self._bolt:
            self._bolt.tick()
            self._bolt.draw(painter, W, H)
        if scene == "snow":
            for f in self._flakes:
                f.tick(1.0)
                f.draw(painter)

    # ── flowing arrow renderer ────────────────────────────────────────────────

    def _draw_arrows(self, painter, W, H):
        vx, vy = self._arrow_vx, self._arrow_vy
        L   = self._arrow_len
        AW  = self._arrow_w
        TL  = self._tail_len
        r, g, b = self._ar, self._ag, self._ab
        angle = math.atan2(vy, vx)
        margin = L + 24

        for a in self._arrows:
            a['x'] += vx * a['spd']
            a['y'] += vy * a['spd']
            if a['growing']:
                a['alpha'] = min(0.88, a['alpha'] + 0.022)
                if a['alpha'] >= 0.88:
                    a['growing'] = False
            if (a['x'] < -margin or a['x'] > W + margin or
                    a['y'] < -margin or a['y'] > H + margin):
                self._recycle_arrow(a, W, H)
                continue

            alpha_i = int(a['alpha'] * 255)
            painter.save()
            painter.translate(a['x'], a['y'])
            painter.rotate(math.degrees(angle))

            # Tapered body via bezier path
            path = QPainterPath()
            path.moveTo(-TL, 0)
            path.cubicTo(-TL * 0.3, -AW * 0.38,
                          L  * 0.30, -AW * 0.44,
                          L  * 0.55,  0)
            path.cubicTo( L  * 0.30,  AW * 0.44,
                          -TL * 0.3,  AW * 0.38,
                          -TL,  0)

            body_grad = QLinearGradient(-TL, 0, L * 0.55, 0)
            body_grad.setColorAt(0.00, QColor(r, g, b, 0))
            body_grad.setColorAt(0.35, QColor(r, g, b, int(alpha_i * 0.18)))
            body_grad.setColorAt(0.72, QColor(r, g, b, int(alpha_i * 0.65)))
            body_grad.setColorAt(1.00, QColor(r, g, b, 0))
            painter.setBrush(QBrush(body_grad))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)

            # Arrowhead triangle
            hx0, hx1 = L * 0.44, L * 0.75
            head = QPainterPath()
            head.moveTo(hx1,  0)
            head.lineTo(hx0, -AW * 0.90)
            head.lineTo(hx0 + L * 0.065, 0)
            head.lineTo(hx0,  AW * 0.90)
            head.closeSubpath()
            head_grad = QLinearGradient(hx0, 0, hx1, 0)
            head_grad.setColorAt(0.0, QColor(r, g, b, int(alpha_i * 0.92)))
            head_grad.setColorAt(1.0, QColor(r, g, b, int(alpha_i * 0.22)))
            painter.setBrush(QBrush(head_grad))
            painter.drawPath(head)

            # Soft glow halo around head
            glow_grad = QRadialGradient(L * 0.60, 0, AW * 1.7)
            glow_grad.setColorAt(0.0, QColor(r, g, b, int(alpha_i * 0.16)))
            glow_grad.setColorAt(1.0, QColor(r, g, b, 0))
            painter.setBrush(QBrush(glow_grad))
            painter.drawEllipse(QPointF(L * 0.60, 0), AW * 1.6, AW * 1.25)

            painter.restore()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)


# ── Scene particle helpers (module-level, used only by WindVectorWidget) ──────


class _Cloud:
    """Drifting cloud puff cluster with lit top / shadowed base."""
    def __init__(self, W, H, spd, scatter=True, dark=False):
        self.W = W; self.H = H; self.base_spd = spd; self.dark = dark
        self._randomize(scatter)

    def _randomize(self, scatter=False):
        self.x     = random.random() * self.W if scatter else -340
        self.y     = self.H * (0.06 + random.random() * 0.54)
        self.spd   = self.base_spd * (0.7 + random.random() * 0.6)
        # dark clouds are more opaque — they're heavy and thick
        self.alpha = (0.55 + random.random() * 0.30) if self.dark else (0.40 + random.random() * 0.35)
        self.scale = 0.60 + random.random() * 0.85
        n = 4 + int(random.random() * 4)
        self.puffs = [{
            'ox': (i - n / 2) * 36 * self.scale + (random.random() - 0.5) * 10,
            'oy': (random.random() - 0.5) * 18 * self.scale,
            'r':  (28 + random.random() * 30) * self.scale
        } for i in range(n)]

    def tick(self, dt):
        self.x += self.spd * dt
        if self.x > self.W + 380:
            self._randomize(False)

    def draw(self, painter):
        painter.setPen(Qt.PenStyle.NoPen)
        for p in self.puffs:
            gx = self.x + p['ox']; gy = self.y + p['oy']; r = p['r']

            if self.dark:
                # Dark overcast puff: heavy charcoal base, almost no bright top
                # Shadow underneath — gives the cloud its belly
                shadow_cg = QRadialGradient(gx, gy + r * 0.30, r * 0.20)
                shadow_cg.setCenter(gx, gy + r * 0.30)
                shadow_cg.setRadius(r * 1.05)
                shadow_cg.setFocalPoint(gx, gy + r * 0.30)
                shadow_cg.setColorAt(0.0, QColor(30,  34,  42, int(self.alpha * 255)))
                shadow_cg.setColorAt(0.5, QColor(48,  54,  66, int(self.alpha * 200)))
                shadow_cg.setColorAt(1.0, QColor(48,  54,  66, 0))
                painter.save()
                painter.setBrush(QBrush(shadow_cg))
                painter.drawEllipse(QPointF(gx, gy), r, r)
                painter.restore()

                # Main body — dark slate
                body_cg = QRadialGradient(gx - r * 0.18, gy - r * 0.20, r * 0.10)
                body_cg.setCenter(gx, gy)
                body_cg.setRadius(r)
                body_cg.setFocalPoint(gx - r * 0.18, gy - r * 0.20)
                body_cg.setColorAt(0.0, QColor(72,  80,  96, int(self.alpha * 255)))
                body_cg.setColorAt(0.5, QColor(58,  66,  80, int(self.alpha * 220)))
                body_cg.setColorAt(1.0, QColor(44,  50,  62, 0))
                painter.save()
                painter.setBrush(QBrush(body_cg))
                painter.drawEllipse(QPointF(gx, gy), r, r)
                painter.restore()

                # Faint silver highlight on the very top rim
                hi_cg = QRadialGradient(gx - r * 0.12, gy - r * 0.42, 0)
                hi_cg.setCenter(gx - r * 0.12, gy - r * 0.38)
                hi_cg.setRadius(r * 0.55)
                hi_cg.setFocalPoint(gx - r * 0.12, gy - r * 0.42)
                hi_cg.setColorAt(0.0, QColor(120, 130, 148, int(self.alpha * 160)))
                hi_cg.setColorAt(1.0, QColor(120, 130, 148, 0))
                painter.save()
                painter.setBrush(QBrush(hi_cg))
                painter.drawEllipse(QPointF(gx - r * 0.12, gy - r * 0.38), r * 0.55, r * 0.42)
                painter.restore()

            else:
                # Bright broken-cloud puff: white top, blue-grey base
                # Shadow belly first so it sits behind the body
                belly_cg = QRadialGradient(gx, gy + r * 0.25, r * 0.15)
                belly_cg.setCenter(gx, gy + r * 0.22)
                belly_cg.setRadius(r * 0.90)
                belly_cg.setFocalPoint(gx, gy + r * 0.25)
                belly_cg.setColorAt(0.0, QColor(148, 170, 192, int(self.alpha * 180)))
                belly_cg.setColorAt(1.0, QColor(148, 170, 192, 0))
                painter.save()
                painter.setBrush(QBrush(belly_cg))
                painter.drawEllipse(QPointF(gx, gy + r * 0.10), r, r * 0.70)
                painter.restore()

                # Main body — bright white-blue
                body_cg = QRadialGradient(gx - r * 0.20, gy - r * 0.22, r * 0.08)
                body_cg.setCenter(gx, gy)
                body_cg.setRadius(r)
                body_cg.setFocalPoint(gx - r * 0.20, gy - r * 0.22)
                body_cg.setColorAt(0.0, QColor(240, 248, 255, int(self.alpha * 255)))
                body_cg.setColorAt(0.45,QColor(210, 228, 244, int(self.alpha * 220)))
                body_cg.setColorAt(0.80,QColor(178, 200, 222, int(self.alpha * 140)))
                body_cg.setColorAt(1.0, QColor(160, 185, 210, 0))
                painter.save()
                painter.setBrush(QBrush(body_cg))
                painter.drawEllipse(QPointF(gx, gy), r, r)
                painter.restore()

                # Bright specular highlight on upper-left
                hi_cg = QRadialGradient(gx - r * 0.15, gy - r * 0.40, 0)
                hi_cg.setCenter(gx - r * 0.15, gy - r * 0.35)
                hi_cg.setRadius(r * 0.50)
                hi_cg.setFocalPoint(gx - r * 0.15, gy - r * 0.40)
                hi_cg.setColorAt(0.0, QColor(255, 255, 255, int(self.alpha * 200)))
                hi_cg.setColorAt(1.0, QColor(255, 255, 255, 0))
                painter.save()
                painter.setBrush(QBrush(hi_cg))
                painter.drawEllipse(QPointF(gx - r * 0.15, gy - r * 0.35), r * 0.50, r * 0.38)
                painter.restore()


class _RainDrop:
    """A single animated raindrop."""
    def __init__(self, W, H, vx, vy, heavy=False):
        self.W = W; self.H = H; self.vx = vx; self.vy = vy; self.heavy = heavy
        self._reset(scatter=True)

    def _reset(self, scatter=False):
        self.x     = random.random() * (self.W + 80) - 20
        self.y     = random.random() * self.H if scatter else -16
        self.length= (12 + random.random() * 16) if self.heavy else (7 + random.random() * 12)
        self.spd   = (9  + random.random() *  7) if self.heavy else (6 + random.random() *  5)
        self.alpha = (0.20 + random.random() * 0.40) if self.heavy else (0.25 + random.random() * 0.45)
        self.lw    = 1.1 if self.heavy else 0.7

    def tick(self, dt):
        self.x += self.vx * self.spd * 0.45 * dt
        self.y += (self.vy * self.spd * 0.18 + self.spd) * dt
        if self.y > self.H + 20 or self.x < -60 or self.x > self.W + 60:
            self._reset(False)

    def draw(self, painter):
        ang = math.atan2(self.vy * 0.18 + 1, self.vx * 0.45)
        ex  = self.x + math.cos(ang) * self.length
        ey  = self.y + math.sin(ang) * self.length
        color = QColor(122, 176, 204, int(self.alpha * 255)) if self.heavy else QColor(144, 192, 216, int(self.alpha * 255))
        pen = QPen(color, self.lw)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.save(); painter.setPen(pen)
        painter.drawLine(QPointF(self.x, self.y), QPointF(ex, ey))
        painter.restore()


class _FogBank:
    """A wide, slowly drifting fog bank that rolls across the skyline,
    partially obscuring the city silhouette below it."""
    def __init__(self, W, H):
        self.W = W; self.H = H
        self._reset(scatter=True)

    def _reset(self, scatter=False):
        self.w     = self.W * (0.45 + random.random() * 0.60)
        self.h     = self.H * (0.18 + random.random() * 0.22)
        self.x     = random.random() * self.W if scatter else -self.w
        # Sits in the lower-mid area — hugging the skyline tops
        self.y     = self.H * (0.38 + random.random() * 0.28)
        self.spd   = 0.12 + random.random() * 0.20
        self.alpha = 0.55 + random.random() * 0.30

    def tick(self, dt):
        self.x += self.spd * dt
        if self.x > self.W + self.w:
            self._reset(False)

    def draw(self, painter):
        cx = self.x + self.w * 0.5
        cy = self.y + self.h * 0.5
        # Soft wide ellipse — feathered edges
        for layer, frac, a_scale in (
            (0, 1.00, 1.00),
            (1, 0.80, 0.60),
            (2, 0.55, 0.30),
        ):
            ew = self.w * frac
            eh = self.h * frac
            g = QRadialGradient(cx, cy, max(ew, eh))
            g.setCenter(cx, cy)
            g.setRadius(max(ew, eh))
            g.setFocalPoint(cx, cy)
            alpha = int(self.alpha * a_scale * 255)
            g.setColorAt(0.0, QColor(195, 205, 212, alpha))
            g.setColorAt(0.5, QColor(188, 198, 206, alpha // 2))
            g.setColorAt(1.0, QColor(185, 196, 204, 0))
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(g))
            painter.drawEllipse(QPointF(cx, cy), ew, eh)
            painter.restore()


class _MistDrop:
    """Tiny suspended drizzle droplet — much shorter and slower than rain,
    falls nearly straight down with minimal horizontal drift, giving the
    impression of fine mist / drizzle hanging in the air."""
    def __init__(self, W, H, vx, vy):
        self.W = W; self.H = H; self.vx = vx; self.vy = vy
        self._reset(scatter=True)

    def _reset(self, scatter=False):
        self.x      = random.random() * (self.W + 40) - 20
        self.y      = random.random() * self.H if scatter else -6
        # Very short — just a pixel or two, barely a streak
        self.length = 2.0 + random.random() * 4.0
        # Slow fall
        self.spd    = 1.2 + random.random() * 1.8
        # Semi-transparent — looks suspended, not driving
        self.alpha  = 0.25 + random.random() * 0.45
        self.lw     = 0.8 + random.random() * 0.6

    def tick(self, dt):
        # Very slight horizontal drift from wind, predominantly vertical
        self.x += self.vx * self.spd * 0.18 * dt
        self.y += self.spd * dt
        if self.y > self.H + 8 or self.x < -30 or self.x > self.W + 30:
            self._reset(False)

    def draw(self, painter):
        # Near-vertical angle with just a whisper of wind lean
        ang = math.atan2(1.0, self.vx * 0.12)
        ex = self.x + math.cos(ang) * self.length
        ey = self.y + math.sin(ang) * self.length
        color = QColor(172, 196, 212, int(self.alpha * 255))
        pen = QPen(color, self.lw)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.save()
        painter.setPen(pen)
        painter.drawLine(QPointF(self.x, self.y), QPointF(ex, ey))
        painter.restore()


class _LightningBolt:
    """Randomly fires branched lightning bolts."""
    def __init__(self, W, H):
        self.W = W; self.H = H
        self.visible  = False
        self.alpha    = 0.0
        self.cooldown = 80 + random.random() * 140
        self.timer    = random.random() * 60
        self.segs     = []
        self._build()

    def _build(self):
        self.segs = []
        cx = self.W * (0.2 + random.random() * 0.6); cy = 0.0
        while cy < self.H * 0.88:
            nx = cx + (random.random() - 0.5) * 42
            ny = cy + 16 + random.random() * 24
            self.segs.append((cx, cy, nx, ny))
            if random.random() < 0.2 and ny < self.H * 0.6:
                self.segs.append((nx, ny, nx + (random.random()-0.5)*60, ny+20+random.random()*40))
            cx = nx; cy = ny

    def tick(self):
        self.timer += 1
        if not self.visible and self.timer >= self.cooldown:
            self.visible = True; self.alpha = 1.0
            self.timer = 0; self.cooldown = 80 + random.random() * 160
            self._build()
        if self.visible:
            self.alpha -= 0.055
            if self.alpha <= 0:
                self.alpha = 0.0; self.visible = False

    def draw(self, painter, W, H):
        if not self.visible or self.alpha <= 0:
            return
        # Sky flash
        painter.save()
        painter.fillRect(0, 0, W, H, QColor(128, 144, 216, int(self.alpha * 36)))
        painter.restore()
        # Glow pass
        glow_pen = QPen(QColor(176, 200, 255, int(self.alpha * 128)), 7)
        painter.save(); painter.setPen(glow_pen)
        for x1,y1,x2,y2 in self.segs:
            painter.drawLine(QPointF(x1,y1), QPointF(x2,y2))
        painter.restore()
        # Sharp core
        core_pen = QPen(QColor(255, 255, 255, int(self.alpha * 242)), 1.5)
        painter.save(); painter.setPen(core_pen)
        for x1,y1,x2,y2 in self.segs:
            painter.drawLine(QPointF(x1,y1), QPointF(x2,y2))
        painter.restore()


class _Snowflake:
    """Gently drifting snowflake."""
    def __init__(self, W, H, vx):
        self.W = W; self.H = H; self.vx = vx
        self._reset(scatter=True)

    def _reset(self, scatter=False):
        self.x       = random.random() * self.W
        self.y       = random.random() * self.H if scatter else -10
        self.r       = 1.2 + random.random() * 3.2
        self.spd     = 0.6 + random.random() * 1.8
        self.wobble  = random.random() * math.pi * 2
        self.wobble_spd = 0.025 + random.random() * 0.02
        self.alpha   = 0.45 + random.random() * 0.55

    def tick(self, dt):
        self.wobble += self.wobble_spd * dt
        self.x += (self.vx * self.spd * 0.65 + math.sin(self.wobble) * 0.55) * dt
        self.y += self.spd * dt
        if self.y > self.H + 12 or self.x < -20 or self.x > self.W + 20:
            self._reset(False)

    def draw(self, painter):
        r = self.r * 2.2
        grad = QRadialGradient(self.x, self.y, r)
        grad.setColorAt(0.00, QColor(255, 255, 255, int(self.alpha * 242)))
        grad.setColorAt(0.55, QColor(210, 228, 240, int(self.alpha * 128)))
        grad.setColorAt(1.00, QColor(200, 218, 235, 0))
        painter.save()
        painter.setBrush(QBrush(grad)); painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(self.x, self.y), r, r)
        painter.restore()


# ============================================================================
# Offline tools
# ----------------------------------------------------------------------------
# Batch jobs, not runtime code: the weather backfill that builds the CPW
# training corpus, and the azimuth check that validates PARK_ORIENTATION.  They
# live here rather than as loose scripts because they are weather-domain and
# share this module's tables; pandas is imported lazily inside each so the GUI
# import path stays light.
#
#   python weatherman.py backfill 2024 2025 2026
#   python weatherman.py report 2024
#   python weatherman.py validate-azimuth 2026-06-01 2026-06-30
# ============================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
# Generated datasets live together rather than scattered through the source
# directory.  Regenerable from the tools, so gitignored.
DATA_DIR = os.path.join(HERE, "model_data")
os.makedirs(DATA_DIR, exist_ok=True)
STATS = "https://statsapi.mlb.com/api/v1"
STATS11 = "https://statsapi.mlb.com/api/v1.1"

STATS = "https://statsapi.mlb.com/api/v1"
STATS11 = "https://statsapi.mlb.com/api/v1.1"

# The Athletics are ATH in Savant throughout, but StatsAPI only renamed them
# with the move: team 133 is OAK in 2024 and ATH from 2025.  So this is a
# FALLBACK, applied only when the code is not already valid for that season —
# forcing it cost the whole 2025 A's home season (7,893 batted balls, 3.3% of
# the corpus) to an unmatched join.
SAVANT_ABBREV_FALLBACK = {"ATH": "OAK", "OAK": "ATH"}

# Statcast gives an inning but no timestamp.  A nine-inning game runs about
# three hours, so each inning advances the clock ~20 minutes — enough to move a
# late-game plate appearance into the next hourly bucket, which is the whole
# point of joining hourly rather than per-game.
MINUTES_PER_INNING = 20.5


def _json(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as fh:
        return json.load(fh)


# --------------------------------------------------------------------- games

def team_abbrevs(year):
    """{team id: abbreviation} for a season."""
    teams = _json(f"{STATS}/teams?sportId=1&season={year}")["teams"]
    return {t["id"]: t["abbreviation"] for t in teams}


def build_game_index(year, workers=12, cache=True):
    import pandas as pd
    """Every regular-season game, with first pitch and MLB's own weather.

    Cached to disk: a finished season never changes, and rebuilding it costs
    one live-feed request per game — ~2,900 of them.
    """
    path = os.path.join(HERE, "weather_cache", f"games_{year}.parquet")
    if cache and os.path.exists(path):
        games = pd.read_parquet(path)
        print(f"[{year}] game index from cache ({len(games)} games)")
        return games
    abbrev = team_abbrevs(year)
    sched = _json(f"{STATS}/schedule?sportId=1&season={year}&gameType=R")
    rows = []
    for date in sched["dates"]:
        for g in date["games"]:
            if g["status"]["abstractGameState"] != "Final":
                continue
            rows.append({
                "game_pk": g["gamePk"],
                "game_date": g["officialDate"],
                "home_abbrev": abbrev.get(g["teams"]["home"]["team"]["id"]),
                "venue_id": g["venue"]["id"],
                "venue_name": g["venue"]["name"],
                "first_pitch_utc": g["gameDate"],
                "game_number": g.get("gameNumber", 1),
            })
    games = pd.DataFrame(rows)
    print(f"[{year}] {len(games)} final regular-season games, "
          f"{games.venue_name.nunique()} venues")

    # MLB's observed conditions.  The fields= whitelist takes each response
    # from ~857 KB to ~370 bytes, which is the difference between this being a
    # background job and a coffee break.
    url = (STATS11 + "/game/{}/feed/live"
           "?fields=gameData,weather,condition,temp,wind")

    def pull(pk):
        try:
            return pk, _json(url.format(pk), timeout=45)["gameData"].get("weather", {})
        except Exception:
            return pk, {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        observed = dict(pool.map(pull, games.game_pk.tolist()))

    obs = []
    for pk in games.game_pk:
        w = observed.get(pk) or {}
        condition = w.get("condition") or ""
        speed, label = None, None
        wind = w.get("wind") or ""
        if "," in wind:
            speed_s, label = (p.strip() for p in wind.split(",", 1))
            try:
                speed = float(speed_s.lower().replace("mph", "").strip())
            except ValueError:
                speed = None
        try:
            temp = float(w.get("temp"))
        except (TypeError, ValueError):
            temp = None
        obs.append({
            "obs_condition": condition or None,
            "obs_temp_f": temp,
            "obs_wind_mph": speed,
            "obs_wind_label": label,
            "obs_wind_field_deg": MLB_WIND_LABELS.get(label),
            "obs_roof_closed": is_indoor_condition(condition),
        })
    got = sum(1 for o in obs if o["obs_condition"])
    print(f"[{year}] observed weather for {got}/{len(games)} games "
          f"({100 * got / max(1, len(games)):.1f}%)")
    games = pd.concat([games, pd.DataFrame(obs)], axis=1)
    if cache:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        games.to_parquet(path, index=False)
    return games


# -------------------------------------------------------------------- venues

def venue_meta(venue_ids):
    """{venue id: {name, lat, lon, elevation_ft, azimuth, roof}}.

    Straight from MLB rather than STADIUM_DATA, so neutral sites and former
    homes — Oakland Coliseum, Rickwood, Tokyo Dome — resolve too.  Those have
    no wall geometry on our side, but they do have weather.
    """
    out = {}
    for vid in sorted(set(int(v) for v in venue_ids)):
        try:
            v = _json(f"{STATS}/venues/{vid}?hydrate=location,fieldInfo")["venues"][0]
        except Exception as e:
            print(f"  [warn] venue {vid} lookup failed: {e}")
            continue
        loc = v.get("location", {}) or {}
        coords = loc.get("defaultCoordinates") or {}
        name = v.get("name")
        # Our key for this park, if we know it under another name.
        ours = resolve_park_name(name)
        roof = (v.get("fieldInfo", {}) or {}).get("roofType", "Open")
        out[vid] = {
            "name": name,
            "lat": coords.get("latitude"),
            "lon": coords.get("longitude"),
            "elevation_ft": loc.get("elevation"),
            "azimuth": loc.get("azimuthAngle"),
            "roof": (roof or "Open").lower(),
            # Our own table wins where we have it — those azimuths are the
            # validated ones (see validate_azimuths below).
            "park_key": ours,
            **({"azimuth": PARK_ORIENTATION[ours]["azimuth"],
                "roof": PARK_ORIENTATION[ours]["roof"]}
               if ours in PARK_ORIENTATION else {}),
        }
    missing = [v["name"] for v in out.values() if v["lat"] is None]
    if missing:
        print(f"  [warn] no coordinates for: {missing}")
    return out


# ------------------------------------------------------------------- weather

def fetch_weather(meta, start_date, end_date, archive=None, chunk_days=31):
    """{venue_id: {'YYYY-MM-DDTHH': row}} over a date range.

    Every venue travels in one request per chunk — Open-Meteo takes parallel
    comma-separated coordinate lists, and a matching `elevation` list, so each
    park's surface pressure comes back downscaled to its own field level.
    Chunked by month purely to keep individual cache entries sane.
    """
    archive = archive or WeatherArchive()
    points = [(vid, m["lat"], m["lon"], m["elevation_ft"] or 0)
              for vid, m in meta.items() if m["lat"] is not None]
    index = {vid: {} for vid, *_ in points}
    start = _dt.date.fromisoformat(str(start_date)[:10])
    end = _dt.date.fromisoformat(str(end_date)[:10])
    cursor = start
    while cursor <= end:
        stop = min(cursor + _dt.timedelta(days=chunk_days - 1), end)
        print(f"  weather {cursor} .. {stop} ({len(points)} venues)", flush=True)
        got = archive.hourly_points(points, cursor, stop)
        for vid, rows in got.items():
            for r in rows:
                index[vid][r["time"][:13]] = r
        cursor = stop + _dt.timedelta(days=1)
    return index


# ------------------------------------------------------------------- joining

def backfill(year, out_path=None, workers=12):
    import pandas as pd
    bbe_path = os.path.join(HERE, f"savant_bbe_{year}.csv")
    if not os.path.exists(bbe_path):
        print(f"[{year}] no {bbe_path} — skipping")
        return None
    out_path = out_path or os.path.join(DATA_DIR, f"weather_backfill_{year}.parquet")

    bbe = pd.read_csv(bbe_path, usecols=["game_date", "home_team", "inning"],
                      low_memory=False)
    print(f"[{year}] {len(bbe)} batted balls")

    games = build_game_index(year, workers=workers)
    meta = venue_meta(games.venue_id)

    # One game per (date, home team) — except doubleheaders, which a BBE row
    # cannot distinguish because the corpus carries no game_pk.  Keep game 1
    # and mark the rows, rather than silently averaging two different evenings.
    games = games.sort_values(["game_date", "home_abbrev", "game_number"])
    dh_counts = games.groupby(["game_date", "home_abbrev"]).size()
    dh_keys = set(dh_counts[dh_counts > 1].index)
    first = games.drop_duplicates(["game_date", "home_abbrev"], keep="first")
    by_key = first.set_index(["game_date", "home_abbrev"]).to_dict("index")
    valid_codes = set(games.home_abbrev.dropna().unique())

    # Pad the window by a day at each end.  Official dates are local, but the
    # weather index is keyed in UTC: a 7:10 pm first pitch on the west coast is
    # 02:10 the FOLLOWING UTC day, and innings push it later still.  Without
    # the pad, every late game on the closing date silently loses its weather.
    # Window comes from the BBE file, not the schedule.  The schedule runs to
    # the end of the season; a part-season corpus does not, and fetching the
    # gap pulls dates near today, which routes to the forecast endpoint and
    # 400s on a month-wide multi-location request.
    season_start = (_dt.date.fromisoformat(str(bbe.game_date.min())[:10])
                    - _dt.timedelta(days=1))
    season_end = (_dt.date.fromisoformat(str(bbe.game_date.max())[:10])
                  + _dt.timedelta(days=1))
    print(f"[{year}] weather window {season_start} .. {season_end}")
    weather = fetch_weather(meta, season_start, season_end)

    archive_unused = None  # noqa: F841  (kept for readability of the flow)
    records = []
    unmatched = 0
    for game_date, home_team, inning in zip(bbe.game_date, bbe.home_team, bbe.inning):
        code = home_team if home_team in valid_codes else \
            SAVANT_ABBREV_FALLBACK.get(home_team, home_team)
        key = (str(game_date)[:10], code)
        g = by_key.get(key)
        if g is None:
            unmatched += 1
            records.append({})
            continue
        m = meta.get(g["venue_id"], {})
        rec = {
            "game_pk": g["game_pk"],
            "venue_id": g["venue_id"],
            "venue_name": g["venue_name"],
            "park_azimuth": m.get("azimuth"),
            "park_elevation_ft": m.get("elevation_ft"),
            "roof_type": m.get("roof"),
            "dh_ambiguous": key in dh_keys,
            "obs_condition": g["obs_condition"],
            "obs_temp_f": g["obs_temp_f"],
            "obs_wind_mph": g["obs_wind_mph"],
            "obs_wind_label": g["obs_wind_label"],
            "obs_wind_field_deg": g["obs_wind_field_deg"],
            "obs_roof_closed": g["obs_roof_closed"],
        }
        # First pitch, advanced by the innings already played.
        fp = _dt.datetime.fromisoformat(g["first_pitch_utc"].replace("Z", "+00:00"))
        fp = fp.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        try:
            offset = (float(inning) - 1.0) * MINUTES_PER_INNING
        except (TypeError, ValueError):
            offset = 0.0
        when = fp + _dt.timedelta(minutes=max(0.0, offset))
        stamp = (when + _dt.timedelta(minutes=30)).replace(
            minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H")
        row = weather.get(g["venue_id"], {}).get(stamp)
        if row is not None:
            # The roof state we KNOW, from MLB, not the park's base rate.
            # MLB told us the roof was shut; apply_roof falls back to a
            # generic sealed-indoor profile for venues we have no measurements
            # for (Seoul, Tokyo), which beats reporting the outdoor forecast.
            closed = bool(g["obs_roof_closed"])
            if closed:
                row = apply_roof(row, m.get("park_key") or g["venue_name"],
                                 closed=True)
            rec.update({
                "event_time_utc": when.isoformat(timespec="minutes"),
                "met_temp_f": row["temperature"],
                "met_humidity": row["humidity"],
                "met_pressure_hpa": row["pressure_hpa"],
                "met_wind_mph": row["wind_speed"],
                "met_wind_compass_deg": row["wind_direction"],
                "met_wind_field_deg": wind_to_field_frame(
                    row["wind_direction"], azimuth=m.get("azimuth")),
                "met_wind_gust_mph": row["wind_gust"],
                # Upper levels, for weatherman.wind_profile: the ball flies to
                # ~45 m and the forecast is quoted at 10 m.
                "met_wind_80m": row.get("wind_speed_80m"),
                "met_wind_dir_80m": row.get("wind_direction_80m"),
                "met_wind_120m": row.get("wind_speed_120m"),
                "met_precip_in": row["precipitation"],
                "met_cloud_pct": row["cloud_cover"],
                "met_condition": row["condition"],
                "met_source": row["source"],
                "roof_closed": closed,
            })
        records.append(rec)

    out = pd.DataFrame.from_records(records)
    if unmatched:
        print(f"[{year}] {unmatched} batted balls matched no game "
              f"({100 * unmatched / len(bbe):.2f}%)")
    joined = out["met_temp_f"].notna().sum() if "met_temp_f" in out else 0
    print(f"[{year}] weather joined for {joined}/{len(bbe)} "
          f"({100 * joined / len(bbe):.1f}%)")
    if "dh_ambiguous" in out:
        print(f"[{year}] doubleheader-ambiguous rows: "
              f"{int(out.dh_ambiguous.fillna(False).sum())}")
    out.to_parquet(out_path, index=False)
    print(f"[{year}] -> {out_path}\n")
    return out


def report(year):
    """Score the Open-Meteo columns against what MLB actually reported.

    This is what the obs_* columns are FOR.  They never enter the model, but
    they are the only independent check that the met_* columns describe the
    right park on the right evening — a wrong venue join or a bad azimuth
    shows up here as agreement collapsing, and nowhere else.
    """
    import pandas as pd
    path = os.path.join(DATA_DIR, f"weather_backfill_{year}.parquet")
    if not os.path.exists(path):
        print(f"[{year}] no backfill to report on")
        return
    df = pd.read_parquet(path)
    out = df[df.met_temp_f.notna() & df.obs_temp_f.notna()]
    open_air = out[~out.obs_roof_closed.fillna(False)]
    print(f"\n=== {year}: Open-Meteo vs MLB observed ({len(out)} rows, "
          f"{len(open_air)} open-air) ===")

    d = open_air.met_temp_f - open_air.obs_temp_f
    print(f"  temperature   bias {d.mean():+5.2f} F   MAE {d.abs().mean():4.2f}   "
          f"r {np.corrcoef(open_air.met_temp_f, open_air.obs_temp_f)[0, 1]:.3f}")

    w = open_air[open_air.obs_wind_mph.notna() & open_air.met_wind_mph.notna()]
    d = w.met_wind_mph - w.obs_wind_mph
    print(f"  wind speed    bias {d.mean():+5.2f} mph MAE {d.abs().mean():4.2f}   "
          f"r {np.corrcoef(w.met_wind_mph, w.obs_wind_mph)[0, 1]:.3f}")

    # Direction is the one that would expose a bad azimuth.  MLB quantises to
    # eight buckets, so "within 45 degrees" is the tightest meaningful test.
    v = w[w.obs_wind_field_deg.notna() & w.met_wind_field_deg.notna()
          & (w.obs_wind_mph >= 5)]
    diff = (v.met_wind_field_deg - v.obs_wind_field_deg + 180) % 360 - 180
    rad = np.radians(diff)
    R = np.hypot(np.sin(rad).mean(), np.cos(rad).mean())
    print(f"  wind bearing  circular mean {np.degrees(np.arctan2(np.sin(rad).mean(), np.cos(rad).mean())):+5.1f} deg"
          f"   R {R:.2f}   within one bucket {100 * (diff.abs() <= 45).mean():.0f}%"
          f"   (n={len(v)})")

    worst = (v.assign(off=diff.abs()).groupby("venue_name")["off"]
             .agg(["size", "mean"]).sort_values("mean", ascending=False).head(5))
    print("  largest per-venue bearing disagreement:")
    print(worst.to_string(float_format=lambda x: f"{x:7.1f}"))




def validate_azimuths(start, end):
    """Score PARK_ORIENTATION against MLB's own field-relative wind labels.

    GUMBO reports observed wind as e.g. "9 mph, Out To CF" — already in the
    field frame — while the forecast gives a compass bearing.  Rotating the
    second by the park azimuth should reproduce the first, so a park whose
    azimuth is wrong shows up as a rotational offset for that park alone.
    """
    import math
    import numpy as np
    START, END = str(start)[:10], str(end)[:10]
    LABEL={"In From CF":0,"In From RF":45,"In From LF":315,
           "Out To CF":180,"Out To RF":225,"Out To LF":135,
           "R To L":90,   # from the 1B/RF side, crossing to left
           "L To R":270}
    ALIASES={"oriole park at camden yards":"Camden Yards","rate field":"Guaranteed Rate Field",
             "daikin park":"Minute Maid Park","uniqlo field at dodger stadium":"Dodger Stadium",
             "loandepot park":"LoanDepot Park"}
    def park_key(n):
        if n in STADIUM_DATA: return n
        a=ALIASES.get(n.lower())
        if a: return a
        for k in STADIUM_DATA:
            if k.lower()==n.lower(): return k
        return None

    def J(u):
        return json.load(urllib.request.urlopen(u,timeout=45))

    sched=J(f"{STATS}/schedule?sportId=1&startDate={START}&endDate={END}")
    games=[]
    for d in sched["dates"]:
        for g in d["games"]:
            if g["status"]["abstractGameState"]!="Final": continue
            k=park_key(g["venue"]["name"])
            if k: games.append((g["gamePk"],k))
    print(f"{len(games)} final games {START}..{END}")

    F=("https://statsapi.mlb.com/api/v1.1/game/{}/feed/live"
       "?fields=gameData,datetime,dateTime,weather,condition,temp,wind,venue,name")
    def pull(t):
        pk,k=t
        try:
            d=J(F.format(pk)); gd=d["gameData"]
            return (pk,k,gd["datetime"]["dateTime"],gd.get("weather",{}))
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=12) as ex:
        obs=[r for r in ex.map(pull,games) if r]
    print(f"pulled {len(obs)} feeds")

    roof=sum(1 for _,_,_,w in obs if "Roof" in (w.get("condition") or ""))
    print(f"  roof-closed games: {roof}")

    # one Open-Meteo request covering every park for the whole window
    wa=WeatherArchive()
    pts=[(n,d["lat"],d["lon"],d.get("altitude",0)) for n,d in sorted(STADIUM_DATA.items())]
    grid=wa.hourly_points(pts,START,END)
    index={p:{r["time"][:13]:r for r in rows} for p,rows in grid.items()}

    def cdiff(a,b): return (a-b+180)%360-180
    rows=[]
    for pk,park,iso,w in obs:
        cond=w.get("condition") or ""
        if "Roof" in cond: continue
        wind=(w.get("wind") or "")
        if "," not in wind: continue
        spd_s,lab=[x.strip() for x in wind.split(",",1)]
        try: spd=float(spd_s.replace("mph","").strip())
        except ValueError: continue
        if lab not in LABEL or spd<5: continue     # <5mph label is noise
        t=_dt.datetime.fromisoformat(iso.replace("Z","+00:00")).replace(tzinfo=None)
        t=(t+_dt.timedelta(minutes=30)).replace(minute=0,second=0,microsecond=0)
        row=index.get(park,{}).get(t.strftime("%Y-%m-%dT%H"))
        if not row or row["wind_direction"] is None: continue
        ours=wind_to_field_frame(row["wind_direction"],park)
        rows.append((park,cdiff(ours,LABEL[lab]),spd,row["wind_speed"],lab))

    print(f"\nusable comparisons: {len(rows)}")
    def circmean(xs):
        s=sum(math.sin(math.radians(x)) for x in xs); c=sum(math.cos(math.radians(x)) for x in xs)
        return math.degrees(math.atan2(s,c)), math.hypot(s,c)/len(xs)
    allm,allR=circmean([r[1] for r in rows])
    print(f"LEAGUE-WIDE circular mean offset: {allm:+.1f} deg   concentration R={allR:.2f}")
    within=sum(1 for r in rows if abs(r[1])<=45)/len(rows)
    print(f"within +-45 deg (one label bucket): {within*100:.0f}%")

    print(f"\n{'park':30}{'n':>4}{'mean off':>10}{'R':>6}  flag")
    bad=[]
    for park in sorted({r[0] for r in rows}):
        xs=[r[1] for r in rows if r[0]==park]
        if len(xs)<4: continue
        m,R=circmean(xs)
        flag=""
        if abs(m)>40 and R>0.35: flag="<-- CHECK AZIMUTH"; bad.append(park)
        elif R<0.25: flag="(swirly/unreliable)"
        print(f"{park:30}{len(xs):4}{m:+10.1f}{R:6.2f}  {flag}")
    print("\nsuspect parks:",bad or "none")


def _tool_main(argv):
    if len(argv) < 2:
        print(__doc__ or "", "\ncommands: backfill | report | validate-azimuth")
        return
    cmd, rest = argv[1], argv[2:]
    if cmd == "backfill":
        years = [int(a) for a in rest if not a.startswith("-")] or [2024, 2025]
        for y in years:
            backfill(y)
            report(y)
    elif cmd == "report":
        for y in [int(a) for a in rest if not a.startswith("-")] or [2024, 2025]:
            report(y)
    elif cmd == "validate-azimuth":
        validate_azimuths(rest[0], rest[1])
    else:
        print(f"unknown command {cmd!r}")


if __name__ == "__main__":
    import sys as _sys
    _tool_main(_sys.argv)
