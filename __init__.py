"""
Edit Layers Add-on for Blender 5.x
3ds Max の Edit Poly のような、レイヤーベースの非破壊メッシュ編集スタック

仕組み:
- 全頂点に永続 ID (INT 属性 "el_id") を付与し、レイヤーは頂点インデックスではなく
  この ID を参照する。上流レイヤーの変更でインデックスがずれても参照が壊れない。
- レイヤー = 編集セッション前後のスナップショットを ID 基準で比較したトポロジ差分。
  移動 (delta) / 頂点追加・削除 / エッジ追加・削除 / 面追加・削除 を JSON で保持する。
- レイヤーはツリー構造 (parent 参照)。ブランチは末端レイヤー (head) へのポインタで、
  アクティブブランチ = head から根まで遡ったパス。分岐点より上流のレイヤーは
  全ブランチで共有されるため、上流の再編集は全ブランチに波及する。
- 再構築はベースメッシュのコピーにアクティブパスの差分を bmesh で順に適用する。
  参照先 ID が存在しない場合は警告してスキップする。
"""

bl_info = {
    "name": "Edit Layers",
    "author": "Hayashihikaru",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Edit Layers",
    "description": "Layer-based non-destructive mesh editing stack with branches",
    "category": "Mesh",
}

import json
import os

import bpy
import bmesh
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from mathutils import Vector

# 永続頂点 ID を保存する属性名 (0 = 未割り当て)
ID_ATTR = "el_id"
# 比較用オブジェクトに付けるマーカー
COMPARE_PROP = "el_compare_of"
# 移動とみなす最小距離
EPS = 1e-6

# 記録中の編集前スナップショット (オブジェクト名 -> {"pre": snapshot, "uid": int})
_recording = {}
# 直近の再構築で出た警告 (オブジェクト名 -> list[str])
_last_warnings = {}
# 直近の再構築の状態 (オブジェクト名 -> {"fp": 指紋, "uids": 適用レイヤー, "branch": int})
# 未記録編集の検出と救済 (取り込み) に使う
_last_state = {}
# セッション中に「シェイプキーなし」を確認済みのスタックオブジェクト名。
# このセットに入っているオブジェクトにキーが現れたら「追加された」と断定して
# ロック (自動削除) を発動できる。入っていない場合 (v0.4 以前のファイル等) は
# 既存データを消さないよう、ガード + 警告モードに留める。
_no_key_confirmed = set()
# シェイプキー追加をブロックした通知 (オブジェクト名 -> True)
_blocked_notice = {}
# このセッションで比較機能が作った複製の名前。マーカー (COMPARE_PROP) は
# オブジェクト複製でコピーされてしまうため、削除して良いものはここで区別する
_compare_names = set()
# 再構築のたびに増えるカウンタ (影響ハイライトのキャッシュ無効化用)
_rebuild_serial = [0]
# 影響ハイライトのキャッシュ {"key": tuple, "data": (moved座標, 生成座標)}
_influence_cache = {}

# ブランチに自動で割り当てる色 (作成順に巡回)
_BRANCH_PALETTE = (
    (0.85, 0.35, 0.35),  # 赤
    (0.35, 0.65, 0.95),  # 青
    (0.45, 0.80, 0.45),  # 緑
    (0.95, 0.75, 0.35),  # 橙
    (0.75, 0.50, 0.90),  # 紫
    (0.40, 0.85, 0.80),  # 青緑
    (0.95, 0.55, 0.75),  # 桃
    (0.70, 0.70, 0.70),  # 灰
)


def _assign_branch_color(stack, branch):
    branch.color = _BRANCH_PALETTE[(len(stack.branches) - 1) % len(_BRANCH_PALETTE)]


# ==================== 翻訳 ====================

# UI 翻訳 (レポートや動的文字列は自動翻訳されないため、このヘルパーを通す)
_T = bpy.app.translations.pgettext_iface

# 日本語訳 (ソースは英語。Blender の言語設定が日本語なら自動で切り替わる)
_JA = {
    # オペレータ名
    "Initialize Stack": "スタックを初期化",
    "Record New Layer": "新規レイヤーを記録",
    "Re-edit Selected Layer": "選択レイヤーを再編集",
    "Commit": "コミット",
    "Discard Recording": "記録を破棄",
    "Adopt Unrecorded Edits": "未記録の編集を取り込む",
    "Remove Layer": "レイヤーを削除",
    "Move Layer": "レイヤーを移動",
    "Branch From Here": "ここからブランチ",
    "Remove Branch": "ブランチを削除",
    "Compare Branches Side by Side": "ブランチを並べて比較",
    "Clear Comparison": "比較を解除",
    "Rebuild": "再構築",
    "Discard Stack (Keep Current Mesh)": "スタックを破棄 (現状を確定)",
    "Bake and Remove Stack": "ベイクしてスタックを削除",
    # 説明 (ツールチップ)
    "Create a layer stack on this object": "このオブジェクトにレイヤースタックを作成する",
    "Start recording a new layer at the tip of the active branch": "新規レイヤーの記録を開始する (アクティブブランチの先端に追加)",
    "Re-edit the selected layer (edits to a shared layer affect all branches)": "選択中のレイヤーを再編集する (共有レイヤーの変更は全ブランチに波及する)",
    "Save the edits into the layer as a diff": "編集内容を差分としてレイヤーに保存する",
    "Discard the recording and restore the previous state": "記録を破棄して元の状態に戻す",
    "Adopt edits made without recording as a new layer (rescue)": "記録せずに行った編集を、遡って新規レイヤーとして取り込む (救済用)",
    "Remove the selected layer (layers shared with other branches cannot be removed)": "選択中のレイヤーを削除する (他ブランチと共有しているレイヤーは削除できない)",
    "Move the selected layer up/down within the branch": "選択中のレイヤーをブランチ内で上下に移動する",
    "Create a new branch that diverges at the selected layer": "選択中のレイヤーを分岐点として新しいブランチを作る",
    "Remove the active branch (its exclusive layers are removed too)": "アクティブなブランチを削除する (このブランチ専用のレイヤーも削除される)",
    "Duplicate other branches side by side for comparison": "他のブランチの結果を横に並べて比較する",
    "Delete the comparison duplicates": "比較用の複製オブジェクトを削除する",
    "Dismiss the blocked shape key notice": "シェイプキー追加をブロックした通知を閉じる",
    "Rebuild the stack": "スタックを再構築する",
    "Discard the stack without rebuilding, keeping the current mesh (preserves shape keys)": "再構築せずに現在のメッシュのままスタックを破棄する (シェイプキー等を保持)",
    "Apply the active branch result and remove the stack": "アクティブブランチの結果を確定してスタックを破棄する",
    # プロパティ
    "Name": "名前",
    "Enabled": "有効",
    "Color": "色",
    "Mode": "モード",
    "Edit Mode": "編集モード",
    "Sculpt Mode": "スカルプトモード",
    "Start recording in Edit Mode": "編集モードで記録を開始する",
    "Start recording in Sculpt Mode": "スカルプトモードで記録を開始する",
    "Up": "上へ",
    "Down": "下へ",
    # レポート
    "Stack initialized ({count} vertices)": "スタックを初期化しました (頂点 {count})",
    "Meshes with shape keys are not supported. Apply/remove the keys first (create shape keys after modeling and baking)": "シェイプキーのあるメッシュは非対応です。先にキーを適用/削除してください (シェイプキーはモデリング完了後・ベイク後の作成を推奨)",
    'Recording started: press "Commit" when done': "記録開始: 編集後に「コミット」してください",
    "Selected layer is not on the current branch": "選択レイヤーは現在のブランチ上にありません",
    'Re-editing layer "{name}" (shared: affects all branches)': "レイヤー「{name}」を再編集中 (共有: 全ブランチに波及)",
    'Re-editing layer "{name}"': "レイヤー「{name}」を再編集中",
    "Recording data not found; recording aborted": "記録データが見つかりません。記録を中止しました",
    "No changes; no layer was created": "変更がないためレイヤーは作成しませんでした",
    "Layer being re-edited no longer exists": "再編集対象のレイヤーが見つかりません",
    'Committed layer "{name}" (rebuild skipped to protect shape keys)': "レイヤー「{name}」をコミットしました (シェイプキー保護のため再構築はスキップ)",
    "Committed ({count} warnings in downstream layers; see panel)": "コミットしました (下流レイヤーで警告 {count} 件。パネル参照)",
    'Committed layer "{name}"': "レイヤー「{name}」をコミットしました",
    "Recording discarded (rebuild skipped to protect shape keys; edits remain in the mesh)": "記録を破棄しました (シェイプキー保護のため再構築はスキップ。編集はメッシュに残っています)",
    "Recording discarded": "記録を破棄しました",
    "No edits to adopt": "取り込む編集はありませんでした",
    "(adopted)": "(取り込み)",
    'Adopted as layer "{name}"': "レイヤー「{name}」として取り込みました",
    " (approximated from current setup; no rebuild history)": " (再構築履歴がないため現在の構成から近似)",
    " ({count} warnings)": " (警告 {count} 件)",
    'There are unrecorded edits. Use "Adopt Unrecorded Edits" or "Rebuild" (discard) first': "未記録の編集があります。「未記録の編集を取り込む」か「再構築」(破棄) をしてください",
    'Shape keys detected. The operation was blocked because rebuilding would destroy them. Apply/remove the keys or use "Discard Stack (Keep Current Mesh)"': "シェイプキーが検出されました。再構築はキーを破壊するため操作をブロックしました。キーを適用/削除するか「スタックを破棄 (現状を確定)」してください",
    "Shared layers cannot be removed (remove the branch first)": "共有レイヤーは削除できません (先にブランチを削除)",
    "Removed ({count} warnings in downstream layers)": "削除しました (下流レイヤーで警告 {count} 件)",
    "Cannot move across a shared layer": "共有レイヤーをまたぐ移動はできません",
    "Moved ({count} warnings; see panel)": "移動しました (警告 {count} 件。パネル参照)",
    'Created branch "{name}". New recordings will diverge from here': "ブランチ「{name}」を作成しました。ここから記録すると分岐します",
    'Removed branch "{name}" ({count} exclusive layers)': "ブランチ「{name}」を削除しました (専用レイヤー {count} 件)",
    "Duplicated {count} branches for comparison": "比較用に {count} ブランチを複製しました",
    "Removed {count} comparison objects": "比較用オブジェクトを {count} 件削除しました",
    " ({count} kept as regular objects)": " ({count} 件は通常オブジェクトとして残しました)",
    "Rebuilt ({count} warnings)": "再構築しました (警告 {count} 件)",
    "Rebuilt": "再構築しました",
    "Stack discarded; current mesh kept": "現在のメッシュを保持したままスタックを破棄しました",
    "Baked and removed the stack": "ベイクしてスタックを削除しました",
    # 再構築の警告
    "{layer}: missing vertices for edge ({a}, {b})": "{layer}: エッジの参照頂点が欠落 ({a}, {b})",
    "{layer}: missing vertices for face {ids}": "{layer}: 面の参照頂点が欠落 {ids}",
    "{layer}: cannot create face {ids}": "{layer}: 面を作成できません {ids}",
    "{layer}: missing vertex {i} to move": "{layer}: 移動対象の頂点 {i} が欠落",
    # パネル / リスト
    "← {count} branches": "← {count} 分岐",
    "shared {shared} + own {own}": "共有{shared} + 専用{own}",
    "{count} layers": "{count} 層",
    "Blocked adding a shape key": "シェイプキーの追加をブロックしました",
    "Create shape keys after baking/discarding the stack": "キーはベイク/破棄の後に作成してください",
    "Shape keys detected": "シェイプキーが検出されました",
    "Rebuilds are paused to protect the keys": "キーを守るため再構築を停止しています",
    "Apply/remove the keys, or:": "続けるにはキーを適用/削除するか:",
    "Re-editing: {name}": "再編集中: {name}",
    "Recording a new layer": "新規レイヤーを記録中",
    "Discard": "破棄",
    "Entered Edit Mode without recording": "記録せずに編集モードに入っています",
    "Entered Sculpt Mode without recording": "記録せずにスカルプトモードに入っています",
    "These edits will not be kept in a layer": "この編集はレイヤーに残りません",
    "Adopt Edits as a Layer": "編集をレイヤーとして取り込む",
    "Record (Edit)": "記録 (編集)",
    "Record (Sculpt)": "記録 (スカルプト)",
    "Unrecorded edits detected": "未記録の編集があります",
    "Adopt as a Layer": "レイヤーとして取り込む",
    "Discard and Rebuild": "破棄して再構築",
    "Branches ({count})": "ブランチ ({count})",
    "Compare": "並べて比較",
    "Clear": "解除",
    "Layers — {name}": "レイヤー — {name}",
    "Bake": "ベイク",
    "Unrecorded edits are detected and can be adopted": "記録外の編集は検出され「取り込み」で救済できます",
    "{count} warnings:": "警告 {count} 件:",
    # v0.8: マージ / 部分ベイク / 影響ハイライト
    "Merge Into Previous": "直前のレイヤーと統合",
    "Merge the selected layer into the previous (upper) layer": "選択中のレイヤーを直前 (1 つ上) のレイヤーに統合する",
    "Bake Up To Here": "ここまでをベースに確定",
    "Apply layers up to the selected one into the base mesh and remove them": "選択レイヤーまでをベースメッシュに焼き込んで取り除く",
    "Layer Operations": "レイヤー操作",
    "There is no previous layer to merge into": "統合先の直前のレイヤーがありません",
    "Shared layers cannot be merged (remove the branch first)": "共有レイヤーは統合できません (先にブランチを削除)",
    "Disabled layers cannot be merged/baked (enable or remove them first)": "無効なレイヤーが含まれています (有効化するか削除してください)",
    'Merged into "{name}"': "「{name}」に統合しました",
    "All branches must pass through the selected layer": "全てのブランチが選択レイヤーを通っている必要があります",
    "Baked {count} layers into the base mesh": "{count} 層をベースメッシュに確定しました",
    "Show Influence": "影響を表示",
    "Highlight vertices affected by the selected layer (orange: moved, green: created)": "選択レイヤーが影響した頂点をハイライト表示 (橙: 移動 / 緑: 生成)",
    "... and {count} more": "... ほか {count} 件",
}


