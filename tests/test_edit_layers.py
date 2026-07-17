"""edit_layers アドオンのヘッドレステスト (v0.2 ブランチ対応)

blender --background --factory-startup --python tests/test_edit_layers.py で実行する。
"""
import os
import sys
import traceback

import bpy
import bmesh

# リポジトリのルート = アドオンパッケージ本体 (フォルダ名が "edit_layers" である必要がある)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
assert os.path.basename(ROOT) == "edit_layers", ROOT
if os.path.dirname(ROOT) not in sys.path:
    sys.path.insert(0, os.path.dirname(ROOT))

import edit_layers

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")


def counts(obj):
    return len(obj.data.vertices), len(obj.data.edges), len(obj.data.polygons)


def edit_bmesh(obj):
    return bmesh.from_edit_mesh(obj.data)


def blocked(op, **kw):
    """ERROR レポートで弾かれるオペレータ呼び出しを判定する"""
    try:
        result = op(**kw)
        return "CANCELLED" in result
    except RuntimeError:
        return True


def main():
    edit_layers.register()
    from edit_layers import _last_warnings

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.name = "TestCube"

    # ---------- 初期化 ----------
    bpy.ops.edit_layers.stack_init()
    stack = obj.edit_layers
    check("init: initialized", stack.initialized)
    check("init: main branch", len(stack.branches) == 1)
    check("init: cube counts", counts(obj) == (8, 12, 6), str(counts(obj)))

    # ---------- レイヤー1: 移動のみ ----------
    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(obj)
    for v in [v for v in bm.verts if v.co.z > 0]:
        v.co.z += 1.0
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.edit_layers.commit()
    check("rec1: layer created", len(stack.layers) == 1)
    z_max = max(v.co.z for v in obj.data.vertices)
    check("rec1: verts moved", abs(z_max - 2.0) < 1e-5, f"z_max={z_max}")

    # ---------- レイヤー2: トポロジ変更 (上面を押し出し) ----------
    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(obj)
    bm.faces.ensure_lookup_table()
    top_face = max(bm.faces, key=lambda f: f.calc_center_median().z)
    ret = bmesh.ops.extrude_face_region(bm, geom=[top_face])
    for v in [g for g in ret["geom"] if isinstance(g, bmesh.types.BMVert)]:
        v.co.z += 1.0
    if top_face.is_valid:
        bmesh.ops.delete(bm, geom=[top_face], context="FACES_ONLY")
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.edit_layers.commit()
    check("rec2: extruded counts", counts(obj) == (12, 20, 10), str(counts(obj)))

    # ---------- 表示切替 ----------
    stack.layers[1].enabled = False
    check("toggle: layer2 off", counts(obj) == (8, 12, 6), str(counts(obj)))
    stack.layers[1].enabled = True
    check("toggle: layer2 on", counts(obj) == (12, 20, 10), str(counts(obj)))
    stack.layers[0].enabled = False
    check("toggle: layer1 off counts", counts(obj) == (12, 20, 10), str(counts(obj)))
    check("toggle: no warnings", not _last_warnings.get(obj.name), str(_last_warnings.get(obj.name)))
    # v0.8: 新規頂点はアンカー相対なので、上流 (持ち上げ) を無効化すると押し出しも下がる
    z_max = max(v.co.z for v in obj.data.vertices)
    check("anchor: extrude follows upstream off", abs(z_max - 2.0) < 1e-4, f"z_max={z_max}")
    stack.layers[0].enabled = True
    z_max = max(v.co.z for v in obj.data.vertices)
    check("anchor: extrude follows upstream on", abs(z_max - 3.0) < 1e-4, f"z_max={z_max}")

    # ---------- レイヤー1 を再編集 → 下流 (押し出し) が追従 ----------
    stack.active_index = 0
    bpy.ops.edit_layers.record_edit()
    bm = edit_bmesh(obj)
    for v in [v for v in bm.verts if v.co.z > 1.5]:
        v.co.x += 0.5
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.edit_layers.commit()
    check("reedit: layer count unchanged", len(stack.layers) == 2)
    check("reedit: counts stable", counts(obj) == (12, 20, 10), str(counts(obj)))
    check("reedit: no broken refs", not _last_warnings.get(obj.name), str(_last_warnings.get(obj.name)))
    x_max = max(v.co.x for v in obj.data.vertices)
    check("reedit: move applied", abs(x_max - 1.5) < 1e-4, f"x_max={x_max}")
    n_right = sum(1 for v in obj.data.vertices if v.co.x > 1.49)
    check("anchor: caps follow reedit", n_right == 4, f"n_right={n_right}")

    # ---------- レイヤー3: subdivide (ID 重複の正規化) ----------
    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(obj)
    bm.edges.ensure_lookup_table()
    bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=1, use_grid_fill=True)
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.edit_layers.commit()
    ids = [a.value for a in obj.data.attributes["el_id"].data]
    check("subdiv: ids unique", len(ids) == len(set(ids)))
    check("subdiv: no zero ids", 0 not in ids)
    check("subdiv: no warnings", not _last_warnings.get(obj.name), str(_last_warnings.get(obj.name)))

    # ---------- レイヤー4: 面削除 ----------
    c3 = counts(obj)
    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(obj)
    bm.faces.ensure_lookup_table()
    bottom = min(bm.faces, key=lambda f: f.calc_center_median().z)
    bmesh.ops.delete(bm, geom=[bottom], context="FACES_ONLY")
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.edit_layers.commit()
    c4 = counts(obj)
    check("delface: one face less", c4[2] == c3[2] - 1, f"{c3} -> {c4}")

    # ---------- 並べ替え ----------
    stack.active_index = 2
    bpy.ops.edit_layers.layer_move(direction="DOWN")
    check("reorder: rebuild survives", counts(obj)[0] > 0)
    # 並べ替えは parent 参照の付け替えなので、フラットなコレクション上の
    # インデックスは変わらない (subdiv は index 2 のまま)
    bpy.ops.edit_layers.layer_move(direction="UP")
    check("reorder: back to original", counts(obj) == c4, f"{counts(obj)} != {c4}")
    check("reorder: no warnings", not _last_warnings.get(obj.name), str(_last_warnings.get(obj.name)))

    # ========== ブランチ ==========

    # ---------- レイヤー1 (移動レイヤー) からブランチを作る ----------
    stack.active_index = 0
    bpy.ops.edit_layers.branch_create()
    check("branch: created", len(stack.branches) == 2)
    check("branch: switched", stack.active_branch == 1)
    check(
        "branch: colors distinct",
        tuple(stack.branches[0].color) != tuple(stack.branches[1].color),
        f"{tuple(stack.branches[0].color)} vs {tuple(stack.branches[1].color)}",
    )
    # branch2 のパスはレイヤー1のみ → 移動済みキューブ
    check("branch: path applied", counts(obj) == (8, 12, 6), str(counts(obj)))
    z_max = max(v.co.z for v in obj.data.vertices)
    check("branch: layer1 applied", abs(z_max - 2.0) < 1e-5, f"z_max={z_max}")

    # ---------- branch2 に専用レイヤーを記録 (面削除で分岐) ----------
    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(obj)
    bm.faces.ensure_lookup_table()
    front = min(bm.faces, key=lambda f: f.calc_center_median().y)
    bmesh.ops.delete(bm, geom=[front], context="FACES_ONLY")
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.edit_layers.commit()
    cb2 = counts(obj)
    check("branch: exclusive layer", cb2 == (8, 12, 5), str(cb2))
    check("branch: total layers", len(stack.layers) == 5)

    # ---------- ブランチ切り替え ----------
    stack.active_branch = 0
    check("switch: main restored", counts(obj) == c4, f"{counts(obj)} != {c4}")
    stack.active_branch = 1
    check("switch: branch2 restored", counts(obj) == cb2, str(counts(obj)))

    # ---------- 比較 ----------
    stack.active_branch = 0
    bpy.ops.edit_layers.compare()
    dups = [o for o in bpy.data.objects if o.get("el_compare_of") == obj.name]
    check("compare: one dup", len(dups) == 1, str(len(dups)))
    if dups:
        check("compare: dup counts", counts(dups[0]) == cb2, str(counts(dups[0])))
        check("compare: dup offset", dups[0].location.x > obj.location.x)
        check("compare: dup has no stack", not dups[0].edit_layers.initialized)
    bpy.ops.edit_layers.compare_clear()
    dups = [o for o in bpy.data.objects if o.get("el_compare_of") == obj.name]
    check("compare: cleared", len(dups) == 0)

    # 比較複製をユーザーが複製した場合、クリアで消えてはいけない
    bpy.ops.edit_layers.compare()
    dup = next(o for o in bpy.data.objects if o.get("el_compare_of") == obj.name)
    user_copy = dup.copy()  # マーカーごとコピーされる
    user_copy.data = dup.data.copy()
    user_copy.name = "MyKeeper"
    bpy.context.collection.objects.link(user_copy)
    bpy.ops.edit_layers.compare_clear()
    check("compare: user copy survives", bpy.data.objects.get("MyKeeper") is not None)
    keeper = bpy.data.objects.get("MyKeeper")
    check(
        "compare: user copy marker stripped",
        keeper is not None and keeper.get("el_compare_of") is None,
    )
    check(
        "compare: own dups still cleared",
        not [o for o in bpy.data.objects if o.get("el_compare_of") == obj.name],
    )
    bpy.data.objects.remove(keeper)

    # ファイル再読込相当 (セッション記憶が消えた状態) では、マーカー付きでも削除しない
    from edit_layers import _compare_names
    bpy.ops.edit_layers.compare()
    dup2 = next(o for o in bpy.data.objects if o.get("el_compare_of") == obj.name)
    dup2_name = dup2.name
    _compare_names.clear()  # load_post 相当
    bpy.ops.edit_layers.compare_clear()
    survivor = bpy.data.objects.get(dup2_name)
    check("compare: stale dup kept after reload", survivor is not None)
    if survivor is not None:
        check("compare: stale dup marker stripped", survivor.get("el_compare_of") is None)
        bpy.data.objects.remove(survivor)

    # ---------- 共有レイヤーの再編集が全ブランチに波及 ----------
    stack.active_index = 0  # レイヤー1 (共有)
    bpy.ops.edit_layers.record_edit()
    bm = edit_bmesh(obj)
    for v in [v for v in bm.verts if v.co.z > 1.5]:
        v.co.y += 0.7
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.edit_layers.commit()
    check("shared: main no warnings", not _last_warnings.get(obj.name), str(_last_warnings.get(obj.name)))
    stack.active_branch = 1
    y_max = max(v.co.y for v in obj.data.vertices)
    check("shared: propagated to branch2", abs(y_max - 1.7) < 1e-5, f"y_max={y_max}")
    check("shared: branch2 no warnings", not _last_warnings.get(obj.name), str(_last_warnings.get(obj.name)))
    stack.active_branch = 0

    # ---------- 共有レイヤーの移動・削除はブロックされる ----------
    stack.active_index = 0
    check("guard: shared move blocked", blocked(bpy.ops.edit_layers.layer_move, direction="DOWN"))
    check("guard: shared remove blocked", blocked(bpy.ops.edit_layers.layer_remove))

    # ---------- ブランチ削除 (専用レイヤーごと消える) ----------
    stack.active_branch = 1
    bpy.ops.edit_layers.branch_remove()
    check("brdel: one branch left", len(stack.branches) == 1)
    check("brdel: exclusive layer removed", len(stack.layers) == 4, str(len(stack.layers)))
    check("brdel: main restored", counts(obj) == c4, f"{counts(obj)} != {c4}")

    # ========== 未記録編集の救済 ==========

    from edit_layers import _is_dirty

    # 記録を通さずに編集モードで直接モデリングしてしまう
    bpy.ops.object.mode_set(mode="EDIT")
    bm = edit_bmesh(obj)
    for v in [v for v in bm.verts if v.co.z > 2.5]:
        v.co.z += 0.5  # 押し出しキャップをさらに持ち上げ
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")

    check("rescue: dirty detected", _is_dirty(obj))
    check("rescue: record_new blocked", blocked(bpy.ops.edit_layers.record_new))
    check("rescue: reedit blocked", blocked(bpy.ops.edit_layers.record_edit))
    check("rescue: bake blocked", blocked(bpy.ops.edit_layers.bake))

    # dirty 中の表示切替は保留され、メッシュは壊れない
    stack.layers[3].enabled = False
    check("rescue: toggle deferred", counts(obj) == c4, str(counts(obj)))
    stack.layers[3].enabled = True

    # 取り込み → 遡ってレイヤー化される
    bpy.ops.edit_layers.adopt()
    check("rescue: layer adopted", len(stack.layers) == 5, str(len(stack.layers)))
    check("rescue: clean after adopt", not _is_dirty(obj))
    check("rescue: counts stable", counts(obj) == c4, str(counts(obj)))
    z_max = max(v.co.z for v in obj.data.vertices)
    check("rescue: edit preserved", abs(z_max - 3.5) < 1e-5, f"z_max={z_max}")
    check("rescue: no warnings", not _last_warnings.get(obj.name), str(_last_warnings.get(obj.name)))

    # 取り込んだレイヤーも普通のレイヤーとして無効化できる
    stack.layers[4].enabled = False
    z_max = max(v.co.z for v in obj.data.vertices)
    check("rescue: adopted layer toggles", abs(z_max - 3.0) < 1e-5, f"z_max={z_max}")
    stack.layers[4].enabled = True

    # 対称な編集 (絶対値和が相殺するケース) も検出できる
    for v in obj.data.vertices:
        v.co.x += 0.4  # ±x の頂点が対で動き、|x| の総和は変わらない
    obj.data.update()
    check("rescue: symmetric edit detected", _is_dirty(obj))
    bpy.ops.edit_layers.rebuild()  # 破棄
    check("rescue: discarded", not _is_dirty(obj))

    # ========== スカルプトモードでの記録 ==========

    icons = set(
        bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items.keys()
    )
    check("sculpt: icon exists", "SCULPTMODE_HLT" in icons)
    for ic in ("DOWNARROW_HLT", "TRIA_UP_BAR", "OVERLAY"):
        check(f"icon exists: {ic}", ic in icons)

    c5 = counts(obj)
    bpy.ops.edit_layers.record_new(mode="SCULPT")
    check("sculpt: in sculpt mode", obj.mode == "SCULPT")
    # ブラシストロークの代わりに頂点を直接動かす (スカルプトは位置変更のみ)
    for v in obj.data.vertices:
        if v.co.z > 3.0:
            v.co.x += 0.3
            v.co.y -= 0.2
    obj.data.update()
    bpy.ops.edit_layers.commit()
    check("sculpt: back to object mode", obj.mode == "OBJECT")
    check("sculpt: layer created", len(stack.layers) == 6, str(len(stack.layers)))
    check("sculpt: counts unchanged", counts(obj) == c5, str(counts(obj)))
    check("sculpt: no warnings", not _last_warnings.get(obj.name), str(_last_warnings.get(obj.name)))
    # v0.8: キャップはアンカー追従で共有編集の y+0.7 に付いてくるため、
    # キャップ自体の y を検証する (元 -1 + 追従 0.7 + スカルプト -0.2 = -0.5)
    cap_y = min(v.co.y for v in obj.data.vertices if v.co.z > 3.2)
    check("sculpt: stroke recorded", abs(cap_y + 0.5) < 1e-4, f"cap_y={cap_y}")
    # スカルプトレイヤーも無効化できる
    stack.layers[5].enabled = False
    cap_y = min(v.co.y for v in obj.data.vertices if v.co.z > 3.2)
    check("sculpt: layer toggles", abs(cap_y + 0.3) < 1e-4, f"cap_y={cap_y}")
    stack.layers[5].enabled = True

    # ---------- 記録の破棄 / 空コミット / ベイク ----------
    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(obj)
    for v in bm.verts:
        v.co *= 2.0
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.edit_layers.cancel()
    check("cancel: state restored", counts(obj) == c4)
    check("cancel: not recording", not stack.is_recording)

    bpy.ops.edit_layers.record_new()
    bpy.ops.edit_layers.commit()
    check("empty commit: no layer added", len(stack.layers) == 6)

    bpy.ops.edit_layers.bake()
    check("bake: stack removed", not stack.initialized)
    check("bake: attr removed", obj.data.attributes.get("el_id") is None)
    check("bake: mesh keeps result", counts(obj) == c4)

    # ========== シェイプキー保護 ==========

    # 1) キーがあるメッシュでは初期化できない
    bpy.ops.mesh.primitive_cube_add()
    sk_obj = bpy.context.object
    sk_obj.name = "SKGuard"
    sk_obj.shape_key_add(name="Basis")
    check("skguard: init blocked", blocked(bpy.ops.edit_layers.stack_init))
    sk_obj.shape_key_clear()
    bpy.ops.edit_layers.stack_init()
    stack2 = sk_obj.edit_layers
    check("skguard: init ok after clear", stack2.initialized)

    # 2) スタック運用中にキーを追加してしまった場合
    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(sk_obj)
    for v in bm.verts:
        if v.co.z > 0:
            v.co.z += 1.0
    bmesh.update_edit_mesh(sk_obj.data)
    bpy.ops.edit_layers.commit()

    # 2a) セッション中のキー追加はロックされ、自動で取り消される
    from edit_layers import _no_key_confirmed, _blocked_notice

    check("lock: object confirmed", sk_obj.name in _no_key_confirmed)
    sk_obj.shape_key_add(name="Basis")
    sk_obj.shape_key_add(name="Wide")
    bpy.context.view_layer.update()  # depsgraph ハンドラを発火させる
    check("lock: keys auto-removed", sk_obj.data.shape_keys is None)
    check("lock: notice shown", _blocked_notice.get(sk_obj.name) is True)
    bpy.ops.edit_layers.notice_clear()
    check("lock: notice cleared", sk_obj.name not in _blocked_notice)
    # ロック後もスタックは普通に使える
    bpy.ops.edit_layers.record_new()
    bpy.ops.edit_layers.commit()  # 空コミット
    check("lock: stack still works", not stack2.is_recording)

    # 2b) レガシー (キーと共存した状態で開いたファイル) はガード + 警告モード
    _no_key_confirmed.discard(sk_obj.name)
    sk_obj.shape_key_add(name="Basis")
    kb = sk_obj.shape_key_add(name="Wide")
    for d in kb.data:
        d.co.x *= 2.0
    bpy.context.view_layer.update()
    check("legacy: keys survive", sk_obj.data.shape_keys is not None)

    # 再構築系の操作はブロックされる
    check("skguard: record blocked", blocked(bpy.ops.edit_layers.record_new))
    check("skguard: rebuild blocked", blocked(bpy.ops.edit_layers.rebuild))
    check("skguard: bake blocked", blocked(bpy.ops.edit_layers.bake))

    # 表示切替は保留され、キーのデータは壊れない
    z_before = max(v.co.z for v in sk_obj.data.vertices)
    stack2.layers[0].enabled = False
    z_after = max(v.co.z for v in sk_obj.data.vertices)
    check("skguard: toggle deferred", abs(z_before - z_after) < 1e-6)
    wide_x = kb.data[0].co.x
    check("skguard: key intact", abs(abs(wide_x) - 2.0) < 1e-6, f"wide_x={wide_x}")
    stack2.layers[0].enabled = True

    # detach でキーを保持したままスタックを外せる
    bpy.ops.edit_layers.detach()
    check("skguard: detached", not stack2.initialized)
    sk = sk_obj.data.shape_keys
    check(
        "skguard: keys survive detach",
        sk is not None and len(sk.key_blocks) == 2
        and abs(abs(sk.key_blocks["Wide"].data[0].co.x) - 2.0) < 1e-6,
    )
    z_max = max(v.co.z for v in sk_obj.data.vertices)
    check("skguard: mesh survives detach", abs(z_max - 2.0) < 1e-6, f"z_max={z_max}")
    check("skguard: attr removed", sk_obj.data.attributes.get("el_id") is None)


