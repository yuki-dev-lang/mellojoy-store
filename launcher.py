#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mellojoy-launcher — GitHub Releases ベースの自動アップデート付きランチャー
================================================================================
役割:
  1. 起動時に GitHub の最新リリースを確認
  2. ローカル版より新しければ本体(exe)をダウンロードして差し替え
  3. 本体(mellojoy.exe)を起動（引数はそのまま引き継ぎ）
  * ネット不調・レート制限・いかなる更新失敗でも「現行の本体を必ず起動」する
    （更新のために本体が起動不能になることは無い）。

なぜ別exeなのか:
  実行中のexeは自分自身を上書きできない。ランチャーを本体と分離することで、
  まだ起動していない mellojoy.exe を安全に差し替えてから起動できる。

ビルド:
  pyinstaller --onefile --name mellojoy-launcher launcher.py
配布:
  mellojoy-launcher.exe をユーザーに渡す。初回だけ本体が無ければ自動DLする。

★ 配布前に下の CONFIG を自分のリポジトリに合わせること。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG — 配布前にここを合わせる
# ══════════════════════════════════════════════════════════════════════════════
GITHUB_OWNER = "yuki-dev-lang"      # ← リリースを置く owner
GITHUB_REPO  = "mellojoy-store"     # ← リリースを置く repo 名（public）
# ★ 配布は ZIP 一括方式を推奨:
#   リリースに mellojoy_app.zip を添付すると、launcher が丸ごとDL→展開して
#   mellojoy.exe / fire_engine.exe / libcurl.dll など全ファイルを一括更新する。
#   （ASSET_NAME が .exe なら従来通りその1ファイルだけ更新）
ASSET_NAME   = "mellojoy_app.zip"   # リリースに添付する資産名（完全一致）。.zip 推奨 / .exe も可
TARGET_EXE   = "mellojoy.exe"       # 展開後に起動する本体ファイル名

# private リポジトリのときだけ使用。配布exeには絶対に埋め込まない（環境変数で渡す）。
GITHUB_TOKEN = os.environ.get("MJ_GH_TOKEN", "").strip()

VERSION_FILE   = "version.txt"      # ローカルの現在版を記録するファイル
UPDATE_TIMEOUT = 10                 # 更新チェック/DLの各HTTPタイムアウト秒
SKIP_UPDATE_ENV = "MJ_NO_UPDATE"    # この環境変数が "1" なら更新をスキップして即起動

API_BASE = "https://api.github.com"
UA = "mellojoy-launcher"

# ══════════════════════════════════════════════════════════════════════════════
# パス解決（PyInstaller onefile でも実行ファイルの隣を基準にする）
# ══════════════════════════════════════════════════════════════════════════════
def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR = _app_dir()


def _p(name: str) -> str:
    return os.path.join(APP_DIR, name)


