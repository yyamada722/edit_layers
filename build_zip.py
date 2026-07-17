"""配布用 zip を作るビルドスクリプト

使い方:
    python build_zip.py

bl_info と blender_manifest.toml のバージョン一致を確認してから 2 種類の zip を生成する:

- dist/edit_layers-<version>.zip
    トップレベルに edit_layers/ フォルダを持つ従来形式。旧来のアドオン
    (プリファレンス > アドオン > インストール) と拡張機能 (Install from Disk)
    のどちらでもインストールできる。
- dist/edit_layers-<version>-extension.zip
    blender_manifest.toml をアーカイブ直下に置く正規の拡張機能形式。
    extensions.blender.org への申請にはこちらを使う
    (blender --command extension build と同じレイアウト)。
"""

import ast
import re
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
PACKAGE = "edit_layers"
# zip に含めるファイル
FILES = ["__init__.py", "blender_manifest.toml", "readme.md"]
# 再帰的に含めるフォルダ (ヘルプなど)
DIRS = ["docs"]


def bl_info_version():
    src = (HERE / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'"version":\s*\(([^)]+)\)', src)
    return ".".join(x.strip() for x in m.group(1).split(","))


def manifest_version():
    src = (HERE / "blender_manifest.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', src, re.M)
    return m.group(1)


def main():
    ast.parse((HERE / "__init__.py").read_text(encoding="utf-8"))  # 構文チェック

    v_info = bl_info_version()
    v_manifest = manifest_version()
    if v_info != v_manifest:
        raise SystemExit(
            f"version mismatch: bl_info={v_info} manifest={v_manifest}"
        )

    dist = HERE / "dist"
    dist.mkdir(exist_ok=True)

    def collect():
        for name in FILES:
            path = HERE / name
            if not path.exists():
                raise SystemExit(f"missing file: {name}")
            yield path, name
        for dirname in DIRS:
            root = HERE / dirname
            for path in sorted(root.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    yield path, path.relative_to(HERE).as_posix()

    # 従来形式 (トップレベルフォルダあり): 手動インストール両対応
    legacy = dist / f"{PACKAGE}-{v_info}.zip"
    with zipfile.ZipFile(legacy, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in collect():
            zf.write(path, f"{PACKAGE}/{rel}")
    print(f"built: {legacy} ({legacy.stat().st_size} bytes)")

    # 正規の拡張機能形式 (マニフェストがアーカイブ直下): extensions.blender.org 申請用
    ext = dist / f"{PACKAGE}-{v_info}-extension.zip"
    with zipfile.ZipFile(ext, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in collect():
            zf.write(path, rel)
    print(f"built: {ext} ({ext.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