def extra_tests():
    from edit_layers import _last_warnings, _influence_local

    # ========== 属性の記録 (v0.8) ==========
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.name = "AttrCube"
    bpy.ops.edit_layers.stack_init()
    stack = obj.edit_layers

    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(obj)
    # レイヤー追加は要素参照を無効化するので、参照を取る前に作る
    ce = bm.edges.layers.float.get("crease_edge") or bm.edges.layers.float.new("crease_edge")
    bm.faces.ensure_lookup_table()
    top = max(bm.faces, key=lambda f: f.calc_center_median().z)
    top.smooth = True
    top.material_index = 2
    e0 = top.edges[0]
    e0.seam = True
    e0.smooth = False  # シャープ
    e0[ce] = 0.8
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.edit_layers.commit()

    def top_face():
        return max(obj.data.polygons, key=lambda p: p.center.z)

    f = top_face()
    check("attr: smooth recorded", f.use_smooth)
    check("attr: material recorded", f.material_index == 2, str(f.material_index))
    seams = sum(1 for e in obj.data.edges if e.use_seam)
    check("attr: seam recorded", seams == 1, str(seams))
    ca = obj.data.attributes.get("crease_edge")
    max_crease = max((d.value for d in ca.data), default=0.0) if ca else 0.0
    check("attr: crease recorded", abs(max_crease - 0.8) < 1e-4, str(max_crease))

    # レイヤーを無効化すると属性も元に戻る
    stack.layers[0].enabled = False
    f = top_face()
    check("attr: smooth reverts", not f.use_smooth)
    check("attr: material reverts", f.material_index == 0)
    stack.layers[0].enabled = True
    f = top_face()
    check("attr: smooth reapplied", f.use_smooth)
    check("attr: no warnings", not _last_warnings.get(obj.name), str(_last_warnings.get(obj.name)))

    # ========== 影響ハイライトのデータ ==========
    stack.active_index = 0
    check("influence: off -> None", _influence_local(obj) is None)
    stack.show_influence = True
    res = _influence_local(obj)
    check("influence: attrs-only layer empty", res == ([], []), str(res))

    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(obj)
    for v in bm.verts:
        if v.co.z > 0:
            v.co.z += 0.5
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.edit_layers.commit()
    stack.active_index = 1
    res = _influence_local(obj)
    check("influence: moved count", res is not None and len(res[0]) == 4, str(res and len(res[0])))
    check("influence: new count", res is not None and len(res[1]) == 0)
    stack.show_influence = False

    # ========== マージ / 部分ベイク (v0.8) ==========
    bpy.ops.mesh.primitive_cube_add()
    obj2 = bpy.context.object
    obj2.name = "MergeCube"
    bpy.ops.edit_layers.stack_init()
    st2 = obj2.edit_layers

    for dz, dx in ((1.0, 0.0), (0.0, 0.5)):
        bpy.ops.edit_layers.record_new()
        bm = edit_bmesh(obj2)
        for v in bm.verts:
            if v.co.z > 0:
                v.co.z += dz
                v.co.x += dx
        bmesh.update_edit_mesh(obj2.data)
        bpy.ops.edit_layers.commit()

    before = [tuple(v.co) for v in obj2.data.vertices]
    st2.active_index = 1
    bpy.ops.edit_layers.layer_merge_down()
    check("merge: one layer left", len(st2.layers) == 1, str(len(st2.layers)))
    after = [tuple(v.co) for v in obj2.data.vertices]
    same = len(before) == len(after) and all(
        abs(a[k] - b[k]) < 1e-4 for a, b in zip(before, after) for k in range(3)
    )
    check("merge: result unchanged", same)
    check("merge: no warnings", not _last_warnings.get(obj2.name), str(_last_warnings.get(obj2.name)))

    # 押し出しレイヤーを積む
    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(obj2)
    bm.faces.ensure_lookup_table()
    top = max(bm.faces, key=lambda f: f.calc_center_median().z)
    ret = bmesh.ops.extrude_face_region(bm, geom=[top])
    for g in ret["geom"]:
        if isinstance(g, bmesh.types.BMVert):
            g.co.z += 0.7
    if top.is_valid:
        bmesh.ops.delete(bm, geom=[top], context="FACES_ONLY")
    bmesh.update_edit_mesh(obj2.data)
    bpy.ops.edit_layers.commit()

    # ブランチ専用レイヤーを選んで部分ベイク -> ブロックされる
    st2.active_index = 0
    bpy.ops.edit_layers.branch_create()  # 統合レイヤーから分岐
    bpy.ops.edit_layers.record_new()
    bm = edit_bmesh(obj2)
    for v in bm.verts:
        v.co.y *= 1.2
    bmesh.update_edit_mesh(obj2.data)
    bpy.ops.edit_layers.commit()
    check("bakeupto: exclusive blocked", blocked(bpy.ops.edit_layers.bake_upto))

    # ブランチを消してから、統合レイヤーまでをベースに確定
    bpy.ops.edit_layers.branch_remove()
    before = [tuple(v.co) for v in obj2.data.vertices]
    n_before = counts(obj2)
    st2.active_index = 0  # 統合レイヤー
    bpy.ops.edit_layers.bake_upto()
    check("bakeupto: one layer left", len(st2.layers) == 1, str(len(st2.layers)))
    check("bakeupto: counts stable", counts(obj2) == n_before, f"{counts(obj2)} != {n_before}")
    after = [tuple(v.co) for v in obj2.data.vertices]
    same = len(before) == len(after) and all(
        abs(a[k] - b[k]) < 1e-4 for a, b in zip(before, after) for k in range(3)
    )
    check("bakeupto: result unchanged", same)
    # 残った押し出しレイヤーがまだ切り替え可能
    st2.layers[0].enabled = False
    check("bakeupto: remaining layer toggles", counts(obj2)[0] == 8, str(counts(obj2)))
    st2.layers[0].enabled = True
    check("bakeupto: no warnings", not _last_warnings.get(obj2.name), str(_last_warnings.get(obj2.name)))


try:
    main()
    extra_tests()
except Exception:
    traceback.print_exc()
    FAIL.append("EXCEPTION")

print(f"\n=== RESULT: {len(PASS)} passed, {len(FAIL)} failed ===")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
