#version 330 core
in vec2 MarkerTexCoord;
in float LightIntensity;
in vec3 WorldPosition;

out vec4 FragColor;

uniform vec3 teamColor;
uniform vec3 homeTeamColor;
uniform vec3 awayTeamColor;
uniform float markerAlpha;
uniform float time;
uniform bool useTexture;
uniform bool useHomeTexture;
uniform bool useAwayTexture;
uniform bool isSplitCube;
uniform bool isAnimated;
uniform sampler2D markerTexture;
uniform sampler2D homeTeamTexture;
uniform sampler2D awayTeamTexture;

void main() {
    vec3 finalColor = teamColor;
    float finalAlpha = markerAlpha;
    
    if (isSplitCube) {
        // Split cube logic - top half vs bottom half
        if (WorldPosition.y > 0.0) {
            // Top half - home team
            if (useHomeTexture) {
                vec4 texColor = texture(homeTeamTexture, MarkerTexCoord);
                if (texColor.a > 0.01) {
                    finalColor = texColor.rgb * LightIntensity;
                    finalAlpha *= texColor.a;
                } else {
                    finalColor = homeTeamColor * LightIntensity;
                }
            } else {
                finalColor = homeTeamColor * LightIntensity;
            }
        } else {
            // Bottom half - away team  
            if (useAwayTexture) {
                vec4 texColor = texture(awayTeamTexture, MarkerTexCoord);
                if (texColor.a > 0.01) {
                    finalColor = texColor.rgb * LightIntensity;
                    finalAlpha *= texColor.a;
                } else {
                    finalColor = awayTeamColor * LightIntensity;
                }
            } else {
                finalColor = awayTeamColor * LightIntensity;
            }
        }
        
        // Add white border at split line
        if (abs(WorldPosition.y) < 0.02) {
            finalColor = mix(finalColor, vec3(1.0, 1.0, 1.0), 0.15);
        }
    } else {
        // Single-team cube logic
        if (useTexture) {
            vec4 texColor = texture(markerTexture, MarkerTexCoord);
            if (texColor.a > 0.01) {
                finalColor = texColor.rgb * LightIntensity;
                finalAlpha *= texColor.a;
            } else {
                finalColor = teamColor * LightIntensity;
            }
        } else {
            finalColor = teamColor * LightIntensity;
        }
    }
    
    if (isAnimated) {
        float glow = sin(time * 3.0) * 0.4 + 0.6;
        finalColor *= glow;
        finalColor += vec3(1.0, 0.8, 0.2) * 0.3;
    }
    
    FragColor = vec4(finalColor, finalAlpha);
}