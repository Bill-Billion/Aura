// K class fragment shader — main objects
// Mathematical matcap + SDF area lights + AO
// Based on gamemcu's rendering approach

precision highp float;

// SDF light system (injected via string concatenation)
SDF_LIGHTS_PLACEHOLDER

varying vec2 v_uv;
varying vec3 v_view;
varying vec3 v_normal;
varying vec3 v_position;
varying vec3 v_worldPos;
varying vec3 v_up;

uniform vec3 color;
uniform float envIntensity;

#ifdef USE_AOMAP
uniform sampler2D aoMap;
uniform float aoMapIntensity;
#endif

void main() {
    vec3 v = normalize(v_view);
    vec3 n = normalize(v_normal);

    // Matcap UV from view-space normal
    vec3 x = normalize(vec3(v.z, 0.0, -v.x));
    vec3 y = cross(v, x);
    vec2 uv = vec2(dot(x, n), dot(y, n)) * 0.495 + 0.5;

    // Mathematical matcap: bottom dark (0.25) to top bright (0.8)
    vec3 matcap = vec3(mix(0.25, 0.8, uv.y));

    // Diffuse and irradiance based on environment intensity
    vec3 diffuse = mix(color * 0.15, color, envIntensity);
    vec3 irradiance = mix(color * 0.5, vec3(1.3), envIntensity);

    // AO from texture
    float ao = 1.0;
#ifdef USE_AOMAP
    ao *= (texture2D(aoMap, v_uv).r - 1.0) * aoMapIntensity + 1.0;
#endif

    // Combine: base diffuse + SDF light contribution
    vec3 outputColor = diffuse;
    outputColor += irradiance * getLightAttenuation(v_worldPos);
    outputColor *= matcap;
    outputColor *= ao;

    // Alpha: top fade (gamemcu exact formula)
    float a = 1.0 - smoothstep(-0.5, 1.5, v_position.y);
    gl_FragColor = vec4(outputColor, a);

    #include <tonemapping_fragment>
    #include <colorspace_fragment>
}
