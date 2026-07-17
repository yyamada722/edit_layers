# -*- coding: utf-8 -*-
"""拡張機能ストア用アイコン (256x256, 透過 PNG) をレンダリングする"""
import os

import bpy

OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "store_assets", "icon.png"
)
os.makedirs(os.path.dirname(OUT), exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def slab(z, color, name, dx=0.0, dy=0.0):
    """レイヤーを表す角丸スラブ"""
    bpy.ops.mesh.primitive_cube_add(location=(dx, dy, z))
    o = bpy.context.object
    o.name = name
    o.scale = (1.0, 1.0, 0.14)
    mod = o.modifiers.new("bevel", "BEVEL")
    mod.width = 0.09
    mod.segments = 4
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.45
    o.data.materials.append(mat)
    for p in o.data.polygons:
        p.use_smooth = True
    return o


# ベース (灰) → 共有レイヤー (青) → ブランチレイヤー (赤、少しずらす)
slab(0.0, (0.16, 0.16, 0.18), "base")
slab(0.55, (0.09, 0.31, 0.65), "layer_blue")
slab(1.10, (0.62, 0.09, 0.09), "layer_red", dx=0.30, dy=-0.20)

# カメラ (ターゲットに向ける)
bpy.ops.object.empty_add(location=(0.10, -0.08, 0.55))
target = bpy.context.object
bpy.ops.object.camera_add(location=(4.2, -4.2, 3.4))
cam = bpy.context.object
con = cam.constraints.new("TRACK_TO")
con.target = target
cam.data.type = "ORTHO"
cam.data.ortho_scale = 3.9
scene.camera = cam

# ライティング
bpy.ops.object.light_add(type="SUN", location=(2, -2, 4))
sun = bpy.context.object
sun.data.energy = 3.5
sun.rotation_euler = (0.7, 0.2, 0.6)
bpy.ops.object.light_add(type="AREA", location=(-2.5, -1.5, 2.5))
fill = bpy.context.object
fill.data.energy = 120.0
fill.data.size = 4.0
fill.rotation_euler = (0.9, 0.0, -0.9)

world = bpy.data.worlds.new("w")
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[1].default_value = 0.6
scene.world = world

# レンダー設定
try:
    scene.render.engine = "BLENDER_EEVEE_NEXT"
except Exception:
    scene.render.engine = "BLENDER_EEVEE"
scene.render.film_transparent = True
scene.render.resolution_x = 256
scene.render.resolution_y = 256
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
scene.render.filepath = OUT

bpy.ops.render.render(write_still=True)
print("icon rendered:", OUT)
