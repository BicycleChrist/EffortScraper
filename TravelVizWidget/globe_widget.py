import sys
import math
import time
import numpy as np
import OpenGL.GL as gl
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime, timedelta
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture
from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal, QVariantAnimation, QEasingCurve
from PyQt6.QtGui import (QMatrix4x4, QVector3D, QQuaternion, QMouseEvent,
                         QWheelEvent, QColor, QImage, QVector4D)
from pathlib import Path
import math
import os
import logging

logger = logging.getLogger(__name__)

class TeamTravelAnimation:
    """Manages animated team travel sequences - Essential for UI"""
    
    def __init__(self, parent_widget=None):
        self.parent_widget = parent_widget
        self.team_sequences = {}
        self.current_team = None
        self.animation_active = True
        self.animation_progress = 0.0
        self.animation_speed = 0.15
        self.current_segment = 0
        self.segment_progress = 0.0
        self.loop_animation = True
        self.debug = False
    
    def build_team_sequence(self, team_id: str, travel_data: List, season_start: datetime = None) -> Dict:
        """Build chronological travel sequence for a team"""
        # NOTE: travel_data is already filtered by season when loaded from the database
        # We don't need additional date filtering here - just filter by team

        # Filter and sort team travel by date
        team_travel = [t for t in travel_data
                      if hasattr(t, 'team_id') and t.team_id.upper() == team_id.upper()
                      and hasattr(t, 'travel_date') and t.travel_date]
        
        team_travel.sort(key=lambda x: x.travel_date)
        
        if not team_travel:
            return {'segments': [], 'total_distance': 0, 'total_duration': 0}
        
        segments = []
        total_distance = 0
        total_duration = 0
        
        for travel in team_travel:
            dep_coords = self.get_city_coordinates(travel.departure_city)
            arr_coords = self.get_city_coordinates(travel.arrival_city)
            
            if not dep_coords or not arr_coords:
                continue
                
            distance = self.haversine_distance(dep_coords[0], dep_coords[1], 
                                             arr_coords[0], arr_coords[1])
            duration = self.estimate_flight_duration(distance)
            
            segment = {
                'departure_city': travel.departure_city,
                'arrival_city': travel.arrival_city,
                'departure_coords': dep_coords,
                'arrival_coords': arr_coords,
                'travel_date': travel.travel_date,
                'game_date': getattr(travel, 'game_date', None),
                'opponent': getattr(travel, 'opponent', ''),
                'distance_miles': distance,
                'duration_hours': duration,
                'path_3d': self.generate_great_circle_path(
                    dep_coords[0], dep_coords[1], arr_coords[0], arr_coords[1]
                )
            }
            
            segments.append(segment)
            total_distance += distance
            total_duration += duration
        
        sequence = {
            'team_id': team_id,
            'segments': segments,
            'total_distance': total_distance,
            'total_duration': total_duration,
            'games_count': len(segments),
            'season_start': season_start if season_start else (team_travel[0].travel_date if team_travel else None),
            'last_update': datetime.now()
        }
        
        self.team_sequences[team_id] = sequence
        return sequence
    
    def start_animation(self, team_id: str):
        """Start animation for a specific team"""
        if team_id not in self.team_sequences:
            return False
            
        self.current_team = team_id
        self.animation_active = True
        self.animation_progress = 0.0
        self.current_segment = 0
        self.segment_progress = 0.0
        return True
    
    def stop_animation(self):
        """Stop current animation"""
        self.animation_active = False
        self.current_team = None
    
    def update_animation(self, frame_time: float) -> Optional[Dict]:
        """Update animation state and return current position WITH orientation"""
        if not self.animation_active or not self.current_team:
            return None
            
        sequence = self.team_sequences.get(self.current_team)
        if not sequence or not sequence['segments']:
            return None
        
        segments = sequence['segments']
        total_segments = len(segments)
        
        progress_delta = (frame_time * self.animation_speed) / total_segments
        self.animation_progress += progress_delta
        
        if self.animation_progress >= 1.0:
            if self.loop_animation:
                self.animation_progress = 0.0
                self.current_segment = 0
                self.segment_progress = 0.0
            else:
                self.animation_active = False
                return None
        
        segment_float = self.animation_progress * total_segments
        self.current_segment = int(segment_float)
        self.segment_progress = segment_float - self.current_segment
        
        if self.current_segment >= total_segments:
            self.current_segment = total_segments - 1
            self.segment_progress = 1.0
        
        current_seg = segments[self.current_segment]
        
        # Calculate position (existing logic)
        if current_seg['path_3d'] and len(current_seg['path_3d']) > 1:
            path_points = current_seg['path_3d']
            point_float = self.segment_progress * (len(path_points) - 1)
            point_index = int(point_float)
            point_progress = point_float - point_index
            
            if point_index >= len(path_points) - 1:
                current_position = path_points[-1]
            else:
                p1 = path_points[point_index]
                p2 = path_points[point_index + 1]
                current_position = (
                    p1[0] + (p2[0] - p1[0]) * point_progress,
                    p1[1] + (p2[1] - p1[1]) * point_progress,
                    p1[2] + (p2[2] - p1[2]) * point_progress
                )
        else:
            dep_coords = current_seg['departure_coords']
            current_position = self.lat_lon_to_3d(dep_coords[0], dep_coords[1], 1.05)
        
        # NEW: Calculate orientation using the plane model
        orientation = None
        if (hasattr(self.parent_widget, 'plane_model') and 
            self.parent_widget.plane_model and 
            self.parent_widget.plane_model.is_ready() and 
            current_seg['path_3d'] and len(current_seg['path_3d']) > 1):
            
            try:
                orientation = self.parent_widget.plane_model.calculate_smooth_orientation_from_path(
                    current_seg['path_3d'], 
                    self.segment_progress
                )
                if (self.debug): print(f"🛩️ Calculated orientation at progress {self.segment_progress:.3f}");
            except Exception as e:
                logger.warning(f"⚠️ Failed to calculate airplane orientation: {e}")
                orientation = None
        
        return {
            'position': current_position,
            'orientation': orientation,  # NOW INCLUDED!
            'segment': current_seg,
            'segment_index': self.current_segment,
            'segment_progress': self.segment_progress,  # Added for debugging
            'total_segments': total_segments,
            'progress': self.animation_progress,
            'team_id': self.current_team
        }
    
    def get_city_coordinates(self, city_name: str) -> Optional[Tuple[float, float]]:
        """Get coordinates from parent widget"""
        if self.parent_widget and hasattr(self.parent_widget, 'get_city_coordinates'):
            return self.parent_widget.get_city_coordinates(city_name)
        return None
    
    def haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great circle distance in miles"""
        R = 3959
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = (math.sin(delta_lat/2)**2 + 
             math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon/2)**2)
        
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        return R * c
    
    def estimate_flight_duration(self, distance_miles: float) -> float:
        """Estimate flight duration in hours based on distance"""
        if distance_miles < 300:
            return 1.0
        elif distance_miles < 1000:
            return distance_miles / 400
        else:
            return distance_miles / 500
    
    def lat_lon_to_3d(self, lat: float, lon: float, radius: float = 1.0) -> Tuple[float, float, float]:
        """Convert latitude/longitude to 3D coordinates"""
        lat_rad = math.radians(lat)
        lon_rad = math.radians(lon)
        
        x = radius * math.cos(lat_rad) * math.cos(lon_rad)
        y = radius * math.sin(lat_rad)
        z = -radius * math.cos(lat_rad) * math.sin(lon_rad)
        
        return (float(x), float(y), float(z))
    
    def generate_great_circle_path(self, start_lat: float, start_lon: float, 
                                 end_lat: float, end_lon: float, num_points: int = 50) -> List[Tuple[float, float, float]]:
        """Generate great circle path between two points"""
        if abs(start_lat - end_lat) < 0.001 and abs(start_lon - end_lon) < 0.001:
            return []
        
        delta_lon = end_lon - start_lon
        if delta_lon > 180:
            end_lon -= 360
        elif delta_lon < -180:
            end_lon += 360
        
        path_points = []
        base_altitude = 1.02
        max_altitude = 1.08
        
        for i in range(num_points + 1):
            t = i / num_points
            lat = start_lat + t * (end_lat - start_lat)
            lon = start_lon + t * (end_lon - start_lon)
            altitude_factor = math.sin(t * math.pi) * (max_altitude - base_altitude) + base_altitude
            point_3d = self.lat_lon_to_3d(lat, lon, altitude_factor)
            path_points.append(point_3d)
        
        return path_points


class ContrailManager:
    """Manages contrail/wake effects behind animated planes"""

    def __init__(self, max_points: int = 150, max_age: float = 2.5):
        self.position_history = []  # List of (position_tuple, timestamp)
        self.max_points = max_points
        self.max_age = max_age

    def add_position(self, position: tuple, current_time: float):
        """Add new position to contrail history"""
        if not position or len(position) != 3:
            return

        self.position_history.append((position, current_time))

        # Remove expired points (older than max_age)
        cutoff_time = current_time - self.max_age
        self.position_history = [
            (pos, t) for pos, t in self.position_history
            if t > cutoff_time
        ][-self.max_points:]

    def get_render_data(self, current_time: float):
        """Get vertex data for rendering: [x, y, z, age] per point"""
        if not self.position_history:
            return None

        data = []
        for pos, timestamp in self.position_history:
            age = current_time - timestamp
            data.extend([pos[0], pos[1], pos[2], age])

        return np.array(data, dtype=np.float32)

    def clear(self):
        """Clear contrail history"""
        self.position_history.clear()

    def has_data(self) -> bool:
        """Check if there's contrail data to render"""
        return len(self.position_history) > 0


