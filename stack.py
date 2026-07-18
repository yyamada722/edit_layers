"""Stack management: branch resolution, rebuild, state detection, guards"""

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
    """Migrate a v0.1 linear stack (no uids assigned) to the tree structure"""
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
    """Walk from the branch head to the root; return layers in root-to-head order"""
    if not stack.branches:
        # Pre-migration (v0.1) data: use registration order as-is
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
    """List of branch indices whose path contains this layer"""
    return [
        bi
        for bi in range(len(stack.branches))
        if any(l.uid == uid for l in _branch_path(stack, bi))
    ]


def _layer_branch_count(stack, uid):
    """Number of branches passing through this layer (2+ means shared)"""
    return len(_layer_branches(stack, uid))


def _divergence_map(stack):
    """Map of uid on the active path -> other branch indices diverging there

    The shared part of another branch's path is always contiguous from the
    root (it is a tree), so the last shared layer is the divergence point.
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
    """Return (number of shared layers, number of layers exclusive to this branch)"""
    mine = {l.uid for l in _branch_path(stack, branch_index)}
    others = set()
    for j in range(len(stack.branches)):
        if j != branch_index:
            others |= {l.uid for l in _branch_path(stack, j)}
    own = len(mine - others)
    return len(mine) - own, own


def _rebuild_mesh(stack, path, mesh, respect_enabled=True, upto=None):
    """Apply the layers of path in order onto a copy of the base mesh, write to mesh

    Returns (warnings, list of layer uids actually applied).
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
            # Applying attributes may add vertex layers, which invalidates
            # existing layer handles, so re-fetch the ID layer every iteration
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
    """Take a mesh fingerprint used to detect unrecorded edits

    Uses element counts plus an absolute-coordinate sum and a position-weighted
    sum: the plain absolute sum cancels out on symmetric edits (e.g. moving
    +/-x vertices together), so a per-index weighted sum is added. Fast via numpy.
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
    """Rebuild the object from the active (or given) branch"""
    stack = obj.edit_layers
    path = _branch_path(stack, branch_index)
    warnings, applied = _rebuild_mesh(stack, path, obj.data, respect_enabled, upto)
    _rebuild_serial[0] += 1  # invalidate the influence highlight cache
    _last_warnings[obj.name] = warnings
    _last_state[obj.name] = {
        "fp": _fingerprint(obj.data),
        "uids": applied,
        "branch": stack.active_branch if branch_index is None else branch_index,
    }
    return warnings


def _safe_rebuild(obj):
    """Skip the rebuild when shape keys exist; only reset state tracking

    Rebuilding (bmesh -> mesh) would overwrite shape key data with the basis,
    so the mesh is left untouched when keys are detected. Returns None on skip.
    """
    if _has_shape_keys(obj):
        _last_state.pop(obj.name, None)
        return None
    return _rebuild(obj)


def _is_dirty(obj):
    """Whether unrecorded edits exist since the last rebuild

    Not evaluated in edit modes (mesh data is not flushed yet). Also returns
    False when there is no rebuild history (e.g. right after loading a file).
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
    """Return local coordinates of vertices affected by the active layer

    Returns (moved coords, created coords), or None when display conditions
    are not met. Called on every redraw from the draw callback, so results are
    cached while the layer and rebuild state stay unchanged.
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
    """Initialized and not currently recording"""
    if not _poll_mesh_object(context):
        return False
    stack = context.object.edit_layers
    return stack.initialized and not stack.is_recording


def _guard_dirty(op, context):
    """Block operations that would rebuild away unrecorded edits"""
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
    """Block operations that would destroy shape keys via a rebuild

    Rebuilding (bmesh -> mesh) overwrites shape key data with the basis, so
    every destructive operation is stopped while keys are detected.
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
    """Delete the comparison duplicates

    Only objects this session's compare feature remembers creating
    (_compare_names) are deleted. Marker-carrying objects we do not recognize
    (user-made copies of comparison duplicates, or objects loaded from a saved
    file) are kept; only their marker is removed so they become regular objects.
    Returns (number removed, number released).
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
