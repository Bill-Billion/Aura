// z1 class fragment shader — indoor floor
// Higher matcap minimum (0.4 vs 0.25), lower irradiance (0.45 vs 1.3)

precision highp float;

SDF_LIGHTS_PLACEHOLDER

varying vec2 v_uv;
varying vec3 v_view;
varying vec3 v_normal;
varying vec3 v_worldPos;
varying vec3 v_position;

uniform vec3 color;
uniform float envIntensity;

void main() {
    vec3 v = normalize(v_view);
    vec3 n = normalize(v_normal);

    vec3 x = normalize(vec3(v.z, 0.0, -v.x));
    vec3 y = cross(v, x);
    vec2 uv = vec2(dot(x, n), dot(y, n)) * 0.495 + 0.5;

    // Floor matcap: brighter minimum than K class (0.4 vs 0.25)
    vec3 matcap = vec3(mix(0.4, 0.8, uv.y));

    // Floor diffuse/irradiance: dimmer irradiance than K class
    vec3 diffuse = mix(color * 0.5, color, envIntensity);
    vec3 irradiance = mix(color * 0.5, vec3(0.45), envIntensity);

    vec3 outputColor = diffuse;
    outputColor += irradiance * getLightAttenuation(v_worldPos);
    outputColor *= matcap;

    // Semi-transparent floor — more opaque than gamemcu's 0.3 for better visibility
    gl_FragColor = vec4(outputColor, 0.8);

    #include <tonemapping_fragment>
    #include <colorspace_fragment>
}
