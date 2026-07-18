"""Edit Layers — 3ds Max の Edit Poly のようなレイヤーベースの非破壊メッシュ編集スタック

エントリポイント。クラス登録とアプリケーションハンドラの管理のみを行い、
実装は以下のモジュールに分かれている:

- common: 定数とセッション状態
- i18n: UI 翻訳
- core: 差分エンジン (永続 ID / スナップショット / 差分 / 適用)
- stack: スタック運用 (ブランチ / 再構築 / 状態検出 / ガード)
- props: プロパティグループ
- operators: オペレータ
- ui: パネル・リスト・オーバーレイ
- handlers: シェイプキーロック等のアプリケーションハンドラ
"""

import bpy
from bpy.props import PointerProperty

from . import handlers, i18n, operators, props, ui

classes = (
    props.EL_Layer,
    props.EL_Branch,
    props.EL_Stack,
    operators.EL_OT_stack_init,
    operators.EL_OT_record_new,
    operators.EL_OT_record_edit,
    operators.EL_OT_commit,
    operators.EL_OT_cancel,
    operators.EL_OT_adopt,
    operators.EL_OT_layer_remove,
    operators.EL_OT_layer_move,
    operators.EL_OT_layer_merge_down,
    operators.EL_OT_bake_upto,
    ui.EL_MT_layer_menu,
    operators.EL_OT_branch_create,
    operators.EL_OT_branch_remove,
    operators.EL_OT_compare,
    operators.EL_OT_compare_clear,
    operators.EL_OT_notice_clear,
    operators.EL_OT_rebuild,
    operators.EL_OT_detach,
    operators.EL_OT_bake,
    ui.EL_UL_layers,
    ui.EL_UL_branches,
    ui.EL_PT_panel,
)


def register():
    try:
        bpy.app.translations.unregister(__name__)
    except Exception:
        pass
    bpy.app.translations.register(__name__, i18n._translations_dict())
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.edit_layers = PointerProperty(type=props.EL_Stack)
    if handlers._el_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(handlers._el_depsgraph_handler)
    if handlers._el_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(handlers._el_load_post)
    ui.register_draw_handler()
    try:
        # アドオン有効化がファイル読み込みより先の場合は bpy.data に触れない
        handlers._rescan_no_keys()
    except AttributeError:
        pass


def unregister():
    ui.unregister_draw_handler()
    try:
        bpy.app.translations.unregister(__name__)
    except Exception:
        pass
    if handlers._el_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(handlers._el_depsgraph_handler)
    if handlers._el_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(handlers._el_load_post)
    del bpy.types.Object.edit_layers
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
