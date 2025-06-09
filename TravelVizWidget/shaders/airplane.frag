#version 330 core
in float LightIntensity;
in vec3 FragNormal;

out vec4 FragColor;

uniform vec3 airplaneColor;

void main() {
    // Ambient lighting component
    float ambient = 0.3;
    
    // Diffuse lighting component  
    float diffuse = LightIntensity * 0.7;
    
    // Combine lighting
    float totalLight = ambient + diffuse;
    
    // Apply lighting to airplane color
    vec3 litColor = airplaneColor * totalLight;
    
    // Add slight specular highlight based on normal
    float specular = pow(max(dot(FragNormal, normalize(vec3(0.0, 1.0, 0.5))), 0.0), 16.0) * 0.2;
    litColor += vec3(specular);
    
    FragColor = vec4(litColor, 1.0);
}