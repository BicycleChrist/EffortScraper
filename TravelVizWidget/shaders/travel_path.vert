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