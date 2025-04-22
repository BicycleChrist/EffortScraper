from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QWidget,
    QPushButton, QSlider, QLabel, QComboBox, QGroupBox, QSpinBox, QCheckBox,
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsPathItem,
    QGraphicsItemGroup, QGraphicsLineItem, QGraphicsRectItem
)
from PyQt6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush, QPainterPath
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, QSizeF
import sys
import numpy as np
import math
from scipy.integrate import solve_ivp
from weatherman import WeatherService, STADIUM_DATA


#TODO: Get more accurate dimensions for ball parks for better outline drawing 

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
        
        # Ball and trajectory items
        self.ball_item = None
        self.shadow_item = None
        self.trajectory_path = None
        scale_factor = 2.0
        # Set background color
        self.setBackgroundBrush(QBrush(QColor(20, 90, 50)))  # Dark green for grass
        
        # Enable mouse tracking for interactive elements
        self.setMouseTracking(True)
        
        # Set the scene rect to a much larger size initially
        self.scene.setSceneRect(-500, -500, 1000, 1000)  # Increased from -200, -200, 400, 400
        
        # Fit the view to the scene
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
    
    def resizeEvent(self, event):
        """Handle resize events to maintain proper view scaling"""
        super().resizeEvent(event)
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
    
    def draw_stadium(self, dimensions):
        """Draw the stadium outline based on provided dimensions"""
        # Delete all stadium items and create a new layer
        self.scene.removeItem(self.stadium_layer)
        self.stadium_layer = QGraphicsItemGroup()
        self.scene.addItem(self.stadium_layer)
        
        # Get dimensions
        left_field = dimensions["left_field"]
        left_center = dimensions["left_center"]
        center_field = dimensions["center_field"]
        right_center = dimensions["right_center"]
        right_field = dimensions["right_field"]
        
        # Scale factor to make the field larger
        scale_factor = 2.0
        
        # Apply scaling to dimensions
        left_field *= scale_factor
        left_center *= scale_factor
        center_field *= scale_factor
        right_center *= scale_factor
        right_field *= scale_factor
        
        # Create a path for the outfield wall
        wall_path = QPainterPath()
        
        # Start at home plate
        wall_path.moveTo(0, 0)
        
        # Draw foul line to left field corner
        left_angle = math.radians(45)
        left_x = -left_field * math.sin(left_angle)
        left_y = -left_field * math.cos(left_angle)
        wall_path.lineTo(left_x, left_y)
        
        # Left-center
        left_center_angle = math.radians(22.5)
        left_center_x = -left_center * math.sin(left_center_angle)
        left_center_y = -left_center * math.cos(left_center_angle)
        
        # Center field
        center_y = -center_field
        
        # Right-center
        right_center_angle = math.radians(22.5)
        right_center_x = right_center * math.sin(right_center_angle)
        right_center_y = -right_center * math.cos(right_center_angle)
        
        # Right field corner
        right_angle = math.radians(45)
        right_x = right_field * math.sin(right_angle)
        right_y = -right_field * math.cos(right_angle)
        
        # Create a smooth curved outfield wall
        wall_path.cubicTo(left_center_x, left_center_y, 0, center_y, right_center_x, right_center_y)
        wall_path.lineTo(right_x, right_y)
        
        # Back to home plate
        wall_path.lineTo(0, 0)
        
        # Create outfield wall item
        outfield_wall = QGraphicsPathItem(wall_path)
        outfield_wall.setPen(QPen(QColor(255, 255, 255), 3))
        self.stadium_layer.addToGroup(outfield_wall)
        
        # Create the base diamond shape
        infield_size = 90 * scale_factor
        diamond_path = QPainterPath()
        diamond_path.moveTo(0, 0)  # Home plate
        diamond_path.lineTo(infield_size, -infield_size)  # First base
        diamond_path.lineTo(0, -infield_size*2)  # Second base
        diamond_path.lineTo(-infield_size, -infield_size)  # Third base
        diamond_path.lineTo(0, 0)  # Back to home
        
        # Draw the baselines
        baselines = QGraphicsPathItem(diamond_path)
        baselines.setPen(QPen(QColor(255, 255, 255), 2))
        self.stadium_layer.addToGroup(baselines)
        
        # Draw bases with larger size for visibility
        base_size = 12
        
        # Home plate
        home = QGraphicsEllipseItem(-base_size/2, -base_size/2, base_size, base_size)
        home.setBrush(QBrush(QColor(255, 255, 255)))
        self.stadium_layer.addToGroup(home)
        
        # First base
        first = QGraphicsEllipseItem(infield_size - base_size/2, -infield_size - base_size/2, 
                                    base_size, base_size)
        first.setBrush(QBrush(QColor(255, 255, 255)))
        self.stadium_layer.addToGroup(first)
        
        # Second base
        second = QGraphicsEllipseItem(-base_size/2, -infield_size*2 - base_size/2, 
                                     base_size, base_size)
        second.setBrush(QBrush(QColor(255, 255, 255)))
        self.stadium_layer.addToGroup(second)
        
        # Third base
        third = QGraphicsEllipseItem(-infield_size - base_size/2, -infield_size - base_size/2, 
                                    base_size, base_size)
        third.setBrush(QBrush(QColor(255, 255, 255)))
        self.stadium_layer.addToGroup(third)
        
        # Add distance markers with larger font
        self.add_distance_marker(left_x, left_y, f"{dimensions['left_field']}'", -1)
        self.add_distance_marker(left_center_x, left_center_y, f"{dimensions['left_center']}'", -1)
        self.add_distance_marker(0, center_y, f"{dimensions['center_field']}'", 0)
        self.add_distance_marker(right_center_x, right_center_y, f"{dimensions['right_center']}'", 1)
        self.add_distance_marker(right_x, right_y, f"{dimensions['right_field']}'", 1)
        
        # Resize and fit the scene - use larger margin
        margin = 150
        max_distance = max(left_field, left_center, center_field, right_center, right_field)
        
        self.scene.setSceneRect(-max_distance-margin, -max_distance-margin, 
                               (max_distance+margin)*2, (max_distance+margin)*2)
        
        # Force a complete update of the view
        self.resetCachedContent()
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.update()
    
    def add_distance_marker(self, x, y, text, align):
        """Add a distance marker at the specified location"""
        text_item = self.scene.addSimpleText(text)
        text_item.setBrush(QBrush(QColor(255, 255, 255)))
        
        # Make text larger
        font = text_item.font()
        font.setPointSize(14)  # Increased from 12
        text_item.setFont(font)
        
        # Position text based on alignment
        text_width = text_item.boundingRect().width()
        text_height = text_item.boundingRect().height()
        
        if align < 0:  # Left-aligned
            text_item.setPos(x - text_width, y - text_height/2)
        elif align > 0:  # Right-aligned
            text_item.setPos(x, y - text_height/2)
        else:  # Center-aligned
            text_item.setPos(x - text_width/2, y - text_height)
        
        self.stadium_layer.addToGroup(text_item)
    
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
        
        def add_arrowhead(self, x, y, angle, size, color):
            """Add an arrowhead to a wind vector"""
            angle1 = angle + math.radians(150)
            angle2 = angle + math.radians(210)
            
            arrow1_x = x + size * math.cos(angle1)
            arrow1_y = y + size * math.sin(angle1)
            arrow2_x = x + size * math.cos(angle2)
            arrow2_y = y + size * math.sin(angle2)
            
            line1 = QGraphicsLineItem(x, y, arrow1_x, arrow1_y)
            line2 = QGraphicsLineItem(x, y, arrow2_x, arrow2_y)
            
            line1.setPen(QPen(color, 3))
            line2.setPen(QPen(color, 3))
            
            self.weather_layer.addToGroup(line1)
            self.weather_layer.addToGroup(line2)
    
    def start_ball_trajectory(self, trajectory_data):
        """Initialize the ball trajectory visualization"""
        # Clear previous ball items
        while self.ball_layer.childItems():
            item = self.ball_layer.childItems()[0]
            self.scene.removeItem(item)
        
        # Scale factor - must match the one used in draw_stadium
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
        
        # Starting point (home plate)
        path.moveTo(0, 0)
        
        # Add points along trajectory - with proper scaling
        for i in range(0, len(trajectory_data["x"]), 5):
            # Scale the coordinates to match our field display
            scene_x = trajectory_data["y"][i] * scale_factor
            scene_y = -trajectory_data["x"][i] * scale_factor
            
            path.lineTo(scene_x, scene_y)
        
        # Create path item with thicker, more visible line
        self.trajectory_path = QGraphicsPathItem(path)
        self.trajectory_path.setPen(QPen(QColor(255, 140, 0), 3, Qt.PenStyle.DashLine))
        
        # Add items to scene
        self.ball_layer.addToGroup(self.shadow_item)
        self.ball_layer.addToGroup(self.trajectory_path)
        self.ball_layer.addToGroup(self.ball_item)
        
        # Set initial positions
        self.update_ball_position(trajectory_data, 0)
        
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
    
    
############################## Vector animation logic ##################    
    
    def stop_wind_animation(self):
        """Stop the wind vector animation"""
        if hasattr(self, 'wind_animation_timer') and self.wind_animation_timer.isActive():
            self.wind_animation_timer.stop()

    def hideEvent(self, event):
        """Handle widget hide event"""
        self.stop_wind_animation()
        super().hideEvent(event)
    
    def closeEvent(self, event):
        """Handle widget close event"""
        self.stop_wind_animation()
        super().closeEvent(event)
    
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
    
    
    
    


