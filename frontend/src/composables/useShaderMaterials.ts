import * as THREE from 'three'

// Import GLSL shaders as raw strings
import sdfLightsChunk from '@/shaders/chunks/sdf-lights.glsl?raw'
import kClassVert from '@/shaders/matcap/vertex.glsl?raw'
import kClassFrag from '@/shaders/matcap/fragment.glsl?raw'
import z1ClassVert from '@/shaders/floor-reflector/vertex.glsl?raw'
import z1ClassFrag from '@/shaders/floor-reflector/fragment.glsl?raw'
import i3ClassVert from '@/shaders/glass/vertex.glsl?raw'
import i3ClassFrag from '@/shaders/glass/fragment.glsl?raw'

// Inject SDF library into fragment shaders
function injectSDF(frag: string): string {
  return frag.replace('SDF_LIGHTS_PLACEHOLDER', sdfLightsChunk)
}

export interface LightGroupConfig {
  lights: Array<{ position: THREE.Vector4; size: THREE.Vector3 }>
  lightsInfo: THREE.Vector4
}

export interface ShaderTextures {
  matcapRoughness: THREE.Texture
  matcapReflection: THREE.Texture
}

export function useShaderMaterials(_textures: ShaderTextures) {

  /**
   * K class: main objects (furniture, fixtures, etc.)
   * Color: 0x2F2F51, matcap range: 0.25-0.8, irradiance: 1.3
   */
  function createKClassMaterial(
    lightGroup: LightGroupConfig | null,
    options?: { color?: THREE.Color; aoMap?: THREE.Texture; envIntensity?: number }
  ): THREE.ShaderMaterial {
    const defines: Record<string, any> = {}
    const uniforms: Record<string, THREE.IUniform> = {
      color: { value: options?.color ?? new THREE.Color(0x2F2F51).convertLinearToSRGB() },
      envIntensity: { value: options?.envIntensity ?? 1.0 },
    }

    if (options?.aoMap) {
      defines.USE_AOMAP = true
      uniforms.aoMap = { value: options.aoMap }
      uniforms.aoMapIntensity = { value: 1.0 }
    }

    if (lightGroup && lightGroup.lights.length > 0) {
      defines.USE_LIGHT = true
      defines.NUM_LIGHTS = lightGroup.lights.length
      uniforms.lights = { value: lightGroup.lights }
      uniforms.lightsInfo = { value: lightGroup.lightsInfo }
    }

    return new THREE.ShaderMaterial({
      vertexShader: kClassVert,
      fragmentShader: injectSDF(kClassFrag),
      uniforms,
      defines,
      lights: false,
      transparent: false,  // K class objects are opaque
    })
  }

  /**
   * z1 class: indoor floor
   * Color: 0x4A5068, matcap range: 0.4-0.8, irradiance: 0.45
   * Smaller SDF light area than K class
   */
  function createZ1ClassMaterial(
    lightGroup: LightGroupConfig | null,
    options?: { color?: THREE.Color; envIntensity?: number }
  ): THREE.ShaderMaterial {
    const defines: Record<string, any> = {}
    const uniforms: Record<string, THREE.IUniform> = {
      color: { value: options?.color ?? new THREE.Color(0x4A5068).convertLinearToSRGB() },
      envIntensity: { value: options?.envIntensity ?? 1.0 },
    }

    if (lightGroup && lightGroup.lights.length > 0) {
      defines.USE_LIGHT = true
      defines.NUM_LIGHTS = lightGroup.lights.length
      uniforms.lights = { value: lightGroup.lights }
      uniforms.lightsInfo = { value: lightGroup.lightsInfo }
    }

    return new THREE.ShaderMaterial({
      vertexShader: z1ClassVert,
      fragmentShader: injectSDF(z1ClassFrag),
      uniforms,
      defines,
      lights: false,
      transparent: true,
    })
  }

  /**
   * I3 class: walls and glass
   * Color: 0x8BAFB1, Fresnel transparency, height fade
   * Uses wall-specific SDF lightGroup
   */
  function createI3ClassMaterial(
    lightGroup: LightGroupConfig | null,
    options?: { color?: THREE.Color; opacity?: number; envIntensity?: number }
  ): THREE.ShaderMaterial {
    const defines: Record<string, any> = {}
    const uniforms: Record<string, THREE.IUniform> = {
      color: { value: options?.color ?? new THREE.Color(0x8BAFB1).convertLinearToSRGB() },
      opacity: { value: options?.opacity ?? 1.0 },
      opaque: { value: 0.0 },
      envIntensity: { value: options?.envIntensity ?? 1.0 },
    }

    if (lightGroup && lightGroup.lights.length > 0) {
      defines.USE_LIGHT = true
      defines.NUM_LIGHTS = lightGroup.lights.length
      uniforms.lights = { value: lightGroup.lights }
      uniforms.lightsInfo = { value: lightGroup.lightsInfo }
    }

    return new THREE.ShaderMaterial({
      vertexShader: i3ClassVert,
      fragmentShader: injectSDF(i3ClassFrag),
      uniforms,
      defines,
      lights: false,
      transparent: true,
      depthWrite: true,
      side: THREE.DoubleSide,
    })
  }

  // Backward compat aliases
  const createMatcapMaterial = createKClassMaterial
  const createGlassMaterial = (opts?: any) => createI3ClassMaterial(null, opts)

  return {
    createKClassMaterial,
    createZ1ClassMaterial,
    createI3ClassMaterial,
    createMatcapMaterial,
    createGlassMaterial,
  }
}
