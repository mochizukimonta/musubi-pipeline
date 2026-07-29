# 開発に参加する

## 先に読んでほしいこと

このアドオンは**壊れると人の作業が消える**種類のコードです。同期検証・
ロック・バージョン履歴が誤動作すると、失われるのは実際に誰かが数時間かけて
作ったデータです。そのため、機能の多さよりも「壊れないこと」を優先します。

具体的には、[README の「このアドオンが自分に課している約束」](README.md)にある制約を
守ってください。この方針と衝突する変更は、良い機能であっても採れないことが
あります。方向性に迷ったら、コードを書く前に Issue で相談してください。

## 開発環境

Blender は**要りません**。素の Python だけで開発とテストができます。

```bash
git clone https://github.com/mochizukimonta/musubi-pipeline.git
cd musubi-pipeline
python -m pip install pytest
python -m pytest                # Blender 不要。数秒で終わります
```

Python 3.11 以降が必要です(Blender 4.2 が 3.11、Blender 5.x が 3.13 を
同梱しているため、この2つが CI の対象です)。

Blender で実際に動かして確認したいときは、`musubi_pipeline/` フォルダに
入ってから:

```bash
blender --command extension build --output-dir ..
blender --command extension validate ../musubi_pipeline-0.27.0.zip
```

できた zip を Blender のウィンドウにドラッグ&ドロップすればインストールされます。

## いちばん大事な規約: データ層に `bpy` を持ち込まない

コードは3層に分かれています([ARCHITECTURE.md](ARCHITECTURE.md) に詳細)。

```
UI層          ui.py                      ← パネル描画のみ。I/O 禁止
オペレーター層 ops.py st_ops.py ...       ← bpy 依存。ここが bpy を使う場所
データ層      core.py sync.py versions.py ← import bpy しない
```

**データ層のモジュールに `import bpy` を書かないでください。** これは
様式の問題ではなく実利です:

- Blender を起動せずにテストできる。だから CI が素のランナーで回り、
  テストが2秒で終わります
- Blender が起動しない状況でも、システムの Python から同期設定を
  読んで修理できます

例外は `quality.py` だけです(シーンの内容を検査する仕事なので `bpy.data` を
読みます。書き込みと UI はしません)。`security.py` は `bpy` を任意参照に
しており、無い環境でも動きます。

## テストについて

### 書き方

- **ファイルシステムをモックしないでください。** このコードの仕事は
  ファイル操作そのものなので、モックするとテストの意味が薄くなります。
  `tmp_path` の使い捨てフォルダで本物の I/O を検証します
- **端末名を偽装するときは `socket.gethostname` を差し替えます**
  (`conftest.as_host`)。`tasks` / `versions` は
  `from .sync import host_id` と名前で import しているため、
  `sync.host_id` を差し替えても各モジュール内のコピーには効きません
- **Syncthing の REST は `syncthing.api` を偽物に差し替えます。**
  実バイナリ・実通信は不要です

### 直してほしいこと

同期・ロック・バージョン管理に手を入れる変更には、**不変条件のテストを
添えてください。** 「メタデータの sha256 が保存実体の sha256 と一致する」
のような形で、過去の修正の理由をアサートに固定していく方針です。

テストが本当に効いているか確かめるには、わざとバグを入れて赤くなるかを
見てください(ミューテーションテスト)。緑のままなら、そのテストは
何も守っていません。

## バージョンを上げるとき

バージョン番号は2箇所にあります。

- `musubi_pipeline/blender_manifest.toml` の `version`
- `musubi_pipeline/__init__.py` の `bl_info["version"]`

**片方だけ直すと `tests/test_manifest.py` が赤くなります。** 忘れても
CI が止めるので、手順書を覚える必要はありません。

新しい `.py` を追加したら SPDX ヘッダーを付けてください。これも
テストが検査しています。

```python
# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
```

## Issue と Pull Request

- **バグ報告・機能提案**は
  [Issue のテンプレート](https://github.com/mochizukimonta/musubi-pipeline/issues/new/choose)を
  使ってください。Blender のバージョンと OS は必ず書いてください
- **セキュリティ上の問題は Issue に書かないでください。**
  [SECURITY.md](SECURITY.md) の手順に従ってください
- **大きな変更は先に Issue で相談してください。** 設計方針と衝突する
  変更を書いてから却下されるのは、お互いに時間の無駄です
- Pull Request は `main` に向けてください。CI(pytest × Python 3.11 / 3.13)が
  緑になっていることを確認してください

### コミットメッセージ

日本語で構いません。「何を変えたか」より**「なぜ変えたか」**を書いてください。
何を変えたかは差分を見れば分かりますが、理由は書かないと失われます。

### 個人情報を含めないでください

このリポジトリは公開されています。テストデータやドキュメントに、
実際のホスト名・ユーザー名・Syncthing デバイスID・取引先や案件の名前・
ローカルの絶対パスを含めないでください。コミット前に架空のものへ
置き換えてください(例: 端末名は `pc-A` / `pc-B`、パスは
`D:\projects\example`、担当者は `animator-A` のように)。

## ライセンス

Pull Request を送ると、その内容は [GPL-3.0-or-later](LICENSE) で
公開されることに同意したものとみなします。
