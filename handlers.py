"""アプリケーションハンドラ: シェイプキーの追加ロックとセッション状態のリセット"""

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
    """キーなしで運用中のスタックオブジェクトを確認済みセットに登録する"""
    for obj in bpy.data.objects:
        if (
            obj.type == "MESH"
            and obj.edit_layers.initialized
            and obj.data.shape_keys is None
        ):
            _no_key_confirmed.add(obj.name)


@persistent
def _el_depsgraph_handler(scene, depsgraph):
    """スタック運用中のオブジェクトへのシェイプキー追加を検知して取り消す"""
    for upd in depsgraph.updates:
        obj = getattr(upd.id, "original", upd.id)
        if not isinstance(obj, bpy.types.Object) or obj.type != "MESH":
            continue
        stack = obj.edit_layers
        if not stack.initialized:
            continue
        if obj.data.shape_keys is None:
            # キーなしを確認 → 以後キーが現れたら「追加された」と断定できる
            _no_key_confirmed.add(obj.name)
            continue
        if obj.name in _no_key_confirmed and obj.mode == "OBJECT":
            # セッション中に追加されたキー → ロック発動 (追加直後なので失うデータはない)
            obj.shape_key_clear()
            _blocked_notice[obj.name] = True
            print(
                f"[Edit Layers] {obj.name}: blocked adding shape keys while a "
                "stack is active (create them after baking/discarding)"
            )
        # 確認済みでない場合 (キーと共存した状態で保存された古いファイル等) は
        # データを消さず、ガード + パネル警告に任せる


@persistent
def _el_load_post(_dummy):
    """ファイル読み込みでセッション状態をリセットする"""
    _recording.clear()
    _compare_names.clear()
    _last_warnings.clear()
    _last_state.clear()
    _no_key_confirmed.clear()
    _blocked_notice.clear()
    _rescan_no_keys()