def _translations_dict():
    d = {}
    for en, ja in _JA.items():
        d[("*", en)] = ja
        d[("Operator", en)] = ja
    return {"ja_JP": d}


# ==================== コア: ID / スナップショット / 差分 ====================


def _ensure_id_layer(bm):
    """bmesh に永続 ID レイヤーを確保して返す"""
    idl = bm.verts.layers.int.get(ID_ATTR)
    if idl is None:
        idl = bm.verts.layers.int.new(ID_ATTR)
    return idl


# 記録する属性のデフォルト値 (これと同値なら差分に含めない)
_FACE_ATTR_DEFAULT = (0, False)  # (material_index, smooth)
_EDGE_ATTR_DEFAULT = (False, True, 0.0, 0.0)  # (seam, smooth, crease, bevel_weight)
_VERT_ATTR_DEFAULT = (0.0, 0.0)  # (crease, bevel_weight)


def _take_snapshot(bm, idl):
    """現在の bmesh の状態を ID 基準で記録する

    verts: {id: (x, y, z)}
    faces: {frozenset(ids): [ids (ループ順)]}
    edges: {frozenset((a, b))}
    face_attrs / edge_attrs / vert_attrs: 要素キー -> 属性タプル
    """
    ce = bm.edges.layers.float.get("crease_edge")
    be = bm.edges.layers.float.get("bevel_weight_edge")
    cv = bm.verts.layers.float.get("crease_vert")
    bv = bm.verts.layers.float.get("bevel_weight_vert")

    verts = {}
    vert_attrs = {}
    for v in bm.verts:
        i = v[idl]
        if i == 0:
            continue
        verts[i] = (v.co.x, v.co.y, v.co.z)
        crease = round(v[cv], 5) if cv else 0.0
        bevel = round(v[bv], 5) if bv else 0.0
        if (crease, bevel) != _VERT_ATTR_DEFAULT:
            vert_attrs[i] = (crease, bevel)

    faces = {}
    face_attrs = {}
    for f in bm.faces:
        ids = [v[idl] for v in f.verts]
        if 0 in ids:
            continue
        key = frozenset(ids)
        faces[key] = ids
        attrs = (f.material_index, f.smooth)
        if attrs != _FACE_ATTR_DEFAULT:
            face_attrs[key] = attrs

    edges = set()
    edge_attrs = {}
    for e in bm.edges:
        a, b = e.verts[0][idl], e.verts[1][idl]
        if a == 0 or b == 0:
            continue
        key = frozenset((a, b))
        edges.add(key)
        attrs = (
            e.seam,
            e.smooth,
            round(e[ce], 5) if ce else 0.0,
            round(e[be], 5) if be else 0.0,
        )
        if attrs != _EDGE_ATTR_DEFAULT:
            edge_attrs[key] = attrs

    return {
        "verts": verts,
        "faces": faces,
        "edges": edges,
        "face_attrs": face_attrs,
        "edge_attrs": edge_attrs,
        "vert_attrs": vert_attrs,
    }


def _resolve_ids(bm, idl, pre_verts, next_id):
    """編集後の bmesh の ID を正規化する

    - 重複 ID (subdivide 等で属性が複製されたもの) は、編集前の位置に最も近い
      1 頂点だけが ID を保持し、残りは新規扱いにする。
    - ID 0 (新規頂点) には新しい ID を振る。
    戻り値: 更新後の next_id
    """
    by_id = {}
    for v in bm.verts:
        by_id.setdefault(v[idl], []).append(v)

    for i, vs in by_id.items():
        if i == 0 or len(vs) == 1:
            continue
        if i in pre_verts:
            pre_co = Vector(pre_verts[i])
            keep = min(vs, key=lambda v: (v.co - pre_co).length_squared)
        else:
            keep = vs[0]
        for v in vs:
            if v is not keep:
                v[idl] = 0

    # 他オブジェクトからのペースト等で next_id より大きい ID が混入しても衝突しないようにする
    max_id = max((v[idl] for v in bm.verts), default=0)
    next_id = max(next_id, max_id + 1)

    for v in bm.verts:
        if v[idl] == 0:
            v[idl] = next_id
            next_id += 1
    return next_id


def _face_edge_pairs(ids):
    """面の頂点 ID リストから、その面が張るエッジの集合を返す"""
    n = len(ids)
    return {frozenset((ids[j], ids[(j + 1) % n])) for j in range(n)}


def _compute_diff(pre, post):
    """編集前後のスナップショットから、レイヤーとして保存する差分を作る"""
    moved = {}
    for i, co in post["verts"].items():
        p = pre["verts"].get(i)
        if p is None:
            continue
        d = (co[0] - p[0], co[1] - p[1], co[2] - p[2])
        if abs(d[0]) > EPS or abs(d[1]) > EPS or abs(d[2]) > EPS:
            moved[i] = d

    new_verts = {i: co for i, co in post["verts"].items() if i not in pre["verts"]}
    deleted_verts = [i for i in pre["verts"] if i not in post["verts"]]
    dead = set(deleted_verts)

    new_faces = [ids for key, ids in post["faces"].items() if key not in pre["faces"]]

    # 新規面を作れば付随するエッジも生成されるので、面に含まれないエッジだけ記録する
    covered = set()
    for ids in new_faces:
        covered |= _face_edge_pairs(ids)
    new_edges = [sorted(p) for p in (post["edges"] - pre["edges"]) if p not in covered]

    # 頂点削除で連鎖的に消えるエッジは記録しない
    deleted_edge_set = {
        p for p in (pre["edges"] - post["edges"]) if not (p & dead)
    }
    deleted_edges = [sorted(p) for p in deleted_edge_set]

    # 頂点削除・エッジ削除で連鎖的に消える面は記録しない
    deleted_faces = []
    for key, ids in pre["faces"].items():
        if key in post["faces"] or (key & dead):
            continue
        if _face_edge_pairs(ids) & deleted_edge_set:
            continue
        deleted_faces.append(sorted(key))

    # 属性の差分 (デフォルト値はスナップショット側で省かれている)
    face_attrs = []
    for key in set(pre["face_attrs"]) | set(post["face_attrs"]):
        if key not in post["faces"]:
            continue
        a = post["face_attrs"].get(key, _FACE_ATTR_DEFAULT)
        if key in pre["faces"]:
            if pre["face_attrs"].get(key, _FACE_ATTR_DEFAULT) == a:
                continue
        elif a == _FACE_ATTR_DEFAULT:
            continue
        face_attrs.append([post["faces"][key], a[0], a[1]])

    edge_attrs = []
    for key in set(pre["edge_attrs"]) | set(post["edge_attrs"]):
        if key not in post["edges"]:
            continue
        a = post["edge_attrs"].get(key, _EDGE_ATTR_DEFAULT)
        if key in pre["edges"]:
            if pre["edge_attrs"].get(key, _EDGE_ATTR_DEFAULT) == a:
                continue
        elif a == _EDGE_ATTR_DEFAULT:
            continue
        edge_attrs.append([sorted(key), *a])

    vert_attrs = []
    for i in set(pre["vert_attrs"]) | set(post["vert_attrs"]):
        if i not in post["verts"]:
            continue
        a = post["vert_attrs"].get(i, _VERT_ATTR_DEFAULT)
        if i in pre["verts"]:
            if pre["vert_attrs"].get(i, _VERT_ATTR_DEFAULT) == a:
                continue
        elif a == _VERT_ATTR_DEFAULT:
            continue
        vert_attrs.append([i, *a])

    return {
        "moved": moved,
        "new_verts": new_verts,
        "deleted_verts": deleted_verts,
        "new_edges": new_edges,
        "deleted_edges": deleted_edges,
        "new_faces": new_faces,
        "deleted_faces": deleted_faces,
        "anchors": _compute_anchors(pre, post, new_verts),
        "face_attrs": face_attrs,
        "edge_attrs": edge_attrs,
        "vert_attrs": vert_attrs,
    }


