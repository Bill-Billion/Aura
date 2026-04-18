precision highp float;

varying vec2 v_uv;

uniform vec3 u_centerColor;
uniform vec3 u_edgeColor;
uniform vec3 u_shadowColor;
uniform vec3 u_reflectionColor;
uniform vec2 u_center;
uniform float u_radius;
uniform float u_time;

float ellipse(vec2 uv, vec2 center, vec2 radius) {
  vec2 delta = (uv - center) / radius;
  return exp(-dot(delta, delta));
}

void main() {
  vec2 p = v_uv;
  float radial = length((p - u_center) / vec2(1.0, 0.72));
  float vignette = smoothstep(0.04, u_radius, radial);

  float floorShadow = ellipse(p, vec2(0.49, 0.49), vec2(0.19, 0.08));
  float towerShadow = ellipse(p, vec2(0.49, 0.43), vec2(0.11, 0.2));
  float carShadow = ellipse(p, vec2(0.71, 0.54), vec2(0.1, 0.04));

  float reflectionCore = ellipse(p, vec2(0.5, 0.52), vec2(0.16, 0.11));
  float reflectionTail = ellipse(p, vec2(0.53, 0.6), vec2(0.22, 0.18));
  float sweep = 0.5 + 0.5 * sin(u_time * 1.35 + p.x * 18.0);
  float reflection = reflectionCore * 0.9 + reflectionTail * 0.55 * sweep;

  vec3 color = mix(u_centerColor, u_edgeColor, vignette);
  color -= u_shadowColor * (floorShadow * 0.45 + towerShadow * 0.65 + carShadow * 0.5);
  color += u_reflectionColor * reflection * 0.26;
  color += vec3(0.02, 0.03, 0.04) * ellipse(p, vec2(0.48, 0.36), vec2(0.34, 0.24));

  gl_FragColor = vec4(color, 1.0);
}
