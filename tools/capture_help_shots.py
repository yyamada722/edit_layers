# -*- coding: utf-8 -*-
"""ヘルプ用スクリーンショットを自動撮影する (UI モードで実行)

blender --factory-startup --window-geometry 100 60 1500 1000 <空の.blend> --python tools/capture_help_shots.py
"""
import os
import sys
import traceback

import bpy
import bmesh

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
assert os.path.basename(ROOT) == "edit_layers", ROOT
OUT_DIR = os.path.join(ROOT, "docs", "images")
os.makedirs(OUT_DIR, exist_ok=True)

sys.path.insert(0, os.path.dirname(ROOT))
import edit_layers

edit_layers.register()

# 日本語 UI で撮影する
try:
    bpy.context.preferences.view.language = "ja_JP"
    bpy.context.preferences.view.use_translate_interface = True
except Exception:
    traceback.print_exc()

# パネルショットはサイドバー部分だけに切り出す
CROP = {"panel_layers.png", "panel_recording.png", "panel_rescue.png"}

LOG = []


def log(msg):
    LOG.append(msg)
    print("[capture]", msg)


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


def ui_region(area):
    for r in area.regions:
        if r.type == "UI":
            return r
    return None


def shot(name):
    win, area = find_view3d()
    path = os.path.join(OUT_DIR, name)
    if name in CROP:
        # サイドバーを最下部までスクロールして自パネルを見えるようにする
        r = ui_region(area)
        with bpy.context.temp_override(window=win, area=area, region=r):
            for _ in range(6):
                bpy.ops.view2d.scroll_down(page=True)
    with bpy.context.temp_override(window=win, area=area, region=win_region(area)):
        bpy.ops.screen.screenshot_area(filepath=path)
    if name in CROP:
        r = ui_region(area)
        _crop_right(path, r.width + 4, r.height)
    log(f"shot {name}")


def _crop_right(path, width, height):
    """スクリーンショットを右下 (サイドバー領域) の width x height に切り出す"""
    import numpy as np

    img = bpy.data.images.load(path)
    w, h = img.size
    px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    width = min(width, w)
    height = min(height, h)
    # Blender のピクセルは下から上の順なので、下 height 行 = リージョン範囲
    cropped = px[:height, w - width:, :]
    out = bpy.data.images.new("crop", width=width, height=height, alpha=True)
    out.pixels[:] = cropped.ravel()
    out.filepath_raw = path
    out.file_format = "PNG"
    out.save()
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


def edit_top_verts(dz=1.0, dx=0.0):
    obj = bpy.data.objects["Cube"]
    bm = bmesh.from_edit_mesh(obj.data)
    for v in bm.verts:
        if v.co.z > 0.5:
            v.co.z += dz
            v.co.x += dx
    bmesh.update_edit_mesh(obj.data)


# ---------- ステップ ----------


