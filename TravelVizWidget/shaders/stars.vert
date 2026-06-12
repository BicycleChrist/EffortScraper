#version 330 core
// Fullscreen triangle from gl_VertexID — no VBO needed
out vec2 NDC;

void main() {
    vec2 pos = vec2((gl_VertexID == 1) ? 3.0 : -1.0,
                    (gl_VertexID == 2) ? 3.0 : -1.0);
    NDC = pos;
    gl_Position = vec4(pos, 0.99999, 1.0);
}