def _compute_anchors(pre, post, new_verts):
    """新規頂点をアンカー相対で表す

    新規頂点ごとに、編集前から生き残っている頂点のうち最も近い 3 点を
    アンカーとして選び、その重心からのオフセットを保存する。再生時に
    アンカーの現在位置から位置を復元することで、上流レイヤーの変形に
    新規ジオメトリが追従する (アンカーが失われた場合は絶対座標に戻る)。
    """
    if not new_verts:
        return {}
    survivors = [
        (i, co) for i, co in post["verts"].items() if i in pre["verts"]
    ]
    if not survivors:
        return {}
    import mathutils

    kd = mathutils.kdtree.KDTree(len(survivors))
    for j, (_i, co) in enumerate(survivors):
        kd.insert(co, j)
    kd.balance()

    anchors = {}
    for i, co in new_verts.items():
        hits = kd.find_n(co, 3)
        if not hits:
            continue
        ids = [survivors[h[1]][0] for h in hits]
        cx = Vector((0.0, 0.0, 0.0))
        for h in hits:
            cx += Vector(h[0])
        cx /= len(hits)
        off = Vector(co) - cx
        anchors[i] = [ids, [off.x, off.y, off.z]]
    return anchors


def _diff_is_empty(diff):
    return not any(diff.values())


def _find_face(vmap, idl, ids):
    """頂点 ID の集合から既存の面を探す"""
    v0 = vmap.get(ids[0])
    if v0 is None or not v0.is_valid:
        return None
    target = set(ids)
    for f in v0.link_faces:
        if len(f.verts) == len(ids) and {v[idl] for v in f.verts} == target:
            return f
    return None


def _apply_layer(bm, idl, data, warnings, layer_name):
    """1 レイヤー分の差分を bmesh に適用する

    削除系で対象が見つからない場合は黙ってスキップする (上流の変更で既に
    消えているだけなので実害がない)。生成・移動系で参照頂点が見つからない
    場合は警告を残してスキップする。
    """
    # カスタムデータレイヤーの追加はその領域の要素参照・レイヤーハンドルを
    # 無効化するため、要素参照を取る前に必要なレイヤーを全て確保しておく
    if any(row[3] for row in data.get("edge_attrs", [])):
        if bm.edges.layers.float.get("crease_edge") is None:
            bm.edges.layers.float.new("crease_edge")
    if any(row[4] for row in data.get("edge_attrs", [])):
        if bm.edges.layers.float.get("bevel_weight_edge") is None:
            bm.edges.layers.float.new("bevel_weight_edge")
    if any(row[1] for row in data.get("vert_attrs", [])):
        if bm.verts.layers.float.get("crease_vert") is None:
            bm.verts.layers.float.new("crease_vert")
    if any(row[2] for row in data.get("vert_attrs", [])):
        if bm.verts.layers.float.get("bevel_weight_vert") is None:
            bm.verts.layers.float.new("bevel_weight_vert")
    idl = _ensure_id_layer(bm)  # 頂点レイヤー追加でハンドルが失効するため取り直す

    vmap = {v[idl]: v for v in bm.verts if v[idl] != 0}

    # 1. 頂点削除 (付随するエッジ・面も連鎖削除される)
    doomed = [vmap.pop(i) for i in data.get("deleted_verts", []) if i in vmap]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context="VERTS")

    # 2. エッジ削除 (付随する面は連鎖削除、頂点は残す)
    # 'EDGES' は孤立した頂点まで削除してしまい、後続レイヤーの参照が壊れるため
    # 'EDGES_FACES' を使う。頂点の削除は deleted_verts で明示的に行われる。
    edges = []
    for a, b in data.get("deleted_edges", []):
        va, vb = vmap.get(a), vmap.get(b)
        if va and vb and va.is_valid and vb.is_valid:
            e = bm.edges.get((va, vb))
            if e:
                edges.append(e)
    if edges:
        bmesh.ops.delete(bm, geom=edges, context="EDGES_FACES")

    # 3. 面削除 (面のみ。頂点・エッジは残す)
    faces = []
    for ids in data.get("deleted_faces", []):
        f = _find_face(vmap, idl, ids)
        if f:
            faces.append(f)
    if faces:
        bmesh.ops.delete(bm, geom=faces, context="FACES_ONLY")

    # 4. 頂点移動 (delta)
    # アンカー相対の新規頂点が「移動後のアンカー位置」を参照できるよう、
    # 生成より先に適用する (差分の計算も移動後の位置を基準にしている)
    for i, d in data.get("moved", {}).items():
        v = vmap.get(int(i))
        if v is None or not v.is_valid:
            warnings.append(_T("{layer}: missing vertex {i} to move").format(layer=layer_name, i=i))
            continue
        v.co += Vector(d)

    # 5. 新規頂点 (JSON のキーは文字列になっているので int に戻す)
    # アンカー情報があれば「アンカー重心 + オフセット」で位置を復元し、
    # 上流レイヤーの変形に追従させる。なければ絶対座標 (旧形式) で生成する。
    anchors = data.get("anchors", {})
    for i_str, co in data.get("new_verts", {}).items():
        i = int(i_str)
        pos = None
        a = anchors.get(i_str) or anchors.get(i)
        if a:
            ids, off = a
            pts = [vmap[j].co for j in ids if j in vmap and vmap[j].is_valid]
            if pts:
                c = Vector((0.0, 0.0, 0.0))
                for p in pts:
                    c += p
                c /= len(pts)
                pos = c + Vector(off)
        if pos is None:
            pos = co
        v = bm.verts.new(pos)
        v[idl] = i
        vmap[i] = v

    # 6. 新規エッジ (ワイヤーエッジなど、面に付随しないもの)
    for a, b in data.get("new_edges", []):
        va, vb = vmap.get(a), vmap.get(b)
        if not (va and vb and va.is_valid and vb.is_valid):
            warnings.append(_T("{layer}: missing vertices for edge ({a}, {b})").format(layer=layer_name, a=a, b=b))
            continue
        if bm.edges.get((va, vb)) is None:
            bm.edges.new((va, vb))

    # 7. 新規面
    for ids in data.get("new_faces", []):
        vs = [vmap.get(i) for i in ids]
        if any(v is None or not v.is_valid for v in vs):
            warnings.append(_T("{layer}: missing vertices for face {ids}").format(layer=layer_name, ids=ids))
            continue
        if bm.faces.get(vs):
            continue
        try:
            bm.faces.new(vs)
        except ValueError:
            warnings.append(_T("{layer}: cannot create face {ids}").format(layer=layer_name, ids=ids))

    # 8. 属性 (マテリアル / スムーズ / シーム / シャープ / クリース / ベベルウェイト)
    # 対象が見つからない場合は上流の変更で消えているだけなので黙ってスキップする
    for ids, mat, smooth in data.get("face_attrs", []):
        f = _find_face(vmap, idl, ids)
        if f:
            f.material_index = mat
            f.smooth = smooth

    ce = bm.edges.layers.float.get("crease_edge")
    be = bm.edges.layers.float.get("bevel_weight_edge")
    for pair, seam, smooth, crease, bevel in data.get("edge_attrs", []):
        a, b = pair
        va, vb = vmap.get(a), vmap.get(b)
        if not (va and vb and va.is_valid and vb.is_valid):
            continue
        e = bm.edges.get((va, vb))
        if e is None:
            continue
        e.seam = seam
        e.smooth = smooth
        if ce is not None:
            e[ce] = crease
        if be is not None:
            e[be] = bevel

    cv = bm.verts.layers.float.get("crease_vert")
    bv = bm.verts.layers.float.get("bevel_weight_vert")
    for i, crease, bevel in data.get("vert_attrs", []):
        v = vmap.get(int(i))
        if v is None or not v.is_valid:
            continue
        if cv is not None:
            v[cv] = crease
        if bv is not None:
            v[bv] = bevel


# ==================== コア: ブランチ / 再構築 ====================


def _ensure_branches(stack):
    """v0.1 の線形スタック (uid 未割り当て) をツリー構造に移行する"""
    if stack.branches:
        return
    prev = 0
    uid = 1
    for layer in stack.layers:
        layer.uid = uid
        layer.parent = prev
        prev = uid
        uid += 1
    stack.next_uid = uid
    br = stack.branches.add()
    br.name = "Main"
    br.head_uid = prev
    _assign_branch_color(stack, br)
    stack.active_branch = 0


def _branch_path(stack, branch_index=None):
    """ブランチの head から根まで遡り、根→head 順のレイヤーリストを返す"""
    if not stack.branches:
        # 移行前 (v0.1 データ) は登録順をそのまま使う
        return list(stack.layers)
    if branch_index is None:
        branch_index = stack.active_branch
    branch_index = max(0, min(branch_index, len(stack.branches) - 1))
    by_uid = {l.uid: l for l in stack.layers}
    path = []
    uid = stack.branches[branch_index].head_uid
    seen = set()
    while uid and uid in by_uid and uid not in seen:
        seen.add(uid)
        layer = by_uid[uid]
        path.append(layer)
        uid = layer.parent
    path.reverse()
    return path


def _layer_branches(stack, uid):
    """このレイヤーを通るブランチ index のリスト"""
    return [
        bi
        for bi in range(len(stack.branches))
        if any(l.uid == uid for l in _branch_path(stack, bi))
    ]


def _layer_branch_count(stack, uid):
    """このレイヤーを通るブランチの数 (2 以上なら共有レイヤー)"""
    return len(_layer_branches(stack, uid))


