# -*- coding: utf-8 -*-
"""extensions.blender.org 用のプレビュー画像 (16:9, 1920x1080, 英語 UI) を撮影する"""
import os
import sys
import traceback

import bpy
import bmesh

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
assert os.path.basename(ROOT) == "edit_layers", ROOT
OUT_DIR = os.path.join(ROOT, "store_assets")
os.makedirs(OUT_DIR, exist_ok=True)

sys.path.insert(0, os.path.dirname(ROOT))
import edit_layers

edit_layers.register()

LOG = []


def log(msg):
    LOG.append(msg)
    print("[store]", msg)


def find_view3d():
    win = bpy.context.window_manager.windows[0]
    for area in win.screen.areas:
        if area.type == "VIEW_3D":
            return win, area
    return win, None


def win_region(area):
    for r in area.regions:
        if r.type == "WINDOW":
            return r
    return None


def shot(name):
    """ウィンドウ全体を撮って 16:9 / 1920x1080 に整える"""
    win, area = find_view3d()
    path = os.path.join(OUT_DIR, name)
    with bpy.context.temp_override(window=win, area=area, region=win_region(area)):
        bpy.ops.screen.screenshot(filepath=path)
    _to_1920x1080(path)
    log(f"shot {name}")


def _to_1920x1080(path):
    import numpy as np

    img = bpy.data.images.load(path)
    w, h = img.size
    px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)

    # 最大の中央 16:9 を切り出す
    target_h = int(w * 9 / 16)
    if target_h <= h:
        cw, ch = w, target_h
    else:
        ch = h
        cw = int(h * 16 / 9)
    x0 = (w - cw) // 2
    y0 = (h - ch) // 2
    cropped = px[y0 : y0 + ch, x0 : x0 + cw, :]

    out = bpy.data.images.new("crop", width=cw, height=ch, alpha=True)
    out.pixels[:] = cropped.ravel()
    out.scale(1920, 1080)
    out.filepath_raw = path
    out.file_format = "PNG"
    out.save()
    log(f"  {w}x{h} -> crop {cw}x{ch} -> 1920x1080")
    bpy.data.images.remove(img)
    bpy.data.images.remove(out)


def obj_override():
    win, area = find_view3d()
    obj = bpy.data.objects.get("Cube")
    return dict(
        window=win,
        area=area,
        region=win_region(area),
        object=obj,
        active_object=obj,
        selected_objects=[obj],
    )


# ---------- ステップ ----------


def s_dismiss():
    win, _ = find_view3d()
    win.cursor_warp(win.width // 2, win.height // 2)
    bpy.context.preferences.view.ui_scale = 1.15
    log("start")


def s_setup():
    win, area = find_view3d()
    area.spaces.active.show_region_ui = True

    # サイドバーの他パネルを退かして自パネルだけにする (撮影用)
    def all_side_panels():
        found = []
        stack_cls = list(bpy.types.Panel.__subclasses__())
        seen = set()
        while stack_cls:
            cls = stack_cls.pop()
            if cls in seen:
                continue
            seen.add(cls)
            stack_cls.extend(cls.__subclasses__())
            if (
                getattr(cls, "bl_space_type", "") == "VIEW_3D"
                and getattr(cls, "bl_region_type", "") == "UI"
                and getattr(cls, "is_registered", False)
                and cls is not edit_layers.EL_PT_panel
            ):
                found.append(cls)
        return found

    for cls in all_side_panels():
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    bpy.utils.unregister_class(edit_layers.EL_PT_panel)
    edit_layers.EL_PT_panel.bl_category = "Item"
    bpy.utils.register_class(edit_layers.EL_PT_panel)

    obj = bpy.context.scene.objects.get("Cube")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.stack_init()
        bpy.ops.edit_layers.record_new(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    for v in bm.verts:
        if v.co.z > 0.5:
            v.co.z += 1.0
    bmesh.update_edit_mesh(obj.data)
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.commit()
        bpy.ops.edit_layers.record_new(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    top = max(bm.faces, key=lambda f: f.calc_center_median().z)
    ret = bmesh.ops.extrude_face_region(bm, geom=[top])
    for g in ret["geom"]:
        if isinstance(g, bmesh.types.BMVert):
            g.co.z += 0.8
    if top.is_valid:
        bmesh.ops.delete(bm, geom=[top], context="FACES_ONLY")
    bmesh.update_edit_mesh(obj.data)
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.commit()

    stack = obj.edit_layers
    stack.layers[0].name = "Base Lift"
    stack.layers[1].name = "Extrude Top"

    stack.active_index = 0
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.branch_create()
        bpy.ops.edit_layers.record_new(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    front = min(bm.faces, key=lambda f: f.calc_center_median().y)
    bmesh.ops.delete(bm, geom=[front], context="FACES_ONLY")
    bmesh.update_edit_mesh(obj.data)
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.commit()
    stack.layers[2].name = "Open Front"
    stack.branches[1].name = "Variant A"

    stack.active_branch = 0
    stack.active_index = 0

    import math
    from mathutils import Euler

    rv3d = find_view3d()[1].spaces.active.region_3d
    rv3d.view_perspective = "PERSP"
    rv3d.view_rotation = Euler(
        (math.radians(70), 0.0, math.radians(-30)), "XYZ"
    ).to_quaternion()
    for o in bpy.data.objects:
        o.select_set(False)
        if o.type != "MESH":
            o.hide_set(True)  # カメラ/ライトのワイヤーを写さない
    win, area = find_view3d()
    with bpy.context.temp_override(window=win, area=area, region=win_region(area)):
        bpy.ops.view3d.view_all(center=False)
        bpy.ops.view3d.zoom(delta=1)
    log("setup done")


def s_shot_layers():
    shot("preview_layers.png")


def s_compare():
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.compare()
    for o in bpy.data.objects:
        o.select_set(False)
    win, area = find_view3d()
    with bpy.context.temp_override(window=win, area=area, region=win_region(area)):
        bpy.ops.view3d.view_all(center=False)
        bpy.ops.view3d.zoom(delta=1)


def s_shot_featured():
    shot("featured_compare.png")
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.compare_clear()
    win, area = find_view3d()
    with bpy.context.temp_override(window=win, area=area, region=win_region(area)):
        bpy.ops.view3d.view_all(center=False)


def s_influence():
    obj = bpy.data.objects["Cube"]
    obj.edit_layers.show_influence = True
    obj.edit_layers.active_index = 1
    find_view3d()[1].tag_redraw()


def s_shot_influence():
    shot("preview_influence.png")
    bpy.data.objects["Cube"].edit_layers.show_influence = False


def s_quit():
    log("DONE " + ",".join(sorted(os.listdir(OUT_DIR))))
    bpy.ops.wm.quit_blender()


STEPS = [
    s_dismiss,
    s_setup,
    s_shot_layers,
    s_compare,
    s_shot_featured,
    s_influence,
    s_shot_influence,
    s_quit,
]
_i = [0]


def tick():
    i = _i[0]
    if i >= len(STEPS):
        return None
    _i[0] += 1
    try:
        STEPS[i]()
    except Exception:
        traceback.print_exc()
        log(f"STEP {i} FAILED")
    return 0.9


def failsafe():
    import os as _os

    print("[store] failsafe exit")
    _os._exit(0)


bpy.app.timers.register(tick, first_interval=1.5)
bpy.app.timers.register(failsafe, first_interval=40.0)
