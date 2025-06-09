#version 330 core
in vec2 TexCoord;
in float LightIntensity;

out vec4 FragColor;

uniform sampler2D earthTexture;
uniform bool showAtmosphere;

void main() {
    vec3 earthColor = texture(earthTexture, TexCoord).rgb;
    float ambient = 0.15;
    float diffuse = LightIntensity * 0.85;
    vec3 litColor = earthColor * (ambient + diffuse);
    
    FragColor = vec4(litColor, 1.0);
}