// SDF Lighting shared library
// Implements area light attenuation using signed distance fields

float sdRoundBox(vec3 p, vec3 b, float r) {
    vec3 q = abs(p) - b + r;
    return length(max(q, 0.0)) + min(max(q.x, max(q.y, q.z)), 0.0) - r;
}

float sdCircle(vec2 p, float r) {
    return length(p) - r;
}

float getTopAttenuation(in vec3 p) {
    float c1 = sdCircle(p.xz - vec2(3.0, 1.0), 2.0);
    float c2 = sdCircle(p.xz - vec2(-3.0, 1.0), 2.0);
    return smoothstep(0.0, 2.0, min(c1, c2));
}

#ifdef USE_LIGHT
    struct Light {
        vec4 position;
        vec3 size;
    };
    uniform Light lights[NUM_LIGHTS];
    uniform vec4 lightsInfo;
#endif

float getLightAttenuation(in vec3 p) {
#ifdef USE_LIGHT
    float atten = 1.0;
    for (int i = 0; i < NUM_LIGHTS; i++) {
        Light light = lights[i];
        float lightAtten = smoothstep(
            lightsInfo.x, lightsInfo.y,
            sdRoundBox(p - light.position.xyz, light.size.xyz, lightsInfo.z)
        );
        atten = min(atten, mix(1.0, lightAtten, light.position.w));
    }
    return 1.0 - atten;
#endif
    return 0.0;
}
