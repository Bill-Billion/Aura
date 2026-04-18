# Bug Fix Status — 2026-04-17

## Session Context
- Branch: `feat/mvp`
- Issues: colors inverted, devices not bound, scene not rendering

---

## Fixed

### 1. Color Inversion
**Root cause**: Previous session modified shader coolTint values, causing all colors to appear wrong.

**Files changed**:
- `src/shaders/matcap/fragment.glsl` — reverted diffuse/irradiance to original values
- `src/shaders/floor-reflector/fragment.glsl` — reverted diffuse/irradiance to original values
- `src/components/scene/SceneRenderer.vue` — reverted 4 background color values back to `0x1e242d`

### 2. Device Binding
**Root cause**: `ac_living_01` missing from `DEVICE_FLOOR_MAP`; curtain animation was hardcoded.

**Files changed**:
- `src/composables/useDeviceAnimations.ts`:
  - Added `ac_living_01: 'F1'` to `DEVICE_FLOOR_MAP`
  - Added `hvacNodes` Map for HVAC mesh tracking
  - Updated `registerDeviceNodes` to find HVAC meshes (`ac*`, `air*`) and broader curtain matching
  - Made curtain animation generic (iterates ALL curtain devices)
  - Added HVAC animation section (emissive color for cool/heat)

### 3. Render Loop Crash (Critical)
**Root cause**: `applyShaderMaterials` replaces ALL mesh materials with `ShaderMaterial` (K class). HVAC animation code called `mat.emissive.set()` on `ShaderMaterial`, which has no `emissive` property → TypeError → entire render loop crashes every frame → canvas stays empty/gray.

**Fix**: Added `isShaderMaterial` type check before accessing `emissive` properties. HVAC nodes with ShaderMaterial are now skipped.

**File changed**: `src/composables/useDeviceAnimations.ts:163-175`

---

## Known Remaining Issues

| Issue | Impact | Priority |
|-------|--------|----------|
| HVAC no visual feedback | ShaderMaterial has no emissive; AC on/off/cool/heat shows no color change | P2 |
| `floorMatCounts` always 0 | `registerDeviceNodes` looks for `u_lightIntensity` uniform but shaders use SDF light system (`lights`/`lightsInfo`). Not a bug — lighting works via `useLightUniforms` | N/A |

---

## HVAC Visual Feedback — Proposed Fix (if needed)

Option A: Exclude HVAC meshes from shader replacement in `applyShaderMaterials` (like `car` is excluded), keeping original `MeshStandardMaterial` so emissive works.

Option B: Modify HVAC ShaderMaterial's `color` uniform to simulate on/off state (blue tint for cool, red tint for heat).

---

## Architecture Notes

### Two Rendering Systems
- **Active**: `SceneRenderer.vue` — GLB-based, uses custom ShaderMaterial (K/z1/I3 classes) + SDF area lights
- **Unused**: `SceneManager.vue` / `RoomModule.vue` / `DeviceVisual.vue` — procedural 3D, not imported by App.vue

### Shader Material Classes
- **K class** (matcap): furniture, fixtures, appliances — `color`, `envIntensity`, SDF lights
- **Z1 class** (floor-reflector): indoor floor — higher matcap min, lower irradiance, semi-transparent
- **I3 class** (glass): walls — Fresnel transparency, height fade

### GLB Mesh Names
- F1: `ac1`, `ac2`, `curtain`, `curtain01`, `curtain02`, `wall`, `floor`, `car`, ...
- F2: `ac1`, `curtain`, `curtain01`-`02`, `curtain1`-`2`, `wall`, `floor`, ...
- F3: `air01`, `air02`, `curtain01`-`02`, `curtain1`-`2`, `wall`, `floor`, ...

### Backend Device IDs
- `light_living_01` (light, living_room)
- `light_bedroom_01` (light, bedroom)
- `ac_living_01` (hvac, living_room)
- `curtain_living_01` (curtain, living_room)
