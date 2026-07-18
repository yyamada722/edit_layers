"""Application handlers: shape key addition lock and session state reset"""

import bpy
from bpy.app.handlers import persistent

from .common import (
    _blocked_notice,
    _compare_names,
    _last_state,
    _last_warnings,
    _no_key_confirmed,
    _recording,
)


def _rescan_no_keys():
    """Register stack objects currently without shape keys into the confirmed set"""
    for obj in bpy.data.objects:
        if (
            obj.type == "MESH"
            and obj.edit_layers.initialized
            and obj.data.shape_keys is None
        ):
            _no_key_confirmed.add(obj.name)


@persistent
def _el_depsgraph_handler(scene, depsgraph):
    """Detect and revert shape keys added to objects with an active stack"""
    for upd in depsgraph.updates:
        obj = getattr(upd.id, "original", upd.id)
        if not isinstance(obj, bpy.types.Object) or obj.type != "MESH":
            continue
        stack = obj.edit_layers
        if not stack.initialized:
            continue
        if obj.data.shape_keys is None:
            # Confirmed key-less: if a key appears later it must have been added
            _no_key_confirmed.add(obj.name)
            continue
        if obj.name in _no_key_confirmed and obj.mode == "OBJECT":
            # Key added during this session -> fire the lock (nothing is lost right after creation)
            obj.shape_key_clear()
            _blocked_notice[obj.name] = True
            print(
                f"[Edit Layers] {obj.name}: blocked adding shape keys while a "
                "stack is active (create them after baking/discarding)"
            )
        # Unconfirmed objects (e.g. old files saved with both keys and a stack)
        # keep their data; the guards and the panel warning handle those


@persistent
def _el_load_post(_dummy):
    """Reset session state when a file is loaded"""
    _recording.clear()
    _compare_names.clear()
    _last_warnings.clear()
    _last_state.clear()
    _no_key_confirmed.clear()
    _blocked_notice.clear()
    _rescan_no_keys()
