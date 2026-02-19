from PyQt6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush, QPainterPath, QLinearGradient, QPolygonF
from PyQt6.QtCore import Qt, QPointF
import math
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
        weather_data = {
            "wind_speed": weather_json["wind"]["speed"],
            "wind_direction": weather_json["wind"]["deg"],
            "temperature": weather_json["main"]["temp"],
            "humidity": weather_json["main"]["humidity"],
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
