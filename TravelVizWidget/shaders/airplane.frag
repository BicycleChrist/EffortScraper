#version 330 core
in float LightIntensity;
in vec3 FragNormal;

out vec4 FragColor;

uniform vec3 airplaneColor;

void main() {
    // Metallic material properties
    float ambient = 0.2;
    float diffuse = LightIntensity * 0.4;  // Reduced diffuse for metallic look

    // Specular for reflective metal
    vec3 lightDir = normalize(vec3(1.0, 0.3, 0.5));
    vec3 viewDir = normalize(vec3(0.0, 0.0, 1.0)); // Simplified view direction
    vec3 reflectDir = reflect(-lightDir, normalize(FragNormal));

    // Multiple specular highlights for realistic metal
    float specular1 = pow(max(dot(viewDir, reflectDir), 0.0), 64.0) * 0.8;  // Sharp highlight
    float specular2 = pow(max(dot(viewDir, reflectDir), 0.0), 16.0) * 0.3;  // Broader highlight
    float totalSpecular = specular1 + specular2;

    // Fresnel-like effect for metallic reflection
    float fresnel = pow(1.0 - max(dot(normalize(FragNormal), viewDir), 0.0), 2.0);
    float metallicReflection = fresnel * 0.4;

    // Combine lighting with metallic properties
    float totalLight = ambient + diffuse;
    vec3 baseColor = airplaneColor * totalLight;

    // Add metallic reflections
    vec3 metallicColor = baseColor + vec3(totalSpecular) + vec3(metallicReflection);

    // Slight blue tint for realistic aircraft metal
    metallicColor += vec3(0.05, 0.1, 0.15) * fresnel;

    FragColor = vec4(metallicColor, 1.0);
}
