"""差分エンジン: 永続 ID / スナップショット / 差分計算 / 適用"""

import bmesh
from mathutils import Vector

from .common import EPS, ID_ATTR
from .i18n import _T


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
