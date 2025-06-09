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