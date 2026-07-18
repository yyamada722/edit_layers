"""プロパティグループ定義と更新コールバック"""

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