def _divergence_map(stack):
    """アクティブパス上の uid -> そこを分岐点とする他ブランチ index のリスト

    他ブランチのパスとアクティブパスの共有部分は (ツリーなので) 必ず根から
    連続するため、共有が途切れる直前のレイヤーが分岐点になる。
    """
    active_set = {l.uid for l in _branch_path(stack)}
    div = {}
    for bi in range(len(stack.branches)):
        if bi == stack.active_branch:
            continue
        last = 0
        for l in _branch_path(stack, bi):
            if l.uid in active_set:
                last = l.uid
            else:
                break
        if last:
            div.setdefault(last, []).append(bi)
    return div


def _branch_layer_stats(stack, branch_index):
    """(共有レイヤー数, このブランチ専用のレイヤー数) を返す"""
    mine = {l.uid for l in _branch_path(stack, branch_index)}
    others = set()
    for j in range(len(stack.branches)):
        if j != branch_index:
            others |= {l.uid for l in _branch_path(stack, j)}
    own = len(mine - others)
    return len(mine) - own, own


def _rebuild_mesh(stack, path, mesh, respect_enabled=True, upto=None):
    """ベースメッシュから path のレイヤーを順に適用して mesh に書き込む

    戻り値: (警告リスト, 実際に適用したレイヤー uid のリスト)
    """
    warnings = []
    applied = []
    bm = bmesh.new()
    try:
        bm.from_mesh(stack.base_mesh)
        _ensure_id_layer(bm)
        for pos, layer in enumerate(path):
            if upto is not None and pos >= upto:
                break
            if respect_enabled and not layer.enabled:
                continue
            if not layer.data:
                continue
            # 属性適用で頂点レイヤーが追加されると既存のレイヤーハンドルが
            # 無効になるため、ID レイヤーは毎回取り直す
            idl = _ensure_id_layer(bm)
            _apply_layer(bm, idl, json.loads(layer.data), warnings, layer.name)
            applied.append(layer.uid)
        bm.normal_update()
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    return warnings, applied


def _fingerprint(mesh):
    """未記録編集の検出用にメッシュの指紋を取る

    要素数に加えて座標の絶対値和と位置依存の重み付き和を使う。単純な絶対値和
    だけだと対称な編集 (例: ±x の頂点を同時に +0.4) が相殺して検出漏れするため、
    要素ごとに異なる重みを掛けた和も併用する。numpy でメッシュサイズに対して高速。
    """
    import numpy as np

    n = len(mesh.vertices)
    buf = np.empty(n * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", buf)
    a = np.abs(buf)
    w = np.arange(1, a.size + 1, dtype=np.float64) % 97.0 + 1.0
    return (
        n,
        len(mesh.edges),
        len(mesh.polygons),
        round(float(a.sum()), 4),
        round(float(np.dot(a, w)), 2),
    )


def _rebuild(obj, upto=None, respect_enabled=True, branch_index=None):
    """アクティブブランチ (または指定ブランチ) でオブジェクトを再構築する"""
    stack = obj.edit_layers
    path = _branch_path(stack, branch_index)
    warnings, applied = _rebuild_mesh(stack, path, obj.data, respect_enabled, upto)
    _rebuild_serial[0] += 1  # 影響ハイライトのキャッシュを無効化する
    _last_warnings[obj.name] = warnings
    _last_state[obj.name] = {
        "fp": _fingerprint(obj.data),
        "uids": applied,
        "branch": stack.active_branch if branch_index is None else branch_index,
    }
    return warnings


def _safe_rebuild(obj):
    """シェイプキーがある場合は再構築せず、状態追跡だけリセットする

    再構築 (bmesh -> mesh) はシェイプキーを Basis で上書きしてしまうため、
    キーが検出されたらメッシュには触らない。戻り値 None がスキップを表す。
    """
    if _has_shape_keys(obj):
        _last_state.pop(obj.name, None)
        return None
    return _rebuild(obj)


def _is_dirty(obj):
    """最後の再構築以降に未記録の編集が入っているか

    編集モード中はメッシュデータが未確定なので判定しない。再構築履歴がない
    (ファイルを開き直した直後など) 場合も判定できないので False を返す。
    """
    if obj.mode != "OBJECT":
        return False
    st = _last_state.get(obj.name)
    if st is None:
        return False
    return _fingerprint(obj.data) != st["fp"]


def _active_layer(stack):
    if 0 <= stack.active_index < len(stack.layers):
        return stack.layers[stack.active_index]
    return None


# ==================== 影響ハイライト ====================


def _influence_local(obj):
    """アクティブレイヤーが影響した頂点のローカル座標を返す

    戻り値: (moved 座標リスト, 生成座標リスト) / 表示条件を満たさない場合 None。
    描画コールバックから毎フレーム呼ばれるため、レイヤーと再構築が変わらない
    限りキャッシュを返す。
    """
    stack = obj.edit_layers
    if not (
        stack.initialized
        and stack.show_influence
        and not stack.is_recording
        and obj.mode == "OBJECT"
    ):
        return None
    layer = _active_layer(stack)
    if layer is None or not layer.data:
        return None

    key = (
        obj.name,
        layer.uid,
        len(layer.data),
        len(obj.data.vertices),
        _rebuild_serial[0],
    )
    if _influence_cache.get("key") == key:
        return _influence_cache["data"]

    import numpy as np

    result = ([], [])
    attr = obj.data.attributes.get(ID_ATTR)
    if attr is not None:
        data = json.loads(layer.data)
        moved_ids = [int(k) for k in data.get("moved", {})]
        new_ids = [int(k) for k in data.get("new_verts", {})]
        if moved_ids or new_ids:
            n = len(obj.data.vertices)
            ids = np.empty(n, dtype=np.int32)
            attr.data.foreach_get("value", ids)
            cos = np.empty(n * 3, dtype=np.float32)
            obj.data.vertices.foreach_get("co", cos)
            cos = cos.reshape(n, 3)
            moved = cos[np.isin(ids, moved_ids)].tolist() if moved_ids else []
            new = cos[np.isin(ids, new_ids)].tolist() if new_ids else []
            result = (moved, new)

    _influence_cache["key"] = key
    _influence_cache["data"] = result
    return result


def _draw_influence():
    """選択レイヤーの影響頂点をオーバーレイ描画する (橙: 移動 / 緑: 生成)"""
    obj = bpy.context.object
    if obj is None or obj.type != "MESH":
        return
    try:
        result = _influence_local(obj)
    except Exception:
        return
    if not result:
        return
    moved, new = result
    if not moved and not new:
        return

    import gpu
    from gpu_extras.batch import batch_for_shader

    # ポイント描画は専用シェーダを使う (Vulkan バックエンドでは固定機能の
    # ポイントサイズが効かないため)。無い環境では UNIFORM_COLOR に落とす。
    try:
        shader = gpu.shader.from_builtin("POINT_UNIFORM_COLOR")
        is_point_shader = True
    except Exception:
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        is_point_shader = False

    gpu.state.depth_test_set("NONE")
    gpu.state.program_point_size_set(False)
    gpu.state.point_size_set(10.0)
    gpu.matrix.push()
    try:
        gpu.matrix.multiply_matrix(obj.matrix_world)
        for coords, color in (
            (moved, (1.0, 0.65, 0.1, 1.0)),
            (new, (0.3, 1.0, 0.4, 1.0)),
        ):
            if not coords:
                continue
            batch = batch_for_shader(shader, "POINTS", {"pos": coords})
            shader.bind()
            shader.uniform_float("color", color)
            if is_point_shader:
                try:
                    shader.uniform_float("size", 10.0)
                except Exception:
                    pass
            batch.draw(shader)
    finally:
        gpu.matrix.pop()
        gpu.state.point_size_set(1.0)
        gpu.state.depth_test_set("LESS_EQUAL")


_draw_handle = None


# ==================== シェイプキー追加のロック ====================


def _rescan_no_keys():
    """キーなしで運用中のスタックオブジェクトを確認済みセットに登録する"""
    for obj in bpy.data.objects:
        if (
            obj.type == "MESH"
            and obj.edit_layers.initialized
            and obj.data.shape_keys is None
        ):
            _no_key_confirmed.add(obj.name)


@persistent
def _el_depsgraph_handler(scene, depsgraph):
    """スタック運用中のオブジェクトへのシェイプキー追加を検知して取り消す"""
    for upd in depsgraph.updates:
        obj = getattr(upd.id, "original", upd.id)
        if not isinstance(obj, bpy.types.Object) or obj.type != "MESH":
            continue
        stack = obj.edit_layers
        if not stack.initialized:
            continue
        if obj.data.shape_keys is None:
            # キーなしを確認 → 以後キーが現れたら「追加された」と断定できる
            _no_key_confirmed.add(obj.name)
            continue
        if obj.name in _no_key_confirmed and obj.mode == "OBJECT":
            # セッション中に追加されたキー → ロック発動 (追加直後なので失うデータはない)
            obj.shape_key_clear()
            _blocked_notice[obj.name] = True
            print(
                f"[Edit Layers] {obj.name}: blocked adding shape keys while a "
                "stack is active (create them after baking/discarding)"
            )
        # 確認済みでない場合 (キーと共存した状態で保存された古いファイル等) は
        # データを消さず、ガード + パネル警告に任せる


@persistent
def _el_load_post(_dummy):
    """ファイル読み込みでセッション状態をリセットする"""
    _recording.clear()
    _compare_names.clear()
    _last_warnings.clear()
    _last_state.clear()
    _no_key_confirmed.clear()
    _blocked_notice.clear()
    _rescan_no_keys()


# ==================== プロパティ ====================


def _tag_redraw_view3d(context):
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _on_enabled_update(self, context):
    obj = context.object
    if obj and obj.type == "MESH":
        stack = obj.edit_layers
        # 未記録の編集がある間は再構築を保留する (編集を上書きしないため)。
        # シェイプキーがある間も保留する (再構築がキーを破壊するため)。
        if (
            stack.initialized
            and not stack.is_recording
            and not _is_dirty(obj)
            and not _has_shape_keys(obj)
        ):
            _rebuild(obj)


def _on_branch_switch(self, context):
    obj = context.object
    if obj and obj.type == "MESH":
        stack = obj.edit_layers
        if (
            stack.initialized
            and not stack.is_recording
            and stack.branches
            and not _is_dirty(obj)
            and not _has_shape_keys(obj)
        ):
            _rebuild(obj)


class EL_Layer(bpy.types.PropertyGroup):
    name: StringProperty(name="Name", default="Layer")
    enabled: BoolProperty(name="Enabled", default=True, update=_on_enabled_update)
    # レイヤーの永続 UID (頂点 ID とは別系統。0 = 未割り当て)
    uid: IntProperty(default=0)
    # 親レイヤーの UID (0 = ベースメッシュ直下)
    parent: IntProperty(default=0)
    # 差分 JSON
    data: StringProperty(default="")


class EL_Branch(bpy.types.PropertyGroup):
    name: StringProperty(name="Name", default="Branch")
    # このブランチの末端レイヤーの UID (0 = ベースメッシュのみ)
    head_uid: IntProperty(default=0)
    # 識別色 (リストのチップと分岐バッジに表示。クリックで変更可)
    color: FloatVectorProperty(
        name="Color",
        subtype="COLOR",
        size=3,
        min=0.0,
        max=1.0,
        default=(0.7, 0.7, 0.7),
    )


class EL_Stack(bpy.types.PropertyGroup):
    initialized: BoolProperty(default=False)
    layers: CollectionProperty(type=EL_Layer)
    active_index: IntProperty(default=0)
    branches: CollectionProperty(type=EL_Branch)
    active_branch: IntProperty(default=0, update=_on_branch_switch)
    # 次に割り当てる頂点 ID (0 は未割り当てを意味するので 1 から)
    next_id: IntProperty(default=1)
    # 次に割り当てるレイヤー UID
    next_uid: IntProperty(default=1)
    is_recording: BoolProperty(default=False)
    # 記録中のレイヤー UID (0 = 新規レイヤー)
    recording_uid: IntProperty(default=0)
    base_mesh: PointerProperty(type=bpy.types.Mesh)
    show_influence: BoolProperty(
        name="Show Influence",
        description=(
            "Highlight vertices affected by the selected layer "
            "(orange: moved, green: created)"
        ),
        default=False,
        update=lambda self, context: _tag_redraw_view3d(context),
    )


# ==================== オペレータ ====================


def _poll_mesh_object(context):
    obj = context.object
    return obj is not None and obj.type == "MESH"


def _poll_stack_idle(context):
    """初期化済みで記録中でないこと"""
    if not _poll_mesh_object(context):
        return False
    stack = context.object.edit_layers
    return stack.initialized and not stack.is_recording


def _guard_dirty(op, context):
    """未記録の編集があるとき、再構築でそれを消してしまう操作をブロックする"""
    if _is_dirty(context.object):
        op.report(
            {"ERROR"},
            _T(
                'There are unrecorded edits. Use "Adopt Unrecorded Edits" '
                'or "Rebuild" (discard) first'
            ),
        )
        return True
    return False


def _has_shape_keys(obj):
    return obj is not None and obj.type == "MESH" and obj.data.shape_keys is not None


def _guard_shape_keys(op, context):
    """シェイプキーがあるとき、再構築でキーを破壊する操作をブロックする

    再構築 (bmesh -> mesh) はシェイプキーのデータを Basis で上書きしてしまうため、
    キーが検出されたら破壊的な操作を全て止める。
    """
    if _has_shape_keys(context.object):
        op.report(
            {"ERROR"},
            _T(
                "Shape keys detected. The operation was blocked because rebuilding "
                'would destroy them. Apply/remove the keys or use '
                '"Discard Stack (Keep Current Mesh)"'
            ),
        )
        return True
    return False


class EL_OT_stack_init(bpy.types.Operator):
    """Create a layer stack on this object"""

    bl_idname = "edit_layers.stack_init"
    bl_label = "Initialize Stack"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            _poll_mesh_object(context)
            and not context.object.edit_layers.initialized
            and context.mode == "OBJECT"
        )

    def execute(self, context):
        obj = context.object
        if _has_shape_keys(obj):
            self.report(
                {"ERROR"},
                _T(
                    "Meshes with shape keys are not supported. Apply/remove the keys "
                    "first (create shape keys after modeling and baking)"
                ),
            )
            return {"CANCELLED"}
        mesh = obj.data
        stack = obj.edit_layers

        # 全頂点に永続 ID を割り当てる
        bm = bmesh.new()
        bm.from_mesh(mesh)
        idl = _ensure_id_layer(bm)
        next_id = 1
        for v in bm.verts:
            v[idl] = next_id
            next_id += 1
        bm.to_mesh(mesh)
        bm.free()
        mesh.update()

        # ID 付きの状態をベースメッシュとして退避する
        base = mesh.copy()
        base.name = mesh.name + "_el_base"
        base.use_fake_user = True

        stack.base_mesh = base
        stack.next_id = next_id
        stack.next_uid = 1
        stack.layers.clear()
        stack.branches.clear()
        br = stack.branches.add()
        br.name = "Main"
        br.head_uid = 0
        _assign_branch_color(stack, br)
        stack.active_branch = 0
        stack.active_index = 0
        stack.initialized = True
        _no_key_confirmed.add(obj.name)
        self.report({"INFO"}, _T("Stack initialized ({count} vertices)").format(count=len(mesh.vertices)))
        return {"FINISHED"}


