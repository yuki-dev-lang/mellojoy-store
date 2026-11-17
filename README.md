# mellojoy 配布・自動アップデート手順（GitHub Releases）

## 構成
- **mellojoy-launcher.exe** … ユーザーに渡す起動用。起動時に最新版を確認し、本体を自動更新してから起動する。
- **mellojoy.exe** … 本体。GitHub Releases の資産として配置し、ランチャーが自動DLする。
- **version.txt** … ランチャーが作る「現在入っている版」の記録（手で触らない）。

```
配布フォルダ/
├─ mellojoy-launcher.exe   ← これを実行する
├─ mellojoy.exe            ← 初回DL後に出来る（手動で置いてもよい）
└─ version.txt             ← 自動生成
```

## 1. 配布用リポジトリを用意
GitHub に配布専用リポジトリを作る（本プロジェクトでは `yuki-dev-lang/mellojoy-store`）。
- **public 推奨**（ランチャーがトークン無しで更新DLできる。レート制限60回/時で十分）。
- private にする場合は、ユーザー側で環境変数 `MJ_GH_TOKEN` にアクセストークンが必要（配布exeにトークンを埋め込まないこと）。

## 2. launcher.py の CONFIG を合わせる
`launcher.py` 冒頭の CONFIG を自分のリポジトリに：
```python
GITHUB_OWNER = "yuki-dev-lang"     # ← owner（確定済み）
GITHUB_REPO  = "mellojoy-store"    # ← 配布リポジトリ名（確定済み・public）
ASSET_NAME   = "mellojoy_app.zip"  # ← リリースに添付する配布ZIP名
TARGET_EXE   = "mellojoy.exe"
```
> ※ 上記は `yuki-dev-lang/mellojoy-store` 用に設定済みです。repo を変える場合はここを書き換えてください。

## 3. ビルド（配布は ZIP 一括方式を推奨）
本体は fire_engine.exe / libcurl.dll など**複数ファイル**を必要とするため、
それらを**まとめて ZIP**にして配布・更新します（全部が揃った状態で原子的に更新できる）。

### 推奨: `build_release.bat` で一発ビルド
`mellojoy.py / launcher.py / fire_engine.exe / libcurl.dll / redcap_squishy.ico` と
**同じフォルダ**に `build_release.bat` を置いてダブルクリックするだけで、
アイコン付きexeのビルド → `mellojoy_app.zip` 作成まで自動で行います。

### 手動でやる場合
```bat
:: 本体とランチャーをビルド（アイコン付き）
pyinstaller --onefile --name mellojoy          --icon redcap_squishy.ico mellojoy.py
pyinstaller --onefile --name mellojoy-launcher --icon redcap_squishy.ico launcher.py

:: 配布 ZIP を作る（launcher は入れない！ 中身=実行時に必要な全ファイル）
mkdir app
copy dist\mellojoy.exe   app\
copy fire_engine.exe     app\
copy libcurl.dll         app\
powershell Compress-Archive -Path app\* -DestinationPath mellojoy_app.zip -Force
```

> `mellojoy_app.zip` の中身がそのまま各ユーザーのフォルダに展開されます。
> **ランチャー自身(mellojoy-launcher.exe)は ZIP に入れない**（自分を上書きしないため）。
> **秘密ファイル（accounts.txt / proxies.txt / capsolverキー / config.json）は入れない**。
> **入力ファイル（accounts/proxies/names/emails.txt）も入れない**（初回起動で空テンプレ自動生成）。

### 依存DLLについて（確認済み）
- `fire_engine.exe` … 依存は KERNEL32 + UCRT(api-ms-win-crt-*) のみ。**Win10/11標準**で追加不要。
  Visual C++ 再頒布可能パッケージも不要。
- `libcurl.dll` … 依存は Normaliz / CRYPT32 / WS2_32 / IPHLPAPI / KERNEL32 の標準DLLのみ。自己完結。
- → 配布ZIPは **mellojoy.exe + fire_engine.exe + libcurl.dll の3点だけ**で動く。

## 4. リリースを作成（＝アップデート公開）
1. GitHub の `mellojoy-store` → Releases → Draft a new release
2. タグは **semver**: `v3.0.0` → 次は `v3.0.1` / `v3.1.0`（ランチャーが数字を比較）
3. **`mellojoy_app.zip` を資産として添付**（名前は launcher の `ASSET_NAME` と完全一致）
4. Publish

次回ユーザーが `mellojoy-launcher.exe` を起動すると、`mellojoy_app.zip` を丸ごとDL→展開し、
**mellojoy.exe / fire_engine.exe / libcurl.dll などを一括更新**してから起動する。

> 単一exeだけ更新したい場合は `ASSET_NAME="mellojoy.exe"` にして .exe を添付すればOK（従来方式）。

## 挙動の要点
- **更新失敗（オフライン/レート制限/DL失敗）でも、現行の本体を必ず起動**する（起動不能にならない）。
- 旧版は `mellojoy.exe.bak` に退避（差し替え失敗時は自動で復帰）。
- 更新を一時的に止めたい: `mellojoy-launcher.exe --no-update` もしくは環境変数 `MJ_NO_UPDATE=1`。
- 本体へ渡した引数はそのまま本体に引き継がれる。

## バージョン比較の仕様
- `v3.0.1` / `3.0.1` / `3.0` を許容。欠けた桁は 0 補完して数値比較。
- タグが semver でない場合は安全側（最新タグが読めないときは更新しない）。

## Whop ライセンス認証（保留中）
- 現状キー未発行のため、`launcher.py` の `check_license()` は常に許可（True）。
- Whop の product/API キーが用意でき次第、`check_license()` に `license_client` 呼び出しを追加するだけで有効化できるよう分離済み。
