#version 330 core
// Starfield fixed in camera-world space: round soft stars (jittered inside
// their hash cells — no more square pixels), two depth layers, subtle color
// temperature variation, slow twinkle, and a faint Milky Way band.
in vec2 NDC;
out vec4 FragColor;

uniform mat4 invVP;   // inverse(projection * view), no model
uniform float time;

float hash13(vec3 p) {
    p = fract(p * 0.1031);
    p += dot(p, p.zyx + 31.32);
    return fract((p.x + p.y) * p.z);
}

// One star layer: cells on the direction sphere, star jittered within cell,
// gaussian-ish round falloff. density = survival threshold for a cell.
float starLayer(vec3 dir, float scale, float density, out float seed) {
    vec3 p = dir * scale;
    vec3 cell = floor(p);
    float h = hash13(cell);
    seed = fract(h * 113.7);
    if (h < density) {
        return 0.0;
    }
    vec3 jitter = vec3(hash13(cell + 17.0), hash13(cell + 31.0), hash13(cell + 47.0));
    vec3 starPos = cell + 0.25 + 0.5 * jitter;
    float d = length(p - starPos);
    float size = mix(0.18, 0.34, hash13(cell + 71.0));
    return smoothstep(size, 0.0, d);
}

// Cheap 2-octave value noise for the Milky Way texture
float vnoise(vec3 p) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float n000 = hash13(i),               n100 = hash13(i + vec3(1, 0, 0));
    float n010 = hash13(i + vec3(0, 1, 0)), n110 = hash13(i + vec3(1, 1, 0));
    float n001 = hash13(i + vec3(0, 0, 1)), n101 = hash13(i + vec3(1, 0, 1));
    float n011 = hash13(i + vec3(0, 1, 1)), n111 = hash13(i + vec3(1, 1, 1));
    return mix(mix(mix(n000, n100, f.x), mix(n010, n110, f.x), f.y),
               mix(mix(n001, n101, f.x), mix(n011, n111, f.x), f.y), f.z);
}

void main() {
    vec4 near = invVP * vec4(NDC, -1.0, 1.0);
    vec4 far  = invVP * vec4(NDC,  1.0, 1.0);
    vec3 dir = normalize(far.xyz / far.w - near.xyz / near.w);

    // Deep space base with a barely-there vertical gradient
    vec3 col = mix(vec3(0.002, 0.004, 0.014),
                   vec3(0.006, 0.012, 0.034), dir.y * 0.5 + 0.5);

    // Milky Way: fuzzy band around a tilted great circle, noise-modulated
    vec3 bandAxis = normalize(vec3(0.45, 0.85, 0.30));
    float bandDist = abs(dot(dir, bandAxis));
    float band = exp(-bandDist * bandDist * 14.0);
    float wisps = vnoise(dir * 7.0) * 0.65 + vnoise(dir * 19.0) * 0.35;
    col += vec3(0.45, 0.52, 0.68) * band * wisps * wisps * 0.24;

    // Dense faint layer + sparse bright layer
    float seedA, seedB;
    float faint  = starLayer(dir, 150.0, 0.984, seedA);
    float bright = starLayer(dir, 55.0, 0.993, seedB);

    float twA = 0.85 + 0.15 * sin(time * (0.8 + seedA * 1.6) + seedA * 40.0);
    float twB = 0.78 + 0.22 * sin(time * (0.6 + seedB * 1.2) + seedB * 40.0);

    // Color temperature: blue-white .. warm white
    vec3 colA = mix(vec3(0.65, 0.75, 1.00), vec3(1.00, 0.92, 0.78), seedA);
    vec3 colB = mix(vec3(0.70, 0.80, 1.00), vec3(1.00, 0.88, 0.70), seedB);

    col += colA * faint * (0.30 + 0.50 * seedA) * twA;
    col += colB * bright * (0.65 + 0.45 * seedB) * twB;
    // Halo around the bright layer for a touch of glow
    col += colB * bright * bright * 0.50;

    FragColor = vec4(col, 1.0);
}
