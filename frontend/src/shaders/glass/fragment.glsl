precision highp float;

SDF_LIGHTS_PLACEHOLDER

varying vec2 v_uv;
varying vec3 v_view;
varying vec3 v_normal;
varying vec3 v_worldPos;
varying vec3 v_position;
varying vec3 v_up;

uniform vec3 color;
uniform float opacity;
uniform float opaque;
uniform float envIntensity;
uniform float u_lightIntensity;

void main() {
    vec3 v = normalize(v_view);
    vec3 n = normalize(v_normal);

    float NoV = max(dot(n, v), 0.0);
    float fresnel = pow(1.0 - NoV, 2.8);
    float verticalFade = smoothstep(-0.2, 2.6, v_position.y);
    float topMask = smoothstep(0.92, 1.0, dot(n, v_up));

    vec3 x = normalize(vec3(v.z, 0.0, -v.x));
    vec3 y = cross(v, x);
    vec2 uv = vec2(dot(x, n), dot(y, n)) * 0.495 + 0.5;

    vec3 matcap = vec3(mix(0.16, 0.58, uv.y));
    vec3 diffuse = mix(color * 0.32, color * 0.74, envIntensity);
    vec3 lightBloom = vec3(0.76, 0.84, 0.96) * getLightAttenuation(v_worldPos) * u_lightIntensity;
    vec3 haze = mix(vec3(0.11, 0.13, 0.16), color, 0.26 + fresnel * 0.24);

    vec3 outputColor = diffuse;
    outputColor += lightBloom * 0.32;
    outputColor *= matcap;
    outputColor = mix(outputColor, haze, 0.34 + fresnel * 0.18);
    outputColor += topMask * 0.03;

    float alpha = mix(0.05, 0.28, fresnel) * opacity;
    alpha *= mix(0.78, 1.0, verticalFade);
    alpha = mix(alpha, 1.0, opaque);

    gl_FragColor = vec4(outputColor, alpha);

    #include <tonemapping_fragment>
    #include <colorspace_fragment>
}
