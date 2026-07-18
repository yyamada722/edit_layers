"""配布用 zip を作るビルドスクリプト

使い方:
    python build_zip.py

blender_manifest.toml のバージョンを読み取り、正規の拡張機能形式
(マニフェストがアーカイブ直下) の zip を dist/edit_layers-<version>.zip に生成する。
命名・内容とも公式の `blender --command extension build` の既定出力と同一で、
extensions.blender.org への申請にも Install from Disk にもこれを使う。

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

    out = dist / f"{PACKAGE}-{version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            zf.write(HERE / name, name)
    print(f"built: {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
