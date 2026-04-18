import * as THREE from 'three'
import { showroomVisualConfig, type ShowroomMaterialRole } from '@/config/showroomVisualConfig'

import sdfLightsChunk from '@/shaders/chunks/sdf-lights.glsl?raw'
import furnitureVert from '@/shaders/matcap/vertex.glsl?raw'
import furnitureFrag from '@/shaders/matcap/fragment.glsl?raw'
import floorVert from '@/shaders/floor-reflector/vertex.glsl?raw'
import floorFrag from '@/shaders/floor-reflector/fragment.glsl?raw'
import wallVert from '@/shaders/glass/vertex.glsl?raw'
import wallFrag from '@/shaders/glass/fragment.glsl?raw'

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

interface VehicleSweepRuntime {
  sweep: THREE.IUniform<number>
  pulse: THREE.IUniform<number>
}

function buildLightUniformBlock(lightGroup: LightGroupConfig | null) {
  const defines: Record<string, unknown> = {}
  const uniforms: Record<string, THREE.IUniform> = {
    u_lightIntensity: { value: 1.0 },
  }

  if (lightGroup && lightGroup.lights.length > 0) {
    defines.USE_LIGHT = true
    defines.NUM_LIGHTS = lightGroup.lights.length
    uniforms.lights = { value: lightGroup.lights }
    uniforms.lightsInfo = { value: lightGroup.lightsInfo }
  }

  return { defines, uniforms }
}

function makeStandardMaterial(base?: THREE.Material | null): THREE.MeshStandardMaterial {
  if (base instanceof THREE.MeshStandardMaterial || base instanceof THREE.MeshPhysicalMaterial) {
    return base.clone()
  }

  const source = base as THREE.Material & { color?: THREE.Color; transparent?: boolean; opacity?: number } | null
  return new THREE.MeshStandardMaterial({
    color: source?.color?.clone() ?? new THREE.Color(showroomVisualConfig.materialPalette.furniture),
    transparent: source?.transparent ?? false,
    opacity: source?.opacity ?? 1,
  })
}

