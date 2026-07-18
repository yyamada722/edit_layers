"""スタック運用: ブランチ解決・再構築・状態検出・ガード"""

import json

import bpy
import bmesh

from .common import (
    COMPARE_PROP,
    ID_ATTR,
    _assign_branch_color,
    _compare_names,
    _influence_cache,
    _last_state,
    _last_warnings,
    _rebuild_serial,
)
from .core import _apply_layer, _ensure_id_layer
from .i18n import _T


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
