// z1 class vertex shader — indoor floor

varying vec2 v_uv;
varying vec3 v_view;
varying vec3 v_normal;
varying vec3 v_worldPos;
varying vec3 v_position;

void main() {
    vec4 worldPos = modelMatrix * vec4(position, 1.0);
    vec4 mvPosition = viewMatrix * worldPos;
    gl_Position = projectionMatrix * mvPosition;

    v_view = -mvPosition.xyz;
    v_worldPos = worldPos.xyz;
    v_normal = normalMatrix * normal;
    v_position = position.xyz;
    v_uv = uv;
}