export function useShaderMaterials(_textures: ShaderTextures) {
  const vehicleSweeps: VehicleSweepRuntime[] = []

  function createFurnitureMaterial(
    lightGroup: LightGroupConfig | null,
    options?: { color?: THREE.Color; aoMap?: THREE.Texture; envIntensity?: number }
  ): THREE.ShaderMaterial {
    const lightBlock = buildLightUniformBlock(lightGroup)
    const defines = { ...lightBlock.defines } as Record<string, unknown>
    const uniforms: Record<string, THREE.IUniform> = {
      ...lightBlock.uniforms,
      color: { value: options?.color ?? new THREE.Color(showroomVisualConfig.materialPalette.furniture) },
      envIntensity: { value: options?.envIntensity ?? 1.0 },
    }

    if (options?.aoMap) {
      defines.USE_AOMAP = true
      uniforms.aoMap = { value: options.aoMap }
      uniforms.aoMapIntensity = { value: 1.0 }
    }

    return new THREE.ShaderMaterial({
      vertexShader: furnitureVert,
      fragmentShader: injectSDF(furnitureFrag),
      uniforms,
      defines,
      transparent: false,
    })
  }

  function createFloorDeckMaterial(
    lightGroup: LightGroupConfig | null,
    options?: { color?: THREE.Color; envIntensity?: number }
  ): THREE.ShaderMaterial {
    const lightBlock = buildLightUniformBlock(lightGroup)
    return new THREE.ShaderMaterial({
      vertexShader: floorVert,
      fragmentShader: injectSDF(floorFrag),
      uniforms: {
        ...lightBlock.uniforms,
        color: { value: options?.color ?? new THREE.Color(showroomVisualConfig.materialPalette.floorDeck) },
        envIntensity: { value: options?.envIntensity ?? 0.9 },
      },
      defines: lightBlock.defines,
      transparent: true,
      depthWrite: false,
    })
  }

  function createWallGlassMaterial(
    lightGroup: LightGroupConfig | null,
    options?: { color?: THREE.Color; opacity?: number; envIntensity?: number }
  ): THREE.ShaderMaterial {
    const lightBlock = buildLightUniformBlock(lightGroup)
    return new THREE.ShaderMaterial({
      vertexShader: wallVert,
      fragmentShader: injectSDF(wallFrag),
      uniforms: {
        ...lightBlock.uniforms,
        color: { value: options?.color ?? new THREE.Color(showroomVisualConfig.materialPalette.wallGlass) },
        opacity: { value: options?.opacity ?? 1.0 },
        opaque: { value: 0.0 },
        envIntensity: { value: options?.envIntensity ?? 0.88 },
      },
      defines: lightBlock.defines,
      transparent: true,
      depthWrite: false,
      side: THREE.DoubleSide,
    })
  }

  function createApplianceMaterial(
    baseMaterial?: THREE.Material | null,
    options?: { color?: THREE.Color; metalness?: number; roughness?: number; envIntensity?: number }
  ): THREE.MeshStandardMaterial {
    const material = makeStandardMaterial(baseMaterial)
    material.color = (options?.color ?? material.color.clone()).clone().lerp(new THREE.Color(showroomVisualConfig.materialPalette.applianceMetal), 0.22)
    material.metalness = options?.metalness ?? 0.36
    material.roughness = options?.roughness ?? 0.42
    material.envMapIntensity = options?.envIntensity ?? 1.35
    material.transparent = false
    material.depthWrite = true
    material.emissive ??= new THREE.Color(0x000000)
    return material
  }

  function createVehicleMaterial(baseMaterial?: THREE.Material | null): THREE.MeshStandardMaterial {
    const material = makeStandardMaterial(baseMaterial)
    material.color = material.color.clone().lerp(new THREE.Color(showroomVisualConfig.materialPalette.vehicleFx), 0.18)
    material.metalness = 0.58
    material.roughness = 0.26
    material.envMapIntensity = 1.6

    const sweep: THREE.IUniform<number> = { value: -1.2 }
    const pulse: THREE.IUniform<number> = { value: 0.6 }
    const sweepColor: THREE.IUniform<THREE.Color> = { value: new THREE.Color(0xfff1b3) }

    material.onBeforeCompile = (shader) => {
      shader.uniforms.uSweep = sweep
      shader.uniforms.uPulse = pulse
      shader.uniforms.uSweepColor = sweepColor
      shader.fragmentShader = shader.fragmentShader.replace(
        'void main() {',
        'uniform float uSweep;\nuniform float uPulse;\nuniform vec3 uSweepColor;\nvoid main() {'
      )
      shader.fragmentShader = shader.fragmentShader.replace(
        'vec3 outgoingLight = totalDiffuse + totalSpecular + totalEmissiveRadiance;',
        `
        float sweepCoord = (-vViewPosition.x * 0.18) + (vViewPosition.z * 0.08) + (normal.z * 0.06);
        float sweepBand = smoothstep(uSweep - 0.26, uSweep - 0.04, sweepCoord) * (1.0 - smoothstep(uSweep + 0.04, uSweep + 0.28, sweepCoord));
        vec3 sweepLight = uSweepColor * sweepBand * (0.12 + 0.12 * uPulse);
        vec3 outgoingLight = totalDiffuse + totalSpecular + totalEmissiveRadiance + sweepLight;
        `
      )
    }
    material.customProgramCacheKey = () => 'showroom-vehicle-sweep-v1'

    vehicleSweeps.push({ sweep, pulse })
    return material
  }

  function createSignageMaterial(baseMaterial?: THREE.Material | null): THREE.MeshStandardMaterial {
    const material = makeStandardMaterial(baseMaterial)
    material.color = material.color.clone().lerp(new THREE.Color(showroomVisualConfig.materialPalette.signage), 0.55)
    material.metalness = 0.22
    material.roughness = 0.66
    material.envMapIntensity = 0.6
    return material
  }

  function createReflectionMaterial(role: ShowroomMaterialRole): THREE.Material {
    const opacityByRole: Record<ShowroomMaterialRole, number> = {
      wallGlass: 0.05,
      floorDeck: 0.12,
      furniture: 0.11,
      applianceMetal: 0.14,
      vehicleFx: 0.18,
      signage: 0.06,
      reflection: 0.14,
    }

    return new THREE.MeshBasicMaterial({
      color: new THREE.Color(showroomVisualConfig.materialPalette.reflection),
      transparent: true,
      opacity: opacityByRole[role],
      depthWrite: false,
      blending: THREE.NormalBlending,
    })
  }

  function updateShowroomEffects(_dt: number, elapsed: number) {
    const sweepValue = ((elapsed * 0.26) % 2.2) - 1.1
    const pulseValue = 0.5 + Math.sin(elapsed * 2.2) * 0.5
    for (const runtime of vehicleSweeps) {
      runtime.sweep.value = sweepValue
      runtime.pulse.value = pulseValue
    }
  }

  const createMatcapMaterial = createFurnitureMaterial
  const createGlassMaterial = (opts?: { color?: THREE.Color; opacity?: number; envIntensity?: number }) => createWallGlassMaterial(null, opts)

  return {
    createFurnitureMaterial,
    createFloorDeckMaterial,
    createWallGlassMaterial,
    createApplianceMaterial,
    createVehicleMaterial,
    createSignageMaterial,
    createReflectionMaterial,
    createMatcapMaterial,
    createGlassMaterial,
    updateShowroomEffects,
  }
}
