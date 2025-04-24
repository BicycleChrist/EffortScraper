from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout
from PyQt6.QtGui import QPixmap, QPainter, QColor, QPen
from PyQt6.QtCore import Qt, QPointF
import math
import numpy as np
import requests
from Creds import open_weather_key
# ==============================================
# Stadium Data
# ==============================================
STADIUM_DATA = {
    "American Family field": {  # Milwaukee Brewers
        "image_path": "MLBstadiumgraphics/GreatAmericanBallpark.gif",
        "lat": 43.0280,
        "lon": -87.9712,
        "altitude": 602,
        "dimensions": {
            "left_field": 344,
            "left_center": 371,
            "center_field": 400,
            "right_center": 374,
            "right_field": 345
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
    },
    "Oakland Coliseum": {  # Oakland Athletics
        "image_path": "MLBstadiumgraphics/OaklandColiseum.gif",
        "lat": 37.7516,
        "lon": -122.2005,
        "altitude": 20,
        "dimensions": {
            "left_field": 330,
            "left_center": 367,
            "center_field": 400,
            "right_center": 367,
            "right_field": 330
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
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
        }
    },
    "Tropicana Field": {  # Tampa Bay Rays
        "image_path": "MLBstadiumgraphics/TropicanaField.gif",
        "lat": 27.7682,
        "lon": -82.6534,
        "altitude": 30,
        "dimensions": {
            "left_field": 315,
            "left_center": 370,
            "center_field": 404,
            "right_center": 370,
            "right_field": 322
        }
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
        }
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
        }
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
        }
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


# ==============================================
# Wind Vector Drawing Functions
# ==============================================
def draw_wind_vector(painter, x, y, speed, direction):
    """Draw a wind vector arrow"""
    # Convert wind direction from meteorological to mathematical angle
    # In meteorological conventions, 0° is North, and increases clockwise
    # In mathematical conventions, 0° is East, and increases counterclockwise
    math_angle = (270 - direction) % 360
    rad_angle = math.radians(math_angle)

    # Scale length based on wind speed - MAKE MUCH BIGGER
    max_length = 80  # Increased from 30 to 80
    min_length = 40  # Increased from 5 to 40
    scaled_length = min(max_length, max(min_length, speed * 4))  # Double multiplier

    # Calculate end point
    end_x = x + scaled_length * math.cos(rad_angle)
    end_y = y + scaled_length * math.sin(rad_angle)

    # Set color based on wind speed - MAKE MORE VIBRANT
    if speed < 5:
        color = QColor(47, 88, 109)  # Bright green for light wind
    elif speed < 10:
        color = QColor(30, 236, 205)  # Bright blue for moderate wind
    else:
        color = QColor(255, 0, 0)  # Bright red for strong wind

    # Draw the arrow line - MAKE THICKER
    pen = QPen(color, 4)  # Thicker line (4px)
    painter.setPen(pen)
    
    # Use QPointF for proper float handling in PyQt6
    start_point = QPointF(float(x), float(y))
    end_point = QPointF(float(end_x), float(end_y))
    painter.drawLine(start_point, end_point)

    # Draw arrowhead - MAKE BIGGER
    arrowhead_angle = 25  # degrees
    arrowhead_length = 25  # pixels - increased from 8 to 20

    angle1 = rad_angle + math.radians(180 - arrowhead_angle)
    angle2 = rad_angle + math.radians(180 + arrowhead_angle)

    arrow1_x = end_x + arrowhead_length * math.cos(angle1)
    arrow1_y = end_y + arrowhead_length * math.sin(angle1)
    arrow2_x = end_x + arrowhead_length * math.cos(angle2)
    arrow2_y = end_y + arrowhead_length * math.sin(angle2)

    # Use QPointF for arrowhead lines too
    arrow1_point = QPointF(float(arrow1_x), float(arrow1_y))
    arrow2_point = QPointF(float(arrow2_x), float(arrow2_y))
    
    painter.drawLine(end_point, arrow1_point)
    painter.drawLine(end_point, arrow2_point)

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
