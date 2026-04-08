precision highp float;

varying vec2 v_uv;

uniform vec3 u_centerColor;
uniform vec3 u_edgeColor;
uniform vec2 u_center;
uniform float u_radius;

void main() {
  float dist = length((v_uv - u_center) / vec2(1.0, 0.7));
  float t = smoothstep(0.0, u_radius, dist);
  vec3 color = mix(u_centerColor, u_edgeColor, t);
  gl_FragColor = vec4(color, 1.0);
}
