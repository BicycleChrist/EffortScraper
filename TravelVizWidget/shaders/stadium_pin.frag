#version 330 core
in vec3 FragNormal;
in float Height;

out vec4 FragColor;

uniform vec3 pinColor;
uniform bool isSelected;
uniform bool isHovered;

void main() {
    vec3 lightDir = normalize(vec3(1.0, 1.0, 0.5));
    vec3 normal = normalize(FragNormal);

    // Basic lighting
    float ambient = 0.35;
    float diffuse = max(dot(normal, lightDir), 0.0) * 0.65;

    vec3 color = pinColor * (ambient + diffuse);

    // Vertical gradient (darker at base, brighter at top)
    color = mix(color * 0.7, color * 1.1, clamp(Height + 0.5, 0.0, 1.0));

    // Selection highlight - golden glow
    if (isSelected) {
        color = mix(color, vec3(1.0, 0.85, 0.3), 0.4);
    }

    // Hover highlight - subtle cyan tint
    if (isHovered) {
        color = mix(color, vec3(0.4, 0.8, 1.0), 0.25);
    }

    FragColor = vec4(color, 1.0);
}
