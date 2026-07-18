"""Panel, lists, menu and the viewport overlay"""

import bpy

from .common import _blocked_notice, _last_warnings
from .i18n import _T
from .operators import (
    EL_OT_adopt,
    EL_OT_bake,
    EL_OT_bake_upto,
    EL_OT_branch_create,
    EL_OT_branch_remove,
    EL_OT_cancel,
    EL_OT_commit,
    EL_OT_compare,
    EL_OT_compare_clear,
    EL_OT_detach,
    EL_OT_layer_merge_down,
    EL_OT_layer_move,
    EL_OT_layer_remove,
    EL_OT_notice_clear,
    EL_OT_rebuild,
    EL_OT_record_edit,
    EL_OT_record_new,
    EL_OT_stack_init,
)
from .stack import (
    _branch_layer_stats,
    _branch_path,
    _divergence_map,
    _has_shape_keys,
    _influence_local,
    _is_dirty,
    _layer_branch_count,
    _poll_mesh_object,
)

# Bundled documentation is excluded from the package, so help opens online
HELP_URL = "https://github.com/yyamada722/edit_layers"


def _draw_influence():
    """Draw vertices affected by the active layer (orange: moved / green: created)"""
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

    # Use the dedicated point shader (fixed-function point size does not work
    # on the Vulkan backend). Fall back to UNIFORM_COLOR where unavailable.
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
class EL_MT_layer_menu(bpy.types.Menu):
    """Extra layer operations shown next to the layer list"""

    bl_idname = "EL_MT_layer_menu"
    bl_label = "Layer Operations"

    def draw(self, context):
        layout = self.layout
        layout.operator(EL_OT_layer_merge_down.bl_idname, icon="TRIA_UP_BAR")
        layout.operator(EL_OT_bake_upto.bl_idname, icon="IMPORT")
class EL_UL_layers(bpy.types.UIList):
    """Show only layers on the active branch path, in root-to-head order

    Leading slot (fixed width): a color chip of the active branch for layers
    exclusive to it, blank for shared layers. Which branch diverges where is
    shown by the "<- branch name" badge on the right (with a color dot).
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
                # Display-only color dot (no click, no tooltip)
                br = stack.branches[stack.active_branch]
                ind.template_node_socket(color=(*br.color, 1.0))
            else:
                ind.label(text="")
        row.prop(item, "name", text="", emboss=False)
        if multi:
            div = _divergence_map(stack).get(item.uid)
            if div:
                # Divergence badge: branch color dot + "<- branch name" (display only)
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
    """Radio buttons mark the active branch; shared/own layer counts on the right"""

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
            # The user entered Edit/Sculpt mode without recording
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

        # Branches
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

        # Layers (path of the active branch)
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
            row.operator("wm.url_open", text="", icon="HELP").url = HELP_URL
            layout.label(text="Unrecorded edits are detected and can be adopted", icon="INFO")

        warnings = _last_warnings.get(obj.name)
        if warnings:
            box = layout.box()
            box.label(text=_T("{count} warnings:").format(count=len(warnings)), icon="ERROR")
            for w in warnings[:8]:
                box.label(text=w)
            if len(warnings) > 8:
                box.label(text=_T("... and {count} more").format(count=len(warnings) - 8))
def register_draw_handler():
    global _draw_handle
    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_influence, (), "WINDOW", "POST_VIEW"
        )


def unregister_draw_handler():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        _draw_handle = None
