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
                         QWheelEvent, QColor, QImage, QVector4D)
from pathlib import Path
import math

#TODO: plane looks like its being flown by Denzel off the sauce. 

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
                print(f"🛩️ Calculated orientation at progress {self.segment_progress:.3f}")  # Debug
            except Exception as e:
                print(f"⚠️ Failed to calculate airplane orientation: {e}")
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
        
        
        # Plane Model
        self.plane_model = PlaneModel(self) 
        
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
        self.setup_marker_vao()
        self.plane_model.setup_airplane_model()
        
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
        """Setup all OpenGL shaders including new dedicated airplane shader"""
        
        # Earth shader (existing)
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
        if not self.shader_program.link():
            print("❌ Failed to link Earth shader")
        else:
            print("✅ Earth shader linked successfully")
        
        # Travel path shader (existing)
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
        if not self.travel_shader.link():
            print("❌ Failed to link Travel shader")
        else:
            print("✅ Travel shader linked successfully")
        
        # Enhanced marker shader (existing)
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
            WorldPosition = rotatedPos;  // Local position for split cube logic
        }
        """
        
        marker_fragment = """
        #version 330 core
        in vec2 MarkerTexCoord;
        in float LightIntensity;
        in vec3 WorldPosition;
        
        out vec4 FragColor;
        
        uniform vec3 teamColor;
        uniform vec3 homeTeamColor;
        uniform vec3 awayTeamColor;
        uniform float markerAlpha;
        uniform float time;
        uniform bool useTexture;
        uniform bool useHomeTexture;
        uniform bool useAwayTexture;
        uniform bool isSplitCube;
        uniform bool isAnimated;
        uniform sampler2D markerTexture;
        uniform sampler2D homeTeamTexture;
        uniform sampler2D awayTeamTexture;
        
        void main() {
            vec3 finalColor = teamColor;
            float finalAlpha = markerAlpha;
            
            if (isSplitCube) {
                // Split cube logic - top half vs bottom half
                if (WorldPosition.y > 0.0) {
                    // Top half - home team
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
                } else {
                    // Bottom half - away team  
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
        if not self.marker_shader.link():
            print("❌ Failed to link Marker shader")
        else:
            print("✅ Marker shader linked successfully")
        
        # NEW: Dedicated airplane shader
        airplane_vertex = """
        #version 330 core
        layout (location = 0) in vec3 position;
        layout (location = 1) in vec3 normal;
        
        uniform mat4 mvp;
        uniform mat4 model;
        uniform mat3 normalMatrix;
        uniform vec3 lightDir;
        
        out float LightIntensity;
        out vec3 FragNormal;
        
        void main() {
            gl_Position = mvp * vec4(position, 1.0);
            
            // Transform normal to world space
            vec3 worldNormal = normalize(normalMatrix * normal);
            FragNormal = worldNormal;
            
            // Calculate diffuse lighting
            LightIntensity = max(dot(worldNormal, normalize(lightDir)), 0.0);
        }
        """
        
        airplane_fragment = """
        #version 330 core
        in float LightIntensity;
        in vec3 FragNormal;
        
        out vec4 FragColor;
        
        uniform vec3 airplaneColor;
        
        void main() {
            // Ambient lighting component
            float ambient = 0.3;
            
            // Diffuse lighting component  
            float diffuse = LightIntensity * 0.7;
            
            // Combine lighting
            float totalLight = ambient + diffuse;
            
            // Apply lighting to airplane color
            vec3 litColor = airplaneColor * totalLight;
            
            // Add slight specular highlight based on normal
            float specular = pow(max(dot(FragNormal, normalize(vec3(0.0, 1.0, 0.5))), 0.0), 16.0) * 0.2;
            litColor += vec3(specular);
            
            FragColor = vec4(litColor, 1.0);
        }
        """
        
        self.airplane_shader = QOpenGLShaderProgram()
        self.airplane_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, airplane_vertex)
        self.airplane_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, airplane_fragment)
        if not self.airplane_shader.link():
            print("❌ Failed to link Airplane shader")
            self.airplane_shader = None
        else:
            print("✅ Airplane shader linked successfully")
    
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
            
            self._create_sequential_team_path(travel_records, team_name, team_abbrev, 
                                            league, team_color, cities_seen)
        
        print(f"✅ Generated {len(self.travel_paths)} travel paths, {len(self.team_city_markers)} city markers")

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
        
        # Create one path per
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
        
        # Render static cube markers only when NOT showing travel animation
        if self.team_city_markers and not self.show_travel_animation:
            for marker in self.team_city_markers:
                marker_type = marker.get('type', 'generic')
                
                if marker_type in ['home_today', 'away_today']:
                    rotation_speed = 0.3
                else:
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
    
    
    
    
    # Airplane model logic
    
    
    
    
    
    
    
    
    
    
    
    
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
    
    
    def cleanup_airplane_resources(self):
        """Clean up airplane OpenGL resources"""
        if hasattr(self, 'airplane_vao') and self.airplane_vao:
            gl.glDeleteVertexArrays(1, [self.airplane_vao])
            self.airplane_vao = 0
        
        if hasattr(self, 'airplane_vbo') and self.airplane_vbo:
            gl.glDeleteBuffers(1, [self.airplane_vbo])
            self.airplane_vbo = 0
    
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
            
            print(f"📁 Loading airplane model: {filepath}")
            
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
                    print(f"⚠️ Skipping invalid line {line_num}: {line} ({e})")
                    continue
            
            # Validation
            if not vertices or not faces:
                print(f"❌ Invalid .obj file: no vertices or faces found")
                return False
            
            print(f"📊 Loaded: {len(vertices)} vertices, {len(normals)} normals, {len(faces)} faces")
            
            # Generate normals if none provided
            if not normals:
                normals = self._generate_face_normals_vectorized(vertices, faces)
                print("🔧 Generated face normals (vectorized)")
            
            # Convert to OpenGL-ready format with optimization
            self.geometry_data = self._create_vertex_array_optimized(vertices, normals, faces)
            if self.geometry_data is None:
                return False
                
            self.vertex_count = len(self.geometry_data) // 6  # 3 position + 3 normal per vertex
            self.bounding_box = self._calculate_bounding_box_fast(vertices)
            self.is_loaded = True
            
            print(f"✅ Airplane geometry loaded: {self.vertex_count} vertices")
            return True
            
        except FileNotFoundError:
            print(f"❌ Airplane model file not found: {filepath}")
            return False
        except Exception as e:
            print(f"❌ Failed to load airplane model: {e}")
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
            print(f"❌ Failed to create vertex array: {e}")
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
        self.airplane_color = (1.0, 0.0, 0.0)
        
        # Performance caches
        self._orientation_cache = {}
        self._transform_cache = None
        self._last_animation_state = None
        
        # Flight dynamics
        self.banking_factor = 0.5  # How much to bank in turns
        self.max_bank_angle = 25.0  # Maximum bank angle in degrees
        self.orientation_smoothing = 0.85  # Smoothing factor for orientation changes
        self._previous_orientation = None
        
        # State
        self.is_loaded = False
    
    def setup_airplane_model(self) -> bool:
        """
        NOTE, THE AIRPLANE MODEL IS EXPORTED FROM BLENDER WITH A 90
        DEGREE ROTATION ON BOTH THE X AND Z AXES TO ACCOUNT FOR CORDINATE SYSTEM MISMATCH WOOPTY 
        """
        # Find airplane model file
        possible_files = ["pj_airplane_0degx.obj", "paper_airplane.obj"]
        model_path = None
        
        for filename in possible_files:
            test_path = Path("plane_model") / filename
            if test_path.exists():
                model_path = test_path
                print(f"✅ Found airplane model: {model_path}")
                break
        
        if not model_path:
            print(f"⚠️ No airplane model found in plane_model/ directory")
            print(f"   Looked for: {possible_files}")
            return False
        
        # Load geometry
        if not self.geometry.load_obj_model(str(model_path)):
            return False
        
        # Create OpenGL buffers
        if not self._create_opengl_buffers():
            return False
        
        self.is_loaded = True
        print("✅ Airplane model setup complete")
        return True
    
    def is_ready(self) -> bool:
        """Check if model is ready for rendering"""
        return (self.is_loaded and 
                self.vao is not None and 
                self.vbo is not None and 
                self.geometry.geometry_data is not None)
    
    def render_animated_airplane(self, animation_state: Dict, mvp: QMatrix4x4, 
                               shader_program=None) -> bool:
        """Optimized rendering method - uses pre-calculated orientation"""
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
        
        # Create transformation matrix (cached if state unchanged)
        model_matrix = self._get_cached_transform_matrix(position, orientation)
        final_mvp = mvp * model_matrix
        
        # Render airplane
        return self._render_airplane(final_mvp, model_matrix)
    
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
        
        # Apply smoothing to prevent jerky rotations
        if self._previous_orientation is not None:
            orientation = self._smooth_orientation_transition(self._previous_orientation, orientation)
        
        self._previous_orientation = orientation
        
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
    
    def _smooth_orientation_transition(self, previous: QMatrix4x4, current: QMatrix4x4) -> QMatrix4x4:
        """Apply smoothing to orientation changes to prevent jerky motion"""
        # FIXED: PyQt6 QMatrix4x4 doesn't support () access
        # Instead, use proper matrix interpolation
        
        # Simple approach: just return current orientation for now
        # TODO: Implement proper quaternion SLERP for smooth transitions
        return current
    
    def _get_cached_transform_matrix(self, position: tuple, orientation: QMatrix4x4) -> QMatrix4x4:
        """Get cached transformation matrix if state unchanged"""
        current_state = (position, orientation)
        
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
        transform.scale(self.airplane_scale)
        
        # Cache for next frame
        self._transform_cache = transform
        self._last_animation_state = current_state
        
        return transform
    
    def _create_opengl_buffers(self) -> bool:
        """Create VAO/VBO for airplane geometry"""
        if self.geometry.geometry_data is None or len(self.geometry.geometry_data) == 0:
            print("❌ No geometry data for airplane buffers")
            return False
        
        # Generate VAO and VBO
        self.vao = gl.glGenVertexArrays(1)
        self.vbo = gl.glGenBuffers(1)
        
        if self.vao == 0 or self.vbo == 0:
            print("❌ Failed to create airplane OpenGL buffers")
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
        
        print("✅ Airplane OpenGL buffers created")
        return True
    
    def _render_airplane(self, mvp: QMatrix4x4, model: QMatrix4x4) -> bool:
        """Internal rendering method"""
        shader = self.parent_widget.airplane_shader
        if not shader:
            return False
        
        # Temporarily disable face culling for debugging
        gl.glDisable(gl.GL_CULL_FACE)
        
        shader.bind()
        
        try:
            shader.setUniformValue("mvp", mvp)
            shader.setUniformValue("model", model)
            shader.setUniformValue("normalMatrix", model.normalMatrix())
            shader.setUniformValue("lightDir", QVector3D(1.0, 0.3, 0.5).normalized())
            shader.setUniformValue("airplaneColor", QVector3D(*self.airplane_color))
            
            # Render
            gl.glBindVertexArray(self.vao)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, self.geometry.vertex_count)
            gl.glBindVertexArray(0)
            
            return True
            
        except Exception as e:
            print(f"❌ Failed to render airplane: {e}")
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
        
        print("✅ Airplane resources cleaned up")
