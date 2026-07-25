# GLB 资产替换指南（S5-T7）

## 背景

当前 `frontend/public/models/F1.glb` / `F2.glb` / `F3.glb` 提取自 gamemcu.com 闭源
demo 站，无授权。S0 已替换 matcap/HDR 纹理，GLB 模型因工作量最大，排到 S5。

## GLB 节点命名契约（必须保留，否则整个 3D 场景断开）

替换后的 GLB 必须包含以下节点命名模式（大小写不敏感，`normalizeSceneNodeName` 会归一化）：

### 房间墙体节点（用于 shader 材质分配）
```
wall*    — I3 glass shader（墙体 Fresnel 描边）
floor*   — z1 floor-reflector shader
```

### 设备节点（sceneBindings.ts 通过正则匹配，格式: <前缀><楼层号><序号>）
```
curtain01  curtain02   — 窗帘（scale 动画，轴 Z）
fan01                 — 风扇（旋转动画，轴 Y）
ac1                   — 空调/HVAC（颜色/转速变化）
cam1                  — 摄像头（锥形 FOV 可视化）
light_living_01       — 客厅灯（SDF 灯光 + emissive 动画）
light_bedroom_01      — 卧室灯
light_kitchen_01      — 厨房灯
light_bathroom_01     — 浴室灯
light_loft_01         — 阁楼灯
hvac_living_01        — HVAC 设备（target_temp/mode 动画）
visualcone1           — 摄像头 FOV 锥形
effect1               — 灯光效果 mesh
```

### 楼层结构
```
F1  — 一楼（车库/入口层，Y ≈ 0-10）
F2  — 二楼（客厅/厨房层，Y ≈ 18-28）
F3  — 三楼（卧室/浴室层，Y ≈ 36-46）
```

## 推荐 CC0 资产来源

1. **Kenney** (kenney.nl) — CC0 家具/建筑套件
2. **Quaternius** (quaternius.com) — CC0 低多边形家具
3. **Poly Haven** (polyhaven.com) — CC0 HDR 环境贴图（已用于替换 gamemcu HDR）

## Blender kitbash 流程

```bash
# 1. 导入 CC0 套件到 Blender
# 2. 按楼层（F1/F2/F3）组织 collection
# 3. 节点命名严格遵循上述契约
# 4. 为每个楼层 bake AO（Cycles，1K 或 2K 纹理）
# 5. 导出 GLB：
#    - Format: glTF Binary (.glb)
#    - Include: Selected Objects
#    - Transform: +Y Up
#    - Compression: Draco + Meshopt (与现有 pipeline 一致)
```

## 替换步骤

```bash
# 1. 将新 GLB 放到 frontend/public/models/
cp F1_new.glb frontend/public/models/F1.glb
cp F2_new.glb frontend/public/models/F2.glb
cp F3_new.glb frontend/public/models/F3.glb

# 2. 调参 showroomVisualConfig
#    编辑 frontend/src/config/showroomVisualConfig.ts
#    调整 floorOffsets、camera 初始位置等

# 3. 重新构建
cd frontend && npm run build

# 4. 验证
./scripts/dev-stack.sh restart
# 打开 http://127.0.0.1:5173 确认三层楼正确渲染

# 5. 清 Git 历史（彻底删除旧 GLB）
#    ⚠️ 这步会 force-push，提前通知协作者
git filter-repo --path frontend/public/models/F1.glb \
                --path frontend/public/models/F2.glb \
                --path frontend/public/models/F3.glb \
                --path frontend/dist/models/F1.glb \
                --path frontend/dist/models/F2.glb \
                --path frontend/dist/models/F3.glb \
                --invert-paths --force
git push origin --force --all
```

## 过渡方案：无 GLB 开发模式

在 GLB 替换完成前，可通过环境变量跳过 GLB 加载，使用纯色方块占位：

```bash
VITE_NO_GLB=1 npm run dev
```

（此模式尚未实现，见下方 TODO）
