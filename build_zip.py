"""配布用 zip を作るビルドスクリプト

使い方:
    python build_zip.py

blender_manifest.toml のバージョンを読み取り、アドオンファイルのみを含む
2 種類の zip を dist/ に生成する:

- edit_layers-<version>-extension.zip
    blender_manifest.toml をアーカイブ直下に置く正規の拡張機能形式。
    extensions.blender.org への申請にはこちらを使う。
- edit_layers-<version>.zip
    トップレベルに edit_layers/ フォルダを持つ形式 (Install from Disk 用)。

パッケージに含めるのは *.py と blender_manifest.toml と LICENSE のみ
(ドキュメント・テスト・ツール類は含めない)。
"""

import ast
import re
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
PACKAGE = "edit_layers"


def manifest_version():
    src = (HERE / "blender_manifest.toml").read_text(encoding="utf-8")
    return re.search(r'^version\s*=\s*"([^"]+)"', src, re.M).group(1)


def package_files():
    files = ["blender_manifest.toml", "LICENSE"]
    for path in sorted(HERE.glob("*.py")):
        if path.name != "build_zip.py":
            files.append(path.name)
    return files


def main():
    files = package_files()
    for name in files:
        path = HERE / name
        if not path.exists():
            raise SystemExit(f"missing file: {name}")
        if name.endswith(".py"):
            ast.parse(path.read_text(encoding="utf-8"))  # 構文チェック

    version = manifest_version()
    dist = HERE / "dist"
    dist.mkdir(exist_ok=True)

    ext = dist / f"{PACKAGE}-{version}-extension.zip"
    with zipfile.ZipFile(ext, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            zf.write(HERE / name, name)
    print(f"built: {ext} ({ext.stat().st_size} bytes)")

    nested = dist / f"{PACKAGE}-{version}.zip"
    with zipfile.ZipFile(nested, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            zf.write(HERE / name, f"{PACKAGE}/{name}")
    print(f"built: {nested} ({nested.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
