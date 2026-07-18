"""Property group definitions and update callbacks"""

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from .stack import _has_shape_keys, _is_dirty, _rebuild


def _tag_redraw_view3d(context):
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _on_enabled_update(self, context):
    obj = context.object
    if obj and obj.type == "MESH":
        stack = obj.edit_layers
        # Defer rebuilds while unrecorded edits exist (do not overwrite them).
        # Also defer while shape keys exist (a rebuild would destroy them).
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
    # Persistent layer UID (separate from vertex IDs; 0 = unassigned)
    uid: IntProperty(default=0)
    # UID of the parent layer (0 = directly on the base mesh)
    parent: IntProperty(default=0)
    # Diff JSON
    data: StringProperty(default="")


class EL_Branch(bpy.types.PropertyGroup):
    name: StringProperty(name="Name", default="Branch")
    # UID of this branch's tip layer (0 = base mesh only)
    head_uid: IntProperty(default=0)
    # Identification color (shown as list chips and divergence badges; click to change)
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
    # Next vertex ID to assign (0 means unassigned, so start from 1)
    next_id: IntProperty(default=1)
    # Next layer UID to assign
    next_uid: IntProperty(default=1)
    is_recording: BoolProperty(default=False)
    # UID of the layer being recorded (0 = new layer)
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
