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