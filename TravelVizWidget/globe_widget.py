import sys
import math
import time
import numpy as np
import OpenGL.GL as gl
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture
from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal, QDateTime
from PyQt6.QtGui import (QMatrix4x4, QVector3D, QQuaternion, QMouseEvent, 
                        QWheelEvent, QColor, QImage)
import os
from pathlib import Path


class FlightGlobeWidget(QOpenGLWidget):
    """3D Globe Widget for sports team travel visualization with textured team logo markers"""
    
    # Signals
    locationSelected = pyqtSignal(float, float, str)
    flightSelected = pyqtSignal(str)
    dataLoadingProgress = pyqtSignal(int)
    performanceUpdate = pyqtSignal(float)
    
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
        
        # Team logo textures
        self.team_logo_textures = {}
        self.default_marker_texture = None
        self.logos_folder = "mlb_logos"  # Folder containing PNG logos
        
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
        self.filtered_travel = []
        self.travel_paths = []
        self.team_city_markers = []
        self.venue_markers = []
        
        # Display options
        self.show_travel_paths = True
        self.show_team_cities = True
        self.show_venues = True
        self.show_labels = True
        self.show_atmosphere = True
        self.path_animation_speed = 0.5
        
        # Performance monitoring
        self.frame_times = []
        self.max_frame_samples = 60
        
        # OpenGL state tracking
        self.gl_initialized = False
        
        # Coordinate cache for performance
        self.coordinate_cache = {}
        
        # Initialize default view
        self.setup_default_view()
        self.setMouseTracking(True)
        self.render_timer.start(16)  # 60 FPS
    
    def setup_default_view(self):
        """Setup default globe view focused on North America"""
        self.rotation_matrix = QMatrix4x4()
        self.rotation_matrix.rotate(0, 1, 0, 0)
        self.rotation_matrix.rotate(-50, 0, 1, 0)
    
    def initializeGL(self):
        """Initialize OpenGL resources"""
        try:
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
            
            self.gl_initialized = True
            print("Globe OpenGL initialized successfully with team logo support")
            
        except Exception as e:
            print(f"OpenGL initialization error: {e}")
            self.gl_initialized = False
    
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
        """Setup OpenGL shaders with enhanced team logo marker support"""
        try:
            # Earth shader (unchanged)
            earth_vertex = """
            #version 330 core
            layout (location = 0) in vec3 position;
            layout (location = 1) in vec2 texCoord;
            layout (location = 2) in vec3 normal;
            
            uniform mat4 mvp;
            uniform mat4 model;
            uniform mat3 normalMatrix;
            uniform vec3 lightDir;
            uniform vec3 viewPos;
            
            out vec2 TexCoord;
            out vec3 Normal;
            out vec3 FragPos;
            out vec3 ViewDir;
            out float LightIntensity;
            
            void main() {
                vec4 worldPos = model * vec4(position, 1.0);
                gl_Position = mvp * vec4(position, 1.0);
                
                TexCoord = texCoord;
                Normal = normalize(normalMatrix * normal);
                FragPos = worldPos.xyz;
                ViewDir = normalize(viewPos - FragPos);
                LightIntensity = max(dot(Normal, normalize(lightDir)), 0.0);
            }
            """
            
            earth_fragment = """
            #version 330 core
            in vec2 TexCoord;
            in vec3 Normal;
            in vec3 FragPos;
            in vec3 ViewDir;
            in float LightIntensity;
            
            out vec4 FragColor;
            
            uniform sampler2D earthTexture;
            uniform vec3 lightDir;
            uniform bool showAtmosphere;
            
            void main() {
                vec3 earthColor = texture(earthTexture, TexCoord).rgb;
                float ambient = 0.15;
                float diffuse = LightIntensity * 0.85;
                vec3 litColor = earthColor * (ambient + diffuse);
                
                if (LightIntensity < 0.3) {
                    vec2 cityCoord = TexCoord * 50.0;
                    float cityNoise = sin(cityCoord.x) * cos(cityCoord.y);
                    float cityLights = smoothstep(0.7, 1.0, cityNoise) * (0.3 - LightIntensity);
                    litColor += vec3(1.0, 0.8, 0.4) * cityLights * 0.3;
                }
                
                vec3 finalColor = litColor;
                
                if (showAtmosphere) {
                    float rim = 1.0 - max(dot(ViewDir, Normal), 0.0);
                    rim = pow(rim, 1.5);
                    vec3 atmosphereColor = vec3(0.4, 0.7, 1.0) * rim * 0.6;
                    finalColor += atmosphereColor;
                }
                
                FragColor = vec4(finalColor, 1.0);
            }
            """
            
            self.shader_program = QOpenGLShaderProgram()
            self.shader_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, earth_vertex)
            self.shader_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, earth_fragment)
            self.shader_program.link()
            
            # Travel path shader (unchanged)
            travel_vertex = """
            #version 330 core
            layout (location = 0) in vec3 position;
            
            uniform mat4 mvp;
            uniform float time;
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
            
            uniform float time;
            
            void main() {
                float pulse = sin(time * 2.0) * 0.3 + 0.7;
                vec3 glowColor = Color * pulse;
                vec3 finalColor = glowColor + Color * 0.2;
                FragColor = vec4(finalColor, Alpha);
            }
            """
            
            self.travel_shader = QOpenGLShaderProgram()
            self.travel_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, travel_vertex)
            self.travel_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, travel_fragment)
            self.travel_shader.link()
            
            # FIXED: Enhanced 3D spinning cube marker shader with proper texture handling
            marker_vertex = """
            #version 330 core
            layout (location = 0) in vec3 position;
            layout (location = 1) in vec3 normal;
            layout (location = 2) in vec2 texCoord;
            
            uniform mat4 mvp;
            uniform mat4 modelMatrix;
            uniform vec3 markerCenter;
            uniform float markerSize;
            uniform float time;
            uniform float rotationSpeed;
            
            out vec2 MarkerTexCoord;
            out vec3 MarkerNormal;
            out float LightIntensity;
            
            void main() {
                // Create rotation matrices for spinning cube
                float rotY = time * rotationSpeed;
                float rotX = time * rotationSpeed * 0.7;
                
                // Rotation around Y axis
                mat3 rotationY = mat3(
                    cos(rotY), 0.0, sin(rotY),
                    0.0, 1.0, 0.0,
                    -sin(rotY), 0.0, cos(rotY)
                );
                
                // Rotation around X axis
                mat3 rotationX = mat3(
                    1.0, 0.0, 0.0,
                    0.0, cos(rotX), -sin(rotX),
                    0.0, sin(rotX), cos(rotX)
                );
                
                // Combine rotations
                mat3 rotation = rotationY * rotationX;
                
                // FIXED: Much smaller cubes as requested
                vec3 rotatedPos = rotation * (position * markerSize * 0.008); // Smaller cubes
                
                // Position cube slightly above the globe surface
                vec3 worldPos = markerCenter + rotatedPos;
                
                gl_Position = mvp * vec4(worldPos, 1.0);
                
                // Transform normal for lighting
                MarkerNormal = rotation * normal;
                
                // Enhanced lighting calculation
                vec3 lightDir = normalize(vec3(1.0, 1.0, 1.0));
                LightIntensity = max(dot(MarkerNormal, lightDir), 0.6); // Good ambient minimum
                
                MarkerTexCoord = texCoord;
            }
            """
            
            # FIXED: Simplified marker fragment shader for better texture display
            marker_fragment = """
            #version 330 core
            in vec2 MarkerTexCoord;
            in vec3 MarkerNormal;
            in float LightIntensity;
            
            out vec4 FragColor;
            
            uniform sampler2D markerTexture;
            uniform float time;
            uniform vec3 teamColor;
            uniform bool useTexture;
            uniform float markerAlpha;
            
            void main() {
                if (useTexture) {
                    // Sample the texture
                    vec4 texColor = texture(markerTexture, MarkerTexCoord);
                    
                    // For debugging - make texture very visible
                    if (texColor.a > 0.01) {
                        // Use texture color directly with lighting
                        vec3 finalRGB = texColor.rgb * LightIntensity;
                        FragColor = vec4(finalRGB, markerAlpha);
                    } else {
                        // Transparent areas get team color
                        vec3 finalRGB = teamColor * LightIntensity;
                        FragColor = vec4(finalRGB, markerAlpha);
                    }
                } else {
                    // No texture - use team color
                    vec3 finalRGB = teamColor * LightIntensity;
                    FragColor = vec4(finalRGB, markerAlpha);
                }
            }
            """
            
            self.marker_shader = QOpenGLShaderProgram()
            self.marker_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, marker_vertex)
            self.marker_shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, marker_fragment)
            self.marker_shader.link()
            
            print("Shaders compiled successfully with enhanced team logo texture support")
            
        except Exception as e:
            print(f"Shader setup error: {e}")
    
    def load_team_logo_textures(self):
        """Load MLB team logo textures from PNG files with proper alpha handling"""
        try:
            logos_path = Path(self.logos_folder)
            if not logos_path.exists():
                print(f"Warning: MLB logos folder '{self.logos_folder}' not found")
                self.create_default_marker_texture()
                return
            
            # MLB team ID to filename mapping (capitalized filenames)
            team_logo_files = {
                "LAD": "Dodgers.png",
                "NYY": "Yankees.png", 
                "BOS": "Redsox.png",
                "CHC": "Cubs.png",
                "SF": "Giants.png",
                "ATL": "Braves.png",
                "HOU": "Astros.png",
                "LAA": "Angels.png",
                "NYM": "Mets.png",
                "PHI": "Phillies.png",
                "STL": "Cardinals.png",
                "WSH": "Nationals.png",
                "MIL": "Brewers.png",
                "COL": "Rockies.png",
                "ARI": "Diamondbacks.png",
                "SD": "Padres.png",
                "MIA": "Marlins.png",
                "TEX": "Rangers.png",
                "CIN": "Reds.png",
                "PIT": "Pirates.png",
                "BAL": "Orioles.png",
                "CLE": "Guardians.png",
                "DET": "Tigers.png",
                "MIN": "Twins.png",
                "CWS": "WhiteSox.png",
                "KC": "Royals.png",
                "OAK": "Athletics.png",
                "SEA": "Mariners.png",
                "TB": "Rays.png",
                "TOR": "BlueJays.png"
            }
            
            logos_loaded = 0
            
            for team_id, filename in team_logo_files.items():
                logo_path = logos_path / filename
                if logo_path.exists():
                    try:
                        # Load PNG image with alpha channel support
                        image = QImage(str(logo_path))
                        if not image.isNull():
                            # FIXED: Ensure consistent image size and format
                            image = image.scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio, 
                                              Qt.TransformationMode.SmoothTransformation)
                            
                            # FIXED: Process PNG with proper alpha handling
                            image = self.process_logo_image(image, team_id)
                            
                            # Create OpenGL texture
                            texture = QOpenGLTexture(image)
                            texture.setMinificationFilter(QOpenGLTexture.Filter.LinearMipMapLinear)
                            texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
                            texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
                            
                            try:
                                texture.generateMipMaps()
                            except AttributeError:
                                pass
                            
                            self.team_logo_textures[team_id] = texture
                            logos_loaded += 1
                            print(f"Successfully loaded logo for {team_id}: {filename}")
                        else:
                            print(f"Warning: Could not load logo image: {logo_path}")
                    except Exception as e:
                        print(f"Error loading logo {filename}: {e}")
                else:
                    print(f"Warning: Logo file not found: {logo_path}")
            
            print(f"Loaded {logos_loaded} team logo textures")
            print(f"Available team IDs: {list(self.team_logo_textures.keys())}")
            
            # Create default marker texture for teams without logos
            self.create_default_marker_texture()
            
        except Exception as e:
            print(f"Error loading team logo textures: {e}")
            self.create_default_marker_texture()
    
    def process_logo_image(self, image: QImage, team_id: str) -> QImage:
        """Process logo image to ensure proper visibility on cube faces"""
        try:
            # Convert to RGBA format if not already
            if image.format() != QImage.Format.Format_RGBA8888:
                image = image.convertToFormat(QImage.Format.Format_RGBA8888)
            
            # Don't modify the original image - return it as is for now
            # The shader will handle transparency properly
            print(f"Processed logo for {team_id}: {image.width()}x{image.height()}")
            return image
            
        except Exception as e:
            print(f"Error processing logo image for {team_id}: {e}")
            return image
    
    def create_default_marker_texture(self):
        """Create a default solid marker texture for teams without logos"""
        try:
            size = 128
            image = QImage(size, size, QImage.Format.Format_RGBA8888)
            
            # Create solid color with team color pattern
            center = size // 2
            for y in range(size):
                for x in range(size):
                    # Create subtle gradient effect
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
            
        except Exception as e:
            print(f"Default marker texture creation error: {e}")
    
    def setup_vertex_buffers(self):
        """Setup vertex buffer objects"""
        if self.vertices is None:
            return
        
        try:
            vertex_count = len(self.vertices) // 3
            vertex_data = []
            
            for i in range(vertex_count):
                # Position + TexCoord + Normal
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
            
        except Exception as e:
            print(f"Vertex buffer setup error: {e}")
    
    def setup_earth_texture(self):
        """Load Earth texture"""
        try:
            texture_files = ["no_ice_clouds_8k.jpg", "earth.jpg"]
            
            image = None
            for texture_file in texture_files:
                image = QImage(texture_file)
                if not image.isNull():
                    break
            
            if image is None or image.isNull():
                self.create_fallback_earth_texture()
                return
            
            image = image.convertToFormat(QImage.Format.Format_RGB888)
            self.earth_texture = QOpenGLTexture(image)
            self.earth_texture.setMinificationFilter(QOpenGLTexture.Filter.LinearMipMapLinear)
            self.earth_texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
            
            try:
                self.earth_texture.setWrapMode(QOpenGLTexture.CoordinateDirection.DirectionS, 
                                             QOpenGLTexture.WrapMode.Repeat)
                self.earth_texture.setWrapMode(QOpenGLTexture.CoordinateDirection.DirectionT, 
                                             QOpenGLTexture.WrapMode.ClampToEdge)
                self.earth_texture.generateMipMaps()
            except AttributeError:
                pass
            
        except Exception as e:
            print(f"Earth texture loading error: {e}")
            self.create_fallback_earth_texture()
    
    def create_fallback_earth_texture(self):
        """Create a fallback Earth texture"""
        try:
            width, height = 512, 256
            image = QImage(width, height, QImage.Format.Format_RGB888)
            
            for y in range(height):
                for x in range(width):
                    blue = int(100 + (y / height) * 155)
                    green = int(50 + (x / width) * 100)
                    red = int(30)
                    color = QColor(red, green, blue)
                    image.setPixelColor(x, y, color)
            
            self.earth_texture = QOpenGLTexture(image)
            self.earth_texture.setMinificationFilter(QOpenGLTexture.Filter.Linear)
            self.earth_texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
            
        except Exception as e:
            print(f"Fallback Earth texture creation error: {e}")
    
    def create_marker_cube_geometry(self):
        """Create geometry for a spinning 3D cube marker"""
        # Cube vertices: position (x, y, z) + normal (x, y, z) + texCoord (u, v)
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
    
    def lat_lon_to_3d(self, lat: float, lon: float, radius: float = 1.0) -> Tuple[float, float, float]:
        """Convert latitude/longitude to 3D coordinates"""
        cache_key = (lat, lon, radius)
        if cache_key in self.coordinate_cache:
            return self.coordinate_cache[cache_key]
        
        try:
            lat_rad = math.radians(lat)
            lon_rad = math.radians(lon)
            
            x = radius * math.cos(lat_rad) * math.cos(lon_rad)
            y = radius * math.sin(lat_rad)
            z = -radius * math.cos(lat_rad) * math.sin(lon_rad)
            
            result = (float(x), float(y), float(z))
            self.coordinate_cache[cache_key] = result
            return result
        except Exception as e:
            print(f"Error converting lat/lon to 3D: {e}")
            return (0, 0, 0)
    
    def generate_great_circle_path(self, start_lat: float, start_lon: float, 
                                 end_lat: float, end_lon: float, num_points: int = 50) -> List[Tuple[float, float, float]]:
        """Generate great circle path between two points"""
        if abs(start_lat - end_lat) < 0.001 and abs(start_lon - end_lon) < 0.001:
            return []
        
        try:
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
            
        except Exception as e:
            print(f"Error generating great circle path: {e}")
            return []
    
    def get_city_coordinates(self, city_name: str) -> Optional[Tuple[float, float]]:
        """Get latitude/longitude for a city"""
        city_coords = {
            "Los Angeles": (34.0522, -118.2437),
            "New York": (40.7128, -74.0060),
            "Chicago": (41.8781, -87.6298),
            "Houston": (29.7604, -95.3698),
            "Phoenix": (33.4484, -112.0740),
            "Philadelphia": (39.9526, -75.1652),
            "San Antonio": (29.4241, -98.4936),
            "San Diego": (32.7157, -117.1611),
            "Dallas": (32.7767, -96.7970),
            "San Jose": (37.3382, -121.8863),
            "Austin": (30.2672, -97.7431),
            "Jacksonville": (30.3322, -81.6557),
            "San Francisco": (37.7749, -122.4194),
            "Columbus": (39.9612, -82.9988),
            "Charlotte": (35.2271, -80.8431),
            "Fort Worth": (32.7555, -97.3308),
            "Detroit": (42.3314, -83.0458),
            "El Paso": (31.7619, -106.4850),
            "Memphis": (35.1495, -90.0490),
            "Baltimore": (39.2904, -76.6122),
            "Boston": (42.3601, -71.0589),
            "Seattle": (47.6062, -122.3321),
            "Denver": (39.7392, -104.9903),
            "Washington": (38.9072, -77.0369),
            "Nashville": (36.1627, -86.7816),
            "Louisville": (38.2527, -85.7585),
            "Portland": (45.5152, -122.6784),
            "Las Vegas": (36.1699, -115.1398),
            "Milwaukee": (43.0389, -87.9065),
            "Atlanta": (33.7490, -84.3880),
            "Miami": (25.7617, -80.1918),
            "Tampa": (27.9506, -82.4572),
            "Pittsburgh": (40.4406, -79.9959),
            "Cincinnati": (39.1031, -84.5120),
            "St. Louis": (38.6270, -90.1994),
            "Minneapolis": (44.9778, -93.2650),
            "Toronto": (43.6532, -79.3832),
            "Montreal": (45.5017, -73.5673),
            "Vancouver": (49.2827, -123.1207),
            "Calgary": (51.0447, -114.0719),
            "Edmonton": (53.5461, -113.4938),
            "Ottawa": (45.4215, -75.6972),
            "Winnipeg": (49.8951, -97.1384),
            "London": (51.5074, -0.1278),
            "Paris": (48.8566, 2.3522),
            "Tokyo": (35.6762, 139.6503),
            "Sydney": (-33.8688, 151.2093),
            "Berlin": (52.5200, 13.4050),
            "Madrid": (40.4168, -3.7038),
            "Rome": (41.9028, 12.4964),
        }
        return city_coords.get(city_name)
    
    def paintGL(self):
        """Main rendering method"""
        if not self.gl_initialized:
            return
        
        try:
            current_time = time.time()
            frame_time = current_time - self.last_frame_time
            self.last_frame_time = current_time
            self.animation_time += frame_time
            
            if not self.is_rotating and self.rotation_momentum.length() > 0.01:
                momentum_rotation = QMatrix4x4()
                momentum_rotation.rotate(self.rotation_momentum.length(), 
                                       self.rotation_momentum.normalized())
                self.rotation_matrix = momentum_rotation * self.rotation_matrix
                self.rotation_momentum *= self.momentum_decay
            
            self.record_frame_time(frame_time)
            
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
            
            model = self.rotation_matrix
            view = QMatrix4x4()
            view.lookAt(QVector3D(0, 0, 3 / self.zoom_level), QVector3D(0, 0, 0), QVector3D(0, 1, 0))
            projection = QMatrix4x4()
            aspect_ratio = self.width() / max(self.height(), 1)
            projection.perspective(45.0, aspect_ratio, 0.1, 100.0)
            mvp = projection * view * model
            
            normal_matrix = model.normalMatrix()
            
            self.render_earth(mvp, model, normal_matrix, view)
            
            if self.show_travel_paths:
                self.render_travel_paths(mvp)
            
            self.render_textured_markers(mvp, view, model)
                
        except Exception as e:
            print(f"Rendering error: {e}")
    
    def render_earth(self, mvp, model, normal_matrix, view):
        """Render Earth sphere"""
        if not self.shader_program or not self.vao:
            return
        
        try:
            self.shader_program.bind()
            
            self.shader_program.setUniformValue("mvp", mvp)
            self.shader_program.setUniformValue("model", model)
            self.shader_program.setUniformValue("normalMatrix", normal_matrix)
            
            light_direction = QVector3D(1.0, 0.3, 0.5).normalized()
            self.shader_program.setUniformValue("lightDir", light_direction)
            self.shader_program.setUniformValue("viewPos", QVector3D(0, 0, 3 / self.zoom_level))
            self.shader_program.setUniformValue("showAtmosphere", self.show_atmosphere)
            
            if self.earth_texture:
                self.earth_texture.bind(0)
                self.shader_program.setUniformValue("earthTexture", 0)
            
            gl.glBindVertexArray(self.vao)
            gl.glDrawElements(gl.GL_TRIANGLES, len(self.indices), gl.GL_UNSIGNED_INT, None)
            
            self.shader_program.release()
            
        except Exception as e:
            print(f"Earth rendering error: {e}")
    
    def render_travel_paths(self, mvp):
        """Render travel paths"""
        if not self.travel_paths or not self.travel_shader:
            return
        
        try:
            gl.glEnable(gl.GL_LINE_SMOOTH)
            gl.glHint(gl.GL_LINE_SMOOTH_HINT, gl.GL_NICEST)
            gl.glLineWidth(2.5)
            
            self.travel_shader.bind()
            self.travel_shader.setUniformValue("mvp", mvp)
            self.travel_shader.setUniformValue("time", self.animation_time)
            
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
            
        except Exception as e:
            print(f"Travel path rendering error: {e}")
    
    def render_textured_markers(self, mvp, view, model):
        """Render spinning 3D cube markers with team logos"""
        if not self.marker_shader:
            return
        
        total_markers = len(self.team_city_markers) + len(self.venue_markers)
        if total_markers == 0:
            return
        
        try:
            # Enable 3D rendering settings
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            gl.glEnable(gl.GL_DEPTH_TEST)
            
            self.marker_shader.bind()
            self.marker_shader.setUniformValue("mvp", mvp)
            self.marker_shader.setUniformValue("modelMatrix", model)
            self.marker_shader.setUniformValue("time", self.animation_time)
            
            cube_geometry = self.create_marker_cube_geometry()
            
            # FIXED: Render team city markers as spinning cubes with proper sizing
            if self.show_team_cities:
                for marker in self.team_city_markers:
                    self.render_spinning_cube_marker(marker, cube_geometry, 1.5)
            
            # FIXED: Render venue markers as smaller spinning cubes
            if self.show_venues:
                for marker in self.venue_markers:
                    self.render_spinning_cube_marker(marker, cube_geometry, 1.0)
            
            self.marker_shader.release()
            
        except Exception as e:
            print(f"3D cube marker rendering error: {e}")
    
    def render_spinning_cube_marker(self, marker, cube_geometry, rotation_speed):
        """Render a single spinning 3D cube marker with team logo texture"""
        try:
            pos = marker['position']
            team_id = marker.get('team_id', '')
            marker_type = marker.get('type', 'generic')
            size = marker.get('size', 4.0)
            
            if not pos or len(pos) != 3:
                return False
            
            # FIXED: Enhanced texture selection and debugging
            texture = None
            use_texture = False
            team_color = (1.0, 1.0, 1.0)
            
            if team_id and team_id in self.team_logo_textures:
                texture = self.team_logo_textures[team_id]
                use_texture = True
                team_color = self.get_team_color(team_id)
                # Only print this once per team, not every frame
                if not hasattr(self, '_logged_teams'):
                    self._logged_teams = set()
                if team_id not in self._logged_teams:
                    print(f"Rendering {team_id} with team logo texture")
                    self._logged_teams.add(team_id)
            elif self.default_marker_texture:
                texture = self.default_marker_texture
                use_texture = True
                if marker_type == 'departure':
                    team_color = (0.2, 0.8, 0.3)  # Green for departures
                else:
                    team_color = (1.0, 0.4, 0.1)  # Orange for arrivals
                # Only log first time
                if not hasattr(self, '_logged_default'):
                    print(f"Using default texture for marker type: {marker_type}")
                    self._logged_default = True
            else:
                # Fallback to solid color
                use_texture = False
                team_color = marker.get('color', (1.0, 1.0, 1.0))
                if not hasattr(self, '_logged_fallback'):
                    print(f"Using solid color fallback")
                    self._logged_fallback = True
            
            # FIXED: Ensure texture is active before setting uniforms
            if texture and use_texture:
                gl.glActiveTexture(gl.GL_TEXTURE0)
                texture.bind()
                self.marker_shader.setUniformValue("markerTexture", 0)
            
            # Set uniforms
            self.marker_shader.setUniformValue("markerCenter", QVector3D(*pos))
            self.marker_shader.setUniformValue("markerSize", size)
            self.marker_shader.setUniformValue("rotationSpeed", rotation_speed)
            self.marker_shader.setUniformValue("teamColor", QVector3D(*team_color))
            self.marker_shader.setUniformValue("useTexture", use_texture)
            self.marker_shader.setUniformValue("markerAlpha", 0.9)
            
            # Create and render cube
            cube_vao = gl.glGenVertexArrays(1)
            cube_vbo = gl.glGenBuffers(1)
            
            gl.glBindVertexArray(cube_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, cube_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, cube_geometry.nbytes, cube_geometry, gl.GL_DYNAMIC_DRAW)
            
            # Vertex attributes: position (3 floats) + normal (3 floats) + texCoord (2 floats) = 8 floats per vertex
            stride = 8 * 4  # 8 floats * 4 bytes per float
            
            # Position attribute (location 0)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(0))
            
            # Normal attribute (location 1) 
            gl.glEnableVertexAttribArray(1)
            gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(12))
            
            # Texture coordinate attribute (location 2)
            gl.glEnableVertexAttribArray(2)
            gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, stride, gl.GLvoidp(24))
            
            # Draw the cube (36 vertices for 12 triangles, 6 faces)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            
            # Clean up
            gl.glDeleteBuffers(1, [cube_vbo])
            gl.glDeleteVertexArrays(1, [cube_vao])
            
            return True
            
        except Exception as e:
            print(f"Spinning cube marker rendering error: {e}")
            return False
    
    def load_flight_data(self, travel_data: List):
        """Load sports team travel data"""
        self.travel_data = travel_data
        self.generate_enhanced_visualizations()
        self.update()
    
    def generate_enhanced_visualizations(self):
        """Generate travel paths and markers from sports data"""
        self.travel_paths = []
        self.team_city_markers = []
        self.venue_markers = []
        
        cities_seen = set()
        venues_seen = set()
        
        print(f"Processing {len(self.travel_data)} travel records...")
        
        for i, travel in enumerate(self.travel_data):
            if not hasattr(travel, 'departure_city'):
                print(f"Travel record {i} missing departure_city")
                continue
            
            # Debug travel data
            team_id = getattr(travel, 'team_id', 'NO_TEAM_ID')
            team_name = getattr(travel, 'team_name', 'NO_TEAM_NAME')
            print(f"Travel {i}: {team_name} ({team_id}) from {travel.departure_city} to {travel.arrival_city}")
            
            dep_coords = self.get_city_coordinates(travel.departure_city)
            arr_coords = self.get_city_coordinates(travel.arrival_city)
            
            if not dep_coords or not arr_coords:
                print(f"Missing coordinates for {travel.departure_city} or {travel.arrival_city}")
                continue
            
            dep_lat, dep_lon = dep_coords
            arr_lat, arr_lon = arr_coords
            
            path_points = self.generate_great_circle_path(dep_lat, dep_lon, arr_lat, arr_lon)
            if path_points and len(path_points) > 0:
                # FIXED: Use team abbreviation for colors
                espn_team_id = getattr(travel, 'team_id', '')
                team_abbrev = self.get_team_abbreviation_from_espn_id(espn_team_id) if espn_team_id else ''
                team_color = self.get_team_color(team_abbrev) if team_abbrev else (1.0, 0.6, 0.2)
                
                path_data = {
                    'points': path_points,
                    'team_name': getattr(travel, 'team_name', 'Unknown'),
                    'route': f"{travel.departure_city} → {travel.arrival_city}",
                    'color': team_color,
                    'alpha': 0.85,
                    'travel_date': getattr(travel, 'travel_date', None)
                }
                self.travel_paths.append(path_data)
                
                # FIXED: Create team logo markers at departure cities with much smaller sizing
                if travel.departure_city not in cities_seen:
                    cities_seen.add(travel.departure_city)
                    dep_3d = self.lat_lon_to_3d(dep_lat, dep_lon, 1.05)
                    espn_team_id = getattr(travel, 'team_id', '')
                    # FIXED: Convert ESPN numeric ID to team abbreviation
                    team_abbrev = self.get_team_abbreviation_from_espn_id(espn_team_id)
                    print(f"Creating DEPARTURE marker for team '{espn_team_id}' -> '{team_abbrev}' at {travel.departure_city}")
                    print(f"Team abbreviation available in logo textures: {team_abbrev in self.team_logo_textures}")
                    city_marker = {
                        'position': dep_3d,
                        'team_id': team_abbrev,  # Use abbreviation instead of ESPN ID
                        'espn_id': espn_team_id,  # Keep original for reference
                        'size': 4.0,  # Much smaller size
                        'city_name': travel.departure_city,
                        'type': 'departure'
                    }
                    self.team_city_markers.append(city_marker)
                
                # FIXED: Create generic markers at arrival venues with smallest sizing
                if travel.arrival_city not in venues_seen:
                    venues_seen.add(travel.arrival_city)
                    arr_3d = self.lat_lon_to_3d(arr_lat, arr_lon, 1.03)
                    print(f"Creating ARRIVAL marker at {travel.arrival_city}")
                    venue_marker = {
                        'position': arr_3d,
                        'team_id': '',  # No team logo for venues
                        'size': 3.0,  # Very small for venues
                        'venue_name': travel.arrival_city,
                        'type': 'arrival'
                    }
                    self.venue_markers.append(venue_marker)
        
        print(f"Created {len(self.team_city_markers)} team city markers and {len(self.venue_markers)} venue markers")
    
    def get_team_abbreviation_from_espn_id(self, espn_team_id: str) -> str:
        """Convert ESPN numeric team ID to team abbreviation for logo lookup"""
        # ESPN API team ID mappings (based on observed ESPN API responses)
        espn_to_abbrev = {
            # Confirmed mappings from debug output
            "21": "NYM",   # New York Mets (confirmed from debug)
            
            # Common ESPN team ID mappings (will be updated as we see more)
            "1": "ATL",    # Atlanta Braves
            "2": "MIA",    # Miami Marlins  
            "3": "PHI",    # Philadelphia Phillies
            "4": "WSH",    # Washington Nationals
            "5": "CHC",    # Chicago Cubs
            "6": "CIN",    # Cincinnati Reds
            "7": "MIL",    # Milwaukee Brewers
            "8": "PIT",    # Pittsburgh Pirates
            "9": "STL",    # St. Louis Cardinals
            "10": "ARI",   # Arizona Diamondbacks
            "11": "COL",   # Colorado Rockies
            "12": "LAD",   # Los Angeles Dodgers
            "13": "SD",    # San Diego Padres
            "14": "SF",    # San Francisco Giants
            "15": "BAL",   # Baltimore Orioles
            "16": "BOS",   # Boston Red Sox
            "17": "NYY",   # New York Yankees
            "18": "TB",    # Tampa Bay Rays
            "19": "TOR",   # Toronto Blue Jays
            "20": "CWS",   # Chicago White Sox
            "22": "CLE",   # Cleveland Guardians
            "23": "DET",   # Detroit Tigers
            "24": "KC",    # Kansas City Royals
            "25": "MIN",   # Minnesota Twins
            "26": "HOU",   # Houston Astros
            "27": "LAA",   # Los Angeles Angels
            "28": "OAK",   # Oakland Athletics
            "29": "SEA",   # Seattle Mariners
            "30": "TEX",   # Texas Rangers
        }
        
        # If we have a mapping, use it; otherwise return the original ID
        abbrev = espn_to_abbrev.get(espn_team_id, espn_team_id)
        if abbrev != espn_team_id:
            print(f"Mapped ESPN ID '{espn_team_id}' to abbreviation '{abbrev}'")
        else:
            print(f"No mapping found for ESPN ID '{espn_team_id}' - using as-is")
        return abbrev
        """Get team color based on team ID"""
        team_colors = {
            # AL East
            "NYY": (0.1, 0.2, 0.5),    # Navy Blue
            "BOS": (0.8, 0.1, 0.2),    # Red Sox Red
            "TB": (0.0, 0.3, 0.6),     # Rays Blue
            "TOR": (0.0, 0.4, 0.8),    # Blue Jays Blue
            "BAL": (1.0, 0.3, 0.0),    # Orioles Orange
            
            # AL Central
            "CLE": (0.8, 0.1, 0.2),    # Guardians Red
            "CWS": (0.1, 0.1, 0.1),    # White Sox Black
            "DET": (0.0, 0.2, 0.5),    # Tigers Navy
            "KC": (0.0, 0.3, 0.6),     # Royals Blue
            "MIN": (0.0, 0.2, 0.5),    # Twins Navy
            
            # AL West
            "HOU": (1.0, 0.4, 0.0),    # Astros Orange
            "SEA": (0.0, 0.4, 0.6),    # Mariners Teal
            "TEX": (0.8, 0.1, 0.2),    # Rangers Red
            "LAA": (0.8, 0.0, 0.2),    # Angels Red
            "OAK": (0.0, 0.5, 0.2),    # Athletics Green
            
            # NL East
            "ATL": (0.7, 0.0, 0.2),    # Braves Red
            "PHI": (0.9, 0.1, 0.2),    # Phillies Red
            "NYM": (0.0, 0.3, 0.8),    # Mets Blue
            "WSH": (0.7, 0.0, 0.2),    # Nationals Red
            "MIA": (0.0, 0.6, 0.8),    # Marlins Teal
            
            # NL Central
            "STL": (0.8, 0.0, 0.2),    # Cardinals Red
            "MIL": (0.0, 0.2, 0.5),    # Brewers Navy
            "CHC": (0.0, 0.2, 0.6),    # Cubs Blue
            "CIN": (0.8, 0.1, 0.2),    # Reds Red
            "PIT": (1.0, 0.8, 0.0),    # Pirates Gold
            
            # NL West
            "LAD": (0.0, 0.4, 0.8),    # Dodger Blue
            "SF": (1.0, 0.3, 0.0),     # Giants Orange
            "SD": (1.0, 0.4, 0.0),     # Padres Orange
            "COL": (0.2, 0.1, 0.4),    # Rockies Purple
            "ARI": (0.6, 0.0, 0.2),    # Diamondbacks Red
        }
        return team_colors.get(team_id, (1.0, 0.6, 0.2))
    
    

    
    
    def get_team_color(self, team_id: str) -> Tuple[float, float, float]:
        """Get team color based on team ID"""
        team_colors = {
            # AL East
            "NYY": (0.1, 0.2, 0.5),    # Navy Blue
            "BOS": (0.8, 0.1, 0.2),    # Red Sox Red
            "TB": (0.0, 0.3, 0.6),     # Rays Blue
            "TOR": (0.0, 0.4, 0.8),    # Blue Jays Blue
            "BAL": (1.0, 0.3, 0.0),    # Orioles Orange

            # AL Central
            "CLE": (0.8, 0.1, 0.2),    # Guardians Red
            "CWS": (0.1, 0.1, 0.1),    # White Sox Black
            "DET": (0.0, 0.2, 0.5),    # Tigers Navy
            "KC": (0.0, 0.3, 0.6),     # Royals Blue
            "MIN": (0.0, 0.2, 0.5),    # Twins Navy

            # AL West
            "HOU": (1.0, 0.4, 0.0),    # Astros Orange
            "SEA": (0.0, 0.4, 0.6),    # Mariners Teal
            "TEX": (0.8, 0.1, 0.2),    # Rangers Red
            "LAA": (0.8, 0.0, 0.2),    # Angels Red
            "OAK": (0.0, 0.5, 0.2),    # Athletics Green

            # NL East
            "ATL": (0.7, 0.0, 0.2),    # Braves Red
            "PHI": (0.9, 0.1, 0.2),    # Phillies Red
            "NYM": (0.0, 0.3, 0.8),    # Mets Blue
            "WSH": (0.7, 0.0, 0.2),    # Nationals Red
            "MIA": (0.0, 0.6, 0.8),    # Marlins Teal

            # NL Central
            "STL": (0.8, 0.0, 0.2),    # Cardinals Red
            "MIL": (0.0, 0.2, 0.5),    # Brewers Navy
            "CHC": (0.0, 0.2, 0.6),    # Cubs Blue
            "CIN": (0.8, 0.1, 0.2),    # Reds Red
            "PIT": (1.0, 0.8, 0.0),    # Pirates Gold

            # NL West
            "LAD": (0.0, 0.4, 0.8),    # Dodger Blue
            "SF": (1.0, 0.3, 0.0),     # Giants Orange
            "SD": (1.0, 0.4, 0.0),     # Padres Orange
            "COL": (0.2, 0.1, 0.4),    # Rockies Purple
            "ARI": (0.6, 0.0, 0.2),    # Diamondbacks Red
        }
        return team_colors.get(team_id, (1.0, 0.6, 0.2))
    
    
    
    def filter_flights(self, filtered_travel: List):
        """Update display with filtered travel data"""
        self.filtered_travel = filtered_travel
        temp_data = self.travel_data
        self.travel_data = filtered_travel
        self.generate_enhanced_visualizations()
        self.travel_data = temp_data
        self.update()
    
    def set_path_animation_speed(self, speed: float):
        """Set travel path animation speed"""
        self.path_animation_speed = max(0.1, min(2.0, speed))
    
    def record_frame_time(self, frame_time: float):
        """Record frame rendering time for performance monitoring"""
        self.frame_times.append(frame_time)
        if len(self.frame_times) > self.max_frame_samples:
            self.frame_times.pop(0)
        
        if len(self.frame_times) >= 10:
            avg_time = sum(self.frame_times) / len(self.frame_times)
            fps = 1.0 / avg_time if avg_time > 0 else 0.0
            self.performanceUpdate.emit(fps)
    
    def set_display_options(self, show_paths: bool = True, show_cities: bool = True, 
                          show_venues: bool = True, show_labels: bool = True, 
                          show_atmosphere: bool = True):
        """Set display options"""
        self.show_travel_paths = show_paths
        self.show_team_cities = show_cities
        self.show_venues = show_venues
        self.show_labels = show_labels
        self.show_atmosphere = show_atmosphere
        self.update()
    
    def set_logos_folder(self, folder_path: str):
        """Set the folder path for MLB logo images"""
        self.logos_folder = folder_path
        if self.gl_initialized:
            self.load_team_logo_textures()
    
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
        try:
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
        except Exception as e:
            print(f"Rotation error: {e}")
    
    def wheelEvent(self, event):
        """Handle mouse wheel for zooming"""
        try:
            delta = event.angleDelta().y()
            zoom_factor = 1.1 if delta > 0 else 0.9
            new_zoom = self.zoom_level * zoom_factor
            self.zoom_level = max(self.min_zoom, min(self.max_zoom, new_zoom))
            self.update()
        except Exception as e:
            print(f"Zoom error: {e}")
    
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
        try:
            if self.gl_initialized:
                self.makeCurrent()
                
                if self.vao:
                    gl.glDeleteVertexArrays(1, [self.vao])
                if self.vbo:
                    gl.glDeleteBuffers(1, [self.vbo])
                if self.ebo:
                    gl.glDeleteBuffers(1, [self.ebo])
                
                self.doneCurrent()
        except Exception as e:
            print(f"Cleanup error: {e}")
        
        super().closeEvent(event)
