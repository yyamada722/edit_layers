"""Diff engine: persistent IDs / snapshots / diff computation / apply"""

import bmesh
from mathutils import Vector

from .common import EPS, ID_ATTR
from .i18n import _T


def _ensure_id_layer(bm):
    """Ensure and return the persistent ID layer on the bmesh"""
    idl = bm.verts.layers.int.get(ID_ATTR)
    if idl is None:
        idl = bm.verts.layers.int.new(ID_ATTR)
    return idl


# Default attribute values (values equal to these are omitted from diffs)
_FACE_ATTR_DEFAULT = (0, False)  # (material_index, smooth)
_EDGE_ATTR_DEFAULT = (False, True, 0.0, 0.0)  # (seam, smooth, crease, bevel_weight)
_VERT_ATTR_DEFAULT = (0.0, 0.0)  # (crease, bevel_weight)


def _take_snapshot(bm, idl):
    """Snapshot the current bmesh state keyed by persistent IDs

    verts: {id: (x, y, z)}
    faces: {frozenset(ids): [ids (loop order)]}
    edges: {frozenset((a, b))}
    face_attrs / edge_attrs / vert_attrs: element key -> attribute tuple
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
    """Normalize persistent IDs of the edited bmesh

    - Duplicated IDs (attribute copies from subdivide etc.): only the vertex
      closest to its pre-edit position keeps the ID, the rest become new.
    - ID 0 (new vertices) get fresh IDs assigned.
    Returns the updated next_id.
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

    # Avoid collisions with IDs pasted from other objects that exceed next_id
    max_id = max((v[idl] for v in bm.verts), default=0)
    next_id = max(next_id, max_id + 1)

    for v in bm.verts:
        if v[idl] == 0:
            v[idl] = next_id
            next_id += 1
    return next_id


def _face_edge_pairs(ids):
    """Return the set of edges spanned by a face given as a vertex ID list"""
    n = len(ids)
    return {frozenset((ids[j], ids[(j + 1) % n])) for j in range(n)}


def _compute_diff(pre, post):
    """Build the diff stored in a layer from the pre/post edit snapshots"""
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

    # Creating new faces also creates their edges, so only record edges not covered by a new face
    covered = set()
    for ids in new_faces:
        covered |= _face_edge_pairs(ids)
    new_edges = [sorted(p) for p in (post["edges"] - pre["edges"]) if p not in covered]

    # Do not record edges that disappear as a side effect of vertex deletion
    deleted_edge_set = {
        p for p in (pre["edges"] - post["edges"]) if not (p & dead)
    }
    deleted_edges = [sorted(p) for p in deleted_edge_set]

    # Do not record faces that disappear as a side effect of vertex/edge deletion
    deleted_faces = []
    for key, ids in pre["faces"].items():
        if key in post["faces"] or (key & dead):
            continue
        if _face_edge_pairs(ids) & deleted_edge_set:
            continue
        deleted_faces.append(sorted(key))

    # Attribute diffs (defaults are already omitted on the snapshot side)
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
    """Express new vertices relative to anchor vertices

    For each new vertex, pick the 3 nearest vertices that survived from the
    pre-edit state as anchors and store the offset from their centroid. On
    replay the position is restored from the anchors' current positions, so new
    geometry follows upstream deformation (absolute fallback when anchors are lost).
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
    """Find an existing face from a set of vertex IDs"""
    v0 = vmap.get(ids[0])
    if v0 is None or not v0.is_valid:
        return None
    target = set(ids)
    for f in v0.link_faces:
        if len(f.verts) == len(ids) and {v[idl] for v in f.verts} == target:
            return f
    return None


def _apply_layer(bm, idl, data, warnings, layer_name):
    """Apply one layer's diff to the bmesh

    Deletions whose target is missing are skipped silently (it just means an
    upstream change already removed it). Creations and moves referencing
    missing vertices are skipped with a warning.
    """
    # Adding a custom data layer invalidates element references and layer
    # handles of that domain, so ensure all needed layers before taking references
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
    idl = _ensure_id_layer(bm)  # re-fetch: adding vertex layers invalidates the handle

    vmap = {v[idl]: v for v in bm.verts if v[idl] != 0}

    # 1. Delete vertices (attached edges/faces are deleted in cascade)
    doomed = [vmap.pop(i) for i in data.get("deleted_verts", []) if i in vmap]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context="VERTS")

    # 2. Delete edges (attached faces cascade, vertices are kept)
    # 'EDGES' would also delete isolated vertices and break downstream layer
    # references, so use 'EDGES_FACES'. Vertex deletion is explicit via deleted_verts.
    edges = []
    for a, b in data.get("deleted_edges", []):
        va, vb = vmap.get(a), vmap.get(b)
        if va and vb and va.is_valid and vb.is_valid:
            e = bm.edges.get((va, vb))
            if e:
                edges.append(e)
    if edges:
        bmesh.ops.delete(bm, geom=edges, context="EDGES_FACES")

    # 3. Delete faces (faces only; keep vertices and edges)
    faces = []
    for ids in data.get("deleted_faces", []):
        f = _find_face(vmap, idl, ids)
        if f:
            faces.append(f)
    if faces:
        bmesh.ops.delete(bm, geom=faces, context="FACES_ONLY")

    # 4. Move vertices (delta)
    # Applied before creation so anchor-relative new vertices can reference
    # the anchors' post-move positions (the diff is computed against them too)
    for i, d in data.get("moved", {}).items():
        v = vmap.get(int(i))
        if v is None or not v.is_valid:
            warnings.append(_T("{layer}: missing vertex {i} to move").format(layer=layer_name, i=i))
            continue
        v.co += Vector(d)

    # 5. New vertices (JSON keys are strings, convert back to int)
    # With anchor data, restore the position as "anchor centroid + offset" so
    # it follows upstream deformation; otherwise use absolute coordinates (legacy format).
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

    # 6. New edges (wire edges etc. not covered by any face)
    for a, b in data.get("new_edges", []):
        va, vb = vmap.get(a), vmap.get(b)
        if not (va and vb and va.is_valid and vb.is_valid):
            warnings.append(_T("{layer}: missing vertices for edge ({a}, {b})").format(layer=layer_name, a=a, b=b))
            continue
        if bm.edges.get((va, vb)) is None:
            bm.edges.new((va, vb))

    # 7. New faces
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

    # 8. Attributes (material / smooth / seam / sharp / crease / bevel weight)
    # Missing targets just mean an upstream change removed them, so skip silently
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
