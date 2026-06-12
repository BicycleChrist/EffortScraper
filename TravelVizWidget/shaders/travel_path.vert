#version 330 core
layout (location = 0) in vec3 position;

uniform mat4 mvp;
uniform mat4 model;
uniform vec3 cameraPos;
uniform vec3 pathColor;
uniform float pathAlpha;

out vec3 Color;
out float Alpha;

void main() {
    gl_Position = mvp * vec4(position, 1.0);
    Color = pathColor;

    // Horizon fade: vertices on the far side of the globe drop to a faint
    // ghost instead of drawing full-strength through the planet.
    vec3 worldPos = (model * vec4(position, 1.0)).xyz;
    float camDist = max(length(cameraPos), 1.0001);
    float horizon = 1.0 / camDist;  // cos of horizon angle for the unit sphere
    float cosAngle = dot(normalize(worldPos), normalize(cameraPos));
    float visibility = smoothstep(horizon - 0.18, horizon + 0.05, cosAngle);

    Alpha = pathAlpha * mix(0.05, 1.0, visibility);
}