class EL_OT_record_new(bpy.types.Operator):
    """Start recording a new layer at the tip of the active branch"""

    # 記録中は編集/スカルプトなどモードを自由に行き来してよい。
    # コミット時のメッシュ状態との差分が記録される。

    bl_idname = "edit_layers.record_new"
    bl_label = "Record New Layer"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="Mode",
        items=[
            ("EDIT", "Edit Mode", "Start recording in Edit Mode"),
            ("SCULPT", "Sculpt Mode", "Start recording in Sculpt Mode"),
        ],
        default="EDIT",
    )

    @classmethod
    def poll(cls, context):
        return _poll_stack_idle(context) and context.mode == "OBJECT"

    def execute(self, context):
        if _guard_dirty(self, context) or _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        _ensure_branches(stack)

        # 記録は enabled フラグに関係なく全レイヤー適用済みの状態に対して行う
        # (無効レイヤー上に記録すると差分の意味が不定になるため)
        _rebuild(obj, respect_enabled=False)

        bm = bmesh.new()
        bm.from_mesh(obj.data)
        idl = _ensure_id_layer(bm)
        pre = _take_snapshot(bm, idl)
        bm.free()

        _recording[obj.name] = {"pre": pre, "uid": 0}
        stack.is_recording = True
        stack.recording_uid = 0

        bpy.ops.object.mode_set(mode=self.mode)
        self.report({"INFO"}, _T('Recording started: press "Commit" when done'))
        return {"FINISHED"}


class EL_OT_record_edit(bpy.types.Operator):
    """Re-edit the selected layer (edits to a shared layer affect all branches)"""

    bl_idname = "edit_layers.record_edit"
    bl_label = "Re-edit Selected Layer"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not (_poll_stack_idle(context) and context.mode == "OBJECT"):
            return False
        return _active_layer(context.object.edit_layers) is not None

    def execute(self, context):
        if _guard_dirty(self, context) or _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        _ensure_branches(stack)
        layer = _active_layer(stack)
        path = _branch_path(stack)
        pos = next((i for i, l in enumerate(path) if l.uid == layer.uid), None)
        if pos is None:
            self.report({"ERROR"}, _T("Selected layer is not on the current branch"))
            return {"CANCELLED"}

        # 対象レイヤーの直前までを構築してスナップショット
        _rebuild(obj, upto=pos, respect_enabled=False)
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        idl = _ensure_id_layer(bm)
        pre = _take_snapshot(bm, idl)

        # 対象レイヤー自身を適用した状態から編集を始める
        warnings = []
        if layer.data:
            _apply_layer(bm, idl, json.loads(layer.data), warnings, layer.name)
        bm.normal_update()
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()

        _recording[obj.name] = {"pre": pre, "uid": layer.uid}
        stack.is_recording = True
        stack.recording_uid = layer.uid

        shared = _layer_branch_count(stack, layer.uid) > 1
        bpy.ops.object.mode_set(mode="EDIT")
        if shared:
            self.report(
                {"INFO"},
                _T('Re-editing layer "{name}" (shared: affects all branches)').format(
                    name=layer.name
                ),
            )
        else:
            self.report(
                {"INFO"}, _T('Re-editing layer "{name}"').format(name=layer.name)
            )
        return {"FINISHED"}


class EL_OT_commit(bpy.types.Operator):
    """Save the edits into the layer as a diff"""

    bl_idname = "edit_layers.commit"
    bl_label = "Commit"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            _poll_mesh_object(context)
            and context.object.edit_layers.is_recording
        )

    def execute(self, context):
        obj = context.object
        stack = obj.edit_layers
        rec = _recording.get(obj.name)
        if rec is None:
            # Blender 再起動などでスナップショットが失われた場合
            stack.is_recording = False
            self.report({"ERROR"}, _T("Recording data not found; recording aborted"))
            _safe_rebuild(obj)
            return {"CANCELLED"}

        if obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        bm = bmesh.new()
        bm.from_mesh(obj.data)
        idl = _ensure_id_layer(bm)
        stack.next_id = _resolve_ids(bm, idl, rec["pre"]["verts"], stack.next_id)
        post = _take_snapshot(bm, idl)
        bm.free()

        diff = _compute_diff(rec["pre"], post)
        uid = rec["uid"]

        if _diff_is_empty(diff) and uid == 0:
            stack.is_recording = False
            del _recording[obj.name]
            _safe_rebuild(obj)
            self.report({"INFO"}, _T("No changes; no layer was created"))
            return {"CANCELLED"}

        if uid == 0:
            # 新規レイヤーをアクティブブランチの先端に追加する
            br = stack.branches[stack.active_branch]
            layer = stack.layers.add()
            layer.uid = stack.next_uid
            stack.next_uid += 1
            layer.parent = br.head_uid
            br.head_uid = layer.uid
            layer.name = f"Layer {layer.uid}"
            stack.active_index = len(stack.layers) - 1
        else:
            layer = next((l for l in stack.layers if l.uid == uid), None)
            if layer is None:
                stack.is_recording = False
                del _recording[obj.name]
                _safe_rebuild(obj)
                self.report({"ERROR"}, _T("Layer being re-edited no longer exists"))
                return {"CANCELLED"}
        layer.data = json.dumps(diff)

        stack.is_recording = False
        stack.recording_uid = 0
        del _recording[obj.name]

        warnings = _safe_rebuild(obj)
        if warnings is None:
            self.report(
                {"WARNING"},
                _T(
                    'Committed layer "{name}" (rebuild skipped to protect shape keys)'
                ).format(name=layer.name),
            )
        elif warnings:
            self.report(
                {"WARNING"},
                _T("Committed ({count} warnings in downstream layers; see panel)").format(
                    count=len(warnings)
                ),
            )
        else:
            self.report({"INFO"}, _T('Committed layer "{name}"').format(name=layer.name))
        return {"FINISHED"}


