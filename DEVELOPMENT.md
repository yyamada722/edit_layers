# 開発メモ

ユーザー向けの説明は [readme.md](readme.md) と同梱ヘルプ ([docs/index.html](docs/index.html)) を参照。
ここにはビルド・配布・内部設計の情報をまとめる。

## ビルド

```
python build_zip.py
```

bl_info と blender_manifest.toml のバージョン一致を確認してから、2 種類の zip が
`dist/` に生成される:

- `edit_layers-<version>.zip` — トップレベルフォルダあり (従来形式)。
  プリファレンス > アドオン > インストール / 拡張機能 > Install from Disk のどちらでも使える
- `edit_layers-<version>-extension.zip` — マニフェストがアーカイブ直下の正規形式。
  **extensions.blender.org への申請にはこちらを使う**

バージョンを上げるときは `__init__.py` の `bl_info["version"]` と
`blender_manifest.toml` の `version` の両方を更新する (不一致だとビルドが止まる)。

## Blender Extensions 申請メモ

- 申請用 zip は `-extension.zip` をそのままアップロードすればよい。公式ビルダーで
  作りたい場合は `blender --command extension build --source-dir . --output-filepath <出力>`
  でも同じ内容になる (`[build]` の除外設定はマニフェスト内に定義済み)
- 検証: `blender --command extension validate dist/edit_layers-<version>-extension.zip`
- サイト側で入力する素材 (アイコン 256x256、Featured image / プレビュー 1920x1080)
  は `store_assets/` にある (配布 zip には含まれない)
- マニフェストの `maintainer` / `copyright` / `version` は申請前に確認すること

## 仕組み

- **永続頂点 ID**: 初期化時に全頂点へ INT 属性 `el_id` で不変 ID を付与する (0 = 未割り当て)。
  レイヤーの記録・再生はすべて頂点インデックスではなくこの ID を参照するため、
  上流レイヤーの変更でインデックスがずれても参照が壊れない。
- **レイヤー = トポロジ差分**: 編集セッションの前後スナップショットを ID 基準で比較し、
  以下を JSON としてレイヤーに保存する:
  - `moved` — 既存頂点の移動 (delta)。上流で頂点が動いても相対的に追従する
  - `new_verts` + `anchors` — 新規頂点。近傍の既存頂点 3 点をアンカーとして
    「アンカー重心 + オフセット」で保存するため、上流の変形に追従する
    (アンカーが失われた場合は絶対座標にフォールバック)
  - `face_attrs` / `edge_attrs` / `vert_attrs` — マテリアルインデックス・スムーズ・
    シーム・シャープ・クリース・ベベルウェイトの変更
  - `deleted_verts` / `deleted_edges` / `deleted_faces` — 削除 (連鎖削除されるものは記録しない)
  - `new_edges` / `new_faces` — 新規トポロジ (頂点 ID 参照)
- **レイヤーツリー**: レイヤーは線形リストではなくツリー (各レイヤーが親レイヤーの UID を参照)。
  ブランチは末端レイヤー (head) へのポインタで、アクティブブランチ = head から根まで
  遡ったパス。分岐点より上流のレイヤーは実体が共有されるため、コピー方式と違って
  上流の修正が全ブランチに自動で波及する。
- **再構築**: 初期化時に退避したベースメッシュのコピーへ、アクティブパスの差分を bmesh で順に適用する。
- **ID の正規化**: subdivide 等で INT 属性が複製されて ID が重複した場合、
  編集前の位置に最も近い 1 頂点だけが ID を保持し、残りは新規頂点として採番し直す。
- **壊れた参照**: 上流の変更で参照先 ID が消えた場合、生成・移動はスキップして警告を
  パネルに表示する。削除系は対象がなければ黙ってスキップする (実害がないため)。
- **未記録編集の検出**: 再構築のたびにメッシュの指紋 (要素数 + 座標の絶対値和 +
  位置依存の重み付き和) を保存し、次の操作時に比較する。検出はセッション内の
  再構築履歴に依存する。
- **シェイプキーロック**: depsgraph ハンドラでスタック運用中のキー追加を検知して
  即時取り消す。セッション中に「キーなし」を確認済みのオブジェクトだけが対象で、
  キーと共存した状態で保存された古いファイルはガード + 警告モードに留める。

### bmesh の罠 (ハマりどころ)

- `bmesh.ops.delete(context='EDGES')` は孤立した頂点まで削除する → `'EDGES_FACES'` を使う
- カスタムデータレイヤーの追加は、その領域の**要素参照とレイヤーハンドルを無効化**する
  → 要素参照を取る前にレイヤーを一括確保し、ID レイヤーはレイヤー適用ごとに取り直す
- Blender 5.x の GPU バックエンドでは固定機能のポイントサイズが効かない
  → オーバーレイ描画は `POINT_UNIFORM_COLOR` シェーダを使う

## テスト

- `tests/test_edit_layers.py` — ヘッドレステスト (119 件)。
  `blender --background --factory-startup --python tests/test_edit_layers.py`
  で実行し、末尾の `RESULT` 行で合否を確認する
- `tools/capture_help_shots.py` — ヘルプ用スクリーンショット撮影 (日本語 UI、UI モードで実行)
- `tools/capture_store_assets.py` — ストア用プレビュー撮影 (英語 UI、16:9 1920x1080)
- `tools/render_icon.py` — ストア用アイコンのレンダリング (ヘッドレス可)

いずれもリポジトリの親フォルダを `sys.path` に足して `import edit_layers` する構成のため、
リポジトリのフォルダ名は `edit_layers` のままにすること。

## 翻訳

UI 文字列は英語がソースで、`__init__.py` 内の `_JA` 辞書に日本語訳を持つ
(`bpy.app.translations` で登録)。文字列を追加・変更したら `_JA` も更新すること。
レポートや f-string 由来の動的文字列は自動翻訳されないため `_T()` ヘルパーを通す。
