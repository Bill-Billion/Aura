// I3 class fragment shader — walls and glass
// Fresnel transparency + height fade + matcap + SDF lights

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

void main() {
    vec3 v = normalize(v_view);
    vec3 n = normalize(v_normal);

    float NoV = dot(n, v);
    float NoY = dot(n, v_up);
    float top = step(0.98, NoY) * step(0.5, v_position.y);
    float mirror = step(0.0, v_up.y);

    // Matcap UV
    vec3 x = normalize(vec3(v.z, 0.0, -v.x));
    vec3 y = cross(v, x);
    vec2 uv = vec2(dot(x, n), dot(y, n)) * 0.495 + 0.5;

    // Wall matcap: slightly lower minimum than K class
    vec3 matcap = vec3(mix(0.2, 0.8, uv.y));

    vec3 diffuse = color;
    vec3 irradiance = mix(color * 0.5, vec3(1.0), envIntensity);

    // Top light calculation
    vec3 topLight = mix(vec3(1.0), vec3(0.5), getTopAttenuation(v_worldPos));
    diffuse = mix(diffuse, mix(topLight, vec3(0.9), envIntensity), mirror * top);

    // Alpha: Fresnel-based transparency
    float alpha = opacity;
    alpha = mix(0.2, 0.9, 1.0 - NoV) * alpha;
    alpha = mix(alpha, 1.0, opaque);
    alpha = mix(0.0, alpha, step(0.5, step(0.0, NoV)));
    alpha = mix(alpha, 1.0, top);
    alpha *= smoothstep(5.0, 15.0, v_view.z);

    // Combine
    vec3 outputColor = diffuse;
    outputColor += irradiance * getLightAttenuation(v_worldPos);
    outputColor *= matcap;
    outputColor *= mix(0.65, 1.0, smoothstep(0.0, 2.0, v_position.y));

    // Height-based alpha fade
    float a = 1.0 - smoothstep(0.0, 2.5, v_position.y);

    // Front/back face mixing
    gl_FragColor = mix(
        vec4(outputColor * 0.65, alpha * a),
        vec4(outputColor, alpha),
        mirror
    );

    #include <tonemapping_fragment>
    #include <colorspace_fragment>
}
