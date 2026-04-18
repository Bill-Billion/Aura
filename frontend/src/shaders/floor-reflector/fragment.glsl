precision highp float;

SDF_LIGHTS_PLACEHOLDER

varying vec2 v_uv;
varying vec3 v_view;
varying vec3 v_normal;
varying vec3 v_worldPos;
varying vec3 v_position;

uniform vec3 color;
uniform float envIntensity;
uniform float u_lightIntensity;

void main() {
    vec3 v = normalize(v_view);
    vec3 n = normalize(v_normal);

    vec3 x = normalize(vec3(v.z, 0.0, -v.x));
    vec3 y = cross(v, x);
    vec2 uv = vec2(dot(x, n), dot(y, n)) * 0.495 + 0.5;

    float fresnel = pow(1.0 - max(dot(n, v), 0.0), 2.0);
    vec3 matcap = vec3(mix(0.28, 0.76, uv.y));

    vec3 diffuse = mix(color * 0.18, color * 0.54, envIntensity);
    vec3 coldGlow = vec3(0.16, 0.19, 0.24) * fresnel;
    vec3 lightBounce = vec3(0.58, 0.62, 0.7) * getLightAttenuation(v_worldPos) * u_lightIntensity;

    vec3 outputColor = diffuse;
    outputColor += coldGlow;
    outputColor += lightBounce * 0.48;
    outputColor *= matcap;
    outputColor *= mix(0.86, 1.02, smoothstep(-0.6, 0.8, v_position.y));

    float alpha = mix(0.22, 0.42, fresnel);
    gl_FragColor = vec4(outputColor, alpha);

    #include <tonemapping_fragment>
    #include <colorspace_fragment>
}
