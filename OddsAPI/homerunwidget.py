from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QWidget,
    QPushButton, QSlider, QLabel, QComboBox, QGroupBox, QSpinBox, QCheckBox,
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsPathItem,
    QGraphicsItemGroup, QGraphicsLineItem, QGraphicsRectItem, QGridLayout, QListWidget
)
# Import QOpenGLWidget from the correct module
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from OpenGL.GL import *
from OpenGL.GLU import *
from OpenGL.GLUT import *
from svg.path import parse_path
from xml.dom import minidom
from PyQt6.QtSvg import QtSvg, QSvgRenderer
from PyQt6.QtSvgWidgets import QGraphicsSvgItem
from PyQt6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush, QPainterPath, QSurfaceFormat
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, QSizeF, QPoint
import sys
import numpy as np
import math
from scipy.integrate import solve_ivp
from weatherman import WeatherService, STADIUM_DATA
from weatherman import open_weather_key
from pathlib import Path
from svgpathtools import svg2paths
from pywavefront import Wavefront
from pyqtgraph import Vector


# Ballpark model in baseballfield.obj file is at 'pos': [-515.808441, 41.099228, -760.366211]
#TODO: PROPERLY LOAD AND DISPLAY .OBJ PARK MODEL IN QopenGLWidget 
# Load the materials as well if possible from baseballfield.mtl

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
    def __init__(self):
        # Constants
        self.g = 9.81  # m/s^2, gravity
        self.m = 0.145  # kg, baseball mass
        self.d = 0.074  # m, baseball diameter
        self.C_d = 0.3  # drag coefficient
        self.C_l = 0.2  # lift coefficient (for Magnus effect)
        self.omega = 1800  # rpm, typical spin rate
        OBJ_POSITION = [-515.808441, 41.099228, -760.366211]  
        OBJ_SCALE = 0.1  # Initial scale factor

    def calculate_trajectory(self, exit_velocity, launch_angle, wind_speed, wind_direction, temp, humidity, altitude):
        """Calculate ball trajectory based on initial conditions and environment"""
        # Convert inputs to SI units
        v0 = exit_velocity * 0.44704  # mph to m/s
        angle = np.radians(launch_angle)
        wind = wind_speed * 0.44704  # mph to m/s
        wind_rad = np.radians(wind_direction)

        # Calculate air density based on temperature, humidity, altitude
        rho = self.calculate_air_density(temp, humidity, altitude)

        # Initial conditions [x, y, z, vx, vy, vz]
        # x is toward right field, y is toward center field, z is height
        initial_state = [0, 0, 0.91,  # Starting at home plate, 3 feet off ground
                         v0 * np.cos(angle) * np.cos(np.radians(0)),  # x component
                         v0 * np.cos(angle) * np.sin(np.radians(0)),  # y component
                         v0 * np.sin(angle)]  # z component

        # Time span for simulation
        t_span = (0, 10)  # 10 seconds should be enough for any baseball flight

        # Solve differential equations
        solution = solve_ivp(
            lambda t, y: self.baseball_ode(t, y, wind, wind_rad, rho),
            t_span,
            initial_state,
            method='RK45',
            max_step=0.01
        )

        # Convert back to imperial units for display
        x = solution.y[0] * 3.28084  # m to ft
        y = solution.y[1] * 3.28084  # m to ft
        z = solution.y[2] * 3.28084  # m to ft

        # Find landing point (where z reaches field level)
        landing_idx = np.argmax(z < 0.91 * 3.28084)
        if landing_idx == 0 and z[-1] > 0.91 * 3.28084:
            landing_idx = len(z) - 1

        distance = np.sqrt(x[landing_idx]**2 + y[landing_idx]**2)

        return {
            "time": solution.t[:landing_idx+1],
            "x": x[:landing_idx+1],
            "y": y[:landing_idx+1],
            "z": z[:landing_idx+1],
            "distance": distance
        }

    def baseball_ode(self, t, state, wind_speed, wind_direction, air_density):
        """ODE system for baseball flight"""
        x, y, z, vx, vy, vz = state

        # Wind components
        wind_x = wind_speed * np.cos(wind_direction)
        wind_y = wind_speed * np.sin(wind_direction)

        # Relative velocity (ball velocity - wind velocity)
        v_rel_x = vx - wind_x
        v_rel_y = vy - wind_y
        v_rel_z = vz

        v_rel = np.sqrt(v_rel_x**2 + v_rel_y**2 + v_rel_z**2)

        # Drag force
        A = np.pi * (self.d/2)**2  # cross-sectional area
        F_drag = 0.5 * air_density * v_rel**2 * self.C_d * A

        # Magnus force (simplified)
        omega_rad = self.omega * 2 * np.pi / 60  # rpm to rad/s
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

        # Magnus acceleration (simplified - assuming backspin)
        # For backspin, the Magnus force is mostly upward
        az_magnus = F_magnus / self.m

        # Total acceleration
        ax = ax_drag
        ay = ay_drag
        az = az_drag + az_magnus - self.g

        return [vx, vy, vz, ax, ay, az]

    def calculate_air_density(self, temp_f, humidity, altitude_ft):
        """Calculate air density based on temperature, humidity, and altitude"""
        # Convert temperature from Fahrenheit to Celsius
        temp_c = (temp_f - 32) * 5/9

        # Convert altitude from feet to meters
        altitude_m = altitude_ft * 0.3048

        # Calculate air pressure at altitude (simplified model)
        p0 = 101325  # sea level pressure in Pa
        T0 = 288.15  # sea level temperature in K
        g = 9.81  # gravity in m/s^2
        L = 0.0065  # temperature lapse rate in K/m
        R = 8.31447  # gas constant in J/(mol·K)
        M = 0.0289644  # molar mass of dry air in kg/mol

        T = temp_c + 273.15  # Convert to Kelvin
        p = p0 * (1 - L * altitude_m / T0) ** (g * M / (R * L))

        # Calculate saturation vapor pressure
        e_s = 6.1078 * 10 ** ((7.5 * temp_c) / (237.3 + temp_c))

        # Calculate actual vapor pressure
        e = humidity / 100 * e_s

        # Calculate air density
        Rd = 287.05  # specific gas constant for dry air in J/(kg·K)
        Rv = 461.495  # specific gas constant for water vapor in J/(kg·K)

        rho = (p - e) / (Rd * T) + e / (Rv * T)

        return rho


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
        self.ball_layer = QGraphicsItemGroup()
        
        self.scene.addItem(self.stadium_layer)
        self.scene.addItem(self.weather_layer)
        self.scene.addItem(self.ball_layer)
        
        # SVG renderer and items
        self.svg_renderer = None
        self.stadium_svg_item = None
        
        # Ball and trajectory items
        self.ball_item = None
        self.shadow_item = None
        self.trajectory_path = None
        
        # Set background color
        self.setBackgroundBrush(QBrush(QColor(20, 90, 50)))  # Dark green for grass
        
        # Enable mouse tracking for interactive elements
        self.setMouseTracking(True)
        
        # Set the scene rect to a much larger size initially
        self.scene.setSceneRect(-500, -500, 1000, 1000)
        
        # Fit the view to the scene
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
    
    def resizeEvent(self, event):
        """Handle resize events to maintain proper view scaling"""
        super().resizeEvent(event)
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
    
    def load_stadium_svg(self, svg_path, dimensions):
        """Load an SVG stadium outline and set it up in the view"""
        # Delete all stadium items and create a new layer
        self.scene.removeItem(self.stadium_layer)
        self.stadium_layer = QGraphicsItemGroup()
        self.scene.addItem(self.stadium_layer)
        
        # Create SVG renderer
        try:
            self.svg_renderer = QSvgRenderer(svg_path)
            
            # Check if the SVG loaded successfully
            if not self.svg_renderer.isValid():
                print(f"Invalid SVG file: {svg_path}")
                # Fall back to drawing method if SVG is invalid
                self.draw_stadium(dimensions)
                return
            
            # Create SVG item
            self.stadium_svg_item = QGraphicsSvgItem()
            self.stadium_svg_item.setSharedRenderer(self.svg_renderer)
            
            # Get the SVG's default size
            default_size = self.svg_renderer.defaultSize()
            if default_size.width() <= 0 or default_size.height() <= 0:
                print(f"Invalid SVG dimensions: {default_size.width()} x {default_size.height()}")
                # Fall back to drawing method
                self.draw_stadium(dimensions)
                return
            
            # Determine appropriate scaling and positioning
            scale_factor = 2.0
            
            # Get maximum distance for proper scaling
            max_distance = max(
                dimensions["left_field"],
                dimensions["left_center"],
                dimensions["center_field"],
                dimensions["right_center"],
                dimensions["right_field"]
            ) * scale_factor
            
            # Calculate the scaling ratio to fit our desired size
            svg_size = QSizeF(max_distance * 2, max_distance * 2)
            scale_x = svg_size.width() / default_size.width()
            scale_y = svg_size.height() / default_size.height()
            
            # Apply scaling transform to the SVG item
            scale = min(scale_x, scale_y)
            self.stadium_svg_item.setScale(scale)
            
            # Center the SVG on home plate (0,0)
            # SVG has its own coordinate system, so we need to adjust positioning
            # to ensure home plate is at (0,0) in the scene
            svg_center_x = default_size.width() / 2
            svg_center_y = default_size.height() / 2
            
            self.stadium_svg_item.setPos(
                -svg_center_x * scale,
                -svg_center_y * scale
            )
            
            # Add to stadium layer
            self.stadium_layer.addToGroup(self.stadium_svg_item)
            
            # Add distance markers
            self.add_distance_markers(dimensions, scale_factor)
            
            # Resize scene to fit the stadium with margin
            margin = 150
            self.scene.setSceneRect(
                -max_distance-margin, 
                -max_distance-margin, 
                (max_distance+margin)*2, 
                (max_distance+margin)*2
            )
            
            # Force view update
            self.resetCachedContent()
            self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self.update()
        
        except Exception as e:
            print(f"Error loading SVG: {e}")
            # Fall back to drawing method
            self.draw_stadium(dimensions)
    
    def draw_stadium(self, dimensions):
        """Legacy method for backward compatibility - creates and loads SVG on the fly"""
        # Generate a basic SVG based on dimensions
        svg_path = f"temp_stadium_{id(dimensions)}.svg"
        svg_manager = StadiumSVGManager()
        svg_content = svg_manager.generate_basic_stadium_svg("Stadium", dimensions)
        
        # Save to temporary file
        with open(svg_path, 'w') as f:
            f.write(svg_content)
        
        # Load the SVG
        self.load_stadium_svg(svg_path, dimensions)
    
    def add_distance_markers(self, dimensions, scale_factor):
        """Add distance markers at key points along the outfield wall"""
        # Calculate points for distance markers
        # Left field corner
        left_angle = math.radians(45)
        left_x = -dimensions["left_field"] * scale_factor * math.sin(left_angle)
        left_y = -dimensions["left_field"] * scale_factor * math.cos(left_angle)
        
        # Left-center
        left_center_angle = math.radians(22.5)
        left_center_x = -dimensions["left_center"] * scale_factor * math.sin(left_center_angle)
        left_center_y = -dimensions["left_center"] * scale_factor * math.cos(left_center_angle)
        
        # Center field
        center_y = -dimensions["center_field"] * scale_factor
        
        # Right-center
        right_center_angle = math.radians(22.5)
        right_center_x = dimensions["right_center"] * scale_factor * math.sin(right_center_angle)
        right_center_y = -dimensions["right_center"] * scale_factor * math.cos(right_center_angle)
        
        # Right field corner
        right_angle = math.radians(45)
        right_x = dimensions["right_field"] * scale_factor * math.sin(right_angle)
        right_y = -dimensions["right_field"] * scale_factor * math.cos(right_angle)

    
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
    
    def start_ball_trajectory(self, trajectory_data):
        """Initialize the ball trajectory visualization"""
        # Clear previous ball items
        while self.ball_layer.childItems():
            item = self.ball_layer.childItems()[0]
            self.scene.removeItem(item)
        
        # Scale factor - must match the one used in load_stadium_svg
        scale_factor = 2.0
        
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
        
        # Create trajectory path with proper scaling
        path = QPainterPath()
        
        # Starting point (home plate at 0,0)
        path.moveTo(0, 0)
        
        # Add points along trajectory with proper scaling
        for i in range(0, len(trajectory_data["x"]), 5):
            # Scale the coordinates to match our field display
            scene_x = trajectory_data["y"][i] * scale_factor
            scene_y = -trajectory_data["x"][i] * scale_factor
            
            path.lineTo(scene_x, scene_y)
        
        # Create path item with thicker, more visible line
        self.trajectory_path = QGraphicsPathItem(path)
        self.trajectory_path.setPen(QPen(QColor(255, 140, 0), 3, Qt.PenStyle.DashLine))
        self.trajectory_path.setZValue(100)  # Put trajectory on top of everything
        
        # Add items to scene - add to ball layer which is on top of stadium layer
        self.ball_layer.addToGroup(self.shadow_item)
        self.ball_layer.addToGroup(self.trajectory_path)
        self.ball_layer.addToGroup(self.ball_item)
        
        # Make sure ball layer is visible
        self.ball_layer.setVisible(True)
        self.ball_layer.setZValue(100)  # Ensure ball layer is on top
        
        # Set initial positions
        self.update_ball_position(trajectory_data, 0)
        
        return True

    def update_ball_position(self, trajectory_data, frame):
        """Update the ball position for animation"""
        if frame >= len(trajectory_data["x"]):
            return False
        
        # Scale factor - must match the one used in load_stadium_svg
        scale_factor = 2.0
        
        # Get coordinates with proper scaling
        x = trajectory_data["y"][frame] * scale_factor
        y = -trajectory_data["x"][frame] * scale_factor
        z = trajectory_data["z"][frame]
        
        # Update ball position
        self.ball_item.setPos(x, y)
        
        # Scale ball based on height
        height_factor = max(0.8, min(1.5, 1 + z/100))
        self.ball_item.setScale(height_factor)
        
        # Update shadow position (directly below ball on ground)
        self.shadow_item.setPos(x, y)
        
        # Make shadow more transparent based on height
        opacity = max(0.2, 1.0 - z/200)
        self.shadow_item.setOpacity(opacity)
        
        # Scale shadow size inversely proportional to height
        shadow_scale = max(0.5, 1.0 - z/300)
        self.shadow_item.setScale(shadow_scale)
        
        # Ensure the ball items are visible
        self.ball_item.setVisible(True)
        self.shadow_item.setVisible(True)
        self.trajectory_path.setVisible(True)
        
        return True
    
    def update_ball_position(self, trajectory_data, frame):
        """Update the ball position for animation"""
        if frame >= len(trajectory_data["x"]):
            return False
        
        # Scale factor - must match the one used in draw_stadium
        scale_factor = 2.0
        
        # Get coordinates with proper scaling
        x = trajectory_data["y"][frame] * scale_factor
        y = -trajectory_data["x"][frame] * scale_factor
        z = trajectory_data["z"][frame]
        
        # Update ball position
        self.ball_item.setPos(x, y)
        
        # Scale ball based on height
        height_factor = max(0.8, min(1.5, 1 + z/100))
        self.ball_item.setScale(height_factor)
        
        # Update shadow position (directly below ball on ground)
        self.shadow_item.setPos(x, y)
        
        # Make shadow more transparent based on height
        opacity = max(0.2, 1.0 - z/200)
        self.shadow_item.setOpacity(opacity)
        
        # Scale shadow size inversely proportional to height
        shadow_scale = max(0.5, 1.0 - z/300)
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
        self.ballpark_model = None
        self.textures = {}  # For texture caching if needed
        
        # Set format for better rendering
        fmt = QSurfaceFormat()
        fmt.setSamples(4)  # 4x MSAA
        self.setFormat(fmt)
        
        # Load model with error handling
        try:
            self.ballpark_model = Wavefront(
                'baseballfield.obj',
                create_materials=True,
                collect_faces=True,
                strict=False,  # Tolerate format issues
                parse=True
            )
            print(f"Loaded 3D model with {len(self.ballpark_model.materials)} materials")
        except Exception as e:
            print(f"Error loading 3D model: {str(e)}")
            self.ballpark_model = None
        
        # Camera setup
        self.camera = {
            'pos': [-20.254,18.313,7.4765],
            'target': [0,0,0],
            'up': [0, 0, 1],
            'fov': 55
        }
        
        
       

    def initializeGL(self):
        glEnable(GL_DEPTH_TEST)
        glDepthFunc(GL_LEQUAL)
        glEnable(GL_LIGHTING)
        glEnable(GL_LIGHT0)
        glEnable(GL_COLOR_MATERIAL)
        glClearColor(0.1, 0.1, 0.15, 1.0)
        
        if self.ballpark_model:
            self.compile_stadium_display_list()
        
        glLightfv(GL_LIGHT0, GL_POSITION, [5, 5, 10, 1])
        glLightfv(GL_LIGHT0, GL_DIFFUSE, [1, 1, 1, 1])
        glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [0.5, 0.5, 0.5, 1])
        self.stadium_display_list = None
        
        if self.stadium_display_list is None and self.ballpark_model:
            self.stadium_display_list = glGenLists(1)
            glNewList(self.stadium_display_list, GL_COMPILE)
        
            glPushMatrix()
            glTranslatef(0,0,0)
            glScalef(0.1, 0.1, 0.1)
        
            vertices = self.ballpark_model.vertices
            for mesh in self.ballpark_model.mesh_list:
                # set material...
                glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [0.7, 0.7, 0.7, 1.0])
                glBegin(GL_TRIANGLES)
                for face in mesh.faces:
                    for vertex_i in face:
                        glVertex3f(*vertices[vertex_i])
                glEnd()
        

            glPopMatrix()
            glEndList()
        

    


    def clear_ball(self):
        """Clear any displayed ball from the 3D view"""
        self.ball_pos = None
        self.update()  # Request a redraw of the scene
    
    
    def update_ball_position(self, x, y, z):
        """Update the position of the ball in the 3D view
        
        Args:
            x: x-coordinate (lateral position)
            y: y-coordinate (distance from home plate)
            z: z-coordinate (height)
        """
        # Store the new ball position
        self.ball_pos = (float(x), float(y), float(z))
        
        # Request a redraw of the scene
        self.update()
        
        
    def paintGL(self):
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()
        gluPerspective(self.camera['fov'], 
                      self.width()/self.height(), 
                      0.1, 500)
        gluLookAt(*self.camera['pos'],
                 *self.camera['target'],
                 *self.camera['up'])
        
        # Lighting setup
        glEnable(GL_LIGHTING)
        glEnable(GL_LIGHT0)
        glLightfv(GL_LIGHT0, GL_POSITION, [50, 50, 100, 1])
        glLightfv(GL_LIGHT0, GL_DIFFUSE, [1, 1, 1, 1])
        glLightfv(GL_LIGHT0, GL_SPECULAR, [1, 1, 1, 1])
        glEnable(GL_COLOR_MATERIAL)
        
        if self.stadium_display_list:
            glCallList(self.stadium_display_list)
        
        # Render stadium model
        #if self.ballpark_model:
        #    glPushMatrix()
        #    glTranslatef(-515.808441, 41.099228, -760.366211)
        #    glScalef(0.1, 0.1, 0.1)
        #    
        #    vertices = self.ballpark_model.vertices
        #    for mesh in self.ballpark_model.mesh_list:
        #        # Set material before glBegin()
        #        if hasattr(mesh, 'materials') and mesh.materials:
        #            try:
        #                mtl_name = mesh.materials[0]
        #                if mtl_name in self.ballpark_model.materials:
        #                    mtl = self.ballpark_model.materials[mtl_name]
        #                    glMaterialfv(GL_FRONT, GL_AMBIENT, mtl.ambient)
        #                    glMaterialfv(GL_FRONT, GL_DIFFUSE, mtl.diffuse)
        #                    glMaterialfv(GL_FRONT, GL_SPECULAR, mtl.specular)
        #                    glMaterialf(GL_FRONT, GL_SHININESS, mtl.shininess)
        #            except Exception as e:
        #                print(f"Material error: {str(e)}")
        #                glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [0.7, 0.7, 0.7, 1.0])
        #        else:
        #            glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [0.7, 0.7, 0.7, 1.0])
        #        
        #        glBegin(GL_TRIANGLES)
        #        for face in mesh.faces:
        #            for vertex_i in face:
        #                glVertex3f(*vertices[vertex_i])
        #        glEnd()
        #    
        #    glPopMatrix()
        
        # Ball rendering
        if self.ball_pos is not None:
            x, y, z = self.ball_pos
            
            glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [1.0, 1.0, 1.0, 1.0])
            
            glPushMatrix()
            glTranslatef(x, y, z)
            sphere = gluNewQuadric()
            gluQuadricDrawStyle(sphere, GLU_FILL)
            gluQuadricNormals(sphere, GLU_SMOOTH)
            gluSphere(sphere, 0.5, 16, 16)
            gluDeleteQuadric(sphere)
            glPopMatrix()
            
            glPushMatrix()
            glTranslatef(x, y, 0.01)
            glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [0.0, 0.0, 0.0, 0.5])
            shadow_scale = max(0.5, 1.0 - z/30)
            glScalef(shadow_scale, shadow_scale, 0.1)
            shadow = gluNewQuadric()
            gluQuadricDrawStyle(shadow, GLU_FILL)
            gluQuadricNormals(shadow, GLU_SMOOTH)
            gluSphere(shadow, 0.5, 16, 16)
            gluDeleteQuadric(shadow)
            glPopMatrix()

    def compile_stadium_display_list(self):
        if not self.ballpark_model:
            return
        
        self.stadium_display_list = glGenLists(1)
        glNewList(self.stadium_display_list, GL_COMPILE)
    
        glPushMatrix()
        glTranslatef(-515.808441, 41.099228, -760.366211)
        glScalef(0.1, 0.1, 0.1)
    
        vertices = self.ballpark_model.vertices
        for mesh in self.ballpark_model.mesh_list:
            # Set material before drawing
            if hasattr(mesh, 'materials') and mesh.materials:
                try:
                    mtl_name = mesh.materials[0]
                    if mtl_name in self.ballpark_model.materials:
                        mtl = self.ballpark_model.materials[mtl_name]
                        glMaterialfv(GL_FRONT, GL_AMBIENT, mtl.ambient)
                        glMaterialfv(GL_FRONT, GL_DIFFUSE, mtl.diffuse)
                        glMaterialfv(GL_FRONT, GL_SPECULAR, mtl.specular)
                        glMaterialf(GL_FRONT, GL_SHININESS, mtl.shininess)
                except Exception as e:
                    print(f"Material error: {str(e)}")
                    glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [0.7, 0.7, 0.7, 1.0])
            else:
                glMaterialfv(GL_FRONT, GL_AMBIENT_AND_DIFFUSE, [0.7, 0.7, 0.7, 1.0])
    
            glBegin(GL_TRIANGLES)
            for face in mesh.faces:
                for vertex_i in face:
                    glVertex3f(*vertices[vertex_i])
            glEnd()
    
        glPopMatrix()
        glEndList()
        print("✅ Stadium model compiled into display list")

    


class WindVectorWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)  # Ensure enough space for wind vectors
        self.setMaximumHeight(180)  # Fixed height
        
        # Set background color
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor(20, 20, 20))  # Dark background
        self.setPalette(palette)
        
        # Wind data
        self.wind_speed = 0
        self.wind_direction = 0
        self.animation_state = 0
        
        # Animation timer
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.pulse_animation)
        
    def set_wind_data(self, speed, direction):
        """Set wind data and update display"""
        self.wind_speed = speed
        self.wind_direction = direction
        self.update()
        
        # Start animation if not already running
        if not self.animation_timer.isActive():
            self.animation_timer.start(500)  # 500ms pulse interval
    
    def pulse_animation(self):
        """Create pulsing effect for wind vectors"""
        self.animation_state = (self.animation_state + 1) % 3
        self.update()
    
    def paintEvent(self, event):
        """Draw wind vector indicators"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Draw wind vectors in center of widget
        width = self.width()
        height = self.height()
        
        # Create three large prominent arrows across the top area
        arrow_positions = [
            QPointF(width * 0.25, height * 0.55),  # Left
            QPointF(width * 0.5, height * 0.55),   # Center
            QPointF(width * 0.75, height * 0.55)   # Right
        ]
        
        # Convert meteorological to mathematical angle
        math_angle = (270 - self.wind_direction) % 360
        rad_angle = math.radians(math_angle)
        
        # Scale based on wind speed
        scale_factor = 20  # Large scale for visibility
        length = scale_factor * max(2, self.wind_speed)  # Minimum size for visibility
        
        # Get color based on wind speed and animation state
        if self.wind_speed < 5:
            base_color = QColor(80, 200, 255)  # Bright blue for light wind
        elif self.wind_speed < 10:
            base_color = QColor(50, 255, 120)  # Bright green for moderate wind
        else:
            base_color = QColor(255, 60, 60)  # Bright red for strong wind
            
        # Adjust brightness based on animation state
        brightness_factor = 1.0 + (self.animation_state * 0.1)  # 1.0, 1.1, or 1.2
        color = QColor(
            min(255, int(base_color.red() * brightness_factor)),
            min(255, int(base_color.green() * brightness_factor)),
            min(255, int(base_color.blue() * brightness_factor))
        )
        
        # Draw arrows
        for center_point in arrow_positions:
            # Calculate endpoint
            end_x = center_point.x() + length * math.cos(rad_angle)
            end_y = center_point.y() + length * math.sin(rad_angle)
            
            # Check if endpoint is within bounds
            end_x = max(20, min(end_x, width - 20))  # Keep 20px from edges
            end_y = max(20, min(end_y, height - 20))
            
            end_point = QPointF(end_x, end_y)
            
            # Create the arrow shaft with thicker line
            pen = QPen(color, 14)  # Thick line
            painter.setPen(pen)
            painter.drawLine(center_point, end_point)
            
            # Add arrowhead
            self.draw_arrowhead(painter, end_point, rad_angle, 30, color)
        
        # Add wind speed text label (only once, in the center)
        painter.setPen(QPen(color, 1))
        font = painter.font()
        font.setPointSize(18)
        painter.setFont(font)
        
        # Draw MPH text at fixed position beneath the arrows
        text = f"{self.wind_speed} mph"
        
        font_metrics = painter.fontMetrics()
        text_width = font_metrics.horizontalAdvance(text)
        
        fixed_text_x = self.width() / 2 - text_width / 2
        fixed_text_y = self.height() - 10  # Lower, but still inside the widget
        
        painter.drawText(QPointF(fixed_text_x, fixed_text_y), text)
        
    def draw_arrowhead(self, painter, point, angle, size, color):
        """Draw an arrowhead at the specified position"""
        angle1 = angle + math.radians(150)
        angle2 = angle + math.radians(210)
        
        arrow1_x = point.x() + size * math.cos(angle1)
        arrow1_y = point.y() + size * math.sin(angle1)
        arrow2_x = point.x() + size * math.cos(angle2)
        arrow2_y = point.y() + size * math.sin(angle2)
        
        arrow1_point = QPointF(arrow1_x, arrow1_y)
        arrow2_point = QPointF(arrow2_x, arrow2_y)
        
        # Use the current pen
        painter.drawLine(point, arrow1_point)
        painter.drawLine(point, arrow2_point)
    
    def hideEvent(self, event):
        """Handle widget hide event"""
        if self.animation_timer.isActive():
            self.animation_timer.stop()
        super().hideEvent(event)
    
    def closeEvent(self, event):
        """Handle widget close event"""
        if self.animation_timer.isActive():
            self.animation_timer.stop()
        super().closeEvent(event)




class StadiumSVGManager:
    def get_clean_outline(self, svg_path):
        """Extract simplified path from SVG"""
        try:
            paths, _ = svg2paths(svg_path)
            return max(paths, key=lambda p: p.length())
        except:
            return None
    
    def get_svg_path(self, stadium_name):
        """Get the SVG path for a given stadium name"""
        # Check if the stadium exists in the stadium data
        if stadium_name not in STADIUM_DATA:
            print(f"Stadium {stadium_name} not found in STADIUM_DATA")
            return None
        
        # Get the image path from the stadium data
        image_path = STADIUM_DATA[stadium_name]["image_path"]
        
        # Extract the filename without path or extension
        filename = Path(image_path).stem
        
        # Create the SVG path in the SVGMLBstadiumgraphics directory
        svg_path = f"MLBstadiumgraphics/SVGMLBstadiumgraphics/{filename}.svg"
        
        # Check if the SVG file exists
        if not Path(svg_path).exists():
            print(f"SVG file not found: {svg_path}")
            return None
        
        return svg_path





class SplitView(QWidget):
    """Widget that contains both top-down and umpire views"""
    def __init__(self, stadium_image_path, lat, lon, altitude, parent=None, api_key=open_weather_key):
        super().__init__(parent)
        
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
        
        # Add SVG manager
        self.svg_manager = StadiumSVGManager()
        
        # Weather and simulation data
        self.weather_data = None
        self.trajectory_data = None
        self.current_frame = 0
        
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
        # Get SVG path for the selected stadium
        svg_path = self.svg_manager.get_svg_path(stadium_name)
        
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
            
            # 2D View
            self.stadium_view.load_stadium_svg(svg_path, self.dimensions)
            
            # 3D View
            self.umpire_view.update()
        else:
            print(f"Stadium {stadium_name} not found in STADIUM_DATA")
    
    def setup_ui(self):
        """Set up the split view UI layout"""
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(5, 5, 5, 5)
        
        # Create top section with wind vector widget and miniature view side by side
        top_layout = QHBoxLayout()
        
        # Add the wind vector widget taking 70% of the width (increased from 60%)
        self.wind_vector_widget = WindVectorWidget()
        top_layout.addWidget(self.wind_vector_widget, 70)  # Increased from 60
        
        # Create a container for the stadium image
        stadium_container = QWidget()
        stadium_layout = QVBoxLayout(stadium_container)
        stadium_layout.setContentsMargins(0, 0, 0, 0)
        
        # Miniature view of the actual stadium image - make larger
        self.mini_view = QLabel()
        self.mini_view.setMinimumSize(200, 150)  # Increased size
        self.mini_view.setScaledContents(True)
        
        def update_stadium_image(self, stadium_name):
             image_path = os.path.join("EffortScraper/OddsAPI","MLBstadiumgraphics", f"{stadium_name}.gif")
             if os.path.exists(image_path):
                 pixmap = QPixmap(image_path)
                 self.mini_view.setPixmap(pixmap)
             else:
                 print(f"⚠️ Image not found for: {stadium_name}")

        
        
        self.mini_view.setFrameShape(QLabel.Shape.Box)
        stadium_layout.addWidget(self.mini_view)
        
        # The stadium info label is now moved to the stadium view
        # and no longer added to the top layout
        self.info_label = QLabel("Stadium Info")
        
        top_layout.addWidget(stadium_container, 30)  # Decreased from 40
        
        self.layout.addLayout(top_layout)
        
        # Main views container - side by side
        views_layout = QHBoxLayout()
        
        # Container for top-down stadium view with overlay elements
        stadium_view_container = QWidget()
        stadium_view_layout = QVBoxLayout(stadium_view_container)
        stadium_view_layout.setContentsMargins(0, 0, 0, 0)
        
        # Top-down stadium view
        self.stadium_view = StadiumView()
        self.stadium_view.setMinimumSize(1000, 850)
        stadium_view_layout.addWidget(self.stadium_view)
        
        # Stadium info label positioned at top-right of stadium view using absolute positioning
        self.info_label.setStyleSheet("color: white; background-color: rgba(0, 0, 0, 120); padding: 5px;")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.info_label.setMinimumSize(100,80)
        self.info_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.info_label.setParent(self.stadium_view)
        self.info_label.move(self.stadium_view.width() - 150, 10)  # Position in top right
        self.info_label.show()
        
        # Connect resize event to reposition the label
        self.stadium_view.resizeEvent = lambda event: (
            self.info_label.move(self.stadium_view.width() - 150, 10),
            type(self.stadium_view).resizeEvent(self.stadium_view, event)
        )
        
        # Add flight stats list widget positioned at bottom right of stadium view
        self.flight_stats_list = QListWidget(self.stadium_view)
        self.flight_stats_list.setMinimumWidth(400)  # Increased from 300
        self.flight_stats_list.setMaximumHeight(150)
        self.flight_stats_list.setStyleSheet("background-color: rgba(0, 0, 0, 150); color: white;")
        self.flight_stats_list.move(
            self.stadium_view.width() - 420,  # Position 420px from right edge
            self.stadium_view.height() - 160   # Position 160px from bottom
        )
        self.flight_stats_list.show()
        
        # Update flight_stats_list position when stadium_view is resized
        original_resize_event = self.stadium_view.resizeEvent
        
        def new_resize_event(event):
            original_resize_event(event)
            self.flight_stats_list.move(
                self.stadium_view.width() - 420,
                self.stadium_view.height() - 160
            )
        self.stadium_view.resizeEvent = new_resize_event
        
        views_layout.addWidget(stadium_view_container, 85)
        
        # 3D umpire view on the right
        self.umpire_view = UmpireView3D()
        self.umpire_view.setMinimumSize(800, 600)
        views_layout.addWidget(self.umpire_view, 75)
        
        self.layout.addLayout(views_layout)
        
        # Weather status label
        self.weather_label = QLabel("Weather data: Not loaded")
        self.layout.addWidget(self.weather_label)
    
    
    
    
    
    def fetch_weather_data(self):
        """Fetch real weather data for the stadium location"""
        try:
            print(f"Fetching weather data for lat: {self.lat}, lon: {self.lon}")
            weather_json = self.weather_service.get_weather_by_location(self.lat, self.lon)
            print("Weather JSON received:", weather_json)
            
            self.weather_data = self.weather_service.extract_weather_data(weather_json)
            print("Extracted weather data:", self.weather_data)
            
            self.update_weather_label()
            self.update_weather_visualization()
        except Exception as e:
            print(f"Error fetching weather data: {e}")
            import traceback
            traceback.print_exc()
            
    def set_custom_weather(self, wind_speed, wind_direction):
        """Set custom weather data for simulation"""
        self.weather_data = {
            "wind_speed": wind_speed,
            "wind_direction": wind_direction,
            "temperature": 75,  # Default temperature
            "humidity": 50,     # Default humidity
            "condition": "Custom",
            "description": "custom weather settings",
            "precipitation": 0,
        }
        self.update_weather_label()
        self.update_weather_visualization()
    
    def update_weather_label(self):
        """Update the weather information display"""
        if self.weather_data:
            wind_info = f"Wind: {self.weather_data['wind_speed']} mph at {self.weather_data['wind_direction']}°"
            temp_info = f"Temp: {self.weather_data['temperature']}°F"
            cond_info = f"Conditions: {self.weather_data['description']}"

            self.weather_label.setText(f"{wind_info} | {temp_info} | {cond_info}")
    
    def update_weather_visualization(self):
        """Update the visual representation of weather conditions"""
        if self.weather_data:
            # Update wind vectors in the dedicated widget instead of StadiumView
            self.wind_vector_widget.set_wind_data(
                self.weather_data["wind_speed"],
                self.weather_data["wind_direction"]
            )
    
    # Modification for simulate_ball_flight method to update the flight stats list
    def simulate_ball_flight(self, exit_velocity, launch_angle, spin_rate=1800):
        """Simulate ball flight with current weather conditions"""
        if not self.weather_data:
            print("Weather data not available")
            return
            
        # Update spin rate
        self.ball_simulator.omega = spin_rate
            
        # Calculate trajectory
        self.trajectory_data = self.ball_simulator.calculate_trajectory(
            exit_velocity,
            launch_angle,
            self.weather_data["wind_speed"],
            self.weather_data["wind_direction"],
            self.weather_data["temperature"],
            self.weather_data["humidity"],
            self.altitude
        )
        
        # Log trajectory data for debugging
        print(f"Trajectory starting point: ({self.trajectory_data['x'][0]}, {self.trajectory_data['y'][0]})")
        print(f"Ball will travel: {self.trajectory_data['distance']:.1f} feet")
        
        # Initialize ball visualization in top-down view
        success = self.stadium_view.start_ball_trajectory(self.trajectory_data)
        if not success:
            print("Warning: Failed to visualize trajectory in 2D view")
        
        # Clear any existing ball in umpire view
        self.umpire_view.clear_ball()
        
        # Start animation
        self.current_frame = 0
        
        # Check if it's a home run
        is_home_run = self.check_if_home_run(self.trajectory_data)
        
        # Calculate stats
        distance = self.trajectory_data["distance"]
        max_height = max(self.trajectory_data["z"])
        hr_text = "HOME RUN!" if is_home_run else ""
        
        # Create stats text
        stats_text = f"Exit Vel: {exit_velocity} mph | Launch: {launch_angle}° | Spin: {spin_rate} rpm | Dist: {distance:.1f} ft | Height: {max_height:.1f} ft {hr_text}"
        
        # Add to flight stats list
        self.flight_stats_list.addItem(stats_text)
        self.flight_stats_list.scrollToBottom()
        
        # Also update the weather label for backward compatibility
        self.weather_label.setText(
            f"{self.weather_label.text()} | Distance: {distance:.1f} ft | "
            f"Max Height: {max_height:.1f} ft{' - ' + hr_text if hr_text else ''}"
        )
        
        # Start animation timer
        self.animation_timer.start(30)  # 30ms per frame (~33fps)
    
    def update_animation(self):
        """Update animation frame for both views"""
        if not self.trajectory_data:
            return
            
        self.current_frame += 1
        
        if self.current_frame >= len(self.trajectory_data["x"]):
            self.animation_timer.stop()
            self.current_frame = 0
            return
        
        # Update ball position in top-down view
        self.stadium_view.update_ball_position(
            self.trajectory_data, 
            self.current_frame
        )
        
        # Update ball position in 3D umpire view
        # Convert ball coordinates for umpire view perspective
        # Note: For 3D view, we can use the actual coordinates directly
        x = self.trajectory_data["y"][self.current_frame]  # Side to side (left/right field)
        y = -self.trajectory_data["x"][self.current_frame]  # Distance from plate (negative is toward outfield)
        z = self.trajectory_data["z"][self.current_frame]  # Height
        
        self.umpire_view.update_ball_position(x, y, z)
    
    def check_if_home_run(self, trajectory_data):
        """Check if the trajectory results in a home run"""
        if not self.dimensions:
            return False
            
        # Get the last point in the trajectory
        final_x = trajectory_data["x"][-1]  # Distance straight out from home plate
        final_y = trajectory_data["y"][-1]  # Distance side to side
        
        # Calculate distance from home plate
        distance = np.sqrt(final_x**2 + final_y**2)
        
        # Get the launch angle in the horizontal plane
        horizontal_angle = np.degrees(np.arctan2(final_y, final_x))
        
        # Determine the wall distance based on the angle
        dimensions = self.dimensions
        wall_distance = None
        
        # Left field (45° to 90° from center)
        if -45 <= horizontal_angle < -15:
            wall_distance = dimensions["left_field"]
        # Left-center field (15° to 45° from center)
        elif -15 <= horizontal_angle < 0:
            wall_distance = dimensions["left_center"]
        # Center field (within 15° of center)
        elif -15 <= horizontal_angle < 15:
            wall_distance = dimensions["center_field"]
        # Right-center field (15° to 45° from center)
        elif 15 <= horizontal_angle < 45:
            wall_distance = dimensions["right_center"]
        # Right field (45° to 90° from center)
        else:
            wall_distance = dimensions["right_field"]
        
        # Check if the ball cleared the wall
        # Also check if the ball was still above ~8 feet when it reached the wall
        return distance >= wall_distance and trajectory_data["z"][-1] > 8


class MLBWeatherApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MLB Weather & Ball Flight Simulator")
        self.setMinimumSize(1200, 950)  # Increased size for better display with wind widget

        # Create central widget and layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Stadium selection
        stadium_layout = QHBoxLayout()  # Changed to horizontal layout for better space usage
        stadium_label = QLabel("Select Stadium:")
        self.stadium_combo = QComboBox()
        self.stadium_combo.addItems(STADIUM_DATA.keys())
        self.stadium_combo.currentTextChanged.connect(self.change_stadium)
        self.stadium_combo.setMinimumWidth(200)  # Ensure enough width for stadium names
        stadium_layout.addWidget(stadium_label)
        stadium_layout.addWidget(self.stadium_combo)
        stadium_layout.addStretch()  # Add stretch to push controls to the left
        main_layout.addLayout(stadium_layout)

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
        
        # Add the split view with stretching to take most of the space
        main_layout.addWidget(self.stadium_widget, 1)  # Use stretch factor

        # Ball flight controls in a horizontal layout for better space usage
        controls_container = QWidget()
        controls_main_layout = QVBoxLayout(controls_container)
        controls_main_layout.setContentsMargins(5, 5, 5, 5)
        
        # Top row of controls
        top_controls = QHBoxLayout()

        # Exit Velocity control
        ev_group = QGroupBox("Exit Velocity (mph)")
        ev_layout = QVBoxLayout()
        self.ev_slider = QSlider(Qt.Orientation.Horizontal)
        self.ev_slider.setRange(80, 120)
        self.ev_slider.setValue(100)
        self.ev_value = QLabel("100")
        self.ev_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ev_slider.valueChanged.connect(lambda v: self.ev_value.setText(str(v)))
        ev_layout.addWidget(self.ev_slider)
        ev_layout.addWidget(self.ev_value)
        ev_group.setLayout(ev_layout)
        top_controls.addWidget(ev_group)

        # Launch Angle control
        la_group = QGroupBox("Launch Angle (degrees)")
        la_layout = QVBoxLayout()
        self.la_slider = QSlider(Qt.Orientation.Horizontal)
        self.la_slider.setRange(0, 45)
        self.la_slider.setValue(25)
        self.la_value = QLabel("25")
        self.la_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.la_slider.valueChanged.connect(lambda v: self.la_value.setText(str(v)))
        la_layout.addWidget(self.la_slider)
        la_layout.addWidget(self.la_value)
        la_group.setLayout(la_layout)
        top_controls.addWidget(la_group)
        
        # Spin rate control  
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
        top_controls.addWidget(spin_group)
        
        controls_main_layout.addLayout(top_controls)
        
        # Bottom row of controls
        bottom_controls = QHBoxLayout()

        # Wind override controls
        wind_group = QGroupBox("Override Weather")
        wind_layout = QGridLayout()  # Use grid layout for better organization
        
        # Wind speed
        wind_layout.addWidget(QLabel("Wind Speed (mph):"), 0, 0)
        self.wind_speed_spin = QSpinBox()
        self.wind_speed_spin.setRange(0, 40)
        self.wind_speed_spin.setValue(10)
        wind_layout.addWidget(self.wind_speed_spin, 0, 1)
        
        # Wind direction
        wind_layout.addWidget(QLabel("Wind Direction (°):"), 1, 0)
        self.wind_dir_spin = QSpinBox()
        self.wind_dir_spin.setRange(0, 359)
        self.wind_dir_spin.setValue(0)
        wind_layout.addWidget(self.wind_dir_spin, 1, 1)
        
        # Override checkbox
        self.override_weather = QCheckBox("Override Weather Data")
        wind_layout.addWidget(self.override_weather, 2, 0, 1, 2)  # Span two columns
        
        wind_group.setLayout(wind_layout)
        bottom_controls.addWidget(wind_group)
        
        # Action buttons
        button_layout = QVBoxLayout()
        
        # Simulate button
        self.simulate_btn = QPushButton("Simulate Ball Flight")
        self.simulate_btn.setMinimumHeight(40)  # Make button taller for emphasis
        self.simulate_btn.clicked.connect(self.simulate_flight)
        button_layout.addWidget(self.simulate_btn)
        
        # Update weather button
        self.update_weather_btn = QPushButton("Update Weather Data")
        self.update_weather_btn.clicked.connect(self.update_weather)
        button_layout.addWidget(self.update_weather_btn)
        
        button_layout.addStretch()
        bottom_controls.addLayout(button_layout)
        
        controls_main_layout.addLayout(bottom_controls)
        main_layout.addWidget(controls_container)

    def change_stadium(self, stadium_name):
        """Update the stadium when selection changes"""
        # Update the stadium view
        self.stadium_widget.update_stadium(stadium_name)
        
        # Also update the weather data for the new stadium location
        print(f"Requesting weather update for new stadium: {stadium_name}")
        self.stadium_widget.fetch_weather_data()

    def simulate_flight(self):
        exit_velocity = self.ev_slider.value()
        launch_angle = self.la_slider.value()
        spin_rate = self.spin_slider.value()
        
        # Check if we should override weather
        if self.override_weather.isChecked():
            wind_speed = self.wind_speed_spin.value()
            wind_direction = self.wind_dir_spin.value()
            self.stadium_widget.set_custom_weather(wind_speed, wind_direction)
            
        self.stadium_widget.simulate_ball_flight(exit_velocity, launch_angle, spin_rate)

    def update_weather(self):
        self.stadium_widget.fetch_weather_data()


def main():
    app = QApplication(sys.argv)
    window = MLBWeatherApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
