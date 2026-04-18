precision highp float;

SDF_LIGHTS_PLACEHOLDER

varying vec2 v_uv;
varying vec3 v_view;
varying vec3 v_normal;
varying vec3 v_position;
varying vec3 v_worldPos;

uniform vec3 color;
uniform float envIntensity;
uniform float u_lightIntensity;

#ifdef USE_AOMAP
uniform sampler2D aoMap;
uniform float aoMapIntensity;
#endif

void main() {
    vec3 v = normalize(v_view);
    vec3 n = normalize(v_normal);

    vec3 x = normalize(vec3(v.z, 0.0, -v.x));
    vec3 y = cross(v, x);
    vec2 uv = vec2(dot(x, n), dot(y, n)) * 0.495 + 0.5;

    float viewLift = mix(0.18, 0.72, uv.y);
    vec3 matcap = vec3(viewLift);

    float ao = 1.0;
#ifdef USE_AOMAP
    ao *= (texture2D(aoMap, v_uv).r - 1.0) * aoMapIntensity + 1.0;
#endif

    float rim = pow(1.0 - max(dot(n, v), 0.0), 2.0);
    vec3 diffuse = mix(color * 0.32, color * 1.06, envIntensity);
    vec3 irradiance = vec3(0.9, 0.95, 1.08) * getLightAttenuation(v_worldPos) * u_lightIntensity;

    vec3 outputColor = diffuse;
    outputColor += irradiance;
    outputColor *= matcap;
    outputColor *= ao;
    outputColor += rim * 0.04;
    outputColor *= mix(0.92, 1.04, smoothstep(-0.6, 1.4, v_position.y));

    gl_FragColor = vec4(outputColor, 1.0);

    #include <tonemapping_fragment>
    #include <colorspace_fragment>
}
