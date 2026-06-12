#version 330 core
in vec2 TexCoord;
in float LightIntensity;
in vec3 WorldNormal;
in vec3 WorldPos;

out vec4 FragColor;

uniform sampler2D earthTexture;
uniform sampler2D nightTexture;
uniform bool hasNightTexture;
uniform bool showAtmosphere;
uniform bool showDayNight;
uniform vec3 sunDirection;
uniform vec3 cameraPos;
uniform float terminatorWidth;

// City lights for the dark side: boost the warm tones of the night map
vec3 cityLights(float darkness) {
    if (!hasNightTexture || darkness <= 0.0) {
        return vec3(0.0);
    }
    vec3 lights = texture(nightTexture, TexCoord).rgb;
    // Suppress the night map's ambient blue haze, keep the sodium glow
    lights = max(lights - vec3(0.03), 0.0) * vec3(1.0, 0.85, 0.6) * 1.6;
    return lights * darkness;
}

void main() {
    vec3 earthColor = texture(earthTexture, TexCoord).rgb;
    vec3 normal = normalize(WorldNormal);
    vec3 viewDir = normalize(cameraPos - WorldPos);
    vec3 finalColor;

    if (showDayNight) {
        float sunDot = dot(normal, sunDirection);

        // Smooth day/night transition
        float dayFactor = smoothstep(-terminatorWidth, terminatorWidth, sunDot);

        // Night side: darkened with subtle blue tint, plus city lights
        vec3 nightColor = earthColor * 0.08 + vec3(0.005, 0.01, 0.02);
        nightColor += cityLights(1.0 - dayFactor);

        // Day side: normal lighting
        float dayAmbient = 0.15;
        float dayDiffuse = max(sunDot, 0.0) * 0.85;
        vec3 dayColor = earthColor * (dayAmbient + dayDiffuse);

        finalColor = mix(nightColor, dayColor, dayFactor);
    } else {
        // Original simple lighting; let city lights peek through on the dark limb
        float ambient = 0.15;
        float diffuse = LightIntensity * 0.85;
        finalColor = earthColor * (ambient + diffuse);
        finalColor += cityLights(clamp(1.0 - LightIntensity * 2.5, 0.0, 1.0)) * 0.7;
    }

    if (showAtmosphere) {
        // Fresnel rim glow: blue scattering on the limb of the planet
        float rim = pow(1.0 - max(dot(normal, viewDir), 0.0), 2.5);
        // On the night side the atmosphere barely glows
        float litRim = showDayNight
            ? clamp(dot(normal, sunDirection) * 0.5 + 0.6, 0.15, 1.0)
            : 1.0;
        finalColor += vec3(0.18, 0.38, 0.75) * rim * 0.55 * litRim;
    }

    FragColor = vec4(finalColor, 1.0);
}
