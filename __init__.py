"""Edit Layers — layer-based non-destructive mesh editing stack

Entry point: this module only handles class registration and application
handler management. The implementation lives in the following modules:

- common: constants and session state
- i18n: UI translations
- core: diff engine (persistent IDs / snapshots / diff / apply)
- stack: stack management (branches / rebuild / state detection / guards)
- props: property groups
- operators: operators
- ui: panel, lists and viewport overlay
- handlers: application handlers (shape key lock, session reset)
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
        # bpy.data may be restricted while add-ons are enabled during startup
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