def s_dismiss_splash():
    win, area = find_view3d()
    win.cursor_warp(win.width // 2, win.height // 2)
    win.cursor_warp(win.width // 2 - 5, win.height // 2 - 5)
    # UI を少し拡大して読みやすくする
    bpy.context.preferences.view.ui_scale = 1.25
    log("splash dismissed")


def s_setup():
    win, area = find_view3d()
    # サイドバーを開いて Edit Layers タブをアクティブに
    space = area.spaces.active
    space.show_region_ui = True
    ok = False
    for r in area.regions:
        if r.type == "UI":
            if hasattr(r, "active_panel_category"):
                try:
                    r.active_panel_category = "Edit Layers"
                    ok = True
                except Exception:
                    traceback.print_exc()
    log(f"category set: {ok}")
    if not ok:
        # フォールバック: サイドバーの Python パネルを全て退かして、うちのパネルだけにする
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

        removed, failed = 0, []
        for cls in all_side_panels():
            try:
                bpy.utils.unregister_class(cls)
                removed += 1
            except Exception:
                failed.append(cls.__name__)
        bpy.utils.unregister_class(edit_layers.EL_PT_panel)
        edit_layers.EL_PT_panel.bl_category = "Item"
        edit_layers.EL_PT_panel.bl_order = -1000  # C パネル (トランスフォーム) より上に
        bpy.utils.register_class(edit_layers.EL_PT_panel)
        log(f"fallback: removed {removed} side panels, failed: {failed[:8]}")

    obj = bpy.context.scene.objects.get("Cube")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    ov = obj_override()
    with bpy.context.temp_override(**ov):
        bpy.ops.edit_layers.stack_init()
        # レイヤー1: 上面を持ち上げ
        bpy.ops.edit_layers.record_new(mode="EDIT")
    edit_top_verts(dz=1.0)
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.commit()
        # レイヤー2: 上面を押し出し
        bpy.ops.edit_layers.record_new(mode="EDIT")
    obj = bpy.data.objects["Cube"]
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

    # レイヤー1 からブランチを作り、面削除レイヤーを積む
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

    # Main に戻す
    stack.active_branch = 0
    stack.active_index = 0

    # ビュー調整: パースの 3/4 視点 (押し出しと正面の穴が見える角度)
    import math
    from mathutils import Euler

    win, area = find_view3d()
    rv3d = area.spaces.active.region_3d
    rv3d.view_perspective = "PERSP"
    rv3d.view_rotation = Euler(
        (math.radians(70), 0.0, math.radians(-30)), "XYZ"
    ).to_quaternion()
    for o in bpy.data.objects:
        o.select_set(False)
    with bpy.context.temp_override(window=win, area=area, region=win_region(area)):
        bpy.ops.view3d.view_all(center=False)
    log("setup done")


def s_shot_layers():
    shot("panel_layers.png")


def s_compare():
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.compare()
    win, area = find_view3d()
    with bpy.context.temp_override(window=win, area=area, region=win_region(area)):
        bpy.ops.view3d.view_all(center=False)
    log("compare done")


def s_shot_compare():
    for o in bpy.data.objects:
        o.select_set(False)
    shot("viewport_compare.png")
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.compare_clear()
        bpy.ops.view3d.view_all(center=False)


def s_influence():
    obj = bpy.data.objects["Cube"]
    stack = obj.edit_layers
    stack.show_influence = True
    stack.active_index = 1  # Extrude Top (生成=緑) を選択
    find_view3d()[1].tag_redraw()
    log("influence on")


def s_shot_influence():
    shot("viewport_influence.png")
    obj = bpy.data.objects["Cube"]
    obj.edit_layers.active_index = 0  # Base Lift (移動=橙)
    find_view3d()[1].tag_redraw()


def s_shot_influence2():
    shot("viewport_influence_moved.png")
    obj = bpy.data.objects["Cube"]
    obj.edit_layers.show_influence = False
    obj.edit_layers.active_index = 0
    find_view3d()[1].tag_redraw()


def s_record():
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.record_new(mode="EDIT")
    log("recording")


def s_shot_recording():
    shot("panel_recording.png")
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.cancel()


def s_dirty():
    obj = bpy.data.objects["Cube"]
    for v in obj.data.vertices:
        if v.co.z > 1.2:
            v.co.x += 0.4
    obj.data.update()
    find_view3d()[1].tag_redraw()
    log("dirty edit made")


def s_shot_rescue():
    shot("panel_rescue.png")
    with bpy.context.temp_override(**obj_override()):
        bpy.ops.edit_layers.rebuild()


def s_quit():
    log("DONE " + ",".join(os.listdir(OUT_DIR)))
    bpy.ops.wm.quit_blender()


STEPS = [
    s_dismiss_splash,
    s_setup,
    s_shot_layers,
    s_compare,
    s_shot_compare,
    s_influence,
    s_shot_influence,
    s_shot_influence2,
    s_record,
    s_shot_recording,
    s_dirty,
    s_shot_rescue,
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
    # 万一の固まり対策: 40 秒で強制終了
    import os as _os

    print("[capture] failsafe exit")
    _os._exit(0)


bpy.app.timers.register(tick, first_interval=1.5)
bpy.app.timers.register(failsafe, first_interval=40.0)
