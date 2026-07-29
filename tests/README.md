# Musubi Pipeline 自動テストスイート

同期・ロック・バージョン管理は「壊れると人の作業が消える」コードなので、
退行を機械検知するための pytest スイート。**Blender は不要**(bpy 非依存の
`core / sync / versions / tasks / syncthing` を素の Python でテストする)。

## 実行

```powershell
python -m pip install pytest      # 初回のみ
python -m pytest                  # リポジトリ直下で実行(pytest.ini が設定を持つ)
python -m pytest -v               # 各テスト名を表示
python -m pytest tests/test_sync.py::test_lock_blocks_other_host   # 個別実行
```

`pytest.ini` の `pythonpath = .` により、Blender 外でも
`from musubi_pipeline import core` が解決される。`musubi_pipeline/__init__.py` は
bpy を `try/except ModuleNotFoundError` で吸収するので、パッケージ import は
Blender なしで通る。

## 構成

| ファイル | 対象 | 主眼 |
|---|---|---|
| `conftest.py` | 共通フィクスチャ | 使い捨てプロジェクト・端末名偽装・時刻固定 |
| `test_core.py` | パス安全性・命名・構造・atomic_replace | パストラバーサル網羅、置換リトライ |
| `test_sync.py` | ロック・照合リスト・危険ファイル | 他端末ロック、壊れロック、改竄検出 |
| `test_versions.py` | snapshot/restore/prune、prune_all/enforce_size_cap(v0.25) | 重複排除、同一秒衝突、復元前退避、ハッシュ不変条件、容量整理でも各ファイルの最新世代は必ず残る |
| `test_tasks.py` | ステータス・ボード | 遷移検証、3 情報源合成、完了率 |
| `test_syncthing.py` | 純関数 + REST | 招待往復、api() 差し替えで分岐検証(ネット非依存)、ensure_folderのパス相乗り防止、accept_pending(only=)の確認画面バイパス防止 |
| `test_manifest.py` | 配布メタデータ | バージョンが bl_info と manifest で一致、ライセンスが GPL、GPL全文の同梱、全 .py の SPDX ヘッダー |

主要な不変条件はミューテーション(わざとバグを入れて赤くなるか)で
退行検知を確認済み: 同一秒衝突回避・enforce_size_cap の最新世代 protected・
accept_pending の only フィルタ・ensure_folder のパス不一致中止。

## 設計メモ

- **本物のディスク I/O**: このコードの仕事はファイル操作そのものなので、
  FS をモックせず tmp_path の使い捨てフォルダで検証する。
- **端末名の偽装**: `host_id()` は毎回 `socket.gethostname()` を呼ぶので、
  `tasks`/`versions` が名前 import した `host_id` にも効くよう
  `socket.gethostname` を差し替える(`conftest.as_host`)。
- **不変条件テスト**: 「メタの sha256 == 保存実体の sha256」のように、
  過去のレース修正の理由をそのままアサートに固定する。
- **ネットワーク非接触**: Syncthing REST は `syncthing.api` を偽物に差し替え、
  呼ばれた method/path/payload を検査する。実バイナリ・実通信は不要。

## リリース時の注意

`tests/`・`pytest.ini` は**配布 zip に含まれない**。これはフォルダ構成で
保証されている: `blender_manifest.toml` は `musubi_pipeline/` の中にあり、
`blender --command extension build` はそのフォルダだけを梱包する。
`tests/` はその外(リポジトリ直下)にあるので、除外指定を書く必要がない。

CI(`.github/workflows/tests.yml`)が push と Pull Request で
`python -m pytest` を Python 3.11 / 3.13 で実行する。
