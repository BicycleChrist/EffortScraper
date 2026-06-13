#version 330 core
layout (location = 0) in vec3 position;
layout (location = 1) in float age;

uniform mat4 mvp;
uniform float maxAge;

out float Age;
out float Alpha;

void main() {
    gl_Position = mvp * vec4(position, 1.0);
    Age = age;

    // Fade from full opacity to transparent as trail ages
    float ageFactor = clamp(age / maxAge, 0.0, 1.0);
    Alpha = 1.0 - ageFactor;

    // Point size decreases with age: 4.0 -> 1.0
    gl_PointSize = mix(4.0, 1.0, ageFactor);
}
