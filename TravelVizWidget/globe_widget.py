import sys
import math
import time
import numpy as np
import OpenGL.GL as gl
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime, timedelta
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture
from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal
from PyQt6.QtGui import (QMatrix4x4, QVector3D, QQuaternion, QMouseEvent, 
                        QWheelEvent, QColor, QImage)
from pathlib import Path


class TeamTravelAnimation:
    """Manages animated team travel sequences - Essential for UI"""
    
    def __init__(self, parent_widget=None):
        self.parent_widget = parent_widget
        self.team_sequences = {}
        self.current_team = None
        self.animation_active = True
        self.animation_progress = 0.0
        self.animation_speed = 1.0
        self.current_segment = 0
        self.segment_progress = 0.0
        self.loop_animation = True
        
    def build_team_sequence(self, team_id: str, travel_data: List, season_start: datetime = None) -> Dict:
        """Build chronological travel sequence for a team"""
        if season_start is None:
            season_start = datetime(datetime.now().year, 3, 1)
        
        today = datetime.now()
        
        # Filter and sort team travel by date
        team_travel = [t for t in travel_data 
                      if hasattr(t, 'team_id') and t.team_id.upper() == team_id.upper() 
                      and hasattr(t, 'travel_date') and t.travel_date
                      and season_start <= t.travel_date <= today]
        
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
            'season_start': season_start,
            'last_update': today
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
        """Update animation state and return current position"""
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
        
        return {
            'position': current_position,
            'segment': current_seg,
            'segment_index': self.current_segment,
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


class FlightGlobeWidget(QOpenGLWidget):
    """3D Globe Widget for sports team travel visualization - Simplified"""
    
    # Signals
    locationSelected = pyqtSignal(float, float, str)
    flightSelected = pyqtSignal(str)
    animationStatusChanged = pyqtSignal(bool, str)
    animationProgressChanged = pyqtSignal(float, dict)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # OpenGL resources
        self.shader_program = None
        self.travel_shader = None
        self.marker_shader = None
        self.vao = None
        self.vbo = None
        self.ebo = None
        self.earth_texture = None
        
        # Marker resources
        self.marker_vao = None
        self.marker_vbo = None
        self.marker_geometry = None
        
        # Team logo textures
        self.team_logo_textures = {}
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
        self.max_zoom = 5.0
        
        # Mouse interaction
        self.last_mouse_pos = QPoint()
        self.is_rotating = False
        self.rotation_speed = 1.0
        self.rotation_momentum = QVector3D(0, 0, 0)
        self.momentum_decay = 0.95
        
        # Animation and timing
        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self.update)
        self.animation_time = 0.0
        self.last_frame_time = time.time()
        
        # Sports travel data
        self.travel_data = []
        self.travel_paths = []
        self.team_city_markers = []
        
        # Team travel animation system (Required by UI)
        self.travel_animation = TeamTravelAnimation(self)
        self.animated_marker_position = None
        self.show_travel_animation = False
        self.disable_spinning_during_animation = True
        
        # Display options
        self.show_travel_paths = True
        self.show_team_cities = True
        self.show_atmosphere = True
        
        # OpenGL state tracking
        self.gl_initialized = False
        
        # Initialize default view
        self.setup_default_view()
        self.setMouseTracking(True)
        self.render_timer.start(16)  # 60 FPS
    
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
        
        self.gl_initialized = bool(self.marker_vao)
        print("✅ Globe OpenGL initialized" if self.gl_initialized else "❌ Globe OpenGL failed")
    
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
    
    def setup_shaders(self):
        """Setup simplified OpenGL shaders"""
        # Earth shader
        earth_vertex = """
        #version 330 core
        layout (location = 0) in vec3 position;
        layout (location = 1) in vec2 texCoord;
        layout (location = 2) in vec3 normal;
        
        uniform mat4 mvp;
        uniform mat4 model;
        uniform mat3 normalMatrix;
        uniform vec3 lightDir;
        
        out vec2 TexCoord;
        out float LightIntensity;
        
        void main() {
            gl_Position = mvp * vec4(position, 1.0);
            TexCoord = texCoord;
            vec3 Normal = normalize(normalMatrix * normal);
            LightIntensity = max(dot(Normal, normalize(lightDir)), 0.0);
        }
        """
        
        earth_fragment = """
        #version 330 core
        in vec2 TexCoord;
        in float LightIntensity;
        
        out vec4 FragColor;
        
        uniform sampler2D earthTexture;
        uniform bool showAtmosphere;
        
        void main() {
            vec3 earthColor = texture(earthTexture, TexCoord).rgb;
            float ambient = 0.15;
            float diffuse = LightIntensity * 0.85;
            vec3 litColor = earthColor * (ambient + diffuse);
            
            FragColor = vec4(litColor, 1.0);
        }
        """
        
        self.shader_program = QOpenGLShaderProgram()
        self.shader_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, earth_vertex)
        self.shader_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, earth_fragment)
        self.shader_program.link()
        
        # Travel path shader
        travel_vertex = """
        #version 330 core
        layout (location = 0) in vec3 position;
        
        uniform mat4 mvp;
        uniform vec3 pathColor;
        uniform float pathAlpha;
        
        out vec3 Color;
        out float Alpha;
        
        void main() {
            gl_Position = mvp * vec4(position, 1.0);
            Color = pathColor;
            Alpha = pathAlpha;
        }
        """
        
        travel_fragment = """
        #version 330 core
        in vec3 Color;
        in float Alpha;
        
        out vec4 FragColor;
        
        void main() {
            FragColor = vec4(Color, Alpha);
        }
        """
        
        self.travel_shader = QOpenGLShaderProgram()
        self.travel_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, travel_vertex)
        self.travel_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, travel_fragment)
        self.travel_shader.link()
        
        # Enhanced marker shader with split cube support
        marker_vertex = """
        #version 330 core
        layout (location = 0) in vec3 position;
        layout (location = 1) in vec3 normal;
        layout (location = 2) in vec2 texCoord;
        
        uniform mat4 mvp;
        uniform vec3 markerCenter;
        uniform float markerSize;
        uniform float time;
        uniform float rotationSpeed;
        uniform bool isAnimated;
        uniform bool isSplitCube;
        
        out vec2 MarkerTexCoord;
        out float LightIntensity;
        out vec3 WorldPosition;  // For split cube detection
        
        void main() {
            float rotY = time * rotationSpeed;
            float rotX = time * rotationSpeed * 0.7;
            
            if (isAnimated) {
                rotY *= 2.0;
                rotX *= 1.5;
            }
            
            mat3 rotationY = mat3(
                cos(rotY), 0.0, sin(rotY),
                0.0, 1.0, 0.0,
                -sin(rotY), 0.0, cos(rotY)
            );
            
            mat3 rotationX = mat3(
                1.0, 0.0, 0.0,
                0.0, cos(rotX), -sin(rotX),
                0.0, sin(rotX), cos(rotX)
            );
            
            mat3 rotation = rotationY * rotationX;
            
            float animatedSize = markerSize;
            if (isAnimated) {
                float pulse = sin(time * 4.0) * 0.3 + 1.0;
                animatedSize *= pulse * 0.012;
            } else if (isSplitCube) {
                animatedSize *= 0.010;  // Slightly larger for split cubes
            } else {
                animatedSize *= 0.008;
            }
            
            vec3 rotatedPos = rotation * (position * animatedSize);
            vec3 worldPos = markerCenter + rotatedPos;
            
            gl_Position = mvp * vec4(worldPos, 1.0);
            
            vec3 MarkerNormal = rotation * normal;
            vec3 lightDir = normalize(vec3(1.0, 1.0, 1.0));
            LightIntensity = max(dot(MarkerNormal, lightDir), 0.6);
            
            MarkerTexCoord = texCoord;
            WorldPosition = rotatedPos;  // Local position for split detection
        }
        """
        
        marker_fragment = """
        #version 330 core
        in vec2 MarkerTexCoord;
        in float LightIntensity;
        in vec3 WorldPosition;  // For split cube detection
        
        out vec4 FragColor;
        
        uniform sampler2D homeTeamTexture;   // Home team texture
        uniform sampler2D awayTeamTexture;   // Away team texture
        uniform sampler2D markerTexture;     // Single texture
        uniform vec3 homeTeamColor;          // Home team color
        uniform vec3 awayTeamColor;          // Away team color
        uniform vec3 teamColor;              // Single color
        uniform bool useTexture;             // Single texture mode
        uniform bool useHomeTexture;         // Use home texture
        uniform bool useAwayTexture;         // Use away texture
        uniform bool isSplitCube;            // Split cube flag
        uniform float markerAlpha;
        uniform bool isAnimated;
        uniform float time;
        
        void main() {
            vec3 finalColor;
            float finalAlpha = markerAlpha;
            
            if (isSplitCube) {
                // Split cube logic - Top half = away team, bottom half = home team
                bool isTopHalf = WorldPosition.y > 0.0;
                
                if (isTopHalf) {
                    // Away team (top half)
                    if (useAwayTexture) {
                        vec4 texColor = texture(awayTeamTexture, MarkerTexCoord);
                        if (texColor.a > 0.01) {
                            finalColor = texColor.rgb * LightIntensity;
                            finalAlpha *= texColor.a;
                        } else {
                            finalColor = awayTeamColor * LightIntensity;
                        }
                    } else {
                        finalColor = awayTeamColor * LightIntensity;
                    }
                } else {
                    // Home team (bottom half)
                    if (useHomeTexture) {
                        vec4 texColor = texture(homeTeamTexture, MarkerTexCoord);
                        if (texColor.a > 0.01) {
                            finalColor = texColor.rgb * LightIntensity;
                            finalAlpha *= texColor.a;
                        } else {
                            finalColor = homeTeamColor * LightIntensity;
                        }
                    } else {
                        finalColor = homeTeamColor * LightIntensity;
                    }
                }
                
                // Add white border at split line
                if (abs(WorldPosition.y) < 0.02) {
                    finalColor = mix(finalColor, vec3(1.0, 1.0, 1.0), 0.15);
                }
            } else {
                // Single-team cube logic
                if (useTexture) {
                    vec4 texColor = texture(markerTexture, MarkerTexCoord);
                    if (texColor.a > 0.01) {
                        finalColor = texColor.rgb * LightIntensity;
                        finalAlpha *= texColor.a;
                    } else {
                        finalColor = teamColor * LightIntensity;
                    }
                } else {
                    finalColor = teamColor * LightIntensity;
                }
            }
            
            if (isAnimated) {
                float glow = sin(time * 3.0) * 0.4 + 0.6;
                finalColor *= glow;
                finalColor += vec3(1.0, 0.8, 0.2) * 0.3;
            }
            
            FragColor = vec4(finalColor, finalAlpha);
        }
        """
        
        self.marker_shader = QOpenGLShaderProgram()
        self.marker_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, marker_vertex)
        self.marker_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, marker_fragment)
        self.marker_shader.link()
    
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
    
    def setup_earth_texture(self):
        """Load Earth texture"""
        texture_files = ["no_ice_clouds_8k.jpg", "earth.jpg"]
        
        image = None
        for texture_file in texture_files:
            image = QImage(texture_file)
            if not image.isNull():
                break
        
        if image and not image.isNull():
            image = image.convertToFormat(QImage.Format.Format_RGB888)
            self.earth_texture = QOpenGLTexture(image)
            self.earth_texture.setMinificationFilter(QOpenGLTexture.Filter.LinearMipMapLinear)
            self.earth_texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
    
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
                    "ana": "Ducks.png", "utah": "Coyotes.png", "bos": "Bruins.png",
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
        for league, config in league_configs.items():
            logos_path = Path(config["folder"])
            
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
                            
                            # Create OpenGL texture
                            texture = QOpenGLTexture(image)
                            texture.setMinificationFilter(QOpenGLTexture.Filter.LinearMipMapLinear)
                            texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
                            texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
                            
                            self.team_logo_textures[texture_key] = texture
                            total_loaded += 1
                            
                    except Exception:
                        pass  # Silent fail for missing logos
        
        print(f"✅ Loaded {total_loaded} team logos")
        
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
        """Generate travel paths and markers from sports data"""
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
            
            # Create paths and markers for each venue change
            for travel in travel_records:
                departure_city = getattr(travel, 'departure_city', '')
                arrival_city = getattr(travel, 'arrival_city', '')
                
                if not departure_city or not arrival_city or departure_city == arrival_city:
                    continue
                
                self._create_path_and_markers(departure_city, arrival_city, team_name, 
                                            team_abbrev, league, team_color, travel, 
                                            cities_seen, alpha=0.85)
        
        print(f"✅ Generated {len(self.travel_paths)} travel paths, {len(self.team_city_markers)} city markers")
    
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
        
        print(f"🏈 Found {len(todays_games)} games today!")
        
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
                print(f"   🏟️  {game['away_team']} @ {game['home_team']} in {venue_city}")
        
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
        
        # Update travel animation
        if self.show_travel_animation:
            animation_state = self.travel_animation.update_animation(frame_time)
            if animation_state:
                self.animated_marker_position = animation_state
                self.animationProgressChanged.emit(animation_state['progress'], animation_state['segment'])
            else:
                self.animated_marker_position = None
        
        # Handle rotation momentum
        if not self.is_rotating and self.rotation_momentum.length() > 0.01:
            momentum_rotation = QMatrix4x4()
            momentum_rotation.rotate(self.rotation_momentum.length(), 
                                   self.rotation_momentum.normalized())
            self.rotation_matrix = momentum_rotation * self.rotation_matrix
            self.rotation_momentum *= self.momentum_decay
        
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        
        model = self.rotation_matrix
        view = QMatrix4x4()
        view.lookAt(QVector3D(0, 0, 3 / self.zoom_level), QVector3D(0, 0, 0), QVector3D(0, 1, 0))
        projection = QMatrix4x4()
        aspect_ratio = self.width() / max(self.height(), 1)
        projection.perspective(45.0, aspect_ratio, 0.1, 100.0)
        mvp = projection * view * model
        
        normal_matrix = model.normalMatrix()
        
        self.render_earth(mvp, model, normal_matrix)
        
        if self.show_travel_paths:
            self.render_travel_paths(mvp)
        
        if self.show_team_cities:
            self.render_markers(mvp, model)
        
        # Render animated traveling marker
        if self.show_travel_animation and self.animated_marker_position:
            self.render_animated_marker(mvp, model)
    
    def render_earth(self, mvp, model, normal_matrix):
        """Render Earth sphere"""
        if not self.shader_program or not self.vao:
            return
        
        self.shader_program.bind()
        
        self.shader_program.setUniformValue("mvp", mvp)
        self.shader_program.setUniformValue("model", model)
        self.shader_program.setUniformValue("normalMatrix", normal_matrix)
        
        light_direction = QVector3D(1.0, 0.3, 0.5).normalized()
        self.shader_program.setUniformValue("lightDir", light_direction)
        self.shader_program.setUniformValue("showAtmosphere", self.show_atmosphere)
        
        if self.earth_texture:
            self.earth_texture.bind(0)
            self.shader_program.setUniformValue("earthTexture", 0)
        
        gl.glBindVertexArray(self.vao)
        gl.glDrawElements(gl.GL_TRIANGLES, len(self.indices), gl.GL_UNSIGNED_INT, None)
        
        self.shader_program.release()
    
    def render_travel_paths(self, mvp):
        """Render travel paths"""
        if not self.travel_paths or not self.travel_shader:
            return
        
        gl.glEnable(gl.GL_LINE_SMOOTH)
        gl.glLineWidth(2.5)
        
        self.travel_shader.bind()
        self.travel_shader.setUniformValue("mvp", mvp)
        
        for path_data in self.travel_paths:
            points = path_data.get('points', [])
            color = path_data.get('color', (1.0, 0.8, 0.2))
            alpha = path_data.get('alpha', 0.9)
            
            if len(points) < 2:
                continue
            
            self.travel_shader.setUniformValue("pathColor", QVector3D(*color))
            self.travel_shader.setUniformValue("pathAlpha", alpha)
            
            path_array = np.array(points, dtype=np.float32).flatten()
            
            path_vao = gl.glGenVertexArrays(1)
            path_vbo = gl.glGenBuffers(1)
            
            gl.glBindVertexArray(path_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, path_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, path_array.nbytes, path_array, gl.GL_DYNAMIC_DRAW)
            
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            
            gl.glDrawArrays(gl.GL_LINE_STRIP, 0, len(points))
            
            gl.glDeleteBuffers(1, [path_vbo])
            gl.glDeleteVertexArrays(1, [path_vao])
        
        self.travel_shader.release()
        gl.glDisable(gl.GL_LINE_SMOOTH)
    
    def render_markers(self, mvp, model):
        """Render team markers"""
        if not self.marker_shader or not self.marker_vao or not self.team_city_markers:
            return
        
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_DEPTH_TEST)
        
        self.marker_shader.bind()
        self.marker_shader.setUniformValue("mvp", mvp)
        self.marker_shader.setUniformValue("time", self.animation_time)
        
        for marker in self.team_city_markers:
            # Determine rotation speed based on marker type
            marker_type = marker.get('type', 'generic')
            
            if marker_type in ['home_today', 'away_today']:
                # Slower rotation for today's games - easier viewing
                rotation_speed = 0.3
            else:
                # Normal rotation speed for regular markers
                rotation_speed = 1.0
            
            self.render_single_marker(marker, rotation_speed)
        
        self.marker_shader.release()
    
    def render_single_marker(self, marker, rotation_speed=1.0):
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
        self.marker_shader.setUniformValue("markerSize", size)
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
    
    # Mouse Interaction Methods
    def mousePressEvent(self, event):
        """Handle mouse press for rotation"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_rotating = True
            self.last_mouse_pos = event.pos()
            self.rotation_momentum = QVector3D(0, 0, 0)
    
    def mouseMoveEvent(self, event):
        """Handle mouse movement with momentum"""
        if self.is_rotating:
            delta = event.pos() - self.last_mouse_pos
            self.update_rotation_with_momentum(delta)
            self.last_mouse_pos = event.pos()
    
    def mouseReleaseEvent(self, event):
        """Handle mouse release"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_rotating = False
    
    def update_rotation_with_momentum(self, delta):
        """Update rotation with momentum calculation"""
        sensitivity = 0.5 * self.rotation_speed
        
        x_rotation = delta.y() * sensitivity
        y_rotation = delta.x() * sensitivity
        
        self.rotation_momentum = QVector3D(x_rotation, y_rotation, 0) * 0.1
        
        x_quat = QQuaternion.fromAxisAndAngle(QVector3D(1, 0, 0), x_rotation)
        y_quat = QQuaternion.fromAxisAndAngle(QVector3D(0, 1, 0), y_rotation)
        
        rotation_quat = y_quat * x_quat
        rotation_matrix = QMatrix4x4()
        rotation_matrix.rotate(rotation_quat)
        
        self.rotation_matrix = rotation_matrix * self.rotation_matrix
        self.update()
    
    def wheelEvent(self, event):
        """Handle mouse wheel for zooming"""
        delta = event.angleDelta().y()
        zoom_factor = 1.1 if delta > 0 else 0.9
        new_zoom = self.zoom_level * zoom_factor
        self.zoom_level = max(self.min_zoom, min(self.max_zoom, new_zoom))
        self.update()
    
    def reset_view(self):
        """Reset view to default"""
        self.setup_default_view()
        self.zoom_level = 1.0
        self.rotation_momentum = QVector3D(0, 0, 0)
        self.update()
    
    def resizeGL(self, width, height):
        """Handle window resize"""
        if height == 0:
            height = 1
        gl.glViewport(0, 0, width, height)
    
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
            
            self.doneCurrent()
        
        super().closeEvent(event)