class UmpireView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Set up the graphics scene
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        
        # Enable antialiasing for smoother graphics
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Set background to sky blue
        self.setBackgroundBrush(QBrush(QColor(135, 206, 235)))
        
        # Setup field elements
        self.setup_field()
        
        # Ball item
        self.ball_item = None
        
        # Set minimum size
        self.setMinimumSize(500, 300)
    
    def setup_field(self):
        """Set up the field from umpire perspective"""
        # Create field surface
        field_path = QPainterPath()
        
        # Draw the basic field trapezoid
        # Bottom of the view (wider part)
        field_path.moveTo(-300, 200)
        field_path.lineTo(300, 200)
        
        # Top of the view (narrower part - distant outfield)
        field_path.lineTo(150, -100)
        field_path.lineTo(-150, -100)
        field_path.closeSubpath()
        
        # Create field item
        field_item = QGraphicsPathItem(field_path)
        field_item.setBrush(QBrush(QColor(20, 90, 50)))  # Dark green for grass
        field_item.setPen(QPen(Qt.GlobalColor.black, 1))
        self.scene.addItem(field_item)
        
        # Draw the infield dirt circle
        infield_dirt = QGraphicsEllipseItem(-120, 50, 240, 100)
        infield_dirt.setBrush(QBrush(QColor(200, 150, 100)))  # Light brown for dirt
        infield_dirt.setPen(QPen(Qt.PenStyle.NoPen))
        self.scene.addItem(infield_dirt)
        
        # Draw the pitcher's mound
        mound = QGraphicsEllipseItem(-15, 40, 30, 15)
        mound.setBrush(QBrush(QColor(180, 130, 80)))  # Slightly darker brown
        mound.setPen(QPen(Qt.PenStyle.NoPen))
        self.scene.addItem(mound)
        
        # Add the foul lines
        left_foul = QGraphicsLineItem(-60, 170, -150, -100)
        left_foul.setPen(QPen(QColor(255, 255, 255), 2))
        self.scene.addItem(left_foul)
        
        right_foul = QGraphicsLineItem(60, 170, 150, -100)
        right_foul.setPen(QPen(QColor(255, 255, 255), 2))
        self.scene.addItem(right_foul)
        
        # Home plate
        home_path = QPainterPath()
        home_path.moveTo(0, 190)
        home_path.lineTo(-10, 180)
        home_path.lineTo(0, 170)
        home_path.lineTo(10, 180)
        home_path.closeSubpath()
        
        home_plate = QGraphicsPathItem(home_path)
        home_plate.setBrush(QBrush(QColor(255, 255, 255)))
        home_plate.setPen(QPen(Qt.GlobalColor.black, 1))
        self.scene.addItem(home_plate)
        
        # Add bases
        first_base = QGraphicsRectItem(90, 100, 10, 10)
        first_base.setBrush(QBrush(QColor(255, 255, 255)))
        first_base.setPen(QPen(Qt.GlobalColor.black, 1))
        self.scene.addItem(first_base)
        
        second_base = QGraphicsRectItem(-5, 30, 10, 10)
        second_base.setBrush(QBrush(QColor(255, 255, 255)))
        second_base.setPen(QPen(Qt.GlobalColor.black, 1))
        self.scene.addItem(second_base)
        
        third_base = QGraphicsRectItem(-100, 100, 10, 10)
        third_base.setBrush(QBrush(QColor(255, 255, 255)))
        third_base.setPen(QPen(Qt.GlobalColor.black, 1))
        self.scene.addItem(third_base)
        
        # Set scene rect for proper sizing
        self.scene.setSceneRect(-300, -150, 600, 400)
    
    def project_ball_position(self, x, y, z):
        """Project 3D coordinates to 2D screen coordinates with perspective"""
        # Improved perspective transformation
        distance_factor = 250 - min(200, y)  # Adjust range for better depth perception
        scale_factor = distance_factor / 250  # Scale from 0 to 1
        
        # Calculate screen position with improved scaling
        screen_x = x * scale_factor * 1.2  # Widen the x-axis movement
        screen_y = 180 - (y * 0.8 * scale_factor) - (z * 0.9 * scale_factor)
        
        # Calculate ball size (smaller as it gets further away)
        # Improved sizing for better perspective
        ball_size = max(5.0, 30.0 * (1.0 - (0.6 * scale_factor)))
        
        return screen_x, screen_y, ball_size
    
    def update_ball_position(self, x, y, z):
        """Update the ball position with 3D coordinates"""
        # If no ball exists yet, create one
        if not self.ball_item:
            self.ball_item = QGraphicsEllipseItem(0, 0, 20, 20)
            self.ball_item.setBrush(QBrush(QColor(255, 255, 255)))
            self.ball_item.setPen(QPen(Qt.GlobalColor.black, 1))
            self.scene.addItem(self.ball_item)
        
        # Project 3D position to 2D screen coordinates
        screen_x, screen_y, ball_size = self.project_ball_position(x, y, z)
        
        # Update ball position and size
        self.ball_item.setRect(screen_x - ball_size/2, screen_y - ball_size/2, 
                              ball_size, ball_size)
        
        # Make sure the ball is visible (bring to front)
        self.ball_item.setZValue(10)
        
        return screen_x, screen_y, ball_size
    
    def clear_ball(self):
        """Remove the ball from the scene"""
        if self.ball_item:
            self.scene.removeItem(self.ball_item)
            self.ball_item = None