class FlightGlobeWidget(QOpenGLWidget):
    """3D Globe Widget for sports team travel visualization - Simplified"""
    
    # Signals
    locationSelected = pyqtSignal(float, float, str)
    flightSelected = pyqtSignal(str)
    animationStatusChanged = pyqtSignal(bool, str)
    animationProgressChanged = pyqtSignal(float, dict)
    markerClicked = pyqtSignal(str, dict)  # marker_id, marker_data
    fpsUpdated = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.debug = False # enables prints extra info in terminal
        
        # OpenGL resources
        self.shader_program = None
        self.travel_shader = None
        self.marker_shader = None
        self.stars_shader = None
        self._stars_vao = None
        self.vao = None
        self.vbo = None
        self.ebo = None
        self.earth_texture = None
        self.night_texture = None  # optional city-lights map (night side)
        
        # Marker resources
        self.marker_vao = None
        self.marker_vbo = None
        self.marker_geometry = None
        
        # Team logo textures (+ small QImages for QPainter overlay chips)
        self.team_logo_textures = {}
        self.team_logo_images = {}
        self.default_marker_texture = None
        
        # Sphere geometry
        self.vertices = None
        self.indices = None
        self.texcoords = None
        self.normals = None
        self.sphere_detail = 64
        
        # View and interaction state
        self.rotation_matrix = QMatrix4x4()
        self.zoom_level = 1.0
        self.min_zoom = 0.3
        self.max_zoom = 8.0

        # Semantic LOD tier (0=far clusters, 1=mid logos, 2=near detail)
        self._lod = 1
        self._cluster_cache_key = None
        self._cluster_cache = []

        # Game card target: set by clicking a game cube, cleared by clicking
        # the same cube / empty space. Keyed by game id, NOT marker object —
        # marker dicts are rebuilt on schedule refreshes, which both killed
        # the card "on a timer" and broke second-click dismissal.
        self._selected_game_key = None

        # Cached per-path GL buffers (rebuilt when travel_paths changes)
        self._path_buffers = []          # [(vao, vbo, point_count), ...]
        self._path_buffer_key = None

        # Cached per-flight route arcs (charter origin->destination)
        self._flight_route_cache = {}    # icao24 -> (vao, vbo, count, color)
        # Remembered label slot per flight so chips don't reshuffle each frame
        self._flight_label_slots = {}    # icao24 -> slot index
        # Same for live score chips, keyed by stable game key. Each entry:
        # {'slot': int, 'off': smoothed [dx, dy] offset from the anchor}.
        # Layout is only re-solved on a slow clock (see _chip_layout_t) —
        # between solves chips ride rigidly on their anchors.
        self._score_chip_state = {}      # game key -> placement state
        self._chip_layout_t = -1.0       # animation_time of last layout solve
        self._chip_layout_keys = set()   # game keys at last solve

        # Cube-unfold game panel: click a game cube and it unfolds into a
        # flat panel showing enriched game data (lineups, weather, fatigue).
        # {'key': game key, 'phase': 'opening'|'closing', 't': 0..1,
        #  'spin0': (yaw, tilt) captured at click so the net starts at the
        #  GL cube's current orientation and spins down to face the camera}
        self._unfold = None
        self.team_logo_images_big = {}   # 128px QImages for unfold faces/panel
        # Route arcs are opt-in: click a plane to toggle its arc
        self._shown_route_icaos = set()
        
        # Mouse interaction
        self.last_mouse_pos = QPoint()
        self.is_grabbed = False
        self.rotation_speed = 1.0
        self.rotation_momentum = QVector3D(0, 0, 0)
        self.passive_spin = True # spins constantly
        self.passive_rotation_speed = QVector3D(0.05, 0.25, 0)
        self.momentum_decay = 0.95
        self.sensitivity = 0.25
        
        # Animation and timing
        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self.update)
        self.animation_time = 0.0
        self.last_frame_time = time.time()

        # FPS accounting (emitted via fpsUpdated about once per second)
        self._fps_frame_count = 0
        self._fps_last_emit = time.time()

        # Eased fly-to animation (center_on_location)
        self._flyto_animation = None
        
        # Sports travel data
        self.travel_data = []
        self.travel_paths = []
        self.team_city_markers = []

        # Live flight tracking
        self.live_flights = {}  # icao24 -> flight_data dict

        # Team travel animation system (Required by UI)
        self.travel_animation = TeamTravelAnimation(self)
        self.animated_marker_position = None
        self.show_travel_animation = False
        self.disable_spinning_during_animation = True
        
        
        # Plane Model
        self.plane_model = PlaneModel(self) 
        
        # Display options
        self.show_travel_paths = True
        self.show_team_cities = True
        self.show_atmosphere = True
        self.show_day_night = False  # Default to original shadow rendering
        self.terminator_width = 0.1

        # Contrail system
        self.contrail_manager = ContrailManager()
        self.contrail_shader = None
        self.show_contrails = True

        # OpenGL state tracking
        self.gl_initialized = False
        
        # Initialize default view
        self.setup_default_view()
        self.setMouseTracking(True)
        # Render timer is started/stopped by showEvent/hideEvent so a hidden
        # tab doesn't burn CPU/GPU at 60fps (important when embedded).
        self.render_interval_ms = 16  # ~60 FPS

    def showEvent(self, event):
        super().showEvent(event)
        if not self.render_timer.isActive():
            self.last_frame_time = time.time()
            self._fps_last_emit = time.time()
            self._fps_frame_count = 0
            self.render_timer.start(self.render_interval_ms)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.render_timer.stop()
    
    def setup_default_view(self):
        """Setup default globe view focused on North America"""
        self.rotation_matrix = QMatrix4x4()
        self.rotation_matrix.rotate(0, 1, 0, 0)
        self.rotation_matrix.rotate(-50, 0, 1, 0)
    
    # OpenGL Initialization
    def initializeGL(self):
        """Initialize OpenGL resources"""
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glClearColor(0.01, 0.02, 0.08, 1.0)
        
        self.generate_sphere_geometry()
        self.setup_shaders()
        self.setup_vertex_buffers()
        self.setup_earth_texture()
        self.load_team_logo_textures()
        self.setup_marker_vao()
        self.plane_model.setup_airplane_model()
        
        self.gl_initialized = bool(self.marker_vao)
        if self.gl_initialized:
            logger.info("✅ Globe OpenGL initialized")
        else:
            logger.error("❌ Globe OpenGL failed")
    
    def generate_sphere_geometry(self):
        """Generate sphere geometry with correct UV mapping for Earth textures"""
        vertices = []
        indices = []
        texcoords = []
        normals = []
        
        stacks = self.sphere_detail
        slices = self.sphere_detail * 2
        
        for i in range(stacks + 1):
            lat = 90.0 - (180.0 * float(i) / stacks)
            lat_rad = math.radians(lat)
            
            for j in range(slices + 1):
                lon = -180.0 + (360.0 * float(j) / slices)
                lon_rad = math.radians(lon)
                
                x = math.cos(lat_rad) * math.cos(lon_rad)
                y = math.sin(lat_rad)
                z = -math.cos(lat_rad) * math.sin(lon_rad)
                
                vertices.extend([x, y, z])
                normals.extend([x, y, z])
                
                u = float(j) / slices
                v = float(i) / stacks
                texcoords.extend([u, v])
        
        for i in range(stacks):
            for j in range(slices):
                first = i * (slices + 1) + j
                second = first + slices + 1
                
                indices.extend([first, second, first + 1])
                indices.extend([second, second + 1, first + 1])
        
        self.vertices = np.array(vertices, dtype=np.float32)
        self.indices = np.array(indices, dtype=np.uint32)
        self.texcoords = np.array(texcoords, dtype=np.float32)
        self.normals = np.array(normals, dtype=np.float32)
    
    
    
    def setup_vertex_buffers(self):
        """Setup vertex buffer objects"""
        if self.vertices is None:
            return
        
        vertex_count = len(self.vertices) // 3
        vertex_data = []
        
        for i in range(vertex_count):
            vertex_data.extend([
                self.vertices[i*3], self.vertices[i*3+1], self.vertices[i*3+2],
                self.texcoords[i*2], self.texcoords[i*2+1],
                self.normals[i*3], self.normals[i*3+1], self.normals[i*3+2]
            ])
        
        vertex_array = np.array(vertex_data, dtype=np.float32)
        
        self.vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(self.vao)
        
        self.vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, vertex_array.nbytes, vertex_array, gl.GL_STATIC_DRAW)
        
        self.ebo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, self.ebo)
        gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, self.indices.nbytes, self.indices, gl.GL_STATIC_DRAW)
        
        stride = 8 * 4
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(0))
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(1, 2, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(12))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(2, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(20))
        gl.glEnableVertexAttribArray(2)

    
    def load_shader_from_file(self, filepath):
        """Load shader source code from a file"""
        try:
            shader_path = Path(filepath)
            return shader_path.read_text(encoding='utf-8')
        except FileNotFoundError:
            logger.error(f"❌ Shader file not found: {filepath}")
            return None
        except Exception as e:
            logger.error(f"❌ Error loading shader file {filepath}: {e}")
            return None

    def setup_shaders(self):
        """Setup all OpenGL shaders - now loading from separate files"""
        shader_dir = Path(__file__).resolve().parent / "shaders"
        
        # Earth shader
        earth_vertex = self.load_shader_from_file(shader_dir / "earth.vert")
        earth_fragment = self.load_shader_from_file(shader_dir / "earth.frag")
        
        if earth_vertex and earth_fragment:
            self.shader_program = QOpenGLShaderProgram()
            self.shader_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, earth_vertex)
            self.shader_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, earth_fragment)
            if not self.shader_program.link():
                logger.error("❌ Failed to link Earth shader")
            else:
                logger.info("✅ Earth shader linked successfully")
        else:
            logger.error("❌ Failed to load Earth shader files")
        
        # Travel path shader
        travel_vertex = self.load_shader_from_file(shader_dir / "travel_path.vert")
        travel_fragment = self.load_shader_from_file(shader_dir / "travel_path.frag")
        
        if travel_vertex and travel_fragment:
            self.travel_shader = QOpenGLShaderProgram()
            self.travel_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, travel_vertex)
            self.travel_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, travel_fragment)
            if not self.travel_shader.link():
                logger.error("❌ Failed to link Travel shader")
            else:
                logger.info("✅ Travel shader linked successfully")
        else:
            logger.error("❌ Failed to load Travel shader files")
        
        # Marker shader
        marker_vertex = self.load_shader_from_file(shader_dir / "marker.vert")
        marker_fragment = self.load_shader_from_file(shader_dir / "marker.frag")
        
        if marker_vertex and marker_fragment:
            self.marker_shader = QOpenGLShaderProgram()
            self.marker_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, marker_vertex)
            self.marker_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, marker_fragment)
            if not self.marker_shader.link():
                logger.error("❌ Failed to link Marker shader")
            else:
                logger.info("✅ Marker shader linked successfully")
        else:
            logger.error("❌ Failed to load Marker shader files")
        
        # Airplane shader
        airplane_vertex = self.load_shader_from_file(shader_dir / "airplane.vert")
        airplane_fragment = self.load_shader_from_file(shader_dir / "airplane.frag")
        
        if airplane_vertex and airplane_fragment:
            self.airplane_shader = QOpenGLShaderProgram()
            self.airplane_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, airplane_vertex)
            self.airplane_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, airplane_fragment)
            if not self.airplane_shader.link():
                logger.error("❌ Failed to link Airplane shader")
                self.airplane_shader = None
            else:
                logger.info("✅ Airplane shader linked successfully")
        else:
            logger.error("❌ Failed to load Airplane shader files")
            self.airplane_shader = None

        # Starfield shader (optional ambiance; missing files just skip it)
        stars_vertex = self.load_shader_from_file(shader_dir / "stars.vert")
        stars_fragment = self.load_shader_from_file(shader_dir / "stars.frag")

        if stars_vertex and stars_fragment:
            self.stars_shader = QOpenGLShaderProgram()
            self.stars_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, stars_vertex)
            self.stars_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, stars_fragment)
            if not self.stars_shader.link():
                logger.warning("Failed to link Stars shader")
                self.stars_shader = None
        else:
            self.stars_shader = None

        # Contrail shader
        contrail_vertex = self.load_shader_from_file(shader_dir / "contrail.vert")
        contrail_fragment = self.load_shader_from_file(shader_dir / "contrail.frag")

        if contrail_vertex and contrail_fragment:
            self.contrail_shader = QOpenGLShaderProgram()
            self.contrail_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, contrail_vertex)
            self.contrail_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, contrail_fragment)
            if not self.contrail_shader.link():
                logger.error("❌ Failed to link Contrail shader")
                self.contrail_shader = None
            else:
                logger.info("✅ Contrail shader linked successfully")
        else:
            logger.warning("⚠️  Contrail shader files not found (optional feature)")
            self.contrail_shader = None

    def setup_earth_texture(self):
        """Load Earth texture (and optional night city-lights map)"""
        base_dir = Path(__file__).resolve().parent
        texture_files = ["no_ice_clouds_8k.jpg", "earth.jpg"]

        image = None
        for texture_file in texture_files:
            image = QImage(str(base_dir / texture_file))
            if not image.isNull():
                break

        if image and not image.isNull():
            image = image.convertToFormat(QImage.Format.Format_RGB888)
            self.earth_texture = QOpenGLTexture(image)
            self.earth_texture.setMinificationFilter(QOpenGLTexture.Filter.LinearMipMapLinear)
            self.earth_texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)

        # Night-side city lights (optional asset; globe works without it)
        night_image = QImage(str(base_dir / "earth_nightmap.jpg"))
        if not night_image.isNull():
            night_image = night_image.convertToFormat(QImage.Format.Format_RGB888)
            self.night_texture = QOpenGLTexture(night_image)
            self.night_texture.setMinificationFilter(QOpenGLTexture.Filter.LinearMipMapLinear)
            self.night_texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
    
    def setup_marker_vao(self):
        """Create reusable marker VAO and VBO"""
        if not self.context():
            return
                
        self.marker_geometry = self.create_marker_cube_geometry()
        if self.marker_geometry is None:
            return
        
        self.marker_vao = gl.glGenVertexArrays(1)
        self.marker_vbo = gl.glGenBuffers(1)
        
        if self.marker_vao == 0 or self.marker_vbo == 0:
            return
        
        gl.glBindVertexArray(self.marker_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.marker_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, self.marker_geometry.nbytes, 
                       self.marker_geometry, gl.GL_STATIC_DRAW)
        
        stride = 8 * 4
        
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(0))
        
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(12))
        
        gl.glEnableVertexAttribArray(2)
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(24))
        
        gl.glBindVertexArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
    
    def create_marker_cube_geometry(self):
        """Create geometry for a spinning 3D cube marker"""
        vertices = [
            # Front face
            -1.0, -1.0,  1.0,  0.0,  0.0,  1.0,  0.0, 0.0,
             1.0, -1.0,  1.0,  0.0,  0.0,  1.0,  1.0, 0.0,
             1.0,  1.0,  1.0,  0.0,  0.0,  1.0,  1.0, 1.0,
            -1.0, -1.0,  1.0,  0.0,  0.0,  1.0,  0.0, 0.0,
             1.0,  1.0,  1.0,  0.0,  0.0,  1.0,  1.0, 1.0,
            -1.0,  1.0,  1.0,  0.0,  0.0,  1.0,  0.0, 1.0,
            # Back face
             1.0, -1.0, -1.0,  0.0,  0.0, -1.0,  0.0, 0.0,
            -1.0, -1.0, -1.0,  0.0,  0.0, -1.0,  1.0, 0.0,
            -1.0,  1.0, -1.0,  0.0,  0.0, -1.0,  1.0, 1.0,
             1.0, -1.0, -1.0,  0.0,  0.0, -1.0,  0.0, 0.0,
            -1.0,  1.0, -1.0,  0.0,  0.0, -1.0,  1.0, 1.0,
             1.0,  1.0, -1.0,  0.0,  0.0, -1.0,  0.0, 1.0,
            # Left face
            -1.0, -1.0, -1.0, -1.0,  0.0,  0.0,  0.0, 0.0,
            -1.0, -1.0,  1.0, -1.0,  0.0,  0.0,  1.0, 0.0,
            -1.0,  1.0,  1.0, -1.0,  0.0,  0.0,  1.0, 1.0,
            -1.0, -1.0, -1.0, -1.0,  0.0,  0.0,  0.0, 0.0,
            -1.0,  1.0,  1.0, -1.0,  0.0,  0.0,  1.0, 1.0,
            -1.0,  1.0, -1.0, -1.0,  0.0,  0.0,  0.0, 1.0,
            # Right face
             1.0, -1.0,  1.0,  1.0,  0.0,  0.0,  0.0, 0.0,
             1.0, -1.0, -1.0,  1.0,  0.0,  0.0,  1.0, 0.0,
             1.0,  1.0, -1.0,  1.0,  0.0,  0.0,  1.0, 1.0,
             1.0, -1.0,  1.0,  1.0,  0.0,  0.0,  0.0, 0.0,
             1.0,  1.0, -1.0,  1.0,  0.0,  0.0,  1.0, 1.0,
             1.0,  1.0,  1.0,  1.0,  0.0,  0.0,  0.0, 1.0,
            # Top face
            -1.0,  1.0,  1.0,  0.0,  1.0,  0.0,  0.0, 0.0,
             1.0,  1.0,  1.0,  0.0,  1.0,  0.0,  1.0, 0.0,
             1.0,  1.0, -1.0,  0.0,  1.0,  0.0,  1.0, 1.0,
            -1.0,  1.0,  1.0,  0.0,  1.0,  0.0,  0.0, 0.0,
             1.0,  1.0, -1.0,  0.0,  1.0,  0.0,  1.0, 1.0,
            -1.0,  1.0, -1.0,  0.0,  1.0,  0.0,  0.0, 1.0,
            # Bottom face
            -1.0, -1.0, -1.0,  0.0, -1.0,  0.0,  0.0, 0.0,
             1.0, -1.0, -1.0,  0.0, -1.0,  0.0,  1.0, 0.0,
             1.0, -1.0,  1.0,  0.0, -1.0,  0.0,  1.0, 1.0,
            -1.0, -1.0, -1.0,  0.0, -1.0,  0.0,  0.0, 0.0,
             1.0, -1.0,  1.0,  0.0, -1.0,  0.0,  1.0, 1.0,
            -1.0, -1.0,  1.0,  0.0, -1.0,  0.0,  0.0, 1.0,
        ]
        
        return np.array(vertices, dtype=np.float32)
    
    # Team Logo and Texture Management
    def load_team_logo_textures(self):
        """Load team logo textures - simplified version"""
        league_configs = {
            "MLB": {
                "folder": "mlb_logos", 
                "teams": {
                    "phi": "Phillies.png", "mil": "Brewers.png", "chc": "Cubs.png",
                    "cin": "Reds.png", "pit": "Pirates.png", "stl": "Cardinals.png",
                    "lad": "Dodgers.png", "sd": "Padres.png", "sf": "Giants.png",
                    "nyy": "Yankees.png", "bos": "Redsox.png", "tb": "Rays.png",
                    "tor": "BlueJays.png", "bal": "Orioles.png", "cle": "Guardians.png",
                    "chw": "WhiteSox.png", "det": "Tigers.png", "kc": "Royals.png",
                    "min": "Twins.png", "hou": "Astros.png", "sea": "Mariners.png",
                    "tex": "Rangers.png", "laa": "Angels.png", "ath": "Athletics.png",
                    "atl": "Braves.png", "mia": "Marlins.png", "nym": "Mets.png",
                    "wsh": "Nationals.png", "col": "Rockies.png", "ari": "Diamondbacks.png"
                }
            },
            "NBA": {
                "folder": "nba_logos", 
                "teams": {
                    "phi": "76ers.png", "mil": "Bucks.png", "chi": "Bulls.png",
                    "cle": "Cavaliers.png", "bos": "Celtics.png", "lac": "Clippers.png",
                    "mem": "Grizzlies.png", "atl": "Hawks.png", "mia": "Heat.png",
                    "cha": "Hornets.png", "utah": "Jazz.png", "ny": "Knicks.png",
                    "lal": "Lakers.png", "orl": "Magic.png", "dal": "Mavericks.png",
                    "bkn": "Nets.png", "den": "Nuggets.png", "ind": "Pacers.png",
                    "no": "Pelicans.png", "det": "Pistons.png", "por": "TrailBlazers.png",
                    "sac": "SACKings.png", "sa": "Spurs.png", "phx": "Suns.png",
                    "okc": "Thunder.png", "min": "Timberwolves.png", "tor": "Raptors.png",
                    "gs": "Warriors.png", "wsh": "Wizards.png", "hou": "Rockets.png"
                }
            },
            "NHL": {
                "folder": "nhl_logos",
                "teams": {
                    "ana": "Ducks.png", "utah": "Mammoth.png", "bos": "Bruins.png",
                    "buf": "Sabres.png", "cgy": "Flames.png", "car": "Hurricanes.png",
                    "chi": "Blackhawks.png", "col": "Avalanche.png", "cbj": "BlueJackets.png",
                    "dal": "Stars.png", "det": "RedWings.png", "edm": "Oilers.png",
                    "fla": "Panthers.png", "la": "LAKings.png", "min": "Wild.png",
                    "mtl": "Canadiens.png", "nsh": "Predators.png", "njd": "Devils.png",
                    "nyi": "Islanders.png", "nyr": "Rangers.png", "ott": "Senators.png",
                    "phi": "Flyers.png", "pit": "Penguins.png", "sj": "Sharks.png",
                    "stl": "Blues.png", "tb": "Lightning.png", "tor": "MapleLeafs.png",
                    "van": "Canucks.png", "vgk": "GoldenKnights.png", "wsh": "Capitals.png",
                    "wpg": "Jets.png", "sea": "Kraken.png"
                }
            }
        }
        
        total_loaded = 0
        
        # Load logos for all leagues with unique keys
        base_dir = Path(__file__).resolve().parent
        for league, config in league_configs.items():
            logos_path = base_dir / config["folder"]
            
            if not logos_path.exists():
                continue
            
            for team_id, filename in config["teams"].items():
                logo_path = logos_path / filename
                
                if logo_path.exists():
                    try:
                        image = QImage(str(logo_path))
                        if not image.isNull():
                            image = image.scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio, 
                                               Qt.TransformationMode.SmoothTransformation)
                            
                            # Create league-specific key to avoid conflicts
                            texture_key = f"{league}_{team_id}".lower()

                            # Small copy for QPainter overlay chips
                            self.team_logo_images[texture_key] = image.scaled(
                                18, 18, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
                            # Full-res copy for unfold faces / panel header
                            self.team_logo_images_big[texture_key] = image

                            # Create OpenGL texture
                            texture = QOpenGLTexture(image)
                            texture.setMinificationFilter(QOpenGLTexture.Filter.LinearMipMapLinear)
                            texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
                            texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
                            
                            self.team_logo_textures[texture_key] = texture
                            total_loaded += 1
                            
                    except Exception:
                        pass  # Silent fail for missing logos
        
        logger.info(f"✅ Loaded {total_loaded} team logos")
        
        # Create default marker texture if none exists
        if not self.default_marker_texture:
            self.create_default_marker_texture()

    def get_team_logo_texture(self, team_abbrev: str, league: str):
        """Get team logo texture using league-aware key"""
        texture_key = f"{league}_{team_abbrev}".lower()
        return self.team_logo_textures.get(texture_key, self.default_marker_texture)
    
    def create_default_marker_texture(self):
        """Create a default solid marker texture"""
        size = 128
        image = QImage(size, size, QImage.Format.Format_RGBA8888)
        
        center = size // 2
        for y in range(size):
            for x in range(size):
                dx = x - center
                dy = y - center
                dist = math.sqrt(dx*dx + dy*dy) / center
                
                intensity = max(0.6, min(1.0, 1.0 - dist * 0.3))
                color = QColor(int(180 * intensity), int(180 * intensity), int(200 * intensity), 220)
                image.setPixelColor(x, y, color)
        
        self.default_marker_texture = QOpenGLTexture(image)
        self.default_marker_texture.setMinificationFilter(QOpenGLTexture.Filter.Linear)
        self.default_marker_texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        self.default_marker_texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
    
    # Coordinate Conversion Methods
    def lat_lon_to_3d(self, lat: float, lon: float, radius: float = 1.0) -> Tuple[float, float, float]:
        """Convert latitude/longitude to 3D coordinates"""
        lat_rad = math.radians(lat)
        lon_rad = math.radians(lon)
        
        x = radius * math.cos(lat_rad) * math.cos(lon_rad)
        y = radius * math.sin(lat_rad)
        z = -radius * math.cos(lat_rad) * math.sin(lon_rad)
        
        return (float(x), float(y), float(z))
    
    def generate_great_circle_path(self, start_lat: float, start_lon: float, 
                                 end_lat: float, end_lon: float, num_points: int = 50) -> List[Tuple[float, float, float]]:
        """Generate great circle path between two points"""
        if abs(start_lat - end_lat) < 0.001 and abs(start_lon - end_lon) < 0.001:
            return []
        
        delta_lon = end_lon - start_lon
        if delta_lon > 180:
            end_lon -= 360
        elif delta_lon < -180:
            end_lon += 360
        
        path_points = []
        base_altitude = 1.02
        max_altitude = 1.08
        
        for i in range(num_points + 1):
            t = i / num_points
            lat = start_lat + t * (end_lat - start_lat)
            lon = start_lon + t * (end_lon - start_lon)
            altitude_factor = math.sin(t * math.pi) * (max_altitude - base_altitude) + base_altitude
            point_3d = self.lat_lon_to_3d(lat, lon, altitude_factor)
            path_points.append(point_3d)
        
        return path_points
    
    def get_city_coordinates(self, city_name: str) -> Optional[Tuple[float, float]]:
        """Get latitude/longitude for a city"""
        city_coords = {
            # Major US Cities
            "Los Angeles": (34.0522, -118.2437), "New York": (40.7128, -74.0060),
            "Chicago": (41.8781, -87.6298), "Houston": (29.7604, -95.3698),
            "Phoenix": (33.4484, -112.0740), "Philadelphia": (39.9526, -75.1652),
            "San Antonio": (29.4241, -98.4936), "San Diego": (32.7157, -117.1611),
            "Dallas": (32.7767, -96.7970), "San Francisco": (37.7749, -122.4194),
            "Boston": (42.3601, -71.0589), "Seattle": (47.6062, -122.3321),
            "Denver": (39.7392, -104.9903), "Washington": (38.9072, -77.0369),
            "Las Vegas": (36.1699, -115.1398), "Milwaukee": (43.0389, -87.9065),
            "Atlanta": (33.7490, -84.3880), "Miami": (25.7617, -80.1918),
            "Tampa": (27.9506, -82.4572), "Pittsburgh": (40.4406, -79.9959),
            "Cincinnati": (39.1031, -84.5120), "St. Louis": (38.6270, -90.1994),
            "Minneapolis": (44.9778, -93.2650), "Kansas City": (39.0997, -94.5786),
            "Cleveland": (41.4993, -81.6944), "Detroit": (42.3314, -83.0458),
            "Baltimore": (39.2904, -76.6122), "Charlotte": (35.2271, -80.8431),
            "Nashville": (36.1627, -86.7816), "Portland": (45.5152, -122.6784),
            "Orlando": (28.5383, -81.3792), "Indianapolis": (39.7684, -86.1581),
            "New Orleans": (29.9511, -90.0715), "Sacramento": (38.5816, -121.4944),
            "Oklahoma City": (35.4676, -97.5164), "Salt Lake City": (40.7608, -111.8910),
            "Buffalo": (42.8864, -78.8784), "Raleigh": (35.7796, -78.6382),
            "Anaheim": (33.8366, -117.9143), "Sunrise": (26.1354, -80.2373),
            "Newark": (40.7357, -74.1724),
            
            # Canadian cities
            "Toronto": (43.6532, -79.3832), "Montreal": (45.5017, -73.5673),
            "Vancouver": (49.2827, -123.1207), "Calgary": (51.0447, -114.0719),
            "Edmonton": (53.5461, -113.4938), "Ottawa": (45.4215, -75.6972),
            "Winnipeg": (49.8951, -97.1384)
        }
        return city_coords.get(city_name)
    
    def get_team_color(self, team_id: str) -> Tuple[float, float, float]:
        """Get team color based on team ID"""
        normalized_id = team_id.upper()
        
        team_colors = {
            # MLB teams
            "NYY": (0.1, 0.2, 0.5), "BOS": (0.8, 0.1, 0.2), "TB": (0.0, 0.3, 0.6), 
            "TOR": (0.0, 0.4, 0.8), "BAL": (1.0, 0.3, 0.0), "CLE": (0.8, 0.1, 0.2), 
            "CHW": (0.1, 0.1, 0.1), "DET": (0.0, 0.2, 0.5), "KC": (0.0, 0.3, 0.6), 
            "MIN": (0.0, 0.2, 0.5), "HOU": (1.0, 0.4, 0.0), "SEA": (0.0, 0.4, 0.6), 
            "TEX": (0.8, 0.1, 0.2), "LAA": (0.8, 0.0, 0.2), "ATH": (0.0, 0.5, 0.2), 
            "ATL": (0.7, 0.0, 0.2), "PHI": (0.9, 0.1, 0.2), "NYM": (0.0, 0.3, 0.8), 
            "WSH": (0.7, 0.0, 0.2), "MIA": (0.0, 0.6, 0.8), "STL": (0.8, 0.0, 0.2), 
            "MIL": (0.0, 0.2, 0.5), "CHC": (0.0, 0.2, 0.6), "CIN": (0.8, 0.1, 0.2), 
            "PIT": (1.0, 0.8, 0.0), "LAD": (0.0, 0.4, 0.8), "SF": (1.0, 0.3, 0.0), 
            "SD": (1.0, 0.4, 0.0), "COL": (0.2, 0.1, 0.4), "ARI": (0.6, 0.0, 0.2),
            
            # NBA teams
            "BOS": (0.0, 0.4, 0.2), "BKN": (0.1, 0.1, 0.1), "NY": (0.0, 0.3, 0.8), 
            "PHI": (0.8, 0.1, 0.2), "TOR": (0.8, 0.0, 0.2), "CHI": (0.8, 0.0, 0.0), 
            "CLE": (0.5, 0.0, 0.2), "DET": (0.8, 0.1, 0.2), "IND": (1.0, 0.8, 0.0), 
            "MIL": (0.0, 0.3, 0.2), "ATL": (0.8, 0.1, 0.2), "CHA": (0.0, 0.5, 0.6), 
            "MIA": (0.6, 0.0, 0.2), "ORL": (0.0, 0.3, 0.8), "WSH": (0.8, 0.1, 0.2), 
            "DEN": (1.0, 0.6, 0.0), "MIN": (0.0, 0.2, 0.5), "OKC": (0.0, 0.3, 0.8), 
            "POR": (0.8, 0.1, 0.2), "UTAH": (0.0, 0.2, 0.4), "GS": (1.0, 0.8, 0.0), 
            "LAC": (0.8, 0.1, 0.2), "LAL": (0.3, 0.0, 0.5), "PHX": (1.0, 0.4, 0.0), 
            "SAC": (0.3, 0.0, 0.5), "DAL": (0.0, 0.3, 0.8), "HOU": (0.8, 0.0, 0.0), 
            "MEM": (0.0, 0.3, 0.6), "NO": (1.0, 0.6, 0.0), "SA": (0.1, 0.1, 0.1),
            
            # NHL teams
            "ANA": (1.0, 0.4, 0.0), "ARI": (0.6, 0.0, 0.2), "BOS": (1.0, 0.8, 0.0), 
            "BUF": (0.0, 0.3, 0.8), "CGY": (0.8, 0.0, 0.0), "CAR": (0.8, 0.1, 0.2), 
            "CHI": (0.8, 0.0, 0.0), "COL": (0.5, 0.0, 0.3), "CBJ": (0.0, 0.2, 0.5), 
            "DAL": (0.0, 0.4, 0.2), "DET": (0.8, 0.0, 0.0), "EDM": (0.0, 0.3, 0.8), 
            "FLA": (0.8, 0.1, 0.2), "LA": (0.1, 0.1, 0.1), "MIN": (0.0, 0.4, 0.2), 
            "MTL": (0.8, 0.1, 0.2), "NSH": (1.0, 0.8, 0.0), "NJD": (0.8, 0.0, 0.0), 
            "NYI": (0.0, 0.3, 0.8), "NYR": (0.0, 0.3, 0.8), "OTT": (0.8, 0.0, 0.0), 
            "PHI": (1.0, 0.4, 0.0), "PIT": (1.0, 0.8, 0.0), "SJ": (0.0, 0.4, 0.4), 
            "STL": (0.0, 0.3, 0.8), "TB": (0.0, 0.3, 0.8), "TOR": (0.0, 0.3, 0.8), 
            "VAN": (0.0, 0.3, 0.8), "VGK": (1.0, 0.6, 0.0), "WSH": (0.8, 0.0, 0.0), 
            "WPG": (0.0, 0.2, 0.5), "SEA": (0.0, 0.4, 0.5)
        }
        
        return team_colors.get(normalized_id, (1.0, 0.6, 0.2))
    
    # Animation Methods (Required by UI)
    def start_team_animation(self, team_id: str) -> bool:
        """Start travel animation for a specific team"""
        if not self.travel_data:
            return False
        
        sequence = self.travel_animation.build_team_sequence(team_id, self.travel_data)
        
        if not sequence or not sequence['segments']:
            return False
        
        success = self.travel_animation.start_animation(team_id)
        if success:
            self.show_travel_animation = True
            self.animationStatusChanged.emit(True, team_id)
        
        return success
    
    def stop_team_animation(self):
        """Stop current team animation"""
        self.travel_animation.stop_animation()
        self.show_travel_animation = False
        self.animated_marker_position = None
        self.animationStatusChanged.emit(False, "")
    
    def set_animation_speed(self, speed: float):
        """Set animation speed (0.1 to 5.0)"""
        self.travel_animation.animation_speed = max(0.1, min(5.0, speed))
    
    def set_animation_looping(self, loop: bool):
        """Set whether animation should loop"""
        self.travel_animation.loop_animation = loop
    
    def get_animation_info(self) -> Dict:
        """Get current animation information"""
        if not self.travel_animation.current_team:
            return {}
        
        sequence = self.travel_animation.team_sequences.get(self.travel_animation.current_team)
        if not sequence:
            return {}
        
        return {
            'team_id': self.travel_animation.current_team,
            'active': self.travel_animation.animation_active,
            'progress': self.travel_animation.animation_progress,
            'current_segment': self.travel_animation.current_segment,
            'total_segments': len(sequence['segments']),
            'total_distance': sequence['total_distance'],
            'total_duration': sequence['total_duration'],
            'games_count': sequence['games_count']
        }
    
    def get_available_teams(self) -> List[str]:
        """Get list of teams available for animation"""
        if not self.travel_data:
            return []
        
        teams = set()
        for travel in self.travel_data:
            if hasattr(travel, 'team_id') and travel.team_id:
                teams.add(travel.team_id.upper())
        
        return sorted(list(teams))
    
    def set_spinning_during_animation(self, enabled: bool):
        """Control whether markers spin during animation"""
        self.disable_spinning_during_animation = not enabled
        
    def get_spinning_during_animation(self) -> bool:
        """Check if markers spin during animation"""
        return not self.disable_spinning_during_animation
    
    def refresh_todays_games(self):
        """Refresh today's games display - can be called from UI"""
        self.display_todays_games_startup()

    # Main Data Loading and Visualization
    def load_flight_data(self, travel_data: List):
        """Load sports team travel data"""
        self.travel_data = travel_data
        self.generate_enhanced_visualizations()

        # Add today's games as split cube markers
        self.display_todays_games_startup()

        self.update()
    
    def generate_enhanced_visualizations(self):
        """Generate travel paths and markers from sports data - FIXED for sequential paths"""
        self.travel_paths = []
        self.team_city_markers = []
        cities_seen = set()
        
        # Group travel data by team for consistent colors
        team_travel_groups = {}
        for travel in self.travel_data:
            if not hasattr(travel, 'departure_city') or not hasattr(travel, 'arrival_city'):
                continue
                
            team_id = getattr(travel, 'team_id', 'NO_TEAM_ID')
            team_name = getattr(travel, 'team_name', 'NO_TEAM_NAME')
            
            if team_id not in team_travel_groups:
                team_travel_groups[team_id] = {'team_name': team_name, 'travel_records': []}
            team_travel_groups[team_id]['travel_records'].append(travel)
        
        # Process each team's travel records
        for team_id, team_data in team_travel_groups.items():
            travel_records = team_data['travel_records']
            team_name = team_data['team_name']
            
            # Sort by travel date to get chronological order
            travel_records.sort(key=lambda x: getattr(x, 'travel_date', datetime.min))
            
            # Get team context
            team_abbrev = team_id.upper() if isinstance(team_id, str) else ''
            league = getattr(travel_records[0], 'league', '').upper() if travel_records else ''
            if not league:
                league = self.infer_league_from_team_id(getattr(travel_records[0], 'game_id', '')) or ''
            
            team_color = self.get_team_color(team_abbrev) if team_abbrev else (1.0, 0.6, 0.2)
            
            # *** FIXED: Create ONE CONNECTED PATH per team instead of individual segments ***
            self._create_sequential_team_path(travel_records, team_name, team_abbrev, 
                                            league, team_color, cities_seen)
        
        logger.info(f"✅ Generated {len(self.travel_paths)} travel paths, {len(self.team_city_markers)} city markers")

    def _create_sequential_team_path(self, travel_records, team_name, team_abbrev, 
                                   league, team_color, cities_seen):
        """Create a single connected path for a team's sequential travel"""
        if not travel_records:
            return
        
        # Collect all cities in order for this team
        team_cities = []
        for travel in travel_records:
            departure_city = getattr(travel, 'departure_city', '')
            arrival_city = getattr(travel, 'arrival_city', '')
            
            if departure_city and arrival_city and departure_city != arrival_city:
                # Add departure city if it's the first segment or different from last city
                if not team_cities or team_cities[-1] != departure_city:
                    team_cities.append(departure_city)
                team_cities.append(arrival_city)
        
        # Remove consecutive duplicates (shouldn't happen with venue_id logic, but just in case)
        team_cities = [city for i, city in enumerate(team_cities) 
                      if i == 0 or city != team_cities[i-1]]
        
        if len(team_cities) < 2:
            return
        
        # Create sequential path segments
        all_path_points = []
        
        for i in range(len(team_cities) - 1):
            from_city = team_cities[i]
            to_city = team_cities[i + 1]
            
            # Get coordinates
            dep_coords = self.get_city_coordinates(from_city)
            arr_coords = self.get_city_coordinates(to_city)
            
            if not dep_coords or not arr_coords:
                continue
            
            # Generate path segment
            segment_points = self.generate_great_circle_path(
                dep_coords[0], dep_coords[1], arr_coords[0], arr_coords[1]
            )
            
            if segment_points:
                # Connect segments smoothly (avoid duplicate points at connections)
                if all_path_points and segment_points:
                    segment_points = segment_points[1:]  # Skip first point to avoid duplication
                all_path_points.extend(segment_points)
        
        # Create ONE path for the entire team journey
        if all_path_points and len(all_path_points) > 1:
            path_data = {
                'points': all_path_points,
                'team_name': team_name,
                'route': f"{team_name} Season Path ({len(team_cities)} cities)",
                'color': team_color,
                'alpha': 0.85,
                'travel_date': getattr(travel_records[0], 'travel_date', None)
            }
            self.travel_paths.append(path_data)
        
        # Add city markers for all cities visited by this team
        for city in set(team_cities):  # Use set to avoid duplicates
            coords = self.get_city_coordinates(city)
            if coords:
                city_key = f"{city}_{league}_{team_abbrev}"
                if city_key not in cities_seen:
                    cities_seen.add(city_key)
                    city_3d = self.lat_lon_to_3d(coords[0], coords[1], 1.05)
                    
                    self.team_city_markers.append({
                        'position': city_3d,
                        'team_id': team_abbrev,
                        'league': league,
                        'size': 4.0,
                        'city_name': city,
                        'type': 'travel_point'
                    })
    
    # Today's Games Methods (for split cube markers)
    def get_todays_games(self):
        """Get today's games for split cube display"""
        from datetime import date
        todays_games = []
        
        if not self.travel_data:
            return todays_games
        
        today = date.today()
        
        # Group travel records by game_id to find games happening today
        game_groups = {}
        for travel in self.travel_data:
            if hasattr(travel, 'game_date') and travel.game_date:
                game_date = travel.game_date.date() if hasattr(travel.game_date, 'date') else travel.game_date
                if game_date == today:
                    game_id = getattr(travel, 'game_id', '')
                    if game_id not in game_groups:
                        game_groups[game_id] = []
                    game_groups[game_id].append(travel)
        
        # Extract game information
        for game_id, travel_records in game_groups.items():
            if travel_records:
                # Get venue city from first record
                venue_city = getattr(travel_records[0], 'arrival_city', '')
                
                # Extract team information from game_id pattern like MLB_2025_20250607_bos_nyy
                parts = game_id.split('_')
                if len(parts) >= 5:
                    league = parts[0].upper()
                    away_team = parts[3].upper()
                    home_team = parts[4].upper()
                    
                    todays_games.append({
                        'game_id': game_id,
                        'league': league,
                        'home_team': home_team,
                        'away_team': away_team,
                        'venue_city': venue_city,
                        'game_date': travel_records[0].game_date
                    })
        
        return todays_games
    
    def display_todays_games_startup(self):
        """Display today's games as split cube markers on startup"""
        todays_games = self.get_todays_games()
        
        if not todays_games:
            return
        
        logger.debug(f"🏈 Found {len(todays_games)} games today!")
        
        # Add split cube markers for today's games
        for game in todays_games:
            venue_city = game['venue_city']
            coords = self.get_city_coordinates(venue_city)
            
            if coords:
                city_3d = self.lat_lon_to_3d(coords[0], coords[1], 1.05)
                
                # Create split cube marker
                split_marker = {
                    'position': city_3d,
                    'home_team_id': game['home_team'],
                    'away_team_id': game['away_team'],
                    'league': game['league'],
                    'size': 6.0,
                    'city_name': venue_city,
                    'type': 'todays_game',
                    'is_split_cube': True,
                    'game_id': game['game_id']
                }
                
                # Add to markers (replace any existing marker for this location)
                city_key = f"{venue_city}_{game['league']}_today"
                
                # Remove any existing markers for this city today
                self.team_city_markers = [m for m in self.team_city_markers 
                                        if not (m.get('city_name') == venue_city and m.get('type') == 'todays_game')]
                
                self.team_city_markers.append(split_marker)
                logger.debug(f"   🏟️  {game['away_team']} @ {game['home_team']} in {venue_city}")
        
        self.update()

    def _create_path_and_markers(self, from_city, to_city, team_name, team_abbrev, 
                               league, team_color, travel, cities_seen, alpha=0.85):
        """Create travel path and city markers between two cities"""
        if from_city == to_city:
            return
        
        # Get coordinates
        dep_coords = self.get_city_coordinates(from_city)
        arr_coords = self.get_city_coordinates(to_city)
        
        if not dep_coords or not arr_coords:
            return
        
        # Create path
        path_points = self.generate_great_circle_path(dep_coords[0], dep_coords[1], 
                                                    arr_coords[0], arr_coords[1])
        if path_points and len(path_points) > 0:
            path_data = {
                'points': path_points,
                'team_name': team_name,
                'route': f"{from_city} → {to_city}",
                'color': team_color,
                'alpha': alpha,
                'travel_date': getattr(travel, 'travel_date', None)
            }
            self.travel_paths.append(path_data)
        
        # Add city markers (only once per city per team)
        for city, coords in [(from_city, dep_coords), (to_city, arr_coords)]:
            city_key = f"{city}_{league}_{team_abbrev}"
            if city_key not in cities_seen:
                cities_seen.add(city_key)
                city_3d = self.lat_lon_to_3d(coords[0], coords[1], 1.05)
                
                self.team_city_markers.append({
                    'position': city_3d,
                    'team_id': team_abbrev,
                    'league': league,
                    'size': 4.0,
                    'city_name': city,
                    'type': 'travel_point'
                })
    
    # Rendering Methods
    def paintGL(self):
        """Main rendering method with animation support"""
        if not self.gl_initialized:
            return
        
        current_time = time.time()
        frame_time = current_time - self.last_frame_time
        self.last_frame_time = current_time
        self.animation_time += frame_time

        # Advance the cube-unfold animation
        if self._unfold is not None:
            step = frame_time / 0.9  # full unfold ~0.9s
            if self._unfold['phase'] == 'closing':
                self._unfold['t'] -= step
                if self._unfold['t'] <= 0.0:
                    self._unfold = None
            else:
                self._unfold['t'] = min(1.0, self._unfold['t'] + step)

        self._fps_frame_count += 1
        if current_time - self._fps_last_emit >= 1.0:
            self.fpsUpdated.emit(self._fps_frame_count / (current_time - self._fps_last_emit))
            self._fps_frame_count = 0
            self._fps_last_emit = current_time

        # Update travel animation
        if self.show_travel_animation:
            animation_state = self.travel_animation.update_animation(frame_time)
            if animation_state:
                self.animated_marker_position = animation_state
                self.animationProgressChanged.emit(animation_state['progress'], animation_state['segment'])
                # Feed position to contrail manager
                if self.show_contrails and animation_state.get('position'):
                    self.contrail_manager.add_position(animation_state['position'], self.animation_time)
            else:
                self.animated_marker_position = None
        else:
            # Clear contrails when animation stops
            if self.contrail_manager.has_data():
                self.contrail_manager.clear()
        
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)

        if (not self.is_grabbed) and self.passive_spin:
            self.update_rotation()
        
        if (not self.is_grabbed) and self.rotation_momentum.length() > 0.01:
            self.decay_momentum()
        
        model = self.rotation_matrix
        camera_pos = QVector3D(0, 0, self.camera_distance())
        view = QMatrix4x4()
        view.lookAt(camera_pos, QVector3D(0, 0, 0), QVector3D(0, 1, 0))
        projection = QMatrix4x4()
        aspect_ratio = self.width() / max(self.height(), 1)
        projection.perspective(45.0, aspect_ratio, 0.1, 100.0)
        mvp = projection * view * model

        normal_matrix = model.normalMatrix()

        self.render_starfield(projection, view)
        self.render_earth(mvp, model, normal_matrix, camera_pos)

        # Render travel visualization
        if self.show_travel_paths:
            self.render_travel_paths(mvp, model, camera_pos)

        if self.show_team_cities:
            self.render_markers(mvp, model)

        # Render contrails behind animated planes
        if self.show_contrails and self.show_travel_animation:
            self.render_contrails(mvp)

        # Render live flights (always on top)
        self.render_flight_routes(mvp, model, camera_pos)
        if self.live_flights:
            self.render_live_flights(mvp)
            # Render 2D labels for live flights
            self.render_flight_labels(mvp)

        # 2D marker overlays: cluster badges, live-score chips, fatigue rings
        if self.show_team_cities and not self.show_travel_animation:
            self.render_marker_overlays(mvp)

    def render_flight_labels(self, mvp: QMatrix4x4):
        """Render 2D team labels above live flight planes"""
        if not self.live_flights:
            return

        from PyQt6.QtCore import Qt, QRect
        from PyQt6.QtGui import (QPainter, QColor, QFont, QPen, QBrush,
                                 QLinearGradient)

        # Same GL-state caveat as render_marker_overlays
        self._prepare_gl_for_qpainter()

        # Begin QPainter for 2D overlay
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        title_font = QFont("Arial", 9, QFont.Weight.Bold)
        small_font = QFont("Arial", 8)
        placed_rects = []

        # Stable iteration order (by icao24) + remembered slots keep each chip
        # parked in the same spot relative to its plane across frames
        self._flight_label_slots = {
            k: v for k, v in self._flight_label_slots.items()
            if k in self.live_flights}

        for icao24, flight_data in sorted(self.live_flights.items()):
            try:
                lat = flight_data['latitude']
                lon = flight_data['longitude']
                altitude_ft = flight_data.get('altitude_ft', 35000)
                team_id = flight_data.get('team_id')
                confidence = flight_data.get('confidence', 50)
                dest_city = flight_data.get('dest_city')
                progress = flight_data.get('route_progress')

                # Position above the plane
                altitude_scale = 1.0 + (altitude_ft / 40000.0) * 0.12
                pos_3d = self.lat_lon_to_3d(lat, lon, altitude_scale + 0.02)
                # Cull labels for planes on the far side of the globe —
                # without this they project anyway and pin to the screen edge
                if not self._marker_front_facing(pos_3d):
                    continue
                screen_pos = self.project_to_screen(pos_3d, mvp)
                if screen_pos is None:
                    continue
                x, y = screen_pos
                if x < 0 or x > self.width() or y < 0 or y > self.height():
                    continue

                # Team look-and-feel (watchlist flights may have no team)
                if team_id:
                    league = (flight_data.get('league')
                              or self.infer_league_from_team_id(team_id) or '')
                    logo = self.team_logo_images.get(
                        f"{league.lower()}_{team_id.lower()}")
                    tc = self.get_team_color(team_id.upper())
                    accent = QColor(int(tc[0] * 255), int(tc[1] * 255), int(tc[2] * 255))
                    title = team_id.upper()
                else:
                    logo = None
                    accent = QColor(150, 180, 220)
                    title = (flight_data.get('label')
                             or flight_data.get('callsign') or icao24).strip()

                if dest_city:
                    title += f"  →  {dest_city}"

                painter.setFont(title_font)
                title_w = painter.fontMetrics().horizontalAdvance(title)

                logo_w = 18 if logo is not None else 0
                pad = 7
                gap = 5 if logo is not None else 0
                w = max(86, pad + logo_w + gap + title_w + pad)
                h = 36 if progress is not None else 26
                px, py = int(x), int(y)

                # Anchored placement: fixed candidate slots around the plane,
                # tried in priority order; the chosen slot is remembered per
                # flight so chips stay parked instead of reshuffling
                def slot_rect(slot):
                    return {
                        0: QRect(px - w // 2, py - h - 20, w, h),       # above
                        1: QRect(px + 22, py - h // 2, w, h),           # right
                        2: QRect(px - w - 22, py - h // 2, w, h),       # left
                        3: QRect(px - w // 2, py + 22, w, h),           # below
                        4: QRect(px - w // 2, py - 2 * h - 30, w, h),   # high above
                        5: QRect(px + 22, py - 3 * h // 2 - 12, w, h),  # right-up
                        6: QRect(px - w - 22, py - 3 * h // 2 - 12, w, h),
                        7: QRect(px - w // 2, py + h + 32, w, h),       # low below
                    }[slot]

                def collides(r):
                    pad_r = r.adjusted(-3, -3, 3, 3)
                    return any(pad_r.intersects(o) for o in placed_rects)

                remembered = self._flight_label_slots.get(icao24, 0)
                chosen = None
                for slot in [remembered] + [s for s in range(8) if s != remembered]:
                    r = slot_rect(slot)
                    if not collides(r):
                        chosen = slot
                        rect = r
                        break
                if chosen is None:  # everything collides; keep remembered slot
                    chosen = remembered
                    rect = slot_rect(chosen)
                self._flight_label_slots[icao24] = chosen

                # Keep on screen
                rect.moveLeft(max(4, min(rect.left(), self.width() - w - 4)))
                rect.moveTop(max(4, min(rect.top(), self.height() - h - 4)))
                placed_rects.append(rect)
                cx, cy = rect.left(), rect.top()

                # Soft pulsing glow halo around the plane (matches the model's
                # brightness throb, desynced per aircraft)
                from PyQt6.QtGui import QRadialGradient
                phase = (sum(map(ord, icao24)) % 628) / 100.0
                pulse = max(0.0, math.sin(self.animation_time * 2.2 + phase))
                glow_r = 14 + 5 * pulse
                glow = QRadialGradient(px, py, glow_r)
                gc = QColor(accent)
                gc.setAlpha(int(55 + 60 * pulse))
                glow.setColorAt(0.0, gc)
                gc2 = QColor(accent)
                gc2.setAlpha(0)
                glow.setColorAt(1.0, gc2)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(glow))
                painter.drawEllipse(int(px - glow_r), int(py - glow_r),
                                    int(2 * glow_r), int(2 * glow_r))

                # Leader line from plane to chip (drawn first, card covers
                # its far end) with a small anchor dot at the plane
                line_color = QColor(accent)
                line_color.setAlpha(150)
                painter.setPen(QPen(line_color, 1))
                painter.drawLine(px, py, rect.center().x(), rect.center().y())
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(line_color))
                painter.drawEllipse(px - 2, py - 2, 5, 5)

                # Card
                grad = QLinearGradient(cx, cy, cx, cy + h)
                grad.setColorAt(0.0, QColor(22, 34, 56, 240))
                grad.setColorAt(1.0, QColor(10, 16, 30, 240))
                painter.setBrush(QBrush(grad))
                border = QColor(accent)
                border.setAlpha(190)
                painter.setPen(QPen(border, 1.2))
                painter.drawRoundedRect(cx, cy, w, h, 6, 6)

                # Logo well + title row
                tx = cx + pad
                if logo is not None:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(QColor(233, 237, 243, 240)))
                    painter.drawRoundedRect(tx - 1, cy + 4, logo_w + 2, logo_w + 2, 4, 4)
                    painter.drawImage(tx + (logo_w - logo.width()) // 2,
                                      cy + 5 + (logo_w - logo.height()) // 2, logo)
                    tx += logo_w + gap
                painter.setFont(title_font)
                painter.setPen(QColor(245, 250, 255))
                painter.drawText(QRect(tx, cy, w - (tx - cx) - pad, 26),
                                 Qt.AlignmentFlag.AlignVCenter, title)

                # Route progress bar with confidence dot
                if progress is not None:
                    bar_x, bar_y = cx + pad + 10, cy + 28
                    bar_w = w - 2 * pad - 10
                    conf_color = QColor(60, 220, 110) if confidence >= 80 \
                        else QColor(235, 190, 60)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(conf_color))
                    painter.drawEllipse(cx + pad, bar_y - 1, 5, 5)

                    painter.setBrush(QBrush(QColor(255, 255, 255, 45)))
                    painter.drawRoundedRect(bar_x, bar_y, bar_w, 3, 1, 1)
                    fill = QColor(accent)
                    fill.setAlpha(230)
                    painter.setBrush(QBrush(fill))
                    painter.drawRoundedRect(bar_x, bar_y,
                                            max(4, int(bar_w * progress)), 3, 1, 1)
                    # position tick
                    painter.setBrush(QBrush(QColor(255, 255, 255, 230)))
                    painter.drawEllipse(bar_x + int(bar_w * progress) - 2,
                                        bar_y - 1, 5, 5)

            except Exception:
                continue

        painter.end()
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glEnable(gl.GL_DEPTH_TEST)

    def project_to_screen(self, pos_3d: tuple, mvp: QMatrix4x4) -> tuple:
        """Project 3D world position to 2D screen coordinates"""
        from PyQt6.QtGui import QVector4D

        # Convert to homogeneous coordinates
        pos_4d = QVector4D(pos_3d[0], pos_3d[1], pos_3d[2], 1.0)

        # Apply MVP transformation
        clip = mvp * pos_4d

        # Check if behind camera
        if clip.w() <= 0:
            return None

        # Perspective divide to get NDC
        ndc_x = clip.x() / clip.w()
        ndc_y = clip.y() / clip.w()
        ndc_z = clip.z() / clip.w()

        # Check if in front of camera (NDC z should be in [-1, 1])
        if ndc_z < -1 or ndc_z > 1:
            return None

        # Convert NDC to screen coordinates
        screen_x = (ndc_x + 1) * 0.5 * self.width()
        screen_y = (1 - ndc_y) * 0.5 * self.height()  # Y is flipped

        return (screen_x, screen_y)

    def calculate_sun_direction(self) -> QVector3D:
        """Calculate sun direction based on current UTC time for day/night cycle"""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        day_of_year = now.timetuple().tm_yday
        hour = now.hour + now.minute / 60.0 + now.second / 3600.0

        # Solar declination angle (varies -23.45 to +23.45 degrees over the year)
        declination = -23.45 * math.cos(math.radians(360.0 / 365.0 * (day_of_year + 10)))

        # Hour angle: sun moves 15 degrees per hour, solar noon at 0 degrees longitude
        hour_angle = (hour - 12) * 15

        # Convert to 3D direction vector
        dec_rad = math.radians(declination)
        ha_rad = math.radians(hour_angle)

        # Sun direction in EARTH frame (same lat/lon convention as the sphere
        # geometry: x=cos(lat)cos(lon), z=-cos(lat)sin(lon)). Solar noon sits
        # at longitude -hour_angle, so the sun points at (dec, -ha); the old
        # +ha sign put noon on the wrong side of Greenwich.
        x = math.cos(dec_rad) * math.cos(ha_rad)
        y = math.sin(dec_rad)
        z = math.cos(dec_rad) * math.sin(ha_rad)

        return QVector3D(x, y, z).normalized()

    def render_starfield(self, projection: QMatrix4x4, view: QMatrix4x4):
        """Fullscreen hash-star background, drawn before the earth."""
        if not self.stars_shader:
            return
        if self._stars_vao is None:
            self._stars_vao = gl.glGenVertexArrays(1)

        inv_vp, ok = (projection * view).inverted()
        if not ok:
            return

        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_FALSE)
        self.stars_shader.bind()
        self.stars_shader.setUniformValue("invVP", inv_vp)
        self.stars_shader.setUniformValue("time", self.animation_time)
        gl.glBindVertexArray(self._stars_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glBindVertexArray(0)
        self.stars_shader.release()
        gl.glDepthMask(gl.GL_TRUE)
        gl.glEnable(gl.GL_DEPTH_TEST)

    def render_earth(self, mvp, model, normal_matrix, camera_pos=None):
        """Render Earth sphere with day/night cycle"""
        if not self.shader_program or not self.vao:
            return

        self.shader_program.bind()

        self.shader_program.setUniformValue("mvp", mvp)
        self.shader_program.setUniformValue("model", model)
        self.shader_program.setUniformValue("normalMatrix", normal_matrix)

        # Day/night cycle uniforms (sun direction also drives city lights).
        # The sun is computed in the earth frame and rotated by the model
        # matrix so the terminator sticks to geography while the globe spins —
        # the night side is genuinely "where it's night right now".
        self.shader_program.setUniformValue("showDayNight", self.show_day_night)
        sun_dir = model.mapVector(self.calculate_sun_direction()).normalized()
        self.shader_program.setUniformValue("sunDirection", sun_dir)
        self.shader_program.setUniformValue("terminatorWidth", self.terminator_width)

        # Fallback light direction for non-day/night mode
        light_direction = QVector3D(1.0, 0.3, 0.5).normalized()
        self.shader_program.setUniformValue("lightDir", light_direction)
        self.shader_program.setUniformValue("showAtmosphere", self.show_atmosphere)
        self.shader_program.setUniformValue(
            "cameraPos", camera_pos if camera_pos is not None else QVector3D(0, 0, 3))

        if self.earth_texture:
            self.earth_texture.bind(0)
            self.shader_program.setUniformValue("earthTexture", 0)

        has_night = self.night_texture is not None
        self.shader_program.setUniformValue("hasNightTexture", has_night)
        if has_night:
            self.night_texture.bind(1)
            self.shader_program.setUniformValue("nightTexture", 1)

        gl.glBindVertexArray(self.vao)
        gl.glDrawElements(gl.GL_TRIANGLES, len(self.indices), gl.GL_UNSIGNED_INT, None)

        self.shader_program.release()
    
    def render_travel_paths(self, mvp, model=None, camera_pos=None):
        """Render travel paths (with horizon fade on the globe's far side)"""
        if not self.travel_paths or not self.travel_shader:
            return

        gl.glEnable(gl.GL_LINE_SMOOTH)
        gl.glLineWidth(2.5)
        # Depth test off: far-side segments are alpha-faded by the shader's
        # horizon test instead of being hard-clipped by the sphere
        gl.glDisable(gl.GL_DEPTH_TEST)

        self.travel_shader.bind()
        self.travel_shader.setUniformValue("mvp", mvp)
        self.travel_shader.setUniformValue(
            "model", model if model is not None else QMatrix4x4())
        self.travel_shader.setUniformValue(
            "cameraPos", camera_pos if camera_pos is not None else QVector3D(0, 0, 3))

        # Per-path buffers are cached across frames; the old code created and
        # destroyed a VAO+VBO per path per frame.
        self._ensure_path_buffers()
        for (vao, _vbo, count, color, alpha) in self._path_buffers:
            self.travel_shader.setUniformValue("pathColor", QVector3D(*color))
            self.travel_shader.setUniformValue("pathAlpha", alpha)
            gl.glBindVertexArray(vao)
            gl.glDrawArrays(gl.GL_LINE_STRIP, 0, count)
        gl.glBindVertexArray(0)

        self.travel_shader.release()
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_LINE_SMOOTH)

    def _ensure_path_buffers(self):
        """(Re)build cached GL buffers when the travel path set changes."""
        key = (id(self.travel_paths), len(self.travel_paths))
        if key == self._path_buffer_key:
            return
        self._release_path_buffers()

        for path_data in self.travel_paths:
            points = path_data.get('points', [])
            if len(points) < 2:
                continue
            color = path_data.get('color', (1.0, 0.8, 0.2))
            alpha = path_data.get('alpha', 0.9)

            path_array = np.array(points, dtype=np.float32).flatten()
            vao = gl.glGenVertexArrays(1)
            vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, path_array.nbytes, path_array,
                            gl.GL_STATIC_DRAW)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            self._path_buffers.append((vao, vbo, len(points), color, alpha))

        gl.glBindVertexArray(0)
        self._path_buffer_key = key

    def _release_path_buffers(self):
        for (vao, vbo, _count, _color, _alpha) in self._path_buffers:
            try:
                gl.glDeleteBuffers(1, [vbo])
                gl.glDeleteVertexArrays(1, [vao])
            except Exception:
                pass
        self._path_buffers = []
        self._path_buffer_key = None

    def render_contrails(self, mvp):
        """Render contrail/wake effects behind animated planes"""
        if not self.show_contrails or not self.contrail_shader:
            return
        if not self.contrail_manager.has_data():
            return

        data = self.contrail_manager.get_render_data(self.animation_time)
        if data is None or len(data) == 0:
            return

        # Setup blending for additive glow effect
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE)
        gl.glEnable(gl.GL_PROGRAM_POINT_SIZE)
        gl.glDisable(gl.GL_DEPTH_TEST)  # Contrails visible through globe

        self.contrail_shader.bind()
        self.contrail_shader.setUniformValue("mvp", mvp)
        self.contrail_shader.setUniformValue("maxAge", self.contrail_manager.max_age)
        self.contrail_shader.setUniformValue("contrailColor", QVector3D(0.85, 0.9, 1.0))

        # Create dynamic VAO/VBO for contrail points
        vao = gl.glGenVertexArrays(1)
        vbo = gl.glGenBuffers(1)

        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_DYNAMIC_DRAW)

        stride = 4 * 4  # 4 floats per vertex (x, y, z, age)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(0))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(1, 1, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(12))

        gl.glDrawArrays(gl.GL_POINTS, 0, len(data) // 4)

        # Cleanup
        gl.glDeleteBuffers(1, [vbo])
        gl.glDeleteVertexArrays(1, [vao])

        self.contrail_shader.release()
        gl.glDisable(gl.GL_PROGRAM_POINT_SIZE)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

    def render_markers(self, mvp, model):
        """Render team markers with airplane for animation"""
        
        # Render animated airplane FIRST, separate from marker shader
        if self.show_travel_animation and self.animated_marker_position:
            if self.plane_model.is_ready():
                self.plane_model.render_animated_airplane(self.animated_marker_position, mvp, None)
        
        # Now render cube markers with their own shader
        if not self.marker_shader:
            return
        
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_DEPTH_TEST)
        
        self.marker_shader.bind()
        self.marker_shader.setUniformValue("mvp", mvp)
        self.marker_shader.setUniformValue("time", self.animation_time)

        # Render static markers only when NOT showing travel animation
        if self.team_city_markers and not self.show_travel_animation:
            if self._lod == self.LOD_FAR:
                # Far tier: collapse to small cluster dots (logos unreadable)
                for cluster in self._get_marker_clusters():
                    self.render_cluster_marker(cluster)
            else:
                # Near tier shrinks markers so they keep a sane screen size
                size_scale = 1.0 if self._lod == self.LOD_MID \
                    else max(2.2 / self.zoom_level, 0.35)
                unfold_key = self._unfold['key'] if self._unfold else None
                for marker in self.team_city_markers:
                    # The unfolding cube is drawn as a 2D net in the overlay
                    # pass — skip its GL cube
                    if (unfold_key is not None and marker.get('game_info')
                            and self._marker_game_key(marker) == unfold_key):
                        continue
                    marker_type = marker.get('type', 'generic')

                    if marker_type in ['home_today', 'away_today']:
                        rotation_speed = 0.3
                    else:
                        rotation_speed = 1.0

                    self.render_single_marker(marker, rotation_speed, size_scale)

        self.marker_shader.release()

    LEAGUE_COLORS = {
        'MLB': (0.85, 0.30, 0.25),
        'NBA': (0.95, 0.55, 0.15),
        'NHL': (0.35, 0.60, 0.95),
    }

    def _get_marker_clusters(self):
        """Greedy angular clustering of markers for the far LOD tier."""
        key = (id(self.team_city_markers), len(self.team_city_markers))
        if key == self._cluster_cache_key:
            return self._cluster_cache

        clusters = []
        threshold = 0.10  # radians between cluster centroid and marker (~6°)
        for marker in self.team_city_markers:
            pos = marker.get('position')
            if not pos:
                continue
            v = QVector3D(*pos).normalized()
            placed = False
            for cluster in clusters:
                if math.acos(max(-1.0, min(1.0,
                        QVector3D.dotProduct(v, cluster['dir'])))) < threshold:
                    cluster['markers'].append(marker)
                    cluster['has_live'] = cluster['has_live'] or ('live' in marker)
                    placed = True
                    break
            if not placed:
                p = v * 1.03
                clusters.append({
                    'dir': v,
                    'position': (p.x(), p.y(), p.z()),
                    'league': marker.get('league', ''),
                    'markers': [marker],
                    'has_live': 'live' in marker,
                })

        self._cluster_cache_key = key
        self._cluster_cache = clusters
        return clusters

    def invalidate_marker_clusters(self):
        """Force cluster rebuild (markers were mutated in place, e.g. live
        score stamping)."""
        self._cluster_cache_key = None

    def render_cluster_marker(self, cluster):
        """Small untextured dot-cube for a far-tier cluster."""
        if not self.marker_vao:
            return
        count = len(cluster['markers'])
        color = self.LEAGUE_COLORS.get((cluster['league'] or '').upper(),
                                       (0.9, 0.8, 0.3))
        if cluster['has_live']:
            color = (0.25, 0.95, 0.45)  # live games pop green

        self.marker_shader.setUniformValue("isSplitCube", False)
        self.marker_shader.setUniformValue("isAnimated", False)
        self.marker_shader.setUniformValue("useTexture", False)
        self.marker_shader.setUniformValue("useHomeTexture", False)
        self.marker_shader.setUniformValue("useAwayTexture", False)
        self.marker_shader.setUniformValue("teamColor", QVector3D(*color))
        self.marker_shader.setUniformValue("markerCenter", QVector3D(*cluster['position']))
        self.marker_shader.setUniformValue("markerSize", min(0.55 + 0.10 * count, 1.0))
        self.marker_shader.setUniformValue("rotationSpeed", 0.15)
        self.marker_shader.setUniformValue("markerAlpha", 0.95)
        self.marker_shader.setUniformValue("time", self.animation_time)

        gl.glBindVertexArray(self.marker_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
        gl.glBindVertexArray(0)

    @staticmethod
    def _prepare_gl_for_qpainter():
        """Reset the GL state Qt's paint engine assumes but does not set.

        Two verified failure modes when this is skipped:
        - GL_CULL_FACE enabled -> QPainter fills (rects/images/gradients)
          silently vanish while text still renders.
        - A leftover texture bound on the active unit (e.g. the last marker
          cube's team logo) -> QPainter samples OUR texture for its image and
          gradient draws, painting the wrong logo / striped backgrounds.
        """
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glUseProgram(0)
        gl.glBindVertexArray(0)

    def _marker_front_facing(self, pos) -> bool:
        """True when a model-space marker is on the camera-facing hemisphere.
        project_to_screen alone happily projects far-side points."""
        world = self.rotation_matrix.map(QVector3D(*pos)).normalized()
        return world.z() > (1.0 / self.camera_distance()) - 0.05

    def render_marker_overlays(self, mvp: QMatrix4x4):
        """2D overlay pass: cluster count badges (far) and live-score chips
        (mid/near), drawn with QPainter like the flight labels."""
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import (QPainter, QColor, QFont, QPen, QBrush,
                                 QLinearGradient)

        far_clusters = []
        if self._lod == self.LOD_FAR:
            far_clusters = [c for c in self._get_marker_clusters()
                            if len(c['markers']) > 1]

        # Game card shows only for an explicitly clicked game cube. Resolved
        # by stable game key so it survives marker rebuilds (refreshes/live
        # stamping); cleared only when the game leaves the board entirely.
        focus_marker = None
        if self._selected_game_key is not None:
            focus_marker = next(
                (m for m in self.team_city_markers
                 if m.get('game_info')
                 and self._marker_game_key(m) == self._selected_game_key), None)
            if focus_marker is None:
                self._selected_game_key = None
            elif not self._marker_front_facing(focus_marker['position']):
                focus_marker = None  # rotated away; key kept for when it returns

        unfold_key = self._unfold['key'] if self._unfold else None

        def _is_unfolding(m):
            return (unfold_key is not None and m.get('game_info')
                    and self._marker_game_key(m) == unfold_key)

        live_markers = [] if self._lod == self.LOD_FAR else \
            [m for m in self.team_city_markers
             if m.get('live') and m is not focus_marker
             and not _is_unfolding(m)]
        fatigue_markers = [] if self._lod != self.LOD_NEAR else \
            [m for m in self.team_city_markers
             if m.get('fatigue_max', 0) >= 40 and not m.get('live')
             and m is not focus_marker and not _is_unfolding(m)]

        if not (far_clusters or live_markers or fatigue_markers
                or focus_marker or self._unfold):
            return

        self._prepare_gl_for_qpainter()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Cluster count badges
        painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        for cluster in far_clusters:
            if not self._marker_front_facing(cluster['position']):
                continue
            screen = self.project_to_screen(cluster['position'], mvp)
            if not screen:
                continue
            x, y = screen
            painter.setBrush(QBrush(QColor(15, 23, 42, 220)))
            painter.setPen(QPen(QColor(100, 150, 200, 200), 1))
            painter.drawEllipse(int(x) + 8, int(y) - 18, 18, 18)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(int(x) + 8, int(y) - 18, 18, 18,
                             Qt.AlignmentFlag.AlignCenter, str(len(cluster['markers'])))

        # Live score chips: [away logo] 3–0 [home logo] · Bot 3rd. Placement
        # mirrors the flight labels: fixed candidate slots around the marker
        # tried in priority order, viewport-clamped BEFORE collision testing,
        # remembered slot per game so chips stay parked across frames. The
        # marker cubes themselves are obstacles so chips don't sit on them.
        score_font = QFont("Arial", 10, QFont.Weight.Bold)
        progress_font = QFont("Arial", 8, QFont.Weight.DemiBold)
        placed_rects = []
        projected = []
        for marker in live_markers:
            if not self._marker_front_facing(marker['position']):
                continue
            screen = self.project_to_screen(marker['position'], mvp)
            if screen:
                projected.append((screen[0], screen[1], marker))

        # Drop placement state for games no longer on the board
        keys_now = {self._marker_game_key(m) for _, _, m in projected}
        self._score_chip_state = {k: v for k, v in self._score_chip_state.items()
                                  if k in keys_now}

        # Layout is re-SOLVED at most ~once a second, or when the set of
        # visible games changes. Between solves chips keep their slot and
        # ride rigidly on their anchors — nothing else can move, so nothing
        # can jitter. (Solving every frame against continuously-moving
        # anchors made chips reshuffle endlessly during globe rotation:
        # each chip's fix invalidated a neighbor's spot and the system
        # never settled.)
        do_solve = (keys_now != self._chip_layout_keys
                    or (self.animation_time - self._chip_layout_t) >= 1.0
                    or self.animation_time < self._chip_layout_t)
        if do_solve:
            self._chip_layout_t = self.animation_time
            self._chip_layout_keys = set(keys_now)
            # Logo cubes as obstacles (own and neighbors')
            for sx, sy, _m in projected:
                placed_rects.append(QRect(int(sx) - 14, int(sy) - 14, 28, 28))

        # Stable iteration order so slot assignment doesn't reshuffle between
        # frames as the globe drifts
        for sx, sy, marker in sorted(
                projected, key=lambda t: str(self._marker_game_key(t[2]))):
            x, y = int(sx), int(sy)
            live = marker['live']
            league = (marker.get('league') or '').lower()

            away_img = self.team_logo_images.get(f"{league}_{live.get('away_id', '')}")
            home_img = self.team_logo_images.get(f"{league}_{live.get('home_id', '')}")
            score_text = f"{live.get('away_score', '?')}–{live.get('home_score', '?')}"
            progress = live.get('progress') or ''
            if progress.upper() == 'LIVE':
                progress = ''

            painter.setFont(score_font)
            score_w = painter.fontMetrics().horizontalAdvance(score_text)
            painter.setFont(progress_font)
            prog_w = painter.fontMetrics().horizontalAdvance(progress) if progress else 0

            logo_w = 18
            pad = 7
            gap = 5
            w = pad + logo_w + gap + score_w + gap + logo_w + \
                ((gap + 3 + gap + prog_w) if progress else 0) + pad
            h = 26

            def slot_rect(slot):
                return {
                    0: QRect(x - w // 2, y - h - 22, w, h),        # above
                    1: QRect(x + 20, y - h // 2, w, h),            # right
                    2: QRect(x - w - 20, y - h // 2, w, h),        # left
                    3: QRect(x - w // 2, y + 22, w, h),            # below
                    4: QRect(x - w // 2, y - 2 * h - 30, w, h),    # high above
                    5: QRect(x + 20, y - 3 * h // 2 - 14, w, h),   # right-up
                    6: QRect(x - w - 20, y - 3 * h // 2 - 14, w, h),  # left-up
                    7: QRect(x - w // 2, y + h + 32, w, h),        # low below
                }[slot]

            def clamp(r):
                r = QRect(r)
                r.moveLeft(max(4, min(r.left(), self.width() - w - 4)))
                r.moveTop(max(4, min(r.top(), self.height() - h - 4)))
                return r

            def collides(r):
                pad_r = r.adjusted(-3, -3, 3, 3)
                return any(pad_r.intersects(o) for o in placed_rects)

            def first_free_slot(prefer):
                for slot in [prefer] + [s for s in range(8) if s != prefer]:
                    r = clamp(slot_rect(slot))
                    if not collides(r):
                        return slot, r
                return None, None

            def stacked_rect(base_slot):
                # All slots taken (dense slate): stack upward from the base
                # slot until a free row appears
                base = clamp(slot_rect(base_slot))
                for k in range(1, 9):
                    r = QRect(base)
                    r.moveTop(base.top() - k * (h + 6))
                    if r.top() < 4:
                        break
                    if not collides(r):
                        return r
                return base

            game_key = self._marker_game_key(marker)
            st = self._score_chip_state.get(game_key)
            if st is None:
                st = {'slot': 0, 'off': None}
                self._score_chip_state[game_key] = st

            if do_solve:
                # Full assignment, preferring the current slot so a stable
                # configuration is kept verbatim across solves
                slot, rect = first_free_slot(st['slot'])
                if slot is not None:
                    st['slot'] = slot
                else:
                    rect = stacked_rect(st['slot'])
                placed_rects.append(rect)
                # Anchor-relative offset reused on frozen frames (also
                # captures the stacked-fallback position, which slot_rect
                # alone can't reproduce)
                st['solved'] = (rect.left() - x, rect.top() - y)
            else:
                # Frozen layout: ride the anchor at the solved offset
                so = st.get('solved')
                if so is None:
                    rect = clamp(slot_rect(st['slot']))
                else:
                    rect = clamp(QRect(x + so[0], y + so[1], w, h))

            # Smooth the chip's offset FROM ITS ANCHOR, not its absolute
            # position: the chip tracks the rotating globe exactly (no lag),
            # while slot changes / edge-clamp shifts slide in over a few
            # frames instead of popping.
            off_x, off_y = rect.left() - x, rect.top() - y
            if st['off'] is None:
                st['off'] = [float(off_x), float(off_y)]
            else:
                a = 0.28
                st['off'][0] += (off_x - st['off'][0]) * a
                st['off'][1] += (off_y - st['off'][1]) * a
                # Snap when close so the chip doesn't crawl subpixel forever
                if abs(off_x - st['off'][0]) < 0.75 and \
                        abs(off_y - st['off'][1]) < 0.75:
                    st['off'] = [float(off_x), float(off_y)]
            rect = QRect(x + round(st['off'][0]), y + round(st['off'][1]), w, h)
            cx, cy = rect.left(), rect.top()

            # Leader line from marker to chip (card drawn after covers the
            # far end), with a small anchor dot at the marker
            painter.setPen(QPen(QColor(120, 170, 220, 110), 1))
            painter.drawLine(x, y, rect.center().x(), rect.center().y())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(120, 170, 220, 130)))
            painter.drawEllipse(x - 2, y - 2, 5, 5)

            # Card: subtle vertical gradient, soft border, green pulse accent
            grad = QLinearGradient(cx, cy, cx, cy + h)
            grad.setColorAt(0.0, QColor(22, 34, 56, 242))
            grad.setColorAt(1.0, QColor(10, 16, 30, 242))
            painter.setBrush(QBrush(grad))
            pulse = 110 + int(70 * abs(math.sin(self.animation_time * 2.2)))
            painter.setPen(QPen(QColor(46, 204, 113, pulse), 1.2))
            painter.drawRoundedRect(rect, 7, 7)

            def draw_logo_well(px, py, img):
                """Light backing pad so dark/navy logos stay visible on the
                dark card (e.g. the Dodgers script)."""
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(233, 237, 243, 240)))
                painter.drawRoundedRect(px - 2, py - 2, logo_w + 4, logo_w + 4, 4, 4)
                painter.drawImage(px + (logo_w - img.width()) // 2,
                                  py + (logo_w - img.height()) // 2, img)

            # Contents
            tx = cx + pad
            iy = cy + (h - logo_w) // 2
            if away_img:
                draw_logo_well(tx, iy, away_img)
            else:
                painter.setFont(progress_font)
                painter.setPen(QColor(200, 210, 225))
                painter.drawText(QRect(tx, cy, logo_w, h),
                                 Qt.AlignmentFlag.AlignCenter,
                                 (live.get('away_id') or '?').upper()[:3])
            tx += logo_w + gap

            painter.setFont(score_font)
            painter.setPen(QColor(245, 250, 255))
            painter.drawText(QRect(tx, cy, score_w, h),
                             Qt.AlignmentFlag.AlignVCenter, score_text)
            tx += score_w + gap

            if home_img:
                draw_logo_well(tx, iy, home_img)
            else:
                painter.setFont(progress_font)
                painter.setPen(QColor(200, 210, 225))
                painter.drawText(QRect(tx, cy, logo_w, h),
                                 Qt.AlignmentFlag.AlignCenter,
                                 (live.get('home_id') or '?').upper()[:3])
            tx += logo_w

            if progress:
                tx += gap
                painter.setBrush(QBrush(QColor(46, 204, 113, 230)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(tx, cy + h // 2 - 2, 4, 4)
                tx += 3 + gap
                painter.setFont(progress_font)
                painter.setPen(QColor(80, 230, 140))
                painter.drawText(QRect(tx, cy, prog_w + 2, h),
                                 Qt.AlignmentFlag.AlignVCenter, progress)

        # Fatigue rings (near tier): amber/red halo on heavy-schedule games
        for marker in fatigue_markers:
            if not self._marker_front_facing(marker['position']):
                continue
            screen = self.project_to_screen(marker['position'], mvp)
            if not screen:
                continue
            x, y = screen
            score = marker.get('fatigue_max', 0)
            color = QColor(220, 60, 50, 200) if score >= 60 \
                else QColor(235, 170, 40, 180)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, 2))
            r = 16
            painter.drawEllipse(int(x) - r, int(y) - r, 2 * r, 2 * r)

        if focus_marker is not None:
            self._draw_game_card(painter, focus_marker, mvp)

        if self._unfold is not None:
            self._render_unfold(painter, mvp)

        painter.end()
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glEnable(gl.GL_DEPTH_TEST)

    @staticmethod
    def _marker_game_key(marker):
        """Stable identity for a game marker across marker-list rebuilds."""
        game = (marker.get('game_info') or {}).get('game')
        game_id = getattr(game, 'game_id', None)
        if game_id:
            return game_id
        return (marker.get('league'), marker.get('home_team_id'),
                marker.get('away_team_id'))

    def toggle_game_unfold(self, marker):
        """Click a game cube: unfold it into the enriched game panel; click
        again (or click empty space) to fold it back."""
        key = self._marker_game_key(marker)
        u = self._unfold
        if u is not None and u['key'] == key and u['phase'] != 'closing':
            u['phase'] = 'closing'
        else:
            # Capture the GL cube's current spin so the net starts at the
            # cube's orientation and visibly spins down to face the camera
            speed = 0.3 if marker.get('type') in ('home_today', 'away_today') \
                else 1.0
            def _wrap(angle):
                angle = angle % (2.0 * math.pi)
                return angle - 2.0 * math.pi if angle > math.pi else angle
            self._unfold = {
                'key': key, 'phase': 'opening', 't': 0.0,
                'spin0': (_wrap(self.animation_time * speed),
                          _wrap(self.animation_time * speed * 0.7)),
            }
        self._selected_game_key = None
        self.update()

    @staticmethod
    def _unfold_net_quads(u, yaw, tilt):
        """Corner geometry of the cube net at unfold progress u (0=closed
        cube, 1=flat cross), spun by yaw/tilt (rad). Local units: face
        half-size = 1; +z = away from viewer. Returns [(face_id, [4 corners
        (x,y,z) in bl,br,tr,tl screen order]), ...]."""
        a = (math.pi / 2.0) * (1.0 - u)
        ca, sa = math.cos(a), math.sin(a)
        c2, s2 = math.cos(2 * a), math.sin(2 * a)

        faces = [
            # Center (cube front)
            ('C', [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)]),
            # Right: hinge x=+1
            ('R', [(1, -1, 0), (1 + 2 * ca, -1, 2 * sa),
                   (1 + 2 * ca, 1, 2 * sa), (1, 1, 0)]),
            # Left: hinge x=-1 (bl..tl order keeps texture upright)
            ('L', [(-1 - 2 * ca, -1, 2 * sa), (-1, -1, 0),
                   (-1, 1, 0), (-1 - 2 * ca, 1, 2 * sa)]),
            # Top: hinge y=+1
            ('T', [(-1, 1, 0), (1, 1, 0),
                   (1, 1 + 2 * ca, 2 * sa), (-1, 1 + 2 * ca, 2 * sa)]),
            # Bottom: hinge y=-1
            ('B', [(-1, -1 - 2 * ca, 2 * sa), (1, -1 - 2 * ca, 2 * sa),
                   (1, -1, 0), (-1, -1, 0)]),
            # Back: chained to right face's far edge, folds with it
            ('K', [(1 + 2 * ca, -1, 2 * sa),
                   (1 + 2 * ca + 2 * c2, -1, 2 * sa + 2 * s2),
                   (1 + 2 * ca + 2 * c2, 1, 2 * sa + 2 * s2),
                   (1 + 2 * ca, 1, 2 * sa)]),
        ]

        cy_, sy_ = math.cos(yaw), math.sin(yaw)
        cx_, sx_ = math.cos(tilt), math.sin(tilt)

        def rot(p):
            x, y, z = p
            # rotX(tilt) then rotY(yaw) — matches the marker shader order
            y, z = y * cx_ - z * sx_, y * sx_ + z * cx_
            x, z = x * cy_ + z * sy_, -x * sy_ + z * cy_
            return (x, y, z)

        return [(fid, [rot(p) for p in corners]) for fid, corners in faces]

    def _render_unfold(self, painter, mvp):
        """Draw the unfolding cube net, then crossfade into the game panel."""
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import (QColor, QPen, QBrush, QPolygonF, QTransform)

        u = self._unfold
        marker = next(
            (m for m in self.team_city_markers
             if m.get('game_info') and self._marker_game_key(m) == u['key']),
            None)
        if marker is None:  # game left the board
            self._unfold = None
            return
        if not self._marker_front_facing(marker['position']):
            if u['phase'] != 'closing':
                u['phase'] = 'closing'
            return
        screen = self.project_to_screen(marker['position'], mvp)
        if not screen:
            return
        ax, ay = screen

        t = max(0.0, min(1.0, u['t']))
        prog = min(1.0, t / 0.65)
        prog = prog * prog * (3.0 - 2.0 * prog)          # smoothstep
        yaw = u['spin0'][0] * (1.0 - prog)
        tilt = u['spin0'][1] * (1.0 - prog)
        s_px = 14.0 + (46.0 - 14.0) * prog               # face half-size px

        # Pull the anchor onscreen as the net grows (cross spans x∈[-3s,5s],
        # y∈[-3s,3s] around the anchor)
        mx_lo, mx_hi = 3 * s_px + 8, self.width() - 5 * s_px - 8
        my_lo, my_hi = 3 * s_px + 8, self.height() - 3 * s_px - 8
        if mx_lo < mx_hi:
            ax = ax + (max(mx_lo, min(ax, mx_hi)) - ax) * prog
        if my_lo < my_hi:
            ay = ay + (max(my_lo, min(ay, my_hi)) - ay) * prog

        cross_alpha = 1.0 - max(0.0, min(1.0, (t - 0.70) / 0.18))
        panel_alpha = max(0.0, min(1.0, (t - 0.68) / 0.22))

        if cross_alpha > 0.01:
            league = (marker.get('league') or '').lower()
            away_img = self.team_logo_images_big.get(
                f"{league}_{(marker.get('away_team_id') or '').lower()}")
            home_img = self.team_logo_images_big.get(
                f"{league}_{(marker.get('home_team_id') or '').lower()}")
            face_imgs = {'C': away_img, 'L': away_img, 'B': away_img,
                         'R': home_img, 'T': home_img, 'K': home_img}
            ac = self.get_team_color((marker.get('away_team_id') or '').upper())
            hc = self.get_team_color((marker.get('home_team_id') or '').upper())
            face_cols = {f: (ac if face_imgs[f] is away_img else hc)
                         for f in face_imgs}

            quads = self._unfold_net_quads(prog, yaw, tilt)

            def project(p):
                x, y, z = p
                f = 1.0 / max(0.4, 1.0 + z * 0.10)
                return (ax + x * s_px * f, ay - y * s_px * f)

            # Painter's algorithm: farthest faces first
            quads.sort(key=lambda q: -sum(c[2] for c in q[1]) / 4.0)

            painter.setOpacity(cross_alpha)
            for fid, corners in quads:
                pts = [QPointF(*project(c)) for c in corners]
                poly = QPolygonF(pts)

                # Face shading from the 3D normal (edge-on -> dark)
                e1 = tuple(corners[1][i] - corners[0][i] for i in range(3))
                e2 = tuple(corners[3][i] - corners[0][i] for i in range(3))
                nx = e1[1] * e2[2] - e1[2] * e2[1]
                ny = e1[2] * e2[0] - e1[0] * e2[2]
                nz = e1[0] * e2[1] - e1[1] * e2[0]
                norm = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
                bright = 0.45 + 0.55 * abs(nz) / norm

                # Dark card backing (logos are transparent PNGs)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(16, 24, 42, 235)))
                painter.drawPolygon(poly)

                img = face_imgs.get(fid)
                if img is not None:
                    src = QPolygonF([
                        QPointF(0, img.height()),
                        QPointF(img.width(), img.height()),
                        QPointF(img.width(), 0), QPointF(0, 0)])
                    tr = QTransform()
                    if QTransform.quadToQuad(src, QPolygonF(pts), tr):
                        painter.save()
                        painter.setTransform(tr, True)
                        painter.drawImage(0, 0, img)
                        painter.restore()
                else:
                    r, g, b = face_cols.get(fid, (0.5, 0.5, 0.6))
                    painter.setBrush(QBrush(QColor(
                        int(r * 255), int(g * 255), int(b * 255), 160)))
                    painter.drawPolygon(poly)

                # Shading overlay + edge
                shade = int((1.0 - bright) * 170)
                if shade > 0:
                    painter.setBrush(QBrush(QColor(0, 0, 0, shade)))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawPolygon(poly)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(140, 180, 230, 120), 1))
                painter.drawPolygon(poly)
            painter.setOpacity(1.0)

        if panel_alpha > 0.01:
            # Panel centered on the cross center (local x=+1)
            self._draw_unfold_panel(painter, marker,
                                    ax + s_px, ay, panel_alpha)

    def _draw_unfold_panel(self, painter, marker, px, py, alpha):
        """The unfolded game panel: matchup, score, venue weather, fatigue,
        and flashscore lineups."""
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QColor, QFont, QPen, QBrush, QLinearGradient

        league = (marker.get('league') or '').upper()
        away_id = (marker.get('away_team_id') or '?')
        home_id = (marker.get('home_team_id') or '?')
        away_img = self.team_logo_images_big.get(f"{league.lower()}_{away_id.lower()}")
        home_img = self.team_logo_images_big.get(f"{league.lower()}_{home_id.lower()}")
        live = marker.get('live')
        game = (marker.get('game_info') or {}).get('game')
        weather = marker.get('weather')
        fatigue = marker.get('fatigue_detail') or {}
        lineups = marker.get('lineups')

        header_font = QFont("Arial", 11, QFont.Weight.Bold)
        score_font = QFont("Arial", 15, QFont.Weight.Bold)
        detail_font = QFont("Arial", 9)
        line_font = QFont("Arial", 8)
        col_head_font = QFont("Arial", 8, QFont.Weight.Bold)

        # ---- text lines
        final = marker.get('final')
        if live:
            status_text = f"{live.get('away_score', '?')} – {live.get('home_score', '?')}"
            progress = live.get('progress') or ''
            sub_text = progress if progress.upper() != 'LIVE' else 'LIVE'
        elif final:
            status_text = f"{final.get('away', '?')} – {final.get('home', '?')}"
            sub_text = "Final"
        else:
            status_text = None
            game_status = getattr(getattr(game, 'status', None), 'value', '')
            if game_status == 'final':
                sub_text = "Final"
            elif game_status in ('postponed', 'cancelled'):
                sub_text = game_status.capitalize()
            else:
                sub_text = game.date.strftime("%a %b %d  ·  %I:%M %p") if game else ''

        venue_text = None
        if game is not None and getattr(game, 'venue', None) is not None:
            venue_text = f"{game.venue.name}  ·  {game.venue.city}"

        weather_text = None
        if weather:
            weather_text = (f"{weather.get('temperature', 0):.0f}°F  ·  "
                            f"{weather.get('condition', '?')}  ·  "
                            f"wind {weather.get('wind_speed', 0):.0f} mph")

        fatigue_lines = []
        for side, tid in (('away', away_id), ('home', home_id)):
            tf = fatigue.get(side)
            if tf is not None:
                fatigue_lines.append(f"{tid.upper()} fatigue {tf.score:.0f}")
        fatigue_text = "  ·  ".join(fatigue_lines) if fatigue_lines else None

        # Lineup rows (capped at 9 — full MLB order; NHL/NBA show first 9)
        MAX_ROWS = 9
        away_rows, home_rows = [], []
        away_starter = home_starter = None
        if lineups and lineups.get('posted'):
            def fmt(p):
                num = f"{p.get('number')} " if p.get('number') else ""
                return f"{num}{p.get('name', '')}"
            away_rows = [fmt(p) for p in lineups['away']['players'][:MAX_ROWS]]
            home_rows = [fmt(p) for p in lineups['home']['players'][:MAX_ROWS]]
            away_starter = lineups['away'].get('starter')
            home_starter = lineups['home'].get('starter')
            lineup_note = None
        elif lineups is not None:
            lineup_note = "Lineups not posted yet"
        elif marker.get('lineups_pending'):
            lineup_note = "Lineups — loading…"
        else:
            lineup_note = None

        # ---- panel size
        w = 340
        pad = 12
        well = 28
        h = 10 + well + 6                      # header
        if status_text:
            h += 26
        h += 18                                # sub line
        for txt in (venue_text, weather_text, fatigue_text):
            if txt:
                h += 15
        n_rows = max(len(away_rows), len(home_rows))
        if n_rows:
            h += 8 + 14                        # divider + column headers
            h += n_rows * 14
            if away_starter or home_starter:
                h += 16
        elif lineup_note:
            h += 8 + 16
        h += 10

        cx = int(px - w / 2)
        cy = int(py - h / 2)
        cx = max(6, min(cx, self.width() - w - 6))
        cy = max(6, min(cy, self.height() - h - 6))

        painter.setOpacity(alpha)

        # ---- chrome
        grad = QLinearGradient(cx, cy, cx, cy + h)
        grad.setColorAt(0.0, QColor(24, 36, 58, 246))
        grad.setColorAt(1.0, QColor(11, 17, 32, 246))
        painter.setBrush(QBrush(grad))
        pulse = 120 + int(60 * abs(math.sin(self.animation_time * 2.2)))
        border = QColor(46, 204, 113, pulse) if live else QColor(110, 160, 215, 200)
        painter.setPen(QPen(border, 1.4))
        painter.drawRoundedRect(QRect(cx, cy, w, h), 9, 9)

        # ---- header: logos + matchup
        ty = cy + 10

        def logo_well(x0, img, fallback):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(233, 237, 243, 240)))
            painter.drawRoundedRect(x0 - 2, ty - 2, well + 4, well + 4, 5, 5)
            if img is not None:
                scaled = img.scaled(well, well,
                                    Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
                painter.drawImage(x0 + (well - scaled.width()) // 2,
                                  ty + (well - scaled.height()) // 2, scaled)
            else:
                painter.setFont(detail_font)
                painter.setPen(QColor(40, 50, 70))
                painter.drawText(QRect(x0, ty, well, well),
                                 Qt.AlignmentFlag.AlignCenter, fallback[:3].upper())

        logo_well(cx + pad, away_img, away_id)
        logo_well(cx + w - pad - well, home_img, home_id)
        painter.setFont(header_font)
        painter.setPen(QColor(245, 250, 255))
        painter.drawText(QRect(cx + pad + well, ty, w - 2 * (pad + well), well),
                         Qt.AlignmentFlag.AlignCenter,
                         f"{away_id.upper()}  @  {home_id.upper()}")
        ty += well + 6

        # ---- score / status
        if status_text:
            painter.setFont(score_font)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(QRect(cx, ty, w, 24),
                             Qt.AlignmentFlag.AlignCenter, status_text)
            ty += 26

        painter.setFont(detail_font)
        if live:
            painter.setPen(QColor(80, 230, 140))
        else:
            painter.setPen(QColor(150, 170, 195))
        painter.drawText(QRect(cx, ty, w, 16),
                         Qt.AlignmentFlag.AlignCenter, sub_text)
        ty += 18

        for txt, col in ((venue_text, QColor(150, 170, 195)),
                         (weather_text, QColor(140, 175, 210))):
            if txt:
                painter.setPen(col)
                painter.drawText(QRect(cx, ty, w, 15),
                                 Qt.AlignmentFlag.AlignCenter, txt)
                ty += 15
        if fatigue_text:
            worst = max((tf.score for tf in fatigue.values()), default=0)
            painter.setPen(QColor(225, 90, 70) if worst >= 60 else
                           QColor(235, 180, 60) if worst >= 40 else
                           QColor(120, 200, 140))
            painter.drawText(QRect(cx, ty, w, 15),
                             Qt.AlignmentFlag.AlignCenter, fatigue_text)
            ty += 15

        # ---- lineups
        if n_rows or lineup_note:
            ty += 4
            painter.setPen(QPen(QColor(255, 255, 255, 30), 1))
            painter.drawLine(cx + pad, ty, cx + w - pad, ty)
            ty += 4

        if n_rows:
            col_w = (w - 2 * pad - 10) // 2
            painter.setFont(col_head_font)
            painter.setPen(QColor(150, 170, 195))
            painter.drawText(QRect(cx + pad, ty, col_w, 13),
                             Qt.AlignmentFlag.AlignLeft, away_id.upper())
            painter.drawText(QRect(cx + w - pad - col_w, ty, col_w, 13),
                             Qt.AlignmentFlag.AlignRight, home_id.upper())
            ty += 14
            painter.setFont(line_font)
            painter.setPen(QColor(210, 220, 235))
            for i in range(n_rows):
                if i < len(away_rows):
                    painter.drawText(QRect(cx + pad, ty, col_w, 13),
                                     Qt.AlignmentFlag.AlignLeft, away_rows[i])
                if i < len(home_rows):
                    painter.drawText(QRect(cx + w - pad - col_w, ty, col_w, 13),
                                     Qt.AlignmentFlag.AlignRight, home_rows[i])
                ty += 14
            if away_starter or home_starter:
                painter.setPen(QColor(140, 200, 250))
                if away_starter:
                    painter.drawText(QRect(cx + pad, ty, col_w, 14),
                                     Qt.AlignmentFlag.AlignLeft,
                                     f"P: {away_starter}")
                if home_starter:
                    painter.drawText(QRect(cx + w - pad - col_w, ty, col_w, 14),
                                     Qt.AlignmentFlag.AlignRight,
                                     f"P: {home_starter}")
                ty += 16
        elif lineup_note:
            painter.setFont(detail_font)
            painter.setPen(QColor(150, 170, 195))
            painter.drawText(QRect(cx, ty, w, 16),
                             Qt.AlignmentFlag.AlignCenter, lineup_note)

        painter.setOpacity(1.0)

    def _draw_game_card(self, painter, marker, mvp):
        """Full game card for the focused near-tier marker: matchup, score or
        start time, venue weather, and both teams' fatigue."""
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QColor, QFont, QPen, QBrush, QLinearGradient

        screen = self.project_to_screen(marker['position'], mvp)
        if not screen:
            return
        mx, my = int(screen[0]), int(screen[1])

        league = (marker.get('league') or '').upper()
        away_id = (marker.get('away_team_id') or '?')
        home_id = (marker.get('home_team_id') or '?')
        away_img = self.team_logo_images.get(f"{league.lower()}_{away_id.lower()}")
        home_img = self.team_logo_images.get(f"{league.lower()}_{home_id.lower()}")
        live = marker.get('live')
        game = (marker.get('game_info') or {}).get('game')
        weather = marker.get('weather')
        fatigue = marker.get('fatigue_detail') or {}

        header_font = QFont("Arial", 11, QFont.Weight.Bold)
        score_font = QFont("Arial", 14, QFont.Weight.Bold)
        detail_font = QFont("Arial", 9)

        # ---- assemble lines (text, font, color)
        if live:
            status_text = f"{live.get('away_score', '?')} – {live.get('home_score', '?')}"
            progress = live.get('progress') or ''
            sub_text = progress if progress.upper() != 'LIVE' else 'LIVE'
        else:
            status_text = None
            game_status = getattr(getattr(game, 'status', None), 'value', '')
            if game_status == 'final':
                sub_text = "Final"
            elif game_status in ('postponed', 'cancelled'):
                sub_text = game_status.capitalize()
            else:
                sub_text = game.date.strftime("%a %b %d  ·  %I:%M %p") if game else ''

        weather_text = None
        if weather:
            weather_text = (f"{weather.get('temperature', 0):.0f}°F  ·  "
                            f"{weather.get('condition', '?')}  ·  "
                            f"wind {weather.get('wind_speed', 0):.0f} mph")

        fatigue_text = None
        if fatigue:
            parts = []
            for side, tid in (('away', away_id), ('home', home_id)):
                tf = fatigue.get(side)
                if tf:
                    parts.append(f"{tid.upper()} {tf.score:.0f}")
            if parts:
                fatigue_text = "⚡ fatigue  " + "  ·  ".join(parts)

        # ---- measure
        pad = 12
        well = 24
        painter.setFont(header_font)
        header_text = f"{away_id.upper()}  @  {home_id.upper()}"
        header_w = painter.fontMetrics().horizontalAdvance(header_text)
        w = max(200, pad + well + 8 + header_w + 8 + well + pad)

        h = pad + well + 6
        if status_text:
            h += 26
        h += 18  # sub_text line
        if weather_text:
            h += 17
        if fatigue_text:
            h += 17
        h += pad - 4

        # ---- placement: beside the marker, flipped/clamped at edges
        cx = mx + 30
        if cx + w > self.width() - 8:
            cx = mx - w - 30
        cy = my - h // 2
        cy = max(8, min(cy, self.height() - h - 8))

        # stem
        painter.setPen(QPen(QColor(120, 170, 220, 130), 1))
        edge_x = cx if cx > mx else cx + w
        painter.drawLine(mx, my, edge_x, cy + h // 2)

        # card body
        grad = QLinearGradient(cx, cy, cx, cy + h)
        grad.setColorAt(0.0, QColor(24, 36, 58, 246))
        grad.setColorAt(1.0, QColor(10, 16, 30, 246))
        painter.setBrush(QBrush(grad))
        painter.setPen(QPen(QColor(100, 150, 200, 150), 1.2))
        painter.drawRoundedRect(cx, cy, w, h, 9, 9)

        # league accent line along the top
        accent = self.LEAGUE_COLORS.get(league, (0.4, 0.6, 0.9))
        painter.setPen(QPen(QColor(int(accent[0] * 255), int(accent[1] * 255),
                                   int(accent[2] * 255), 230), 3))
        painter.drawLine(cx + 10, cy + 1, cx + w - 10, cy + 1)

        # ---- header row: logo wells + matchup
        ty = cy + pad

        def well_at(px, img, fallback):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(233, 237, 243, 240)))
            painter.drawRoundedRect(px, ty, well, well, 5, 5)
            if img:
                painter.drawImage(px + (well - img.width()) // 2,
                                  ty + (well - img.height()) // 2, img)
            else:
                painter.setFont(detail_font)
                painter.setPen(QColor(40, 50, 70))
                painter.drawText(QRect(px, ty, well, well),
                                 Qt.AlignmentFlag.AlignCenter, fallback[:3].upper())

        well_at(cx + pad, away_img, away_id)
        well_at(cx + w - pad - well, home_img, home_id)
        painter.setFont(header_font)
        painter.setPen(QColor(245, 250, 255))
        painter.drawText(QRect(cx + pad + well, ty, w - 2 * (pad + well), well),
                         Qt.AlignmentFlag.AlignCenter, header_text)
        ty += well + 6

        # ---- score (live) and status line
        if status_text:
            painter.setFont(score_font)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(QRect(cx, ty, w, 24), Qt.AlignmentFlag.AlignCenter,
                             status_text)
            ty += 26

        painter.setFont(detail_font)
        if live:
            pulse = 150 + int(90 * abs(math.sin(self.animation_time * 2.2)))
            painter.setBrush(QBrush(QColor(46, 204, 113, pulse)))
            painter.setPen(Qt.PenStyle.NoPen)
            sub_w = painter.fontMetrics().horizontalAdvance(sub_text)
            painter.drawEllipse(cx + w // 2 - sub_w // 2 - 11, ty + 5, 6, 6)
            painter.setPen(QColor(80, 230, 140))
        else:
            painter.setPen(QColor(150, 170, 195))
        painter.drawText(QRect(cx, ty, w, 16), Qt.AlignmentFlag.AlignCenter, sub_text)
        ty += 18

        if weather_text:
            painter.setPen(QColor(140, 175, 210))
            painter.drawText(QRect(cx, ty, w, 15), Qt.AlignmentFlag.AlignCenter,
                             weather_text)
            ty += 17

        if fatigue_text:
            worst = max((tf.score for tf in fatigue.values()), default=0)
            color = QColor(225, 90, 70) if worst >= 60 else \
                QColor(235, 180, 60) if worst >= 40 else QColor(120, 200, 140)
            painter.setPen(color)
            painter.drawText(QRect(cx, ty, w, 15), Qt.AlignmentFlag.AlignCenter,
                             fatigue_text)
    
    def render_single_marker(self, marker, rotation_speed=1.0, size_scale=1.0):
        """Render a single marker with split cube support"""
        if not self.marker_vao:
            return
            
        pos = marker['position']
        team_id = marker.get('team_id', '')
        marker_league = marker.get('league', '').upper()
        size = marker.get('size', 4.0)
        marker_type = marker.get('type', 'generic')
        is_split_cube = marker.get('is_split_cube', False)
        
        if not pos or len(pos) != 3:
            return
        
        if not marker_league:
            marker_league = self.infer_league_from_team_id(team_id)
        
        # Handle animated markers with special properties
        is_animated = marker_type == 'animated'
        if is_animated:
            rotation_speed = 2.5  # Faster rotation for animated markers
        
        # Set split cube uniform
        self.marker_shader.setUniformValue("isSplitCube", is_split_cube)
        self.marker_shader.setUniformValue("isAnimated", is_animated)
        
        if is_split_cube:
            # Handle split cube with both home and away teams
            home_team_id = marker.get('home_team_id', '')
            away_team_id = marker.get('away_team_id', '')
            
            # Load home team texture
            home_texture = self.get_team_logo_texture(home_team_id, marker_league)
            home_color = self.get_team_color(home_team_id.upper())
            
            # Load away team texture  
            away_texture = self.get_team_logo_texture(away_team_id, marker_league)
            away_color = self.get_team_color(away_team_id.upper())
            
            # Bind textures
            if home_texture:
                gl.glActiveTexture(gl.GL_TEXTURE0)
                home_texture.bind()
                self.marker_shader.setUniformValue("homeTeamTexture", 0)
                self.marker_shader.setUniformValue("useHomeTexture", True)
            else:
                self.marker_shader.setUniformValue("useHomeTexture", False)
                
            if away_texture:
                gl.glActiveTexture(gl.GL_TEXTURE1)
                away_texture.bind()
                self.marker_shader.setUniformValue("awayTeamTexture", 1)
                self.marker_shader.setUniformValue("useAwayTexture", True)
            else:
                self.marker_shader.setUniformValue("useAwayTexture", False)
            
            # Set team colors
            self.marker_shader.setUniformValue("homeTeamColor", QVector3D(*home_color))
            self.marker_shader.setUniformValue("awayTeamColor", QVector3D(*away_color))
            
            # Clear single texture uniforms (not used in split cube mode)
            self.marker_shader.setUniformValue("useTexture", False)
            self.marker_shader.setUniformValue("teamColor", QVector3D(1.0, 1.0, 1.0))
            
        else:
            # Regular single-team cube logic
            texture = None
            use_texture = False
            team_color = (1.0, 1.0, 1.0)
            
            if team_id:
                if marker_league:
                    league_team_key = f"{marker_league.lower()}_{team_id.lower()}"
                    if league_team_key in self.team_logo_textures:
                        texture = self.team_logo_textures[league_team_key]
                        use_texture = True
                        team_color = self.get_team_color(team_id.upper())
            
            # Fallback to default marker
            if not texture and self.default_marker_texture:
                texture = self.default_marker_texture
                use_texture = True
                if is_animated:
                    team_color = (1.0, 0.8, 0.2)  # Special color for animated markers
                elif marker_type == 'live_flight_confirmed':
                    team_color = (0.2, 1.0, 0.4)  # Bright green for confirmed flights
                elif marker_type == 'live_flight_likely':
                    team_color = (1.0, 0.9, 0.2)  # Yellow for likely flights
                elif marker_type == 'live_flight_possible':
                    team_color = (1.0, 0.6, 0.2)  # Orange for possible flights
                else:
                    team_color = self.get_team_color(team_id.upper()) if team_id else (1.0, 0.4, 0.1)
            
            # Bind single texture
            if texture and use_texture:
                gl.glActiveTexture(gl.GL_TEXTURE0)
                texture.bind()
                self.marker_shader.setUniformValue("markerTexture", 0)
            
            self.marker_shader.setUniformValue("useTexture", use_texture)
            self.marker_shader.setUniformValue("teamColor", QVector3D(*team_color))
            
            # Clear split cube uniforms
            self.marker_shader.setUniformValue("useHomeTexture", False)
            self.marker_shader.setUniformValue("useAwayTexture", False)
        
        # Set common uniforms
        self.marker_shader.setUniformValue("markerCenter", QVector3D(*pos))
        self.marker_shader.setUniformValue("markerSize", size * size_scale)
        self.marker_shader.setUniformValue("rotationSpeed", rotation_speed)
        self.marker_shader.setUniformValue("markerAlpha", 0.9)
        self.marker_shader.setUniformValue("time", self.animation_time)
        
        gl.glBindVertexArray(self.marker_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
        gl.glBindVertexArray(0)
    
    def render_animated_marker(self, mvp, model):
        """Render the animated traveling marker"""
        if not self.animated_marker_position or not self.marker_vao:
            return
        
        position = self.animated_marker_position['position']
        team_id = self.animated_marker_position['team_id']
        
        animated_marker = {
            'position': position,
            'team_id': team_id.upper(),
            'size': 6.0,
            'type': 'animated',
            'color': (1.0, 0.8, 0.2)
        }
        
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_DEPTH_TEST)
        
        self.marker_shader.bind()
        self.marker_shader.setUniformValue("mvp", mvp)
        self.marker_shader.setUniformValue("time", self.animation_time)
        
        self.render_single_marker(animated_marker, 2.5)  # Fast rotation for animated marker
        
        self.marker_shader.release()
    
    
    # Utility Methods
    def infer_league_from_team_id(self, team_id: str) -> Optional[str]:
        """Infer league from team_id pattern like MLB_2025_20250511_bos_kc"""
        if not team_id:
            return None
        parts = team_id.split('_')
        if len(parts) >= 1 and parts[0].upper() in ["MLB", "NBA", "NHL"]:
            return parts[0].upper()
        return None
    
    def set_display_options(self, show_paths: bool = True, show_cities: bool = True, 
                          show_atmosphere: bool = True):
        """Set display options"""
        self.show_travel_paths = show_paths
        self.show_team_cities = show_cities
        self.show_atmosphere = show_atmosphere
        self.update()
    
    def filter_flights(self, filtered_travel: List):
        """Update display with filtered travel data"""
        temp_data = self.travel_data
        self.travel_data = filtered_travel
        self.generate_enhanced_visualizations()
        self.travel_data = temp_data
        self.update()
    
    # Raycasting for Click Detection
    def screen_to_ray(self, screen_x: int, screen_y: int):
        """Convert screen coordinates to world ray (origin, direction)"""
        width = self.width()
        height = self.height()

        # Normalize screen coordinates to NDC (-1 to 1)
        ndc_x = (2.0 * screen_x / width) - 1.0
        ndc_y = 1.0 - (2.0 * screen_y / height)

        # Recreate the matrices from paintGL
        view = QMatrix4x4()
        view.lookAt(QVector3D(0, 0, self.camera_distance()), QVector3D(0, 0, 0), QVector3D(0, 1, 0))

        projection = QMatrix4x4()
        aspect_ratio = width / max(height, 1)
        projection.perspective(45.0, aspect_ratio, 0.1, 100.0)

        # Inverse matrices
        inv_projection, proj_ok = projection.inverted()
        inv_view, view_ok = view.inverted()
        inv_model, model_ok = self.rotation_matrix.inverted()

        if not (proj_ok and view_ok and model_ok):
            return None, None

        # Near and far points in NDC
        near_ndc = QVector4D(ndc_x, ndc_y, -1.0, 1.0)
        far_ndc = QVector4D(ndc_x, ndc_y, 1.0, 1.0)

        # Transform to world space
        near_clip = inv_projection * near_ndc
        far_clip = inv_projection * far_ndc

        # Perspective divide
        near_eye = QVector4D(near_clip.x() / near_clip.w(), near_clip.y() / near_clip.w(),
                            near_clip.z() / near_clip.w(), 1.0)
        far_eye = QVector4D(far_clip.x() / far_clip.w(), far_clip.y() / far_clip.w(),
                           far_clip.z() / far_clip.w(), 1.0)

        near_world = inv_model * inv_view * near_eye
        far_world = inv_model * inv_view * far_eye

        origin = QVector3D(near_world.x(), near_world.y(), near_world.z())
        direction = QVector3D(
            far_world.x() - near_world.x(),
            far_world.y() - near_world.y(),
            far_world.z() - near_world.z()
        ).normalized()

        return origin, direction

    def ray_sphere_intersect(self, origin: QVector3D, direction: QVector3D,
                            sphere_center: QVector3D, radius: float):
        """Check if ray intersects sphere, return distance or None"""
        oc = origin - sphere_center
        a = QVector3D.dotProduct(direction, direction)
        b = 2.0 * QVector3D.dotProduct(oc, direction)
        c = QVector3D.dotProduct(oc, oc) - radius * radius

        discriminant = b * b - 4 * a * c
        if discriminant < 0:
            return None

        t = (-b - math.sqrt(discriminant)) / (2.0 * a)
        return t if t > 0 else None

    def find_clicked_flight(self, screen_x: int, screen_y: int):
        """Return the icao24 of the plane model under the cursor, or None."""
        origin, direction = self.screen_to_ray(screen_x, screen_y)
        if origin is None:
            return None

        closest, closest_t = None, float('inf')
        for icao24, fd in self.live_flights.items():
            lat, lon = fd.get('latitude'), fd.get('longitude')
            if lat is None or lon is None:
                continue
            altitude_scale = 1.0 + (fd.get('altitude_ft', 35000) / 40000.0) * 0.12
            pos = self.lat_lon_to_3d(lat, lon, altitude_scale)
            t = self.ray_sphere_intersect(origin, direction,
                                          QVector3D(*pos), 0.05)
            if t is not None and t < closest_t:
                closest, closest_t = icao24, t
        return closest

    def toggle_flight_route(self, icao24: str):
        if icao24 in self._shown_route_icaos:
            self._shown_route_icaos.discard(icao24)
        else:
            self._shown_route_icaos.add(icao24)
        self.update()

    def find_clicked_marker(self, screen_x: int, screen_y: int):
        """Find which marker (if any) was clicked"""
        ray_result = self.screen_to_ray(screen_x, screen_y)
        if ray_result[0] is None:
            return None

        origin, direction = ray_result
        closest_marker = None
        closest_distance = float('inf')
        hit_radius = 0.045  # Marker hit detection radius

        # Check team city markers
        for marker in self.team_city_markers:
            pos = marker.get('position')
            if not pos:
                continue

            sphere_center = QVector3D(pos[0], pos[1], pos[2])
            distance = self.ray_sphere_intersect(origin, direction, sphere_center, hit_radius)

            if distance is not None and distance < closest_distance:
                closest_distance = distance
                closest_marker = marker

        return closest_marker

    # Mouse Interaction Methods
    def mousePressEvent(self, event):
        """Handle mouse press for rotation and marker selection"""
        if event.button() == Qt.MouseButton.LeftButton:
            # Planes first (drawn on top): click toggles that charter's arc
            clicked_flight = self.find_clicked_flight(event.pos().x(), event.pos().y())
            if clicked_flight:
                self.toggle_flight_route(clicked_flight)
                return

            # Check for marker click
            clicked_marker = self.find_clicked_marker(event.pos().x(), event.pos().y())

            if clicked_marker:
                # Game cube: toggle the unfold panel (cube unfolds into the
                # enriched game panel; clicking again folds it back)
                if clicked_marker.get('game_info'):
                    self.toggle_game_unfold(clicked_marker)

                # Emit signal with marker data
                marker_id = clicked_marker.get('team_id', clicked_marker.get('city_name', 'unknown'))
                self.markerClicked.emit(marker_id, clicked_marker)
                return  # Don't start globe rotation

            # No marker clicked - fold back any open panel, dismiss any
            # game card, start rotation
            if self._unfold is not None and self._unfold['phase'] != 'closing':
                self._unfold['phase'] = 'closing'
                self.update()
            if self._selected_game_key is not None:
                self._selected_game_key = None
                self.update()
            self.is_grabbed = True
            self.last_mouse_pos = event.pos()
            self.rotation_momentum = QVector3D(0, 0, 0)
    
    def mouseMoveEvent(self, event):
        """Handle mouse movement with momentum"""
        if self.is_grabbed:
            delta = event.pos() - self.last_mouse_pos
            self.update_rotation_with_momentum(delta)
            self.last_mouse_pos = event.pos()
    
    def mouseReleaseEvent(self, event):
        """Handle mouse release"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_grabbed = False
    
    def update_rotation(self):
        #self.rotation_momentum = self.passive_rotation_speed
        x_quat = QQuaternion.fromAxisAndAngle(QVector3D(1, 0, 0), self.passive_rotation_speed.x())
        y_quat = QQuaternion.fromAxisAndAngle(QVector3D(0, 1, 0), self.passive_rotation_speed.y())
        rotation_quat = y_quat * x_quat
        rotation_matrix = QMatrix4x4()
        rotation_matrix.rotate(rotation_quat)
        self.rotation_matrix = rotation_matrix * self.rotation_matrix
        self.update()
    
    def update_rotation_with_momentum(self, delta):
        """Update rotation with momentum calculation"""
        scaled_sensitivity = self.sensitivity * (1.0/(self.zoom_level*self.zoom_level))
        x_rotation = delta.y() * scaled_sensitivity
        y_rotation = delta.x() * scaled_sensitivity
        self.rotation_momentum = QVector3D(x_rotation, y_rotation, 0)
        
        x_quat = QQuaternion.fromAxisAndAngle(QVector3D(1, 0, 0), x_rotation)
        y_quat = QQuaternion.fromAxisAndAngle(QVector3D(0, 1, 0), y_rotation)
        
        rotation_quat = y_quat * x_quat
        rotation_matrix = QMatrix4x4()
        rotation_matrix.rotate(rotation_quat)
        
        self.rotation_matrix = rotation_matrix * self.rotation_matrix
        self.update()
    
    def decay_momentum(self):
        momentum_rotation = QMatrix4x4()
        momentum_rotation.rotate(self.rotation_momentum.length(),
                                 self.rotation_momentum.normalized())
        self.rotation_matrix = momentum_rotation * self.rotation_matrix
        self.rotation_momentum *= self.momentum_decay
        
    def camera_distance(self) -> float:
        """Camera distance from globe center. Asymptotes toward the surface
        (radius 1.0) instead of passing through it — the old 3/zoom put the
        camera INSIDE the sphere for zoom > 3. zoom 1 still gives 3.0."""
        return 1.0 + 2.0 / self.zoom_level

    def _model_point_under_cursor(self, screen_x: int, screen_y: int):
        """Model-space point on the globe surface under the cursor, or None."""
        origin, direction = self.screen_to_ray(screen_x, screen_y)
        if origin is None:
            return None
        t = self.ray_sphere_intersect(origin, direction, QVector3D(0, 0, 0), 1.0)
        if t is None:
            return None
        return origin + direction * t

    def wheelEvent(self, event):
        """Zoom toward the point under the cursor (Google-Earth style)."""
        delta = event.angleDelta().y()
        zoom_factor = 1.05 if delta > 0 else 0.95
        new_zoom = max(self.min_zoom, min(self.max_zoom, self.zoom_level * zoom_factor))
        if new_zoom == self.zoom_level:
            return

        pos = event.position()
        before = self._model_point_under_cursor(int(pos.x()), int(pos.y()))
        self.zoom_level = new_zoom
        after = self._model_point_under_cursor(int(pos.x()), int(pos.y()))

        if before is not None and after is not None:
            # Keep the surface point under the cursor: post-multiply by the
            # model-space rotation taking the old hit point to the new one.
            q = QQuaternion.rotationTo(before.normalized(), after.normalized())
            spin = QMatrix4x4()
            spin.rotate(q)
            self.rotation_matrix = self.rotation_matrix * spin

        self._update_lod()
        self.update()

    # ------------------------------------------------------------ LOD tiers
    LOD_FAR, LOD_MID, LOD_NEAR = 0, 1, 2

    def _update_lod(self):
        """Semantic level-of-detail from zoom, with hysteresis at boundaries.
        far (<~0.8): clustered dots; mid: logo cubes + live chips;
        near (>~2.2): scaled pins + detail chips."""
        z = self.zoom_level
        cur = self._lod
        H = 0.08
        new = cur
        if z < 0.8 - H:
            new = self.LOD_FAR
        elif z > 2.2 + H:
            new = self.LOD_NEAR
        elif (cur == self.LOD_FAR and z > 0.8 + H) or \
             (cur == self.LOD_NEAR and z < 2.2 - H):
            new = self.LOD_MID
        if new != cur:
            self._lod = new
            logger.debug("LOD tier -> %d (zoom %.2f)", new, z)

    def reset_view(self):
        """Reset view to default"""
        self.setup_default_view()
        self.zoom_level = 1.0
        self.rotation_momentum = QVector3D(0, 0, 0)
        self._update_lod()
        self.update()

    def center_on_location(self, lat: float, lon: float, animate: bool = True,
                           duration_ms: int = 900):
        """Center the globe view on a specific latitude/longitude.

        Animates with an eased quaternion slerp from the current orientation;
        pass animate=False for the old instant snap."""
        target = QMatrix4x4()
        # Tilt +lat: the longitude spin leaves the target +lat above the
        # camera axis in the YZ plane; Rx(+lat) brings it to center.
        # (The old -lat sign centered on the mirrored latitude.)
        target.rotate(lat, 1, 0, 0)
        target.rotate(-lon - 90, 0, 1, 0)  # Spin for longitude
        self.rotation_momentum = QVector3D(0, 0, 0)

        if not animate:
            self.rotation_matrix = target
            self.update()
            return

        # normalMatrix() is the upper-left 3x3 inverse-transpose, which equals
        # the rotation itself for the pure-rotation matrices used here
        start_q = QQuaternion.fromRotationMatrix(self.rotation_matrix.normalMatrix())
        end_q = QQuaternion.fromRotationMatrix(target.normalMatrix())

        if self._flyto_animation:
            self._flyto_animation.stop()

        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration_ms)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def on_tick(t):
            q = QQuaternion.slerp(start_q, end_q, float(t))
            m = QMatrix4x4()
            m.rotate(q)
            self.rotation_matrix = m
            self.update()

        anim.valueChanged.connect(on_tick)
        anim.finished.connect(lambda: setattr(self, '_flyto_animation', None))
        self._flyto_animation = anim
        anim.start()

    def resizeGL(self, width, height):
        """Handle window resize"""
        if height == 0: height = 1;
        gl.glViewport(0, 0, width, height)
    
    def cleanup_airplane_resources(self):
        """Clean up airplane OpenGL resources"""
        if hasattr(self, 'airplane_vao') and self.airplane_vao:
            gl.glDeleteVertexArrays(1, [self.airplane_vao])
            self.airplane_vao = 0

        if hasattr(self, 'airplane_vbo') and self.airplane_vbo:
            gl.glDeleteBuffers(1, [self.airplane_vbo])
            self.airplane_vbo = 0

    # ============== Live Flight Tracking Methods ==============

    def add_live_flight(self, flight_data: dict):
        """Add a new live flight to the globe"""
        icao24 = flight_data['icao24']
        self.live_flights[icao24] = flight_data

        if self.debug:
            label = flight_data.get('label', flight_data.get('callsign', icao24))
            logger.info(f"🛫 Added live flight: {label}")

        self.update()  # Trigger redraw

    def update_live_flight(self, flight_data: dict):
        """Update position of an existing live flight"""
        icao24 = flight_data['icao24']

        if icao24 in self.live_flights:
            # Update flight data
            self.live_flights[icao24] = flight_data

            if self.debug:
                lat = flight_data['latitude']
                lon = flight_data['longitude']
                alt = flight_data.get('altitude_ft', 0)
                logger.debug(f"📍 Updated flight {icao24}: ({lat:.2f}, {lon:.2f}) @ {alt:.0f}ft")

            self.update()  # Trigger redraw

    def remove_live_flight(self, icao24: str):
        """Remove a live flight from the globe (landed or out of range)"""
        if icao24 in self.live_flights:
            del self.live_flights[icao24]

            if self.debug:
                logger.debug(f"🛬 Removed live flight: {icao24}")

            self.update()  # Trigger redraw

    def clear_live_flights(self):
        """Clear all live flights"""
        self.live_flights.clear()

        if self.debug:
            logger.debug("🧹 Cleared all live flights")

        self.update()  # Trigger redraw

    def render_flight_routes(self, mvp, model=None, camera_pos=None):
        """Team-colored great-circle arcs for tracked charters
        (origin -> destination from schedule correlation)."""
        if not self.travel_shader:
            return

        # Arcs are opt-in (click a plane to toggle); drop toggles for landed
        # flights and prune unneeded GL buffers (needs GL context — here)
        self._shown_route_icaos &= set(self.live_flights)
        stale = [k for k in self._flight_route_cache
                 if k not in self.live_flights or k not in self._shown_route_icaos]
        for k in stale:
            vao, vbo, _count, _color = self._flight_route_cache.pop(k)
            try:
                gl.glDeleteBuffers(1, [vbo])
                gl.glDeleteVertexArrays(1, [vao])
            except Exception:
                pass

        if not self._shown_route_icaos:
            return

        # Build missing arcs (only for toggled-on flights)
        for icao24 in self._shown_route_icaos:
            fd = self.live_flights[icao24]
            if icao24 in self._flight_route_cache:
                continue
            o_lat, o_lon = fd.get('origin_lat'), fd.get('origin_lon')
            d_lat, d_lon = fd.get('dest_lat'), fd.get('dest_lon')
            if None in (o_lat, o_lon, d_lat, d_lon):
                continue
            points = self.generate_great_circle_path(o_lat, o_lon, d_lat, d_lon, 60)
            if len(points) < 2:
                continue
            arr = np.array(points, dtype=np.float32).flatten()
            vao = gl.glGenVertexArrays(1)
            vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, arr.nbytes, arr, gl.GL_STATIC_DRAW)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            color = self.get_team_color((fd.get('team_id') or '').upper())
            self._flight_route_cache[icao24] = (vao, vbo, len(points), color)

        if not self._flight_route_cache:
            return

        gl.glEnable(gl.GL_LINE_SMOOTH)
        gl.glLineWidth(1.5)
        gl.glDisable(gl.GL_DEPTH_TEST)

        self.travel_shader.bind()
        self.travel_shader.setUniformValue("mvp", mvp)
        self.travel_shader.setUniformValue(
            "model", model if model is not None else QMatrix4x4())
        self.travel_shader.setUniformValue(
            "cameraPos", camera_pos if camera_pos is not None else QVector3D(0, 0, 3))

        # Two-tone transparency split at the plane's position: the flown
        # portion is a faint ghost, the portion ahead is slightly stronger —
        # shows direction without the full-strength spaghetti of the
        # single-team travel paths
        for icao24, (vao, _vbo, count, color) in self._flight_route_cache.items():
            fd = self.live_flights.get(icao24, {})
            progress = fd.get('route_progress')
            split = int(max(1, min(count - 1, (progress or 0.0) * count)))

            self.travel_shader.setUniformValue("pathColor", QVector3D(*color))
            gl.glBindVertexArray(vao)
            if progress is not None:
                self.travel_shader.setUniformValue("pathAlpha", 0.14)
                gl.glDrawArrays(gl.GL_LINE_STRIP, 0, split + 1)      # flown
                self.travel_shader.setUniformValue("pathAlpha", 0.45)
                gl.glDrawArrays(gl.GL_LINE_STRIP, split, count - split)  # ahead
            else:
                self.travel_shader.setUniformValue("pathAlpha", 0.25)
                gl.glDrawArrays(gl.GL_LINE_STRIP, 0, count)
        gl.glBindVertexArray(0)

        self.travel_shader.release()
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_LINE_SMOOTH)

    def render_live_flights(self, mvp):
        """Render all tracked live flights as airplanes"""
        if not self.live_flights:
            return

        if not self.plane_model or not self.plane_model.is_ready():
            # Fallback: render as simple markers if airplane model not loaded
            self.render_live_flights_as_markers(mvp)
            return

        import math
        from PyQt6.QtGui import QVector3D, QMatrix4x4

        # Render each live flight with airplane model
        for icao24, flight_data in self.live_flights.items():
            try:
                lat = flight_data['latitude']
                lon = flight_data['longitude']
                altitude_ft = flight_data.get('altitude_ft', 35000)
                heading = flight_data.get('heading', 0)
                team_id = flight_data.get('team_id')

                # Convert to 3D position (higher altitude for live flights)
                # Scale altitude: ground = 1.0, 40000ft = 1.12
                altitude_scale = 1.0 + (altitude_ft / 40000.0) * 0.12
                position_3d = self.lat_lon_to_3d(lat, lon, altitude_scale)

                # Calculate orientation matrix from heading
                heading_rad = math.radians(heading)

                # Position normal (points away from earth center) - this is "up" on the globe surface
                up = QVector3D(*position_3d).normalized()

                # Calculate east direction at this point (perpendicular to up and world Y)
                world_up = QVector3D(0, 1, 0)

                # Handle poles (where up is parallel to world_up)
                if abs(QVector3D.dotProduct(up, world_up)) > 0.999:
                    east = QVector3D(1, 0, 0)
                else:
                    east = QVector3D.crossProduct(world_up, up).normalized()

                # North direction on the surface (perpendicular to both up and east)
                north = QVector3D.crossProduct(up, east).normalized()

                # Calculate forward direction based on heading (clockwise from north)
                forward = (
                    north * math.cos(heading_rad) +
                    east * math.sin(heading_rad)
                ).normalized()

                # Right direction for proper orientation
                right = QVector3D.crossProduct(forward, up).normalized()

                # Recalculate up to ensure perfect orthogonality
                up = QVector3D.crossProduct(right, forward).normalized()

                # Build orientation matrix - MUST match _create_flight_orientation format
                # Airplane model: +X forward (nose), +Y up, +Z right
                from PyQt6.QtGui import QVector4D
                orientation = QMatrix4x4()
                orientation.setRow(0, QVector4D(forward.x(), up.x(), right.x(), 0.0))
                orientation.setRow(1, QVector4D(forward.y(), up.y(), right.y(), 0.0))
                orientation.setRow(2, QVector4D(forward.z(), up.z(), right.z(), 0.0))
                orientation.setRow(3, QVector4D(0.0, 0.0, 0.0, 1.0))

                # Get team color for high-confidence flights
                confidence = flight_data.get('confidence', 50)
                team_color = None
                if team_id and confidence >= 65:
                    team_color = self.get_team_color(team_id.upper())

                # Create animation state dict for render_animated_airplane.
                # Live charters render at ~45% model scale: several planes can
                # share a region and the full-size model swamps the map.
                # Brightness pulses softly, desynced per aircraft.
                phase = (sum(map(ord, icao24)) % 628) / 100.0
                pulse = 1.0 + 0.30 * max(0.0, math.sin(self.animation_time * 2.2 + phase))
                animation_state = {
                    'position': position_3d,
                    'orientation': orientation,
                    'team_id': team_id,
                    'team_color': team_color,
                    'confidence': confidence,
                    'is_live_flight': True,
                    'scale': 0.45,
                    'brightness': pulse,
                }

                # Render airplane using existing method
                self.plane_model.render_animated_airplane(animation_state, mvp)

            except Exception as e:
                if self.debug:
                    logger.warning(f"⚠️  Error rendering live flight {icao24}: {e}")
                continue

    def render_live_flights_as_markers(self, mvp):
        """Fallback: render live flights as colored cube markers when airplane model unavailable"""
        if not self.marker_shader or not self.live_flights:
            return

        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_DEPTH_TEST)

        self.marker_shader.bind()
        self.marker_shader.setUniformValue("mvp", mvp)
        self.marker_shader.setUniformValue("time", self.animation_time)

        for icao24, flight_data in self.live_flights.items():
            try:
                lat = flight_data['latitude']
                lon = flight_data['longitude']
                altitude_ft = flight_data.get('altitude_ft', 35000)
                team_id = flight_data.get('team_id')
                confidence = flight_data.get('confidence', 50)

                # Slightly elevated position based on altitude
                altitude_scale = 1.0 + (altitude_ft / 40000.0) * 0.12
                position_3d = self.lat_lon_to_3d(lat, lon, altitude_scale)

                # Determine marker type based on confidence
                if confidence >= 80:
                    marker_type = 'live_flight_confirmed'
                elif confidence >= 65:
                    marker_type = 'live_flight_likely'
                else:
                    marker_type = 'live_flight_possible'

                # Try to infer league from team_id if available
                league = None
                if team_id:
                    league = self.infer_league_from_team_id(team_id)

                # Create marker dict for rendering
                live_marker = {
                    'position': position_3d,
                    'team_id': team_id or icao24,
                    'league': league or '',
                    'size': 3.0 + (confidence / 50.0),  # Larger for higher confidence
                    'type': marker_type,
                    'is_live_flight': True,
                    'icao24': icao24,
                    'confidence': confidence
                }

                # Render with faster rotation to indicate live tracking
                self.render_single_marker(live_marker, rotation_speed=2.5)

            except Exception as e:
                if self.debug:
                    logger.warning(f"⚠️  Error rendering live flight marker {icao24}: {e}")
                continue

        self.marker_shader.release()

    # ============== End Live Flight Tracking Methods ==============

    def closeEvent(self, event):
        """Clean up OpenGL resources"""
        if self.gl_initialized:
            self.makeCurrent()
            
            # Clean up resources
            if self.marker_vao:
                gl.glDeleteVertexArrays(1, [self.marker_vao])
            if self.marker_vbo:
                gl.glDeleteBuffers(1, [self.marker_vbo])
            if self.vao:
                gl.glDeleteVertexArrays(1, [self.vao])
            if self.vbo:
                gl.glDeleteBuffers(1, [self.vbo])
            if self.ebo:
                gl.glDeleteBuffers(1, [self.ebo])
            
            # Clean up textures
            for texture in self.team_logo_textures.values():
                if texture:
                    texture.destroy()
            
            if self.default_marker_texture:
                self.default_marker_texture.destroy()
            
            if self.earth_texture:
                self.earth_texture.destroy()
            
            self.plane_model.cleanup_resources()
            self.doneCurrent()
        
        super().closeEvent(event)
                




class AirplaneGeometry:
    """Optimized geometry loading and data management - no OpenGL dependencies"""
    
    def __init__(self):
        self.vertices = None
        self.vertex_count = 0
        self.geometry_data = None
        self.bounding_box = None
        self.is_loaded = False
        
        # Performance caches
        self._normals_cache = {}
        self._face_cache = {}
    
    def load_obj_model(self, filepath: str) -> bool:
        """Load .obj file with optimized parsing and validation"""
        try:
            vertices = []
            normals = []
            faces = []
            
            logger.debug(f"📁 Loading airplane model: {filepath}")
            
            with open(filepath, 'r') as file:
                lines = file.readlines()
                
            # Parse in chunks for better performance
            for line_num, line in enumerate(lines, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                    
                parts = line.split()
                if not parts:
                    continue
                    
                try:
                    command = parts[0]
                    
                    if command == 'v' and len(parts) >= 4:  # Vertex position
                        x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                        vertices.append([x, y, z])
                    
                    elif command == 'vn' and len(parts) >= 4:  # Vertex normal
                        x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                        normals.append([x, y, z])
                    
                    elif command == 'f':  # Face - optimized parsing
                        face_vertices = []
                        for vertex_data in parts[1:]:
                            vertex_idx = vertex_data.split('/')[0]
                            if vertex_idx:
                                face_vertices.append(int(vertex_idx) - 1)  # OBJ is 1-indexed
                        
                        if len(face_vertices) >= 3:
                            faces.append(face_vertices)
                
                except (ValueError, IndexError) as e:
                    logger.warning(f"⚠️ Skipping invalid line {line_num}: {line} ({e})")
                    continue
            
            # Validation
            if not vertices or not faces:
                logger.error(f"❌ Invalid .obj file: no vertices or faces found")
                return False
            
            logger.debug(f"📊 Loaded: {len(vertices)} vertices, {len(normals)} normals, {len(faces)} faces")
            
            # Generate normals if none provided
            if not normals:
                normals = self._generate_face_normals_vectorized(vertices, faces)
                logger.debug("🔧 Generated face normals (vectorized)")
            
            # Convert to OpenGL-ready format with optimization
            self.geometry_data = self._create_vertex_array_optimized(vertices, normals, faces)
            if self.geometry_data is None:
                return False
                
            self.vertex_count = len(self.geometry_data) // 6  # 3 position + 3 normal per vertex
            self.bounding_box = self._calculate_bounding_box_fast(vertices)
            self.is_loaded = True
            
            logger.info(f"✅ Airplane geometry loaded: {self.vertex_count} vertices")
            return True
            
        except FileNotFoundError:
            logger.error(f"❌ Airplane model file not found: {filepath}")
            return False
        except Exception as e:
            logger.error(f"❌ Failed to load airplane model: {e}")
            return False
    
    def _generate_face_normals_vectorized(self, vertices, faces):
        """Generate normals using vectorized operations for better performance"""
        import numpy as np
        
        vertex_array = np.array(vertices, dtype=np.float32)
        vertex_normals = np.zeros_like(vertex_array)
        
        # Process faces in batches for efficiency
        for face in faces:
            if len(face) >= 3:
                # Get triangle vertices
                v0, v1, v2 = vertex_array[face[0]], vertex_array[face[1]], vertex_array[face[2]]
                
                # Calculate face normal using cross product
                edge1 = v1 - v0
                edge2 = v2 - v0
                face_normal = np.cross(edge1, edge2)
                
                # Normalize
                norm = np.linalg.norm(face_normal)
                if norm > 0:
                    face_normal /= norm
                
                # Add to vertex normals (average for shared vertices)
                for vertex_idx in face:
                    vertex_normals[vertex_idx] += face_normal
        
        # Normalize all vertex normals
        norms = np.linalg.norm(vertex_normals, axis=1, keepdims=True)
        norms[norms == 0] = 1  # Prevent division by zero
        vertex_normals /= norms
        
        return vertex_normals.tolist()
    
    def _create_vertex_array_optimized(self, vertices, normals, faces):
        """Create optimized vertex array with proper memory layout"""
        import numpy as np
        
        try:
            vertex_data = []
            
            # Pre-allocate for efficiency
            total_vertices = sum(len(face) - 2 for face in faces) * 3  # Triangulated
            vertex_data = np.zeros((total_vertices, 6), dtype=np.float32)
            
            vertex_idx = 0
            
            for face in faces:
                if len(face) < 3:
                    continue
                
                # Triangulate face (simple fan triangulation)
                for i in range(1, len(face) - 1):
                    for j, vertex_index in enumerate([face[0], face[i], face[i + 1]]):
                        if vertex_index < len(vertices) and vertex_index < len(normals):
                            # Position
                            vertex_data[vertex_idx, 0:3] = vertices[vertex_index]
                            # Normal
                            vertex_data[vertex_idx, 3:6] = normals[vertex_index]
                            vertex_idx += 1
            
            # Trim to actual size
            return vertex_data[:vertex_idx].flatten()
            
        except Exception as e:
            logger.error(f"❌ Failed to create vertex array: {e}")
            return None
    
    def _calculate_bounding_box_fast(self, vertices):
        """Fast bounding box calculation using numpy"""
        import numpy as np
        
        if not vertices:
            return None
            
        vertices_array = np.array(vertices, dtype=np.float32)
        min_coords = np.min(vertices_array, axis=0)
        max_coords = np.max(vertices_array, axis=0)
        
        return {
            'min': min_coords,
            'max': max_coords,
            'size': max_coords - min_coords,
            'center': (max_coords + min_coords) / 2
        }


class PlaneModel:
    """Optimized airplane model with proper curve following and orientation"""
    
    def __init__(self, parent_widget):
        self.parent_widget = parent_widget
        
        # Geometry management
        self.geometry = AirplaneGeometry()
        
        # OpenGL resources
        self.vao = None
        self.vbo = None
        
        # Configuration
        self.airplane_scale = 0.05
        self.airplane_color = (0.8,0.85,0.9)
        
        # Performance caches
        self._orientation_cache = {}
        self._transform_cache = None
        self._last_animation_state = None
        
        # Flight dynamics
        self.banking_factor = 0.5  # How much to bank in turns
        self.max_bank_angle = 25.0  # Maximum bank angle in degrees
        self.orientation_smoothing = 0.85  # Smoothing factor for orientation changes
        
        # State
        self.is_loaded = False
    
    def setup_airplane_model(self) -> bool:
        """
        NOTE, THE AIRPLANE MODEL IS EXPORTED FROM BLENDER WITH A 90
        DEGREE ROTATION ON BOTH THE X AND Z AXES TO ACCOUNT FOR COORDINATE SYSTEM MISMATCH WOOPTY 
        """
        # Find airplane model file
        possible_files = ["pj_airplane_0degx.obj", "paper_airplane.obj"]
        model_path = None
        
        plane_dir = Path(__file__).resolve().parent / "plane_model"
        for filename in possible_files:
            test_path = plane_dir / filename
            if test_path.exists():
                model_path = test_path
                logger.info(f"✅ Found airplane model: {model_path}")
                break
        
        if not model_path:
            logger.warning(f"⚠️ No airplane model found in plane_model/ directory")
            logger.debug(f"   Looked for: {possible_files}")
            return False
        
        # Load geometry
        if not self.geometry.load_obj_model(str(model_path)):
            return False
        
        # Create OpenGL buffers
        if not self._create_opengl_buffers():
            return False
        
        self.is_loaded = True
        logger.info("✅ Airplane model setup complete")
        return True
    
    def is_ready(self) -> bool:
        """Check if model is ready for rendering"""
        return (self.is_loaded and 
                self.vao is not None and 
                self.vbo is not None and 
                self.geometry.geometry_data is not None)
    
    def render_animated_airplane(self, animation_state: Dict, mvp: QMatrix4x4,
                               shader_program=None) -> bool:
        """Optimized rendering method - uses pre-calculated orientation and team color"""
        if not self.is_ready() or not animation_state:
            return False

        # Get pre-calculated position and orientation
        position = animation_state.get('position')
        orientation = animation_state.get('orientation')

        if not position:
            return False

        # Use identity matrix if no orientation provided (fallback)
        if orientation is None:
            orientation = QMatrix4x4()

        # Get team color if available
        team_color = animation_state.get('team_color')
        confidence = animation_state.get('confidence', 50)

        # Blend team color with base gray based on confidence
        if team_color and confidence >= 65:
            # Higher confidence = more team color
            blend = min(1.0, (confidence - 50) / 50.0)  # 0-1 scale from 50-100%
            base = self.airplane_color
            color = (
                base[0] * (1 - blend) + team_color[0] * blend,
                base[1] * (1 - blend) + team_color[1] * blend,
                base[2] * (1 - blend) + team_color[2] * blend
            )
        else:
            color = self.airplane_color

        # Soft pulse for tracked charters (brightness throb)
        brightness = animation_state.get('brightness', 1.0)
        if brightness != 1.0:
            color = (min(1.0, color[0] * brightness),
                     min(1.0, color[1] * brightness),
                     min(1.0, color[2] * brightness))

        # Create transformation matrix (cached if state unchanged)
        model_matrix = self._get_cached_transform_matrix(
            position, orientation, animation_state.get('scale', 1.0))
        final_mvp = mvp * model_matrix

        # Render airplane with color
        return self._render_airplane(final_mvp, model_matrix, color)
    
    def calculate_smooth_orientation_from_path(self, path_points: List, segment_progress: float) -> QMatrix4x4:
        """Calculate smooth orientation using curve tangent (called from animation)"""
        if not path_points or len(path_points) < 2:
            return QMatrix4x4()
        
        # Create cache key for this calculation
        cache_key = (id(path_points), round(segment_progress, 3))
        if cache_key in self._orientation_cache:
            return self._orientation_cache[cache_key]
        
        # Calculate curve tangent direction (not next point!)
        tangent_vector = self._calculate_curve_tangent(path_points, segment_progress)
        
        # Get current position for up vector calculation
        current_pos = self._interpolate_position_on_path(path_points, segment_progress)
        
        # Calculate banking for realistic flight dynamics
        curvature = self._estimate_path_curvature(path_points, segment_progress)
        bank_angle = min(self.max_bank_angle, curvature * self.banking_factor)
        
        # Create orientation matrix with banking
        orientation = self._create_flight_orientation(tangent_vector, current_pos, bank_angle)
        
        # Cache result
        self._orientation_cache[cache_key] = orientation
        
        return orientation
    
    def _calculate_curve_tangent(self, path_points: List, t: float) -> QVector3D:
        """Calculate tangent vector from curve, not discrete points"""
        if len(path_points) < 2:
            return QVector3D(-1, 0, 0)  # Default forward direction
        
        # Convert to parameter space
        point_float = t * (len(path_points) - 1)
        point_idx = int(point_float)
        local_t = point_float - point_idx
        
        if point_idx >= len(path_points) - 1:
            # At end of path - use direction of last segment
            p1, p2 = path_points[-2], path_points[-1]
            tangent = np.array(p2) - np.array(p1)
        elif point_idx == 0 and len(path_points) > 2:
            # At start - use weighted combination for smoother start
            p0, p1, p2 = path_points[0], path_points[1], path_points[2]
            tangent = np.array(p1) - np.array(p0) + 0.5 * (np.array(p2) - np.array(p1))
        else:
            # In middle - interpolate between adjacent segments for smooth curves
            p1, p2 = path_points[point_idx], path_points[point_idx + 1]
            tangent = np.array(p2) - np.array(p1)
            
            # Add curvature information if available
            if point_idx > 0 and point_idx < len(path_points) - 1:
                prev_tangent = np.array(path_points[point_idx]) - np.array(path_points[point_idx - 1])
                # Blend with previous tangent for smoother curves
                tangent = (1 - local_t) * prev_tangent + local_t * tangent
        
        # Normalize and convert to Qt
        norm = np.linalg.norm(tangent)
        if norm > 0:
            tangent = tangent / norm
        
        return QVector3D(float(tangent[0]), float(tangent[1]), float(tangent[2]))
    
    def _interpolate_position_on_path(self, path_points: List, t: float) -> tuple:
        """Get current position on path for up vector calculation"""
        if not path_points:
            return (0, 0, 0)
        
        if t <= 0:
            return path_points[0]
        if t >= 1:
            return path_points[-1]
        
        point_float = t * (len(path_points) - 1)
        point_idx = int(point_float)
        local_t = point_float - point_idx
        
        if point_idx >= len(path_points) - 1:
            return path_points[-1]
        
        # Linear interpolation between points
        p1, p2 = path_points[point_idx], path_points[point_idx + 1]
        return (
            p1[0] + (p2[0] - p1[0]) * local_t,
            p1[1] + (p2[1] - p1[1]) * local_t,
            p1[2] + (p2[2] - p1[2]) * local_t
        )
    
    
    def _estimate_path_curvature(self, path_points: List, t: float) -> float:
        """Estimate curvature at current position for banking calculation"""
        if len(path_points) < 3:
            return 0.0
        
        point_float = t * (len(path_points) - 1)
        point_idx = int(point_float)
        
        # Get three points for curvature calculation
        if point_idx == 0:
            p1, p2, p3 = path_points[0], path_points[1], path_points[2]
        elif point_idx >= len(path_points) - 2:
            p1, p2, p3 = path_points[-3], path_points[-2], path_points[-1]
        else:
            p1, p2, p3 = path_points[point_idx], path_points[point_idx + 1], path_points[point_idx + 2]
        
        # Calculate curvature using three points
        a = np.array(p1, dtype=np.float64)  # FIXED: Explicit float type
        b = np.array(p2, dtype=np.float64)
        c = np.array(p3, dtype=np.float64)
        
        # Vectors
        ab = b - a
        bc = c - b
        
        # Cross product magnitude gives curvature indication
        cross = np.cross(ab, bc)
        if hasattr(cross, '__len__'):
            curvature = float(np.linalg.norm(cross))  # FIXED: Explicit float conversion
        else:
            curvature = float(abs(cross))
        
        # Normalize by segment length
        ab_length = float(np.linalg.norm(ab))  # FIXED: Explicit float conversion
        bc_length = float(np.linalg.norm(bc))  # FIXED: Explicit float conversion
        
        if ab_length > 0 and bc_length > 0:
            curvature /= (ab_length * bc_length)
        
        return float(curvature * 10.0)  # Scale factor for banking
    
    def _create_flight_orientation(self, forward: QVector3D, position: tuple, bank_angle: float) -> QMatrix4x4:
        """Create realistic flight orientation with banking - FIXED VERSION"""
        
        # Ensure forward vector is normalized
        forward = forward.normalized()
        
        # Get up vector for globe surface (points away from center)
        position_vec = QVector3D(*position)
        globe_up = position_vec.normalized()
        
        # Calculate right vector (perpendicular to both forward and globe_up)
        right = QVector3D.crossProduct(forward, globe_up)
        if right.length() < 0.001:  # Handle edge case where forward is parallel to globe_up
            # Use a different reference vector
            alt_up = QVector3D(0, 1, 0) if abs(forward.y()) < 0.9 else QVector3D(1, 0, 0)
            right = QVector3D.crossProduct(forward, alt_up)
        right = right.normalized()
        
        # Recalculate up to ensure perfect orthogonality
        up = QVector3D.crossProduct(right, forward).normalized()
        
        # Create rotation matrix manually
        # This matrix will transform from airplane space to world space
        # Airplane default: +X forward, +Y up, +Z right
        
        orientation = QMatrix4x4()
        
        # Set the rotation matrix columns
        # Column 0: where airplane's +X axis (forward) points in world space
        # Column 1: where airplane's +Y axis (up) points in world space  
        # Column 2: where airplane's +Z axis (right) points in world space
        
        # METHOD 1: Use setRow to build the matrix
        orientation.setRow(0, QVector4D(forward.x(), up.x(), right.x(), 0.0))
        orientation.setRow(1, QVector4D(forward.y(), up.y(), right.y(), 0.0))
        orientation.setRow(2, QVector4D(forward.z(), up.z(), right.z(), 0.0))
        orientation.setRow(3, QVector4D(0.0, 0.0, 0.0, 1.0))
        
        # Apply banking rotation around forward axis
        if abs(bank_angle) > 0.1:
            bank_rotation = QMatrix4x4()
            bank_rotation.rotate(bank_angle, forward)
            orientation = bank_rotation * orientation
        
        return orientation
    
    def _get_cached_transform_matrix(self, position: tuple, orientation: QMatrix4x4,
                                     scale: float = 1.0) -> QMatrix4x4:
        """Get cached transformation matrix if state unchanged"""
        current_state = (position, orientation, scale)

        # Simple cache check - in production you'd use better hashing
        if (self._last_animation_state == current_state and
            self._transform_cache is not None):
            return self._transform_cache

        # Create new transformation matrix
        transform = QMatrix4x4()

        # 1. Translate to world position
        transform.translate(*position)

        # 2. Apply orientation
        transform *= orientation

        # 3. Scale
        transform.scale(self.airplane_scale * scale)
        
        # Cache for next frame
        self._transform_cache = transform
        self._last_animation_state = current_state
        
        return transform
    
    def _create_opengl_buffers(self) -> bool:
        """Create VAO/VBO for airplane geometry"""
        if self.geometry.geometry_data is None or len(self.geometry.geometry_data) == 0:
            logger.error("❌ No geometry data for airplane buffers")
            return False
        
        # Generate VAO and VBO
        self.vao = gl.glGenVertexArrays(1)
        self.vbo = gl.glGenBuffers(1)
        
        if self.vao == 0 or self.vbo == 0:
            logger.error("❌ Failed to create airplane OpenGL buffers")
            return False
        
        # Bind and upload data
        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, self.geometry.geometry_data.nbytes, 
                       self.geometry.geometry_data, gl.GL_STATIC_DRAW)
        
        stride = 6 * 4  # 3 position + 3 normal = 6 floats * 4 bytes
        
        # Position attribute (location 0)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(0))
        
        # Normal attribute (location 1)  
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(12))
        
        # Unbind
        gl.glBindVertexArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        
        logger.info("✅ Airplane OpenGL buffers created")
        return True
    
    def _render_airplane(self, mvp: QMatrix4x4, model: QMatrix4x4, color: tuple = None) -> bool:
        """Internal rendering method with optional team color"""
        shader = self.parent_widget.airplane_shader
        if not shader:
            return False

        # Use passed color or default
        render_color = color if color else self.airplane_color

        # Temporarily disable face culling for debugging
        gl.glDisable(gl.GL_CULL_FACE)

        shader.bind()

        try:
            shader.setUniformValue("mvp", mvp)
            shader.setUniformValue("model", model)
            shader.setUniformValue("normalMatrix", model.normalMatrix())
            shader.setUniformValue("lightDir", QVector3D(1.0, 0.3, 0.5).normalized())
            shader.setUniformValue("airplaneColor", QVector3D(*render_color))
            
            # Render
            gl.glBindVertexArray(self.vao)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, self.geometry.vertex_count)
            gl.glBindVertexArray(0)
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to render airplane: {e}")
            return False
        finally:
            shader.release()
            gl.glEnable(gl.GL_CULL_FACE)  # Re-enable face culling
    
    def cleanup_resources(self):
        """Clean up OpenGL resources"""
        if self.vao:
            gl.glDeleteVertexArrays(1, [self.vao])
            self.vao = None
        
        if self.vbo:
            gl.glDeleteBuffers(1, [self.vbo])
            self.vbo = None
        
        # Clear caches
        self._orientation_cache.clear()
        self._transform_cache = None
        self._last_animation_state = None
        
        logger.info("✅ Airplane resources cleaned up")
