from PyQt6.QtGui import (QPixmap, QPainter, QColor, QPen, QBrush, QPainterPath,
                         QLinearGradient, QRadialGradient, QPolygonF)
from PyQt6.QtCore import Qt, QTimer, QPointF
from PyQt6.QtWidgets import QWidget
import math
import random
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
        "altitude": 5280,  # Famous for high altitude
        "dimensions": {
            "left_field": 347,
            "left_center": 390,
            "center_field": 415,
            "right_center": 387,
            "right_field": 350
        },
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
        "altitude": 750,
        "dimensions": {
            "left_field": 330,
            "left_center": 375,
            "center_field": 410,
            "right_center": 375,
            "right_field": 330
        },
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
            "temperature": weather_json["main"]["temp"],
            "humidity": weather_json["main"]["humidity"],
            "pressure_hpa": pressure_hpa,           # hPa  – for display
            "pressure_pa": pressure_hpa * 100.0,    # Pa   – for physics
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
