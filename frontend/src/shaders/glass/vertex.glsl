// I3 class vertex shader — walls and glass

varying vec2 v_uv;
varying vec3 v_view;
varying vec3 v_normal;
varying vec3 v_worldPos;
varying vec3 v_position;
varying vec3 v_up;

void main() {
    vec4 worldPos = modelMatrix * vec4(position, 1.0);
    vec4 mvPosition = viewMatrix * worldPos;
    gl_Position = projectionMatrix * mvPosition;

    v_view = -mvPosition.xyz;
    v_worldPos = worldPos.xyz;
    v_normal = normalMatrix * normal;
    v_position = position.xyz;
    v_up = mat3(viewMatrix) * vec3(0.0, 1.0, 0.0);
    v_uv = uv;
}
