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
out vec3 WorldNormal;
out vec3 WorldPos;

void main() {
    gl_Position = mvp * vec4(position, 1.0);
    TexCoord = texCoord;
    WorldNormal = normalize(normalMatrix * normal);
    WorldPos = (model * vec4(position, 1.0)).xyz;
    LightIntensity = max(dot(WorldNormal, normalize(lightDir)), 0.0);
}