class EL_OT_cancel(bpy.types.Operator):
    """Discard the recording and restore the previous state"""

    bl_idname = "edit_layers.cancel"
    bl_label = "Discard Recording"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            _poll_mesh_object(context)
            and context.object.edit_layers.is_recording
        )

    def execute(self, context):
        obj = context.object
        stack = obj.edit_layers
        if obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        _recording.pop(obj.name, None)
        stack.is_recording = False
        stack.recording_uid = 0
        if _safe_rebuild(obj) is None:
            self.report(
                {"WARNING"},
                _T(
                    "Recording discarded (rebuild skipped to protect shape keys; "
                    "edits remain in the mesh)"
                ),
            )
        else:
            self.report({"INFO"}, _T("Recording discarded"))
        return {"FINISHED"}


class EL_OT_adopt(bpy.types.Operator):
    """Adopt edits made without recording as a new layer (rescue)"""

    bl_idname = "edit_layers.adopt"
    bl_label = "Adopt Unrecorded Edits"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _poll_stack_idle(context)

    def execute(self, context):
        if _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        _ensure_branches(stack)
        if obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        # 「編集を始めた時点の状態」= 最後の再構築を再現する。
        # 再構築履歴 (_last_state) があればその時のレイヤー構成・ブランチを使う。
        # 履歴がない (ファイルを開き直した等) 場合は現在の構成で近似する。
        st = _last_state.get(obj.name)
        by_uid = {l.uid: l for l in stack.layers}
        approx = False
        if st is not None:
            pre_layers = [by_uid[u] for u in st["uids"] if u in by_uid]
            approx = len(pre_layers) != len(st["uids"])
            target_branch = max(0, min(st["branch"], len(stack.branches) - 1))
        else:
            pre_layers = [l for l in _branch_path(stack) if l.enabled and l.data]
            target_branch = stack.active_branch
            approx = True

        # 編集前の状態を一時メッシュに再構成してスナップショット
        tmp = bpy.data.meshes.new("_el_adopt_tmp")
        try:
            _rebuild_mesh(stack, pre_layers, tmp, respect_enabled=False)
            bm = bmesh.new()
            bm.from_mesh(tmp)
            idl = _ensure_id_layer(bm)
            pre = _take_snapshot(bm, idl)
            bm.free()
        finally:
            bpy.data.meshes.remove(tmp)

        # 現在のメッシュ (未記録編集入り) を post として差分化
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        idl = _ensure_id_layer(bm)
        stack.next_id = _resolve_ids(bm, idl, pre["verts"], stack.next_id)
        post = _take_snapshot(bm, idl)
        bm.free()

        diff = _compute_diff(pre, post)
        if _diff_is_empty(diff):
            _rebuild(obj)
            self.report({"INFO"}, _T("No edits to adopt"))
            return {"CANCELLED"}

        # 編集を行ったブランチの先端にレイヤーを追加する
        if stack.active_branch != target_branch:
            stack.active_branch = target_branch  # dirty 中なので再構築は走らない
        br = stack.branches[target_branch]
        layer = stack.layers.add()
        layer.uid = stack.next_uid
        stack.next_uid += 1
        layer.parent = br.head_uid
        br.head_uid = layer.uid
        layer.name = f"Layer {layer.uid} " + _T("(adopted)")
        layer.data = json.dumps(diff)
        stack.active_index = len(stack.layers) - 1

        warnings = _rebuild(obj)
        msg = _T('Adopted as layer "{name}"').format(name=layer.name)
        if approx:
            msg += _T(" (approximated from current setup; no rebuild history)")
        if warnings:
            self.report(
                {"WARNING"}, msg + _T(" ({count} warnings)").format(count=len(warnings))
            )
        else:
            self.report({"INFO"}, msg)
        return {"FINISHED"}


class EL_OT_layer_remove(bpy.types.Operator):
    """Remove the selected layer (layers shared with other branches cannot be removed)"""

    bl_idname = "edit_layers.layer_remove"
    bl_label = "Remove Layer"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not _poll_stack_idle(context):
            return False
        return _active_layer(context.object.edit_layers) is not None

    def execute(self, context):
        if _guard_dirty(self, context) or _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        _ensure_branches(stack)
        layer = _active_layer(stack)
        if _layer_branch_count(stack, layer.uid) > 1:
            self.report({"ERROR"}, _T("Shared layers cannot be removed (remove the branch first)"))
            return {"CANCELLED"}

        uid, parent = layer.uid, layer.parent
        # 子レイヤーとブランチ head を親につなぎ替える
        for l in stack.layers:
            if l.parent == uid:
                l.parent = parent
        for br in stack.branches:
            if br.head_uid == uid:
                br.head_uid = parent

        stack.layers.remove(stack.active_index)
        stack.active_index = min(stack.active_index, len(stack.layers) - 1)
        warnings = _rebuild(obj)
        if warnings:
            self.report({"WARNING"}, _T("Removed ({count} warnings in downstream layers)").format(count=len(warnings)))
        return {"FINISHED"}


class EL_OT_layer_move(bpy.types.Operator):
    """Move the selected layer up/down within the branch"""

    bl_idname = "edit_layers.layer_move"
    bl_label = "Move Layer"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(
        items=[("UP", "Up", ""), ("DOWN", "Down", "")],
        default="UP",
    )

    @classmethod
    def poll(cls, context):
        if not _poll_stack_idle(context):
            return False
        return _active_layer(context.object.edit_layers) is not None

    def execute(self, context):
        if _guard_dirty(self, context) or _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        _ensure_branches(stack)
        layer = _active_layer(stack)
        path = _branch_path(stack)
        pos = next((i for i, l in enumerate(path) if l.uid == layer.uid), None)
        if pos is None:
            self.report({"ERROR"}, _T("Selected layer is not on the current branch"))
            return {"CANCELLED"}

        # パス上で入れ替える相手 (parent 側が「上」)
        if self.direction == "UP":
            if pos == 0:
                return {"CANCELLED"}
            upper, lower = path[pos - 1], layer
        else:
            if pos >= len(path) - 1:
                return {"CANCELLED"}
            upper, lower = layer, path[pos + 1]

        # 分岐点をまたぐ入れ替えは他ブランチの意味が変わるため禁止する
        if (
            _layer_branch_count(stack, upper.uid) > 1
            or _layer_branch_count(stack, lower.uid) > 1
        ):
            self.report({"ERROR"}, _T("Cannot move across a shared layer"))
            return {"CANCELLED"}

        # ... P -> upper -> lower -> C ... を ... P -> lower -> upper -> C ... にする
        lower_children = [l for l in stack.layers if l.parent == lower.uid]
        lower.parent, upper.parent = upper.parent, lower.uid
        for c in lower_children:
            c.parent = upper.uid
        for br in stack.branches:
            if br.head_uid == lower.uid:
                br.head_uid = upper.uid

        warnings = _rebuild(obj)
        if warnings:
            self.report({"WARNING"}, _T("Moved ({count} warnings; see panel)").format(count=len(warnings)))
        return {"FINISHED"}


class EL_OT_layer_merge_down(bpy.types.Operator):
    """Merge the selected layer into the previous (upper) layer"""

    bl_idname = "edit_layers.layer_merge_down"
    bl_label = "Merge Into Previous"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not _poll_stack_idle(context):
            return False
        return _active_layer(context.object.edit_layers) is not None

    def execute(self, context):
        if _guard_dirty(self, context) or _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        _ensure_branches(stack)
        layer = _active_layer(stack)
        path = _branch_path(stack)
        pos = next((i for i, l in enumerate(path) if l.uid == layer.uid), None)
        if pos is None:
            self.report({"ERROR"}, _T("Selected layer is not on the current branch"))
            return {"CANCELLED"}
        if pos == 0:
            self.report({"ERROR"}, _T("There is no previous layer to merge into"))
            return {"CANCELLED"}
        parent = path[pos - 1]
        if (
            _layer_branch_count(stack, layer.uid) > 1
            or _layer_branch_count(stack, parent.uid) > 1
        ):
            self.report(
                {"ERROR"},
                _T("Shared layers cannot be merged (remove the branch first)"),
            )
            return {"CANCELLED"}
        if not (layer.enabled and parent.enabled):
            self.report(
                {"ERROR"},
                _T("Disabled layers cannot be merged/baked (enable or remove them first)"),
            )
            return {"CANCELLED"}

        # 統合前 (parent の直前) と統合後の状態を作り、差分を取り直す
        warnings = []
        bm = bmesh.new()
        try:
            bm.from_mesh(stack.base_mesh)
            _ensure_id_layer(bm)
            # 記録系と同じく enabled フラグは無視して決定的に積む
            for l in path[: pos - 1]:
                if l.data:
                    _apply_layer(
                        bm, _ensure_id_layer(bm), json.loads(l.data), warnings, l.name
                    )
            pre = _take_snapshot(bm, _ensure_id_layer(bm))
            for l in (parent, layer):
                if l.data:
                    _apply_layer(
                        bm, _ensure_id_layer(bm), json.loads(l.data), warnings, l.name
                    )
            post = _take_snapshot(bm, _ensure_id_layer(bm))
        finally:
            bm.free()

        parent.data = json.dumps(_compute_diff(pre, post))

        # コレクションから要素を削除すると既存の参照が失効するため、先に控える
        uid, parent_uid, parent_name = layer.uid, parent.uid, parent.name
        for l in stack.layers:
            if l.parent == uid:
                l.parent = parent_uid
        for br in stack.branches:
            if br.head_uid == uid:
                br.head_uid = parent_uid
        idx = next(i for i, l in enumerate(stack.layers) if l.uid == uid)
        stack.layers.remove(idx)
        stack.active_index = next(
            (i for i, l in enumerate(stack.layers) if l.uid == parent_uid), 0
        )
        _rebuild(obj)
        self.report({"INFO"}, _T('Merged into "{name}"').format(name=parent_name))
        return {"FINISHED"}


