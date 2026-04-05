// src/types/scene-config.ts

export type Vec3 = [number, number, number]

export interface DeviceAnchor {
  anchor: Vec3
  type: 'light' | 'hvac' | 'curtain'
}

export interface RoomConfig {
  id: string
  type: string
  model?: string
  position: Vec3
  rotation: Vec3
  devices: Record<string, DeviceAnchor>
}

export interface SceneConfig {
  id: string
  name: string
  rooms: RoomConfig[]
}