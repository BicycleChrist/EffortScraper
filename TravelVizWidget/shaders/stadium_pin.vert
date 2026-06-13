#version 330 core
layout (location = 0) in vec3 position;
layout (location = 1) in vec3 normal;

uniform mat4 mvp;
uniform mat4 model;
uniform vec3 pinPosition;
uniform float pinScale;
uniform float time;

out vec3 FragNormal;
out float Height;

void main() {
    // Scale and position the pin geometry
    vec3 scaledPos = position * pinScale;
    vec3 worldPos = pinPosition + scaledPos;

    // Subtle bobbing animation
    worldPos += normalize(pinPosition) * sin(time * 2.5) * 0.003;

    gl_Position = mvp * vec4(worldPos, 1.0);
    FragNormal = mat3(model) * normal;
    Height = position.y;
}