class EL_OT_bake_upto(bpy.types.Operator):
    """Apply layers up to the selected one into the base mesh and remove them"""

    bl_idname = "edit_layers.bake_upto"
    bl_label = "Bake Up To Here"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not _poll_stack_idle(context):
            return False
        return _active_layer(context.object.edit_layers) is not None

    def execute(self, context):
        if _guard_dirty(self, context) or _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        _ensure_branches(stack)
        layer = _active_layer(stack)
        path = _branch_path(stack)
        pos = next((i for i, l in enumerate(path) if l.uid == layer.uid), None)
        if pos is None:
            self.report({"ERROR"}, _T("Selected layer is not on the current branch"))
            return {"CANCELLED"}
        # 幹以外のブランチが失われないよう、全ブランチが通っている必要がある
        if _layer_branch_count(stack, layer.uid) != len(stack.branches):
            self.report(
                {"ERROR"}, _T("All branches must pass through the selected layer")
            )
            return {"CANCELLED"}
        target = path[: pos + 1]
        if any(not l.enabled for l in target):
            self.report(
                {"ERROR"},
                _T("Disabled layers cannot be merged/baked (enable or remove them first)"),
            )
            return {"CANCELLED"}

        # ベースメッシュに焼き込む (from_mesh で読み込んでから書き戻すので安全)
        _rebuild_mesh(stack, path, stack.base_mesh, respect_enabled=True, upto=pos + 1)

        removed_uids = {l.uid for l in target}
        for l in stack.layers:
            if l.uid not in removed_uids and l.parent in removed_uids:
                l.parent = 0
        for br in stack.branches:
            if br.head_uid in removed_uids:
                br.head_uid = 0
        for uid in removed_uids:
            idx = next(
                (i for i, l in enumerate(stack.layers) if l.uid == uid), None
            )
            if idx is not None:
                stack.layers.remove(idx)
        stack.active_index = max(0, min(stack.active_index, len(stack.layers) - 1))
        _rebuild(obj)
        self.report(
            {"INFO"},
            _T("Baked {count} layers into the base mesh").format(count=len(removed_uids)),
        )
        return {"FINISHED"}


class EL_MT_layer_menu(bpy.types.Menu):
    """レイヤーリスト横の追加操作メニュー"""

    bl_idname = "EL_MT_layer_menu"
    bl_label = "Layer Operations"

    def draw(self, context):
        layout = self.layout
        layout.operator(EL_OT_layer_merge_down.bl_idname, icon="TRIA_UP_BAR")
        layout.operator(EL_OT_bake_upto.bl_idname, icon="IMPORT")


class EL_OT_branch_create(bpy.types.Operator):
    """Create a new branch that diverges at the selected layer"""

    bl_idname = "edit_layers.branch_create"
    bl_label = "Branch From Here"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _poll_stack_idle(context)

    def execute(self, context):
        if _guard_dirty(self, context) or _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        _ensure_branches(stack)

        layer = _active_layer(stack)
        path = _branch_path(stack)
        if layer is not None and any(l.uid == layer.uid for l in path):
            head = layer.uid
        else:
            # レイヤー未選択なら現在のブランチ先端から分岐する
            head = stack.branches[stack.active_branch].head_uid

        br = stack.branches.add()
        br.name = f"Branch {len(stack.branches)}"
        br.head_uid = head
        _assign_branch_color(stack, br)
        stack.active_branch = len(stack.branches) - 1  # update で再構築される
        self.report(
            {"INFO"},
            _T('Created branch "{name}". New recordings will diverge from here').format(
                name=br.name
            ),
        )
        return {"FINISHED"}


class EL_OT_branch_remove(bpy.types.Operator):
    """Remove the active branch (its exclusive layers are removed too)"""

    bl_idname = "edit_layers.branch_remove"
    bl_label = "Remove Branch"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not _poll_stack_idle(context):
            return False
        return len(context.object.edit_layers.branches) > 1

    def execute(self, context):
        if _guard_dirty(self, context) or _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        bi = stack.active_branch

        # このブランチだけが使うレイヤーを特定する
        mine = {l.uid for l in _branch_path(stack, bi)}
        others = set()
        for j in range(len(stack.branches)):
            if j != bi:
                others |= {l.uid for l in _branch_path(stack, j)}
        doomed = mine - others

        name = stack.branches[bi].name
        stack.branches.remove(bi)
        for uid in doomed:
            idx = next(
                (i for i, l in enumerate(stack.layers) if l.uid == uid), None
            )
            if idx is not None:
                stack.layers.remove(idx)

        stack.active_index = min(stack.active_index, len(stack.layers) - 1)
        stack.active_branch = min(bi, len(stack.branches) - 1)
        _rebuild(obj)
        self.report(
            {"INFO"},
            _T('Removed branch "{name}" ({count} exclusive layers)').format(
                name=name, count=len(doomed)
            ),
        )
        return {"FINISHED"}


class EL_OT_compare(bpy.types.Operator):
    """Duplicate other branches side by side for comparison"""

    bl_idname = "edit_layers.compare"
    bl_label = "Compare Branches Side by Side"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not (_poll_stack_idle(context) and context.mode == "OBJECT"):
            return False
        return len(context.object.edit_layers.branches) > 1

    def execute(self, context):
        if _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        _clear_compares(obj)

        offset = max(obj.dimensions.x * 1.5, 2.0)
        n = 1
        total_warnings = 0
        for bi, br in enumerate(stack.branches):
            if bi == stack.active_branch:
                continue
            mesh = obj.data.copy()
            mesh.name = f"{obj.data.name}_cmp_{br.name}"
            path = _branch_path(stack, bi)
            warns, _ = _rebuild_mesh(stack, path, mesh)
            total_warnings += len(warns)

            dup = obj.copy()
            dup.data = mesh
            dup.name = f"{obj.name} [{br.name}]"
            # 比較用コピーにはスタックを持たせない
            ds = dup.edit_layers
            ds.initialized = False
            ds.is_recording = False
            ds.base_mesh = None
            ds.layers.clear()
            ds.branches.clear()
            dup[COMPARE_PROP] = obj.name
            dup.location = obj.location.copy()
            dup.location.x += offset * n
            context.collection.objects.link(dup)
            _compare_names.add(dup.name)
            n += 1

        msg = _T("Duplicated {count} branches for comparison").format(count=n - 1)
        if total_warnings:
            self.report(
                {"WARNING"},
                msg + _T(" ({count} warnings)").format(count=total_warnings),
            )
        else:
            self.report({"INFO"}, msg)
        return {"FINISHED"}


def _clear_compares(obj):
    """比較用の複製を削除する

    削除するのは、このセッションで比較機能自身が作ったと記憶しているもの
    (_compare_names) だけ。マーカーが付いていても身に覚えのないオブジェクト
    (ユーザーが比較複製をさらに複製したもの、保存を跨いだもの) は削除せず、
    マーカーだけ外して通常オブジェクトとして残す。
    戻り値: (削除数, マーカー解除して残した数)
    """
    removed = 0
    released = 0
    for other in list(bpy.data.objects):
        if other.get(COMPARE_PROP) != obj.name:
            continue
        if other.name in _compare_names:
            _compare_names.discard(other.name)
            mesh = other.data
            bpy.data.objects.remove(other)
            if mesh and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            removed += 1
        else:
            del other[COMPARE_PROP]
            released += 1
    return removed, released


class EL_OT_compare_clear(bpy.types.Operator):
    """Delete the comparison duplicates"""

    bl_idname = "edit_layers.compare_clear"
    bl_label = "Clear Comparison"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _poll_mesh_object(context)

    def execute(self, context):
        removed, released = _clear_compares(context.object)
        msg = _T("Removed {count} comparison objects").format(count=removed)
        if released:
            msg += _T(" ({count} kept as regular objects)").format(count=released)
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class EL_OT_notice_clear(bpy.types.Operator):
    """Dismiss the blocked shape key notice"""

    bl_idname = "edit_layers.notice_clear"
    bl_label = "OK"

    @classmethod
    def poll(cls, context):
        return _poll_mesh_object(context)

    def execute(self, context):
        _blocked_notice.pop(context.object.name, None)
        return {"FINISHED"}


class EL_OT_rebuild(bpy.types.Operator):
    """Rebuild the stack"""

    bl_idname = "edit_layers.rebuild"
    bl_label = "Rebuild"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _poll_stack_idle(context)

    def execute(self, context):
        if _guard_shape_keys(self, context):
            return {"CANCELLED"}
        warnings = _rebuild(context.object)
        if warnings:
            self.report({"WARNING"}, _T("Rebuilt ({count} warnings)").format(count=len(warnings)))
        else:
            self.report({"INFO"}, _T("Rebuilt"))
        return {"FINISHED"}


class EL_OT_detach(bpy.types.Operator):
    """Discard the stack without rebuilding, keeping the current mesh (preserves shape keys)"""

    bl_idname = "edit_layers.detach"
    bl_label = "Discard Stack (Keep Current Mesh)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _poll_stack_idle(context)

    def execute(self, context):
        obj = context.object
        stack = obj.edit_layers
        _clear_compares(obj)

        base = stack.base_mesh
        stack.base_mesh = None
        if base and base.users <= 1:
            bpy.data.meshes.remove(base)

        stack.layers.clear()
        stack.branches.clear()
        stack.initialized = False
        stack.active_index = 0
        stack.active_branch = 0
        stack.next_id = 1
        stack.next_uid = 1

        attr = obj.data.attributes.get(ID_ATTR)
        if attr:
            obj.data.attributes.remove(attr)
        _last_warnings.pop(obj.name, None)
        _last_state.pop(obj.name, None)
        _no_key_confirmed.discard(obj.name)
        _blocked_notice.pop(obj.name, None)
        self.report({"INFO"}, _T("Stack discarded; current mesh kept"))
        return {"FINISHED"}


class EL_OT_bake(bpy.types.Operator):
    """Apply the active branch result and remove the stack"""

    bl_idname = "edit_layers.bake"
    bl_label = "Bake and Remove Stack"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _poll_stack_idle(context) and context.mode == "OBJECT"

    def execute(self, context):
        if _guard_dirty(self, context) or _guard_shape_keys(self, context):
            return {"CANCELLED"}
        obj = context.object
        stack = obj.edit_layers
        _rebuild(obj)
        _clear_compares(obj)

        base = stack.base_mesh
        stack.base_mesh = None
        if base and base.users <= 1:
            bpy.data.meshes.remove(base)

        stack.layers.clear()
        stack.branches.clear()
        stack.initialized = False
        stack.active_index = 0
        stack.active_branch = 0
        stack.next_id = 1
        stack.next_uid = 1

        attr = obj.data.attributes.get(ID_ATTR)
        if attr:
            obj.data.attributes.remove(attr)
        _last_warnings.pop(obj.name, None)
        _last_state.pop(obj.name, None)
        _no_key_confirmed.discard(obj.name)
        _blocked_notice.pop(obj.name, None)
        self.report({"INFO"}, _T("Baked and removed the stack"))
        return {"FINISHED"}


# ==================== UI ====================