class SplitView(QWidget):
    """Widget that contains both top-down and umpire views"""
    def __init__(self, stadium_image_path, lat, lon, altitude, parent=None, api_key=None):
        super().__init__(parent)
        
        # Set up weather service with the API key
        try:
            from Creds import open_weather_key
            self.api_key = api_key or open_weather_key
        except ImportError:
            self.api_key = api_key or "YOUR_OPENWEATHER_API_KEY"
        
        self.weather_service = WeatherService(self.api_key)
        
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
        
        # Setup UI
        # Setup UI
        self.setup_ui()
        
        # Animation timer
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.update_animation)
        
        
        # Fetch initial weather data
        self.fetch_weather_data()
    
    def setup_ui(self):
        """Set up the split view UI layout"""
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(5, 5, 5, 5)
        
        # Top section with stadium info and miniature view
        top_layout = QHBoxLayout()
        
        # Stadium name and info
        self.info_label = QLabel("Stadium Info")
        top_layout.addWidget(self.info_label)
        
        # Miniature view of the actual stadium image
        self.mini_view = QLabel()
        self.mini_view.setFixedSize(150, 100)
        self.mini_view.setScaledContents(True)
        self.mini_view.setPixmap(self.stadium_pixmap)
        self.mini_view.setFrameShape(QLabel.Shape.Box)
        top_layout.addWidget(self.mini_view)
        
        self.layout.addLayout(top_layout)
        
        # Main views container - side by side
        views_layout = QHBoxLayout()
        
        # Top-down stadium view on the left (make it narrower)
        self.stadium_view = StadiumView()
        self.stadium_view.setMinimumSize(1000, 850)
        views_layout.addWidget(self.stadium_view, 85)  # 40% of width
        
        # Umpire perspective view on the right (make it wider)
        self.umpire_view = UmpireView()
        self.umpire_view.setMinimumSize(800, 600)
        views_layout.addWidget(self.umpire_view, 75)  # 60% of width
        
        self.layout.addLayout(views_layout)
        
        # Weather and simulation status label
        self.weather_label = QLabel("Weather data: Not loaded")
        self.layout.addWidget(self.weather_label)
    
    def update_stadium(self, stadium_name):
        """Update the stadium with data from the selected stadium"""
        if stadium_name not in STADIUM_DATA:
            return
            
        data = STADIUM_DATA[stadium_name]
        self.stadium_pixmap = QPixmap(data["image_path"])
        self.mini_view.setPixmap(self.stadium_pixmap)
        self.lat = data["lat"]
        self.lon = data["lon"]
        self.altitude = data["altitude"]
        self.dimensions = data["dimensions"]
        self.stadium_name = stadium_name
        
        # Update stadium name and info label
        self.info_label.setText(f"{stadium_name}\nAlt: {self.altitude} ft")
        
        # Draw the new stadium outline in top-down view
        self.stadium_view.draw_stadium(self.dimensions)
        
        # Reset weather and simulation data
        self.weather_data = None
        self.trajectory_data = None
        self.current_frame = 0
        self.umpire_view.clear_ball()
        self.update()
        
        # Update weather if we already have it
        if self.weather_data:
            self.update_weather_visualization()
    
    def fetch_weather_data(self):
        """Fetch real weather data for the stadium location"""
        try:
            weather_json = self.weather_service.get_weather_by_location(self.lat, self.lon)
            self.weather_data = self.weather_service.extract_weather_data(weather_json)
            self.update_weather_label()
            self.update_weather_visualization()
        except Exception as e:
            print(f"Error fetching weather data: {e}")
            
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
            # Draw wind vectors in top-down view
            self.stadium_view.draw_wind_indicators(
                self.weather_data["wind_speed"],
                self.weather_data["wind_direction"]
            )
    
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
        
        # Initialize ball visualization in top-down view
        self.stadium_view.start_ball_trajectory(self.trajectory_data)
        
        # Clear any existing ball in umpire view
        self.umpire_view.clear_ball()
        
        # Start animation
        self.current_frame = 0
        
        # Check if it's a home run
        is_home_run = self.check_if_home_run(self.trajectory_data)
        
        # Update status with distance
        distance = self.trajectory_data["distance"]
        max_height = max(self.trajectory_data["z"])
        hr_text = " - HOME RUN!" if is_home_run else ""
        self.weather_label.setText(
            f"{self.weather_label.text()} | Distance: {distance:.1f} ft | "
            f"Max Height: {max_height:.1f} ft{hr_text}"
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
        
        # Update ball position in umpire view
        # Convert ball coordinates for umpire view perspective
        x = self.trajectory_data["y"][self.current_frame]  # Side to side
        y = self.trajectory_data["x"][self.current_frame]  # Distance from plate
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
        self.setMinimumSize(1000, 700)  # Larger size for split view

        # Create central widget and layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Stadium selection
        stadium_layout = QVBoxLayout()
        stadium_label = QLabel("Select Stadium:")
        self.stadium_combo = QComboBox()
        self.stadium_combo.addItems(STADIUM_DATA.keys())
        self.stadium_combo.currentTextChanged.connect(self.change_stadium)
        stadium_layout.addWidget(stadium_label)
        stadium_layout.addWidget(self.stadium_combo)
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
        
        main_layout.addWidget(self.stadium_widget)

        # Ball flight controls
        controls_layout = QVBoxLayout()

        # Exit Velocity control
        ev_layout = QVBoxLayout()
        ev_label = QLabel("Exit Velocity (mph):")
        self.ev_slider = QSlider(Qt.Orientation.Horizontal)
        self.ev_slider.setRange(80, 120)
        self.ev_slider.setValue(100)
        self.ev_value = QLabel("100")
        self.ev_slider.valueChanged.connect(lambda v: self.ev_value.setText(str(v)))
        ev_layout.addWidget(ev_label)
        ev_layout.addWidget(self.ev_slider)
        ev_layout.addWidget(self.ev_value)
        controls_layout.addLayout(ev_layout)

        # Launch Angle control
        la_layout = QVBoxLayout()
        la_label = QLabel("Launch Angle (degrees):")
        self.la_slider = QSlider(Qt.Orientation.Horizontal)
        self.la_slider.setRange(0, 45)
        self.la_slider.setValue(25)
        self.la_value = QLabel("25")
        self.la_slider.valueChanged.connect(lambda v: self.la_value.setText(str(v)))
        la_layout.addWidget(la_label)
        la_layout.addWidget(self.la_slider)
        la_layout.addWidget(self.la_value)
        controls_layout.addLayout(la_layout)
        
        # Spin rate control
        spin_layout = QVBoxLayout()
        spin_label = QLabel("Spin Rate (rpm):")
        self.spin_slider = QSlider(Qt.Orientation.Horizontal)
        self.spin_slider.setRange(1000, 3000)
        self.spin_slider.setValue(1800)
        self.spin_value = QLabel("1800")
        self.spin_slider.valueChanged.connect(lambda v: self.spin_value.setText(str(v)))
        spin_layout.addWidget(spin_label)
        spin_layout.addWidget(self.spin_slider)
        spin_layout.addWidget(self.spin_value)
        controls_layout.addLayout(spin_layout)

        # Wind override controls
        wind_group = QGroupBox("Override Weather")
        wind_layout = QVBoxLayout()

        # Wind speed
        wind_speed_layout = QHBoxLayout()
        wind_speed_layout.addWidget(QLabel("Wind Speed (mph):"))
        self.wind_speed_spin = QSpinBox()
        self.wind_speed_spin.setRange(0, 40)
        self.wind_speed_spin.setValue(10)
        wind_speed_layout.addWidget(self.wind_speed_spin)
        wind_layout.addLayout(wind_speed_layout)

        # Wind direction
        wind_dir_layout = QHBoxLayout()
        wind_dir_layout.addWidget(QLabel("Wind Direction (°):"))
        self.wind_dir_spin = QSpinBox()
        self.wind_dir_spin.setRange(0, 359)
        self.wind_dir_spin.setValue(0)
        wind_dir_layout.addWidget(self.wind_dir_spin)
        wind_layout.addLayout(wind_dir_layout)

        # Override checkbox
        self.override_weather = QCheckBox("Override Weather Data")
        wind_layout.addWidget(self.override_weather)

        wind_group.setLayout(wind_layout)
        controls_layout.addWidget(wind_group)

        # Simulate button
        self.simulate_btn = QPushButton("Simulate Ball Flight")
        self.simulate_btn.clicked.connect(self.simulate_flight)
        controls_layout.addWidget(self.simulate_btn)

        # Update weather button
        self.update_weather_btn = QPushButton("Update Weather Data")
        self.update_weather_btn.clicked.connect(self.update_weather)
        controls_layout.addWidget(self.update_weather_btn)

        # Change Stadium upon selection
        self.stadium_combo.currentTextChanged.connect(self.change_stadium)

        main_layout.addLayout(controls_layout)

    def update_stadium(self, stadium_name):
        if stadium_name not in STADIUM_DATA:
            return
            
        data = STADIUM_DATA[stadium_name]
        self.stadium_pixmap = QPixmap(data["image_path"])
        self.mini_view.setPixmap(self.stadium_pixmap)
        self.lat = data["lat"]
        self.lon = data["lon"]
        self.altitude = data["altitude"]
        self.dimensions = data["dimensions"]
        self.stadium_name = stadium_name
        
        # Update stadium name and info label
        self.info_label.setText(f"{stadium_name}\nAlt: {self.altitude} ft")
        
        # Draw the new stadium outline in top-down view
        self.stadium_view.draw_stadium(self.dimensions)
        
        # Reset weather and simulation data
        self.weather_data = None
        self.trajectory_data = None
        self.current_frame = 0
        self.umpire_view.clear_ball()
        self.update()
        
        # Update weather if we already have it
        if self.weather_data:
            self.update_weather_visualization()

    def change_stadium(self, stadium_name):
        """Update the stadium when selection changes"""
        self.stadium_widget.update_stadium(stadium_name)


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
