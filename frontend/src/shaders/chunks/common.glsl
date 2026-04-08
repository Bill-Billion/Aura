// Common utilities

vec3 linearToSRGB(vec3 color) {
  return pow(color, vec3(1.0 / 2.2));
}

vec3 sRGBToLinear(vec3 color) {
  return pow(color, vec3(2.2));
}

float pow4(float x) {
  float x2 = x * x;
  return x2 * x2;
}

// Fresnel Schlick approximation
float fresnelSchlick(float NoV, float F0) {
  return F0 + (1.0 - F0) * pow(1.0 - NoV, 5.0);
}
