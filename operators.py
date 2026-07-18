"""Operator definitions"""

import json

import bpy
import bmesh
from bpy.props import EnumProperty

from .common import (
    COMPARE_PROP,
    ID_ATTR,
    _assign_branch_color,
    _blocked_notice,
    _compare_names,
    _last_state,
    _last_warnings,
    _no_key_confirmed,
    _recording,
)
from .core import (
    _apply_layer,
    _compute_diff,
    _diff_is_empty,
    _ensure_id_layer,
    _resolve_ids,
    _take_snapshot,
)
from .i18n import _T
from .stack import (
    _active_layer,
    _branch_path,
    _clear_compares,
    _ensure_branches,
    _guard_dirty,
    _guard_shape_keys,
    _has_shape_keys,
    _layer_branch_count,
    _poll_mesh_object,
    _poll_stack_idle,
    _rebuild,
    _rebuild_mesh,
    _safe_rebuild,
)


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

        # Assign persistent IDs to all vertices
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

        # Keep a copy of the ID-carrying state as the base mesh
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

    # While recording, the user may freely switch between Edit/Sculpt modes.
    # The diff against the mesh state at commit time is what gets recorded.

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

        # Recording always starts from the state with all layers applied, ignoring
        # the enabled flags (recording on a disabled layer would make the diff ambiguous)
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

        # Build up to just before the target layer and take the snapshot
        _rebuild(obj, upto=pos, respect_enabled=False)
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        idl = _ensure_id_layer(bm)
        pre = _take_snapshot(bm, idl)

        # Start editing from the state with the target layer itself applied
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
            # Snapshot lost, e.g. Blender was restarted while recording
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
            # Append the new layer at the tip of the active branch
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

        # Reproduce "the state when editing started" = the last rebuild.
        # Use the layer set and branch from the rebuild history (_last_state)
        # when available; approximate from the current setup otherwise.
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

        # Reconstruct the pre-edit state into a temporary mesh and snapshot it
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

        # Diff the current mesh (containing the unrecorded edits) as post
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

        # Append the layer at the tip of the branch where the edits were made
        if stack.active_branch != target_branch:
            stack.active_branch = target_branch  # no rebuild fires while dirty
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
        # Reconnect child layers and branch heads to the parent
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

        # The swap partner on the path (the parent side is "up")
        if self.direction == "UP":
            if pos == 0:
                return {"CANCELLED"}
            upper, lower = path[pos - 1], layer
        else:
            if pos >= len(path) - 1:
                return {"CANCELLED"}
            upper, lower = layer, path[pos + 1]

        # Swapping across a divergence point would change other branches, so forbid it
        if (
            _layer_branch_count(stack, upper.uid) > 1
            or _layer_branch_count(stack, lower.uid) > 1
        ):
            self.report({"ERROR"}, _T("Cannot move across a shared layer"))
            return {"CANCELLED"}

        # Turn ... P -> upper -> lower -> C ... into ... P -> lower -> upper -> C ...
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

        # Build the pre-merge state (just before parent) and re-diff the merged result
        warnings = []
        bm = bmesh.new()
        try:
            bm.from_mesh(stack.base_mesh)
            _ensure_id_layer(bm)
            # Deterministic like the record flow: ignore the enabled flags
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

        # Removing a collection item invalidates existing references, so save these first
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
        # Every branch must pass through the layer so no branch loses its trunk
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

        # Bake into the base mesh (safe: from_mesh reads fully before writing back)
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
            # No layer selected: branch from the current branch tip instead
            head = stack.branches[stack.active_branch].head_uid

        br = stack.branches.add()
        br.name = f"Branch {len(stack.branches)}"
        br.head_uid = head
        _assign_branch_color(stack, br)
        stack.active_branch = len(stack.branches) - 1  # the update callback rebuilds
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

        # Find the layers used only by this branch
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
            # Comparison copies must not carry a stack of their own
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