def _log(msg: str) -> None:
    print(f"[launcher] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# バージョン比較（"v3.0.1" / "3.0.1" / "3.0" を許容。非semverは文字列不一致で更新扱い）
# ══════════════════════════════════════════════════════════════════════════════
def _ver_tuple(s: str):
    s = (s or "").strip().lstrip("vV").strip()
    if not s:
        return None
    nums = []
    for part in s.replace("-", ".").replace("_", ".").split("."):
        if part.isdigit():
            nums.append(int(part))
        else:
            # 数字先頭だけ拾う（例 "3rc" → 3）。拾えなければ中断。
            d = ""
            for ch in part:
                if ch.isdigit():
                    d += ch
                else:
                    break
            if d:
                nums.append(int(d))
            break
    return tuple(nums) if nums else None


def _is_newer(latest: str, local: str) -> bool:
    lt, lv = _ver_tuple(latest), _ver_tuple(local)
    if lt is None:
        return False                 # 最新版のタグが読めない → 更新しない（安全側）
    if lv is None:
        return True                  # ローカル版不明 → 更新する
    # 長さを揃えて比較
    n = max(len(lt), len(lv))
    lt += (0,) * (n - len(lt))
    lv += (0,) * (n - len(lv))
    return lt > lv


def local_version() -> str:
    try:
        with open(_p(VERSION_FILE), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _write_version(tag: str) -> None:
    try:
        with open(_p(VERSION_FILE), "w", encoding="utf-8") as f:
            f.write((tag or "").strip())
    except Exception as e:
        _log(f"version.txt 書き込み失敗（無視）: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# GitHub API
# ══════════════════════════════════════════════════════════════════════════════
def _req(url: str, accept: str = "application/vnd.github+json"):
    headers = {"User-Agent": UA, "Accept": accept}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return urllib.request.Request(url, headers=headers)


def latest_release():
    """最新リリースの (tag, asset_dict) を返す。asset_dict は ASSET_NAME に一致する資産。
       取得できなければ (None, None)。"""
    url = f"{API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    try:
        with urllib.request.urlopen(_req(url), timeout=UPDATE_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _log(f"リリース取得 HTTP {e.code}（更新チェックをスキップ）")
        return None, None
    except Exception as e:
        _log(f"リリース取得失敗（オフライン等, 更新チェックをスキップ）: {e}")
        return None, None
    tag = data.get("tag_name") or data.get("name") or ""
    asset = None
    for a in (data.get("assets") or []):
        if a.get("name") == ASSET_NAME:
            asset = a
            break
    return tag, asset


def _download_asset(asset: dict, dest: str) -> bool:
    """資産をダウンロード。private は asset API(url)+octet-stream、public は browser_download_url。"""
    if GITHUB_TOKEN and asset.get("url"):
        url, accept = asset["url"], "application/octet-stream"
    else:
        url, accept = asset.get("browser_download_url"), "application/octet-stream"
    if not url:
        _log("ダウンロードURLが見つからない")
        return False
    try:
        with urllib.request.urlopen(_req(url, accept=accept), timeout=UPDATE_TIMEOUT * 6) as r, \
                open(dest, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            last = 0.0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                now = time.time()
                if total and (now - last) > 0.3:
                    pct = done * 100 // total
                    print(f"\r[launcher] ダウンロード {pct:3d}%  "
                          f"({done//1024}/{total//1024} KiB)", end="", flush=True)
                    last = now
        if total:
            print()
        return True
    except Exception as e:
        print()
        _log(f"ダウンロード失敗: {e}")
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        return False


# ══════════════════════════════════════════════════════════════════════════════
# 更新適用
# ══════════════════════════════════════════════════════════════════════════════
def _self_exe_name() -> str:
    """このランチャー自身の実行ファイル名（展開時に自分を上書きしないため）。"""
    if getattr(sys, "frozen", False):
        return os.path.basename(sys.executable).lower()
    return os.path.basename(__file__).lower()


def _safe_replace(dest: str, src_tmp: str) -> None:
    """dest を src_tmp で原子的に置換（既存は .bak 退避）。exe/dll はこれで安全に入替。"""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest):
        bak = dest + ".bak"
        try:
            if os.path.exists(bak):
                os.remove(bak)
        except Exception:
            pass
        try:
            os.replace(dest, bak)
        except Exception:
            pass
    os.replace(src_tmp, dest)


def _apply_zip(zip_path: str, tag: str) -> bool:
    """ZIP を APP_DIR に展開して全ファイルを一括更新（配布用）。
       ランチャー自身と version.txt は上書きしない。パストラバーサルは拒否。"""
    try:
        base_abs = os.path.abspath(APP_DIR)
        self_exe = _self_exe_name()
        with zipfile.ZipFile(zip_path) as zf:
            members = [m for m in zf.namelist() if not m.endswith("/")]
            # 事前にパス安全性を検査
            for n in members:
                dest = os.path.abspath(os.path.join(APP_DIR, n))
                if dest != base_abs and not dest.startswith(base_abs + os.sep):
                    _log(f"不正なパスを含むZIP → 中止: {n}")
                    return False
            applied = 0
            for n in members:
                base = os.path.basename(n).lower()
                if base == self_exe or base == VERSION_FILE.lower():
                    continue                       # 自分自身と版ファイルは触らない
                dest = os.path.join(APP_DIR, n)
                # 一時ファイルに書き出してから原子的に置換
                tfd, tpath = tempfile.mkstemp(prefix="mj_up_", suffix=".part", dir=APP_DIR)
                try:
                    with os.fdopen(tfd, "wb") as out, zf.open(n) as src:
                        while True:
                            chunk = src.read(65536)
                            if not chunk:
                                break
                            out.write(chunk)
                    _safe_replace(dest, tpath)
                    applied += 1
                except Exception as e:
                    _log(f"展開失敗 {n}: {e}")
                    try: os.remove(tpath)
                    except Exception: pass
                    return False
        _write_version(tag)
        _log(f"更新完了（{applied}ファイル展開）→ {tag}")
        return True
    except zipfile.BadZipFile:
        _log("ZIP が壊れています → 更新中止")
        return False
    except Exception as e:
        _log(f"ZIP展開失敗: {e}")
        return False
    finally:
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception:
            pass


def _apply_single(asset: dict, tag: str, tmp_path: str) -> bool:
    """単一ファイル(.exe)を差し替え（旧版は .bak に退避）。"""
    target = _p(TARGET_EXE)
    try:
        if os.path.getsize(tmp_path) < 1024:
            _log("ダウンロードファイルが小さすぎる → 破棄")
            os.remove(tmp_path)
            return False
    except Exception:
        pass
    try:
        _safe_replace(target, tmp_path)
        _write_version(tag)
        _log(f"更新完了 → {tag}")
        return True
    except Exception as e:
        _log(f"差し替え失敗: {e}")
        try:
            if not os.path.exists(target) and os.path.exists(target + ".bak"):
                os.replace(target + ".bak", target)
        except Exception:
            pass
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def _apply_update(asset: dict, tag: str) -> bool:
    """資産を一時ファイルにDL→ .zip なら一括展開 / .exe なら単一差し替え。成功で True。"""
    suffix = ".zip" if (asset.get("name", "").lower().endswith(".zip")) else ".part"
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="mj_update_", suffix=suffix, dir=APP_DIR)
    os.close(tmp_fd)
    if not _download_asset(asset, tmp_path):
        try: os.remove(tmp_path)
        except Exception: pass
        return False
    if asset.get("name", "").lower().endswith(".zip"):
        return _apply_zip(tmp_path, tag)
    return _apply_single(asset, tag, tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# ライセンス認証フック（Whop 用プレースホルダ）
#   ★ 現状: キー未発行のため常に許可（True）。
#   ★ Whop 準備後にここで license_client を呼ぶだけで有効化できるよう分離してある。
# ══════════════════════════════════════════════════════════════════════════════
def check_license() -> bool:
    # TODO(Whop): license_client.verify(APP_DIR) を呼ぶ。失敗したら False を返して起動を止める。
    return True


# ══════════════════════════════════════════════════════════════════════════════
# 本体起動
# ══════════════════════════════════════════════════════════════════════════════
def _launch_target(passthrough_args) -> int:
    target = _p(TARGET_EXE)
    if not os.path.exists(target):
        _log(f"本体が見つかりません: {target}")
        _log("初回はリリースから自動DLされます。更新に失敗した場合はネット接続を確認してください。")
        return 2
    try:
        # 本体の作業ディレクトリは本体の場所に合わせる
        proc = subprocess.Popen([target, *passthrough_args], cwd=APP_DIR)
        proc.wait()
        return proc.returncode or 0
    except Exception as e:
        _log(f"本体の起動に失敗: {e}")
        return 3


# ══════════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    args = sys.argv[1:]

    if not check_license():
        _log("ライセンス認証に失敗しました。起動を中止します。")
        return 1

    skip = os.environ.get(SKIP_UPDATE_ENV, "") == "1" or "--no-update" in args
    if "--no-update" in args:
        args = [a for a in args if a != "--no-update"]

    if GITHUB_OWNER == "YOUR_GH_OWNER":
        _log("CONFIG 未設定（GITHUB_OWNER/REPO）。更新チェックをスキップして起動します。")
        skip = True

    if not skip:
        _log("更新を確認中...")
        tag, asset = latest_release()
        if tag:
            cur = local_version()
            if _is_newer(tag, cur):
                if asset:
                    _log(f"新バージョン {tag}（現在: {cur or '不明'}）→ 更新します")
                    if not _apply_update(asset, tag):
                        _log("更新に失敗 → 現行バージョンで起動します")
                else:
                    _log(f"新バージョン {tag} はあるが資産 '{ASSET_NAME}' が見つからない → 現行で起動")
            else:
                _log(f"最新です（{cur or tag}）")
        # tag が None（オフライン等）のときは黙って現行起動
    else:
        _log("更新スキップ指定 → そのまま起動")

    return _launch_target(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