class EL_UL_layers(bpy.types.UIList):
    """アクティブブランチのパス上のレイヤーだけを根→先端の順で表示する

    行頭 (1 枠固定): このブランチ専用のレイヤーにはアクティブブランチの色チップ、
    共有レイヤーは空白。どのブランチがどこから分かれるかは右側の
    「← ブランチ名」バッジ (色チップ付き) が示す。
    """

    def draw_item(
        self, context, layout, data, item, icon,
        active_data, active_propname, index=0, flt_flag=0,
    ):
        stack = data
        multi = len(stack.branches) > 1
        row = layout.row(align=True)
        if multi:
            ind = row.row(align=True)
            ind.ui_units_x = 0.5
            if _layer_branch_count(stack, item.uid) == 1:
                # 表示専用の色ドット (クリック・ツールチップなし)
                br = stack.branches[stack.active_branch]
                ind.template_node_socket(color=(*br.color, 1.0))
            else:
                ind.label(text="")
        row.prop(item, "name", text="", emboss=False)
        if multi:
            div = _divergence_map(stack).get(item.uid)
            if div:
                # 分岐バッジ: ブランチ色のドット + 「← ブランチ名」(表示専用)
                sub = row.row(align=True)
                sub.alignment = "RIGHT"
                for bi in div[:3]:
                    dot = sub.row(align=True)
                    dot.ui_units_x = 0.5
                    dot.template_node_socket(
                        color=(*stack.branches[bi].color, 1.0)
                    )
                if len(div) == 1:
                    sub.label(text=f"← {stack.branches[div[0]].name}")
                else:
                    sub.label(text=_T("← {count} branches").format(count=len(div)))
        row.prop(
            item,
            "enabled",
            text="",
            icon="HIDE_OFF" if item.enabled else "HIDE_ON",
            emboss=False,
        )

    def filter_items(self, context, data, propname):
        stack = data
        layers = getattr(data, propname)
        path_pos = {l.uid: pos for pos, l in enumerate(_branch_path(stack))}
        flags = []
        order = []
        hidden_order = len(path_pos)
        for l in layers:
            pos = path_pos.get(l.uid)
            if pos is not None and (l.uid != 0 or not stack.branches):
                flags.append(self.bitflag_filter_item)
                order.append(pos)
            else:
                flags.append(0)
                order.append(hidden_order)
                hidden_order += 1
        return flags, order


class EL_UL_branches(bpy.types.UIList):
    """ラジオボタンでアクティブブランチを示し、右側に共有/専用レイヤー数を表示する"""

    def draw_item(
        self, context, layout, data, item, icon,
        active_data, active_propname, index=0, flt_flag=0,
    ):
        stack = data
        row = layout.row(align=True)
        chip = row.row(align=True)
        chip.scale_x = 0.35
        chip.prop(item, "color", text="")
        row.label(
            text="",
            icon="RADIOBUT_ON" if index == stack.active_branch else "RADIOBUT_OFF",
        )
        row.prop(item, "name", text="", emboss=False)
        sub = row.row(align=True)
        sub.alignment = "RIGHT"
        if len(stack.branches) > 1:
            shared, own = _branch_layer_stats(stack, index)
            sub.label(text=_T("shared {shared} + own {own}").format(shared=shared, own=own))
        else:
            sub.label(text=_T("{count} layers").format(count=len(_branch_path(stack, index))))


class EL_PT_panel(bpy.types.Panel):
    bl_label = "Edit Layers"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Edit Layers"

    @classmethod
    def poll(cls, context):
        return _poll_mesh_object(context)

    def draw(self, context):
        layout = self.layout
        obj = context.object
        stack = obj.edit_layers

        if not stack.initialized:
            layout.operator(EL_OT_stack_init.bl_idname, icon="ADD")
            return

        if _blocked_notice.get(obj.name):
            box = layout.box()
            box.label(text="Blocked adding a shape key", icon="ERROR")
            box.label(text="Create shape keys after baking/discarding the stack")
            box.operator(EL_OT_notice_clear.bl_idname, icon="CHECKMARK")

        if _has_shape_keys(obj):
            box = layout.box()
            box.alert = True
            box.label(text="Shape keys detected", icon="ERROR")
            box.label(text="Rebuilds are paused to protect the keys")
            box.label(text="Apply/remove the keys, or:")
            box.operator(EL_OT_detach.bl_idname, icon="X")

        if stack.is_recording:
            box = layout.box()
            rec_layer = next(
                (l for l in stack.layers if l.uid == stack.recording_uid), None
            )
            if rec_layer is not None:
                box.label(text=_T("Re-editing: {name}").format(name=rec_layer.name), icon="REC")
            else:
                box.label(text="Recording a new layer", icon="REC")
            row = box.row(align=True)
            row.operator(EL_OT_commit.bl_idname, icon="CHECKMARK")
            row.operator(EL_OT_cancel.bl_idname, text="Discard", icon="X")
        elif obj.mode in {"EDIT", "SCULPT"}:
            # 記録を通さずに編集/スカルプトモードに入っている
            box = layout.box()
            box.alert = True
            if obj.mode == "EDIT":
                box.label(text="Entered Edit Mode without recording", icon="ERROR")
            else:
                box.label(text="Entered Sculpt Mode without recording", icon="ERROR")
            box.label(text="These edits will not be kept in a layer")
            box.operator(
                EL_OT_adopt.bl_idname, text="Adopt Edits as a Layer", icon="IMPORT"
            )
        else:
            row = layout.row(align=True)
            op = row.operator(
                EL_OT_record_new.bl_idname, text="Record (Edit)", icon="EDITMODE_HLT"
            )
            op.mode = "EDIT"
            op = row.operator(
                EL_OT_record_new.bl_idname, text="Record (Sculpt)", icon="SCULPTMODE_HLT"
            )
            op.mode = "SCULPT"
            if _is_dirty(obj):
                box = layout.box()
                box.alert = True
                box.label(text="Unrecorded edits detected", icon="ERROR")
                box.operator(
                    EL_OT_adopt.bl_idname,
                    text="Adopt as a Layer",
                    icon="IMPORT",
                )
                box.operator(
                    EL_OT_rebuild.bl_idname,
                    text="Discard and Rebuild",
                    icon="FILE_REFRESH",
                )

        # ブランチ
        if stack.branches:
            col = layout.column()
            col.label(
                text=_T("Branches ({count})").format(count=len(stack.branches)),
                icon="NODETREE",
            )
            row = col.row()
            row.template_list(
                "EL_UL_branches", "", stack, "branches", stack, "active_branch", rows=2
            )
            side = row.column(align=True)
            side.enabled = not stack.is_recording
            side.operator(EL_OT_branch_create.bl_idname, text="", icon="ADD")
            side.operator(EL_OT_branch_remove.bl_idname, text="", icon="REMOVE")
            if len(stack.branches) > 1 and not stack.is_recording:
                sub = col.row(align=True)
                sub.operator(
                    EL_OT_compare.bl_idname, text="Compare", icon="MOD_MIRROR"
                )
                sub.operator(EL_OT_compare_clear.bl_idname, text="Clear", icon="X")

        # レイヤー (アクティブブランチのパス)
        col = layout.column()
        hdr = col.row(align=True)
        if stack.branches:
            br_name = stack.branches[
                max(0, min(stack.active_branch, len(stack.branches) - 1))
            ].name
            hdr.label(text=_T("Layers — {name}").format(name=br_name), icon="RENDERLAYERS")
        else:
            hdr.label(text="", icon="RENDERLAYERS")
        sub = hdr.row(align=True)
        sub.alignment = "RIGHT"
        sub.prop(stack, "show_influence", text="", icon="OVERLAY")
        row = col.row()
        row.template_list(
            "EL_UL_layers", "", stack, "layers", stack, "active_index", rows=4
        )
        side = row.column(align=True)
        side.enabled = not stack.is_recording
        side.operator(EL_OT_layer_move.bl_idname, text="", icon="TRIA_UP").direction = "UP"
        side.operator(EL_OT_layer_move.bl_idname, text="", icon="TRIA_DOWN").direction = "DOWN"
        side.separator()
        side.operator(EL_OT_layer_remove.bl_idname, text="", icon="REMOVE")
        side.separator()
        side.menu(EL_MT_layer_menu.bl_idname, text="", icon="DOWNARROW_HLT")

        if not stack.is_recording:
            layout.operator(EL_OT_record_edit.bl_idname, icon="EDITMODE_HLT")
            row = layout.row(align=True)
            row.operator(EL_OT_rebuild.bl_idname, icon="FILE_REFRESH")
            row.operator(EL_OT_bake.bl_idname, text="Bake", icon="IMPORT")
            help_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "docs", "index.html"
            )
            if os.path.exists(help_path):
                row.operator(
                    "wm.url_open", text="", icon="HELP"
                ).url = "file:///" + help_path.replace("\\", "/")
            layout.label(text="Unrecorded edits are detected and can be adopted", icon="INFO")

        warnings = _last_warnings.get(obj.name)
        if warnings:
            box = layout.box()
            box.label(text=_T("{count} warnings:").format(count=len(warnings)), icon="ERROR")
            for w in warnings[:8]:
                box.label(text=w)
            if len(warnings) > 8:
                box.label(text=_T("... and {count} more").format(count=len(warnings) - 8))


# ==================== 登録 ====================

classes = (
    EL_Layer,
    EL_Branch,
    EL_Stack,
    EL_OT_stack_init,
    EL_OT_record_new,
    EL_OT_record_edit,
    EL_OT_commit,
    EL_OT_cancel,
    EL_OT_adopt,
    EL_OT_layer_remove,
    EL_OT_layer_move,
    EL_OT_layer_merge_down,
    EL_OT_bake_upto,
    EL_MT_layer_menu,
    EL_OT_branch_create,
    EL_OT_branch_remove,
    EL_OT_compare,
    EL_OT_compare_clear,
    EL_OT_notice_clear,
    EL_OT_rebuild,
    EL_OT_detach,
    EL_OT_bake,
    EL_UL_layers,
    EL_UL_branches,
    EL_PT_panel,
)


def register():
    try:
        bpy.app.translations.unregister(__name__)
    except Exception:
        pass
    bpy.app.translations.register(__name__, _translations_dict())
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.edit_layers = PointerProperty(type=EL_Stack)
    if _el_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_el_depsgraph_handler)
    if _el_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_el_load_post)
    global _draw_handle
    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_influence, (), "WINDOW", "POST_VIEW"
        )
    try:
        # アドオン有効化がファイル読み込みより先の場合は bpy.data に触れない
        _rescan_no_keys()
    except AttributeError:
        pass


def unregister():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        _draw_handle = None
    try:
        bpy.app.translations.unregister(__name__)
    except Exception:
        pass
    if _el_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_el_depsgraph_handler)
    if _el_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_el_load_post)
    del bpy.types.Object.edit_layers
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
