#version 330 core
in float Age;
in float Alpha;

out vec4 FragColor;

uniform vec3 contrailColor;

void main() {
    // Soft circular falloff for each point sprite
    vec2 circCoord = 2.0 * gl_PointCoord - 1.0;
    float dist = dot(circCoord, circCoord);

    if (dist > 1.0) discard;

    // Soft edge with gaussian-like falloff
    float softEdge = exp(-dist * 2.0);

    // Final alpha combines age-based fade and soft edge
    float finalAlpha = Alpha * softEdge * 0.75;

    FragColor = vec4(contrailColor, finalAlpha);
}
