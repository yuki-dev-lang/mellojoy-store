#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MelloJoy BOT v2.0  ─  CLI Edition
mellojoylucky.cn  Ticket Lottery  Auto-Registration & FCFS Booking Tool

Files:
  accounts.txt   email:password  (one per line)
  proxies.txt    proxy URLs      (one per line, empty = direct)
  success.txt    booking results (appended on success)
  names.txt      氏名:性別       (e.g. 田中 太郎:男)
  emails.txt     email list      (one per line, optional)
  mellojoy_config.json  config
"""

from __future__ import annotations

import base64
import datetime
import email as email_lib
import imaplib
import itertools
import json
import logging
import os
import pathlib
import queue as _queue
import random
import re
import secrets
import string
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _CFTimeout
from typing import Optional

# ── BASE path (must be computed before Playwright is imported) ───────────────
# When running as a PyInstaller onedir EXE sys.frozen is True and
# sys.executable points to mellojoy.exe; __file__ is unreliable there.
if getattr(sys, 'frozen', False):
    BASE = pathlib.Path(sys.executable).parent
else:
    BASE = pathlib.Path(__file__).parent

# Point Playwright at pw-browsers/ next to the EXE (or next to the .py in dev)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BASE / "pw-browsers"))

# ── third-party (graceful degradation) ──────────────────────────────────────
try:
    from curl_cffi import requests as cf_requests
    _HAS_CURL = True
except ImportError:
    import requests as cf_requests
    _HAS_CURL = False

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _HAS_PW = True
except ImportError:
    _HAS_PW = False

try:
    from colorama import Fore, Style, init as _cinit
    _cinit(autoreset=True)
    _HAS_COLOR = True
except ImportError:
    class _Fore:
        RED = YELLOW = GREEN = CYAN = MAGENTA = BLUE = WHITE = RESET = ""
    class _Style:
        BRIGHT = DIM = RESET_ALL = ""
    Fore = _Fore()
    Style = _Style()
    _HAS_COLOR = False

import requests as _req   # YesCaptcha HTTP

# ── paths ────────────────────────────────────────────────────────────────────
# BASE is set near the top of this file (before Playwright import)
ACCOUNTS_F = BASE / "accounts.txt"
CREATED_F  = BASE / "created.txt"         # permanent log of all created accounts
PROXIES_F  = BASE / "proxies.txt"
SUCCESS_F  = BASE / "success.txt"
NAMES_F    = BASE / "names.txt"
EMAILS_F   = BASE / "emails.txt"
CFG_F      = BASE / "mellojoy_config.json"
LOG_F      = BASE / "mellojoy.log"
LOGS_DIR   = BASE / "logs"               # セッション別ログ（Fix 45）
USED_PFX_F = BASE / "used_prefixes.txt"   # catch-all dedup
DEBUG_DIR   = BASE / "debug"              # HTML + HAR snapshots (debug_mode)
WEBHOOKS_F       = BASE / "webhooks.txt"         # Webhook URLs (one per line, # = comment)
ACCOUNT_RESULT_F = BASE / "accountresult.txt"   # Result mode input  (same format as success.txt)
RESULT_DIR       = BASE / "result"              # Result mode CSV output folder


# ── 初回起動時のテンプレ自動生成 ───────────────────────────────────────────────
# 配布ZIP には秘密ファイル（accounts/proxies/names/emails）を含めないため、
# 初回起動で「無ければ」空テンプレ（記入例つきコメント）を自動生成する。
# ★ 既に存在するファイルは絶対に上書きしない（ユーザーのデータを潰さない）。
_DATA_FILE_TEMPLATES: dict = {
    "accounts.txt": (
        "# accounts.txt — ログイン用アカウント（1行1件）\n"
        "# 形式: email:password            （最小）\n"
        "#       email:password:氏名        （氏名つき）\n"
        "#       email:password:氏名:時間   （希望枠の開始時刻を前方一致で指定 例 13:00）\n"
        "# 例) taro@example.com:Passw0rd:田中 太郎:13:00\n"
        "# （# で始まる行と空行は無視されます）\n"
    ),
    "proxies.txt": (
        "# proxies.txt — プロキシ（1行1件 / 空ファイル or 全コメントなら direct 接続）\n"
        "# 形式: http://user:pass@host:port   または  host:port:user:pass\n"
        "# 例) http://user:pass@123.45.67.89:8080\n"
        "# （# で始まる行と空行は無視されます）\n"
    ),
    "names.txt": (
        "# names.txt — アカウント作成時に使う氏名リスト（1行1件 / 任意）\n"
        "# 形式: 氏名:性別            例) 田中 太郎:男\n"
        "#       氏名:性別:年齢       例) 山田 花子:女:28\n"
        "# （# で始まる行と空行は無視されます）\n"
    ),
    "emails.txt": (
        "# emails.txt — アカウント作成のメール（list モード時に使用 / 1行1件 / 任意）\n"
        "# 形式: 1行に1メールアドレス   例) user001@example.com\n"
        "# （catch-all モードでは不要。# で始まる行と空行は無視されます）\n"
    ),
    "accountresult.txt": (
        "# accountresult.txt — リザルトモード[3]の入力（1行1件）\n"
        "# 各アカウントにログインして実際の予約時間を取得し CSV 化します。\n"
        "# 形式: email:password            （最小）\n"
        "#       email:password:氏名        （氏名つき）\n"
        "# 例) taro@example.com:Passw0rd:田中 太郎\n"
        "# （# で始まる行と空行は無視されます）\n"
    ),
}


def _ensure_data_files() -> list[str]:
    """accounts/proxies/names/emails の各txtが無ければ空テンプレを生成する。
    返り値: 新規作成したファイル名の一覧（起動時に案内表示するため）。"""
    created: list[str] = []
    for _fname, _tmpl in _DATA_FILE_TEMPLATES.items():
        _p = BASE / _fname
        try:
            if not _p.exists():
                _p.write_text(_tmpl, encoding="utf-8")
                created.append(_fname)
        except Exception:
            # 生成失敗（権限等）は致命的ではない。ローダ側で「無い」として扱われるだけ。
            pass
    return created


def _set_taskbar_icon() -> None:
    """起動中のコンソール窓のアイコンを、この exe に埋め込まれたアイコンに設定する。
    PyInstaller の --icon はファイルアイコンを設定するが、タスクバー（実行中の窓）には
    反映されないことがあるため、WM_SETICON で明示設定する。Windows 以外・失敗時は無視。
    ★ 配布ZIPに .ico を同梱しなくても、exe 自身に埋め込まれたアイコンを使う。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        # タスクバーで python/conhost とグルーピングされないよう独自AppIDを付与
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NigronWorks.MelloJoy.Bot")
        except Exception:
            pass
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return
        # この実行ファイル(exe)に埋め込まれた先頭アイコンを取り出す
        exe = sys.executable
        big = ctypes.c_void_p()
        small = ctypes.c_void_p()
        n = ctypes.windll.shell32.ExtractIconExW(exe, 0, ctypes.byref(big),
                                                 ctypes.byref(small), 1)
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        if n and big and big.value:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        if n and small and small.value:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
    except Exception:
        pass


# ── SSL エラー識別文字列（モジュールレベル定数）────────────────────────────────
# MelloJoyAPI._post() と .book() で共有。定義を一か所に集約。
_SSL_ERROR_STRS: tuple[str, ...] = (
    "BoringSSL", "SSL_connect", "SSL_ERROR_SYSCALL",
    "SSLError", "Connection closed abruptly", "ssl",
)

# ── Android device profiles ──────────────────────────────────────────────────
# (user_agent, width, height, device_pixel_ratio)
DEVICES: dict[str, tuple[str, int, int, int]] = {
    "pixel7":      ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36", 412, 915, 3),
    "pixel8":      ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36", 412, 915, 3),
    "pixel8pro":   ("Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36", 412, 900, 3),
    "pixel9":      ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36", 412, 915, 3),
    "pixel9pro":   ("Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36", 412, 900, 3),
    "galaxy_s23":  ("Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36", 360, 780, 3),
    "galaxy_s24":  ("Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36", 360, 780, 3),
    "galaxy_s25":  ("Mozilla/5.0 (Linux; Android 15; SM-S931B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36", 360, 780, 3),
    "xperia_1v":   ("Mozilla/5.0 (Linux; Android 13; XQ-DQ44) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36", 360, 780, 3),
    "aquos_r8pro": ("Mozilla/5.0 (Linux; Android 13; SH-51D) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36", 360, 780, 3),
    "arrows_we2":  ("Mozilla/5.0 (Linux; Android 13; FCG01) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36", 360, 780, 2),
}
DEVICE_LABELS: dict[str, str] = {
    "pixel7":      "Pixel 7 (Android 13)",
    "pixel8":      "Pixel 8 (Android 14)",
    "pixel8pro":   "Pixel 8 Pro (Android 14)",
    "pixel9":      "Pixel 9 (Android 15)",
    "pixel9pro":   "Pixel 9 Pro (Android 15)",
    "galaxy_s23":  "Galaxy S23 (SM-S911B)",
    "galaxy_s24":  "Galaxy S24 (SM-S921B)",
    "galaxy_s25":  "Galaxy S25 (SM-S931B)",
    "xperia_1v":   "Xperia 1 V (XQ-DQ44)",
    "aquos_r8pro": "AQUOS R8 Pro (SH-51D)",
    "arrows_we2":  "arrows We2 (FCG01)",
}

# ★ Fix 36: UA の Chrome バージョンに合わせた sec-ch-ua / sec-ch-ua-mobile ヘッダーを生成。
# Playwright の実際の Chromium バージョン（例: 155）が自動挿入されると UA と食い違い、
# Cloudflare のフィンガープリント検査で矛盾を検出されるため、UA から Chrome バージョンを
# 抽出して extra_http_headers で上書きする。
# sec-ch-ua-mobile は Mobile Safari を含む UA なら ?1 (モバイル) で統一。
import re as _re_ua

def _ua_ch_headers(ua: str) -> dict[str, str]:
    """UA 文字列から sec-ch-ua 系ヘッダーと Accept ヘッダーを生成する。

    ★ Fix 36: sec-ch-ua のバージョンを UA の Chrome バージョンに統一
    ★ Fix 37: Accept ヘッダーも UA バージョンに合わせ image/jxl 矛盾を解消
              Chromium 155 が自動挿入する image/jxl は Chrome 126 以降に追加された
              フォーマットのため、それ以前の UA バージョンと矛盾する。
    ★ Fix 40: extra_http_headers では Chromium の Accept を上書きできないため
              このヘッダーは sec-ch-ua 系のみに留める。Accept の強制上書きは
              page.route() で行う (_install_accept_route を参照)。
    """
    m = _re_ua.search(r'Chrome/(\d+)', ua)
    ver_int = int(m.group(1)) if m else 116
    ver = str(ver_int)
    is_mobile = "Mobile" in ua
    # "Not:A-Brand" のダミーブランドはバージョン不問で 99 固定
    sec_ch_ua = f'"Google Chrome";v="{ver}", "Chromium";v="{ver}", "Not:A-Brand";v="99"'
    # image/jxl は Chrome 126 以降。UA バージョンが 125 以下なら除外
    if ver_int >= 126:
        accept = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        )
    else:
        accept = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        )
    return {
        "sec-ch-ua":          sec_ch_ua,
        "sec-ch-ua-mobile":   "?1" if is_mobile else "?0",
        "sec-ch-ua-platform": '"Android"',
        "Accept":             accept,           # ★ Fix 37
    }

# ★ Fix 72: UA / Chrome バージョン整合性ヘルパー ─────────────────────────────
# 方針:
#   - UA は Chrome 110 以降の実機と同じ「縮小形式」（Android 10; K）にする。
#     実機 Chrome は端末名・実際の Android バージョンを UA に入れない。
#   - ブラウザは実エンジンのメジャーバージョンをそのまま名乗る（古い版を名乗らない）。
#   - HTTP（curl_cffi）は Android 実機の TLS を持つ chrome131_android を使い、UA も 131 に合わせる。
#   - ブラウザで取った cf_clearance を HTTP に流用するときは、そのブラウザの UA に HTTP 側を合わせる。
_HTTP_CHROME_VER = 131

def _reduced_android_ua(major: int) -> str:
    return (f"Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{int(major)}.0.0.0 Mobile Safari/537.36")

def _ua_major(ua: str, default: int = _HTTP_CHROME_VER) -> int:
    m = _re_ua.search(r'Chrome/(\d+)', ua or "")
    return int(m.group(1)) if m else default

def _sec_ch_ua_for(major: int, brand: Optional[str] = "Google Chrome") -> str:
    """Chromium の GREASE ブランド生成アルゴリズムを再現した sec-ch-ua を返す。
    （Chrome 131 → "Google Chrome", "Chromium", "Not_A Brand";v="24" /
      Chrome 141 → "Google Chrome", "Not?A_Brand";v="8", "Chromium" と実機一致を確認済み）"""
    chars = [" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_"]
    vers  = ["8", "99", "24"]
    seed  = int(major)
    grease = (f"Not{chars[seed % 11]}A{chars[(seed + 1) % 11]}Brand", vers[seed % 3])
    chromium = ("Chromium", str(seed))
    if brand:
        orders = [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)]
        o = orders[seed % 6]
        lst = [None, None, None]
        lst[o[0]], lst[o[1]], lst[o[2]] = grease, chromium, (brand, str(seed))
    else:
        o = [(0, 1), (1, 0)][seed % 2]
        lst = [None, None]
        lst[o[0]], lst[o[1]] = grease, chromium
    return ", ".join(f'"{b}";v="{v}"' for b, v in lst)

def _curl_supported_targets() -> set:
    try:
        from curl_cffi.requests.impersonate import BrowserType as _BT
        return {b.value for b in _BT}
    except Exception:
        return {"chrome116", "chrome120", "chrome124", "chrome131", "chrome131_android"}

def _pick_imp_target(major: int, android: bool = True) -> str:
    """curl_cffi が実際に対応しているターゲットから最適なものを選ぶ。
    ★ Fix 72: 旧実装は存在しない chrome128/130/134 を指定し、最終的に素の requests
              （Python の TLS 指紋）に落ちていた。"""
    sup = _curl_supported_targets()
    if android and f"chrome{major}_android" in sup:
        return f"chrome{major}_android"
    desk = sorted(int(t[6:]) for t in sup if re.fullmatch(r"chrome\d+", t))
    if not desk:
        return "chrome131_android" if "chrome131_android" in sup else "chrome"
    le = [v for v in desk if v <= major]
    return f"chrome{(le[-1] if le else desk[0])}"

def _device_model_info(dev_key: Optional[str]) -> tuple[str, str]:
    """DEVICES の UA から (端末モデル名, Androidバージョン) を取り出す（getHighEntropyValues 用）。"""
    try:
        _ua0 = DEVICES[dev_key][0]
        m = _re_ua.search(r"Android (\d+); ([^)]+)\)", _ua0)
        if m:
            return m.group(2).strip(), f"{m.group(1)}.0.0"
    except Exception:
        pass
    return "", "10.0.0"

# ── geolocation presets ──────────────────────────────────────────────────────
_GEO_KANTO = [
    (35.6812, 139.7671, "東京（皇居）"),
    (35.6580, 139.7016, "渋谷"),
    (35.6938, 139.7034, "新宿"),
    (35.7281, 139.7183, "池袋"),
    (35.6642, 139.7321, "品川"),
    (35.7090, 139.7327, "秋葉原"),
]
_GEO_KANSAI = [
    (34.6937, 135.5023, "大阪（難波）"),
    (34.7024, 135.4959, "大阪（梅田）"),
    (35.0116, 135.7681, "京都"),
    (34.6913, 135.1956, "神戸"),
    (34.6851, 135.8325, "奈良"),
    (34.7278, 135.5032, "吹田"),
]
GEO_LABELS: dict[str, str] = {
    "kanto":          "関東（東京・固定）",
    "kanto_random":   "関東（ランダム）",
    "kansai":         "関西（大阪・固定）",
    "kansai_random":  "関西（ランダム）",
    "tokyo":          "東京",
    "osaka":          "大阪",
    "kyoto":          "京都",
    "kobe":           "神戸",
    "yokohama":       "横浜",
    "random":         "完全ランダム（関東・関西）",
}

def _resolve_geo(key: str) -> tuple[float, float]:
    k = (key or "kanto_random").lower()
    if k in ("kanto", "tokyo"):     return _GEO_KANTO[0][:2]
    if k == "yokohama":             return (35.4437, 139.6380)
    if k == "kanto_random":
        p = random.choice(_GEO_KANTO); return p[0], p[1]
    if k in ("kansai", "osaka"):    return _GEO_KANSAI[0][:2]
    if k == "kyoto":                return (35.0116, 135.7681)
    if k == "kobe":                 return (34.6913, 135.1956)
    if k == "kansai_random":
        p = random.choice(_GEO_KANSAI); return p[0], p[1]
    p = random.choice(_GEO_KANTO + _GEO_KANSAI); return p[0], p[1]

# ── default config ───────────────────────────────────────────────────────────
DEF_CFG: dict = {
    # ── Captcha API ───────────────────────────────────────────────────────────
    "captcha_enabled":  True,           # False にすると API solve をスキップ（ブラウザのみ）
    "captcha_provider": "yescaptcha",   # yescaptcha | 2captcha | capsolver | anticaptcha
    "captcha_key":      "",             # 選択プロバイダの API キー
    # (後方互換) yescaptcha_key が設定済みの場合、captcha_key が空なら自動引継ぎ
    "yescaptcha_key":   "",
    # ★ Fix 48: フォールバックチェーン。主 API 失敗時に順番に試行する
    # 例: [{"provider": "capsolver", "key": "cs-xxx"}, {"provider": "2captcha", "key": "2c-yyy"}]
    "captcha_fallback": [],
    # ── FCFS / general ────────────────────────────────────────────────────────
    "parallel":         3,
    "monitor_delay":    0.5,  # ★ Fix 60: 15s → 0.5s (スロット監視間隔を大幅短縮)
    "retry_delay":      3,    # 5s → 3s (proxy rotate後の待機を短縮)
    "devices":          ["pixel7", "pixel8", "galaxy_s24"],
    "device_mode":      "random",
    "geo":              "kanto_random",
    "browser":          "chromium",
    "base_url":         "https://mellojoylucky.cn",
    "cf_sitekey":       "0x4AAAAAAFF0MPh5SYT7ywAo",
    # IMAP
    "imap_host":        "imap.gmail.com",
    "imap_port":        993,
    "imap_user":        "",
    "imap_pass":        "",
    "imap_code_length": 6,              # 認証コードの桁数（6以外のサイトは変更）
    "imap_from_hint":   "mellojoy",     # From絞り/判定の語。Gmail検索は単語単位なので
                                        #   部分語 "mello" は不可。localパート共通の "mellojoy" を使う
    # Account creator
    "email_mode":       "catchall",     # "catchall" | "list"
    "catchall_domain":  "",             # e.g.  example.com
    "password_template": "Mello@{r6}", # {r6} = 6 random chars
    "age_min":          20,
    "age_max":          40,
    "register_count":   1,
    "register_delay":   10,             # seconds between accounts
    "register_attempts": 2,             # register POST の最大試行回数（新トークンで再試行）
    "send_code_attempts": 4,            # ★ Fix 73: send-code の最大試行回数（理由別にトークン/プロキシを替えて再試行）
    "fire_detect_timeout_ms": 1200,     # ★ 検知GETの打ち切り時間。大きいと時刻優先の盲撃ちが
                                        #   進行中GETに阻まれ発火が遅延する（過負荷時に顕著）。
    "solve_timeout_sec": 120,           # ★ 1個のTurnstileソルブ待ち上限(秒)。ソルバー遅延時に
                                        #   波全体が引きずられるのを防ぐ。先着はTTL50s制約があるため
                                        #   フォールバック(captcha_fallback)併用時は40〜45推奨。
    "captcha_fallback": [],             # 例: [{"provider":"yescaptcha","key":"..."}] 主が遅い日の保険
    "captcha_keys": [],                 # 同一プロバイダの追加キー ["key2","key3"]。presolveを全キーに分散
    "yescaptcha_proxyless": True,       # True → always TurnstileTaskProxyless (recommended)
    "presolve_enabled": True,           # ★ Fix 55: False → JITモード（POST直前にTurnstileをsolve）
                                        #   Trueはスロット開放前に事前取得（高速・デフォルト）
                                        #   Falseはスロット検知後・POST直前にsolve（IP整合性↑）
                                        #   browser_presolve=True と組み合わせると最も通過率が高い
    "presolve_prewarm": False,          # ★ True → スロットデータが無い「解禁前」でもプールを連続充填し続ける。
                                        #   解禁時に全ワーカーが新鮮なトークンを即持てる＝fire精度↑。
                                        #   ただし待機中もcapsolverを消費し続ける（TTL50s毎に再solve）。
                                        #   解禁が近い時だけONにするのが無駄がない。
    "browser_presolve": False,          # ★ Fix 54: True → プリソルブをPlaywrightブラウザで実行
                                        #   cf_clearanceと同IPでTurnstileを解くためCloudflare通過率↑
                                        #   capsolverなしでも動作。OFF時はcapsolver API（高速）
    "browser_mode":     False,          # ★ ブラウザモード: True → Playwright で予約フォームを直接操作
                                        #   Cloudflare/JS チャレンジをブラウザネイティブで通過
                                        #   GPS 許可ポップアップを自動承認（geolocation 設定）
                                        #   Turnstile をブラウザネイティブで自動通過（capsolver 不要）
                                        #   予約 POST をブラウザ内 JS fetch で実行（Cookie/セッション引き継ぎ）
    "debug_mode":       False,          # True → save HTML + HAR to debug/ for every browser session
    # ── C++ 同期一斉発火モード(新) ─────────────────────────────────────────────
    "fire_mode":        False,          # True → book() を C++発火コア(fire_core)で同期一斉発火
    "fire_core_path":   "",             # 省略時は exe/py と同じ場所の fire_core(.exe) を自動探索
    "fire_engine_path": "",             # A案エンジン(fire_engine)。省略時は同フォルダを自動探索
    "fire_engine_open_min": 20,         # [6]本番: この残数以上の枠だけ「解禁」とみなし発火(売れ残り誤爆防止)
    "fire_align_ms":    1500,           # 最初のワーカー登録からこの時間内に集まった分を一括発火
    "fire_spin_lead_ms": 3.0,           # 発火この手前(ms)から busy-wait
    "fire_attempts":    1,              # 2xx が出るまでの最大試行(409等はリトライ)
    "token_pool":       1,              # [6]1台あたり事前ソルブ数(使い捨てトークン対策/=fire_attemptsで安全)
    "fire_time_grace_ms": 400,          # [6]解禁+この時間で発火(時刻優先の基準)。早すぎると409
    "fire_align_lead_sec": 25,          # [6]解禁の何秒前からプレソルブ開始(小=新鮮/大=ソルブ余裕)
    "retry_remaining_min": 20,          # [6]廃止（リトライは解禁検知後の生の残り人数が多い枠から）
    "solve_workers":    24,             # [6]capsolver同時解決数(契約上限に合わせる。多台で短縮)
    "token_single_use": True,           # [6]トークン使い捨て前提(リトライを実トークン数で自動制限)
    "fire_time_priority": True,         # [6]基本は解禁+graceで発火。検知は早発時のみ前倒し(既定ON)
    "fire_resync_gap_sec": 30.0,        # 前回一斉発火からこの秒数空いて新規発火要求 → 新解禁サイクルとみなし窓リセット
                                        #   （リトライは数秒内=単発即時 / 7日後枠などの再解禁=再同期）
    "fire_barrier_mode": True,          # True → 検知バリア型: 全ワーカー共有の「解禁検知時刻 t0」を基準に
                                        #        fire_at = t0 + barrier_lead_ms に全発火を固定(絶対時刻同期)。
                                        #        False → 旧: first-submit から fire_align_ms の相対窓。
    "fire_barrier_lead_ms": 250,        # 検知 t0 から何ms後に一斉発火するか(準備が間に合うワーカーを集める猶予)
    "fire_launch_margin_ms": 80,
    "fire_clock_sync":  True,           # [6]サーバー時刻(Dateヘッダ)とPC時計のずれを測り、発火時刻を補正
    "fire_clock_max_ms": 3000,          # [6]補正の上限(ms)。これ以上ずれていたら補正せず警告のみ
    "fire_retry_window_sec": 10.0,
    "fire_post_timeout_ms": 8000,       # [6]予約POSTの応答待ち上限(ms)。本隊・追い撃ち共通（旧15秒）      # [6]解禁からこの秒数は、落選/未発火の台がソルブし続けて連続リトライ（0で無効）
    "fire_warm_quiet_ms": 6000,         # [6]発火のこの時間前からは接続の再温めをしない(エンジンv2)        # 発火時刻の何ms手前で fire_core を起動するか(残りはC++ busy-waitで精密整列)
    # ★ 店舗ID。API が store スコープ化され、予約POST(/api/registrations)に store_id(正の整数)が
    #   必須になった（欠落すると code 2001 PARAM_INVALID で全落選）。URLの ?store_id=N と一致させる。
    "store_id":         2,
    # ── 再撃(追い撃ち)の事前ログイン（方式A）──────────────────────────────────────
    #   初回ログイン(本隊発火用)とは別に、各アカウントを追加ログインして追い撃ち専用
    #   セッションを13時前に確保しておく。落選/未発火の台は、毎回このフレッシュな
    #   専用セッションで撃ち直す（本隊セッションのトークン消費/フラグの影響を避ける）。
    "chase_relogin":    True,           # [6]追い撃ちを別ログインの専用セッションで撃つ(方式A)。False=本隊セッション流用
    "chase_sessions_max": 3,            # [6]1台あたり事前確保する追い撃ちセッションの上限(実値=min(リトライ回数,これ))
    # ── 解禁検知の速度（先着レースの最重要レバー）─────────────────────────────────
    "dates_cache_ttl":  0.9,            # bookable-dates 共有キャッシュTTL(秒)。小さいほど解禁を早く検知。
    "slots_cache_ttl":  0.45,           # slots 共有キャッシュTTL(秒)。小さいほど早いがAPI呼び出し増。
                                        #   先着5秒レースでは 0.2〜0.3 推奨（Failed to get dates が増えない範囲で）
    # ── 優先時間枠 ────────────────────────────────────────────────────────────
    "preferred_times":  [],             # 優先する時間枠 (HH:MM 形式, 複数可)
                                        # accounts.txt の4フィールド目でアカウント別に上書き可能
    # ── Ajax / Sequential ─────────────────────────────────────────────────────
    "ajax_mode":        False,           # ★ Fix 47: True → 全枠に同時POST（Ajaxモード）
    "ajax_jitter_max":  0.3,             # ★ Fix 47: 非Ajaxモード: POST直前の最大ジッター秒数（0で無効）
}

# ── logging ──────────────────────────────────────────────────────────────────
def _setup_logging(debug_mode: bool = False):
    """★ Fix 45: セッション別ログファイルを logs/ に保存。
    debug_mode=True のとき DEBUG レベル（[DBG] API詳細含む）も記録する。
    通常時は INFO 以上のみ。mellojoy.log は常時 INFO 以上を追記。

    出力ファイル:
      mellojoy.log           — 常設・INFO 以上（全セッション追記）
      logs/YYYYMMDD_HHMMSS.log — セッション別・通常: INFO以上 / debug_mode: DEBUG以上
    """
    import datetime as _dt
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    handlers: list[logging.Handler] = []

    # 1) 常設ログ（INFO 以上・全セッション通し）
    fh = logging.FileHandler(LOG_F, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(fmt))
    handlers.append(fh)

    # 2) セッション別ログ（logs/YYYYMMDD_HHMMSS.log）
    LOGS_DIR.mkdir(exist_ok=True)
    _ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    _lvl = logging.DEBUG if debug_mode else logging.INFO
    sfh = logging.FileHandler(LOGS_DIR / f"{_ts}.log", encoding="utf-8")
    sfh.setLevel(_lvl)
    sfh.setFormatter(logging.Formatter(fmt))
    handlers.append(sfh)

    # ログはファイルのみ。コンソールには出さない（print()と混在防止）
    logging.basicConfig(level=logging.DEBUG, format=fmt, handlers=handlers)

# ── locks ─────────────────────────────────────────────────────────────────────
_acc_lock      = threading.Lock()
_prx_lock      = threading.Lock()
_succ_lock     = threading.Lock()
_email_lock    = threading.Lock()
_pfx_lock      = threading.Lock()
_con_lock      = threading.Lock()   # コンソール出力の排他（複数スレッドの行混在防止）

# ── send-code グローバルレートリミッター ──────────────────────────────────────
# Resend の上限は 10件/秒。安全マージンとして 8件/秒 に制限する。
import collections as _collections
_send_code_lock  = threading.Lock()
_send_code_times: _collections.deque = _collections.deque()
_SEND_CODE_RPS   = 8   # 1秒あたり最大リクエスト数

def _throttle_send_code() -> None:
    """send-code 送信前に呼ぶ。8件/秒を超える場合はスリープして待つ。"""
    while True:
        with _send_code_lock:
            now = time.time()
            while _send_code_times and now - _send_code_times[0] >= 1.0:
                _send_code_times.popleft()
            if len(_send_code_times) < _SEND_CODE_RPS:
                _send_code_times.append(now)
                return
        time.sleep(0.05)

# ★ Fix 73: send-code が 429 / レート制限を返したら、全ワーカーの送信を一時停止する
_send_code_cooldown_until = [0.0]

def _send_code_cooldown(sec: float) -> None:
    with _send_code_lock:
        _send_code_cooldown_until[0] = max(_send_code_cooldown_until[0], time.time() + sec)

def _wait_send_code_cooldown() -> None:
    while True:
        with _send_code_lock:
            rem = _send_code_cooldown_until[0] - time.time()
        if rem <= 0:
            return
        time.sleep(min(rem, 1.0))


def _classify_send_code_error(e: dict) -> str:
    """send-code 失敗理由の分類。
    fatal=メール使用済み/無効（リトライ無意味） / rate=レート制限 / cf=Cloudflare ブロック /
    net=通信・SSL・5xx / token=Turnstile 拒否・その他（新トークンでリトライ）"""
    if not e:
        return "token"
    st = e.get("status")
    txt = (str(e.get("code") or "") + " " + str(e.get("body") or "")).lower()
    if st is None:
        return "net"
    if any(k in txt for k in ("already", "exist", "registered", "duplicate", "invalid email",
                              "email_invalid", "invalid_email", "使用", "登録済")):
        return "fatal"
    if st == 429 or any(k in txt for k in ("rate", "too many", "too_many", "limit", "頻繁", "稍后")):
        return "rate"
    if st in (403, 503) and any(k in txt for k in ("cloudflare", "just a moment", "cf-", "challenge",
                                                    "<html", "attention required")):
        return "cf"
    if st >= 500:
        return "net"
    return "token"


# "No slots available" 最低表示間隔 (秒)。monitor_delay×100 か 10s の大きい方。
# 例: monitor_delay=0.1s → 10s に1回表示（0.1×15W=150行/秒 → スパム防止）
_STATUS_PRINT_MIN_SEC: float = 10.0

# ── Webhook シングルトンプール ─────────────────────────────────────────────────
# 50ワーカーが同時に success しても 1 つのプールで処理（スレッド爆発防止）
# max_workers=16 で URL数×同時success数のタスクをキューイング処理
_wh_pool: ThreadPoolExecutor = ThreadPoolExecutor(
    max_workers=16, thread_name_prefix="webhook"
)

# ── FCFSアカウント共有キュー ──────────────────────────────────────────────────
# 各要素: (pair: str, name_rank: int, name_count: int)
# permanent worker が get() → 処理 → re-login失敗時は put() で戻す
_acct_q: _queue.Queue = _queue.Queue()

# ── Turnstileプリソルブ共有プール ──────────────────────────────────────────────
# 全FCFSワーカーが共有するトークンプール（menu_fcfs起動時にpool_sizeで再初期化）
# 100ワーカーでも同時solve数はpool_sizeに制限 → クレジット消費を抑制
# 各要素: (token_str: str, timestamp: float)
_shared_ts_pool: _queue.Queue    = _queue.Queue(maxsize=5)
_presolve_sem: threading.Semaphore = threading.Semaphore(5)
_PRE_TOKEN_TTL: float            = 50.0   # Turnstileトークン保持上限(s)；サーバー側~60sより短め

# ★ Fix 29: _NoLock → Semaphore(5) に変更。
#   parallel=20 等で全ワーカーが同時に Playwright ブラウザを起動すると
#   Chromium が parallel 数だけ同時起動して RAM/CPU を枯渇させる可能性がある。
#   Semaphore(5) で同時起動を最大5プロセスに制限する。
#   threading.Semaphore は __enter__/__exit__ をサポートするため
#   既存の "with _browser_lock:" 呼び出しはそのまま動作する。
_browser_lock = threading.Semaphore(5)   # 同時 Chromium 起動は最大5まで（FCFS 開始時に変更される）

def _set_browser_limit(n: int) -> None:
    """★ Fix 73: ブラウザ同時起動数の上限を変更する。
    ブラウザモードではワーカー1つにつき待機ブラウザを1つ起動し続けるため、
    FCFS 開始時に「並列数＋予備」に合わせる。各呼び出し側は実行時に
    グローバル _browser_lock を参照するので、ワーカー起動前に差し替えれば反映される。"""
    global _browser_lock
    _browser_lock = threading.Semaphore(max(1, int(n)))

# ★ Fix 71: 起動画面に表示するビルド識別子。実行中の exe / py が最新かどうかの確認用。
BUILD_ID = "fire-A5"
# ★ アプリのバージョン表示。配布リリースのタグ(vX.Y.Z)と合わせること。
#   リリースを上げるときは、ここと GitHub のタグを同じ番号にする。
APP_VERSION = "3.0.2"

# ★ Fix 62: グローバル cf_clearance キャッシュ
#   全ワーカーが取得した cf_clearance を共有し、ブラウザログイン回数を削減する。
#   cf_clearance は IP/プロキシに紐付くため proxy URL をキーにする（プロキシなしは ""）。
#   TTL は 25分（CF clearance のデフォルト有効期限は 30分 前後）。
_cf_clearance_cache: dict = {}          # {proxy_key: (cf_clearance_value, expires_ts)}
_cf_clearance_cache_lock = threading.Lock()
_CF_CLEARANCE_CACHE_TTL = 1500.0        # 秒（25分）

def _get_shared_cf_clearance(proxy: Optional[str]) -> Optional[str]:
    """プロキシに対応するグローバルキャッシュから cf_clearance を返す。期限切れなら None。"""
    import time as _t62
    with _cf_clearance_cache_lock:
        key = proxy or ""
        entry = _cf_clearance_cache.get(key)
        if entry:
            val, exp = entry[0], entry[1]
            if _t62.time() < exp:
                return val
            del _cf_clearance_cache[key]
    return None

def _get_shared_cf_identity(proxy: Optional[str]) -> tuple:
    """★ Fix 72: (cf_clearance, 取得時のUA, sec-ch-ua) を返す。cf_clearance は UA と組で有効。"""
    import time as _t72
    with _cf_clearance_cache_lock:
        entry = _cf_clearance_cache.get(proxy or "")
        if entry and _t72.time() < entry[1]:
            return (entry[0],
                    entry[2] if len(entry) > 2 else None,
                    entry[3] if len(entry) > 3 else None)
    return (None, None, None)

def _set_shared_cf_clearance(proxy: Optional[str], value: str,
                             ua: Optional[str] = None, sec_ch_ua: Optional[str] = None) -> None:
    """取得した cf_clearance をグローバルキャッシュに保存する。
    ★ Fix 72: 取得したブラウザの UA / sec-ch-ua も一緒に保存する（cf_clearance は UA に紐付く）。"""
    import time as _t62
    if not value:
        return
    with _cf_clearance_cache_lock:
        key = proxy or ""
        _cf_clearance_cache[key] = (value, _t62.time() + _CF_CLEARANCE_CACHE_TTL, ua, sec_ch_ua)

# ★ Fix 32: _monitor_account() 例外による無限requeue 防止の上限値
_MAX_REQUEUE = 5   # 同一アカウントをキューに戻せる最大回数

# ★ Fix 35: _pre_token の残り有効期限がこの秒数以下になったら、
#   プールに新しいトークンがあれば入れ替える（TTL=50s, margin=15s → 35s経過で更新試行）
_PRE_REFRESH_MARGIN = 15.0

# （Fix 33: _proxy_cycle は Fix 39 で random.choice に置き換えたため削除）

# ── IMAP 共有コードキャッシュ ─────────────────────────────────────────────────
# バックグラウンドの _imap_poller スレッドが書き込み、
# fetch_mello_code_cached() が読み出す。接続は常時1本のみ。
_code_cache      : dict[str, str] = {}   # target_email(lower) → 6桁コード
_code_cache_lock : threading.Lock = threading.Lock()
# ★ in-flight: この実行が現在コード待ちをしているアカウントのメールアドレス集合。
#   同一 catch-all 受信箱を複数の実行/マシンが共有している場合、ポーラーが
#   「自分が送っていない宛先」のコードまで消費して \Seen を立ててしまい、
#   本来の持ち主（別実行）がコード取りこぼし→timeout する事故を防ぐ。
#   送信直前に add、取得/タイムアウト後に discard する。
_inflight        : set[str] = set()
_inflight_lock   : threading.Lock = threading.Lock()

# ── file helpers ──────────────────────────────────────────────────────────────
def load_accounts() -> list[str]:
    if not ACCOUNTS_F.exists(): return []
    with _acc_lock:
        lines = ACCOUNTS_F.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines
            if l.strip() and not l.strip().startswith("#") and ":" in l]

def save_accounts(pairs: list[str]):
    with _acc_lock:
        ACCOUNTS_F.write_text(
            "\n".join(pairs) + ("\n" if pairs else ""),
            encoding="utf-8"
        )

def remove_account(pair: str):
    with _acc_lock:
        if not ACCOUNTS_F.exists(): return
        lines = ACCOUNTS_F.read_text(encoding="utf-8").splitlines()
        new = [l for l in lines if l.strip() != pair.strip()]
        ACCOUNTS_F.write_text("\n".join(new) + "\n", encoding="utf-8")

_webhooks_cache: Optional[list[str]] = None
_webhooks_mtime: float = 0.0
_webhooks_cache_lock: threading.Lock = threading.Lock()

def load_webhooks() -> list[str]:
    """webhooks.txt から Webhook URL リストを読み込む（#コメント・空行除外）。
    ファイルの mtime が変わっていなければキャッシュを返す → ファイル I/O を最小化。
    """
    global _webhooks_cache, _webhooks_mtime
    with _webhooks_cache_lock:
        if not WEBHOOKS_F.exists():
            _webhooks_cache = []
            return []
        mtime = WEBHOOKS_F.stat().st_mtime
        if _webhooks_cache is not None and mtime == _webhooks_mtime:
            return _webhooks_cache
        lines = WEBHOOKS_F.read_text(encoding="utf-8").splitlines()
        _webhooks_cache = [l.strip() for l in lines
                           if l.strip() and not l.strip().startswith("#")]
        _webhooks_mtime = mtime
        return _webhooks_cache

def _is_discord_webhook(url: str) -> bool:
    u = url.lower()
    return ("/api/webhooks/" in u) and ("discord.com" in u or "discordapp.com" in u)


def _to_discord_payload(data: dict) -> dict:
    """生イベント dict を Discord Webhook が受け付ける形（embeds/content）に変換する。
    ★ これが無いと Discord は 400 'Cannot send an empty message' を返し、通知が永久に飛ばない。
    """
    ev = str(data.get("event", ""))
    if ev == "booking_success":
        desc = "\n".join(filter(None, [
            f"**氏名**: {data.get('name','')}" if data.get('name') else "",
            f"**メール**: {data.get('email','')}" if data.get('email') else "",
            f"**予約枠**: {data.get('slot_time','')}" if data.get('slot_time') else "",
            f"**時刻**: {data.get('timestamp','')}" if data.get('timestamp') else "",
        ])) or "予約成功"
        return {"username": "MelloJoy BOT",
                "embeds": [{"title": "🎉 予約成功 (MelloJoy)", "description": desc,
                            "color": 3066993}]}
    # 汎用: 既知でないイベントは key: value を content に整形して送る（空メッセージ回避）。
    txt = "\n".join(f"**{k}**: {v}" for k, v in data.items())
    return {"username": "MelloJoy BOT", "content": (txt[:1900] or "(通知)")}


def fire_webhooks(data: dict) -> None:
    """Webhook URLs へ並列で JSON POST する。
    プロキシ環境変数を除外して直接送信（HTTP_PROXY / HTTPS_PROXY を迂回）。
    ★ Discord の webhook URL には Discord 形式(embeds/content)に変換して送る。
      それ以外のURLには従来どおり生の data を送る（独自エンドポイント互換）。
    """
    urls = load_webhooks()
    if not urls:
        return
    _wh_log = logging.getLogger("webhook")
    # requests の Session を毎回生成しプロキシ環境変数を無効化
    def _post(url: str):
        try:
            s = _req.Session()
            s.trust_env = False   # HTTP_PROXY / HTTPS_PROXY 環境変数を無視
            payload = _to_discord_payload(data) if _is_discord_webhook(url) else data
            r = s.post(url, json=payload, timeout=12)
            if not r.ok:
                _wh_log.warning(f"Webhook POST failed {r.status_code}: {url}  resp={r.text[:160]}")
            else:
                _wh_log.info(f"Webhook sent OK → {url[:60]}")
        except Exception as e:
            _wh_log.error(f"Webhook POST error: {url[:60]} — {e}")
    # シングルトンプールに submit（非ブロッキング・fire-and-forget）
    # 50ワーカー同時 success でも新規プールを生成しないのでスレッド爆発なし
    for url in urls:
        _wh_pool.submit(_post, url)

def mark_success(pair: str, slot_time: str = ""):
    """success.txt に予約結果を追記し、Webhook に通知する。
    フォーマット: ID,PW,氏名,予約時間,タイムスタンプ
    """
    ts    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = pair.split(":", 3)   # 最大4分割 (email:pw:name:preferred_slot)
    email    = parts[0] if len(parts) > 0 else pair
    password = parts[1] if len(parts) > 1 else ""
    name     = parts[2] if len(parts) > 2 else ""
    # 4フィールド目(preferred_slot)は success.txt には含めない
    line = f"{email},{password},{name},{slot_time},{ts}"
    with _succ_lock:
        with open(SUCCESS_F, "a", encoding="utf-8") as f:
            f.write(f"{line}\n")
    # ★ Fix 22: remove_account() が I/O 例外を投げても fire_webhooks() を確実に呼ぶ
    try:
        remove_account(pair)
    except Exception as _re:
        logging.getLogger("mark_success").error(f"remove_account failed: {_re}")
    # ── Webhook 通知 ──────────────────────────────────────────────────────────
    fire_webhooks({
        "event":     "booking_success",
        "email":     email,
        "name":      name,
        "slot_time": slot_time,
        "timestamp": ts,
    })


# ── 全体Webhook（成功時のみ・個人情報なし・URLハードコード）──────────────────────
#   当選1件ごとに固定の Discord Webhook へ通知する。
#   ★ メールアドレス・氏名・パスワードなどの個人情報は一切載せない。
#   ★ 送る内容: 日付 / 予約枠 / 設定アカウント数 / プレソルブ秒前 / 再撃回数 / Ver / 取得時間。
_GLOBAL_WEBHOOK_URL = ("https://discordapp.com/api/webhooks/1572358343043981484/"
                       "5Piw2ZHHnDaCmkGdv0E95YTpS4p-9KQxB7sIKDScqWGiVNTxNouNweIF95ODKqsnVRr8")
# ★ 連投時に Discord のレート制限(429)で弾かれないよう、送信を直列化するロック。
#   複数当選が同時に発生しても、1通ずつ順に送り、429 は Retry-After を尊重して再送する。
_win_wh_lock = threading.Lock()


def _coerce_field(v, fallback: str = "-") -> str:
    """Discord の embed field value は空文字だと 400 で弾かれる。必ず非空の文字列にする。"""
    s = "" if v is None else str(v)
    s = s.strip()
    return s if s else fallback


def _fire_win_webhook(date, slot_time, acct_count, presolve_lead, chase_count, store_id=None) -> None:
    """当選1件ごとに固定 Webhook へ通知（成功時のみ・個人情報なし・fire-and-forget）。"""
    url = _GLOBAL_WEBHOOK_URL
    if not url:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _pl = str(int(float(presolve_lead)))
    except Exception:
        _pl = str(presolve_lead)
    embed = {
        "title": "success(MelloJoy)",
        "color": 3066993,   # green
        "fields": [
            {"name": "日付",            "value": _coerce_field(date),      "inline": True},
            {"name": "予約店舗",        "value": _coerce_field(store_id),  "inline": True},
            {"name": "予約枠",          "value": _coerce_field(slot_time), "inline": True},
            {"name": "設定アカウント数", "value": _coerce_field(acct_count), "inline": True},
            {"name": "プレソルブ",      "value": _coerce_field(f"{_pl}s前"), "inline": True},
            {"name": "再撃回数",        "value": _coerce_field(chase_count), "inline": True},
        ],
        "footer": {"text": f"MelloJoy BOT v{APP_VERSION} • 取得 {ts}"[:2040]},
    }
    payload = {"username": "MelloJoy BOT", "embeds": [embed]}

    def _post():
        # ★ 直列送信＋429(レート制限)は Retry-After を尊重して再送。連投でも弾かれず全通届く。
        with _win_wh_lock:
            for _att in range(6):
                try:
                    s = _req.Session()
                    s.trust_env = False   # HTTP_PROXY / HTTPS_PROXY を無視して直接送信
                    r = s.post(url, json=payload, timeout=12)
                    if r.status_code == 429:
                        try:
                            _ra = r.headers.get("Retry-After")
                            if _ra is None:
                                _ra = (r.json() or {}).get("retry_after")
                            wait = float(_ra) if _ra is not None else 1.0
                        except Exception:
                            wait = 1.0
                        time.sleep(min(max(wait, 0.2), 10.0))
                        continue
                    # 成功/その他 → バースト回避に軽く間隔を空けて終了
                    time.sleep(0.35)
                    return
                except Exception:
                    time.sleep(0.5)
    try:
        _wh_pool.submit(_post)
    except Exception:
        pass

def load_proxies() -> list[str]:
    if not PROXIES_F.exists(): return []
    with _prx_lock:
        lines = PROXIES_F.read_text(encoding="utf-8").splitlines()
    # '#' で始まる行とブランク行を除外
    return [l.strip() for l in lines
            if l.strip() and not l.strip().startswith('#')]

def _normalize_proxy_url(proxy_url: str) -> str:
    """Ensure proxy URL has a scheme so Playwright / requests accept it.

    対応フォーマット:
      host:port                    → http://host:port
      host:port:user:pass          → http://user:pass@host:port
      http://host:port             → unchanged
      http://user:pass@host:port   → unchanged
      socks5://host:port           → unchanged
    """
    url = proxy_url.strip()
    if not url:
        return url
    if "://" in url:
        return url
    # host:port:user:pass 形式（コロン区切り4分割）を標準 URL に変換
    # パスワードにコロンが含まれる場合も考慮して最大3分割
    parts = url.split(":", 3)
    if len(parts) == 4:
        host, port, user, passwd = parts
        return f"http://{user}:{passwd}@{host}:{port}"
    # host:port のみ
    return "http://" + url

def _parse_proxy(proxy_url: str) -> dict:
    """Split a proxy URL into the separate fields YesCaptcha requires.

    Input (any of):
      host:port
      http://host:port
      http://user:pass@host:port
      socks5://host:port

    Returns dict: type, address, port  (+ login/password when present).
    """
    from urllib.parse import urlparse
    url = _normalize_proxy_url(proxy_url)
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() if parsed.scheme else "http"
    if scheme not in ("http", "https", "socks4", "socks5"):
        scheme = "http"
    result: dict = {
        "type":    scheme,
        "address": parsed.hostname or "",
        "port":    parsed.port or 8080,
    }
    if parsed.username:
        result["login"]    = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result

def pick_proxy(proxies: list[str]) -> Optional[str]:
    """プロキシリストからランダムに1件選択する。★ Fix 39: cycle→random.choice"""
    if not proxies: return None
    return random.choice(proxies)

def pick_device(cfg: dict) -> tuple[str, tuple[str, int, int, int]]:
    keys = [k for k in cfg.get("devices", ["pixel7"]) if k in DEVICES] or ["pixel7"]
    key  = random.choice(keys)
    return key, DEVICES[key]

# ── config ───────────────────────────────────────────────────────────────────
def load_cfg() -> dict:
    cfg = dict(DEF_CFG)
    if CFG_F.exists():
        try:
            cfg.update(json.loads(CFG_F.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg

def save_cfg(cfg: dict):
    CFG_F.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

# ── names.txt helpers ─────────────────────────────────────────────────────────
# Entry = (full_name, gender_en, age_or_None)
# Supported formats:
#   氏名:性別        e.g.  田中 太郎:男          → age = None (use range)
#   氏名:性別:年齢   e.g.  田中 太郎:男:28        → age = 28 (used as-is)

def load_names() -> list[tuple[str, str, Optional[int]]]:
    """Return list of (full_name, gender_en, age|None) from names.txt."""
    if not NAMES_F.exists(): return []
    results = []
    for line in NAMES_F.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line: continue
        parts = [p.strip() for p in line.split(":")]
        name      = parts[0]
        gender_raw = parts[1] if len(parts) > 1 else "男"
        gender_en = "male" if gender_raw in ("男", "male", "M", "m") else "female"
        age: Optional[int] = None
        if len(parts) >= 3 and parts[2].isdigit():
            age = int(parts[2])
        if name:
            results.append((name, gender_en, age))
    return results

def pick_name(names: list[tuple[str, str, Optional[int]]]) -> tuple[str, str, Optional[int]]:
    """Pick a random (name, gender_en, age|None) entry."""
    if not names:
        defaults = [("山田 太郎", "male", None), ("鈴木 花子", "female", None)]
        return random.choice(defaults)
    return random.choice(names)

# ── email generation ──────────────────────────────────────────────────────────
def _load_used_prefixes() -> set[str]:
    if not USED_PFX_F.exists(): return set()
    return set(USED_PFX_F.read_text(encoding="utf-8").splitlines())

_used_prefixes_set: Optional[set[str]] = None   # メモリキャッシュ（起動後一度だけファイル読込）

def _get_used_prefixes() -> set[str]:
    """used_prefixes.txt を一度だけ読み込んでメモリにキャッシュする。
    高並列環境で毎回ファイル全読みする O(n) コストを排除。"""
    global _used_prefixes_set
    # _pfx_lock 保護下でチェック → 複数スレッドが同時に初期化しない
    with _pfx_lock:
        if _used_prefixes_set is None:
            _used_prefixes_set = _load_used_prefixes()
        return _used_prefixes_set

def _save_prefix(pfx: str):
    global _used_prefixes_set
    with _pfx_lock:
        with open(USED_PFX_F, "a", encoding="utf-8") as f:
            f.write(pfx + "\n")
        if _used_prefixes_set is not None:
            _used_prefixes_set.add(pfx)   # キャッシュも同時更新

def gen_catchall_email(domain: str) -> str:
    """Generate a unique email prefix for a catch-all domain.
    Uses timestamp (ms) + random hex to ensure >1M combinations, no collision.
    Format: <4-alpha><timestamp-hex><3-random-hex>@domain
    e.g.  xkbq17a3f2c00a91@example.com
    """
    domain = domain.lstrip("@").strip()
    used = _get_used_prefixes()   # ← ファイルではなくメモリキャッシュから取得
    for _ in range(100):
        alpha = "".join(random.choices(string.ascii_lowercase, k=4))
        ts    = f"{int(time.time() * 1000):x}"    # 13 hex chars
        rnd   = secrets.token_hex(3)              # 6 hex chars
        pfx   = f"{alpha}{ts}{rnd}"
        if pfx not in used:
            _save_prefix(pfx)
            return f"{pfx}@{domain}"
    # fallback: uuid4 hex
    pfx = uuid.uuid4().hex
    _save_prefix(pfx)
    return f"{pfx}@{domain}"

_email_list_cycle = None
_email_list_mtime: float = 0.0  # ★ Fix 15: emails.txt 変更検知用
def pick_list_email() -> Optional[str]:
    global _email_list_cycle, _email_list_mtime
    if not EMAILS_F.exists(): return None
    with _email_lock:
        # ★ Fix 15: mtime が変わっていたら cycle を再初期化して変更を即反映
        try:
            cur_mtime = EMAILS_F.stat().st_mtime
        except OSError:
            return None
        if _email_list_cycle is None or cur_mtime != _email_list_mtime:
            lines = [l.strip() for l in EMAILS_F.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.strip().startswith("#")]
            if not lines: return None
            _email_list_cycle = itertools.cycle(lines)
            _email_list_mtime = cur_mtime
        return next(_email_list_cycle)

def gen_password(template: str) -> str:
    """Expand password template.
    {r6}  → 6 random alphanum chars
    {r8}  → 8 random alphanum chars
    {r4d} → 4 random digits
    """
    pw = template
    pw = re.sub(r"\{r(\d+)d\}",
                lambda m: "".join(random.choices(string.digits, k=int(m.group(1)))), pw)
    pw = re.sub(r"\{r(\d+)\}",
                lambda m: "".join(random.choices(string.ascii_letters + string.digits, k=int(m.group(1)))), pw)
    return pw

# ── UI helpers ────────────────────────────────────────────────────────────────
W = 62
def _box(title: str):
    print(f"\n{Fore.CYAN}{Style.BRIGHT}╔{'═'*(W-2)}╗")
    pad = (W - 2 - len(title)) // 2
    print(f"║{' '*pad}{Fore.WHITE}{title}{Fore.CYAN}{' '*(W-2-pad-len(title))}║")
    print(f"╚{'═'*(W-2)}╝{Style.RESET_ALL}")

def _ok(m: str):   print(f"{Fore.GREEN}[✓] {m}{Style.RESET_ALL}")
def _err(m: str):  print(f"{Fore.RED}[✗] {m}{Style.RESET_ALL}")
def _info(m: str): print(f"{Fore.CYAN}[i] {m}{Style.RESET_ALL}")
def _warn(m: str): print(f"{Fore.YELLOW}[!] {m}{Style.RESET_ALL}")
def _wait(m: str): print(f"{Fore.MAGENTA}[…] {m}{Style.RESET_ALL}", flush=True)

def _inp(prompt: str, default: str = "") -> str:
    sfx = f" [{default}]" if default else ""
    v = input(f"{Fore.WHITE}{prompt}{sfx}: {Style.RESET_ALL}").strip()
    return v if v else default

def _inp_int(prompt: str, default: int) -> int:
    while True:
        raw = _inp(prompt, str(default))
        try: return int(raw)
        except ValueError: _err("Please enter a number.")

def _inp_float(prompt: str, default: float) -> float:
    while True:
        raw = _inp(prompt, str(default))
        try: return float(raw)
        except ValueError: _err("Please enter a number (e.g. 0.1, 1.5, 5).")

def _pause(msg: str = "Press Enter to continue..."):
    input(f"\n{Fore.WHITE}{msg}{Style.RESET_ALL}")

# ── Browser resolver ──────────────────────────────────────────────────────────
# Maps setting name → (playwright browser type attr, channel or None)
# "chromium" uses Playwright's bundled Chromium (needs  playwright install chromium)
# "chrome"   uses system Google Chrome via Playwright channel
# "msedge"   uses system Microsoft Edge  via Playwright channel
# "firefox"  uses Playwright's bundled Firefox
# "webkit"   uses Playwright's bundled WebKit (macOS only)
_BROWSER_MAP: dict[str, tuple[str, Optional[str]]] = {
    "chromium": ("chromium", None),
    "firefox":  ("firefox",  None),
    "webkit":   ("webkit",   None),
    "chrome":   ("chromium", "chrome"),
    "edge":     ("chromium", "msedge"),
    "msedge":   ("chromium", "msedge"),
}

def _resolve_browser(pw_obj, br_type: str):
    """Return (browser_type_obj, channel_str_or_None) for a pw instance."""
    attr, channel = _BROWSER_MAP.get(br_type.lower(), ("chromium", None))
    btype = getattr(pw_obj, attr, pw_obj.chromium)
    return btype, channel


# ── Debug helpers ─────────────────────────────────────────────────────────────
def _debug_dump(name: str, html: str):
    """Silently save an HTML snapshot to debug/<name>.html."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"{name}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass

def _debug_dump_json(name: str, data) -> None:
    """Silently save JSON data to debug/<name>.json (for API-mode debug snapshots)."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        txt = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        (DEBUG_DIR / f"{name}.json").write_text(txt, encoding="utf-8")
    except Exception:
        pass

def _priority_shuffle(slots: list[dict], front_n: int = 4,
                      front_prob: Optional[float] = None) -> list[dict]:
    """前半front_n枠を前半優先の確率でシャッフルして返す。

    ★ Fix 51: front_prob を固定 0.6 から毎回ランダム (0.35〜0.75) に変更。
    午前枠と午後枠の狙い目が毎回微妙に変わることで、同一スロットへの
    集中を緩和し、予約が散らばりやすくなる。

    各ステップで「前半から取る確率 front_prob : 後半から取る確率 1-front_prob」の
    グループ単位の確率で次に試す枠を決定（非復元）。
    片方のグループが尽きたら残りを順次追加。
    枠が front_n 以下の場合は通常シャッフルのみ。
    """
    # ★ Fix 51: front_prob 未指定 → ランダム (0.35〜0.75)
    if front_prob is None:
        front_prob = random.uniform(0.35, 0.75)
    if not slots:
        return []
    # startTime 文字列でソート（"HH:MM" / "HH:MM:SS" 形式を想定）
    by_time = sorted(slots, key=lambda s: s.get("startTime", ""))
    n = len(by_time)
    if n <= front_n:
        result = by_time[:]
        random.shuffle(result)
        return result

    front = by_time[:front_n]
    back  = by_time[front_n:]
    random.shuffle(front)
    random.shuffle(back)

    # グループ単位の確率で非復元サンプリング
    result: list[dict] = []
    fi = bi = 0   # front / back ポインタ
    while fi < len(front) or bi < len(back):
        can_front = fi < len(front)
        can_back  = bi < len(back)
        if can_front and can_back:
            pick_front = random.random() < front_prob
        else:
            pick_front = can_front
        if pick_front:
            result.append(front[fi]);  fi += 1
        else:
            result.append(back[bi]);   bi += 1
    return result

# ── ブラウザスタンバイセッション（事前起動モード用） ────────────────────────────
class _BrowserStandby:
    """
    ブラウザスタンバイセッション。
    ready_event  : ブラウザが攻撃可能状態になったことをメインスレッドに通知
    cmd_queue    : メインスレッド → ブラウザスレッド（slot dict or None=停止）
    res_queue    : ブラウザスレッド → メインスレッド（result 文字列）
    """
    __slots__ = ("ready_event", "cmd_queue", "res_queue")
    def __init__(self):
        self.ready_event = threading.Event()
        self.cmd_queue   = _queue.Queue()
        self.res_queue   = _queue.Queue()

# ── Cloudflare Bypass (Playwright headed) ─────────────────────────────────────
class CloudflareBypass:

    def __init__(self, cfg: dict, logger: logging.Logger,
                 proxies_list: Optional[list] = None):
        self.cfg          = cfg
        self.logger       = logger
        self.proxies_list      = proxies_list  # ★ Fix 38: プロキシローテーション用リスト
        self.last_cf_clearance: Optional[str] = None  # ★ Fix 46: Playwright から抽出した cf_clearance
        # ★ Fix 72: cf_clearance を取得したブラウザの識別情報（HTTP 側を合わせるのに使う）
        self.last_cf_ua: Optional[str] = None
        self.last_cf_sec_ch_ua: Optional[str] = None
        self._identity_aligned: bool = False

    @staticmethod
    def _stealth(ua: str, dev_key: Optional[str] = None) -> str:
        """★ Fix 72: 偽装は最小限にする。
        UA・sec-ch-ua・userAgentData・タッチ・Accept は Playwright のコンテキスト設定
        （user_agent + is_mobile + has_touch）で Chrome 自身に整合した値を出させる。
        旧実装は userAgentData を素のオブジェクトで差し替えており getHighEntropyValues が消えていた。
        ここでは Android 実機で値が変わる platform と、端末モデル名だけを補う。"""
        _model, _pver = _device_model_info(dev_key)
        _model = _model.replace("\\", "").replace("'", "")
        return (
            "try{Object.defineProperty(Navigator.prototype,'platform',{get:()=>'Linux aarch64',configurable:true});}catch(e){}"
            "if(!window.chrome){window.chrome={runtime:{}};}"
            "try{const P=(typeof NavigatorUAData!=='undefined')?NavigatorUAData.prototype:null;"
            "if(P&&P.getHighEntropyValues){const o=P.getHighEntropyValues;"
            "const f=function getHighEntropyValues(h){return o.call(this,h).then(r=>{"
            f"if(r&&'model' in r)r.model='{_model}';"
            f"if(r&&'platformVersion' in r)r.platformVersion='{_pver}';return r;}});}};"
            "Object.defineProperty(f,'toString',{value:()=>'function getHighEntropyValues() { [native code] }'});"
            "P.getHighEntropyValues=f;}}catch(e){}"
        )

    def _align_identity(self, browser, btype, ctx_opts: dict, ua: str,
                        channel: Optional[str], dev_key: Optional[str] = None) -> str:
        """★ Fix 72: 実際に起動したブラウザのバージョンに UA を合わせ、コンテキスト設定を整える。
        旧実装は Chrome 116〜130 固定の UA を実エンジン（140 以降）で名乗っており、
        TLS 指紋・JS 機能と UA が食い違って Cloudflare に検出されやすかった。"""
        self._identity_aligned = False
        try:
            if getattr(btype, "name", "") != "chromium" or "Android" not in (ua or ""):
                return ua
            major = int(str(browser.version).split(".")[0])
        except Exception:
            return ua
        new_ua = _reduced_android_ua(major)
        ctx_opts["user_agent"] = new_ua
        ctx_opts["is_mobile"]  = True
        ctx_opts["has_touch"]  = True
        ctx_opts.pop("extra_http_headers", None)   # sec-ch-ua 等は Chrome 自身に生成させる
        _brand = {"chrome": "Google Chrome", "msedge": "Microsoft Edge"}.get(channel or "")
        self.last_cf_ua        = new_ua
        self.last_cf_sec_ch_ua = _sec_ch_ua_for(major, _brand)
        self._identity_aligned = True
        self.logger.info(f"Fix 72: browser identity → Chrome/{major} Android (channel={channel or 'bundled'})")
        return new_ua

    def login_and_get_token(self, email: str, password: str,
                            proxy: Optional[str] = None,
                            stop_event: Optional[threading.Event] = None,
                            captcha_client=None) -> Optional[str]:
        """Browser-based login → returns accessToken JWT.
        If proxy is supplied but unreachable (ERR_PROXY_CONNECTION_FAILED),
        automatically retries without proxy.
        When cfg['debug_mode'] is True, saves HTML snapshots + HAR to debug/.
        stop_event: ★ Fix 28 — set するとポーリングを即時中断して None を返す。
        """
        if not _HAS_PW:
            self.logger.error("playwright not installed")
            return None

        debug    = self.cfg.get("debug_mode", False)
        dev_key, (ua, w, h, dpr) = pick_device(self.cfg)
        lat, lng  = _resolve_geo(self.cfg.get("geo", "kanto_random"))
        base_url  = self.cfg.get("base_url", "https://mellojoylucky.cn")
        br_type   = self.cfg.get("browser", "chromium")

        self.logger.info(f"CF bypass login: device={dev_key} geo=({lat:.4f},{lng:.4f})")

        # ★ Fix 38: プロキシエラー時は次のプロキシにローテーション（最大3回）
        #   proxies_list が設定されていれば pick_proxy() で次を取得。
        #   なければ従来通り None（プロキシなし）にフォールバック。
        _PROXY_MAX_RETRY = 3
        _PROXY_ERRORS    = (
            "ERR_PROXY_CONNECTION_FAILED",
            "ERR_EMPTY_RESPONSE",
            "ERR_CONNECTION_RESET",
            "ERR_CONNECTION_CLOSED",
            "ERR_TUNNEL_CONNECTION_FAILED",
        )
        _current_proxy = proxy
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        with _browser_lock:
            with sync_playwright() as pw:
                btype, channel = _resolve_browser(pw, br_type)

                for attempt in range(_PROXY_MAX_RETRY + 1):
                    use_proxy = _current_proxy
                    opts: dict = {
                        "headless": False,
                        "args": [
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            # ISP の DNS リゾルバが brunhild.challenges.cloudflare.com を
                            # 解決できない場合に備えて DoH (Cloudflare 1.1.1.1) を使用する
                            "--enable-features=DnsOverHttps",
                            "--dns-over-https-servers=https://1.1.1.1/dns-query",
                        ],
                    }
                    if channel:
                        opts["channel"] = channel
                    if use_proxy:
                        p = _parse_proxy(use_proxy)
                        proxy_server = f"{p['type']}://{p['address']}:{p['port']}"
                        pw_proxy: dict = {"server": proxy_server}
                        if "login"    in p: pw_proxy["username"] = p["login"]
                        if "password" in p: pw_proxy["password"] = p["password"]
                        pw_proxy["bypass"] = (
                            "*.challenges.cloudflare.com,"
                            "brunhild.challenges.cloudflare.com,"
                            "challenges.cloudflare.com"
                        )
                        opts["proxy"] = pw_proxy

                    # Context options (HAR recording when debug_mode is on)
                    ctx_opts: dict = {
                        "user_agent":          ua,
                        "viewport":            {"width": w, "height": h},
                        "device_scale_factor": dpr,
                        "locale":              "ja-JP",
                        "timezone_id":         "Asia/Tokyo",
                        "permissions":         ["geolocation"],
                        "geolocation":         {"latitude": lat, "longitude": lng, "accuracy": 10.0},
                        "extra_http_headers":  _ua_ch_headers(ua),  # ★ Fix 36
                    }
                    if debug:
                        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                        har_path = DEBUG_DIR / f"{ts}_login_a{attempt}.har"
                        ctx_opts["record_har_path"] = str(har_path)
                        self.logger.info(f"[DEBUG] HAR → {har_path.name}")

                    browser = btype.launch(**opts)
                    ua = self._align_identity(browser, btype, ctx_opts, ua, channel, dev_key)  # ★ Fix 72
                    ctx = browser.new_context(**ctx_opts)
                    ctx.add_init_script(self._stealth(ua, dev_key))
                    page = ctx.new_page()

                    # ★ Fix 40: page.route() で Accept を強制上書き
                    # extra_http_headers は Chromium がネイティブ生成する Accept を
                    # 置き換えられないため、リクエストインターセプトで確実に書き換える。
                    _accept_fix40 = _ua_ch_headers(ua)["Accept"]
                    def _fix_accept_login(route, request):
                        headers = {**request.headers, "accept": _accept_fix40}
                        route.continue_(headers=headers)
                    if not self._identity_aligned:   # ★ Fix 72
                        page.route("**/*", _fix_accept_login)

                    tokens: list[str] = []

                    def _on_resp(resp):
                        if "/api/user-auth/login" in resp.url and resp.status == 200:
                            try:
                                body = resp.json()
                                # ネスト形式 {"data":{"accessToken":...}} にも対応
                                _tk = (body.get("accessToken")
                                       or (body.get("data") or {}).get("accessToken")) \
                                      if isinstance(body, dict) else None
                                if _tk:
                                    tokens.append(_tk)
                            except Exception:
                                pass

                    page.on("response", _on_resp)

                    token: Optional[str] = None
                    _proxy_err = False   # ★ Fix 38: プロキシエラーフラグ
                    try:
                        page.goto(f"{base_url}/login", timeout=30_000,
                                  wait_until="domcontentloaded")
                        if debug:
                            _debug_dump(f"{ts}_login_a{attempt}_loaded", page.content())
                        page.wait_for_timeout(2000)

                        em_sel = ("input[type='email'],input[name='email'],"
                                  "input[placeholder*='メール'],input[placeholder*='Email'],"
                                  "input[placeholder*='メールアドレス']")
                        pw_sel = "input[type='password']"

                        page.wait_for_selector(em_sel, timeout=15_000)
                        page.fill(em_sel, email)
                        page.wait_for_timeout(300)
                        page.fill(pw_sel, password)
                        page.wait_for_timeout(500)

                        # ★ Fix 41: ログインページの Turnstile を検出して解決待機
                        # /api/turnstile-config が enabled:true を返す場合、
                        # React がボタンを disabled にするため先に Turnstile を解決する必要がある。
                        # iframe が DOM に出現してから token 書き込みを待つ（最大 45s）。
                        _ts_login_sel = (
                            "iframe[src*='challenges.cloudflare.com'],"
                            "div.cf-turnstile,"
                            "div[data-sitekey]"
                        )
                        _cf_login_sel = "input[name='cf-turnstile-response']"
                        try:
                            page.wait_for_selector(_ts_login_sel, timeout=10_000)
                            self.logger.info("Login page: Turnstile widget detected → waiting for solve")
                            for _ in range(45):
                                if stop_event is not None and stop_event.is_set():
                                    self.logger.info("login_and_get_token: stop_event → Turnstile待機中断")
                                    break
                                try:
                                    v = page.input_value(_cf_login_sel, timeout=500)
                                    if v and len(v) > 20:
                                        self.logger.info("Login page Turnstile solved ✓")
                                        break
                                except Exception:
                                    pass
                                page.wait_for_timeout(1000)
                        except Exception:
                            pass  # Turnstile なし / タイムアウト → そのまま進む

                        # ★ Fix 42: page.evaluate() でブラウザコンテキストから直接 fetch
                        # 理由: enabled:true Turnstile + invisible widget がヘッドレスで解決されず
                        #       React が cf-turnstile-response なしの submit をブロックするため。
                        # ブラウザ内 fetch はページ Cookie/セッションを引き継ぎ Cloudflare WAF を通過できる。
                        _fix42_ok = False
                        try:
                            _r42 = page.evaluate("""async ([em, pw]) => {
                                try {
                                    const resp = await fetch('/api/user-auth/login', {
                                        method: 'POST',
                                        headers: {
                                            'Content-Type': 'application/json',
                                            'Accept': 'application/json'
                                        },
                                        body: JSON.stringify({email: em, password: pw}),
                                        credentials: 'include',
                                    });
                                    const body = await resp.json().catch(() => ({}));
                                    return {
                                        status: resp.status,
                                        token: (body.accessToken || (body.data && body.data.accessToken) || null),
                                        body: body
                                    };
                                } catch(e) {
                                    return {error: String(e)};
                                }
                            }""", [email, password])
                            if _r42 and _r42.get("token"):
                                tokens.append(_r42["token"])
                                _fix42_ok = True
                                self.logger.info("Fix 42: direct fetch login ✓")
                            else:
                                _status = (_r42 or {}).get("status", "?")
                                _body_s = str((_r42 or {}).get("body", {}))[:160]
                                _err    = (_r42 or {}).get("error", "")
                                self.logger.info(
                                    f"Fix 42: direct fetch → status={_status} "
                                    f"body={_body_s} err={_err}"
                                )
                        except Exception as _e42:
                            self.logger.warning(f"Fix 42 direct fetch 例外: {_e42}")

                        # ★ Fix 42-B: Fix 42-A 失敗 かつ Capsolver 等が設定済みの場合
                        # → Turnstile トークンを取得して cf-turnstile-response に inject → 再 fetch
                        if not _fix42_ok and captcha_client is not None:
                            _login_sitekey = "0x4AAAAAAFF0MPh5SYT7ywAo"
                            try:
                                # ★ Fix 48: captcha_client はチェーン(list)または単一オブジェクト
                                _cc42_chain = captcha_client if isinstance(captcha_client, list) else [captcha_client]
                                _ts42b = _solve_turnstile_chain(
                                    _cc42_chain, f"{base_url}/login", _login_sitekey, proxy=proxy
                                )
                                if _ts42b:
                                    self.logger.info(
                                        f"Fix 42-B: Capsolver token OK ({len(_ts42b)} chars)"
                                    )
                                    # cf-turnstile-response hidden input を inject（存在しなければ作成）
                                    page.evaluate("""(token) => {
                                        let inp = document.querySelector(
                                            'input[name="cf-turnstile-response"]');
                                        if (!inp) {
                                            inp = document.createElement('input');
                                            inp.type = 'hidden';
                                            inp.name = 'cf-turnstile-response';
                                            const form = document.querySelector('form');
                                            if (form) form.appendChild(inp);
                                            else document.body.appendChild(inp);
                                        }
                                        inp.value = token;
                                        inp.dispatchEvent(
                                            new Event('input', {bubbles: true}));
                                        inp.dispatchEvent(
                                            new Event('change', {bubbles: true}));
                                    }""", _ts42b)
                                    page.wait_for_timeout(400)
                                    # inject 後に再 fetch
                                    _r42b = page.evaluate("""async ([em, pw]) => {
                                        try {
                                            const resp = await fetch('/api/user-auth/login', {
                                                method: 'POST',
                                                headers: {
                                                    'Content-Type': 'application/json',
                                                    'Accept': 'application/json'
                                                },
                                                body: JSON.stringify({email: em, password: pw}),
                                                credentials: 'include',
                                            });
                                            const body = await resp.json().catch(() => ({}));
                                            return {
                                                status: resp.status,
                                                token: (body.accessToken || (body.data && body.data.accessToken) || null),
                                                body: body
                                            };
                                        } catch(e) {
                                            return {error: String(e)};
                                        }
                                    }""", [email, password])
                                    if _r42b and _r42b.get("token"):
                                        tokens.append(_r42b["token"])
                                        _fix42_ok = True
                                        self.logger.info(
                                            "Fix 42-B: Turnstile inject + direct fetch ✓"
                                        )
                                    else:
                                        _s42b = str((_r42b or {}).get("status", "?"))
                                        _b42b = str((_r42b or {}).get("body", {}))[:160]
                                        self.logger.warning(
                                            f"Fix 42-B: fetch still failed "
                                            f"status={_s42b} body={_b42b}"
                                        )
                                else:
                                    self.logger.warning("Fix 42-B: Capsolver returned no token")
                            except Exception as _e42b:
                                self.logger.warning(f"Fix 42-B Capsolver error: {_e42b}")

                        if not _fix42_ok:
                            # フォールバック: 従来のフォームボタンクリック
                            btn = page.query_selector(
                                "button[type='submit'],button:has-text('ログイン'),button:has-text('Login')"
                            )
                            if btn:
                                btn.click()
                            else:
                                page.keyboard.press("Enter")

                        # ★ Fix 28: stop_event チェックを追加
                        for _ in range(30):
                            if tokens: break
                            if stop_event is not None and stop_event.is_set():
                                self.logger.info("login_and_get_token: stop_event → ログイン応答待ち中断")
                                break
                            page.wait_for_timeout(500)

                        token = tokens[-1] if tokens else None
                        if debug and not token:
                            _debug_dump(f"{ts}_login_a{attempt}_no_token", page.content())

                        # ★ Fix 46: cf_clearance をブラウザコンテキストから抽出して保持
                        # JSD challenge が完了していれば cf_clearance が ctx.cookies() に存在する。
                        try:
                            for _c46 in ctx.cookies():
                                if _c46.get("name") == "cf_clearance":
                                    self.last_cf_clearance = _c46["value"]
                                    self.logger.info(
                                        f"Fix 46: cf_clearance extracted from login browser "
                                        f"(len={len(_c46['value'])})"
                                    )
                                    # ★ Fix 62: グローバルキャッシュに共有保存
                                    _set_shared_cf_clearance(proxy, self.last_cf_clearance, self.last_cf_ua, self.last_cf_sec_ch_ua)  # ★ Fix 72
                                    self.logger.info("Fix 62: cf_clearance → shared cache (login browser)")
                                    break
                        except Exception as _e46:
                            self.logger.warning(f"Fix 46: cf_clearance 抽出エラー: {_e46}")

                    except PWTimeout as e:
                        if debug:
                            try: _debug_dump(f"{ts}_login_a{attempt}_timeout", page.content())
                            except Exception: pass
                        self.logger.warning(f"Browser timeout: {e}")
                    except Exception as e:
                        if use_proxy and any(pe in str(e) for pe in _PROXY_ERRORS):
                            # ★ Fix 38: プロキシエラー → ローテーション
                            if self.proxies_list:
                                _current_proxy = pick_proxy(self.proxies_list)
                                self.logger.warning(
                                    f"Login proxy error → rotating proxy "
                                    f"(attempt {attempt + 1}/{_PROXY_MAX_RETRY}): {e}"
                                )
                            else:
                                _current_proxy = None   # リストなし → プロキシなしフォールバック
                                self.logger.warning(
                                    f"Login proxy error → retrying without proxy: {e}"
                                )
                            _proxy_err = True
                        else:
                            self.logger.error(f"Browser error: {e}")
                    finally:
                        ctx.close()    # flush HAR before browser.close()
                        browser.close()

                    if token:
                        return token
                    if not _proxy_err:
                        break   # プロキシエラーでない → リトライしない

        return None

    def solve_turnstile_browser(self, page_url: str, sitekey: str,
                                proxy: Optional[str] = None,
                                stop_event: Optional[threading.Event] = None,
                                email: Optional[str] = None,
                                password: Optional[str] = None) -> Optional[str]:
        """Open browser, navigate to page, poll the hidden cf-turnstile-response
        field until Turnstile writes its solved token into it.
        If proxy is supplied but unreachable (ERR_PROXY_CONNECTION_FAILED),
        automatically retries without proxy.
        When cfg['debug_mode'] is True, saves HTML snapshots + HAR to debug/.
        Note: requires brunhild.challenges.cloudflare.com to be resolvable on the
        local network; if that DNS entry fails, Turnstile will never complete and
        this method returns None — use YesCaptcha as the primary solve path.
        stop_event: ★ Fix 26 — set するとポーリングを即時中断して None を返す。
        """
        if not _HAS_PW:
            return None

        debug    = self.cfg.get("debug_mode", False)
        dev_key, (ua, w, h, dpr) = pick_device(self.cfg)
        lat, lng  = _resolve_geo(self.cfg.get("geo", "kanto_random"))
        br_type   = self.cfg.get("browser", "chromium")
        base_url  = self.cfg.get("base_url", "https://mellojoylucky.cn")

        # ★ Fix 38: プロキシエラー時はローテーション（最大3回）
        _PROXY_MAX_RETRY = 3
        _PROXY_ERRORS    = (
            "ERR_PROXY_CONNECTION_FAILED",
            "ERR_EMPTY_RESPONSE",
            "ERR_CONNECTION_RESET",
            "ERR_CONNECTION_CLOSED",
            "ERR_TUNNEL_CONNECTION_FAILED",
        )
        _current_proxy = proxy
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        with _browser_lock:
            with sync_playwright() as pw:
                btype, channel = _resolve_browser(pw, br_type)

                for attempt in range(_PROXY_MAX_RETRY + 1):
                    use_proxy = _current_proxy
                    opts: dict = {
                        "headless": False,
                        "args": [
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            # ISP の DNS リゾルバが brunhild.challenges.cloudflare.com を
                            # 解決できない場合に備えて DoH (Cloudflare 1.1.1.1) を使用する
                            "--enable-features=DnsOverHttps",
                            "--dns-over-https-servers=https://1.1.1.1/dns-query",
                        ],
                    }
                    if channel:
                        opts["channel"] = channel
                    if use_proxy:
                        p = _parse_proxy(use_proxy)
                        proxy_server = f"{p['type']}://{p['address']}:{p['port']}"
                        pw_proxy: dict = {"server": proxy_server}
                        if "login"    in p: pw_proxy["username"] = p["login"]
                        if "password" in p: pw_proxy["password"] = p["password"]
                        # brunhild.challenges.cloudflare.com は Cloudflare Turnstile の
                        # PoW (Proof-of-Work) サブドメイン。プロキシ経由だと到達できない
                        # ことが多いため、challenges.cloudflare.com 系を直接接続に。
                        # DoH フラグ（起動引数）と組み合わせて DNS も確実に解決する。
                        pw_proxy["bypass"] = (
                            "*.challenges.cloudflare.com,"
                            "brunhild.challenges.cloudflare.com,"
                            "challenges.cloudflare.com"
                        )
                        opts["proxy"] = pw_proxy

                    # Context options (HAR recording when debug_mode is on)
                    ctx_opts: dict = {
                        "user_agent":          ua,
                        "viewport":            {"width": w, "height": h},
                        "device_scale_factor": dpr,
                        "locale":              "ja-JP",
                        "timezone_id":         "Asia/Tokyo",
                        "permissions":         ["geolocation"],
                        "geolocation":         {"latitude": lat, "longitude": lng, "accuracy": 10.0},
                        "extra_http_headers":  _ua_ch_headers(ua),  # ★ Fix 36
                    }
                    if debug:
                        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                        har_path = DEBUG_DIR / f"{ts}_turnstile_a{attempt}.har"
                        ctx_opts["record_har_path"] = str(har_path)
                        self.logger.info(f"[DEBUG] HAR → {har_path.name}")

                    browser = btype.launch(**opts)
                    ua = self._align_identity(browser, btype, ctx_opts, ua, channel, dev_key)  # ★ Fix 72
                    ctx = browser.new_context(**ctx_opts)
                    ctx.add_init_script(self._stealth(ua, dev_key))
                    page = ctx.new_page()

                    # ★ Fix 40: page.route() で Accept を強制上書き
                    # extra_http_headers は Chromium がネイティブ生成する Accept を
                    # 置き換えられないため、リクエストインターセプトで確実に書き換える。
                    _accept_fix40_ts = _ua_ch_headers(ua)["Accept"]
                    def _fix_accept_ts(route, request):
                        headers = {**request.headers, "accept": _accept_fix40_ts}
                        route.continue_(headers=headers)
                    if not self._identity_aligned:   # ★ Fix 72
                        page.route("**/*", _fix_accept_ts)

                    # Selector for the hidden field Turnstile writes its token into
                    CF_SEL = "input[name='cf-turnstile-response']"

                    token: Optional[str] = None
                    _proxy_err = False
                    try:
                        # ★ Fix 70: email/password がある場合は book_via_browser と同じフローで
                        #   /login → JS fetch ログイン → page_url (/) の順に遷移する。
                        #   （旧: "/" に先に遷移してからログイン → CF チャレンジ後にログインページが
                        #    出ると JS fetch がブロックされることがあったため逆順に変更）
                        _ts_sel = (
                            "iframe[src*='challenges.cloudflare.com'],"
                            "iframe[title*='Cloudflare'],"
                            "div.cf-turnstile,"
                            "div[data-sitekey]"
                        )
                        if email and password:
                            # ① /login に遷移（book_via_browser と同じ起点）
                            try:
                                page.goto(f"{base_url}/login", timeout=30_000,
                                          wait_until="domcontentloaded")
                                page.wait_for_timeout(2000)   # book_via_browser と同じ 2s
                            except Exception as _ge:
                                self.logger.warning(f"solve_turnstile_browser: /login 遷移失敗: {_ge}")

                            # ② JS fetch でログイン（エンドポイント /api/user-auth/login — Fix 70b）
                            try:
                                _r_stb_login = page.evaluate("""async ([em, pw]) => {
                                    try {
                                        const resp = await fetch('/api/user-auth/login', {
                                            method: 'POST',
                                            headers: {
                                                'Content-Type': 'application/json',
                                                'Accept': 'application/json'
                                            },
                                            body: JSON.stringify({email: em, password: pw}),
                                            credentials: 'include',
                                        });
                                        const body = await resp.json().catch(() => ({}));
                                        return {status: resp.status, token: (body.accessToken || (body.data && body.data.accessToken) || null)};
                                    } catch(e) {
                                        return {error: String(e)};
                                    }
                                }""", [email, password])
                                _at_stb = (_r_stb_login or {}).get("token")
                                if _at_stb:
                                    self.logger.info(
                                        f"solve_turnstile_browser: JS fetch ログイン成功 "
                                        f"→ {page_url} に遷移")
                                else:
                                    self.logger.warning(
                                        f"solve_turnstile_browser: JS fetch ログイン失敗 "
                                        f"status={(_r_stb_login or {}).get('status')} "
                                        f"err={(_r_stb_login or {}).get('error')}")
                            except Exception as _le:
                                self.logger.warning(f"solve_turnstile_browser: ログイン例外: {_le}")

                            # ③ page_url (/) に遷移 → CF チャレンジ → Turnstile
                            page.goto(page_url, timeout=30_000,
                                      wait_until="domcontentloaded")
                            page.wait_for_timeout(2000)   # book_via_browser と同じ 2s
                        else:
                            # ★ Fix 46: email/password なしの場合は直接 page_url に遷移
                            page.goto(page_url, timeout=30_000,
                                      wait_until="domcontentloaded")

                        if debug:
                            _debug_dump(f"{ts}_turnstile_a{attempt}_loaded", page.content())

                        # React アプリが mount して turnstile.render() を呼ぶまで待機。

                        try:
                            page.wait_for_selector(_ts_sel, timeout=25_000)
                            self.logger.info("Turnstile widget appeared in DOM")
                            page.wait_for_timeout(500)   # レンダリング安定待ち
                        except Exception:
                            self.logger.warning(
                                "Turnstile widget not found within 25 s — "
                                "React may still be loading, continuing to poll..."
                            )
                            if debug:
                                _debug_dump(f"{ts}_turnstile_a{attempt}_no_widget", page.content())

                        # 「利用規約に同意します」チェックボックスをクリック。
                        # ボタン全体をクリックすると内部の《プライバシーポリシー》
                        # リンクに当たってページ遷移してしまうため、
                        # ボタンの最初の子 span（チェックボックス枠）だけを狙う。
                        try:
                            chk_span = page.query_selector(
                                "button:has-text('利用規約') > span:first-child"
                            )
                            if chk_span:
                                chk_span.click(timeout=3000)
                                page.wait_for_timeout(500)
                        except Exception:
                            pass

                        # Poll the hidden cf-turnstile-response field until it fills
                        # (Turnstile writes the token there on success).
                        # Timeout: 90 s — Cloudflare can take a while on slow networks.
                        # ★ Fix 26: stop_event が set されたらポーリングを即時中断する。
                        for _ in range(90):
                            if stop_event is not None and stop_event.is_set():
                                self.logger.info("solve_turnstile_browser: stop_event → 中断")
                                break
                            try:
                                val = page.input_value(CF_SEL, timeout=500)
                                if val and len(val) > 20:
                                    token = val
                                    break
                            except Exception:
                                pass
                            # 1s 待機を 10 × 100ms に分割し、各 100ms 後に stop_event を確認する
                            for _ in range(10):
                                if stop_event is not None and stop_event.is_set():
                                    break
                                page.wait_for_timeout(100)

                        if not token:
                            self.logger.warning(
                                "Turnstile hidden field still empty after 90 s — "
                                "possible DNS failure (brunhild.challenges.cloudflare.com)"
                            )
                        if debug and not token:
                            _debug_dump(f"{ts}_turnstile_a{attempt}_no_token", page.content())

                        # ★ Fix 46: cf_clearance をブラウザコンテキストから抽出して保持
                        # JSD challenge が完了していれば cf_clearance が ctx.cookies() に存在する。
                        # （トークン取得失敗でも cf_clearance は利用できる可能性がある）
                        try:
                            for _c46 in ctx.cookies():
                                if _c46.get("name") == "cf_clearance":
                                    self.last_cf_clearance = _c46["value"]
                                    self.logger.info(
                                        f"Fix 46: cf_clearance extracted from Turnstile browser "
                                        f"(len={len(_c46['value'])})"
                                    )
                                    # ★ Fix 62: グローバルキャッシュに共有保存
                                    _set_shared_cf_clearance(proxy, self.last_cf_clearance, self.last_cf_ua, self.last_cf_sec_ch_ua)  # ★ Fix 72
                                    self.logger.info("Fix 62: cf_clearance → shared cache (Turnstile browser)")
                                    break
                        except Exception as _e46:
                            self.logger.warning(f"Fix 46: cf_clearance 抽出エラー (Turnstile): {_e46}")

                    except PWTimeout as e:
                        if debug:
                            try: _debug_dump(f"{ts}_turnstile_a{attempt}_timeout", page.content())
                            except Exception: pass
                        self.logger.warning(f"Browser timeout (Turnstile): {e}")
                    except Exception as e:
                        if use_proxy and any(pe in str(e) for pe in _PROXY_ERRORS):
                            # ★ Fix 38: プロキシエラー → ローテーション
                            if self.proxies_list:
                                _current_proxy = pick_proxy(self.proxies_list)
                                self.logger.warning(
                                    f"Turnstile proxy error → rotating proxy "
                                    f"(attempt {attempt + 1}/{_PROXY_MAX_RETRY}): {e}"
                                )
                            else:
                                _current_proxy = None
                                self.logger.warning(
                                    f"Turnstile proxy error → retrying without proxy: {e}"
                                )
                            _proxy_err = True
                        else:
                            self.logger.error(f"Browser Turnstile error: {e}")
                    finally:
                        ctx.close()    # flush HAR before browser.close()
                        browser.close()

                    if token:
                        return token
                    if not _proxy_err:
                        break   # プロキシエラーでない → リトライしない

        return None

    # ── ブラウザモード予約 ────────────────────────────────────────────────────
    def book_via_browser(self, email: str, password: str,
                         slot: dict,
                         lat: float, lng: float,
                         proxy: Optional[str] = None,
                         stop_event: Optional[threading.Event] = None) -> Optional[str]:
        """
        ★ ブラウザモード予約:
          Playwright ブラウザを起動 → サイトに遷移（Cloudflare/JS チャレンジをネイティブ通過）
          → GPS 許可ポップアップを geolocation 設定で自動承認
          → Turnstile をブラウザネイティブで自動通過（capsolver 不要）
          → ブラウザ内 JS fetch で /api/registrations を POST

        Returns: "won" / "not_won" / "cooldown" / "net_error" / None（RATE_LIMIT）
        """
        if not _HAS_PW:
            self.logger.error("browser_mode: playwright not installed")
            return "net_error"

        dev_key, (ua, w, h, dpr) = pick_device(self.cfg)
        base_url = self.cfg.get("base_url", "https://mellojoylucky.cn")
        br_type  = self.cfg.get("browser", "chromium")

        self.logger.info(
            f"browser_mode: book start slot={slot.get('slotDate')} "
            f"idx={slot.get('slotIndex')} geo=({lat:.4f},{lng:.4f})"
        )

        _PROXY_MAX_RETRY = 3
        _PROXY_ERRORS    = (
            "ERR_PROXY_CONNECTION_FAILED", "ERR_EMPTY_RESPONSE",
            "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED",
            "ERR_TUNNEL_CONNECTION_FAILED",
        )
        _current_proxy = proxy

        with _browser_lock:
            with sync_playwright() as pw:
                btype, channel = _resolve_browser(pw, br_type)

                for attempt in range(_PROXY_MAX_RETRY + 1):
                    use_proxy = _current_proxy
                    opts: dict = {
                        "headless": False,
                        "args": [
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--enable-features=DnsOverHttps",
                            "--dns-over-https-servers=https://1.1.1.1/dns-query",
                        ],
                    }
                    if channel:
                        opts["channel"] = channel
                    if use_proxy:
                        p = _parse_proxy(use_proxy)
                        proxy_server = f"{p['type']}://{p['address']}:{p['port']}"
                        pw_proxy: dict = {"server": proxy_server}
                        if "login"    in p: pw_proxy["username"] = p["login"]
                        if "password" in p: pw_proxy["password"] = p["password"]
                        pw_proxy["bypass"] = (
                            "*.challenges.cloudflare.com,"
                            "brunhild.challenges.cloudflare.com,"
                            "challenges.cloudflare.com"
                        )
                        opts["proxy"] = pw_proxy

                    ctx_opts: dict = {
                        "user_agent":          ua,
                        "viewport":            {"width": w, "height": h},
                        "device_scale_factor": dpr,
                        "locale":              "ja-JP",
                        "timezone_id":         "Asia/Tokyo",
                        # ★ GPS 許可ポップアップを自動承認
                        "permissions":         ["geolocation"],
                        "geolocation":         {"latitude": lat, "longitude": lng, "accuracy": 10.0},
                        "extra_http_headers":  _ua_ch_headers(ua),
                    }

                    browser = btype.launch(**opts)
                    ua = self._align_identity(browser, btype, ctx_opts, ua, channel, dev_key)  # ★ Fix 72
                    ctx = browser.new_context(**ctx_opts)
                    ctx.add_init_script(self._stealth(ua, dev_key))
                    page = ctx.new_page()

                    # Accept ヘッダー修正（Fix 40 と同様）
                    _accept_hdr = _ua_ch_headers(ua)["Accept"]
                    def _fix_accept_bm(route, request):
                        headers = {**request.headers, "accept": _accept_hdr}
                        route.continue_(headers=headers)
                    if not self._identity_aligned:   # ★ Fix 72
                        page.route("**/*", _fix_accept_bm)

                    _proxy_err  = False
                    book_result = "net_error"

                    try:
                        # ① ログイン（Fix 42: JS fetch で直接 API を叩く）
                        page.goto(f"{base_url}/login", timeout=30_000,
                                  wait_until="domcontentloaded")
                        page.wait_for_timeout(2000)

                        _r_login = page.evaluate("""async ([em, pw]) => {
                            try {
                                const resp = await fetch('/api/user-auth/login', {
                                    method: 'POST',
                                    headers: {
                                        'Content-Type': 'application/json',
                                        'Accept': 'application/json'
                                    },
                                    body: JSON.stringify({email: em, password: pw}),
                                    credentials: 'include',
                                });
                                const body = await resp.json().catch(() => ({}));
                                return {status: resp.status, token: (body.accessToken || (body.data && body.data.accessToken) || null)};
                            } catch(e) {
                                return {error: String(e)};
                            }
                        }""", [email, password])

                        access_token = (_r_login or {}).get("token")
                        if not access_token:
                            self.logger.error(
                                f"browser_mode: login failed "
                                f"status={(_r_login or {}).get('status')} "
                                f"err={(_r_login or {}).get('error')}"
                            )
                            ctx.close()
                            browser.close()
                            return "net_error"

                        self.logger.info("browser_mode: login ✓")

                        # ② 予約ページ（/）に遷移 → Cloudflare チャレンジを自動通過
                        #   GPS 許可ポップアップ → geolocation 設定で自動承認済み
                        page.goto(f"{base_url}/", timeout=30_000,
                                  wait_until="domcontentloaded")
                        page.wait_for_timeout(2000)

                        # ③ Turnstile を待機（ブラウザネイティブで自動解決）
                        _CF_RESP_SEL = "input[name='cf-turnstile-response']"
                        _cf_ts_tok: Optional[str] = None
                        try:
                            page.wait_for_selector(_CF_RESP_SEL, timeout=5_000)
                            for _ in range(60):  # 最大 60s 待機
                                if stop_event is not None and stop_event.is_set():
                                    ctx.close(); browser.close()
                                    return "net_error"
                                try:
                                    v = page.input_value(_CF_RESP_SEL, timeout=500)
                                    if v and len(v) > 20:
                                        _cf_ts_tok = v
                                        self.logger.info(
                                            f"browser_mode: Turnstile solved ✓ "
                                            f"(len={len(v)})"
                                        )
                                        break
                                except Exception:
                                    pass
                                for _ in range(10):
                                    if stop_event is not None and stop_event.is_set():
                                        break
                                    page.wait_for_timeout(100)
                        except Exception:
                            # Turnstile ウィジェットなし → そのまま続行
                            self.logger.debug("browser_mode: Turnstile widget not found → proceed without token")

                        if not _cf_ts_tok:
                            self.logger.warning(
                                "browser_mode: Turnstile token 未取得 — "
                                "turnstileToken なしで POST を試みます"
                            )

                        # cf_clearance を共有キャッシュに保存（Fix 62 と同様）
                        try:
                            for _c in ctx.cookies():
                                if _c.get("name") == "cf_clearance":
                                    self.last_cf_clearance = _c["value"]
                                    _set_shared_cf_clearance(proxy, self.last_cf_clearance, self.last_cf_ua, self.last_cf_sec_ch_ua)  # ★ Fix 72
                                    self.logger.info(
                                        f"browser_mode: cf_clearance extracted "
                                        f"(len={len(_c['value'])})"
                                    )
                                    break
                        except Exception as _e_cf:
                            self.logger.debug(f"browser_mode: cf_clearance 抽出スキップ: {_e_cf}")

                        # ④ 予約 POST（ブラウザ内 JS fetch）
                        fp = f"{uuid.uuid4().hex[:8]}-{random.randint(1000, 9999)}"
                        _book_body: dict = {
                            "store_id":          int(self.cfg.get("store_id", 2)),   # ★ 必須
                            "slotDate":          slot.get("slotDate", ""),
                            "slotIndex":         slot.get("slotIndex", 0),
                            "deviceFingerprint": fp,
                            "gpsLat":            round(lat, 4),
                            "gpsLng":            round(lng, 4),
                        }
                        if _cf_ts_tok:
                            _book_body["turnstileToken"] = _cf_ts_tok

                        _book_r = page.evaluate("""async ([tok, body]) => {
                            try {
                                const resp = await fetch('/api/registrations', {
                                    method: 'POST',
                                    headers: {
                                        'Authorization': 'Bearer ' + tok,
                                        'Content-Type': 'application/json',
                                        'Accept': 'application/json'
                                    },
                                    body: JSON.stringify(body),
                                    credentials: 'include',
                                });
                                const data = await resp.json().catch(() => ({}));
                                return {
                                    status: resp.status,
                                    result: data.result || '',
                                    code:   data.code   || ''
                                };
                            } catch(e) {
                                return {error: String(e)};
                            }
                        }""", [access_token, _book_body])

                        _bstatus = (_book_r or {}).get("status", 0)
                        _bresult = (_book_r or {}).get("result", "")
                        _bcode   = (_book_r or {}).get("code",   "")
                        _berr    = (_book_r or {}).get("error",  "")

                        self.logger.info(
                            f"browser_mode: book → status={_bstatus} "
                            f"result={_bresult!r} code={_bcode!r} "
                            f"slot={slot.get('slotDate')} idx={slot.get('slotIndex')}"
                        )

                        if _berr:
                            book_result = "net_error"
                        elif _bstatus == 409:
                            if _bcode in ("COOLDOWN", "SYSTEM_BUSY"):
                                book_result = "cooldown"
                            elif _bcode in ("SLOT_ENDED", "SLOT_FULL", "REGISTRATION_CLOSED"):
                                book_result = "not_won"
                            else:
                                book_result = None   # RATE_LIMIT
                        elif _bstatus in (200, 201):
                            if _bresult in ("won", "not_won"):
                                book_result = _bresult
                            else:
                                book_result = "not_won"
                        else:
                            book_result = "net_error"

                    except Exception as e:
                        err_s = str(e)
                        if use_proxy and any(pe in err_s for pe in _PROXY_ERRORS):
                            if self.proxies_list:
                                _current_proxy = pick_proxy(self.proxies_list)
                                self.logger.warning(
                                    f"browser_mode: proxy error → rotating "
                                    f"(attempt {attempt + 1}/{_PROXY_MAX_RETRY}): {e}"
                                )
                            else:
                                _current_proxy = None
                                self.logger.warning(
                                    f"browser_mode: proxy error → retrying without proxy: {e}"
                                )
                            _proxy_err = True
                        else:
                            self.logger.error(f"browser_mode: exception: {e}")
                    finally:
                        try: ctx.close()
                        except Exception: pass
                        try: browser.close()
                        except Exception: pass

                    if not _proxy_err:
                        return book_result
                    # プロキシエラーの場合はリトライ

        return "net_error"

    # ── ブラウザスタンバイモード（事前起動 + 待機 + 即攻撃） ─────────────────────
    def book_via_browser_standby(self,
                                  email: str, password: str,
                                  lat: float, lng: float,
                                  standby: "_BrowserStandby",
                                  proxy: Optional[str] = None,
                                  stop_event: Optional[threading.Event] = None) -> None:
        """
        ブラウザスタンバイモード（別スレッドで実行）:
          Phase 1: ブラウザ起動 → ログイン → "/" 遷移 → CF/Turnstile 通過
                   → standby.ready_event.set() で準備完了通知
          Phase 2: standby.cmd_queue.get() でスロット受信を待機（ブロッキング）
          Phase 3: 最新トークンで即 POST → standby.res_queue.put(result)
                   → ページリロード → Turnstile 再取得 → ready_event.set() で再スタンバイ
                   → Phase 2 へ戻る（当選 or 停止で終了）
        """
        if not _HAS_PW:
            self.logger.error("browser_standby: playwright not installed")
            standby.res_queue.put("net_error")
            return

        dev_key, (ua, w, h, dpr) = pick_device(self.cfg)
        base_url = self.cfg.get("base_url", "https://mellojoylucky.cn")
        br_type  = self.cfg.get("browser", "chromium")
        _stop    = stop_event or threading.Event()

        _PROXY_ERRORS = (
            "ERR_PROXY_CONNECTION_FAILED", "ERR_EMPTY_RESPONSE",
            "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED",
            "ERR_TUNNEL_CONNECTION_FAILED",
        )
        _CF_RESP_SEL = "input[name='cf-turnstile-response']"

        def _wait_turnstile(page, timeout_s: int = 60) -> Optional[str]:
            """Turnstile トークンをポーリング（最大 timeout_s 秒）"""
            try:
                page.wait_for_selector(_CF_RESP_SEL, timeout=5_000)
            except Exception:
                return None
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if _stop.is_set():
                    return None
                try:
                    v = page.input_value(_CF_RESP_SEL, timeout=500)
                    if v and len(v) > 20:
                        return v
                except Exception:
                    pass
                page.wait_for_timeout(200)
            return None

        def _do_post(page, access_token: str, slot: dict, ts_tok: Optional[str]) -> Optional[str]:
            """JS fetch で /api/registrations を POST して結果を返す"""
            fp = f"{uuid.uuid4().hex[:8]}-{random.randint(1000, 9999)}"
            body: dict = {
                "store_id":          int(self.cfg.get("store_id", 2)),   # ★ 必須
                "slotDate":          slot.get("slotDate", ""),
                "slotIndex":         slot.get("slotIndex", 0),
                "deviceFingerprint": fp,
                "gpsLat":            round(lat, 4),
                "gpsLng":            round(lng, 4),
            }
            if ts_tok:
                body["turnstileToken"] = ts_tok
            try:
                r = page.evaluate("""async ([tok, body]) => {
                    try {
                        const resp = await fetch('/api/registrations', {
                            method: 'POST',
                            headers: {
                                'Authorization': 'Bearer ' + tok,
                                'Content-Type': 'application/json',
                                'Accept': 'application/json'
                            },
                            body: JSON.stringify(body),
                            credentials: 'include',
                        });
                        const data = await resp.json().catch(() => ({}));
                        return {status: resp.status, result: data.result||'', code: data.code||''};
                    } catch(e) { return {error: String(e)}; }
                }""", [access_token, body])
            except Exception as e:
                self.logger.error(f"browser_standby: _do_post exception: {e}")
                return "net_error"
            s = (r or {}).get("status", 0)
            rv = (r or {}).get("result", "")
            rc = (r or {}).get("code",   "")
            re_ = (r or {}).get("error",  "")
            self.logger.info(
                f"browser_standby: POST → status={s} result={rv!r} code={rc!r} "
                f"slot={slot.get('slotDate')} idx={slot.get('slotIndex')}"
            )
            if re_:  return "net_error"
            if s == 409:
                if rc in ("COOLDOWN", "SYSTEM_BUSY"):       return "cooldown"
                if rc in ("SLOT_ENDED", "SLOT_FULL", "REGISTRATION_CLOSED"): return "not_won"
                return None   # RATE_LIMIT
            if s in (200, 201):
                return rv if rv in ("won", "not_won") else "not_won"
            return "net_error"

        with _browser_lock:
            with sync_playwright() as pw:
                btype, channel = _resolve_browser(pw, br_type)
                opts: dict = {
                    "headless": False,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--enable-features=DnsOverHttps",
                        "--dns-over-https-servers=https://1.1.1.1/dns-query",
                    ],
                }
                if channel:
                    opts["channel"] = channel
                if proxy:
                    p = _parse_proxy(proxy)
                    pw_proxy: dict = {
                        "server": f"{p['type']}://{p['address']}:{p['port']}",
                        "bypass": (
                            "*.challenges.cloudflare.com,"
                            "brunhild.challenges.cloudflare.com,"
                            "challenges.cloudflare.com"
                        ),
                    }
                    if "login"    in p: pw_proxy["username"] = p["login"]
                    if "password" in p: pw_proxy["password"] = p["password"]
                    opts["proxy"] = pw_proxy

                ctx_opts: dict = {
                    "user_agent":          ua,
                    "viewport":            {"width": w, "height": h},
                    "device_scale_factor": dpr,
                    "locale":              "ja-JP",
                    "timezone_id":         "Asia/Tokyo",
                    "permissions":         ["geolocation"],
                    "geolocation":         {"latitude": lat, "longitude": lng, "accuracy": 10.0},
                    "extra_http_headers":  _ua_ch_headers(ua),
                }

                browser = btype.launch(**opts)
                ua = self._align_identity(browser, btype, ctx_opts, ua, channel, dev_key)  # ★ Fix 72
                ctx = browser.new_context(**ctx_opts)
                ctx.add_init_script(self._stealth(ua, dev_key))
                page = ctx.new_page()
                _accept_hdr = _ua_ch_headers(ua).get("Accept", "")
                def _fix_accept_sb(route, request):
                    # ★ Fix 72: 旧実装はリクエストのたびに page.route() を追加登録していた
                    route.continue_(headers={**request.headers, "accept": _accept_hdr})
                if not self._identity_aligned:   # ★ Fix 72
                    page.route("**/*", _fix_accept_sb)

                access_token: Optional[str] = None
                cf_ts_tok:    Optional[str] = None

                try:
                    # ── Phase 1: ログイン ──────────────────────────────────
                    page.goto(f"{base_url}/login", timeout=30_000,
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    _r = page.evaluate("""async ([em, pw]) => {
                        try {
                            const resp = await fetch('/api/user-auth/login', {
                                method: 'POST',
                                headers: {'Content-Type':'application/json','Accept':'application/json'},
                                body: JSON.stringify({email:em, password:pw}),
                                credentials: 'include',
                            });
                            const body = await resp.json().catch(() => ({}));
                            return {status: resp.status, token: (body.accessToken || (body.data && body.data.accessToken) || null)};
                        } catch(e) { return {error: String(e)}; }
                    }""", [email, password])
                    access_token = (_r or {}).get("token")
                    if not access_token:
                        self.logger.error(
                            f"browser_standby: login failed "
                            f"status={(_r or {}).get('status')} err={(_r or {}).get('error')}"
                        )
                        standby.res_queue.put("net_error")
                        return
                    self.logger.info("browser_standby: login ✓")

                    # ── Phase 1: "/" 遷移 → CF クリア → Turnstile ─────────
                    page.goto(f"{base_url}/", timeout=30_000,
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    cf_ts_tok = _wait_turnstile(page, 60)
                    self.logger.info(
                        f"browser_standby: Turnstile {'✓' if cf_ts_tok else '(なし — トークンなしで続行)'}"
                    )

                    # cf_clearance を共有キャッシュに保存
                    try:
                        for _c in ctx.cookies():
                            if _c.get("name") == "cf_clearance":
                                self.last_cf_clearance = _c["value"]
                                _set_shared_cf_clearance(proxy, self.last_cf_clearance, self.last_cf_ua, self.last_cf_sec_ch_ua)  # ★ Fix 72
                                break
                    except Exception:
                        pass

                    _ts_solved_at = time.time()
                    standby.ready_event.set()    # ★ スタンバイ完了を通知
                    self.logger.info("browser_standby: ✓ STANDBY READY — 攻撃待機中")

                    # ── Phase 2+3: 攻撃ループ ──────────────────────────────
                    while not _stop.is_set():
                        # スロットシグナルを待機
                        try:
                            slot = standby.cmd_queue.get(timeout=1.0)
                        except _queue.Empty:
                            continue
                        if slot is None:
                            break   # 停止シグナル

                        # トークン鮮度チェック（4分超過なら再取得）
                        _token_age = time.time() - _ts_solved_at
                        if _token_age > 240 or not cf_ts_tok:
                            self.logger.info(
                                f"browser_standby: token stale ({_token_age:.0f}s) → reload & re-solve"
                            )
                            page.reload(wait_until="domcontentloaded")
                            page.wait_for_timeout(1000)
                            cf_ts_tok = _wait_turnstile(page, 30)
                            _ts_solved_at = time.time()

                        # ★ 即 POST
                        result = _do_post(page, access_token, slot, cf_ts_tok)
                        standby.res_queue.put(result)

                        if result == "won" or _stop.is_set():
                            break

                        # 次スロット用に Turnstile 再取得（ページリロード）
                        page.reload(wait_until="domcontentloaded")
                        page.wait_for_timeout(800)
                        cf_ts_tok     = _wait_turnstile(page, 30)
                        _ts_solved_at = time.time()
                        standby.ready_event.set()   # ★ 次の攻撃準備完了
                        self.logger.info("browser_standby: re-armed → 次スロット待機中")

                except Exception as e:
                    err_s = str(e)
                    if proxy and any(pe in err_s for pe in _PROXY_ERRORS):
                        self.logger.warning(f"browser_standby: proxy error: {e}")
                    else:
                        self.logger.error(f"browser_standby: exception: {e}")
                    standby.res_queue.put("net_error")
                finally:
                    try: ctx.close()
                    except Exception: pass
                    try: browser.close()
                    except Exception: pass
                    self.logger.info("browser_standby: ブラウザ終了")

# ── Captcha API (マルチプロバイダ対応) ──────────────────────────────────────
_CAPTCHA_PROVIDERS: dict = {
    # プロバイダ名 → API エンドポイント / Turnstile タスク種別
    "yescaptcha":  {
        "api":     "https://api.yescaptcha.com",
        "task_pl": "TurnstileTaskProxyless",   # proxyless
        "task_px": "TurnstileTask",            # proxy あり
    },
    "2captcha": {
        "api":     "https://api.2captcha.com",
        "task_pl": "TurnstileTaskProxyless",
        "task_px": "TurnstileTask",
    },
    "capsolver": {
        "api":     "https://api.capsolver.com",
        "task_pl": "AntiTurnstileTaskProxyless",
        "task_px": "AntiTurnstileTask",
    },
    "anticaptcha": {
        "api":     "https://api.anti-captcha.com",
        "task_pl": "AntiTurnstileTaskProxyless",
        "task_px": "AntiTurnstileTask",
    },
}


class CaptchaClient:
    def __init__(self, provider: str, key: str, logger: logging.Logger):
        self.provider = (provider or "yescaptcha").lower()
        self.key      = key
        self.logger   = logger
        _p            = _CAPTCHA_PROVIDERS.get(self.provider,
                                                _CAPTCHA_PROVIDERS["yescaptcha"])
        self._api     = _p["api"]
        self._tpl     = _p["task_pl"]
        self._tpx     = _p["task_px"]
        self._solve_timeout = 120   # 1個のソルブ待ち上限(秒)。_captcha_client_chain が cfg から設定。

    def solve_turnstile(self, page_url: str, sitekey: str,
                        proxy: Optional[str] = None, timeout: Optional[int] = None,
                        stop_event: Optional[threading.Event] = None) -> Optional[str]:
        _to = int(timeout) if timeout is not None else int(getattr(self, "_solve_timeout", 120))
        # ★ Fix 17: stop_event が渡された場合は time.sleep の代わりに event.wait を使い
        #           停止要求を受けたら即 None を返す
        if not self.key:
            self.logger.warning(f"Captcha API key not set ({self.provider})")
            return None

        task: dict = {
            "type":       self._tpl,
            "websiteURL": page_url,
            "websiteKey": sitekey,
        }
        if proxy:
            p = _parse_proxy(proxy)
            task["type"]         = self._tpx
            task["proxyType"]    = p["type"]
            task["proxyAddress"] = p["address"]
            task["proxyPort"]    = p["port"]
            if "login"    in p: task["proxyLogin"]    = p["login"]
            if "password" in p: task["proxyPassword"] = p["password"]

        # ── createTask: 3回リトライ / 各30秒タイムアウト ─────────────────────
        tid = None
        for attempt in range(3):
            try:
                r    = _req.post(f"{self._api}/createTask",
                                 json={"clientKey": self.key, "task": task},
                                 timeout=30)
                data = r.json()
                tid  = data.get("taskId")
                if tid:
                    self.logger.info(f"Captcha task {tid} ({self.provider})")
                    break
                err_code = data.get("errorCode", "")
                self.logger.error(f"Captcha createTask [{self.provider}]: {data}")
                print(f"\033[31m[✗] Captcha [{self.provider}] エラー: {err_code or data}\033[0m",
                      flush=True)
                if err_code in ("ERROR_KEY_DOES_NOT_EXIST", "ERROR_ZERO_BALANCE",
                                "ERROR_IP_BLOCKED"):
                    return None
                if attempt < 2:
                    if stop_event and stop_event.wait(3): return None
                    else: time.sleep(3)
            except Exception as e:
                self.logger.warning(
                    f"Captcha createTask attempt {attempt + 1}/3 [{self.provider}]: {e}")
                if attempt < 2:
                    if stop_event and stop_event.wait(3): return None
                    else: time.sleep(3)

        if not tid:
            return None

        # ── ポーリング ────────────────────────────────────────────────────────
        deadline = time.time() + _to
        while time.time() < deadline:
            # ★ Fix 17: stop_event 対応 — 停止要求があれば即 None を返す（3s ウェイトも兼ねる）
            if stop_event:
                if stop_event.wait(3): return None   # set → 停止; timeout → ポーリング継続
            else:
                time.sleep(3)
            try:
                r2  = _req.post(f"{self._api}/getTaskResult",
                                json={"clientKey": self.key, "taskId": tid},
                                timeout=30)
                res = r2.json()
            except Exception as e:
                self.logger.warning(f"Captcha getTaskResult: {e}")
                continue
            if res.get("status") == "ready":
                t = res.get("solution", {}).get("token")
                # ★ Fix 16: token が None のとき誤った "solved ✓" ログを出さない
                if t:
                    self.logger.info("Turnstile solved ✓")
                else:
                    self.logger.warning(
                        "Turnstile status=ready but token is None — treating as failed"
                    )
                return t
            if res.get("status") == "failed":
                self.logger.error(f"Turnstile failed: {res}")
                print(f"\033[31m[✗] Captcha [{self.provider}] Turnstile solve FAILED: "
                      f"{res.get('errorCode', res.get('errorDescription', '?'))}\033[0m",
                      flush=True)
                return None

        self.logger.warning(f"Turnstile timeout ({_to}s)")
        print(f"\033[33m[!] Captcha [{self.provider}] Turnstile timeout ({_to}s)\033[0m",
              flush=True)
        return None


# 後方互換エイリアス
YesCaptcha = CaptchaClient


def _captcha_client(cfg: dict, logger: logging.Logger) -> CaptchaClient:
    """設定から CaptchaClient を生成。captcha_key → yescaptcha_key の順でキーを引き継ぐ。"""
    provider = cfg.get("captcha_provider", "yescaptcha")
    key      = cfg.get("captcha_key") or cfg.get("yescaptcha_key", "")
    return CaptchaClient(provider, key, logger)


def _captcha_ready(cfg: dict) -> bool:
    """captcha_enabled が True かつ主または任意のフォールバック API キーが設定済みの場合に True。"""
    if not cfg.get("captcha_enabled", True):
        return False
    if cfg.get("captcha_key") or cfg.get("yescaptcha_key", ""):
        return True
    # ★ Fix 48: フォールバックチェーンにキーがあれば True
    for _fb in cfg.get("captcha_fallback", []):
        if _fb.get("key"):
            return True
    return False


def _captcha_client_chain(cfg: dict, logger: logging.Logger) -> list:
    """設定から CaptchaClient のリストを生成する（主 → フォールバック順）。
    ★ Fix 48: ソルバー API フォールバックチェーン対応。
    captcha_enabled=False または全キー未設定の場合は空リストを返す。"""
    if not cfg.get("captcha_enabled", True):
        return []
    clients: list = []
    # 主プロバイダ
    _primary_prov = (cfg.get("captcha_provider", "yescaptcha") or "yescaptcha").lower()
    _primary_key = cfg.get("captcha_key") or cfg.get("yescaptcha_key", "")
    if _primary_key:
        clients.append(_captcha_client(cfg, logger))
    # ★ 同一プロバイダの追加キー（負荷分散用）。captcha_keys=["key2","key3",...]。
    #   1キーに全台のソルブが集中すると接続が詰まる(SSLEOF等)ため、キーを増やして分散する。
    for _k in cfg.get("captcha_keys", []):
        _k = (_k or "").strip()
        if _k and _k != _primary_key:
            clients.append(CaptchaClient(_primary_prov, _k, logger))
    # フォールバックプロバイダ（別プロバイダ、または {provider,key} 形式の追加キー）
    for _fb in cfg.get("captcha_fallback", []):
        _prov = (_fb.get("provider") or "yescaptcha").lower()
        _key  = (_fb.get("key") or "").strip()
        if _key:
            clients.append(CaptchaClient(_prov, _key, logger))
    # ★ 1個のソルブ待ち上限を設定（詰まったタスクで波全体を長時間引きずらない）。
    _st = int(cfg.get("solve_timeout_sec", 120))
    for _c in clients:
        _c._solve_timeout = _st
    return clients


def _solve_turnstile_chain(chain: list, page_url: str, sitekey: str,
                           proxy=None,
                           stop_event=None) -> Optional[str]:
    """CaptchaClient リストを順に試し、最初に成功したトークンを返す。
    ★ Fix 48: フォールバックチェーン実行。chain が空の場合は None を返す。"""
    for _client in chain:
        if stop_event and stop_event.is_set():
            return None
        _tok = _client.solve_turnstile(page_url, sitekey,
                                       proxy=proxy, stop_event=stop_event)
        if _tok:
            return _tok
    return None

# ── MelloJoy API ──────────────────────────────────────────────────────────────
class MelloJoyAPI:
    BASE = "https://mellojoylucky.cn"

    def __init__(self, cfg: dict, logger: logging.Logger,
                 proxy: Optional[str] = None,
                 proxies_list: Optional[list] = None):
        self.cfg     = cfg
        self.logger  = logger
        self.proxy   = proxy
        self.token:  Optional[str] = None
        self.rtoken: Optional[str] = None
        self.user_id: Optional[int] = None
        # ★ Fix 38: proxies_list をブラウザバイパスに渡してローテーション対応
        self._cf = CloudflareBypass(cfg, logger, proxies_list=proxies_list)
        self._yc = _captcha_client_chain(cfg, logger)   # ★ Fix 48: フォールバックチェーン
        self.BASE = cfg.get("base_url", "https://mellojoylucky.cn")

        # UA をセッション開始時に1度だけ決定して固定する。
        # 毎リクエストで pick_device() を呼ぶとセッション内で UA が変わり
        # サーバー側のフィンガープリント照合で不整合が生じるリスクがある。
        # ★ Fix 72: HTTP 側は Android 実機の TLS を持つ chrome131_android に統一し、
        #   UA・sec-ch-ua もそれに合わせる（旧: 端末ごとに 116〜130 を名乗り、
        #   chrome128/130 は curl_cffi 非対応 → 素の requests に落ちていた）。
        self._fixed_ua: str = _reduced_android_ua(_HTTP_CHROME_VER)
        self._sec_ch_ua: str = _sec_ch_ua_for(_HTTP_CHROME_VER)

        if _HAS_CURL:
            self._imp_target: str = _pick_imp_target(_HTTP_CHROME_VER)
            self._sess = cf_requests.Session(impersonate=self._imp_target)
        else:
            import requests as req_mod
            self._imp_target = ""
            self._sess = req_mod.Session()

    def _adopt_identity(self, ua: Optional[str], sec_ch_ua: Optional[str]) -> None:
        """★ Fix 72: cf_clearance を取得したブラウザと同じ UA / TLS で HTTP を送るよう合わせる。
        cf_clearance は取得時の UA と組でしか通らないため、UA が違うと 403 になる。"""
        if not ua or ua == self._fixed_ua:
            return
        old_ua = self._fixed_ua
        self._fixed_ua  = ua
        self._sec_ch_ua = sec_ch_ua or _sec_ch_ua_for(_ua_major(ua))
        if _HAS_CURL and self._imp_target:
            new_t = _pick_imp_target(_ua_major(ua))
            if new_t != self._imp_target:
                try:
                    new_sess = cf_requests.Session(impersonate=new_t)
                    for c in list(self._sess.cookies.jar):
                        new_sess.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
                    self._sess = new_sess
                    self._imp_target = new_t
                except Exception as _e72:
                    self.logger.warning(f"Fix 72: session 再作成失敗: {_e72}")
        self.logger.info(f"Fix 72: HTTP identity adopted Chrome/{_ua_major(old_ua)} → "
                         f"Chrome/{_ua_major(ua)} (impersonate={self._imp_target})")

    def _recreate_session_fallback(self, failed_target: str) -> bool:
        """★ Fix 49: curl_cffi が指定 impersonate に未対応の場合のフォールバック。
        Session を古い Chrome バージョンで再作成し、リクエストを再試行可能にする。
        ★ Fix 53b: フォールバック順を拡張（chrome134 → chrome132 → chrome131 →
                   chrome130 → chrome128 → chrome124 → chrome120 → chrome116）。
                   curl_cffi 0.16.3+ では 134 まで対応しているが、古い環境でも
                   動作するよう新→旧の順で試行する。
        """
        if not _HAS_CURL:
            return False
        # ★ Fix 72: curl_cffi が実際に対応しているターゲットだけを新しい順に試す
        _sup72 = _curl_supported_targets()
        _fb_list = (["chrome131_android"] if "chrome131_android" in _sup72 else []) + [
            f"chrome{v}" for v in sorted(
                (int(t[6:]) for t in _sup72 if re.fullmatch(r"chrome\d+", t)), reverse=True)]
        for _fb in _fb_list:
            if _fb == failed_target:
                continue
            try:
                self._sess = cf_requests.Session(impersonate=_fb)
                old = self._imp_target
                self._imp_target = _fb
                self.logger.warning(
                    f"Fix 49: impersonate={old!r} 未対応 → {_fb!r} にフォールバック "
                    f"(pip install 'curl_cffi>=0.16.3' で最新Chromeフィンガープリント対応可能)"
                )
                return True
            except Exception:
                pass
        # ★ Fix 58: curl_cffi 全バージョン Session 作成失敗 → plain requests にフォールバック
        self.logger.error(
            f"Fix 49/58: 利用可能な impersonate が見つかりません。"
            f"curl_cffi を更新してください: pip install 'curl_cffi>=0.16.3'"
        )
        self._disable_curl_cffi_permanently()
        return True   # plain requests でリトライさせる

    def _disable_curl_cffi_permanently(self) -> None:
        """★ Fix 58: curl_cffi が全バージョン未対応の場合、plain requests に切り替える。
        chrome134↔chrome132 の無限ループを防止する。
        """
        if self._imp_target == "":
            return  # 既に無効化済み
        old = self._imp_target
        import requests as _req_plain
        self._sess = _req_plain.Session()
        self._imp_target = ""
        self.logger.warning(
            f"Fix 58: curl_cffi (impersonate={old!r}) が全バージョン未対応 → "
            f"plain requests にフォールバック。TLSフィンガープリントなしで動作します。"
            f" (pip install 'curl_cffi>=0.16.3' で解決)"
        )

    def _ua(self) -> str:
        return self._fixed_ua   # セッション固定 UA

    def _hdr(self) -> dict:
        h = {
            "User-Agent":      self._ua(),
            # ★ Fix 72: curl_cffi が自動で付ける sec-ch-ua 系（macOS/Windows・mobile ?0）を
            #   UA（Android・モバイル）と一致する値で上書きする
            "sec-ch-ua":          self._sec_ch_ua,
            "sec-ch-ua-mobile":   "?1",
            "sec-ch-ua-platform": '"Android"',
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
            "Origin":          self.BASE,
            "Referer":         f"{self.BASE}/",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _prx(self) -> Optional[dict]:
        if not self.proxy:
            return None
        url = _normalize_proxy_url(self.proxy)
        return {"http": url, "https": url}

    def _get(self, path: str, **kw) -> Optional[dict | list]:
        url = f"{self.BASE}{path}"
        debug = self.cfg.get("debug_mode", False)
        _retry_imp = True   # ★ Fix 49: Impersonating エラー時に1回だけリトライ
        while True:
            try:
                r = self._sess.get(url, headers=self._hdr(), proxies=self._prx(),
                                   timeout=20, **kw)
                if debug:
                    self.logger.debug(f"[DBG] GET {r.status_code} {path}  body={r.text[:500]}")
                # ★ Fix 44: 4xx/5xx を常にコンソールに出す（Failed to get dates の根本原因を可視化）
                if not r.ok:
                    self.logger.warning(f"GET {path}: HTTP {r.status_code} body={r.text[:200]}")
                r.raise_for_status()
                return r.json()
            except Exception as e:
                err_s = str(e)
                # ★ Fix 49: "Impersonating chromeXXX is not supported" 検出 → フォールバック
                if "Impersonating" in err_s and "not supported" in err_s:
                    if _retry_imp:
                        _retry_imp = False
                        _cur = self._imp_target
                        if self._recreate_session_fallback(_cur):
                            continue   # フォールバック成功 → リトライ
                    else:
                        # ★ Fix 58: フォールバック後もImpersonatingエラー → plain requests に永続切り替え
                        self._disable_curl_cffi_permanently()
                        _retry_imp = True   # plain requests で1回リトライ
                        continue
                self.logger.error(f"GET {path}: {e}")
                return None

    def _post(self, path: str, body: dict, **kw) -> Optional[dict]:
        url = f"{self.BASE}{path}"
        debug = self.cfg.get("debug_mode", False)
        _retry_imp = True   # ★ Fix 49: Impersonating エラー時に1回だけリトライ
        while True:
            try:
                r = self._sess.post(url, headers=self._hdr(), json=body,
                                    proxies=self._prx(), timeout=15, **kw)
                try:
                    resp = r.json()
                except Exception:
                    resp = None
                if debug:
                    self.logger.debug(
                        f"[DBG] POST {r.status_code} {path}  req={str(body)[:200]}  "
                        f"res={str(resp)[:300]}"
                    )
                if not r.ok:
                    # エラーボディをそのままログに出す（原因特定用）
                    _body = resp if resp is not None else r.text[:300]
                    _scode = (resp.get("code") or resp.get("message")
                              if isinstance(resp, dict) else None)
                    self._last_err = {"status": r.status_code, "code": _scode, "body": _body}
                    self.logger.error(
                        f"POST {path}: HTTP Error {r.status_code}: {_body}"
                    )
                    return None
                self._last_err = None
                return resp
            except Exception as e:
                err_s = str(e)
                # ★ Fix 49: "Impersonating chromeXXX is not supported" 検出 → フォールバック
                if "Impersonating" in err_s and "not supported" in err_s:
                    if _retry_imp:
                        _retry_imp = False
                        _cur = self._imp_target
                        if self._recreate_session_fallback(_cur):
                            continue   # フォールバック成功 → リトライ
                    else:
                        # ★ Fix 58: フォールバック後もImpersonatingエラー → plain requests に永続切り替え
                        self._disable_curl_cffi_permanently()
                        _retry_imp = True   # plain requests で1回リトライ
                        continue
                if any(kw in err_s for kw in _SSL_ERROR_STRS):
                    self.logger.error(f"POST {path}: SSL接続エラー (BoringSSL) → CF bypass にフォールバック: {e}")
                    # SSL エラーを呼び出し元が識別できるよう特殊値をセット
                    self._last_ssl_error = True
                    self._last_err = {"status": None, "code": "SSL", "body": err_s[:200]}
                else:
                    self.logger.error(f"POST {path}: {e}")
                    self._last_err = {"status": None, "code": "EXC", "body": err_s[:200]}
                return None

    # ── auth ──────────────────────────────────────────────────────────────────
    def _inject_cf_clearance(self) -> None:
        """★ Fix 46: Playwright ブラウザで取得した cf_clearance を curl_cffi セッションに inject する。
        cf_clearance は Cloudflare JSD (challenge-platform/scripts/jsd/main.js) が
        ブラウザ内でフィンガープリントを生成・検証した後にセットする Cookie。
        curl_cffi は TLS フィンガープリントを偽装するが JS を実行しないため、
        直接 API リクエストでは cf_clearance が生成されない。
        Playwright ブラウザが保持している cf_clearance を _sess.cookies に注入することで
        全 API リクエスト（booking POST 含む）に自動的に付与される。
        """
        try:
            if not (self._cf and self._cf.last_cf_clearance):
                return
            self._adopt_identity(self._cf.last_cf_ua, self._cf.last_cf_sec_ch_ua)   # ★ Fix 72
            from urllib.parse import urlparse as _up46
            _domain = _up46(
                self.cfg.get("base_url", "https://mellojoylucky.cn")
            ).hostname or "mellojoylucky.cn"
            self._sess.cookies.set(
                "cf_clearance", self._cf.last_cf_clearance, domain=_domain
            )
            self.logger.info(
                f"Fix 46: cf_clearance injected → _sess.cookies (domain={_domain})"
            )
        except Exception as _e46:
            self.logger.warning(f"Fix 46: _inject_cf_clearance 失敗: {_e46}")

    def login(self, email: str, password: str,
              stop_event: Optional[threading.Event] = None) -> bool:
        """API ログイン試行 → 失敗時は CF bypass ブラウザログインにフォールバック。
        stop_event: ★ Fix 28 — ブラウザログインポーリング中に停止を即時伝達する。
        """
        self._last_ssl_error = False
        # ★ Fix 62: APIログイン前に、グローバルキャッシュから共有 cf_clearance を注入する。
        # curl_cffi 未インストール（plain requests）環境では Cloudflare が TLS フィンガープリントで
        # API リクエストをブロックするが、有効な cf_clearance Cookie があれば通過できる。
        # 他ワーカーがブラウザログインで取得した cf_clearance をここで再利用することで
        # ブラウザ起動回数を大幅に削減する。
        _cached_clr, _cached_ua, _cached_sec = _get_shared_cf_identity(self.proxy)   # ★ Fix 72
        if _cached_clr and not (self._cf and self._cf.last_cf_clearance):
            try:
                self._adopt_identity(_cached_ua, _cached_sec)   # ★ Fix 72: UA を取得元ブラウザに合わせる
                from urllib.parse import urlparse as _up62
                _d62 = (_up62(self.cfg.get("base_url", "https://mellojoylucky.cn")).hostname
                        or "mellojoylucky.cn")
                self._sess.cookies.set("cf_clearance", _cached_clr, domain=_d62)
                self.logger.info("Fix 62: shared cf_clearance injected before API login")
            except Exception as _e62:
                self.logger.warning(f"Fix 62: cf_clearance 注入失敗: {_e62}")
        data = self._post("/api/user-auth/login",
                          {"email": email, "password": password})
        # ★ レスポンスが {"code":0,"data":{accessToken,...}} のネスト形式に変更された場合に対応。
        _p = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
        _tok = (data.get("accessToken") if isinstance(data, dict) else None) or _p.get("accessToken")
        if _tok:
            self.token   = _tok
            self.rtoken  = (data.get("refreshToken") if isinstance(data, dict) else None) or _p.get("refreshToken")
            _u = (data.get("user") if isinstance(data, dict) else None) or _p.get("user") or {}
            self.user_id = _u.get("id") if isinstance(_u, dict) else None
            self.logger.info(f"API login OK uid={self.user_id}")
            # ★ Fix 46: API ログイン成功時は Playwright は使わないため
            # cf_clearance は取得できていないが、inject は no-op で安全に呼べる
            self._inject_cf_clearance()
            return True
        # BoringSSL SSL エラーの場合は即 CF browser へ (再試行しない)
        if self._last_ssl_error:
            self.logger.info("SSL接続エラー → CF bypass に直接フォールバック")
        else:
            self.logger.info("API login failed → CF bypass")
        t = self._cf.login_and_get_token(email, password, proxy=self.proxy,
                                         stop_event=stop_event,
                                         captcha_client=self._yc)  # ★ Fix 28 / Fix 42-B
        if t:
            self.token = t
            # ★ Fix 46: Playwright login が cf_clearance を取得していれば _sess に inject
            self._inject_cf_clearance()
            return True
        return False

    def refresh(self) -> bool:
        # HARより: /api/user-auth/refresh は Authorization ヘッダー (Bearer) のみで動作。
        # ログインレスポンスに refreshToken は含まれないため rtoken は常に None。
        # ボディは空 JSON で OK。
        if not self.token: return False
        data = self._post("/api/user-auth/refresh", {})
        _p = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
        _tok = (data.get("accessToken") if isinstance(data, dict) else None) or _p.get("accessToken")
        if _tok:
            self.token = _tok
            return True
        return False

    # ── registration ──────────────────────────────────────────────────────────
    def get_registration_config(self) -> dict:
        return self._get("/api/user-auth/registration-config") or {}

    def send_code(self, email: str, turnstile_token: str, scene: str = "register") -> bool:
        """POST /api/user-auth/send-code  → sends 6-digit code to email.
        ★ サーバが scene（register/login）の指定を必須化（未指定だと code 2001
          「scene は register または login を指定してください」で失敗）。登録時は register。"""
        data = self._post("/api/user-auth/send-code",
                          {"email": email, "turnstileToken": turnstile_token,
                           "scene": scene})
        # ★ 成功レスポンスの形が {"code":0,"message":"ok","data":{"success":true}} に変更。
        #   旧コードはトップ階層の data.get("success") だけを見ており、実際は成功なのに失敗扱い
        #   → 無駄な再送で EMAIL_COOLDOWN を誘発していた。ネスト/旧形式の両対応にする。
        _inner = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
        _ok = isinstance(data, dict) and (
            data.get("success") is True
            or _inner.get("success") is True
            or (data.get("code") in (0, "0") and str(data.get("message", "")).strip().lower() == "ok")
        )
        if _ok:
            self.logger.info(f"send-code OK → {email}")
            return True
        if data is not None and not getattr(self, "_last_err", None):
            # HTTP 200 だが success:false → 本文を失敗理由として残す
            self._last_err = {"status": 200,
                              "code": (data.get("code") or data.get("message")
                                       if isinstance(data, dict) else None),
                              "body": data}
        self.logger.error(f"send-code failed: {data} err={getattr(self, '_last_err', None)}")
        return False

    def inject_shared_cf(self) -> None:
        """★ Fix 73: 他ワーカーが取得済みの cf_clearance（同プロキシ）を API セッションに入れる。
        アカウント作成は login() を通らないため、従来は cf_clearance 無しで send-code を叩いていた。"""
        _clr, _ua, _sec = _get_shared_cf_identity(self.proxy)
        if not _clr:
            return
        try:
            self._adopt_identity(_ua, _sec)
            from urllib.parse import urlparse as _up73
            _d = _up73(self.cfg.get("base_url", "https://mellojoylucky.cn")).hostname or "mellojoylucky.cn"
            self._sess.cookies.set("cf_clearance", _clr, domain=_d)
            self.logger.info("Fix 73: shared cf_clearance injected (creator)")
        except Exception as _e73:
            self.logger.warning(f"Fix 73: cf_clearance 注入失敗: {_e73}")

    def register(self, name: str, email: str, password: str,
                 gender: str, age: int,
                 email_code: str, turnstile_token: str) -> Optional[dict]:
        """POST /api/user-auth/register  → returns response dict on success."""
        body = {
            "privacyAccepted": True,
            "name":            name,
            "email":           email,
            "password":        password,
            "gender":          gender,   # "male" | "female"
            "age":             age,
            "emailCode":       email_code,
            "turnstileToken":  turnstile_token,
        }
        data = self._post("/api/user-auth/register", body)
        # ★ send-code と同様、成功レスポンスが {"code":0,"data":{...}} のネスト形式に変更された
        #   ケースに対応。accessToken/user はトップ階層・data階層のどちらにあっても拾う。
        _inner = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
        _tok = (data.get("accessToken") if isinstance(data, dict) else None) or _inner.get("accessToken")
        if _tok:
            self.token = _tok
            _user = (data.get("user") if isinstance(data, dict) else None) or _inner.get("user") or {}
            self.user_id = _user.get("id") if isinstance(_user, dict) else None
            self.logger.info(f"Register OK: {email} uid={self.user_id}")
            return data
        self.logger.error(f"Register failed: {data}")
        return None

    # ── bookable dates & slots ─────────────────────────────────────────────────
    def get_bookable_dates(self) -> list[str]:
        # ★ API は store スコープ化済み。store_id を付けないと日付が空で返る。
        _sid = int(self.cfg.get("store_id", 2))
        raw = self._get(f"/api/registrations/bookable-dates?store_id={_sid}")
        if raw is None: return []
        # ★ 新API: {"code":0,"message":"ok","data":{"bookableDays":7,"dates":[...]}}
        #   _get はエンベロープ全体を返すので、ここで data を展開する（欠落すると空になる）。
        if isinstance(raw, dict) and "dates" not in raw and "data" in raw:
            raw = raw["data"]
        # Response is base64-encoded JSON string or dict with base64 value
        def _decode(v: str) -> list[str]:
            try:
                j = json.loads(base64.b64decode(v).decode("utf-8"))
                return j.get("dates", [])
            except Exception:
                return []
        if isinstance(raw, str):
            return _decode(raw)
        if isinstance(raw, dict):
            if "dates" in raw: return raw["dates"]
            for v in raw.values():
                if isinstance(v, str):
                    r = _decode(v)
                    if r: return r
        return []

    def get_slots(self, date: str) -> list[dict]:
        # ★ slots も store スコープ化済み。store_id を付けないと枠が空で返る。
        _sid = int(self.cfg.get("store_id", 2))
        raw = self._get(f"/api/registrations/slots?date={date}&store_id={_sid}")
        # ★ 新API: {"code":0,"message":"ok","data":[{slotIndex...}]} 形式。data(list) を展開。
        if isinstance(raw, dict) and isinstance(raw.get("data"), list):
            return raw["data"]
        return raw if isinstance(raw, list) else []

    def get_available_slots(self, date: str) -> list[dict]:
        return [s for s in self.get_slots(date)
                if not s.get("isFull") and s.get("isOpen")
                and not s.get("isPast") and s.get("remaining", 0) > 0]

    def get_my_tickets(self) -> Optional[list[dict]]:
        """GET /api/ticket/mine → 自分の予約一覧を返す。
        各要素は {"slotDate", "startTime", "endTime", "status", "name", ...} を含む。
        ★ Fix 71c: 取得失敗は None、正常に取得した空一覧は [] を返して区別する。
          旧: どちらも [] → 通信失敗が「予約なし(no_ticket)」と誤判定されていた。
        """
        raw = self._get("/api/ticket/mine")
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            # {"data": [...]} 等のラップ形式にも対応
            for _k in ("data", "tickets", "items", "list"):
                if isinstance(raw.get(_k), list):
                    return raw[_k]
            self.logger.warning(f"get_my_tickets: 想定外のレスポンス形式 keys={list(raw)[:10]}")
        return None

    # ── booking ───────────────────────────────────────────────────────────────
    def book(self, slot: dict, cf_token: Optional[str] = None) -> Optional[str]:
        """POST /api/registrations
        Returns:
          "won"       — 当選
          "not_won"   — 落選確定（次スロットへリトライ）
          "cooldown"  — 409 COOLDOWN（アカウントクールダウン）
          None        — 409 RATE_LIMIT（スロット失敗カウントを増やす）
          "net_error" — ネットワーク/SSL エラー（スロット失敗カウントは増やさない）
        """
        lat, lng = _resolve_geo(self.cfg.get("geo", "kanto_random"))
        fp = f"{uuid.uuid4().hex[:8]}-{random.randint(1000, 9999)}"
        body: dict = {
            "store_id":          int(self.cfg.get("store_id", 2)),   # ★ 必須（欠落=PARAM_INVALID）
            "slotDate":          slot["slotDate"],
            "slotIndex":         slot["slotIndex"],
            "deviceFingerprint": fp,
            "gpsLat":            round(lat, 4),
            "gpsLng":            round(lng, 4),
        }
        if cf_token:
            body["turnstileToken"] = cf_token
        url = f"{self.BASE}/api/registrations"
        _retry_imp_book = True   # ★ Fix 49b: book() にも Impersonating フォールバック
        while True:
            try:
                r = self._sess.post(url, headers=self._hdr(), json=body,
                                    proxies=self._prx(), timeout=15)
                try:
                    resp = r.json()
                except Exception:
                    resp = {}
                # ★ 新API: 予約結果はエンベロープ {code,message,data:{...}}。
                #   抽選では result=="pending_draw"(エントリー成立)が成功。当選=won/already_won。
                #   当日登録済=sing_up:true。param/cooldown等のエラーも HTTP200+code!=0 で返るため本文で判定。
                _st_, _dt_ = _interpret_booking(resp)
                if _st_ == "won":
                    self.logger.info(f"book OK({_dt_})  slot={slot.get('slotIndex')}")
                    return "won"
                if _st_ == "not_won":
                    self.logger.info(f"book not_won({_dt_})  slot={slot.get('slotIndex')}")
                    return "not_won"
                if _st_ == "cooldown":
                    self.logger.info(f"book cooldown({_dt_})  slot={slot.get('slotIndex')}")
                    return "cooldown"
                if _st_ == "error":
                    self.logger.error(f"book param/err({_dt_})  slot={slot.get('slotIndex')}  body={str(resp)[:200]}")
                    self._last_err = {"status": r.status_code, "code": _dt_, "body": resp}
                    return None
                # ── ここから従来コード系（念のため維持）──
                if r.status_code == 409:
                    code = (resp or {}).get("code", "RATE_LIMIT")
                    self.logger.info(f"book 409 {code}  slot={slot.get('slotIndex')}")
                    if code in ("COOLDOWN", "SYSTEM_BUSY"):
                        return "cooldown"
                    if code in ("SLOT_ENDED", "SLOT_FULL", "REGISTRATION_CLOSED"):
                        return "not_won"
                    return None
                if r.status_code in (200, 201):
                    self.logger.warning(f"book 200 unknown → not_won  body={str(resp)[:200]}")
                    return "not_won"
                self.logger.error(f"book HTTP {r.status_code}: {resp}")
                return "net_error"   # HTTPエラー → スロットカウントせず
            except Exception as e:
                err_s = str(e)
                # ★ Fix 49b: Impersonating エラー → フォールバックして1回リトライ
                if _retry_imp_book and "Impersonating" in err_s and "not supported" in err_s:
                    _retry_imp_book = False
                    _cur = self._imp_target
                    if self._recreate_session_fallback(_cur):
                        continue
                if any(k in err_s for k in _SSL_ERROR_STRS):
                    self.logger.error(f"book SSL接続エラー: {e}")
                else:
                    self.logger.error(f"book error: {e}")
                return "net_error"   # ネット/SSL エラー → スロットカウントせず

def _interpret_booking(resp):
    """予約レスポンス(エンベロープ {code,message,data:{...}} 想定)を解釈。
       戻り: (status, detail)。status ∈ {won, not_won, cooldown, error, unknown}。
       ★ 抽選仕様: result=="pending_draw"(エントリー成立) / won / already_won / sing_up:true は成功(won扱い=完了)。
         これにより「1アカウント1日1枠まで」を満たした時点でその垢のリトライを止められる。"""
    if not isinstance(resp, dict):
        return "unknown", ""
    inner = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    top_code = resp.get("code")
    result = inner.get("result")
    dcode = inner.get("code")
    if inner.get("sing_up") is True or inner.get("sign_up") is True \
            or result in ("pending_draw", "won", "already_won"):
        return "won", (result or "signed_up")
    if result in ("not_won", "lost"):
        return "not_won", result
    if dcode in ("COOLDOWN", "SYSTEM_BUSY"):
        return "cooldown", dcode
    if dcode in ("SLOT_ENDED", "SLOT_FULL", "REGISTRATION_CLOSED", "SLOT_NOT_OPEN", "ALREADY_REGISTERED"):
        # ALREADY_REGISTERED（当日登録済）は「完了」= これ以上叩かない
        return ("won" if dcode == "ALREADY_REGISTERED" else "not_won"), dcode
    if dcode == "PARAM_INVALID" or (top_code not in (0, None) and dcode):
        return "error", (dcode or top_code)
    return "unknown", (result or dcode or "")


# ══════════════════════════════════════════════════════════════════════════════
# C++ 同期一斉発火コア連携 (fire_mode) — 新モード
#   各ワーカーが book() 直前まで準備(login/cf_clearance/token)を終えたら、調整役(_FireCoord)へ
#   発射パケットを登録してブロック。全員そろう or fire_align_ms 経過で、1プロセスの
#   fire_core(.exe) が N 接続を同一 T0 に一斉発火する(GIL無し・独立接続)。
#   cf_clearance / Turnstile token / UA / impersonate は book() と同一(api から抽出)。
#   OFF(既定)なら従来の api.book() がそのまま使われる。
# ══════════════════════════════════════════════════════════════════════════════
import subprocess as _fire_sp


def _fire_core_path(cfg: dict) -> str:
    p = (cfg or {}).get("fire_core_path") or ""
    if p and os.path.exists(p):
        return p
    name = "fire_core.exe" if os.name == "nt" else "fire_core"
    for c in (os.path.join(str(BASE), name),
              os.path.join(str(BASE), "fire", name),
              os.path.join(str(BASE), "_internal", name)):   # PyInstaller datas 配置も探索
        if os.path.exists(c):
            return c
    return name


def _fire_engine_path(cfg: dict) -> str:
    p = (cfg or {}).get("fire_engine_path") or ""
    if p and os.path.exists(p):
        return p
    name = "fire_engine.exe" if os.name == "nt" else "fire_engine"
    for c in (os.path.join(str(BASE), name),
              os.path.join(str(BASE), "fire", name),
              os.path.join(str(BASE), "_internal", name)):
        if os.path.exists(c):
            return c
    return name


def _parse_engine_out(r) -> dict:
    """エンジンの stdout から最後のJSON行を取り出す。
    空や不正出力でも IndexError で落ちず、明示的な例外にする（誤った再発火を防ぐ）。"""
    raw = (getattr(r, "stdout", "") or "").strip()
    for ln in reversed(raw.splitlines()):
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            return json.loads(ln)
    raise ValueError(f"engine stdout empty/invalid (len={len(raw)})")


def _build_engine_packet(api, cf_token, tag, tokens=None, pref_time="") -> dict:
    """A案エンジン用パケット（枠は発火時にエンジンが選ぶので body_template に __SLOTDATE__/__SLOTINDEX__/__TOKEN__）。
    tokens: 事前ソルブしたトークンのプール。リトライで試行ごとに別トークンを使う（使い捨て対策）。
    pref_time: アカウント指定の希望時間枠(startTime前方一致)。空なら枠は自動分散。"""
    fp = f"{uuid.uuid4().hex[:8]}-{random.randint(1000,9999)}"
    lat, lng = _resolve_geo(api.cfg.get("geo", "kanto_random"))
    # ★ store_id はリテラル（プレースホルダ不要）。exe は body_template を置換してそのまま POST するため、
    #   ここに入れておけば exe 改修なしで主隊の予約POSTにも store_id が入る（欠落=PARAM_INVALID 対策）。
    body_template = json.dumps({
        "store_id": int(api.cfg.get("store_id", 2)),
        "slotDate": "__SLOTDATE__", "slotIndex": "__SLOTINDEX__",
        "deviceFingerprint": fp, "gpsLat": round(lat, 4), "gpsLng": round(lng, 4),
        "turnstileToken": "__TOKEN__",
    }, ensure_ascii=False, separators=(",", ":"))
    # __SLOTINDEX__ は数値で入るので "__SLOTINDEX__" のクオートを外す
    body_template = body_template.replace('"__SLOTINDEX__"', "__SLOTINDEX__")
    try: prx = api._prx().get("https") or api._prx().get("http") or ""
    except Exception: prx = getattr(api, "proxy", "") or ""
    _toks = [t for t in (tokens or []) if t] or ([cf_token] if cf_token else [])
    return {"tag": tag, "url": f"{api.BASE}/api/registrations", "method": "POST",
            "headers": dict(api._hdr()), "cookie": _fire_extract_cookie(api._sess),
            "impersonate": getattr(api, "_imp_target", "") or "",
            "proxy": prx, "warmup_url": f"{api.BASE}/",
            "body_template": body_template,
            "token": (_toks[0] if _toks else ""), "tokens": _toks,
            "pref_time": pref_time or "",
            "timeout_ms": int(api.cfg.get("fire_post_timeout_ms", 8000))}


def _fire_extract_cookie(sess) -> str:
    out = []
    try:
        jar = sess.cookies.jar
    except Exception:
        jar = sess.cookies
    try:
        for c in jar:
            out.append(f"{c.name}={c.value}")
    except TypeError:
        for k, v in dict(sess.cookies).items():
            out.append(f"{k}={v}")
    return "; ".join(out)


def _build_fire_packet(api, slot: dict, cf_token, tag: str) -> dict:
    """book() と同一スキーマの発射パケットを作る(cf_clearance 含む Cookie / token / body / impersonate)。"""
    lat, lng = _resolve_geo(api.cfg.get("geo", "kanto_random"))
    fp = f"{uuid.uuid4().hex[:8]}-{random.randint(1000, 9999)}"
    body = {"slotDate": slot["slotDate"], "slotIndex": slot["slotIndex"],
            "deviceFingerprint": fp, "gpsLat": round(lat, 4), "gpsLng": round(lng, 4)}
    if cf_token:
        body["turnstileToken"] = cf_token
    prx = ""
    try:
        _p = api._prx()
        if isinstance(_p, dict):
            prx = _p.get("https") or _p.get("http") or ""
    except Exception:
        prx = getattr(api, "proxy", "") or ""
    return {"tag": tag, "url": f"{api.BASE}/api/registrations", "method": "POST",
            "headers": dict(api._hdr()), "cookie": _fire_extract_cookie(api._sess),
            "body": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            "impersonate": getattr(api, "_imp_target", "") or "",
            "proxy": prx, "warmup": True, "warmup_url": f"{api.BASE}/"}


def _interpret_fire(res: dict, logger=None):
    """発火結果 → book() と同じ戻り値(won/not_won/cooldown/None/net_error)。"""
    if not res.get("ok"):
        if logger:
            logger.info(f"book(core) net_error: {res.get('error')}")
        return "net_error"
    status = int(res.get("http_status") or 0)
    try:
        j = json.loads(res.get("body") or "{}")
    except Exception:
        j = {}
    if status == 409:
        code = j.get("code", "RATE_LIMIT")
        if code in ("COOLDOWN", "SYSTEM_BUSY"):
            return "cooldown"
        if code in ("SLOT_ENDED", "SLOT_FULL", "REGISTRATION_CLOSED"):
            return "not_won"
        return None
    if status in (200, 201):
        r = j.get("result", "")
        return r if r in ("won", "not_won") else "not_won"
    return "net_error"


class _DetectBarrier:
    """解禁検知の「全ワーカー共有の1イベント」。フリート内で最初に空き枠を検知した
    ワーカーが t0 を刻み、以降のワーカーは同じ t0 を受け取る。resync_gap 以上空いたら再武装。"""
    def __init__(self):
        self._lock = threading.Lock()
        self._t0 = None
        self._armed_at = 0.0

    def mark(self, resync_gap: float) -> float:
        now = time.time()
        with self._lock:
            if self._t0 is not None and (now - self._armed_at) >= resync_gap:
                self._t0 = None   # 新サイクル → 再武装
            if self._t0 is None:
                self._t0 = now
                self._armed_at = now
            return self._t0


_detect_barrier = _DetectBarrier()


class _FireCoord:
    """同期一斉発火の調整役(プロセス内シングルトン)。
    - barrier_mode=True : 全ワーカー共有の検知時刻 t0 を基準に fire_at = t0 + barrier_lead に固定し、
                          その手前(launch_margin)で fire_core を起動 → C++ busy-wait で絶対時刻に精密整列。
    - barrier_mode=False: 旧挙動。first-submit から fire_align_ms の相対窓で集めて一斉発火。
    いずれも発火済みの後続(リトライ等)は単発で即撃つ。"""
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.exe = _fire_core_path(cfg)
        self.align = float(cfg.get("fire_align_ms", 1500)) / 1000.0
        self.lead = float(cfg.get("fire_spin_lead_ms", 3.0))
        self.attempts = int(cfg.get("fire_attempts", 1))
        self.resync_gap = float(cfg.get("fire_resync_gap_sec", 30.0))
        self.barrier = bool(cfg.get("fire_barrier_mode", True))
        self.barrier_lead = float(cfg.get("fire_barrier_lead_ms", 250)) / 1000.0
        self.launch_margin = float(cfg.get("fire_launch_margin_ms", 80)) / 1000.0
        self._cv = threading.Condition(threading.Lock())
        self._pending = []
        self._results = {}
        self._deadline = None     # Python 側で窓を閉じて dispatch する時刻
        self._fire_at = None      # C++ に渡す絶対発火時刻(barrier時)
        self._fired = False
        self._fired_at = 0.0

    def submit(self, tag, packet, logger=None, stop_event=None, detect_ts=None) -> dict:
        with self._cv:
            # 新解禁サイクル検知: 前回一斉発火から resync_gap 以上空いた新規発火 → 窓をリセットし再同期。
            #   （not_won→次枠 / cooldown 等のリトライは数秒内なので従来どおり単発即時発火のまま）
            if self._fired and (time.time() - self._fired_at) >= self.resync_gap:
                self._reset_locked()
                if logger:
                    logger.info("fire_mode: 新しい解禁サイクルを検知 → バッチ窓を再同期")
            if self._fired:
                return self._fire_single(packet)   # 同一サイクルの後続/リトライ → 待たず単発

            # 窓(deadline)と絶対発火時刻(fire_at)を決定 ── 最初の submit が基準
            if self._deadline is None:
                if self.barrier:
                    t0 = detect_ts or time.time()           # 共有の解禁検知時刻
                    self._fire_at = t0 + self.barrier_lead  # 全パケット共通の絶対発火時刻
                    # 窓は発火時刻の launch_margin 手前で閉じる(残りはC++ busy-waitが整列)
                    self._deadline = self._fire_at - self.launch_margin
                    if logger:
                        logger.info(f"fire_mode[barrier]: t0検知 → fire_at=t0+{self.barrier_lead*1000:.0f}ms "
                                    f"(launch -{self.launch_margin*1000:.0f}ms)")
                else:
                    self._deadline = time.time() + self.align
                    self._fire_at = None

            # barrier で既に発火時刻を過ぎている(＝出遅れ) → 待たず単発即時
            if self.barrier and self._fire_at is not None and time.time() >= self._fire_at:
                return self._fire_single(packet)

            self._pending.append((tag, packet))
            while not self._fired:
                if stop_event is not None and stop_event.is_set():
                    return {"ok": False, "error": "stopped"}   # 停止要求 → 即脱出
                remain = self._deadline - time.time()
                if remain <= 0:
                    self._do_fire_locked(logger)
                    break
                self._cv.wait(timeout=min(remain, 0.2))   # stop 応答性: 最大0.2s刻み
            return self._results.get(tag, {"ok": False, "error": "no result"})

    def _reset_locked(self):
        """新解禁サイクル用に窓状態をクリア（_cv ロック保持中に呼ぶこと）。"""
        self._pending = []
        self._results = {}
        self._deadline = None
        self._fire_at = None
        self._fired = False

    def _do_fire_locked(self, logger):
        if self._fired:
            return
        self._fired = True
        self._fired_at = time.time()
        pkts = [dict(p, attempts=self.attempts) for _, p in self._pending]
        # barrier: 検知基準の絶対時刻 / 旧: 起動直後(now+0.05)
        fire_at = self._fire_at if (self.barrier and self._fire_at) else (time.time() + 0.05)
        if logger:
            _in = fire_at - time.time()
            logger.info(f"fire_mode: {len(pkts)} ワーカーを一斉発火 (fire_at in {_in*1000:+.0f}ms)")
        req = {"fire_at_unix": fire_at, "spin_lead_ms": self.lead, "packets": pkts}
        out = self._run(req)
        for item in (out.get("results", []) if isinstance(out, dict) else []):
            self._results[item.get("tag", "")] = item
        self._cv.notify_all()

    def _fire_single(self, packet) -> dict:
        return self._run(dict(packet, attempts=self.attempts, fire_at_unix=0, spin_lead_ms=self.lead))

    def _run(self, req: dict) -> dict:
        try:
            r = _fire_sp.run([self.exe], input=json.dumps(req, ensure_ascii=False),
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
            lines = (r.stdout or "").strip().splitlines()
            return json.loads(lines[-1]) if lines else {"ok": False, "error": "no output",
                                                         "stderr": (r.stderr or "")[:200]}
        except Exception as e:
            return {"ok": False, "error": f"fire_core: {e}"}


_fire_coord = None
_fire_coord_lock = threading.Lock()


def _get_fire_coord(cfg):
    global _fire_coord
    with _fire_coord_lock:
        if _fire_coord is None:
            _fire_coord = _FireCoord(cfg)
        return _fire_coord


def _book_dispatch(worker, api, slot, cf_token):
    """book() のディスパッチ。
    ★ fire_core（旧C++同期一斉発火, fire_mode）は廃止。常に従来 book() を使用する。"""
    return api.book(slot, cf_token=cf_token)


# ── IMAP utilities ────────────────────────────────────────────────────────────
def _html_to_text(html: str) -> str:
    """HTMLメールからテキストを抽出（タグ除去＋実体参照復元）。
    コードが <span>1</span><span>2</span>… のようにタグで分断されても拾えるよう、
    タグは空文字に潰し、その後に数字が連結されるようにする。"""
    try:
        html = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', html)
        # <br>,</p>,</div> などは改行、その他タグは空文字（数字分断を連結するため）
        html = re.sub(r'(?i)<(br|/p|/div|/td|/tr|/li)[^>]*>', '\n', html)
        txt = re.sub(r'(?s)<[^>]+>', '', html)
        import html as _hmod
        txt = _hmod.unescape(txt)
        return txt
    except Exception:
        return html


def _decode_mail_part(part) -> str:
    """メールパートを charset を見てデコード（utf-8 固定をやめる）。"""
    pl = part.get_payload(decode=True)
    if not pl:
        return ""
    cs = (part.get_content_charset() or "").strip() or "utf-8"
    for enc in (cs, "utf-8", "iso-2022-jp", "shift_jis", "cp932", "euc-jp", "latin-1"):
        try:
            return pl.decode(enc, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return pl.decode("utf-8", errors="replace")


def _extract_body(msg) -> str:
    """メールメッセージから本文テキストを抽出する（charset対応＋HTML→テキスト）。
    text/plain を優先しつつ text/html も必ず連結して、どちらにコードがあっても拾う。"""
    plains, htmls = [], []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            ct = part.get_content_type()
            if ct == "text/plain":
                plains.append(_decode_mail_part(part))
            elif ct == "text/html":
                htmls.append(_html_to_text(_decode_mail_part(part)))
    else:
        d = _decode_mail_part(msg)
        (htmls if msg.get_content_type() == "text/html" else plains).append(
            _html_to_text(d) if msg.get_content_type() == "text/html" else d)
    return "\n".join(plains + htmls)


def _extract_code(text: str, length: int = 6) -> Optional[str]:
    """本文から認証コードを抽出。全角数字・空白/ハイフン区切り・HTML分断・
    キーワード近傍を考慮し、誤って別の数字（年号・注文番号等）を拾いにくくする。"""
    if not text:
        return None
    # 全角数字→半角
    t = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    # 数字間の空白/ハイフンを除去（"123 456" / "1 2 3 4 5 6" / "12-34-56" → "123456"）。
    #   改行は跨がない（別行の末尾数字と次行コードを誤連結しないため）。
    joined = re.sub(r'(?<=\d)[ \t\-・.]{1,3}(?=\d)', '', t)
    pat = r'(?<!\d)(\d{%d})(?!\d)' % length
    # ① コード系キーワードの近くの数字を最優先（年号等の誤取得を避ける）
    m = re.search(r'(?:認証|確認|verification|verify|code|コード|OTP|ワンタイム)[^0-9]{0,30}' + pat,
                  joined, re.I)
    if m:
        return m.group(1)
    # ② 本文中の独立した length 桁の数字
    m = re.search(pat, joined)
    if m:
        return m.group(1)
    return None


def _imap_poller(cfg: dict, stop_event: threading.Event) -> None:
    """バックグラウンドIMAPポーラー。
    IMAP接続は常時1本のみ。全ワーカー分のコードを一括取得し
    _code_cache に格納する。ワーカーは fetch_mello_code_cached() で読み出す。

    検索戦略:
      - SINCE today + FROM "mellojoy@" + UNSEEN をサーバー側で絞り込む
        → 古いメール・過去セッションのメールを取得しない
      - Python 側で Date ヘッダと BOT 起動時刻を比較し、起動前のメールをスキップ
        （IMAP SINCE は日付粒度のため、同日の古いメールを除外するため）
      - UID ベースの検索・フェッチ（再接続後もIDが変わらない）
      - 処理済みメールは \\Seen フラグ → 次ポールで重複処理しない
    """
    from email.header import decode_header as _decode_hdr
    from email.utils import parseaddr, parsedate_to_datetime as _parse_dt, getaddresses as _getaddresses

    host = cfg.get("imap_host", "imap.gmail.com")
    port = int(cfg.get("imap_port", 993))
    user = cfg.get("imap_user", "")
    pw   = cfg.get("imap_pass", "")

    SUBJ_KEYWORDS = ("認証", "確認", "抽選", "コード", "code", "Code",
                     "verification", "Verification", "verify", "OTP", "ワンタイム")
    _code_len = int(cfg.get("imap_code_length", 6))
    # 送信元ヒント。Gmailのsearchは単語単位なので部分語(mello)はヒットしない。
    #   localパート共通の "mellojoy" を既定にすると最初からサーバ側で絞れる。
    _from_hint = (cfg.get("imap_from_hint", "mellojoy") or "mellojoy").lower()
    # ★ Gmail検索は単語単位。部分語 "mello" は送信元 "mellojoy@…" にヒットせず、毎回
    #   自己修復で全UNSEEN総なめ→遅延＆ノイズ。設定が "mellojoy" の前方部分なら
    #   正規の送信元トークン "mellojoy" に自動補正する（設定ファイルの直し忘れ対策）。
    if _from_hint and _from_hint != "mellojoy" and "mellojoy".startswith(_from_hint):
        _info(f"[IMAP] from_hint '{_from_hint}' は部分語でGmail検索に不適 → 'mellojoy' に自動補正")
        _from_hint = "mellojoy"
    _dbg = bool(cfg.get("debug_mode", False))
    # 検索対象メールボックス。大量作成時 Gmail が認証メールを Spam に振り分けることがあるので
    #   INBOX に加え Spam も見る。存在しないフォルダは自動スキップ。
    _mailboxes = cfg.get("imap_mailboxes") or ["INBOX", "[Gmail]/Spam"]

    # ── 起動時刻フィルタ ──────────────────────────────────────────────────────
    # 認証メールは「ポーラー起動後」に送られるので、起動時刻より前のメールは別実行の
    # 残骸。バッファは時刻ズレ吸収ぶんだけ（既定120s）に絞る。
    # ★ 広すぎる(30分等)と前回実行の古いコードまで拾う（wggo…の誤検知の原因）。
    #   PCがNTP同期なら数秒ズレ程度なので120sで十分。
    _date_buf = int(cfg.get("imap_date_buffer_sec", 120))
    poller_start_time = time.time() - _date_buf

    # ★ IMAP の SINCE 日付は英語月名固定（%b はロケール依存で日本語Windowsだと
    #   「10月」等になり検索式が壊れて“全く読めない”原因になる）。
    _MON = ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")
    _now_d = datetime.datetime.now()
    today_str = f"{_now_d.day:02d}-{_MON[_now_d.month-1]}-{_now_d.year}"

    # FROM は部分一致ヒントで絞る（IMAPのFROMは部分文字列検索）。取りこぼし時は自動で外す。
    def _crit(use_from: bool) -> str:
        if use_from and _from_hint:
            return f'(SINCE "{today_str}" UNSEEN FROM "{_from_hint}")'
        return f'(SINCE "{today_str}" UNSEEN)'
    # 可変: 連続で0件が続いたら FROM 絞りを外して全UNSEENに切替（送信元ヒント誤りの自己修復）
    _use_from = {"v": bool(_from_hint)}
    _empty_polls = {"n": 0}
    # 他実行宛と判定したUIDは \Seen を立てずに記録し、以後再フェッチしない。
    _skipped_uids: set = set()

    start_disp = datetime.datetime.fromtimestamp(poller_start_time).strftime("%H:%M:%S")
    _info(f"[IMAP] Poller started: {host}:{port} as {user}  crit={_crit(_use_from['v'])}  "
          f"since≥{start_disp}  codeLen={_code_len}  fromHint={_from_hint!r}")

    def _poll_once(M: imaplib.IMAP4_SSL) -> None:
        """接続済みの M に対して1回の検索・取得・キャッシュを行う"""
        crit = _crit(_use_from["v"])
        _, msgs = M.uid('search', None, crit)
        uids = msgs[0].split() if msgs and msgs[0] else []

        # 自己修復: FROM絞りで連続0件が続いたら FROM を外して全UNSEENに切替。
        if _from_hint and _use_from["v"]:
            if not uids:
                _empty_polls["n"] += 1
                if _empty_polls["n"] >= 6:   # 約30秒(5s×6)ヒット無し → FROM絞りを解除
                    _use_from["v"] = False
                    _warn(f"[IMAP] FROM '{_from_hint}' で30s新着ヒット無し → FROM絞りを解除し"
                          f"全UNSEENを検査します（送信元ヒントが実際の送信元と不一致の可能性）。")
            else:
                _empty_polls["n"] = 0

        if uids and _dbg:
            _info(f"[IMAP] poll: {len(uids)} UNSEEN mail(s) matched crit")

        # ★ 古い順（UID昇順）に処理 → 最新コードが最後に残る。
        for uid in uids:
            if uid in _skipped_uids:
                continue   # 他実行宛と判定済み → 再フェッチしない（\Seen も立てない）
            try:
                _process_uid(M, uid)
            except Exception as _pe:
                # 1通の失敗で全体を落とさない（再接続ループ化を防ぐ）
                _warn(f"[IMAP] uid={uid!r} 処理中に例外: {_pe}")
                continue

    def _process_uid(M, uid) -> None:
            # BODY.PEEK[] → \Seen を立てずにフェッチ
            _, data = M.uid('fetch', uid, '(BODY.PEEK[])')
            if not data or not isinstance(data[0], tuple):
                return
            msg = email_lib.message_from_bytes(data[0][1])

            # ── 起動前メールをスキップ（Date ヘッダで判定） ─────────────────
            date_hdr = msg.get("Date", "")
            if date_hdr:
                try:
                    mail_ts = _parse_dt(date_hdr).timestamp()
                    if mail_ts < poller_start_time:
                        return   # 起動前のメールはスキップ
                except Exception:
                    pass   # パース失敗 → スキップしない（安全側）

            # ① Subject デコード
            try:
                parts = _decode_hdr(msg.get("Subject", ""))
                subj = "".join(
                    p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p
                    for p, enc in parts
                )
            except Exception:
                subj = msg.get("Subject", "")

            # ② 送信元
            _, from_addr = parseaddr(msg.get("From", ""))
            from_addr = (from_addr or "").lower().strip()

            # 対象判定: 件名キーワード or 送信元ヒント一致。どちらも無ければスキップ。
            _subj_ok = any(kw.lower() in subj.lower() for kw in SUBJ_KEYWORDS)
            _from_ok = bool(_from_hint) and (_from_hint in from_addr)
            if not (_subj_ok or _from_ok):
                return

            # ③ 宛先を全ヘッダから収集（catch-all/転送で To 以外に本当の宛先が入ることへの対策）
            _rcpt_raw = []
            for _h in ("To", "Cc", "Delivered-To", "X-Original-To",
                       "X-Forwarded-To", "Envelope-To", "X-Delivered-To", "Resent-To"):
                _rcpt_raw += msg.get_all(_h, [])
            rcpts = sorted({a.lower().strip() for _n, a in _getaddresses(_rcpt_raw)
                            if a and "@" in a})

            # ③' 他実行（同一catch-all共有）宛のメールは触らない。
            #   この実行がコード待ち中のアドレス(_inflight)に1つも一致しなければ、
            #   別実行の持ち分とみなし、キャッシュも \Seen も行わずスキップ記録だけ残す。
            #   → 相手実行がそのUNSEENを受け取れる。_inflight が空のときは判定不能なので
            #     従来どおり処理（単独実行の挙動は不変）。
            with _inflight_lock:
                _infl = set(_inflight)
            if _infl and not (set(rcpts) & _infl):
                _skipped_uids.add(uid)
                if _dbg:
                    _info(f"[IMAP] 他実行宛としてスキップ uid={uid!r} rcpts={rcpts}")
                return

            # ④ 本文からコード抽出（charset/HTML/全角/区切り対応）
            body_text = _extract_body(msg)
            code = _extract_code(body_text, _code_len)

            if code and rcpts:
                with _code_cache_lock:
                    for _a in rcpts:
                        _code_cache[_a] = code
                _ok(f"[IMAP] Code cached: {code}  (rcpts: {', '.join(rcpts)})")
                M.uid('store', uid, '+FLAGS', '\\Seen')   # 処理済み
            elif code and not rcpts:
                # 宛先が取れない稀ケース。件名に宛先が無いと紐付けできないので警告のみ。
                _warn(f"[IMAP] コード {code} を検出したが宛先ヘッダを特定できず（subj={subj!r}）。")
            else:
                # 届いているのに読めない → 原因を出す（件名/送信元/本文先頭）
                if _subj_ok or _from_ok:
                    _warn(f"[IMAP] 対象メールだがコード抽出に失敗: from={from_addr!r} subj={subj!r} "
                          f"rcpts={rcpts} bodyHead={body_text[:120].replace(chr(10),' ')!r}")

    while not stop_event.is_set():
        M: Optional[imaplib.IMAP4_SSL] = None
        try:
            # ── 接続確立（1本を維持。エラー時のみ再接続） ─────────────────
            M = imaplib.IMAP4_SSL(host, port)
            M.login(user, pw)

            # ── 接続維持ループ（5秒ごとに NOOP + 各メールボックスを検索） ──────
            while not stop_event.is_set():
                try:
                    M.noop()          # 接続を生かし続けるためのキープアライブ
                    for _mb in _mailboxes:
                        try:
                            typ, _ = M.select(_mb)
                        except (imaplib.IMAP4.abort, OSError):
                            raise                      # 接続断 → 外側で再接続
                        except imaplib.IMAP4.error:
                            continue                   # そのフォルダが無い → スキップ
                        if typ != "OK":
                            continue
                        _poll_once(M)
                except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError):
                    # 接続が切れた → 外側ループで再接続
                    break
                stop_event.wait(5)   # 5秒待機（stop_event が立ったら即抜ける）

        except Exception as e:
            _warn(f"[IMAP poller] {e}")
            stop_event.wait(5)       # 再接続前に少し待つ
        finally:
            if M is not None:
                try:
                    M.logout()
                except Exception:
                    pass
            M = None                 # 参照を明示的に手放す（GC の助け）

    _info("[IMAP] Poller stopped.")


def fetch_mello_code_cached(target_email: str,
                            timeout: int = 120,
                            stop_event: Optional[threading.Event] = None) -> Optional[str]:
    """IMAPポーラーが _code_cache に貯めたコードを待って返す。
    IMAP接続を自前では張らない。ポーラーが停止していると永遠に返らないので
    _start_registration() でポーラーを起動してから呼ぶこと。

    ★ Fix 18: stop_event を受け取り、停止要求があれば即 None を返す。
              time.sleep(5) → stop_event.wait(5) で 5秒単位の遅延を排除。
    """
    key      = target_email.lower().strip()
    # +tag を除いた正規形（site 側が +tag を落とす/付ける場合のフォールバック）
    def _norm(a: str) -> str:
        a = a.lower().strip()
        if "@" in a:
            lp, _, dom = a.partition("@")
            lp = lp.split("+", 1)[0]
            return f"{lp}@{dom}"
        return a
    nkey     = _norm(key)
    deadline = time.time() + timeout

    while time.time() < deadline:
        with _code_cache_lock:
            if key in _code_cache:
                return _code_cache.pop(key)
            # 正規形で一致するキーがあれば採用（厳密一致が取れなかった時の保険）
            for _k in list(_code_cache.keys()):
                if _norm(_k) == nkey:
                    return _code_cache.pop(_k)
        rem = int(deadline - time.time())
        if rem <= 0:
            break
        _wait(f"Waiting for email code... ({rem}s left)")
        if stop_event:
            if stop_event.wait(5): return None   # 停止要求
        else:
            time.sleep(5)

    return None

# ── Account Creator ───────────────────────────────────────────────────────────
def _create_one_account(cfg: dict,
                        names: list[tuple[str, str, Optional[int]]],
                        proxies: list[str], idx: int = 1,
                        total: int = 1) -> bool:
    """Create one MelloJoy account. Returns True on success."""
    logger = logging.getLogger(f"creator-{idx}")

    # 1. Pick name, gender, and age (age=None → use configured range)
    name, gender, name_age = pick_name(names)

    # 2. Generate email
    mode = cfg.get("email_mode", "catchall")
    if mode == "list":
        email = pick_list_email()
        if not email:
            _err(f"[{idx}/{total}] emails.txt is empty or not found")
            return False
    else:
        domain = cfg.get("catchall_domain", "").strip()
        if not domain:
            _err("[!] catchall_domain not configured. Set it in Settings.")
            return False
        email = gen_catchall_email(domain)

    # 3. Generate password
    password = gen_password(cfg.get("password_template", "Mello@{r6}"))

    # 4. Determine age: use names.txt value if present AND within range, else random range
    _amin = int(cfg.get("age_min", 20)); _amax = int(cfg.get("age_max", 40))
    if name_age is not None and _amin <= int(name_age) <= _amax:
        age = int(name_age)
    else:
        if name_age is not None:
            # names.txt の年齢が設定レンジ外（例: 未成年の15）→ 使わずレンジ内で乱択。
            logger.warning(f"names.txt age {name_age} が範囲[{_amin}-{_amax}]外 → 範囲内に置換 (name={name})")
        age = random.randint(_amin, _amax)

    proxy = pick_proxy(proxies)
    api   = MelloJoyAPI(cfg, logger, proxy=proxy)
    api.inject_shared_cf()                       # ★ Fix 73
    yc    = _captcha_client_chain(cfg, logger)   # ★ Fix 48: フォールバックチェーン
    base_url = cfg.get("base_url", "https://mellojoylucky.cn")
    sitekey  = cfg.get("cf_sitekey", "0x4AAAAAAFF0MPh5SYT7ywAo")

    _info(f"[{idx}/{total}] Registering  {email}  name={name}  gender={gender}  age={age}")

    # 5. Solve Turnstile (for send-code)
    # captcha_proxyless=True (default): Captcha API がサーバ側で解くため
    # ユーザのプロキシを渡す必要がない。
    yc_proxy = None if cfg.get("yescaptcha_proxyless", True) else proxy

    _wait(f"[{idx}/{total}] Solving Turnstile (send-code)...")
    ts_token: Optional[str] = None
    if _captcha_ready(cfg):
        ts_token = _solve_turnstile_chain(yc, f"{base_url}/register", sitekey, proxy=yc_proxy)  # ★ Fix 48
    if not ts_token:
        # Fallback: browser-intercept
        _wait(f"[{idx}/{total}] Browser Turnstile fallback...")
        ts_token = api._cf.solve_turnstile_browser(f"{base_url}/register",
                                                    sitekey, proxy=proxy)
    if not ts_token:
        _err(f"[{idx}/{total}] Failed to get Turnstile token")
        return False

    # 6. Send verification code（グローバル 8件/秒 レートリミット適用）
    #   ★ 送信直前に in-flight 登録 → ポーラーが「この実行の宛先」と認識し、
    #     他実行宛のコードを誤って消費しなくなる。取得/失敗後に必ず discard。
    #   ★ Fix 73: 失敗理由をコンソールに表示し、理由別にリトライする
    #     token: Turnstile を取り直して再送 / rate: 全体を一時停止して待ってから再送 /
    #     cf・net: プロキシを替えて再送 / fatal(メール使用済み等): 中断
    _ekey = email.lower().strip()
    with _inflight_lock:
        _inflight.add(_ekey)
    _sc_attempts = max(1, int(cfg.get("send_code_attempts", 4)))
    _sent = False
    for _sa in range(1, _sc_attempts + 1):
        _tag = "" if _sa == 1 else f" (retry {_sa}/{_sc_attempts})"
        if _sa > 1:
            # Turnstile トークンは使い捨て → 毎回取り直す
            _wait(f"[{idx}/{total}] Solving Turnstile (send-code){_tag}...")
            ts_token = None
            if _captcha_ready(cfg):
                ts_token = _solve_turnstile_chain(yc, f"{base_url}/register", sitekey, proxy=yc_proxy)
            if not ts_token:
                ts_token = api._cf.solve_turnstile_browser(f"{base_url}/register", sitekey, proxy=api.proxy)
            if not ts_token:
                _err(f"[{idx}/{total}] Failed to get Turnstile token (send-code){_tag}")
                continue
        _wait_send_code_cooldown()
        _throttle_send_code()
        _wait(f"[{idx}/{total}] Sending email code to {email}{_tag}...")
        api._last_err = None
        if api.send_code(email, ts_token):
            _sent = True
            break
        _e = getattr(api, "_last_err", None) or {}
        _kind = _classify_send_code_error(_e)
        _detail = (f"HTTP {_e.get('status')}"
                   + (f" [{_e.get('code')}]" if _e.get("code") else "")
                   + (f" {str(_e.get('body'))[:160]}" if _e.get("body") not in (None, "") else "")
                   ) if _e else "原因不明（レスポンス無し）"
        _err(f"[{idx}/{total}] send-code failed{_tag} ({_kind}) → {_detail}")
        if _kind == "fatal":
            break
        if _kind == "rate":
            _cd = min(60, 15 * _sa)
            _send_code_cooldown(_cd)
            _warn(f"[{idx}/{total}] send-code レート制限 → 全体の送信を {_cd}s 停止")
        if _kind in ("cf", "net", "rate") and proxies:
            _np = pick_proxy([p for p in proxies if p != api.proxy] or proxies)
            if _np and _np != api.proxy:
                api.proxy = _np
                api.inject_shared_cf()
        time.sleep(min(10, 2 * _sa))
    if not _sent:
        with _inflight_lock:
            _inflight.discard(_ekey)
        _err(f"[{idx}/{total}] send-code failed (全{_sc_attempts}回) — {email}")
        return False

    # 7. Fetch code via IMAP poller cache
    #    ポーラースレッドが _code_cache に貯めたコードを待つだけ（接続不要）
    code: Optional[str] = None
    try:
        if cfg.get("imap_user") and cfg.get("imap_pass"):
            code = fetch_mello_code_cached(email, timeout=180)
    finally:
        with _inflight_lock:
            _inflight.discard(_ekey)
    if not code:
        # 並列実行中は input() 不可（複数スレッド衝突）→ スキップ
        _err(f"[{idx}/{total}] Code timeout — skipping {email}")
        return False

    if not code or len(code) != 6 or not code.isdigit():
        _err(f"[{idx}/{total}] Invalid code")
        return False

    # 8-9. Register（毎回 Turnstile を取り直してリトライ）
    #   register 失敗の大半はトークン拒否/一時的 anti-bot（単独実行でも ~40% 落ちる）。
    #   新トークンで数回リトライすれば大半が通る。email_code は成功時のみ消費される前提で
    #   再利用する（消費済みなら下の理由表示に出るので中断）。
    _reg_attempts = max(1, int(cfg.get("register_attempts", 2)))
    result = None
    for _ra in range(1, _reg_attempts + 1):
        _tag = "" if _ra == 1 else f" (retry {_ra}/{_reg_attempts})"
        # 8. Solve Turnstile (register) ← send-code とは別トークン。毎回取り直す。
        _wait(f"[{idx}/{total}] Solving Turnstile (register){_tag}...")
        ts_token2: Optional[str] = None
        if _captcha_ready(cfg):
            ts_token2 = _solve_turnstile_chain(yc, f"{base_url}/register", sitekey, proxy=yc_proxy)  # ★ Fix 48
        if not ts_token2:
            # Fallback: browser-intercept（send-code と同じフロー）
            _wait(f"[{idx}/{total}] Browser Turnstile fallback (register){_tag}...")
            ts_token2 = api._cf.solve_turnstile_browser(f"{base_url}/register",
                                                         sitekey, proxy=proxy)
        if not ts_token2:
            _err(f"[{idx}/{total}] Failed to get Turnstile token (register){_tag}")
            continue

        # 9. Register
        _wait(f"[{idx}/{total}] Submitting registration{_tag}...")
        result = api.register(
            name=name,
            email=email,
            password=password,
            gender=gender,
            age=age,
            email_code=code,
            turnstile_token=ts_token2 or ts_token,
        )
        if result:
            break

        # 失敗理由を表に出す（従来は generic な [✗] のみ → 原因が分からなかった）
        _e = getattr(api, "_last_err", None) or {}
        _detail = (f"HTTP {_e.get('status')}"
                   + (f" [{_e.get('code')}]" if _e.get("code") else "")
                   + (f" {str(_e.get('body'))[:160]}" if _e.get("body") not in (None, "") else "")
                   ) if _e else "原因不明（レスポンス無し）"
        _err(f"[{idx}/{total}] Registration failed for {email}{_tag} → {_detail}")

        # email_code が消費/無効と明示されたらリトライ無意味 → 中断（ソルブ浪費回避）
        _cs = (str(_e.get("code", "")) + " " + str(_e.get("body", ""))).upper()
        if ("EMAIL" in _cs and "CODE" in _cs) or "VERIFICATION" in _cs:
            break

    if result:
        # accounts.txt には氏名を3フィールド目として含める → FCFSワーカーで誰のアカウントか判別可能
        pair = f"{email}:{password}:{name}"
        ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # ── accounts.txt / created.txt を1ロックブロックで処理 ────────────────
        # 旧実装: _acc_lock を2回取得（逐次取得のため deadlock はないが冗長）
        # 修正:   1ブロックにまとめてロック取得オーバーヘッドを半減
        with _acc_lock:
            # accounts.txt: 末尾改行補完 + 追記
            if ACCOUNTS_F.exists() and ACCOUNTS_F.stat().st_size > 0:
                if ACCOUNTS_F.read_bytes()[-1:] != b"\n":
                    with open(ACCOUNTS_F, "ab") as _fb:
                        _fb.write(b"\n")
            with open(ACCOUNTS_F, "a", encoding="utf-8") as f:
                f.write(pair + "\n")
            # created.txt: 末尾改行補完 + 追記
            if CREATED_F.exists() and CREATED_F.stat().st_size > 0:
                if CREATED_F.read_bytes()[-1:] != b"\n":
                    with open(CREATED_F, "ab") as _fb:
                        _fb.write(b"\n")
            with open(CREATED_F, "a", encoding="utf-8") as f:
                # ID:PW:氏名 形式（accounts.txt と同じ）。
                f.write(f"{pair}\n")
        _ok(f"[{idx}/{total}] Account created ✓ → accounts.txt / created.txt")
        _ok(f"  {email}:{password}  ({name})")
        return True
    else:
        # 失敗理由は上のリトライループ内で既に表示済み。ここは確定のみ。
        _err(f"[{idx}/{total}] Registration failed (全{_reg_attempts}回) — {email}")
        return False

def menu_create_account(cfg: dict):
    while True:
        _box("Account Creator")
        email_mode  = cfg.get("email_mode", "catchall")
        em_display  = (f"@{cfg.get('catchall_domain','(not set)')}"
                       if email_mode == "catchall" else "emails.txt")
        print(f"""
  {Fore.CYAN}[1]{Style.RESET_ALL} Register Settings
         Email mode   : {Fore.WHITE}{email_mode}  {em_display}{Style.RESET_ALL}
         Catch-all    : {Fore.WHITE}{cfg.get('catchall_domain','(not set)')}{Style.RESET_ALL}
         Names file   : {Fore.WHITE}{'OK (' + str(len(load_names())) + ' names)' if NAMES_F.exists() else 'names.txt not found'}{Style.RESET_ALL}
         Batch count  : {Fore.WHITE}{cfg.get('register_count',1)}{Style.RESET_ALL}
         Age range    : {Fore.WHITE}{cfg.get('age_min',20)}–{cfg.get('age_max',40)}{Style.RESET_ALL}
  {Fore.CYAN}[2]{Style.RESET_ALL} Start Registration
  {Fore.CYAN}[0]{Style.RESET_ALL} Back
""")
        choice = input(f"{Fore.WHITE}> {Style.RESET_ALL}").strip()

        if choice == "1":
            cfg = _register_settings(cfg)
        elif choice == "2":
            _start_registration(cfg)
        elif choice == "0":
            break

def _register_settings(cfg: dict) -> dict:
    _box("Register Settings")
    while True:
        print(f"""
  {Fore.CYAN}[1]{Style.RESET_ALL} Email mode          : {Fore.WHITE}{cfg.get('email_mode','catchall')}{Style.RESET_ALL}
  {Fore.CYAN}[2]{Style.RESET_ALL} Catch-all domain    : {Fore.WHITE}{cfg.get('catchall_domain','') or '(not set)'}{Style.RESET_ALL}
  {Fore.CYAN}[3]{Style.RESET_ALL} Password template   : {Fore.WHITE}{cfg.get('password_template','Mello@{{r6}}')}{Style.RESET_ALL}
  {Fore.CYAN}[4]{Style.RESET_ALL} Age range           : {Fore.WHITE}{cfg.get('age_min',20)}–{cfg.get('age_max',40)}{Style.RESET_ALL}
  {Fore.CYAN}[5]{Style.RESET_ALL} Batch count         : {Fore.WHITE}{cfg.get('register_count',1)}{Style.RESET_ALL}
  {Fore.CYAN}[6]{Style.RESET_ALL} Delay between regs  : {Fore.WHITE}{cfg.get('register_delay',10)}s{Style.RESET_ALL}
  {Fore.CYAN}[0]{Style.RESET_ALL} Back / Save
""")
        choice = input(f"{Fore.WHITE}> {Style.RESET_ALL}").strip()

        if choice == "1":
            print("  [1] catchall  [2] list (emails.txt)")
            m = input("Mode: ").strip()
            if m == "1": cfg["email_mode"] = "catchall"
            elif m == "2": cfg["email_mode"] = "list"
        elif choice == "2":
            d = _inp("Catch-all domain (e.g. example.com)",
                     cfg.get("catchall_domain", ""))
            cfg["catchall_domain"] = d.lstrip("@").strip()
        elif choice == "3":
            _info("Use {r6} = 6 random chars, {r8} = 8, {r4d} = 4 digits")
            cfg["password_template"] = _inp("Template", cfg.get("password_template","Mello@{r6}"))
        elif choice == "4":
            cfg["age_min"] = _inp_int("Min age", int(cfg.get("age_min",20)))
            cfg["age_max"] = _inp_int("Max age", int(cfg.get("age_max",40)))
        elif choice == "5":
            cfg["register_count"] = _inp_int("Batch count", int(cfg.get("register_count",1)))
        elif choice == "6":
            cfg["register_delay"] = _inp_int("Delay between registrations (s)",
                                              int(cfg.get("register_delay",10)))
        elif choice == "0":
            break

    save_cfg(cfg)
    _ok("Settings saved.")
    return cfg

def _start_registration(cfg: dict):
    names   = load_names()
    proxies = load_proxies()
    count   = int(cfg.get("register_count", 1))
    delay   = int(cfg.get("register_delay", 10))

    if not names:
        _warn("names.txt not found or empty — using default names")

    email_mode = cfg.get("email_mode", "catchall")
    if email_mode == "catchall" and not cfg.get("catchall_domain"):
        _err("Catch-all domain is not set. Go to Register Settings first.")
        _pause()
        return
    if email_mode == "list" and not EMAILS_F.exists():
        _err("emails.txt not found.")
        _pause()
        return

    parallel = int(cfg.get("parallel", 3))

    print()
    _info(f"Starting batch registration: {count} account(s)  parallel={parallel}")
    if input(f"{Fore.WHITE}Proceed? [y/N]: {Style.RESET_ALL}").strip().lower() != "y":
        return

    # ── IMAP ポーラー起動 ──────────────────────────────────────────────────────
    # ポーラーは IMAP 接続を1本だけ維持し、全ワーカー分のコードをキャッシュする。
    # 10並列でも接続は1本 → Gmail の接続数上限に引っかからない。
    # バッチ開始前にキャッシュをクリア（前回バッチの残留エントリを除去）
    with _code_cache_lock:
        _code_cache.clear()

    stop_event    = threading.Event()
    poller_thread = None
    if cfg.get("imap_user") and cfg.get("imap_pass"):
        poller_thread = threading.Thread(
            target=_imap_poller,
            args=(cfg, stop_event),
            daemon=True,
            name="imap-poller",
        )
        poller_thread.start()

    ok_count = 0

    # ★ Fix 71f: register_delay を反映。各登録の開始時刻を delay 秒ずつずらす。
    #   旧: delay を読み込むだけで未使用 → 全件が同時に開始していた。
    _reg_start_lock = threading.Lock()
    _reg_next_start = [time.time()]

    def _worker(i: int) -> bool:
        if delay > 0:
            with _reg_start_lock:
                _now = time.time()
                _start_at = max(_now, _reg_next_start[0])
                _reg_next_start[0] = _start_at + delay
            _wait_s = _start_at - time.time()
            if _wait_s > 0:
                if stop_event.wait(_wait_s):
                    return False
        try:
            return _create_one_account(cfg, names, proxies, idx=i, total=count)
        except Exception as e:
            _err(f"[{i}/{count}] Unexpected error: {e}")
            return False

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futs = {pool.submit(_worker, i): i for i in range(1, count + 1)}
        for f in as_completed(futs):
            if f.result():
                ok_count += 1

    # ── ポーラー停止 ───────────────────────────────────────────────────────────
    stop_event.set()
    if poller_thread:
        poller_thread.join(timeout=10)

    print()
    _ok(f"Registration complete: {ok_count}/{count} succeeded")
    _pause()

# ── FCFS グローバルスロット失敗カウンタ ───────────────────────────────────────
# key: (slotDate, slotIndex)  value: 409 失敗回数（全ワーカー合計）
_slot_fail_lock: threading.Lock = threading.Lock()
_slot_fail_count: dict[tuple, int] = {}
_slot_fail_last_date: str = ""  # 最後にクリアした日付（YYYY-MM-DD）
_SLOT_FAIL_MAX = 5   # この回数以上 409 RATE_LIMIT されたスロットはスキップ (3→5に緩和)

def _time_match(s: dict, t: str) -> bool:
    """startTime が t に前方一致（HH:MM:SS vs HH:MM 両対応）。
    モジュールレベル定義 — ループ内で毎回再生成しない。"""
    return s.get("startTime", "").startswith(t)


def _slot_fail_cleanup_if_new_day() -> None:
    """日付が変わったら _slot_fail_count をクリアする。
    古い (slotDate, slotIndex) キーが永遠に蓄積する問題を防ぐ。
    ワーカーの外側ループ先頭で呼ぶ（1日1回のみ実行される）。

    ★ Fix 20: ロック外の _slot_fail_last_date 読み取りを廃止し、
              ロック内でのダブルチェックに一本化する（CPython の GIL に依存しない）。
    """
    global _slot_fail_last_date
    today = datetime.date.today().isoformat()
    # 高速パス: ロックなしで読む（CPython str 参照はアトミックだが規約上はロックが必要）
    # → ロック取得コストを避けるため、まず非同期に確認してからロック取得する
    if _slot_fail_last_date == today:
        return
    with _slot_fail_lock:
        if _slot_fail_last_date != today:   # ダブルチェック（ロック内）
            _slot_fail_count.clear()
            _slot_fail_last_date = today

# ── FCFS グローバルスロットキャッシュ（全ワーカー共有）─────────────────────────
# 50ワーカー規模でも1秒あたりのスロット取得APIリクエストを最小化する
# ★ Fix 60: TTL=2s → 0.45s に短縮。monitor_delay=0.5s に合わせて最速検知を実現。
# 同一ラウンド内（~0.13s）で複数ワーカーが同じ日付を叩いてもキャッシュhit。
# TTL < monitor_delay なので次のラウンドでは必ずAPIから最新データを取得する。
_SLOT_CACHE_TTL   = 0.45   # seconds  ★ Fix 60: 2.0 → 0.45
# ★ Fix 44: TTL を 5s → 30s に延長（当時は各ワーカーが個別にAPIを叩いていた）。
# ★ Fix 64: TTL を 30s → 0.9s に短縮。monitor_delay=1s に合わせて最速検知を実現。
# 【Fix 44との矛盾について】Fix 44当時はワーカーごとに個別キャッシュだったため
# 20ワーカーが5sごとに一斉リフレッシュ → Cloudflare レートリミット発生。
# 現在は全ワーカー共有キャッシュなので、TTLをいくら短くしても1回のAPI呼び出しで済む。
# → TTL=0.9s でもレートリミットリスクなし。スロット開放を最大1s以内に検知できる。
_DATES_CACHE_TTL  = 0.9    # seconds  ★ Fix 64: 30s → 0.9s（monitor_delay=1sに合わせる）
_slot_cache_lock: threading.Lock = threading.Lock()
_slot_cache:   dict[str, tuple[float, list]] = {}   # date  → (ts, slots_raw)
_dates_cache:  tuple[float, list] | None     = None  # (ts, dates)
_dates_fetching: bool                        = False  # フェッチ中フラグ（N重APIコール防止 ★ Fix 65）

def _fcfs_get_slots_cached(api, date: str) -> list[dict]:
    """全ワーカー共有のスロットキャッシュ経由で get_slots() を呼ぶ。
    TTL=2s 以内に同じ日付を複数ワーカーが要求した場合は1回のAPI呼び出しで済む。

    ダブルチェックロックパターン:
      1. ロック外で高速チェック（ほとんどのケースはここで hit してリターン）
      2. キャッシュミス → API呼び出し（ロック外）
      3. 書き込み前に再度ロックを取得しキャッシュを再チェック。
         他ワーカーが同時にフェッチを完了していた場合はその結果を返し、
         自分の結果で上書きしない（より新しいデータを優先）。
    """
    now = time.time()
    with _slot_cache_lock:
        entry = _slot_cache.get(date)
        if entry and now - entry[0] < _SLOT_CACHE_TTL:
            return entry[1]

    # キャッシュミス → ロック外でAPIを呼ぶ（他ワーカーの同時呼び出しは許容）
    raw = api.get_slots(date)

    with _slot_cache_lock:
        # 書き込み前にキャッシュを再チェック（他ワーカーが先に書いた場合は新しいほうを優先）
        entry = _slot_cache.get(date)
        if entry and time.time() - entry[0] < _SLOT_CACHE_TTL:
            return entry[1]   # 他ワーカーの結果を使用（自分の結果を捨てる）
        # ★ Fix 19: API障害 / エラーで [] が返った場合はキャッシュしない
        #           スロット開放直後の一時不安定時に 2s 間スロットを見逃さないため
        if raw:
            _slot_cache[date] = (time.time(), raw)
    return raw

def _fcfs_get_dates_cached(api) -> list[str]:
    """全ワーカー共有の bookable-dates キャッシュ。TTL=0.9s。★ Fix 64
    ★ Fix 65: single-flight パターン（stale-while-revalidate）
      TTL切れ時、最初の1ワーカーだけがAPIを叩く。他のワーカーは古いキャッシュを返す。
      100並列でも bookable-dates へのリクエストは常に1本/TTL を保証。
    """
    global _dates_cache, _dates_fetching
    now = time.time()
    with _slot_cache_lock:
        # ① キャッシュhit → 即返す
        if _dates_cache is not None and now - _dates_cache[0] < _DATES_CACHE_TTL:
            return _dates_cache[1]
        # ② 他ワーカーがフェッチ中 → stale-while-revalidate（古いキャッシュ or 空）
        if _dates_fetching:
            return _dates_cache[1] if _dates_cache is not None else []
        # ③ 自分がフェッチ担当
        _dates_fetching = True

    raw: list = []
    try:
        raw = api.get_bookable_dates()
    except Exception:
        pass
    finally:
        with _slot_cache_lock:
            _dates_fetching = False   # 完了 → 次のフェッチ担当を解放
            if raw:
                # 他ワーカーが先に書いた場合は新しいほうを優先（★ Fix 14）
                if _dates_cache is None or time.time() - _dates_cache[0] >= _DATES_CACHE_TTL:
                    _dates_cache = (time.time(), raw)
    # フェッチ失敗時は古いキャッシュを返す（全ワーカーを止めない）
    return raw if raw else (_dates_cache[1] if _dates_cache is not None else [])

def _slot_key(slot: dict) -> tuple:
    return (slot.get("slotDate", ""), slot.get("slotIndex", ""))

def _record_slot_fail(slot: dict) -> int:
    """409 を記録し、更新後の失敗回数を返す"""
    key = _slot_key(slot)
    with _slot_fail_lock:
        _slot_fail_count[key] = _slot_fail_count.get(key, 0) + 1
        return _slot_fail_count[key]

def _slot_exhausted(slot: dict) -> bool:
    """このスロットが失敗上限に達しているか"""
    key = _slot_key(slot)
    with _slot_fail_lock:
        return _slot_fail_count.get(key, 0) >= _SLOT_FAIL_MAX

# ── FCFS Worker ───────────────────────────────────────────────────────────────
class FCFSWorker:
    """永続ワーカー: _acct_q からアカウントを取り出して順次処理する。
    アカウントが固定割り当てではないため、re-login 失敗時にアカウントをキューに
    戻して次のアカウントを処理できる。"""

    def __init__(self, cfg: dict, proxies: list[str], wid: int):
        self.cfg     = cfg
        self.proxies = proxies
        self.wid     = wid
        self.logger  = logging.getLogger(f"fcfs-{wid}")
        self._stop   = threading.Event()

    def stop(self): self._stop.set()
    def _tag(self): return f"[W{self.wid:02d}]"

    def run(self) -> int:
        """永続ワーカーメインループ。戻り値: 当選アカウント数。
        _acct_q が空になるか _stop が set されるまで繰り返す。"""
        won_count = 0
        while not self._stop.is_set():
            try:
                item = _acct_q.get_nowait()
            except _queue.Empty:
                break   # アカウントなし → 終了

            # ★ Fix 32: item は (pair, name_rank, name_count, retry_count) の4-tuple
            pair, name_rank, name_count, retry_count = item
            # ★ Fix 30: _monitor_account が予期しない例外を投げてもワーカーが死なないよう
            #   try/except で保護し、アカウントをキューに戻して次へ進む。
            # ★ Fix 32: retry_count が上限に達したらキューに戻さずドロップして無限ループを防ぐ。
            try:
                result = self._monitor_account(pair, name_rank, name_count)
            except Exception as _exc:
                self.logger.error(
                    f"{self._tag()} _monitor_account crashed "
                    f"({pair.split(':')[0]}): {_exc}", exc_info=True
                )
                if retry_count < _MAX_REQUEUE:
                    _acct_q.put((pair, name_rank, name_count, retry_count + 1))
                    self.logger.info(
                        f"{self._tag()} アカウントをキューに戻しました "
                        f"({pair.split(':')[0]}, retry={retry_count + 1}/{_MAX_REQUEUE})"
                    )
                else:
                    self.logger.error(
                        f"{self._tag()} requeue上限 ({_MAX_REQUEUE}回) 超過 → "
                        f"{pair.split(':')[0]} をドロップ"
                    )
                    with _con_lock:
                        print(f"{Fore.RED}{self._tag()} ⚠ requeue上限超過 → "
                              f"{pair.split(':')[0]} をスキップ{Style.RESET_ALL}")
                continue

            if result == "won":
                won_count += 1
                # 当選 → 次のアカウントをキューから取得して続行
            elif result == "requeue":
                # re-login 失敗（proxyやネット起因）→ キューに戻して次のアカウントへ
                if retry_count < _MAX_REQUEUE:
                    _acct_q.put((pair, name_rank, name_count, retry_count + 1))
                    self.logger.info(
                        f"{self._tag()} アカウントをキューに戻しました "
                        f"({pair.split(':')[0]}, retry={retry_count + 1}/{_MAX_REQUEUE})"
                    )
                else:
                    self.logger.error(
                        f"{self._tag()} requeue上限 ({_MAX_REQUEUE}回) 超過 → "
                        f"{pair.split(':')[0]} をドロップ"
                    )
                    with _con_lock:
                        print(f"{Fore.RED}{self._tag()} ⚠ requeue上限超過 → "
                              f"{pair.split(':')[0]} をスキップ{Style.RESET_ALL}")
            # "done" / "stopped" → 次のアカウントへ (or 外ループ終了)

        return won_count

    def _monitor_account(self, pair: str, name_rank: int, name_count: int) -> str:
        """★ Fix 71e: 本体を try/finally で包み、どの経路で return / 例外終了しても
        ブラウザスタンバイ（スレッド・Chromium・_browser_lock の枠）を必ず解放する。
        旧: 終了処理が関数末尾にしかなく、won / requeue の早期 return や
            スタンバイ準備タイムアウト時に Chromium が残り続け、
            _browser_lock(最大5) の枠を占有し続けていた。"""
        self._bm_cleanup = None   # (standby, thread, stop_event)
        try:
            return self._monitor_account_body(pair, name_rank, name_count)
        finally:
            self._shutdown_standby()

    def _shutdown_standby(self) -> None:
        _c = getattr(self, "_bm_cleanup", None)
        self._bm_cleanup = None
        if not _c:
            return
        _sb, _th, _ev = _c
        try:
            _ev.set()                  # 待機中の Turnstile ポーリング等も即中断
            _sb.cmd_queue.put(None)    # 攻撃ループへの停止シグナル
            _th.join(timeout=10)
            if _th.is_alive():
                self.logger.warning(f"{self._tag()} ブラウザスタンバイの終了待ちがタイムアウト（10s）")
        except Exception:
            pass

    def _monitor_account_body(self, pair: str, name_rank: int, name_count: int) -> str:
        """1アカウントの監視ループ。
        戻り値: "won" | "requeue" | "done" | "stopped"
          won      : 予約成功
          requeue  : re-login 失敗（proxy/ネット要因）→ キューに戻す
          done     : ログイン失敗（認証要因）or _stop
          stopped  : _stop が set された
        """
        parts          = pair.split(":", 3)
        email          = parts[0]
        password       = parts[1]
        name           = parts[2].strip() if len(parts) > 2 else ""
        preferred_slot = parts[3].strip() if len(parts) > 3 else ""

        proxy       = pick_proxy(self.proxies)
        api         = MelloJoyAPI(self.cfg, self.logger, proxy=proxy,
                                  proxies_list=self.proxies)  # ★ Fix 38
        yc          = _captcha_client_chain(self.cfg, self.logger)   # ★ Fix 48: フォールバックチェーン
        base_url    = self.cfg.get("base_url", "https://mellojoylucky.cn")
        sitekey     = self.cfg.get("cf_sitekey", "0x4AAAAAAFF0MPh5SYT7ywAo")
        mon_delay   = float(self.cfg.get("monitor_delay", 15))
        retry_delay = float(self.cfg.get("retry_delay", 3))

        name_disp = f"  ({name})" if name else ""
        with _con_lock:
            print(f"{Fore.CYAN}{self._tag()} Logging in: {email}{name_disp}{Style.RESET_ALL}")
        if not api.login(email, password, stop_event=self._stop):   # ★ Fix 28
            with _con_lock:
                print(f"{Fore.RED}{self._tag()} Login failed: {email} → スキップ（次のアカウントへ）{Style.RESET_ALL}")
            return "done"   # 認証失敗は requeue しない
        with _con_lock:
            print(f"{Fore.GREEN}{self._tag()} Login OK: {email}{Style.RESET_ALL}")

        token_ts         = time.time()
        TOKEN_TTL        = 18 * 60   # refresh every 18 min
        _last_status_ts: float      = 0.0    # "No slots" スロットル用タイムスタンプ
        _pre_token: Optional[tuple] = None   # 共有プールから取得した (token_str, ts)

        # ★ ブラウザスタンバイモード事前起動（browser_mode=ON 時のみ）
        _bm_standby: Optional[_BrowserStandby] = None
        _bm_thread:  Optional[threading.Thread] = None
        _bm_active = self.cfg.get("browser_mode", False) and _HAS_PW

        # ★ デバッグ: browser_mode / _HAS_PW の実際の値を常時表示
        with _con_lock:
            print(f"{self._tag()} [DEBUG] browser_mode={self.cfg.get('browser_mode', False)}  _HAS_PW={_HAS_PW}  → _bm_active={_bm_active}")

        if _bm_active:
            _bm_lat, _bm_lng = _resolve_geo(self.cfg.get("geo", "kanto_random"))
            _bm_standby = _BrowserStandby()
            # ★ Fix 71e: アカウント単位の停止イベント（このアカウントの監視終了時に set）
            _bm_stop_ev = threading.Event()

            def _bm_bg_entry(_sb=_bm_standby, _ev=_bm_stop_ev):
                try:
                    api._cf.book_via_browser_standby(
                        email, password, _bm_lat, _bm_lng, _sb,
                        proxy=proxy, stop_event=_ev)
                except Exception as _bse:
                    # ★ Fix 71e: ブラウザ起動失敗などがスレッド内で黙って消えないようにログ出力
                    self.logger.error(f"{self._tag()} browser_standby crashed: {_bse}", exc_info=True)
                    with _con_lock:
                        print(f"{Fore.RED}{self._tag()} [BrowserMode] スタンバイ異常終了: {_bse}{Style.RESET_ALL}")

            _bm_thread = threading.Thread(
                target=_bm_bg_entry,
                name=f"BSB-W{self.wid}",
                daemon=True,
            )
            _bm_thread.start()
            self._bm_cleanup = (_bm_standby, _bm_thread, _bm_stop_ev)

            with _con_lock:
                print(f"{Fore.CYAN}{self._tag()} [BrowserMode] 事前起動中... "
                      f"(ブラウザ起動 → CF → Turnstile){Style.RESET_ALL}")

            # ブラウザ準備完了まで待機（最大 90s、タイムアウト時は HTTP fallback）
            # ★ Fix 71e: 0.5s 刻みで待ち、Ctrl+C（self._stop）やスレッド異常終了で即抜ける
            _bm_deadline = time.time() + 90
            while (not _bm_standby.ready_event.is_set()
                   and time.time() < _bm_deadline
                   and not self._stop.is_set()
                   and _bm_thread.is_alive()):
                _bm_standby.ready_event.wait(0.5)
            if _bm_standby.ready_event.is_set():
                with _con_lock:
                    print(f"{Fore.GREEN}{self._tag()} [BrowserMode] ✓ スタンバイ完了 "
                          f"— スロット監視中（HTTP）{Style.RESET_ALL}")
            else:
                with _con_lock:
                    print(f"{Fore.YELLOW}{self._tag()} [BrowserMode] ⚠ 事前起動タイムアウト "
                          f"→ HTTP+BrowserMode fallback に切替{Style.RESET_ALL}")
                _bm_standby = None   # 準備失敗 → 通常 book_via_browser() を使う
                # ★ Fix 71e: 準備に失敗したスタンバイは即終了させ、_browser_lock の枠を返す
                #   （残したままだと fallback の book_via_browser が枠待ちで詰まる）
                self._shutdown_standby()

        while not self._stop.is_set():
            # 日付変更時のスロット失敗カウンタクリア（1日1回）
            _slot_fail_cleanup_if_new_day()

            # ★ Fix 11: _pre_token proactive expiry check
            # スロット発見時のみチェックしていた失効検知を外側ループに移動。
            # これにより 13:00 開放時点で全ワーカーが有効トークンを保持できる。
            if _pre_token is not None:
                if time.time() - _pre_token[1] >= _PRE_TOKEN_TTL:
                    self.logger.debug(
                        f"{self._tag()} _pre_token 期限切れ ({_PRE_TOKEN_TTL}s) → プール再取得待ち"
                    )
                    _pre_token = None

            # Token refresh（外側ループ）
            # ★ Fix 13a: refresh() 失敗時も token_ts を更新して連打防止。
            #            失敗が続く場合は再ログインを試みる。
            if time.time() - token_ts > TOKEN_TTL:
                if api.refresh():
                    token_ts = time.time()
                    self.logger.info(f"{self._tag()} Token refreshed")
                else:
                    self.logger.warning(f"{self._tag()} Token refresh failed → re-login")
                    if api.login(email, password, stop_event=self._stop):   # ★ Fix 28
                        token_ts = time.time()
                        self.logger.info(f"{self._tag()} Re-login OK")
                    else:
                        self.logger.error(f"{self._tag()} Re-login failed → requeueing")
                        return "requeue"

            # Get available dates（全ワーカー共有キャッシュ TTL=0.9s）  ★ Fix 64
            dates = _fcfs_get_dates_cached(api)
            if not dates:
                with _con_lock:
                    print(f"{Fore.YELLOW}{self._tag()} Failed to get dates → retry in {retry_delay}s{Style.RESET_ALL}")
                if self._stop.wait(retry_delay): break
                continue

            found_any     = False   # スロットAPIに残あり枠が存在したか
            raw_found     = False   # スロット日程データがAPIから返ってきたか（残0含む）
            any_attempted = False   # exhaustedでないスロットを1つでも試みたか
            for date in dates:
                if self._stop.is_set(): break

                # 内側ループでもトークン切れを検知して refresh する
                # (dates が多い場合、外側チェックだけでは TOKEN_TTL を超えることがある)
                # ★ Fix 13b: refresh() 失敗時も token_ts を更新して連打防止
                if time.time() - token_ts > TOKEN_TTL:
                    if api.refresh():
                        token_ts = time.time()
                        self.logger.info(f"{self._tag()} Token refreshed (inner loop)")
                    else:
                        token_ts = time.time()   # 失敗でも更新 → 次の TOKEN_TTL まで待機
                        self.logger.warning(f"{self._tag()} Token refresh failed (inner loop)")

                # 全ワーカー共有キャッシュ TTL=0.45s — 同一日付を複数Wが叩いても1本で済む  ★ Fix 60
                raw_slots = _fcfs_get_slots_cached(api, date)
                if raw_slots:
                    raw_found = True   # 日程データあり（restock待ち候補）
                avail = [s for s in raw_slots
                         if not s.get("isFull") and s.get("isOpen")
                         and not s.get("isPast") and s.get("remaining", 0) > 0]
                if not avail: continue

                found_any = True
                # ★ fire_mode[barrier]: 空き枠を検知した瞬間をフリート共有の t0 として刻む。
                #    （トークン取得より前＝本物の「解禁検知時刻」。全ワーカーが同じ t0 を受け取る）
                if self.cfg.get("fire_mode", False):
                    self._fire_detect_ts = _detect_barrier.mark(
                        float(self.cfg.get("fire_resync_gap_sec", 30.0)))
                # ① 前半4枠(重み6) : 後半5枠(重み4) の優先度付きシャッフル
                ordered = _priority_shuffle(avail)

                # ② 同一氏名ワーカー間で狙う枠をずらす（重複エントリーは許容）
                #    name_count 人で均等分散: step = len/name_count, offset = rank * step
                if name_count > 1 and len(ordered) > 1:
                    step   = max(1, len(ordered) // name_count)
                    offset = (name_rank * step) % len(ordered)
                    ordered = ordered[offset:] + ordered[:offset]
                    self.logger.debug(
                        f"{self._tag()} name_rank={name_rank}/{name_count}"
                        f" → slot offset={offset} (step={step})"
                    )

                # ③ 優先時間枠を先頭に配置
                #    優先度: アカウント固定指定 > メニュー設定 > シャッフル順
                pref_times = self.cfg.get("preferred_times", [])   # メニュー設定

                if preferred_slot:
                    # アカウント個別指定: 一致スロットを先頭へ
                    matched = [s for s in ordered if _time_match(s, preferred_slot)]
                    rest    = [s for s in ordered if not _time_match(s, preferred_slot)]
                    ordered = matched + rest
                    if matched:
                        self.logger.debug(
                            f"{self._tag()} preferred_slot={preferred_slot!r} → "
                            f"{len(matched)} slot(s) moved to front"
                        )
                elif pref_times:
                    # メニュー設定: 指定時間枠をリスト順に先頭へ（残りは既存順）
                    pref_head: list[dict] = []
                    rest = list(ordered)
                    for pt in pref_times:
                        m = [s for s in rest if _time_match(s, pt)]
                        pref_head.extend(m)
                        rest = [s for s in rest if not _time_match(s, pt)]
                    ordered = pref_head + rest

                # デバッグ: 取得したスロット一覧を保存
                _dbg = self.cfg.get("debug_mode", False)
                if _dbg:
                    _ts_now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    _debug_dump_json(
                        f"fcfs_w{self.wid}_{_ts_now}_slots_{date}",
                        {"date": date, "slots": ordered, "worker": self.wid,
                         "email": email, "name_rank": name_rank,
                         "name_count": name_count,
                         "preferred_slot": preferred_slot,
                         "preferred_times": self.cfg.get("preferred_times", [])}
                    )

                # ★ Fix 47: Ajax / Sequential モード分岐
                _ajax_mode_on = self.cfg.get("ajax_mode", False)
                _ajax_jitter  = float(self.cfg.get("ajax_jitter_max", 0.3))

                if _ajax_mode_on:
                    # ─── Ajax mode: 全スロットに同時 POST ───────────────────
                    _avail_aj = [s for s in ordered if not _slot_exhausted(s)]
                    if _avail_aj:
                        any_attempted = True
                        with _con_lock:
                            print(f"{Fore.CYAN}{self._tag()} [Ajax] {len(_avail_aj)}枠に同時POST開始"
                                  f"{Style.RESET_ALL}")
                        _yc_prx_aj = None if self.cfg.get("yescaptcha_proxyless", True) else proxy
                        _now_aj    = time.time()
                        # ★ Fix 63: Ajax modeのTSトークン取得を並列化
                        #   従来: 各スロットのTS取得を直列実行 → N×3秒の遅延
                        #   Fix 63: ThreadPoolExecutor で全スロットのTS取得を同時実行
                        #   効果: 8スロットなら 8×3.3秒(26秒) → 3.3秒(最大1本分) に短縮
                        #
                        # 最初のスロットは _pre_token を消費（メインスレッド）
                        # 残りは ThreadPoolExecutor で並列取得
                        _base_aj63  = f"{base_url}/"
                        _api_aj63   = api
                        _cfg_aj63   = self.cfg
                        _stop_aj63  = self._stop
                        _prx_aj63   = _yc_prx_aj
                        _yc_aj63    = yc

                        # ① _pre_tokenを最初のスロットへ（メインスレッドで消費）
                        _first_tok63: Optional[str] = None
                        if _pre_token is not None:
                            _pk63, _pts63 = _pre_token
                            if _now_aj - _pts63 < _PRE_TOKEN_TTL:
                                _first_tok63 = _pk63
                            _pre_token = None   # 期限切れでもクリア

                        def _get_aj_tok63(_slot_idx63: int):
                            """1スロット分のTSトークンを取得（並列スレッド内）"""
                            _tok63i: Optional[str] = None
                            _now63i = time.time()
                            # ★ Fix 68: presolve/browser_presolve フラグを Ajax mode でも尊重
                            _presolve_on_aj  = _cfg_aj63.get("presolve_enabled", True)
                            _browser_pre_aj  = _cfg_aj63.get("browser_presolve", False) and _HAS_PW
                            # ② 共有プール（Queue はスレッドセーフ）— presolve=ON の場合のみ
                            if _presolve_on_aj:
                                try:
                                    _t63p, _ts63p = _shared_ts_pool.get_nowait()
                                    if _now63i - _ts63p < _PRE_TOKEN_TTL:
                                        _tok63i = _t63p
                                except _queue.Empty:
                                    pass
                            # ③ オンデマンドsolve（並列実行・スレッドセーフ）
                            if not _tok63i and _captcha_ready(_cfg_aj63):
                                _tok63i = _solve_turnstile_chain(
                                    _yc_aj63, _base_aj63, sitekey,
                                    proxy=_prx_aj63, stop_event=_stop_aj63)
                            # ④ ブラウザfallback — browser_presolve=ON かつ _HAS_PW の場合のみ
                            if not _tok63i and _browser_pre_aj:
                                _tok63i = _api_aj63._cf.solve_turnstile_browser(
                                    _base_aj63, sitekey, proxy=proxy,
                                    email=email, password=password,
                                    stop_event=_stop_aj63)
                                _api_aj63._inject_cf_clearance()
                            return _slot_idx63, _tok63i

                        # ★ Fix 70: _pre_token なし（JITモード or プール空）の場合もオンデマンドsolve
                        # Fix 68 はスロット[1以降]（_get_aj_tok63）のみカバー。
                        # スロット[0]もトークン未取得なら同じ経路でオンデマンドsolve。
                        if not _first_tok63:
                            _, _first_tok63 = _get_aj_tok63(0)

                        # スロット[0] = _pre_token（or オンデマンドsolve）、残りを並列取得
                        _stpairs_aj: list = [(_avail_aj[0], _first_tok63)]

                        if len(_avail_aj) > 1:
                            _nw63 = min(len(_avail_aj) - 1, 8)
                            self.logger.info(
                                f"Fix 63: Ajax TS取得 並列化 {_nw63}スロット同時"
                                f" (従来直列: 推定{_nw63 * 3.3:.0f}秒 → 並列: ~3秒)")
                            _tok63_map: dict = {}
                            with ThreadPoolExecutor(max_workers=_nw63) as _ex63:
                                _futs63 = {
                                    _ex63.submit(_get_aj_tok63, _i63): _i63
                                    for _i63 in range(1, len(_avail_aj))
                                }
                                for _f63 in as_completed(_futs63):
                                    try:
                                        _i63r, _t63r = _f63.result()
                                        _tok63_map[_i63r] = _t63r
                                    except Exception as _e63:
                                        _i63r = _futs63[_f63]
                                        _tok63_map[_i63r] = None
                                        self.logger.warning(
                                            f"Fix 63: スロット{_i63r} TS取得失敗: {_e63}")
                            for _i63s in range(1, len(_avail_aj)):
                                _stpairs_aj.append(
                                    (_avail_aj[_i63s], _tok63_map.get(_i63s)))

                        # 同時 POST
                        _aj_resmap:  dict       = {}
                        _aj_reslock: threading.Lock = threading.Lock()

                        def _book_aj_fn(_sl, _tok,
                                        _rm=_aj_resmap, _rl=_aj_reslock,
                                        _dbg_f=_dbg, _wid=self.wid,
                                        _em=email, _dt=date):
                            _r = api.book(_sl, cf_token=_tok)
                            with _rl:
                                _rm[_sl.get("startTime", "?")] = (_sl, _r)
                            # ★ Fix 61: Ajaxモードにもbooking結果デバッグ保存を追加
                            if _dbg_f:
                                _ts_b = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                                _st_b = _sl.get("startTime", "?").replace(":", "")
                                _debug_dump_json(
                                    f"fcfs_w{_wid}_{_ts_b}_book_{_dt}_{_st_b}",
                                    {"date": _dt, "slot": _sl, "result": _r,
                                     "has_cf_token": bool(_tok),
                                     "worker": _wid, "email": _em}
                                )

                        _aj_ths = [
                            threading.Thread(target=_book_aj_fn,
                                             args=(_s, _t), daemon=True)
                            for _s, _t in _stpairs_aj
                        ]
                        for _th in _aj_ths: _th.start()
                        for _th in _aj_ths: _th.join(timeout=20)

                        # 結果処理
                        _aj_need_relogin = False
                        for _stk, (_asl, _ares) in _aj_resmap.items():
                            _adate = _asl.get("slotDate", date)
                            if _ares == "won":
                                _nm_disp = f"  ({name})" if name else ""
                                with _con_lock:
                                    print(f"{Fore.GREEN}{Style.BRIGHT}"
                                          f"{self._tag()} ★ 当選![Ajax] BOOKED!"
                                          f" {_adate} {_stk} — {email}{_nm_disp}"
                                          f"{Style.RESET_ALL}")
                                mark_success(pair, f"{_adate},{_stk}")
                                return "won"
                            elif _ares == "not_won":
                                with _con_lock:
                                    print(f"{Fore.YELLOW}{self._tag()} [Ajax] ✗ 落選:"
                                          f" {_adate} {_stk}{Style.RESET_ALL}")
                            elif _ares == "cooldown":
                                # ★ Fix 52: Ajax COOLDOWN/SYSTEM_BUSY → 短い待機
                                _cd_w = random.uniform(1.0, 3.0)
                                with _con_lock:
                                    print(f"{Fore.YELLOW}{self._tag()} [Ajax] COOLDOWN:"
                                          f" {_adate} {_stk} → {_cd_w:.1f}s待機{Style.RESET_ALL}")
                                if self._stop.wait(_cd_w):
                                    break
                                _aj_need_relogin = True
                            elif _ares == "net_error":
                                with _con_lock:
                                    print(f"{Fore.RED}{self._tag()} [Ajax] NET_ERROR:"
                                          f" {_adate} {_stk}{Style.RESET_ALL}")
                                _aj_need_relogin = True
                            else:  # None = RATE_LIMIT
                                _fc = _record_slot_fail(_asl)
                                with _con_lock:
                                    if _fc >= _SLOT_FAIL_MAX:
                                        print(f"{Fore.RED}{self._tag()} [Ajax] RATE_LIMIT×{_fc}"
                                              f" (上限到達) → {_adate} {_stk} 永久スキップ"
                                              f"{Style.RESET_ALL}")
                                    else:
                                        print(f"{Fore.YELLOW}{self._tag()} [Ajax] RATE_LIMIT"
                                              f" [{_fc}/{_SLOT_FAIL_MAX}] {_adate} {_stk}"
                                              f"{Style.RESET_ALL}")

                        if _aj_need_relogin:
                            proxy = pick_proxy(self.proxies)
                            api   = MelloJoyAPI(self.cfg, self.logger, proxy=proxy,
                                                proxies_list=self.proxies)
                            if not api.login(email, password, stop_event=self._stop):
                                with _con_lock:
                                    print(f"{Fore.RED}{self._tag()} [Ajax] Re-login failed"
                                          f" → キューに戻す{Style.RESET_ALL}")
                                return "requeue"
                            token_ts = time.time()
                            yc = _captcha_client_chain(self.cfg, self.logger)   # ★ Fix 48
                    continue  # Ajax mode: sequential ループをスキップして次の date へ

                # ─── Sequential mode (既存ループ + Fix 47 ジッター) ─────────
                for slot in ordered:
                    if self._stop.is_set(): break
                    st  = slot.get("startTime", "?")
                    rem = slot.get("remaining", "?")

                    # グローバル 409 上限チェック → スキップ
                    if _slot_exhausted(slot):
                        with _con_lock:
                            print(f"{Fore.MAGENTA}{self._tag()} SKIP (409×{_SLOT_FAIL_MAX}+): {date} {st}{Style.RESET_ALL}")
                        continue

                    any_attempted = True   # exhaustedでないスロットに到達
                    with _con_lock:
                        print(f"{Fore.GREEN}{self._tag()} SLOT OPEN: {date} {st}  (remaining: {rem}){Style.RESET_ALL}")

                    # ★ ブラウザモード: スタンバイ済み → 即攻撃 / 未準備 → 通常起動
                    _bm_on = self.cfg.get("browser_mode", False) and _HAS_PW
                    if _bm_on:
                        if _bm_standby is not None:
                            # ── スタンバイ済み: ready_event を確認して即攻撃 ──
                            if not _bm_standby.ready_event.is_set():
                                # 前回の攻撃後 Turnstile 再取得中 → 最大 30s 待機
                                with _con_lock:
                                    print(f"{Fore.CYAN}{self._tag()} [BrowserMode] ⏳ Turnstile再取得中..."
                                          f"{Style.RESET_ALL}")
                                _bm_standby.ready_event.wait(timeout=30)

                            if _bm_standby.ready_event.is_set():
                                _bm_standby.ready_event.clear()   # consume
                                _bm_standby.cmd_queue.put(slot)   # ★ 即攻撃シグナル
                                with _con_lock:
                                    print(f"{Fore.CYAN}{Style.BRIGHT}"
                                          f"{self._tag()} [BrowserMode] ⚡ 即攻撃! {date} {st}"
                                          f"{Style.RESET_ALL}")
                                try:
                                    result = _bm_standby.res_queue.get(timeout=120)
                                except _queue.Empty:
                                    result = "net_error"
                                api._inject_cf_clearance()
                            else:
                                # ready タイムアウト → fallback: 新規ブラウザ起動
                                with _con_lock:
                                    print(f"{Fore.YELLOW}{self._tag()} [BrowserMode] ⚠ スタンバイ応答なし"
                                          f" → 新規起動 fallback{Style.RESET_ALL}")
                                _bm_lat2, _bm_lng2 = _resolve_geo(self.cfg.get("geo", "kanto_random"))
                                result = api._cf.book_via_browser(
                                    email, password, slot,
                                    _bm_lat2, _bm_lng2,
                                    proxy=proxy, stop_event=self._stop)
                                api._inject_cf_clearance()
                        else:
                            # スタンバイ未準備（タイムアウト等） → 通常 book_via_browser()
                            _bm_lat2, _bm_lng2 = _resolve_geo(self.cfg.get("geo", "kanto_random"))
                            with _con_lock:
                                print(f"{Fore.CYAN}{self._tag()} [BrowserMode] 起動中..."
                                      f" {date} {st}{Style.RESET_ALL}")
                            result = api._cf.book_via_browser(
                                email, password, slot,
                                _bm_lat2, _bm_lng2,
                                proxy=proxy, stop_event=self._stop)
                        # cf_clearance を HTTP セッションに inject（次の API リクエストに利用）
                        api._inject_cf_clearance()
                        if result == "won":
                            name_disp = f"  ({name})" if name else ""
                            with _con_lock:
                                print(f"{Fore.GREEN}{Style.BRIGHT}"
                                      f"{self._tag()} [BrowserMode] ★ 当選! {date} {st} — {email}{name_disp}"
                                      f"{Style.RESET_ALL}")
                            mark_success(pair, f"{date},{st}")
                            return "won"
                        elif result == "not_won":
                            name_disp = f"  ({name})" if name else ""
                            with _con_lock:
                                print(f"{Fore.YELLOW}{self._tag()} [BrowserMode] ✗ 落選"
                                      f" ({date} {st}) → 次の枠{name_disp}{Style.RESET_ALL}")
                            continue
                        elif result == "cooldown":
                            _cd_wait = random.uniform(1.0, 3.0)
                            with _con_lock:
                                print(f"{Fore.YELLOW}{self._tag()} [BrowserMode] COOLDOWN"
                                      f" {date} {st} → {_cd_wait:.1f}s待機{Style.RESET_ALL}")
                            if self._stop.wait(_cd_wait): break
                            proxy = pick_proxy(self.proxies)
                            api   = MelloJoyAPI(self.cfg, self.logger, proxy=proxy)
                            if not api.login(email, password, stop_event=self._stop):
                                with _con_lock:
                                    print(f"{Fore.RED}{self._tag()} [BrowserMode] Re-login failed"
                                          f" → キューに戻す{Style.RESET_ALL}")
                                return "requeue"
                            token_ts = time.time()
                            if self._stop.wait(retry_delay): break
                        elif result == "net_error":
                            with _con_lock:
                                print(f"{Fore.YELLOW}{self._tag()} [BrowserMode] NET_ERROR"
                                      f" {date} {st} → proxy rotate{Style.RESET_ALL}")
                            proxy = pick_proxy(self.proxies)
                            api   = MelloJoyAPI(self.cfg, self.logger, proxy=proxy)
                            if not api.login(email, password, stop_event=self._stop):
                                return "requeue"
                            token_ts = time.time()
                        else:  # None = RATE_LIMIT
                            _fc = _record_slot_fail(slot)
                            with _con_lock:
                                if _fc >= _SLOT_FAIL_MAX:
                                    print(f"{Fore.RED}{self._tag()} [BrowserMode] RATE_LIMIT×{_fc}"
                                          f" → {date} {st} 永久スキップ{Style.RESET_ALL}")
                                else:
                                    print(f"{Fore.YELLOW}{self._tag()} [BrowserMode] RATE_LIMIT"
                                          f" [{_fc}/{_SLOT_FAIL_MAX}] {date} {st}{Style.RESET_ALL}")
                        continue  # 次の slot へ

                    # Solve Turnstile（共有プールのトークンがあれば即使用、なければオンデマンド）
                    cf_token: Optional[str] = None
                    yc_proxy = None if self.cfg.get("yescaptcha_proxyless", True) else proxy
                    _presolve_on = self.cfg.get("presolve_enabled", True)
                    # ★ Fix 55: JITモード（presolve_enabled=False）はプールをスキップ
                    #   スロット検知後・POST直前にsolveするため、cf_clearanceとIPが一致しやすい
                    if _presolve_on:
                        # ★ Fix 23b / Fix 35: プールからトークン取得 or 古いトークンを入れ替え。
                        #   - _pre_token が None → 新規取得
                        #   - _pre_token の残り有効期限 < _PRE_REFRESH_MARGIN → プールに新しいものがあれば入れ替え
                        #     （古いものは廃棄。プールが空なら古いまま保持して使用を試みる）
                        _now_pt = time.time()
                        _pt_needs_refresh = (
                            _pre_token is None
                            or (_now_pt - _pre_token[1]) > _PRE_TOKEN_TTL - _PRE_REFRESH_MARGIN
                        )
                        if _pt_needs_refresh:
                            try:
                                _t, _ts = _shared_ts_pool.get_nowait()
                                if _now_pt - _ts < _PRE_TOKEN_TTL:
                                    _pre_token = (_t, _ts)   # 新鮮なトークンに更新
                                # else: プールのトークンも古い → 現状の _pre_token を維持
                            except _queue.Empty:
                                pass   # プール空 → 現状の _pre_token を維持
                        if _pre_token is not None:
                            _tok_str, _tok_ts = _pre_token
                            if time.time() - _tok_ts < _PRE_TOKEN_TTL:
                                cf_token = _tok_str
                                self.logger.debug(f"{self._tag()} 共有プールのTurnstileトークンを使用")
                            else:
                                self.logger.debug(f"{self._tag()} プリソルブトークン期限切れ → オンデマンドsolve")
                            _pre_token = None   # 使用/期限切れ問わずリセット
                    else:
                        # ★ Fix 55: JITモード — プリソルブトークンを使わず直接solve
                        _pre_token = None   # プールも使わない
                        self.logger.debug(f"{self._tag()} JITモード: POST直前にTurnstileをsolve")
                        # browser_presolve=True なら cf_clearance と同一ブラウザで solve（最高整合性）
                        _jit_use_browser = self.cfg.get("browser_presolve", False) and _HAS_PW
                        if _jit_use_browser:
                            _wait(f"{self._tag()} JIT browser solve...")
                            # ★ Fix 57: ログイン後のページでTurnstileを取得するためemail/passwordを渡す
                            cf_token = api._cf.solve_turnstile_browser(
                                f"{base_url}/", sitekey, proxy=proxy,
                                email=email, password=password,
                                stop_event=self._stop)
                            api._inject_cf_clearance()
                        elif _captcha_ready(self.cfg):
                            _wait(f"{self._tag()} JIT capsolver solve...")
                            cf_token = _solve_turnstile_chain(
                                yc, f"{base_url}/", sitekey, proxy=yc_proxy,
                                stop_event=self._stop)
                    # ★ Fix 55: presolveモード時のフォールバック（プール空・期限切れ時）
                    #   JITモードはここに到達しない（上のブロックで解決済み or None確定）
                    if not cf_token and _presolve_on and _captcha_ready(self.cfg):
                        _wait(f"{self._tag()} Solving Turnstile...")
                        # ★ Fix 46: 予約ページは / (root)。旧: /register (会員登録ページ)
                        # ★ Fix 48: フォールバックチェーン
                        cf_token = _solve_turnstile_chain(
                            yc, f"{base_url}/", sitekey, proxy=yc_proxy,
                            stop_event=self._stop)
                    if not cf_token and _HAS_PW and _presolve_on and self.cfg.get("browser_presolve", False):
                        # presolveモードのブラウザfallback（JITブラウザは上で処理済み）
                        # ★ Fix 69: browser_presolve=OFF の場合はfallbackもスキップ（遅延ゼロ・整合性確保）
                        _wait(f"{self._tag()} Turnstile browser fallback...")
                        # ★ Fix 46: page_url に / を渡す（solve_turnstile_browser が正しく使う）
                        cf_token = api._cf.solve_turnstile_browser(
                            f"{base_url}/", sitekey, proxy=proxy,
                            email=email, password=password,  # ★ Fix 61: Fix 57と同様の対応（ログインページ対策）
                            stop_event=self._stop)   # ★ Fix 26: 停止時に即脱出
                        # ★ Fix 46: ブラウザから cf_clearance を取得できていれば _sess に inject
                        api._inject_cf_clearance()

                    if not cf_token:
                        with _con_lock:
                            print(f"\033[33m[!] {self._tag()} ⚠ cf_token=None — "
                                  f"turnstileToken なしで booking送信（サーバー拒否→✗ 落選の可能性大）"
                                  f"\033[0m", flush=True)

                    # Attempt booking（★ Fix 47: 非Ajaxモード POST直前ジッター）
                    if _ajax_jitter > 0:
                        time.sleep(random.uniform(0, _ajax_jitter))
                    result = _book_dispatch(self, api, slot, cf_token)   # fire_mode: C++同期一斉発火 / OFF時は従来 book()

                    # デバッグ: 予約結果を保存
                    if _dbg:
                        _ts_book = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        _debug_dump_json(
                            f"fcfs_w{self.wid}_{_ts_book}_book_{date}_{st}",
                            {"date": date, "slot": slot, "result": result,
                             "has_cf_token": bool(cf_token),
                             "worker": self.wid, "email": email}
                        )

                    if result == "won":
                        name_disp = f"  ({name})" if name else ""
                        with _con_lock:
                            print(f"{Fore.GREEN}{Style.BRIGHT}"
                                  f"{self._tag()} ★ 当選! BOOKED! {date} {st} — {email}{name_disp}"
                                  f"{Style.RESET_ALL}")
                        mark_success(pair, f"{date},{st}")
                        return "won"
                    elif result == "not_won":
                        # 落選確定 → 同アカウントで次のスロットをリトライ
                        name_disp = f"  ({name})" if name else ""
                        with _con_lock:
                            print(f"{Fore.YELLOW}{self._tag()} ✗ 落選 ({date} {st}) → 次の枠を試みます{name_disp}{Style.RESET_ALL}")
                        continue  # 次の slot へ
                    elif result == "cooldown":
                        # ★ Fix 52: COOLDOWN / SYSTEM_BUSY → 短い待機 (1〜3s) + proxy rotate + re-login
                        _cd_wait = random.uniform(1.0, 3.0)
                        with _con_lock:
                            print(f"{Fore.YELLOW}{self._tag()} COOLDOWN (アカウント制限)  {date} {st}"
                                  f" → {_cd_wait:.1f}s待機後 proxy rotate{Style.RESET_ALL}")
                        if self._stop.wait(_cd_wait): break
                        proxy = pick_proxy(self.proxies)
                        api   = MelloJoyAPI(self.cfg, self.logger, proxy=proxy)
                        if not api.login(email, password, stop_event=self._stop):   # ★ Fix 31
                            with _con_lock:
                                print(f"{Fore.RED}{self._tag()} Re-login failed (COOLDOWN): {email} → キューに戻す{Style.RESET_ALL}")
                            return "requeue"
                        token_ts = time.time()
                        yc = _captcha_client_chain(self.cfg, self.logger)   # ★ Fix 48
                        if self._stop.wait(retry_delay): break
                    elif result == "net_error":
                        # ネット/SSL エラー → proxy rotate + re-login（スロット失敗カウントは増やさない）
                        with _con_lock:
                            print(f"{Fore.YELLOW}{self._tag()} NET_ERROR (SSL/接続エラー)  {date} {st} → proxy rotate{Style.RESET_ALL}")
                        proxy = pick_proxy(self.proxies)
                        api   = MelloJoyAPI(self.cfg, self.logger, proxy=proxy)
                        if not api.login(email, password, stop_event=self._stop):   # ★ Fix 31
                            with _con_lock:
                                print(f"{Fore.RED}{self._tag()} Re-login failed (NET_ERROR): {email} → キューに戻す{Style.RESET_ALL}")
                            return "requeue"
                        token_ts = time.time()
                        yc = _captcha_client_chain(self.cfg, self.logger)   # ★ Fix 48
                        if self._stop.wait(retry_delay): break
                    else:  # None = 409 RATE_LIMIT → スロットスキップカウント増加
                        fail_count = _record_slot_fail(slot)
                        with _con_lock:
                            if fail_count >= _SLOT_FAIL_MAX:
                                print(f"{Fore.RED}{self._tag()} RATE_LIMIT×{fail_count} (上限{_SLOT_FAIL_MAX}到達) → {date} {st} を永久スキップ{Style.RESET_ALL}")
                            else:
                                print(f"{Fore.YELLOW}{self._tag()} RATE_LIMIT [{fail_count}/{_SLOT_FAIL_MAX}]  {date} {st} → proxy rotate{Style.RESET_ALL}")
                        proxy = pick_proxy(self.proxies)
                        api   = MelloJoyAPI(self.cfg, self.logger, proxy=proxy)
                        if not api.login(email, password, stop_event=self._stop):   # ★ Fix 31
                            with _con_lock:
                                print(f"{Fore.RED}{self._tag()} Re-login failed (RATE_LIMIT): {email} → キューに戻す{Style.RESET_ALL}")
                            return "requeue"
                        token_ts = time.time()
                        yc = _captcha_client_chain(self.cfg, self.logger)   # ★ Fix 48
                        if self._stop.wait(retry_delay): break

            if not found_any or not any_attempted:
                # スロットなし or 全スロットexhausted → monitor_delay待機
                # スロットル: 最低 _STATUS_PRINT_MIN_SEC 秒に1回だけ表示
                _now = time.time()
                if _now - _last_status_ts >= _STATUS_PRINT_MIN_SEC:
                    now_str    = datetime.datetime.now().strftime("%H:%M:%S")
                    delay_disp = f"{mon_delay:g}s"
                    reason     = "Waiting for restock" if found_any else "No slots available"
                    acct_disp  = f" [{email}]" if email else ""
                    with _con_lock:
                        print(f"{Fore.MAGENTA}{self._tag()}{acct_disp} [{now_str}] {reason}...  "
                              f"(next check in {delay_disp}){Style.RESET_ALL}")
                    _last_status_ts = _now

                # ── プリソルブ（グローバル共有プール方式） ───────────────────────
                # raw_found=True（日程データあり）かつプールに空きがある時のみ
                # _presolve_sem でワーカー数に依らず同時solve数を制限
                # 例: parallel=100 → pool_size=max(20,100)=100 → 最大100並列solve  ★ Fix 66
                _yc_proxy_pre = None if self.cfg.get("yescaptcha_proxyless", True) else proxy
                # ★ Fix 23a: qsize() < maxsize チェックを削除。
                #   _presolve_sem が同時solve数を制限し、put_nowait の Full 例外で
                #   キュー溢れを吸収するため、qsize() の非アトミック読み取りは不要。
                if ((raw_found or self.cfg.get("presolve_prewarm", False))   # prewarm: 解禁前(スロット無し)でも充填継続
                        and self.cfg.get("presolve_enabled", True)   # ★ Fix 67: JITモード時はプリソルブをスキップ
                        and _captcha_ready(self.cfg)
                        and _presolve_sem.acquire(blocking=False)):

                    def _do_presolve(_yc=yc, _base=base_url, _sk=sitekey,
                                     _prx=_yc_proxy_pre, _pool=_shared_ts_pool,
                                     _sem=_presolve_sem,
                                     _stop_ev=self._stop,
                                     _log=self.logger, _tag=self._tag()):   # ★ Fix 27: stop_event をクロージャで捕捉
                        try:
                            # ★ Fix 46: 予約ページは / (root)。旧: /register (会員登録ページ)
                            # ★ Fix 48: フォールバックチェーン
                            token = _solve_turnstile_chain(_yc, f"{_base}/", _sk,
                                                           proxy=_prx,
                                                           stop_event=_stop_ev)
                            if token:
                                try:
                                    _pool.put_nowait((token, time.time()))
                                    # 可視化: プリソルブ成功（プール充填中のみ出る。満杯になれば静かになる）
                                    _log.info(f"{_tag} presolve ✓ Turnstileトークン取得 "
                                              f"(pool={_pool.qsize()}/{_pool.maxsize})")
                                except _queue.Full: pass   # 他ワーカーが先に埋めた（満杯）
                        except Exception:
                            pass
                        finally:
                            _sem.release()   # 完了 → 次のworkerがacquire可能に

                    # ★ Fix 12: Thread.start() 失敗時にセマフォをリークしない
                    try:
                        threading.Thread(target=_do_presolve, daemon=True).start()
                    except Exception as _te:
                        _presolve_sem.release()
                        self.logger.warning(f"{self._tag()} presolve thread start failed: {_te}")

                if self._stop.wait(mon_delay): break

                # ★ Fix 35: 共有プールからトークン取得 or 古いトークンを入れ替え。
                #   _pre_token が None、または残り有効期限 < _PRE_REFRESH_MARGIN なら更新を試みる。
                _now_pm = time.time()
                _pm_needs_refresh = (
                    _pre_token is None
                    or (_now_pm - _pre_token[1]) > _PRE_TOKEN_TTL - _PRE_REFRESH_MARGIN
                )
                if _pm_needs_refresh:
                    try:
                        tok, ts = _shared_ts_pool.get_nowait()
                        if _now_pm - ts < _PRE_TOKEN_TTL:
                            _pre_token = (tok, ts)
                            self.logger.debug(f"{self._tag()} 共有プールからTurnstileトークン取得/更新")
                        # else: プールのトークンも古い → 現状の _pre_token を維持
                    except _queue.Empty:
                        pass   # プール空 → 現状の _pre_token を維持

        # ★ Fix 71e: スタンバイ終了処理は _monitor_account の finally（_shutdown_standby）に移動
        return "stopped" if self._stop.is_set() else "done"

def _probe_stores(cfg, lo: int = 1, hi: int = 12) -> list:
    """store_id 候補を bookable-dates で軽くプローブし、[(store_id, 解禁日数)] を返す。
       CF/ネット等で失敗したら空リスト（その場合は手動入力にフォールバック）。"""
    base = cfg.get("base_url", "https://mellojoylucky.cn")
    found = []
    try:
        import requests as _rq
    except Exception:
        return found
    for sid in range(lo, hi + 1):
        try:
            r = _rq.get(f"{base}/api/registrations/bookable-dates?store_id={sid}",
                        timeout=4, headers={"accept": "application/json"})
            if r.status_code == 200:
                j = r.json()
                data = j.get("data") if isinstance(j, dict) else None
                dates = (data or {}).get("dates") if isinstance(data, dict) else \
                        (j.get("dates") if isinstance(j, dict) else None)
                if isinstance(dates, list):
                    found.append((sid, len(dates)))
        except Exception:
            pass
    return found


def _prompt_store_id(cfg: dict) -> int:
    """実行時に store_id を選択。既定は cfg['store_id']。'p' で有効店舗をプローブ。
       選んだ値は cfg に保存して次回の既定にする。"""
    cur = int(cfg.get("store_id", 2))
    print(f"\n  {Fore.CYAN}── 店舗ID(store_id) 選択 ──{Style.RESET_ALL}  現在={Fore.WHITE}{cur}{Style.RESET_ALL}")
    print(f"  {Style.DIM}数値=その店舗 / 空Enter=現在値({cur}) / 'p'=有効な店舗をプローブ{Style.RESET_ALL}")
    s = input(f"  store_id > ").strip().lower()
    if s == "p":
        print("  プローブ中... (bookable-dates で store_id 1-12 を確認)")
        res = _probe_stores(cfg)
        if res:
            print(f"  {Fore.GREEN}有効な店舗ID:{Style.RESET_ALL}")
            for sid, nd in res:
                _mk = "  ← 現在" if sid == cur else ""
                print(f"    store_id={Fore.WHITE}{sid}{Style.RESET_ALL}  解禁日={nd}日{_mk}")
        else:
            print(f"  {Fore.YELLOW}プローブ失敗/該当なし（CF等）。手動で入力してください。{Style.RESET_ALL}")
        s = input(f"  store_id > ").strip()
    if s.isdigit() and int(s) > 0:
        cfg["store_id"] = int(s)
        try: save_cfg(cfg)
        except Exception: pass
    sid = int(cfg.get("store_id", cur))
    print(f"  → {Fore.GREEN}store_id = {sid}{Style.RESET_ALL} を使用します")
    return sid


# ── FCFS Menu ─────────────────────────────────────────────────────────────────
def menu_fcfs(cfg: dict):
    _box("FCFS — First Come First Served")
    _prompt_store_id(cfg)

    accounts = load_accounts()
    if not accounts:
        _err(f"accounts.txt is empty: {ACCOUNTS_F}")
        _info("Add  email:password  lines to accounts.txt")
        _pause()
        return

    proxies  = load_proxies()
    parallel = min(int(cfg.get("parallel", 3)), len(accounts))

    _wh_count = len(load_webhooks())
    _pt_disp  = ', '.join(cfg.get('preferred_times', [])) or '(なし)'
    # ── presolve 診断 ───────────────────────────────────────────────────────────
    # fire_core（旧C++同期一斉発火, fire_mode）は廃止。常に従来 book() を使用。
    _fire_disp = f"{Fore.WHITE}（fire_core は廃止済み・従来 book() を使用）{Style.RESET_ALL}"
    _ps_on  = cfg.get("presolve_enabled", True)
    _cap_ok = _captcha_ready(cfg)
    _pw_on = cfg.get("presolve_prewarm", False)
    if _ps_on and _cap_ok:
        _ps_disp = (f"{Fore.GREEN}ON{Style.RESET_ALL}（captchaキーOK"
                    + (f" / {Fore.CYAN}prewarm:ON 解禁前も連続充填{Style.RESET_ALL}" if _pw_on
                       else " / ※スロット検知後に起動")
                    + "）")
    elif _ps_on and not _cap_ok:
        _ps_disp = f"{Fore.RED}ON だが captchaキー未設定 → プリソルブ不可{Style.RESET_ALL}（Settingsでキー設定を）"
    else:
        _ps_disp = f"{Fore.WHITE}OFF（JIT: POST直前にsolve）{Style.RESET_ALL}"
    print(f"""
  Accounts  : {Fore.WHITE}{len(accounts)}{Style.RESET_ALL}
  Proxies   : {Fore.WHITE}{len(proxies) if proxies else 'none (direct)'}{Style.RESET_ALL}
  Parallel  : {Fore.WHITE}{parallel}{Style.RESET_ALL}
  Devices   : {Fore.WHITE}{', '.join(cfg.get('devices',[]))} ({cfg.get('device_mode','random')}){Style.RESET_ALL}
  Region    : {Fore.WHITE}{GEO_LABELS.get(cfg.get('geo',''), cfg.get('geo',''))}{Style.RESET_ALL}
  Monitor Δ : {Fore.WHITE}{float(cfg.get('monitor_delay',0.5)):g}s{Style.RESET_ALL}{Fore.YELLOW + '  ★ 推奨: 0.5s以下' if float(cfg.get('monitor_delay',0.5)) > 1.0 else ''}{Style.RESET_ALL}
  検知TTL   : {Fore.WHITE}dates={float(cfg.get('dates_cache_ttl',0.9)):g}s / slots={float(cfg.get('slots_cache_ttl',0.45)):g}s{Style.RESET_ALL}  ← 小さいほど解禁を早く検知
  Retry Δ   : {Fore.WHITE}{float(cfg.get('retry_delay',3)):g}s{Style.RESET_ALL}
  Webhooks  : {Fore.WHITE}{_wh_count} URL(s){'  ← webhooks.txt' if _wh_count == 0 else ''}{Style.RESET_ALL}
  優先時間枠 : {Fore.WHITE}{_pt_disp}{Style.RESET_ALL}  ← accounts.txtの4フィールド目でアカウント別指定も可
  BrowserMode: {Fore.GREEN + 'ON  ← Playwright で予約フォームを直接操作' if cfg.get('browser_mode') else Fore.WHITE + 'OFF ← HTTP リクエストで予約（通常モード）'}{Style.RESET_ALL}
  FireMode   : {_fire_disp}
  Presolve   : {_ps_disp}
""")
    if input(f"{Fore.WHITE}Start? [y/N]: {Style.RESET_ALL}").strip().lower() != "y":
        return

    # ── タイマー設定 ──────────────────────────────────────────────────────────
    timer_raw = input(
        f"{Fore.WHITE}タイマー設定 (HH:MM または HH:MM:SS で指定 / Enter でスキップ): {Style.RESET_ALL}"
    ).strip()
    if timer_raw:
        try:
            parts = timer_raw.split(":")
            hh = int(parts[0])
            mm = int(parts[1])
            ss = int(parts[2]) if len(parts) >= 3 else 0
            now    = datetime.datetime.now()
            target = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            print(f"\n{Fore.CYAN}⏱  {target.strftime('%Y-%m-%d %H:%M:%S')} にスタートします{Style.RESET_ALL}")
            while True:
                remain = (target - datetime.datetime.now()).total_seconds()
                if remain <= 0:
                    break
                h_r, rem_r = divmod(int(remain), 3600)
                m_r, s_r   = divmod(rem_r, 60)
                print(f"\r  スタートまで {h_r:02d}:{m_r:02d}:{s_r:02d} ...   ",
                      end="", flush=True)
                time.sleep(min(0.1, max(0.0, remain)))   # 秒指定時も±0.1s精度で発火
            print(f"\n{Fore.GREEN}✓ タイマー発火! {datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}{Style.RESET_ALL}\n")
        except (ValueError, IndexError):
            _warn("タイマー形式エラー (HH:MM または HH:MM:SS で指定してください) → スキップ")

    # ── 同一氏名アカウント間の枠分散マップを構築 ──────────────────────────────────
    # pair をキーとして (name_rank, name_count) を事前計算し _acct_q に投入する
    _name_index: dict[str, list[str]] = {}   # 氏名 → [pair, ...]
    for pair in accounts:
        parts = pair.split(":", 2)
        nm    = parts[2].strip() if len(parts) > 2 else ""
        _name_index.setdefault(nm, []).append(pair)

    # pair → (rank, count)
    _name_rank_map: dict[str, tuple[int, int]] = {}
    for nm, pairs in _name_index.items():
        cnt = len(pairs)
        for rank, pair in enumerate(pairs):
            _name_rank_map[pair] = (rank, cnt)

    # 同一氏名が複数いれば表示
    multi = {nm: ps for nm, ps in _name_index.items() if len(ps) > 1}
    if multi:
        for nm, ps in multi.items():
            print(f"  {Fore.CYAN}[枠分散]{Style.RESET_ALL} 氏名「{nm or '(なし)'}」: "
                  f"{len(ps)} アカウント → 枠オフセット自動割り当て")

    # ── Turnstileプリソルブ共有プールを初期化 ─────────────────────────────────
    global _shared_ts_pool, _presolve_sem, _DATES_CACHE_TTL, _SLOT_CACHE_TTL
    # 解禁検知TTLを config から反映（先着レースの最重要レバー）
    _DATES_CACHE_TTL = float(cfg.get("dates_cache_ttl", 0.9))
    _SLOT_CACHE_TTL  = float(cfg.get("slots_cache_ttl", 0.45))
    # ★ Fix 66: プールサイズを parallel に合わせてスケール。
    # parallel=100 なら pool_size=100。全ワーカーが同時にスロットオープンを検知した際、
    # プール不足でトークンなしになるワーカーを減らす（下限20は元の動作を維持）。
    _pool_size = max(20, parallel)   # ★ Fix 66: 20固定 → max(20, parallel)
    while not _shared_ts_pool.empty():
        try: _shared_ts_pool.get_nowait()
        except _queue.Empty: break
    _shared_ts_pool = _queue.Queue(maxsize=_pool_size)
    _presolve_sem   = threading.Semaphore(_pool_size)

    # ── タイマー発火直後にTurnstileプールを事前充填 ──────────────────────────
    # ワーカー起動前にcapsolverで解いたトークンをプールに貯めておく。
    # これにより、スロットオープン時にワーカーが即座にトークンを使える。
    # ★ Fix 54: browser_presolve=True の場合はPlaywrightブラウザで solve。
    #   cf_clearance と同じブラウザ・同一IPで Turnstile を解くため、
    #   Cloudflare が「コンテキスト整合」と判断し通過率が向上する。
    # ★ Fix 55: presolve_enabled=False の場合はプリソルブをスキップ（JITモード）。
    #   スロット検知後・POST直前にTurnstileをsolveする。
    _presolve_enabled = cfg.get("presolve_enabled", True)
    _browser_presolve = cfg.get("browser_presolve", False) and _HAS_PW
    _should_presolve  = _presolve_enabled and (_captcha_ready(cfg) or _browser_presolve)

    if _should_presolve:
        _pre_base_url = cfg.get("base_url", "https://mellojoylucky.cn")
        _pre_sitekey  = cfg.get("cf_sitekey", "0x4AAAAAAFF0MPh5SYT7ywAo")
        _pre_logger   = logging.getLogger("presolve")
        _pre_yc       = _captcha_client_chain(cfg, _pre_logger) if _captcha_ready(cfg) else []
        # ★ Fix 54: ブラウザプリソルブ用 CloudflareBypass インスタンス（_browser_lock で排他制御済み）
        _pre_cf       = CloudflareBypass(cfg, _pre_logger) if _browser_presolve else None
        # ★ Fix 34: プリソルブ枚数を parallel 数に合わせる（上限10の撤廃）
        #   20パラレルなら20枚プリソルブし、全ワーカーが起動時にトークンを持てるようにする。
        #   capsolver は並列リクエストに対して安定しているため上限は pool_size のみとする。
        _pre_fill_n   = min(_pool_size, parallel)
        # ★ Fix 59: プリソルブは常にcapsolver（browser_presolve設定に関わらず）
        _solver_disp  = cfg.get("captcha_provider", "capsolver")
        _browser_note = " [browser_presolve=ONはJITモードのPOST直前に適用]" if _browser_presolve else ""
        print(f"{Fore.CYAN}⏳ Turnstileトークンをプリソルブ中 ({_pre_fill_n}並列, solver={_solver_disp}{_browser_note})...{Style.RESET_ALL}")

        _pre_ok_count = [0]   # スレッドセーフカウンタ（リスト経由）
        _pre_lock     = threading.Lock()
        _pre_done_ev  = threading.Event()   # 最初の1件完了通知

        def _pre_fill_one(_yc=_pre_yc, _base=_pre_base_url, _sk=_pre_sitekey,
                          _pool=_shared_ts_pool, _sem=_presolve_sem,
                          _cf=_pre_cf, _use_browser=_browser_presolve):
            try:
                tok = None
                # ★ Fix 59: browser_presolve=ON でもプリソルブプール充填はブラウザ不使用。
                #   理由: プール充填時はアカウントが未確定のためemail/passwordを渡せず、
                #   ブラウザが /→ログインページにリダイレクトされてTurnstileを取得できない。
                #   ブラウザsolveはJITモード（POST直前・email/password確定後）のみで使用する。
                #   → プール充填は常にcapsolverのみ使用。
                # (Fix 54 browser_presolve ブロックをプリソルブからは除外)
                _ = _use_browser   # 変数参照のみ（将来の拡張用）
                # capsolver solve のみ（または通常モードの capsolver solve）
                if not tok and _yc:
                    # ★ Fix 46: 予約ページは / (root)。旧: /register (会員登録ページ)
                    # ★ Fix 48: フォールバックチェーン
                    tok = _solve_turnstile_chain(_yc, f"{_base}/", _sk)
                if tok:
                    try:
                        _pool.put_nowait((tok, time.time()))
                        with _pre_lock:
                            _pre_ok_count[0] += 1
                            n = _pre_ok_count[0]
                        with _con_lock:
                            print(f"{Fore.GREEN}  ✅ プリソルブ成功 [{n}/{_pre_fill_n}]  "
                                  f"(pool: {_pool.qsize()}/{_pool.maxsize}){Style.RESET_ALL}")
                        _pre_done_ev.set()   # 最初の1件完了を通知
                    except _queue.Full:
                        pass
                else:
                    with _con_lock:
                        print(f"{Fore.YELLOW}  ⚠ プリソルブ失敗 (token=None){Style.RESET_ALL}")
            except Exception as e:
                with _con_lock:
                    print(f"{Fore.YELLOW}  ⚠ プリソルブ例外: {e}{Style.RESET_ALL}")
            finally:
                _sem.release()

        for _ in range(_pre_fill_n):
            if _presolve_sem.acquire(blocking=False):
                # ★ Fix 24: Thread.start() 失敗時に _pre_fill_one の finally が
                #   実行されないためセマフォリークが起きる（Fix 12 と同様）
                try:
                    threading.Thread(target=_pre_fill_one, daemon=True).start()
                except Exception as _e:
                    _presolve_sem.release()
                    with _con_lock:
                        print(f"{Fore.YELLOW}  ⚠ presolve thread start failed: {_e}{Style.RESET_ALL}")

        # 最低1件のトークンが揃うまで最大15秒待つ（ワーカー起動前に保証）
        _pre_done_ev.wait(timeout=15.0)
        with _con_lock:
            _final_n = _shared_ts_pool.qsize()
            if _final_n > 0:
                print(f"{Fore.GREEN}✅ プリソルブ完了: {_final_n}件トークン準備完了 → ワーカー起動{Style.RESET_ALL}")
            else:
                print(f"{Fore.YELLOW}⚠ プリソルブ0件（タイムアウト or capsolver失敗）→ オンデマンドsolveで続行{Style.RESET_ALL}")

    # ── 共有キューに全アカウントを投入 ────────────────────────────────────────
    # グローバル _acct_q をクリアしてから再投入（前回実行の残留を防ぐ）
    while not _acct_q.empty():
        try: _acct_q.get_nowait()
        except _queue.Empty: break

    for pair in accounts:
        rank, cnt = _name_rank_map.get(pair, (0, 1))
        # ★ Fix 32: retry_count=0 を4フィールド目に追加（run()が上限管理に使用）
        _acct_q.put((pair, rank, cnt, 0))

    workers: list[FCFSWorker] = []
    futures: dict             = {}

    # ★ Fix 73: ブラウザモードではブラウザ同時起動数を並列数に合わせる
    #   （待機ブラウザ = 並列数、予備2 = 待機失敗時の再起動・Turnstile 取得用）
    if cfg.get("browser_mode", False) and _HAS_PW:
        _bl = max(5, parallel + 2)
        _set_browser_limit(_bl)
        print(f"{Fore.CYAN}  ブラウザ同時起動上限: {_bl}（並列数 {parallel} + 予備 2）{Style.RESET_ALL}")
        if parallel > 30:
            _warn(f"ブラウザを {parallel} 個同時に起動します。1個あたり約0.4〜0.7GBのメモリを使います。")
    else:
        _set_browser_limit(5)

    print(f"\n{Fore.CYAN}FCFS running ({parallel} workers / {len(accounts)} accounts)..."
          f" Press Ctrl+C to stop{Style.RESET_ALL}\n")

    def _stop_all():
        for w in workers: w.stop()

    # parallel 個の永続ワーカーを起動。各ワーカーは _acct_q が空になるまで
    # アカウントを取り出して監視を続ける。re-login 失敗時はキューに戻す。
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for wid in range(1, parallel + 1):
            w = FCFSWorker(cfg, proxies, wid=wid)
            workers.append(w)
            futures[pool.submit(w.run)] = w

        try:
            for f in as_completed(futures):
                w = futures[f]
                try:
                    won = f.result()
                    if won > 0:
                        _ok(f"Worker {w.wid} 終了 (当選: {won} アカウント)")
                    else:
                        _warn(f"Worker {w.wid} 終了 (当選なし)")
                except Exception as e:
                    _err(f"Worker {w.wid} exception: {e}")
        except KeyboardInterrupt:
            _warn("Stopping all workers...")
            _stop_all()

    print()
    _ok("FCFS session ended.")
    _pause()

# ── Settings Menu ─────────────────────────────────────────────────────────────
def menu_settings(cfg: dict) -> dict:
    _box("Settings")
    while True:
        print(f"""
  {Fore.CYAN}[1]{Style.RESET_ALL} Parallel workers      : {Fore.WHITE}{cfg.get('parallel',3)}{Style.RESET_ALL}
  {Fore.CYAN}[2]{Style.RESET_ALL} Monitor delay (s)     : {Fore.WHITE}{float(cfg.get('monitor_delay',0.5)):g}{Style.RESET_ALL}{Fore.YELLOW + '  ★ 推奨0.5s以下' if float(cfg.get('monitor_delay',0.5)) > 1.0 else ''}{Style.RESET_ALL}
  {Fore.CYAN}[3]{Style.RESET_ALL} Retry delay (s)       : {Fore.WHITE}{float(cfg.get('retry_delay',3)):g}{Style.RESET_ALL}
  {Fore.CYAN}[4]{Style.RESET_ALL} Devices               : {Fore.WHITE}{', '.join(cfg.get('devices',[]))}{Style.RESET_ALL}
  {Fore.CYAN}[5]{Style.RESET_ALL} Device mode           : {Fore.WHITE}{cfg.get('device_mode','random')}{Style.RESET_ALL}
  {Fore.CYAN}[6]{Style.RESET_ALL} Region (geolocation)  : {Fore.WHITE}{GEO_LABELS.get(cfg.get('geo',''), cfg.get('geo',''))}{Style.RESET_ALL}
  {Fore.CYAN}[7]{Style.RESET_ALL} IMAP settings
  {Fore.CYAN}[8]{Style.RESET_ALL} Browser               : {Fore.WHITE}{cfg.get('browser','chromium')}{Style.RESET_ALL}
  {Fore.CYAN}[9]{Style.RESET_ALL} Captcha API           : {Fore.WHITE}{('✓ ON  [' + cfg.get('captcha_provider','yescaptcha') + ']  ' + ('key set' if (cfg.get('captcha_key') or cfg.get('yescaptcha_key')) else 'key NOT set') + (('  +' + str(len(cfg.get('captcha_fallback',[]))) + 'fb') if cfg.get('captcha_fallback') else '')) if cfg.get('captcha_enabled', True) else '✗ OFF (ブラウザのみ)'}{Style.RESET_ALL}
  {Fore.CYAN}[D]{Style.RESET_ALL} Debug mode (HTML+HAR) : {Fore.WHITE}{'ON  ← debug/ にHTML・HARを保存' if cfg.get('debug_mode') else 'OFF'}{Style.RESET_ALL}
  {Fore.CYAN}[T]{Style.RESET_ALL} 優先時間枠             : {Fore.WHITE}{', '.join(cfg.get('preferred_times', [])) or '(なし)'}{Style.RESET_ALL}
  {Fore.CYAN}[A]{Style.RESET_ALL} Ajax mode             : {Fore.WHITE}{'ON  ← 全枠同時POST' if cfg.get('ajax_mode') else 'OFF ← 枠を順番に試行'}{Style.RESET_ALL}  [jitter={cfg.get('ajax_jitter_max', 0.3):.2g}s]
  {Fore.CYAN}[C]{Style.RESET_ALL} 解禁検知TTL(s)         : {Fore.WHITE}dates={float(cfg.get('dates_cache_ttl',0.9)):g} / slots={float(cfg.get('slots_cache_ttl',0.45)):g}{Style.RESET_ALL}  ← 小さいほど早く検知（先着の肝）
  {Fore.CYAN}[P]{Style.RESET_ALL} Presolve mode         : {Fore.WHITE}{'ON  ← スロット前に事前取得（高速）' if cfg.get('presolve_enabled', True) else 'OFF ← JIT・POST直前に取得（IP整合性↑）'}{Style.RESET_ALL}  ← Fix 55
  {Fore.CYAN}[B]{Style.RESET_ALL} Browser presolve      : {Fore.WHITE}{'ON  ← ブラウザでプリソルブ（IP整合性↑/推奨）' if cfg.get('browser_presolve') else 'OFF ← capsolver APIでプリソルブ（高速）'}{Style.RESET_ALL}  ← Fix 54
  {Fore.CYAN}[M]{Style.RESET_ALL} Browser mode          : {Fore.WHITE}{'ON  ← Playwright で予約フォームを直接操作（GPS/Turnstile 自動通過）' if cfg.get('browser_mode') else 'OFF ← HTTP リクエストで予約（通常モード）'}{Style.RESET_ALL}
  {Fore.CYAN}[W]{Style.RESET_ALL} Webhook 確認           : {Fore.WHITE}{len(load_webhooks())} URL(s)  ← webhooks.txt を直接編集{Style.RESET_ALL}
  {Style.DIM}  ※ Fire Engine[6]の設定（リトライ/プール/リード/grace/ソルブ並列）は[6]起動時に入力・記憶{Style.RESET_ALL}
  {Fore.CYAN}[0]{Style.RESET_ALL} Back / Save
""")
        c = input(f"{Fore.WHITE}> {Style.RESET_ALL}").strip()
        if   c == "1": cfg["parallel"]      = _inp_int("Parallel workers", cfg.get("parallel",3))
        elif c == "2":
            v = _inp_float("Monitor delay (s) [推奨: 0.5 / 最速: 0.1]", float(cfg.get("monitor_delay",0.5)))
            cfg["monitor_delay"] = v
            if v > 1.0:
                print(f"{Fore.YELLOW}  ⚠ monitor_delay={v:g}s は遅めです。0.5s以下を推奨します。{Style.RESET_ALL}")
        elif c == "3": cfg["retry_delay"]   = _inp_float("Retry delay (s)", float(cfg.get("retry_delay",3)))
        elif c == "4": _choose_devices(cfg)
        elif c == "5":
            ms = ["random","ratio"]
            for i,m in enumerate(ms): print(f"  [{i+1}] {m}")
            x = input("Mode: ").strip()
            if x.isdigit() and 1<=int(x)<=len(ms): cfg["device_mode"] = ms[int(x)-1]
        elif c == "6": _choose_geo(cfg)
        elif c == "7": _choose_imap(cfg)
        elif c == "8":
            brs = [
                ("chromium", "Playwright Chromium  ← pw-browsers/ が必要"),
                ("chrome",   "System Google Chrome ← インストール済みのChromeを使用"),
                ("msedge",   "System Edge          ← インストール済みのEdgeを使用"),
                ("firefox",  "Playwright Firefox   ← pw-browsers/ が必要"),
                ("webkit",   "Playwright WebKit    ← macOSのみ"),
            ]
            print()
            for i, (key, label) in enumerate(brs):
                sel = "●" if cfg.get("browser") == key else " "
                print(f"  [{i+1}] [{sel}] {label}")
            x = input("\nSelect: ").strip()
            if x.isdigit() and 1 <= int(x) <= len(brs):
                cfg["browser"] = brs[int(x)-1][0]
                _ok(f"Browser: {cfg['browser']}")
        elif c == "9":
            _choose_captcha(cfg)
        elif c.upper() == "D":
            cfg["debug_mode"] = not cfg.get("debug_mode", False)
            state = "ON" if cfg["debug_mode"] else "OFF"
            _ok(f"Debug mode: {state}  (debug/ フォルダに HTML + HAR を保存)")
        elif c.upper() == "T":
            _choose_preferred_times(cfg)
        elif c.upper() == "A":
            # ★ Fix 47: Ajax mode トグル
            cfg["ajax_mode"] = not cfg.get("ajax_mode", False)
            if cfg["ajax_mode"]:
                _ok("Ajax mode: ON  ← 全枠同時POST")
                jv = _inp_float("POST直前ジッター上限(s) (0=無効, 推奨0.3)", float(cfg.get("ajax_jitter_max", 0.3)))
                cfg["ajax_jitter_max"] = max(0.0, jv)
            else:
                _ok("Ajax mode: OFF ← 枠を順番に試行")
        elif c.upper() == "P":
            # ★ Fix 55: Presolve mode トグル
            cfg["presolve_enabled"] = not cfg.get("presolve_enabled", True)
            if cfg["presolve_enabled"]:
                _ok(
                    "Presolve: ON  ← スロット開放前に事前取得（高速・デフォルト）\n"
                    "  → 20枚のTurnstileトークンをFCFS開始前に用意します。"
                )
                _pw = input("  解禁前も連続充填(prewarm)しますか？ 解禁直前だけ推奨 [y/N]: ").strip().lower()
                cfg["presolve_prewarm"] = (_pw == "y")
                if cfg["presolve_prewarm"]:
                    _ok("  prewarm: ON  ← スロット無しでもプールを満たし続けます（capsolver消費↑）")
                else:
                    _ok("  prewarm: OFF ← スロット検知後に充填（従来どおり）")
            else:
                _ok(
                    "Presolve: OFF ← JITモード（スロット検知後・POST直前に取得）\n"
                    "  → cf_clearance と同タイミングで Turnstile を解くため IP 整合性↑。\n"
                    "  → [B] Browser presolve=ON と組み合わせると最も通過率が高くなります。\n"
                    "  → ただしsolve時間分だけ book() が遅れます（capsolver: ~3s / browser: ~10s）。"
                )
        elif c.upper() == "B":
            # ★ Fix 54: Browser presolve トグル
            cfg["browser_presolve"] = not cfg.get("browser_presolve", False)
            if cfg["browser_presolve"]:
                _ok(
                    "Browser presolve: ON\n"
                    "  → Playwright ブラウザでプリソルブします。\n"
                    "  → cf_clearance と同一IP/コンテキストで Turnstile を解くため\n"
                    "    Cloudflare の整合性チェック通過率が向上します（推奨）。\n"
                    "  → capsolverなしでも動作します。\n"
                    "  → ブラウザ起動分だけ開始が約10〜30s 遅くなります。"
                )
            else:
                _ok(
                    "Browser presolve: OFF\n"
                    "  → capsolver API でプリソルブします（高速・並列対応）。\n"
                    "  → capsolverのIP と cf_clearance のIP が異なる場合、\n"
                    "    Cloudflare が不整合を検知して book() を拒否する可能性があります。"
                )
        elif c.upper() == "M":
            # ★ ブラウザモード トグル
            cfg["browser_mode"] = not cfg.get("browser_mode", False)
            if cfg["browser_mode"]:
                if not _HAS_PW:
                    _err("Playwright が未インストールです。pip install playwright && playwright install chromium")
                    cfg["browser_mode"] = False
                else:
                    _ok(
                        "Browser mode: ON\n"
                        "  → Playwright ブラウザで予約フォームを直接操作します。\n"
                        "  → GPS 許可ポップアップを自動承認します（geolocation 設定）。\n"
                        "  → Cloudflare / Turnstile をブラウザネイティブで自動通過します。\n"
                        "  → 予約 POST をブラウザ内 JS fetch で実行します（capsolver 不要）。\n"
                        "  → ブラウザ起動のため通常より時間がかかります（~10〜20s/スロット）。\n"
                        "  ※ browser_presolve / presolve_enabled の設定は無視されます。"
                    )
            else:
                _ok(
                    "Browser mode: OFF\n"
                    "  → HTTP リクエスト（curl_cffi）で予約します（通常モード）。"
                )
        elif c.upper() == "C":
            print(f"\n  {Fore.CYAN}解禁検知TTL（先着レースの最重要レバー）{Style.RESET_ALL}")
            print(f"  小さいほど解禁を早く検知できますが、API呼び出しが増えます。")
            print(f"  先着5秒レースでは 0.2〜0.3 推奨。まず0.3で試し、Failed to get dates が増えないか確認。")
            cfg["dates_cache_ttl"] = max(0.1, _inp_float("  dates TTL (s)", float(cfg.get("dates_cache_ttl", 0.9))))
            cfg["slots_cache_ttl"] = max(0.1, _inp_float("  slots TTL (s)", float(cfg.get("slots_cache_ttl", 0.45))))
            _mon = _inp_float("  monitor_delay (s) [検知ループ間隔・TTL以下推奨]", float(cfg.get("monitor_delay", 0.5)))
            cfg["monitor_delay"] = max(0.1, _mon)
            _ok(f"検知: dates={cfg['dates_cache_ttl']:g}s / slots={cfg['slots_cache_ttl']:g}s / monitor={cfg['monitor_delay']:g}s")
        elif c.upper() == "W":
            urls = load_webhooks()
            if not urls:
                print(f"\n  {Fore.YELLOW}webhooks.txt が空か存在しません。{Style.RESET_ALL}")
                print(f"  {WEBHOOKS_F} を作成し、1行1 URL で記述してください。")
                print(f"  例:  https://discord.com/api/webhooks/xxxx/yyyy")
            else:
                print(f"\n  登録済み Webhook URL ({len(urls)} 件):")
                for u in urls:
                    _kind = "Discord" if _is_discord_webhook(u) else "generic"
                    print(f"    [{_kind}] {u}")
                _t = input(f"\n  テスト通知を送信しますか？ [y/N]: ").strip().lower()
                if _t == "y":
                    import requests as _rq
                    _wh_log = logging.getLogger("webhook")
                    _test = {"event": "booking_success", "name": "テスト太郎",
                             "email": "test@example.com", "slot_time": f"{datetime.date.today()},13:30:00",
                             "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    for u in urls:
                        try:
                            s = _rq.Session(); s.trust_env = False
                            payload = _to_discord_payload(_test) if _is_discord_webhook(u) else _test
                            r = s.post(u, json=payload, timeout=12)
                            if r.ok:
                                print(f"    {Fore.GREEN}✓ 送信OK {r.status_code} → {u[:55]}{Style.RESET_ALL}")
                            else:
                                print(f"    {Fore.RED}✗ 失敗 {r.status_code}: {r.text[:120]} → {u[:55]}{Style.RESET_ALL}")
                                if r.status_code == 404:
                                    print(f"      {Fore.YELLOW}404=このWebhook URLは削除/失効。Discordでチャンネル設定→連携サービス→新規Webhookで作り直してwebhooks.txtを更新。{Style.RESET_ALL}")
                        except Exception as _e:
                            print(f"    {Fore.RED}✗ 送信エラー: {_e} → {u[:55]}{Style.RESET_ALL}")
        elif c == "0":
            break

    save_cfg(cfg)
    _ok("Settings saved.")
    return cfg

def _choose_fire_mode(cfg: dict) -> None:
    """[F] Fire mode（C++同期一斉発火）のトグル＋主要パラメータ設定。"""
    cfg["fire_mode"] = not cfg.get("fire_mode", False)
    if not cfg["fire_mode"]:
        _ok("Fire mode: OFF ← 従来 book()（ワーカーごとに個別発火）")
        return
    # ON: fire_core.exe の有無を確認
    path   = _fire_core_path(cfg)
    exists = os.path.exists(path)
    dll_ok = os.path.exists(os.path.join(os.path.dirname(path) or str(BASE), "libcurl.dll")) \
             or os.path.exists(str(BASE / "libcurl.dll"))
    if exists:
        _ok(f"Fire mode: ON  → fire_core: {path}")
        if not dll_ok:
            print(f"  {Fore.RED}⚠ libcurl.dll が見つかりません。fire_core.exe と同じ場所に置いてください。{Style.RESET_ALL}")
    else:
        _err("Fire mode: ON にしましたが fire_core.exe が見つかりません。")
        print(f"  {Fore.YELLOW}探索先: {path}{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}fire_core.exe と libcurl.dll を mellojoy の隣（exe運用なら mellojoy.exe の隣）に置いてください。{Style.RESET_ALL}")
    # 検知バリア型 / 従来窓 の選択
    _bm = input(f"  検知バリア型を使う？（推奨・検知時刻を基準に全員同時発火） [Y/n]: ").strip().lower()
    cfg["fire_barrier_mode"] = (_bm != "n")
    if cfg["fire_barrier_mode"]:
        cfg["fire_barrier_lead_ms"] = _inp_float(
            "  検知→発火までの猶予 barrier_lead (ms) [推奨150〜300]",
            float(cfg.get("fire_barrier_lead_ms", 250)))
    else:
        cfg["fire_align_ms"] = _inp_float(
            "  集約窓 fire_align (ms) [最初の登録からこの時間内を集約]",
            float(cfg.get("fire_align_ms", 1000)))
    print(f"  {Fore.CYAN}※ Fire mode は presolve でトークンを事前に揃えておくと最も効果が出ます。{Style.RESET_ALL}")
    _ok("Fire mode 設定を保存しました。")


def _choose_captcha_fallback(cfg: dict, providers: list, labels: dict):
    """★ Fix 48: フォールバック Captcha API チェーン設定サブメニュー。"""
    while True:
        fallbacks = cfg.setdefault("captcha_fallback", [])
        print(f"\n  ┌─ フォールバック Captcha 設定 ({len(fallbacks)}件) ──────────────────────┐")
        if fallbacks:
            for _i, _fb in enumerate(fallbacks):
                _fk   = _fb.get("key", "")
                _fkd  = ("*" * min(8, len(_fk)) + _fk[-4:]) if len(_fk) > 4 else ("set ✓" if _fk else "未設定")
                print(f"  {Fore.WHITE}  {_i+1}. {_fb.get('provider','?'):12s}  key: {_fkd}{Style.RESET_ALL}")
        else:
            print(f"  {Fore.YELLOW}  (フォールバックなし — 主APIが失敗するとブラウザに移行){Style.RESET_ALL}")
        print(f"""  {Fore.CYAN}[A]{Style.RESET_ALL} 追加  {Fore.CYAN}[R]{Style.RESET_ALL} 削除  {Fore.CYAN}[0]{Style.RESET_ALL} 戻る
  └─────────────────────────────────────────────────────────┘""")
        fc = input(f"{Fore.WHITE}fallback> {Style.RESET_ALL}").strip().upper()
        if fc == "A":
            print()
            for _i2, _pv in enumerate(providers):
                print(f"  [{_i2+1}] {labels[_pv]}")
            px = input("\nプロバイダ選択: ").strip()
            if px.isdigit() and 1 <= int(px) <= len(providers):
                _sel_prov = providers[int(px)-1]
                _new_key  = _inp(f"{_sel_prov} API キー", "").strip()
                if _new_key:
                    fallbacks.append({"provider": _sel_prov, "key": _new_key})
                    _ok(f"フォールバック追加: {_sel_prov}  ({len(fallbacks)}件目)")
                else:
                    print(f"  {Fore.YELLOW}APIキーが空のためスキップしました{Style.RESET_ALL}")
        elif fc == "R":
            if not fallbacks:
                print(f"  {Fore.YELLOW}フォールバックが設定されていません{Style.RESET_ALL}")
            else:
                _ri = input("削除する番号: ").strip()
                if _ri.isdigit() and 1 <= int(_ri) <= len(fallbacks):
                    _removed = fallbacks.pop(int(_ri)-1)
                    _ok(f"削除: {_removed.get('provider','?')}")
        elif fc == "0":
            break


def _choose_captcha(cfg: dict):
    """Captcha API 設定サブメニュー。"""
    _PROVIDERS = list(_CAPTCHA_PROVIDERS.keys())
    _PROVIDER_LABELS = {
        "yescaptcha":  "YesCaptcha   (yescaptcha.com)   ← 日本語サポート/推奨",
        "2captcha":    "2captcha     (2captcha.com)      ← 老舗・安定",
        "capsolver":   "CapSolver    (capsolver.com)     ← Turnstile 高速",
        "anticaptcha": "AntiCaptcha  (anti-captcha.com)  ← 実績あり",
    }
    while True:
        enabled   = cfg.get("captcha_enabled", True)
        provider  = cfg.get("captcha_provider", "yescaptcha")
        key       = cfg.get("captcha_key") or cfg.get("yescaptcha_key", "")
        key_disp  = ("*" * min(8, len(key)) + key[-4:]) if len(key) > 4 else ("set ✓" if key else "未設定")
        fallbacks = cfg.get("captcha_fallback", [])
        _fb_disp  = f"{len(fallbacks)}件設定済み" if fallbacks else "なし（主APIのみ）"
        print(f"""
  ┌─ Captcha API 設定 ──────────────────────────────┐
  {Fore.CYAN}[E]{Style.RESET_ALL} 有効/無効              : {Fore.WHITE}{'✓ ON' if enabled else '✗ OFF (ブラウザのみ)'}{Style.RESET_ALL}
  {Fore.CYAN}[P]{Style.RESET_ALL} プロバイダ             : {Fore.WHITE}{provider}{Style.RESET_ALL}
  {Fore.CYAN}[K]{Style.RESET_ALL} API キー               : {Fore.WHITE}{key_disp}{Style.RESET_ALL}
  {Fore.CYAN}[F]{Style.RESET_ALL} フォールバック         : {Fore.WHITE}{_fb_disp}{Style.RESET_ALL}  ← Fix 48
  {Fore.CYAN}[0]{Style.RESET_ALL} 戻る
  └────────────────────────────────────────────────┘""")
        c = input(f"{Fore.WHITE}captcha> {Style.RESET_ALL}").strip().upper()
        if c == "E":
            cfg["captcha_enabled"] = not enabled
            _ok(f"Captcha API: {'ON' if cfg['captcha_enabled'] else 'OFF'}")
        elif c == "P":
            print()
            for i, pv in enumerate(_PROVIDERS):
                sel = "●" if pv == provider else " "
                print(f"  [{i+1}] [{sel}] {_PROVIDER_LABELS[pv]}")
            x = input("\nSelect: ").strip()
            if x.isdigit() and 1 <= int(x) <= len(_PROVIDERS):
                cfg["captcha_provider"] = _PROVIDERS[int(x)-1]
                _ok(f"Provider: {cfg['captcha_provider']}")
        elif c == "K":
            k = _inp("API キー", key)
            cfg["captcha_key"] = k
            # 旧フィールドも更新（後方互換）
            if cfg.get("captcha_provider", "yescaptcha") == "yescaptcha":
                cfg["yescaptcha_key"] = k
        elif c == "F":
            # ★ Fix 48: フォールバックチェーン設定サブメニュー
            _choose_captcha_fallback(cfg, _PROVIDERS, _PROVIDER_LABELS)
        elif c == "0":
            break


def _choose_devices(cfg: dict):
    keys    = list(DEVICES.keys())
    current = cfg.get("devices", [])
    print()
    for i, k in enumerate(keys):
        sel = "✓" if k in current else " "
        print(f"  [{i+1:2d}] [{sel}] {DEVICE_LABELS.get(k,k)}")
    raw = input("\nEnter numbers (comma-separated, e.g. 1,3,5): ").strip()
    if raw:
        chosen = [keys[int(x.strip())-1]
                  for x in raw.split(",")
                  if x.strip().isdigit() and 1<=int(x.strip())<=len(keys)]
        if chosen:
            cfg["devices"] = chosen
            _ok(f"Devices: {chosen}")


_SLOT_TIMES = [
    "10:30", "11:30", "12:30", "13:30", "14:30",
    "15:30", "16:30", "17:30", "18:30", "19:30",
]

def _choose_preferred_times(cfg: dict):
    """優先時間枠をチェックボックス番号選択で設定する。"""
    current = cfg.get("preferred_times", [])
    print()
    print(f"  {Fore.CYAN}優先時間枠を選択してください（複数可）{Style.RESET_ALL}")
    print(f"  ※ accounts.txt の4フィールド目でアカウント別に上書き可能")
    print()
    for i, t in enumerate(_SLOT_TIMES):
        sel = "✓" if t in current else " "
        print(f"  [{i+1:2d}] [{sel}] {t}")
    print()
    print(f"  {Fore.YELLOW}0{Style.RESET_ALL} または {Fore.YELLOW}-{Style.RESET_ALL} → クリア（全枠対象）")
    raw = input(f"\n番号を入力 (カンマ区切り、例: 1,3,5) [{', '.join(current) or 'なし'}]: ").strip()
    if raw in ("0", "-"):
        cfg["preferred_times"] = []
        _ok("優先時間枠をクリアしました（全枠対象）")
    elif raw:
        chosen = []
        for x in raw.split(","):
            x = x.strip()
            if x.isdigit() and 1 <= int(x) <= len(_SLOT_TIMES):
                t = _SLOT_TIMES[int(x) - 1]
                if t not in chosen:
                    chosen.append(t)
        if chosen:
            cfg["preferred_times"] = chosen
            _ok(f"優先時間枠: {', '.join(chosen)}")
    # Enter のみ → 現状維持

def _choose_geo(cfg: dict):
    keys = list(GEO_LABELS.keys())
    print()
    for i, k in enumerate(keys):
        sel = "●" if cfg.get("geo") == k else " "
        print(f"  [{i+1:2d}] [{sel}] {GEO_LABELS[k]}")
    x = input("\nSelect: ").strip()
    if x.isdigit() and 1<=int(x)<=len(keys):
        cfg["geo"] = keys[int(x)-1]
        _ok(f"Region: {GEO_LABELS[cfg['geo']]}")

def _choose_imap(cfg: dict):
    print(f"\n  Host  : {cfg.get('imap_host','imap.gmail.com')}")
    print(f"  Port  : {cfg.get('imap_port',993)}")
    print(f"  User  : {cfg.get('imap_user','')}")
    print(f"  Pass  : {'(set)' if cfg.get('imap_pass') else '(not set)'}\n")
    cfg["imap_host"] = _inp("IMAP host", cfg.get("imap_host","imap.gmail.com"))
    cfg["imap_port"] = _inp_int("IMAP port", int(cfg.get("imap_port",993)))
    cfg["imap_user"] = _inp("IMAP user (email)", cfg.get("imap_user",""))
    pw = input(f"{Fore.WHITE}IMAP password (Enter to skip): {Style.RESET_ALL}").strip()
    if pw: cfg["imap_pass"] = pw
    _ok("IMAP updated.")

# ── Playwright browser auto-install ───────────────────────────────────────────
def _ensure_playwright_browsers():
    """Check for Chromium; download it on first run if absent.

    In a PyInstaller onedir EXE the playwright Node.js driver is bundled inside
    _internal/playwright/driver/.  We invoke it directly so the download works
    even without a separate Python installation on the target machine.
    In source-run mode we simply call  python -m playwright install chromium.
    """
    import subprocess

    pw_dir = BASE / "pw-browsers"

    # Quick presence check — look for chrome / chromium binary anywhere under pw-browsers/
    def _browser_present() -> bool:
        if not pw_dir.exists():
            return False
        for pattern in ("chrome", "chromium", "chrome.exe", "chromium.exe"):
            if any(pw_dir.rglob(pattern)):
                return True
        return False

    if _browser_present():
        return   # already installed

    _warn("Chromium not found in pw-browsers/ — first-run download starting…")
    _info(f"  Download target: {pw_dir}")
    _info("  This may take a few minutes depending on your connection.")
    print()

    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(pw_dir)}

    if getattr(sys, "frozen", False):
        # --- PyInstaller EXE: invoke the bundled Playwright Node.js driver ---
        success = False
        try:
            # playwright._impl._driver ships the Node binary + driver JS
            from playwright._impl._driver import compute_driver_executable
            # ★ Fix 71a: 新しい Playwright は (node実行ファイル, cli.js) のタプルを返す。
            #   旧実装は Path(tuple) で TypeError → 自動ダウンロードが必ず失敗していた。
            _drv = compute_driver_executable()
            if isinstance(_drv, (tuple, list)):
                _drv_cmd = [str(p) for p in _drv]
            else:
                _drv_cmd = [str(_drv)]
            if pathlib.Path(_drv_cmd[0]).exists():
                result = subprocess.run(
                    _drv_cmd + ["install", "chromium"],
                    env=env,
                )
                success = result.returncode == 0
            else:
                _warn(f"Playwright driver not found: {_drv_cmd[0]}")
        except Exception as exc:
            _warn(f"Auto-download via bundled driver failed: {exc}")

        if not success:
            _err("Could not auto-download Chromium.")
            _info("To fix: set PLAYWRIGHT_BROWSERS_PATH to pw-browsers/ and run:")
            _info("  playwright install chromium")
            _info("Then place the pw-browsers/ folder next to mellojoy.exe.")
            _pause("Press Enter to continue (Turnstile browser-fallback will be unavailable)…")
            return
    else:
        # --- Source run: standard   python -m playwright install chromium ---
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            env=env,
        )
        if result.returncode != 0:
            _warn("playwright install chromium returned non-zero; browser may not work.")
            return

    _ok("Chromium is ready.")


# ── Main ──────────────────────────────────────────────────────────────────────
def _result_fetch_one(entry: tuple, cfg: dict, proxies: list[str],
                      idx: int, total: int) -> dict:
    """
    1アカウントにログインして /api/ticket/mine を取得し、辞書を返す。
    戻り値キー: email, pw, name, slot_date, slot_time, status, error
    """
    email, pw, name_hint = entry
    logger = logging.getLogger(f"result-{idx}")
    proxy  = pick_proxy(proxies)

    result = {
        "email":     email,
        "pw":        pw,
        "name":      name_hint,
        "slot_date": "",
        "slot_time": "",
        "status":    "",
        "error":     "",
    }

    api = MelloJoyAPI(cfg, logger, proxy=proxy)
    if not api.login(email, pw):
        result["error"] = "login_failed"
        _warn(f"[{idx}/{total}] {email}  → login failed")
        return result

    # ★ Fix 71c: 取得失敗(None)は最大2回まで再試行し、それでも失敗なら fetch_failed とする
    tickets = api.get_my_tickets()
    for _retry in range(2):
        if tickets is not None:
            break
        time.sleep(2)
        tickets = api.get_my_tickets()
    if tickets is None:
        result["error"] = "fetch_failed"
        _warn(f"[{idx}/{total}] {email}  → 予約一覧の取得に失敗（予約の有無は不明）")
        return result
    if not tickets:
        result["error"] = "no_ticket"
        _info(f"[{idx}/{total}] {email}  → no ticket")
        return result

    # 複数チケットがある場合は最初の pending/won を優先
    ticket = next((t for t in tickets if t.get("status") in ("pending", "won")),
                  tickets[0])

    result["name"]      = ticket.get("name", "") or name_hint
    result["slot_date"] = ticket.get("slotDate", "")
    result["slot_time"] = ticket.get("startTime", "")
    result["status"]    = ticket.get("status", "")
    _ok(f"[{idx}/{total}] {email}  → {result['slot_date']} {result['slot_time']}  ({result['status']})")
    return result


def menu_result_mode():
    """
    リザルトモード
    accountresult.txt の各アカウントにログインして実際の予約時間を取得し、
    result/ フォルダに日付ファイル名の CSV を保存する。

    accountresult.txt の行フォーマット (# コメント・空行は無視):
        ID:PW
        ID:PW:氏名
        ID:PW:氏名:時間   ← accounts.txt / success.txt からのコピーも可
    """
    import csv

    _box("Result Mode")

    if not ACCOUNT_RESULT_F.exists():
        _err(f"accountresult.txt not found: {ACCOUNT_RESULT_F}")
        _info("Create accountresult.txt with  ID:PW[:name]  lines.")
        _pause()
        return

    raw = ACCOUNT_RESULT_F.read_text(encoding="utf-8").splitlines()
    entries: list[tuple[str, str, str]] = []
    for line in raw:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # ★ Fix 71d: success.txt 形式（ID,PW,氏名,予約日,時間,タイムスタンプ のカンマ区切り）にも対応。
        #   先頭フィールド（メール）に ":" がなく "," がある行はカンマ区切りとして読む。
        _head = line.split(",", 1)[0]
        if "," in line and ":" not in _head:
            parts = [p.strip() for p in line.split(",")]
        else:
            parts = line.split(":", 4)
        if len(parts) < 2:
            continue
        email     = parts[0].strip()
        pw        = parts[1].strip()
        name_hint = parts[2].strip() if len(parts) > 2 else ""
        entries.append((email, pw, name_hint))

    if not entries:
        _err("accountresult.txt has no valid entries.")
        _pause()
        return

    cfg     = load_cfg()
    proxies = load_proxies()
    total   = len(entries)
    parallel = min(int(cfg.get("parallel", 3)), total)

    print()
    _info(f"Fetching ticket info for {total} account(s)  parallel={parallel}")
    if input(f"{Fore.WHITE}Proceed? [y/N]: {Style.RESET_ALL}").strip().lower() != "y":
        return

    results: list[dict] = [{}] * total

    def _worker(i: int) -> tuple[int, dict]:
        # ★ Fix 71g: 1アカウントの予期しない例外で全体（CSV保存）が止まらないよう個別に捕捉
        try:
            return i, _result_fetch_one(entries[i], cfg, proxies, i + 1, total)
        except Exception as _e:
            _err(f"[{i + 1}/{total}] {entries[i][0]}  → 予期しないエラー: {_e}")
            return i, {
                "email": entries[i][0], "pw": entries[i][1], "name": entries[i][2],
                "slot_date": "", "slot_time": "", "status": "",
                "error": f"exception: {_e}",
            }

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futs = {pool.submit(_worker, i): i for i in range(total)}
        for f in as_completed(futs):
            try:
                i, res = f.result()
            except Exception as _e:   # 念のための二重防御
                i = futs[f]
                res = {"email": entries[i][0], "pw": entries[i][1], "name": entries[i][2],
                       "slot_date": "", "slot_time": "", "status": "",
                       "error": f"exception: {_e}"}
            results[i] = res

    # ── CSV 出力 ──────────────────────────────────────────────────────────────
    RESULT_DIR.mkdir(exist_ok=True)
    ts_now   = datetime.datetime.now()
    filename = ts_now.strftime("%Y%m%d_%H%M%S") + ".csv"
    out_path = RESULT_DIR / filename

    ok_count  = sum(1 for r in results if r.get("slot_time"))
    err_count = total - ok_count

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "PW", "氏名", "予約日", "予約時間", "ステータス", "備考"])
        for r in results:
            writer.writerow([
                r.get("email",     ""),
                r.get("pw",        ""),
                r.get("name",      ""),
                r.get("slot_date", ""),
                r.get("slot_time", ""),
                r.get("status",    ""),
                r.get("error",     ""),
            ])

    print()
    _ok(f"CSV saved → {out_path}")
    _info(f"  予約確認: {ok_count}/{total}  エラー: {err_count}")
    _pause()


def menu_test_turnstile(cfg: dict):
    """Turnstile受け渡しデバッグテスト — スロット開放不要"""
    _box("Turnstile デバッグテスト")
    base_url = cfg.get("base_url", "https://mellojoylucky.cn")
    sitekey  = cfg.get("cf_sitekey", "0x4AAAAAAFF0MPh5SYT7ywAo")

    print(f"""
  {Fore.CYAN}[1]{Style.RESET_ALL} Turnstileトークン取得テスト  (capsolver / browser fallback のみ)
  {Fore.CYAN}[2]{Style.RESET_ALL} フルパイプラインテスト        (ログイン → Turnstile → booking POST 送信)
  {Fore.CYAN}[3]{Style.RESET_ALL} Fire Engine テスト (A案)      (常駐・自己検知・一斉発火／複数台を同時発火)
  {Fore.CYAN}[0]{Style.RESET_ALL} 戻る
""")
    c = input(f"{Fore.WHITE}> {Style.RESET_ALL}").strip()

    if c == "1":
        # ── Turnstileトークン取得のみ ────────────────────────────────────
        _box("Turnstileトークン取得テスト")
        print(f"  websiteURL : {Fore.CYAN}{base_url}/register{Style.RESET_ALL}")
        print(f"  sitekey    : {Fore.CYAN}{sitekey}{Style.RESET_ALL}\n")

        logger = logging.getLogger("test_ts")
        prxs   = load_proxies()
        proxy  = prxs[0] if prxs else None
        token  = None

        if _captcha_ready(cfg):
            yc = _captcha_client_chain(cfg, logger)   # ★ Fix 48
            _prov_disp = cfg.get("captcha_provider", "yescaptcha")
            _fb_cnt    = len(cfg.get("captcha_fallback", []))
            _fb_info   = f" + {_fb_cnt}フォールバック" if _fb_cnt else ""
            print(f"  {Fore.YELLOW}⏳ {_prov_disp}{_fb_info} に送信中...{Style.RESET_ALL}", flush=True)
            t0    = time.time()
            token = _solve_turnstile_chain(yc, f"{base_url}/register", sitekey, proxy=proxy)  # ★ Fix 48
            elapsed = time.time() - t0
            if token:
                print(f"  {Fore.GREEN}✓ API: {elapsed:.1f}s で取得{Style.RESET_ALL}")
                print(f"  Token: {Fore.WHITE}{token[:35]}...{token[-10:]}{Style.RESET_ALL}  "
                      f"(len={len(token)})")
            else:
                print(f"  {Fore.RED}✗ API: 取得失敗 ({elapsed:.1f}s){Style.RESET_ALL}")
        else:
            print(f"  {Fore.YELLOW}! capsolver: 未設定 → スキップ{Style.RESET_ALL}")

        if not token and _HAS_PW:
            print(f"\n  {Fore.YELLOW}⏳ ブラウザ fallback で取得中...{Style.RESET_ALL}", flush=True)
            cf  = CloudflareBypass(cfg, logging.getLogger("cfbypass_test"))
            t0  = time.time()
            token = cf.solve_turnstile_browser(f"{base_url}/register", sitekey, proxy=proxy)
            elapsed = time.time() - t0
            if token:
                print(f"  {Fore.GREEN}✓ browser fallback: {elapsed:.1f}s で取得{Style.RESET_ALL}")
                print(f"  Token: {Fore.WHITE}{token[:35]}...{token[-10:]}{Style.RESET_ALL}  "
                      f"(len={len(token)})")
            else:
                print(f"  {Fore.RED}✗ browser fallback: 取得失敗{Style.RESET_ALL}")

        print()
        if token:
            print(f"  {Fore.GREEN}✅ Turnstileトークン取得成功！{Style.RESET_ALL}")
        else:
            print(f"  {Fore.RED}✗ トークン取得できませんでした{Style.RESET_ALL}")
        _pause()

    elif c == "2":
        # ── フルパイプライン: ログイン → Turnstile → book() ──────────────
        _box("フルパイプラインテスト")
        accs = load_accounts()
        if not accs:
            print(f"  {Fore.RED}accounts.txt が空です。アカウントを追加してください。{Style.RESET_ALL}")
            _pause()
            return

        _parts   = accs[0].split(":", 3)
        email    = _parts[0]
        password = _parts[1] if len(_parts) > 1 else ""
        prxs  = load_proxies()
        proxy = prxs[0] if prxs else None
        print(f"  使用アカウント : {Fore.CYAN}{email}{Style.RESET_ALL}")
        print(f"  プロキシ       : {Fore.CYAN}{proxy or 'なし'}{Style.RESET_ALL}\n")

        logger = logging.getLogger("test_pipe")
        api    = MelloJoyAPI(cfg, logger, proxy=proxy)

        # ── Step 1: ログイン ─────────────────────────────────────────────
        print(f"  {Fore.YELLOW}[1/3] ログイン中...{Style.RESET_ALL}", flush=True)
        ok = api.login(email, password)
        if not ok or not api.token:
            print(f"  {Fore.RED}✗ ログイン失敗{Style.RESET_ALL}")
            _pause()
            return
        print(f"  {Fore.GREEN}✓ ログイン成功{Style.RESET_ALL}  auth_token: "
              f"{Fore.WHITE}{api.token[:25]}...{Style.RESET_ALL}")

        # ── Step 2 & 3: browser_mode=ON → ブラウザで一括実行 ───────────
        _test_bm_on = cfg.get("browser_mode", False) and _HAS_PW
        if _test_bm_on:
            print(f"\n  {Fore.CYAN}[BrowserMode] スロット取得 → ブラウザ予約 (Step 2+3){Style.RESET_ALL}", flush=True)

            # スロット取得
            test_slot = None
            try:
                dates = api.get_bookable_dates()
                if dates:
                    for d in dates[:3]:
                        slots = api.get_slots(d)
                        if slots:
                            test_slot = slots[0]
                            break
            except Exception as e:
                logger.warning(f"get_slots: {e}")

            if not test_slot:
                print(f"  {Fore.YELLOW}! スロット情報取得失敗 — bookable-dates が空の可能性{Style.RESET_ALL}")
                _pause()
                return

            print(f"  使用スロット : date={test_slot.get('slotDate')}  "
                  f"idx={test_slot.get('slotIndex')}  "
                  f"remaining={test_slot.get('remaining','?')}  "
                  f"isFull={test_slot.get('isFull','?')}")
            print(f"\n  {Fore.CYAN}⏳ ブラウザ起動 → ログイン → Turnstile → POST...{Style.RESET_ALL}", flush=True)

            bm_lat, bm_lng = _resolve_geo(cfg.get("geo", "kanto_random"))
            t0 = time.time()
            bm_result = api._cf.book_via_browser(
                email, password, test_slot,
                bm_lat, bm_lng,
                proxy=proxy
            )
            elapsed = time.time() - t0

            print(f"\n  {Fore.YELLOW}結果: {elapsed:.1f}s{Style.RESET_ALL}")
            if bm_result == "won":
                print(f"  {Fore.GREEN}🎉 当選！ (テスト中に当選){Style.RESET_ALL}")
            elif bm_result == "not_won":
                print(f"  {Fore.GREEN}✅ result=not_won — ブラウザモード正常動作確認！{Style.RESET_ALL}")
                print(f"  {Fore.CYAN}  スロット満席のため落選（正常）。ブラウザがCloudflareを通過できています。{Style.RESET_ALL}")
            elif bm_result == "cooldown":
                print(f"  {Fore.YELLOW}⚠ COOLDOWN — アカウント制限中{Style.RESET_ALL}")
                print(f"  {Fore.CYAN}  リクエスト自体は通過しています。しばらく待ってから再試行してください。{Style.RESET_ALL}")
            elif bm_result is None:
                print(f"  {Fore.YELLOW}409 RATE_LIMIT — スロット制限{Style.RESET_ALL}")
                print(f"  {Fore.GREEN}  → ブラウザモードのリクエストは通過しています（正常）{Style.RESET_ALL}")
                print(f"  {Fore.CYAN}  （スロット開放時にのみ result=not_won/won が返ります）{Style.RESET_ALL}")
            else:  # net_error
                print(f"  {Fore.RED}✗ エラー — ブラウザ起動またはネットワークの問題{Style.RESET_ALL}")
                print(f"  {Fore.RED}  ログファイルで詳細を確認してください。{Style.RESET_ALL}")

            _pause()
            return

        # ── Step 2: Turnstile solve ──────────────────────────────────────
        print(f"\n  {Fore.YELLOW}[2/3] Turnstile solve中...{Style.RESET_ALL}", flush=True)
        cf_token  = None
        yc_proxy  = proxy
        # ★ Fix 54: browser_presolve=True → ブラウザを最初に試みる
        #   テストでも本番FCFSと同じsolveパスを使い、結果を正確に確認できるようにする。
        _test_browser_first = cfg.get("browser_presolve", False) and _HAS_PW

        if _test_browser_first:
            print(f"  {Fore.CYAN}⏳ ブラウザ solve (browser_presolve=ON)...{Style.RESET_ALL}", flush=True)
            t0       = time.time()
            # ★ Fix 57: ログイン後のページでTurnstileを取得するためemail/passwordを渡す
            cf_token = api._cf.solve_turnstile_browser(
                f"{base_url}/", sitekey, proxy=proxy,
                email=email, password=password)
            elapsed  = time.time() - t0
            if cf_token:
                print(f"  {Fore.GREEN}✓ browser: {elapsed:.1f}s{Style.RESET_ALL}  "
                      f"token: {Fore.WHITE}{cf_token[:35]}...{Style.RESET_ALL}  (len={len(cf_token)})")
            else:
                print(f"  {Fore.YELLOW}⚠ ブラウザ solve 失敗 ({elapsed:.1f}s) → API fallback...{Style.RESET_ALL}")

        if not cf_token and _captcha_ready(cfg):
            yc = _captcha_client_chain(cfg, logger)   # ★ Fix 48
            t0 = time.time()
            # ★ Fix 53a: テストパイプの Turnstile URL を予約ページ "/" に修正。
            #   旧: /register（会員登録ページ）→ 新: /（予約トップ）。
            #   FCFS 本番が "/" で解いているのと一致させる（Fix 46 の適用漏れ）。
            cf_token = _solve_turnstile_chain(yc, f"{base_url}/", sitekey, proxy=yc_proxy)
            elapsed  = time.time() - t0
            if cf_token:
                _prov_disp2 = cfg.get("captcha_provider", "yescaptcha")
                print(f"  {Fore.GREEN}✓ {_prov_disp2}: {elapsed:.1f}s{Style.RESET_ALL}  "
                      f"token: {Fore.WHITE}{cf_token[:35]}...{Style.RESET_ALL}  (len={len(cf_token)})")
            else:
                print(f"  {Fore.RED}✗ API solve 失敗 ({elapsed:.1f}s){Style.RESET_ALL}")

        if not cf_token and _HAS_PW and not _test_browser_first:
            # browser_presolve=OFF 時のみブラウザfallbackを試みる
            print(f"  {Fore.YELLOW}⏳ ブラウザ fallback...{Style.RESET_ALL}", flush=True)
            t0       = time.time()
            # ★ Fix 53a: ブラウザ fallback も "/" に統一
            cf_token = api._cf.solve_turnstile_browser(f"{base_url}/", sitekey, proxy=proxy,
                                                        email=email, password=password)  # ★ Fix 70c: email/password なしだとログイン後ページ(/)にアクセスできない
            elapsed  = time.time() - t0
            if cf_token:
                print(f"  {Fore.GREEN}✓ browser fallback: {elapsed:.1f}s{Style.RESET_ALL}  "
                      f"token: {Fore.WHITE}{cf_token[:35]}...{Style.RESET_ALL}  (len={len(cf_token)})")
            else:
                print(f"  {Fore.RED}✗ browser fallback 失敗{Style.RESET_ALL}")

        if not cf_token:
            print(f"  {Fore.RED}✗ Turnstileトークン取得失敗 → booking送信をスキップ{Style.RESET_ALL}")
            _pause()
            return

        # ── Step 3: スロット取得 → book() ───────────────────────────────
        print(f"\n  {Fore.YELLOW}[3/3] booking POST 送信中...{Style.RESET_ALL}", flush=True)

        # 予約可能日を取得して最初のスロットを使う（満席でもIDは存在する）
        test_slot = None
        try:
            dates = api.get_bookable_dates()
            if dates:
                for d in dates[:3]:
                    slots = api.get_slots(d)
                    if slots:
                        test_slot = slots[0]
                        break
        except Exception as e:
            logger.warning(f"get_slots: {e}")

        if not test_slot:
            print(f"  {Fore.YELLOW}! スロット情報取得失敗 — bookable-dates が空の可能性{Style.RESET_ALL}")
            _pause()
            return

        print(f"  使用スロット : date={test_slot.get('slotDate')}  "
              f"idx={test_slot.get('slotIndex')}  "
              f"remaining={test_slot.get('remaining','?')}  "
              f"isFull={test_slot.get('isFull','?')}")

        # ── raw POST で実際のレスポンスを表示 ───────────────────────────
        lat, lng = _resolve_geo(api.cfg.get("geo", "kanto_random"))
        fp = f"{uuid.uuid4().hex[:8]}-{random.randint(1000,9999)}"
        body = {
            "store_id":          int(api.cfg.get("store_id", 2)),   # ★ 必須
            "slotDate":          test_slot.get("slotDate"),
            "slotIndex":         test_slot.get("slotIndex"),
            "deviceFingerprint": fp,
            "gpsLat":            round(lat, 4),
            "gpsLng":            round(lng, 4),
            "turnstileToken":    cf_token,
        }
        print(f"\n  {Fore.YELLOW}POST {base_url}/api/registrations{Style.RESET_ALL}")
        try:
            r = api._sess.post(f"{base_url}/api/registrations",
                               headers=api._hdr(), json=body,
                               proxies=api._prx(), timeout=15)
            print(f"  HTTP {Fore.WHITE}{r.status_code}{Style.RESET_ALL}")
            try:
                resp_body = r.json()
                body_str  = json.dumps(resp_body, ensure_ascii=False, indent=2)
            except Exception:
                resp_body = {}
                body_str  = r.text[:500]
            print(f"  Body: {Fore.CYAN}{body_str}{Style.RESET_ALL}")

            # ── 結果判定 ────────────────────────────────────────────────
            print()
            status   = r.status_code
            code     = resp_body.get("code", "") if isinstance(resp_body, dict) else ""
            result_v = resp_body.get("result", "") if isinstance(resp_body, dict) else ""

            if result_v == "won":
                print(f"  {Fore.GREEN}🎉 当選！ (テスト実行中に当選しました){Style.RESET_ALL}")
            elif result_v == "not_won":
                print(f"  {Fore.GREEN}✅ result=not_won — Turnstileはサーバーに受け入れられました！{Style.RESET_ALL}")
                print(f"  {Fore.CYAN}  スロット満席のため落選（正常）。次の抽選で当選が期待できます。{Style.RESET_ALL}")
            elif status == 409:
                ts_rejected = any(k in str(resp_body).lower()
                                  for k in ("turnstile", "captcha", "invalid_token"))
                if ts_rejected:
                    print(f"  {Fore.RED}✗ 409 — Turnstile関連の拒否の可能性{Style.RESET_ALL}")
                    print(f"  {Fore.RED}  → code={code!r}  上のBodyを確認してください{Style.RESET_ALL}")
                elif code in ("SLOT_ENDED", "SLOT_FULL", "REGISTRATION_CLOSED"):
                    print(f"  {Fore.GREEN}✅ 409 {code} — Turnstile通過確認！{Style.RESET_ALL}")
                    print(f"  {Fore.CYAN}  サーバーはリクエストを正常受理 → スロット期間終了のため抽選対象外{Style.RESET_ALL}")
                    print(f"  {Fore.CYAN}  次回スロット開放時に当選が期待できます。{Style.RESET_ALL}")
                else:
                    print(f"  {Fore.YELLOW}409 {code or 'RATE_LIMIT'} — スロット制限{Style.RESET_ALL}")
                    print(f"  {Fore.GREEN}  → Turnstileトークン自体は受け入れられた可能性が高いです{Style.RESET_ALL}")
                    print(f"  {Fore.CYAN}  （スロット開放時にのみ result=not_won/won が返ります）{Style.RESET_ALL}")
            elif status in (400, 401, 403):
                print(f"  {Fore.RED}✗ HTTP {status} — リクエスト拒否{Style.RESET_ALL}")
                print(f"  {Fore.RED}  → Turnstile無効またはauth_token期限切れの可能性{Style.RESET_ALL}")
            else:
                print(f"  {Fore.YELLOW}HTTP {status} — 予期しないレスポンス{Style.RESET_ALL}")
        except Exception as e:
            print(f"  {Fore.RED}✗ 送信エラー: {e}{Style.RESET_ALL}")

        _pause()

    elif c == "3":
        # ── Fire Engine テスト（A案: 常駐・自己検知・一斉発火）────────────────
        _box("Fire Engine 発火テスト (A案)")
        epath = _fire_engine_path(cfg)
        if not os.path.exists(epath):
            print(f"  {Fore.RED}✗ fire_engine が見つかりません: {epath}{Style.RESET_ALL}")
            print(f"  {Fore.YELLOW}  fire_engine.exe と libcurl.dll を mellojoy の隣に置いてください。{Style.RESET_ALL}")
            _pause(); return
        print(f"  fire_engine : {Fore.CYAN}{epath}{Style.RESET_ALL}")
        accs = load_accounts()
        if not accs:
            print(f"  {Fore.RED}accounts.txt が空です。{Style.RESET_ALL}"); _pause(); return
        _nmax = _inp_int("  使用アカウント数", min(len(accs), int(cfg.get("parallel", 5))))
        _nmax = max(1, min(_nmax, len(accs)))
        prxs = load_proxies()
        _prompt_store_id(cfg)   # ★ 本番と同様に store_id を選択（枠取得・予約に必須）
        logger = logging.getLogger("test_engine")

        def _prep(i):
            pair = accs[i]; parts = pair.split(":", 3)
            email = parts[0]; pw = parts[1] if len(parts) > 1 else ""
            proxy = prxs[i % len(prxs)] if prxs else None
            api = MelloJoyAPI(cfg, logger, proxy=proxy)
            if not api.login(email, pw) or not api.token:
                return None
            tok = None
            if _captcha_ready(cfg):
                tok = _solve_turnstile_chain(_captcha_client_chain(cfg, logger),
                                             f"{base_url}/", sitekey, proxy=proxy)
            if not tok and _HAS_PW:
                tok = api._cf.solve_turnstile_browser(f"{base_url}/", sitekey, proxy=proxy,
                                                      email=email, password=pw)
            try: api._inject_cf_clearance()
            except Exception: pass
            if not tok: return None
            return (api, tok, email)

        print(f"\n  {Fore.YELLOW}[1/3] {_nmax}台をログイン＋トークン取得中...{Style.RESET_ALL}", flush=True)
        ready = []
        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=min(_nmax, 10)) as ex:
            for r in ex.map(_prep, range(_nmax)):
                if r: ready.append(r)
        print(f"  {Fore.GREEN}✓ {len(ready)}/{_nmax} 台 準備完了{Style.RESET_ALL}")
        if not ready:
            print(f"  {Fore.RED}✗ 準備できた台がありません{Style.RESET_ALL}"); _pause(); return

        # 強制発火テスト（満員でもパイプライン確認）か、検知発火かを選択
        _force = input(f"\n  満員でも強制発火テストしますか？（枠があれば満員でも1発撃ち 409で確認）[Y/n]: ").strip().lower()
        force_mode = (_force != "n")

        print(f"  {Fore.YELLOW}[2/3] 日付・スロット取得...{Style.RESET_ALL}", flush=True)
        _api0 = ready[0][0]
        dates = []
        try: dates = _api0.get_bookable_dates() or []
        except Exception as e: logger.warning(f"dates: {e}")

        packets = [_build_engine_packet(api, tok, f"W{idx:02d}") for idx, (api, tok, _em) in enumerate(ready)]
        try: _dprx = _api0._prx().get("https") or _api0._prx().get("http") or ""
        except Exception: _dprx = getattr(_api0, "proxy", "") or ""
        _d_common = {"cookie": _fire_extract_cookie(_api0._sess), "headers": dict(_api0._hdr()),
                     "impersonate": getattr(_api0, "_imp_target", "") or "", "proxy": _dprx}

        if force_mode:
            # 枠がある日付を探し、満員でも index を集めて強制発火対象にする
            force_slots = []
            for d in (dates[::-1] or []):
                try: _sl = _api0.get_slots(d) or []
                except Exception: _sl = []
                if _sl:
                    force_slots = [{"date": s.get("slotDate", d), "index": s.get("slotIndex")} for s in _sl[:10]]
                    break
            if not force_slots:
                print(f"  {Fore.YELLOW}! 枠が全く取得できません（日付も空）。解禁期間中に実行してください。{Style.RESET_ALL}")
                _pause(); return
            print(f"  強制発火対象 : {len(force_slots)}枠（満員含む / 409が返ればパイプラインOK）")
            arm = {"spin_lead_ms": 2.0, "slots": force_slots, "packets": packets}  # detect無し→即発火
            print(f"  {Fore.YELLOW}[3/3] エンジン起動 → 即・一斉発火（強制）...{Style.RESET_ALL}", flush=True)
        else:
            if not dates:
                print(f"  {Fore.YELLOW}! bookable-dates が空（解禁前）。検知URLに日付が無いので中止。{Style.RESET_ALL}")
                print(f"  {Fore.CYAN}  ※ 検知発火は解禁の数十秒前に実行してください。{Style.RESET_ALL}")
                _pause(); return
            target_date = dates[-1]
            detect_url = f"{_api0.BASE}/api/registrations/bookable-dates?store_id={int(cfg.get('store_id', 2))}"
            try: _tsl = _api0.get_slots(target_date) or []
            except Exception: _tsl = []
            _tslots = [{"date": s.get("slotDate", target_date), "index": s.get("slotIndex")}
                       for s in _tsl if s.get("slotIndex") is not None] or \
                      [{"date": target_date, "index": i} for i in range(10)]
            print(f"  検知URL : {Fore.CYAN}{detect_url}{Style.RESET_ALL}  （{target_date} の解禁を検知 / 枠{len(_tslots)}）")
            arm = {
                "detect": dict(_d_common, url=detect_url, match_date=target_date,
                               interval_ms=int(float(cfg.get("slots_cache_ttl", 0.3)) * 1000) or 80,
                               timeout_ms=4000, max_wait_ms=120000, open_min=1),
                "slots": _tslots, "fire_attempts": int(cfg.get("fire_attempts", 3)),
                "spin_lead_ms": 2.0, "warm_interval_ms": 20000, "packets": packets,
            }
            print(f"  {Fore.YELLOW}[3/3] エンジン起動 → 検知待ち → 一斉発火...{Style.RESET_ALL}", flush=True)
        try:
            r = _fire_sp.run([epath], input=json.dumps(arm, ensure_ascii=False),
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=130)
            out = _parse_engine_out(r)
        except Exception as e:
            print(f"  {Fore.RED}✗ エンジン実行エラー: {e}{Style.RESET_ALL}")
            print(f"  stderr: {(r.stderr if 'r' in dir() else '')[:300]}")
            _pause(); return

        results = out.get("results", [])
        fs = [x.get("fired_at_unix", 0) for x in results if x.get("fired_at_unix")]
        spread = (max(fs) - min(fs)) * 1000 if len(fs) >= 2 else 0.0
        won = sum(1 for x in results if '"result":"won"' in (x.get("body") or "") or '"result": "won"' in (x.get("body") or ""))
        print()
        print(f"  detected={out.get('detected')}  open_slots={out.get('open_slots')}  count={out.get('count')}")
        print(f"  {Fore.GREEN}★ 発火スプレッド: {spread:.2f}ms{Style.RESET_ALL}（全{len(fs)}台が同時発火）")
        _ok_n = sum(1 for x in results if x.get("ok"))
        _imp_n = sum(1 for x in results if x.get("impersonated"))
        print(f"  ok={_ok_n}/{len(results)}  impersonated={_imp_n}/{len(results)}")
        for x in results[:5]:
            print(f"    {x.get('tag')} status={x.get('http_status')} latency={x.get('latency_ms'):.0f}ms "
                  f"body={(x.get('body') or '')[:70]}")
        print(f"\n  {Fore.GREEN}✅ A案エンジン: 自己検知→常駐温接続で {len(fs)}台を {spread:.1f}ms 以内に一斉発火{Style.RESET_ALL}")
        _pause()


def _measure_server_clock_offset(url: str, samples_sec: float = 4.0,
                                 interval: float = 0.05) -> tuple[Optional[float], int, float]:
    """サーバー応答の Date ヘッダ(秒単位)が繰り上がる瞬間を捉えて、PC時計とのずれを推定する。
    返り値: (offset_sec, 捉えた繰り上がり回数, 最小往復秒)。
      offset_sec = サーバー時刻 - PC時刻（正ならPCが遅れている）。測れなければ None。
    プロキシを通さず直接取得（往復の揺れを小さくするため）。Cloudflare配下でも Date は付く。"""
    import email.utils as _eu
    import urllib.request as _ur
    opener = _ur.build_opener(_ur.ProxyHandler({}))
    pts = []   # (mid_local, server_sec, rtt)
    t_end = time.time() + samples_sec
    while time.time() < t_end:
        t0 = time.time()
        try:
            req = _ur.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
            try:
                resp = opener.open(req, timeout=2.0)
                hdr = resp.headers.get("Date", "")
                resp.close()
            except _ur.HTTPError as he:      # 403/404 等でも Date は取れる
                hdr = he.headers.get("Date", "") if he.headers else ""
            t1 = time.time()
            if hdr:
                srv = _eu.parsedate_to_datetime(hdr).timestamp()
                pts.append(((t0 + t1) / 2.0, srv, t1 - t0))
        except Exception:
            pass
        time.sleep(interval)
    if len(pts) < 3:
        return None, 0, 0.0
    ests = []
    for (m0, s0, r0), (m1, s1, r1) in zip(pts, pts[1:]):
        if s1 == s0 + 1 and max(r0, r1) < 0.8:
            boundary_local = (m0 + m1) / 2.0        # この間でサーバー秒が繰り上がった
            ests.append(s1 - boundary_local)
    if not ests:
        return None, 0, min(p[2] for p in pts)
    ests.sort()
    return ests[len(ests) // 2], len(ests), min(p[2] for p in pts)


class _FireRetryManager:
    """解禁直後の追い撃ち（[6] Fire Engine 本番用）。
    - 本隊(エンジン)に入れなかった台（締切までにソルブが間に合わなかった台）:
        解禁+grace 以降、トークンが解け次第すぐ撃つ。
    - 本隊の台: エンジンの結果(@@RES)が落選だったら、新しいトークンで撃ち直す。
    - どちらも「当選するか、解禁+window 秒になるまで」ソルブ→POST を繰り返す（トークンは使い捨て）。
    - 撃つ枠:
        解禁検知（bookable-dates に対象日が出た）後に取った生の枠データがあれば、残り人数の多い枠から優先。
        検知できていなければ残り人数は使わず、希望時間(pref_time)の枠、なければ未試行の枠からランダム。
      ※ 13時直後の SLOT_CLOSED はトークンが古い可能性があるため、枠を除外しない（新トークンで撃ち直すだけ）。
    """
    def __init__(self, cfg, logger, sessions, slots, idx2time, solve_one, solve_workers,
                 fire_at, window_end, engine_si, pref_of, target_date, pending=None,
                 chase_pool=None, relogin_one=None):
        self.cfg = cfg; self.log = logger; self.sessions = sessions
        self.slots = [s for s in slots if s.get("index") is not None]
        self.idx2time = idx2time; self.solve_one = solve_one
        self.fire_at = fire_at; self.window_end = window_end
        self.target_date = target_date; self.pref_of = pref_of
        n = len(sessions)
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.queue = {si: [] for si in range(n)}
        self.solving = {si: False for si in range(n)}
        self.won = {si: False for si in range(n)}
        # 本隊に入っていない台は最初から追い撃ち対象。本隊の台は @@RES で落選が分かってから。
        self.eligible = {si: (si not in engine_si) for si in range(n)}
        self.in_engine = set(engine_si)
        self.tried = {si: set() for si in range(n)}
        self.sess_lock = {si: threading.Lock() for si in range(n)}   # 1セッションを複数スレッドで同時に使わない
        # ── 方式A: 追い撃ち専用の事前ログインセッション（1台ごとのプール）────────────
        #   撃つたびにプールからフレッシュな専用セッションを取り出して使う。
        #   枯渇したら relogin_one(si) で都度ログインして撃ち直す（リトライ回数に応じて）。
        #   どちらも不可なら本隊セッションへフォールバック。
        self.chase_pool = {si: list((chase_pool or {}).get(si, [])) for si in range(n)}
        self.chase_used = {si: 0 for si in range(n)}
        self.relogin_one = relogin_one
        self._chase_lock = threading.Lock()
        self.live = None          # 解禁検知後の生データ {index: remaining}
        self.live_t = 0.0
        self.detected_at = 0.0
        self.stats = {"post": 0, "won": 0, "codes": {}}
        self.wins = []          # (si, email, slot_time, attempt)
        self.stop = threading.Event()
        self.solve_stop = threading.Event()
        self._ex = ThreadPoolExecutor(max_workers=max(1, solve_workers))
        self._inflight = 0; self._max_inflight = max(1, solve_workers)
        self._threads = []
        self._mine = set()
        self._pending_stop = None
        if pending:
            self._pending_stop = pending.pop("__stop__", None)
            for fut, si in pending.items():
                self.solving[si] = True
                fut.add_done_callback(self._on_solved)

    # ── トークン供給 ─────────────────────────────────────────────────────────
    def _on_solved(self, fut):
        try:
            si, tok = fut.result()
        except Exception:
            return
        with self.cv:
            self.solving[si] = False
            if fut in getattr(self, "_mine", set()):
                self._inflight = max(0, self._inflight - 1)
            if tok and not self.won[si] and time.time() < self.window_end:
                self.queue[si].append(tok)
            self.cv.notify_all()

    def _scheduler(self):
        while not self.stop.is_set() and time.time() < self.window_end:
            subs = []
            with self.cv:
                for si in range(len(self.sessions)):
                    if self._inflight >= self._max_inflight:
                        break
                    if self.won[si] or self.solving[si] or self.queue[si]:
                        continue
                    self.solving[si] = True; self._inflight += 1
                    subs.append(si)
            for si in subs:
                try:
                    f = self._ex.submit(self.solve_one, (si, self.sessions[si]), self.solve_stop)
                    self._mine.add(f)
                    f.add_done_callback(self._on_solved)
                except Exception:
                    with self.cv:
                        self.solving[si] = False; self._inflight = max(0, self._inflight - 1)
            time.sleep(0.05)

    # ── 本隊結果の受け取り（エンジン stderr の @@RES 行）──────────────────────
    def engine_result(self, si, won):
        with self.cv:
            if won:
                self.won[si] = True
            else:
                self.eligible[si] = True
            self.cv.notify_all()

    # ── 撃つ ────────────────────────────────────────────────────────────────
    def _pick_slot(self, si):
        with self.lock:
            cand = list(self.slots)
            live = self.live
            if live:
                # 検知済み: 残り人数が一番多い枠（同数はランダム）
                rich = [s for s in cand if live.get(str(s["index"]), 0) > 0]
                if rich:
                    random.shuffle(rich)
                    rich.sort(key=lambda s: -live.get(str(s["index"]), 0))
                    return rich[0]
            pref = self.pref_of(self.sessions[si][1])
            if pref:
                for s in cand:
                    if str(s.get("time", "")).startswith(pref):
                        return s
            fresh = [s for s in cand if s["index"] not in self.tried[si]]
            return random.choice(fresh or cand) if cand else None

    def _next_fire_api(self, si):
        """方式A: 追い撃ちは毎回フレッシュな専用セッションで撃つ。
        返り値: (api, is_main)。is_main=True のときだけ本隊 _sess のロックが必要。"""
        with self._chase_lock:
            pool = self.chase_pool.get(si)
            if pool:
                api = pool.pop(0)
                self.chase_used[si] += 1
                return api, False
        # プール枯渇 → リトライ回数に応じて都度ログインして撃ち直す
        if self.relogin_one is not None:
            try:
                api = self.relogin_one(si)
            except Exception:
                api = None
            if api is not None and getattr(api, "token", None):
                with self._chase_lock:
                    self.chase_used[si] += 1
                return api, False
        # それも不可 → 従来どおり本隊セッションで撃つ
        return self.sessions[si][0], True

    def _post(self, si, tok, slot):
        api, is_main = self._next_fire_api(si)
        lat, lng = _resolve_geo(self.cfg.get("geo", "kanto_random"))
        body = {"store_id": int(self.cfg.get("store_id", 2)),   # ★ 必須（欠落=PARAM_INVALID）
                "slotDate": slot.get("date", self.target_date), "slotIndex": slot["index"],
                "deviceFingerprint": f"{uuid.uuid4().hex[:8]}-{random.randint(1000, 9999)}",
                "gpsLat": round(lat, 4), "gpsLng": round(lng, 4), "turnstileToken": tok}
        _to = max(1.0, float(self.cfg.get("fire_post_timeout_ms", 8000)) / 1000.0)
        t0 = time.time()
        try:
            if is_main:
                # 本隊セッションは _poller と共有するためロック必須
                with self.sess_lock[si]:
                    r = api._sess.post(f"{api.BASE}/api/registrations", headers=api._hdr(), json=body,
                                       proxies=api._prx(), timeout=_to)
            else:
                # 追い撃ち専用セッションは単独スレッド使用 → ロック不要（本隊発火と干渉しない）
                r = api._sess.post(f"{api.BASE}/api/registrations", headers=api._hdr(), json=body,
                                   proxies=api._prx(), timeout=_to)
            txt = r.text or ""
            return r.status_code, txt, (time.time() - t0) * 1000
        except Exception as e:
            return 0, f"ERR {str(e)[:80]}", (time.time() - t0) * 1000

    def _firer(self, si):
        email = self.sessions[si][2]
        att = 0
        while not self.stop.is_set():
            with self.cv:
                while not self.stop.is_set():
                    now = time.time()
                    if self.won[si] or now >= self.window_end:
                        return
                    if now >= self.fire_at and self.eligible[si] and self.queue[si]:
                        tok = self.queue[si].pop(0)
                        break
                    _until = self.fire_at if now < self.fire_at else self.window_end
                    self.cv.wait(timeout=min(0.05, max(0.0005, _until - now)))
                else:
                    return
            slot = self._pick_slot(si)
            if slot is None:
                return
            att += 1
            st, txt, lat = self._post(si, tok, slot)
            code = ""
            m = re.search(r'"code"\s*:\s*"([A-Z_]+)"', txt)
            if m: code = m.group(1)
            # ★ 抽選成功の判定: pending_draw(エントリー成立)/won/already_won/当日登録済 を成功とみなす。
            try:
                _stt, _ = _interpret_booking(json.loads(txt))
                won = (_stt == "won")
            except Exception:
                _t = txt.replace(" ", "")
                won = ('"result":"won"' in _t or '"result":"pending_draw"' in _t
                       or '"result":"already_won"' in _t or '"sing_up":true' in _t)
            with self.cv:
                self.stats["post"] += 1
                key = code or (f"{st}:not_won" if '"not_won"' in txt else str(st))
                self.stats["codes"][key] = self.stats["codes"].get(key, 0) + 1
                self.tried[si].add(slot["index"])
                if won:
                    self.won[si] = True; self.stats["won"] += 1
            tag = "追撃" if si not in self.in_engine else "再撃"
            stime = self.idx2time.get(str(slot["index"]), "") or str(slot.get("time", ""))
            if won:
                self.wins.append((si, email, stime, att))
                try: mark_success(self.sessions[si][1], f"{self.target_date},{stime}" if stime else self.target_date)
                except Exception: pass
                # ★ 全体Webhook（1当選ごと・個人情報なし）
                try:
                    _fire_win_webhook(self.target_date, stime, len(self.sessions),
                                      self.cfg.get("fire_align_lead_sec", 25),
                                      self.cfg.get("fire_attempts", 1),
                                      store_id=self.cfg.get("store_id", 2))
                except Exception: pass
                print(f"  {Fore.GREEN}🎉 当選({tag}{att}回目) {email} 枠{stime} status={st} lat={lat:.0f}ms{Style.RESET_ALL}", flush=True)
                self.log.info(f"WIN {tag}{att} {email} status={st} 枠={stime or '?'} lat={lat:.0f}ms body={txt[:120]}")
                return
            self.log.info(f"LOSE {tag}{att} {email} status={st} 枠={stime or '?'} lat={lat:.0f}ms body={txt[:120]}")

    # ── 解禁検知 → 生の残り人数の取得（リトライの枠選び用）────────────────────
    def _poller(self):
        """解禁(fire_at)以降、bookable-dates に対象日が出たら「検知」。検知後だけ slots を取り、
        残り人数を更新する。検知前の slots（解禁前の値）は使わない。"""
        while not self.stop.is_set() and time.time() < self.fire_at:
            time.sleep(0.01)
        k = 0
        n = len(self.sessions)
        while not self.stop.is_set() and time.time() < self.window_end:
            si = k % n; k += 1
            api = self.sessions[si][0]
            if not self.sess_lock[si].acquire(timeout=0.05):
                continue
            try:
                if not self.detected_at:
                    try:
                        ds = api.get_bookable_dates() or []
                    except Exception:
                        ds = []
                    if self.target_date in ds:
                        self.detected_at = time.time()
                        self.log.info(f"追い撃ち: 解禁検知 {datetime.datetime.fromtimestamp(self.detected_at).strftime('%H:%M:%S.%f')[:-3]}"
                                      f"（以降は残り人数の多い枠からリトライ）")
                if self.detected_at:
                    try:
                        sl = api.get_slots(self.target_date) or []
                    except Exception:
                        sl = []
                    live = {}
                    for x in sl:
                        if x.get("slotIndex") is None:
                            continue
                        ok = x.get("isOpen", True) and not x.get("isPast", False) and not x.get("isFull", False)
                        live[str(x.get("slotIndex"))] = int(x.get("remaining", 0) or 0) if ok else 0
                    if live:
                        with self.lock:
                            first = self.live is None
                            self.live = live; self.live_t = time.time()
                        if first:
                            self.log.info(f"追い撃ち: 残り人数 {live}")
            finally:
                self.sess_lock[si].release()
            time.sleep(0.25)

    def start(self):
        t = threading.Thread(target=self._scheduler, daemon=True); t.start(); self._threads.append(t)
        t = threading.Thread(target=self._poller, daemon=True); t.start(); self._threads.append(t)
        for si in range(len(self.sessions)):
            t = threading.Thread(target=self._firer, args=(si,), daemon=True); t.start(); self._threads.append(t)

    def finish(self):
        """窓の終了まで待ってから全停止。"""
        while time.time() < self.window_end and not all(self.won.values()):
            time.sleep(0.05)
        self.stop.set(); self.solve_stop.set()
        if self._pending_stop is not None:
            self._pending_stop.set()
        with self.cv:
            self.cv.notify_all()
        for t in self._threads:
            t.join(timeout=12)
        try:
            self._ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._ex.shutdown(wait=False)

    def abort(self):
        self.window_end = time.time()
        self.finish()


def menu_fire_engine_prod(cfg: dict):
    """A案エンジン本番モード: 全台ログイン→解禁検知→一斉発火→当選保存。
    トークンTTL(50s)内で撃つため、未検知なら新トークンで再armを繰り返す。"""
    _box("Fire Engine 本番 (A案)")
    _prompt_store_id(cfg)
    base_url = cfg.get("base_url", "https://mellojoylucky.cn")
    sitekey  = cfg.get("cf_sitekey", "0x4AAAAAAFF0MPh5SYT7ywAo")
    epath = _fire_engine_path(cfg)
    if not os.path.exists(epath):
        _err(f"fire_engine が見つかりません: {epath}")
        print(f"  {Fore.YELLOW}fire_engine.exe と libcurl.dll を mellojoy の隣に置いてください。{Style.RESET_ALL}")
        _pause(); return
    if not _captcha_ready(cfg):
        _err("capsolver キー未設定（エンジンはトークン必須）。Settings[9]で設定してください。")
        _pause(); return
    accs = load_accounts()
    if not accs:
        _err("accounts.txt が空です。"); _pause(); return
    prxs = load_proxies()

    # --- 台数別 推奨設定ガイド（起動時に表示。実測: attempts と プロキシ往復が勝率の主因）---
    _have = len(accs)
    def _reco(n: int) -> tuple[int, int, str]:
        """台数 → (推奨リード秒, 推奨solve_workers, 推奨ログイン開始)"""
        lead = 30 if n <= 35 else 35          # pool3=波3前提（≈24s）+ 余裕
        workers = max(n, 24)                   # 台数以上（詰めたいなら最大2倍）
        login = "12:52" if n <= 15 else ("12:50" if n <= 35 else "12:45")
        return lead, workers, login
    _rl, _rw, _rlog = _reco(_have)
    print(f"\n  {Fore.CYAN}── 台数別 推奨設定ガイド ──（attempts3/pool3 固定・grace300）{Style.RESET_ALL}")
    print(f"    ~10台 : lead30 / workers≥台数 / login 12:52")
    print(f"    ~30台 : lead30 / workers≥台数(〜2倍で波短縮) / login 12:50")
    print(f"    ~50台 : lead35 / workers≥台数(〜2倍で波短縮) / login 12:45")
    print(f"    {Fore.WHITE}※ token_pool は必ず attempts と同数（使い捨て）。{Style.RESET_ALL}")
    print(f"    {Fore.WHITE}※ 勝率の最大要因は低レイテンシの近接プロキシ（往復が速い台が勝つ）。{Style.RESET_ALL}")
    print(f"    {Fore.GREEN}手持ち {_have}台 → 推奨: attempts3 pool3 / lead{_rl} / "
          f"workers≥{_rw} / login {_rlog}{Style.RESET_ALL}")

    _n = _inp_int("使用アカウント数", min(len(accs), int(cfg.get("parallel", len(accs)))))
    _n = max(1, min(_n, len(accs)))
    logger = logging.getLogger("engine_prod")
    from concurrent.futures import ThreadPoolExecutor as _TPE

    # ══ 設定はすべて先に入力（ログイン前）。タイマー待機中は無人で発火まで進むように ══

    # --- 解禁日ガイド（ログイン不要・「当日+6」ルールで推定）---
    _nowt0 = datetime.datetime.now()
    _base = datetime.date.today() + (datetime.timedelta(days=0) if _nowt0.hour < 13
                                     else datetime.timedelta(days=1))
    _suggest = (_base + datetime.timedelta(days=6)).strftime("%Y-%m-%d")
    _today_s = datetime.date.today().strftime("%Y-%m-%d (%a)")
    _rel_side = "本日13:00で開く見込み" if _nowt0.hour < 13 else "次回(翌日)13:00で開く見込み"
    print(f"\n  {Fore.CYAN}── 解禁日ガイド ──（13:00にその日の+6日が開くルール）{Style.RESET_ALL}")
    print(f"    今日              : {_today_s}")
    print(f"    {Fore.GREEN}次の解禁日({_rel_side}): {Style.BRIGHT}{_suggest}{Style.RESET_ALL}  ← これを検知対象に推奨")
    target_date = input(f"  検知対象の日付 YYYY-MM-DD [Enter={_suggest}]: ").strip() or _suggest
    if not target_date:
        _err("日付が特定できません。"); _pause(); return

    # --- リトライ / token_pool ---
    _attempts = _inp_int("リトライ回数 fire_attempts（満員なら別枠へ連射。推奨3〜5）",
                         int(cfg.get("fire_attempts", 3)))
    _attempts = max(1, _attempts)
    _tokpool = _inp_int(f"1台あたり事前ソルブ数 token_pool（使い捨てなら={_attempts}推奨 / 再利用可なら1）",
                        max(1, min(_attempts, int(cfg.get("token_pool", _attempts)))))
    _tokpool = max(1, min(_tokpool, _attempts))
    if _attempts > 1 and _tokpool < _attempts:
        print(f"  {Fore.YELLOW}! token_pool({_tokpool}) < リトライ({_attempts})。使い捨てなら token_pool={_attempts} に。{Style.RESET_ALL}")
    _open_min = max(1, int(cfg.get("fire_engine_open_min", 20)))

    # --- 解禁時刻（アライン）---
    _rel_in = input(f"  解禁時刻 HH:MM:SS [Enter=13:00:00 / 無効化は none]: ").strip() or "13:00:00"
    rel_dt = None
    if _rel_in.lower() != "none":
        try:
            _h, _m, _s = (_rel_in.split(":") + ["0", "0"])[:3]
            _nowd = datetime.datetime.now()
            rel_dt = _nowd.replace(hour=int(_h), minute=int(_m), second=int(_s), microsecond=0)
            if (rel_dt - _nowd).total_seconds() < -5:   # 過ぎていれば翌日の同時刻へ
                rel_dt += datetime.timedelta(days=1)
        except Exception:
            print(f"  {Fore.YELLOW}! 解禁時刻の解釈に失敗。アライン無効で監視します。{Style.RESET_ALL}")
            rel_dt = None

    # --- プレソルブ開始リード ---
    _ALIGN_LEAD = float(_inp_int("プレソルブ開始リード秒（解禁の何秒前から事前解決を始めるか / 推奨: ソルブ秒+5）",
                                 int(cfg.get("fire_align_lead_sec", 25))))
    _ALIGN_LEAD = max(10.0, min(_ALIGN_LEAD, 46.0))   # TTL50s を超えない範囲
    _ALIGN_OVER = 8.0      # 解禁後も8s張り付く（遅発対策）
    if rel_dt is not None:
        _ps = rel_dt - datetime.timedelta(seconds=_ALIGN_LEAD)
        print(f"  {Fore.CYAN}解禁アライン: {rel_dt.strftime('%m/%d %H:%M:%S')} 解禁。"
              f"プレソルブ開始 {Style.BRIGHT}{_ps.strftime('%H:%M:%S')}{Style.NORMAL}"
              f"（解禁{int(_ALIGN_LEAD)}s前）{Style.RESET_ALL}")

    # --- 発火grace（時刻優先の基準。解禁+この時間に発火。早すぎ409・推奨300〜600）---
    _grace = max(0, _inp_int("発火grace(ms)（解禁+この時間で発火 / 推奨300〜600・早すぎ409）",
                             int(cfg.get("fire_time_grace_ms", 400))))
    # --- ソルブ並列数（capsolver同時解決数。多台はソルブ短縮のため台数以上に）---
    _solvw = max(1, _inp_int("ソルブ並列数 solve_workers（capsolver同時上限 / 多台は台数以上に）",
                             int(cfg.get("solve_workers", 24))))

    # ★ 今回の入力を保存 → 次回[6]起動時の初期値として記憶（設定ページ不要・起動時だけで完結）
    cfg["fire_attempts"] = _attempts
    cfg["token_pool"] = _tokpool
    cfg["fire_align_lead_sec"] = int(_ALIGN_LEAD)
    cfg["fire_time_grace_ms"] = _grace
    cfg["solve_workers"] = _solvw
    try: save_cfg(cfg)
    except Exception: pass

    # --- ログイン開始タイマー（全設定の最後）。ここで待機 → 自動ログイン → 無人で発火まで ---
    _lg_in = input(f"  ログイン開始時刻 HH:MM:SS [Enter=今すぐ / 例 12:50]: ").strip()
    if _lg_in:
        try:
            _h, _m, _s = (_lg_in.split(":") + ["0", "0"])[:3]
            _now0 = datetime.datetime.now()
            _login_at = _now0.replace(hour=int(_h), minute=int(_m), second=int(_s), microsecond=0)
            if (_login_at - _now0).total_seconds() < -5:
                _login_at += datetime.timedelta(days=1)
            if (_login_at - datetime.datetime.now()).total_seconds() > 0:
                _wait = (_login_at - datetime.datetime.now()).total_seconds()
                print(f"  {Fore.CYAN}全設定完了。ログイン開始 {Style.BRIGHT}{_login_at.strftime('%m/%d %H:%M:%S')}{Style.NORMAL}"
                      f" まで待機（約{int(_wait)}s）。Ctrl+C で即ログイン。{Style.RESET_ALL}", flush=True)
                try:
                    while True:
                        _left = (_login_at - datetime.datetime.now()).total_seconds()
                        if _left <= 0: break
                        print(f"\r  待機中... ログインまで {int(_left)//3600:02d}:{int(_left)%3600//60:02d}:{int(_left)%60:02d}   ",
                              end="", flush=True)
                        time.sleep(min(2.0, _left))
                    print(f"\r  {Fore.GREEN}ログイン開始時刻になりました。{Style.RESET_ALL}            ")
                except KeyboardInterrupt:
                    print(f"\n  {Fore.YELLOW}待機を中止。すぐにログインします。{Style.RESET_ALL}")
            else:
                print(f"  {Fore.YELLOW}! 指定時刻は過ぎています。すぐにログインします。{Style.RESET_ALL}")
        except Exception:
            print(f"  {Fore.YELLOW}! 時刻の解釈に失敗。すぐにログインします。{Style.RESET_ALL}")

    # --- ログイン（全アカウント）---
    print(f"\n{Fore.YELLOW}[1] {_n}台をログイン中...{Style.RESET_ALL}", flush=True)
    def _login_pair(pair, proxy):
        """1アカウントぶんのログイン済み api を返す（失敗なら None）。
        本隊ログインと追い撃ち用の再ログインで共通に使う。"""
        parts = pair.split(":", 3)
        _em = parts[0]; _pw = parts[1] if len(parts) > 1 else ""
        api = MelloJoyAPI(cfg, logger, proxy=proxy)
        if api.login(_em, _pw) and api.token:
            try: api._inject_cf_clearance()
            except Exception: pass
            return api
        return None
    def _login(i):
        pair = accs[i]
        proxy = prxs[i % len(prxs)] if prxs else None
        api = _login_pair(pair, proxy)
        return (api, pair, pair.split(":", 1)[0]) if api is not None else None
    sessions = []
    with _TPE(max_workers=min(_n, 10)) as ex:
        for r in ex.map(_login, range(_n)):
            if r: sessions.append(r)
    print(f"  {Fore.GREEN}✓ {len(sessions)}/{_n} 台ログインOK{Style.RESET_ALL}")
    logger.info(f"ログイン {len(sessions)}/{_n} 台OK target_date={target_date}")
    if not sessions:
        _err("ログインできた台がありません。"); _pause(); return
    _api0 = sessions[0][0]
    detect_url = f"{_api0.BASE}/api/registrations/bookable-dates?store_id={int(cfg.get('store_id', 2))}"
    print(f"  検知URL : {Fore.CYAN}{detect_url}{Style.RESET_ALL}  （{target_date} が解禁入りした瞬間を検知）")

    # ── 再撃(追い撃ち)用セッションを事前ログイン（方式A / 13時前に確保）────────────
    #   初回ログイン(本隊発火用)とは別に、各アカウントを追加ログインして追い撃ち専用
    #   セッションを確保する。1台あたり min(リトライ回数, chase_sessions_max) 個。
    #   落選/未発火の台は、撃つたびにこのフレッシュな専用セッションを消費する。
    #   枯渇後はリトライ回数に応じて都度ログインして撃ち直す（下の _chase_relogin_one）。
    chase_pool: dict = {si: [] for si in range(len(sessions))}
    _chase_on = bool(cfg.get("chase_relogin", True))
    _chase_k = max(0, min(int(_attempts), int(cfg.get("chase_sessions_max", 3)))) if _chase_on else 0
    if _chase_on and _chase_k > 0:
        print(f"\n{Fore.YELLOW}[1b] 追い撃ち用セッションを事前ログイン中"
              f"（方式A / 1台あたり最大{_chase_k}セッション）...{Style.RESET_ALL}", flush=True)
        _cjobs = [(si, _kk) for si in range(len(sessions)) for _kk in range(_chase_k)]
        _cerr = 0
        def _mk_chase(job):
            si, _kk = job
            _cprx = getattr(sessions[si][0], "proxy", None)
            return (si, _login_pair(sessions[si][1], _cprx))
        with _TPE(max_workers=min(max(1, len(_cjobs)), 10)) as ex:
            for si, capi in ex.map(_mk_chase, _cjobs):
                if capi is not None:
                    chase_pool[si].append(capi)
                else:
                    _cerr += 1
        _ctot = sum(len(v) for v in chase_pool.values())
        print(f"  {Fore.GREEN}✓ 追い撃ち用セッション {_ctot}個を確保"
              f"（本隊{len(sessions)}台 / 失敗{_cerr}）{Style.RESET_ALL}")
        logger.info(f"追い撃ち事前ログイン: 確保{_ctot} 失敗{_cerr} (1台最大{_chase_k})")
        if _ctot == 0:
            print(f"  {Fore.YELLOW}! 追い撃ち用セッションを確保できず → 本隊セッション流用にフォールバック{Style.RESET_ALL}")
    def _chase_relogin_one(si):
        """プール枯渇時に、その台をフレッシュにログインし直して返す（方式Aの『都度再ログイン』）。"""
        if not _chase_on or not (0 <= si < len(sessions)):
            return None
        return _login_pair(sessions[si][1], getattr(sessions[si][0], "proxy", None))

    # --- ログイン後: 既予約可能の注意 + 事前枠(slotIndex/時間)取得 ---
    try: dates = _api0.get_bookable_dates() or []
    except Exception: dates = []
    if target_date in dates:
        print(f"  {Fore.YELLOW}! 注意: {target_date} は既に予約可能（新規解禁ではない＝満員の可能性）。{Style.RESET_ALL}")
    try: _pre = _api0.get_slots(target_date) or []
    except Exception: _pre = []
    pre_slots = [{"date": s.get("slotDate", target_date), "index": s.get("slotIndex"),
                  "time": s.get("startTime", ""), "remaining": int(s.get("remaining", 0) or 0)}
                 for s in _pre if s.get("slotIndex") is not None]
    if pre_slots:
        _tmap = ", ".join(f"{s['index']}:{s['time'] or '?'}" for s in pre_slots)
        print(f"  事前取得した枠: {Fore.CYAN}{len(pre_slots)}枠{Style.RESET_ALL}（index:時間 = {_tmap}）")
        logger.info(f"事前取得枠 {len(pre_slots)}枠 index:時間 = {_tmap}")
    else:
        print(f"  {Fore.YELLOW}! 枠を事前取得できず。解禁後に0席でも撃てるよう index=0..9 を仮置き。{Style.RESET_ALL}")
        pre_slots = [{"date": target_date, "index": i} for i in range(10)]
    # 当選枠の index → 予約時間 マップ（success.txt に予約時間を入れるため）
    _idx2time = {str(s.get("index")): (s.get("time") or "") for s in pre_slots}
    # [T]優先時間枠の適用状況を表示（個別時間指定が無い台はこの枠群へ分散）
    _pt = list(cfg.get("preferred_times", []))
    if _pt:
        _match = [s for s in pre_slots if any(str(s.get("time", "")).startswith(t) for t in _pt)]
        if _match:
            print(f"  {Fore.CYAN}[T]優先時間枠を適用: {', '.join(_pt)} → 該当{len(_match)}枠"
                  f"（index {[s['index'] for s in _match]}）に分散攻撃{Style.RESET_ALL}")
        else:
            print(f"  {Fore.YELLOW}! [T]優先時間枠 {_pt} に一致する枠がありません。全枠へ分散します。{Style.RESET_ALL}")
    _mons = int(float(cfg.get("slots_cache_ttl", 0.3)) * 1000) or 80

    # ── サーバー時刻とPC時計のずれ測定 ──────────────────────────────────────
    #   offset = サーバー - PC。発火時刻(PC時計)は「サーバー時刻の解禁+grace」に合わせて -offset 補正する。
    _clk_off = 0.0
    if cfg.get("fire_clock_sync", True):
        print(f"  時刻ずれ測定中（約4秒）...", end="", flush=True)
        _off, _nb, _rtt = _measure_server_clock_offset(f"{base_url}/")
        if _off is None:
            print(f"\r  {Fore.YELLOW}! 時刻ずれを測定できませんでした（Dateヘッダ取得不可）。PC時計のまま撃ちます。{Style.RESET_ALL}")
            logger.info("時刻ずれ測定: 失敗（PC時計のまま）")
        else:
            _om = _off * 1000.0
            _side = "PCが遅れ" if _om > 0 else "PCが進み"
            _lim = float(cfg.get("fire_clock_max_ms", 3000))
            if abs(_om) <= _lim:
                _clk_off = _off
                print(f"\r  時刻ずれ: サーバー−PC = {Style.BRIGHT}{_om:+.0f}ms{Style.NORMAL}（{_side} / 繰上り{_nb}回 / 最小往復{_rtt*1000:.0f}ms）→ 発火時刻を補正します")
            else:
                print(f"\r  {Fore.RED}! 時刻ずれ {_om:+.0f}ms が上限{_lim:.0f}msを超過。補正せずPC時計で撃ちます。w32tm /resync を推奨。{Style.RESET_ALL}")
            logger.info(f"時刻ずれ: サーバー-PC={_om:+.0f}ms {_side} 繰上り{_nb}回 最小往復{_rtt*1000:.0f}ms "
                        f"適用={'yes' if _clk_off else 'no'}")

    print(f"\n{Fore.CYAN}解禁を監視中... 検知で全台一斉発火します。Ctrl+C で停止。{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}※ トークンTTL50s内で撃つため、未検知なら約45秒ごとに再準備します。{Style.RESET_ALL}\n")

    # トークンを「波(wave)」単位で解く。1波 = 全台に1個ずつ(フラット並列)。
    #   ★ 最優先は「解禁前に、全台が最低1トークンを持ってarm済み」を保証すること。
    #   wave1(発火用1個)を先に確保し、解禁(deadline)までに次の波が丸ごと入る時間がある時だけ
    #   追加の保険トークンを解く。これで「ソルブ中に13時を跨いで出遅れる」を防ぐ。
    _cc = _captcha_client_chain(cfg, logger)
    _ncc = max(1, len(_cc))
    if _ncc > 1:
        print(f"  {Fore.CYAN}ソルバー{_ncc}系統でラウンドロビン分散（1キー集中を回避）{Style.RESET_ALL}")
    _ARM_MARGIN = 4.0   # エンジン起動＋接続温め＋ポーリング開始に要する余裕(秒)
    def _solve_one(job, _ev=None):
        si, s = job; api = s[0]
        # ★ タスクごとに先頭キーを回して負荷分散。失敗時は残りのキー/プロバイダへフォールバック。
        _start = si % _ncc
        _rot = _cc[_start:] + _cc[:_start]
        t = _solve_turnstile_chain(_rot, f"{base_url}/", sitekey, proxy=getattr(api, "proxy", None),
                                   stop_event=_ev)
        return (si, t)
    _solve_workers = max(1, int(cfg.get("solve_workers", 24)))   # capsolver同時解決数
    def _solve_wave(hard_deadline=None, keep_pending=None):
        # keep_pending: dict を渡すと、締切で未完了のsolveを止めずに {future: si} を詰めて返す
        #   （遅れ組の追い撃ち用。解けたトークンは追い撃ちマネージャが受け取る）。
        # hard_deadline(unix秒)を渡すと、その時刻で未完了のsolveは待たずに打ち切り、
        # 「準備完了ぶんだけ」を返す。遅い台にwave全体が引っ張られて13:00を跨ぐのを防ぐ。
        jobs = [(si, sessions[si]) for si in range(len(sessions))]
        got = {}
        _stop = threading.Event()   # 締切で未完了のソルブを止める（発火時に回線/CPUを食わせない）
        ex = _TPE(max_workers=min(len(jobs), _solve_workers))
        try:
            futs = [ex.submit(_solve_one, j, _stop) for j in jobs]
            _consumed = set()
            if hard_deadline is not None:
                _to = max(0.0, hard_deadline - time.time())
                try:
                    for fut in as_completed(futs, timeout=_to):
                        _consumed.add(fut)
                        try:
                            si, t = fut.result()
                            if t: got[si] = t
                        except Exception:
                            pass
                        if time.time() >= hard_deadline:
                            break
                except _CFTimeout:
                    # 締切到達：未解決の残りは待たず、準備完了ぶんだけで発火へ回す
                    pass
            else:
                for fut in as_completed(futs):
                    try:
                        si, t = fut.result()
                        if t: got[si] = t
                    except Exception:
                        pass
        finally:
            # 残スレッドは待たずに解放（間に合わなかったsolveは破棄）
            #   ★ stop_event で capsolver のポーリングも止める（旧: 13:00を跨いでも解き続けていた）
            if keep_pending is not None and hard_deadline is not None:
                # 追い撃ちあり: 未完了ぶんは止めずに引き継ぐ（stop は追い撃ち窓の終了時）
                for _k, _f in enumerate(futs):
                    if _f not in _consumed:
                        keep_pending[_f] = jobs[_k][0]
                keep_pending["__stop__"] = _stop
                ex.shutdown(wait=False)
            else:
                _stop.set()
                try:
                    ex.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    ex.shutdown(wait=False)
        return got
    def _solve_pool(want_pool, deadline_unix=None, keep_pending=None):
        pm = {si: [] for si in range(len(sessions))}
        t0 = time.time(); waves = 0; overran = False
        cut = 0  # 間に合わず切り捨てた台数（直近wave基準）
        for w in range(max(1, want_pool)):
            w0 = time.time()
            # ★ wave0（発火用の本命波）は「解禁-ARM_MARGIN」で強制打ち切り。
            #   遅い台を待たず、準備完了ぶんだけ即armへ回す（出遅れ防止）。
            _hd = None
            if w == 0 and deadline_unix is not None:
                _hd = deadline_unix - _ARM_MARGIN
                _now = time.time()
                if _hd <= _now:
                    # 既に解禁直前/直後：取りこぼし防止に短時間だけ待って即発火へ
                    _hd = _now + float(cfg.get("fire_late_solve_grace_sec", 1.5))
            got = _solve_wave(_hd, keep_pending if w == 0 else None)
            for si, t in got.items(): pm[si].append(t)
            waves += 1
            if w == 0:
                cut = len(sessions) - len(got)
            wdur = time.time() - w0
            # wave0 が（締切を跨いでもなお）解禁後まで食い込んだら「出遅れ」フラグ
            if w == 0 and deadline_unix and time.time() > deadline_unix:
                overran = True; break
            # 次の波が「解禁-ARM_MARGIN」までに丸ごと入らないなら打ち切ってarmへ
            if deadline_unix is not None:
                left = deadline_unix - time.time() - _ARM_MARGIN
                if left < wdur: break
        toks = [(sessions[si], pm[si]) for si in range(len(sessions)) if pm[si]]
        return toks, time.time() - t0, waves, overran, cut

    fired = False
    try:
        while not fired:
            # ── 解禁アライン: 解禁が LEAD 秒以内に迫ったら、T-LEAD まで待ってから
            #    「解禁を跨ぐ一発arm」を張る。これで再arm間の空白に解禁が落ちても出遅れない。
            aligned = False
            _mw = 45000; _iv = _mons
            if rel_dt is not None:
                _sec = (rel_dt - datetime.datetime.now()).total_seconds()
                if _sec > _ALIGN_LEAD + 5:
                    # まだ解禁まで余裕 → 通常の張り付きで待機（空ループ防止に少し待つ）
                    _wait = min(_sec - _ALIGN_LEAD, 30.0)
                    print(f"  {Fore.CYAN}解禁まで約{int(_sec)}s。T-{int(_ALIGN_LEAD)}sまで待機...（{int(_wait)}s）{Style.RESET_ALL}", flush=True)
                    time.sleep(max(1.0, _wait)); continue
                elif _sec > -5:
                    # 解禁直前(〜T-LEAD付近) → すぐ新トークンを作り、解禁を跨いで連続ポーリング。
                    #   ここでは待たない。待つと arm 開始が解禁より後ろにずれて出遅れる。
                    aligned = True
                    _mw = int((_ALIGN_LEAD + _ALIGN_OVER) * 1000)        # 解禁を跨ぐ連続arm
                    _iv = 120                                            # 高速ポーリング
                # _sec <= -5（解禁を過ぎている）→ 通常ループで即張り付き

            # アライン時は解禁時刻を締切に渡し、「解禁前に1個ずつ確保してarm」を最優先。
            _deadline = (rel_dt.timestamp() - _clk_off) if (aligned and rel_dt is not None) else None
            _rwin = float(cfg.get("fire_retry_window_sec", 10.0) or 0.0)
            _retry_on = bool(aligned and rel_dt is not None and _rwin > 0)
            _pending = {} if _retry_on else None
            toks, _soldt, _waves, _overran, _cut = _solve_pool(_tokpool, _deadline, _pending)
            if not toks and not _retry_on:
                print(f"  {Fore.RED}トークン取得0 → 2秒後再試行{Style.RESET_ALL}"); time.sleep(2); continue
            if not toks:
                # 追い撃ちあり: 本隊0台でも、解禁から窓内に解けた台が順次撃つ
                print(f"  {Fore.YELLOW}締切までのトークン0台 → 本隊なし。解禁から{_rwin:.0f}秒は解けた台から追い撃ちします。{Style.RESET_ALL}")
            if aligned and _cut > 0:
                if _retry_on:
                    print(f"  {Fore.YELLOW}準備完了 {len(toks)}台で本隊発火。残 {_cut}台はソルブ継続し、解け次第追い撃ち"
                          f"（解禁+{_rwin:.0f}秒まで）{Style.RESET_ALL}", flush=True)
                else:
                    print(f"  {Fore.YELLOW}準備完了 {len(toks)}台で先行発火（残 {_cut}台はソルブ間に合わず切り捨て）"
                          f"{Style.RESET_ALL}", flush=True)
            _now = datetime.datetime.now().strftime("%H:%M:%S")
            _tag = "解禁跨ぎ連続" if aligned else "最大45s"
            _tok_tot = sum(len(p) for _, p in toks)
            _tok_min = min((len(p) for _, p in toks), default=0)
            _short = f" {Fore.YELLOW}⚠保険{_tok_min}個<リトライ{_attempts}{Style.RESET_ALL}" if _tok_min < _attempts else ""
            _ttl_warn = f" {Fore.RED}⚠ソルブ{_soldt:.0f}s=TTL50s圧迫{Style.RESET_ALL}" if _soldt > 35 else ""
            if _overran:
                print(f"  {Fore.RED}⚠ ソルブが解禁(13時)を跨ぎました＝出遅れ。即armで撃ちますが窓を外す恐れ。"
                      f"台数かtoken_poolを減らしてください。{Style.RESET_ALL}", flush=True)
            print(f"  [{_now}] {len(toks)}台 準備完了（presolve✓ {_tok_tot}個 / 波{_waves} / 1台最少{_tok_min}個{_short} / ソルブ{_soldt:.0f}s{_ttl_warn}）"
                  f" → 解禁検知 arm（{_tag} / poll {_iv}ms）...", flush=True)
            logger.info(f"ARM [{_now}] {len(toks)}台 準備完了 presolve={_tok_tot}個 波{_waves} 1台最少{_tok_min}個 "
                        f"ソルブ{_soldt:.0f}s overran={_overran} poll={_iv}ms")
            def _pref_of(pair):
                # accounts.txt の4番目(時間)。 ID:PW:氏名:時間 → startTime前方一致で希望枠へ。
                pp = pair.split(":", 3)
                return pp[3].strip() if len(pp) > 3 else ""
            packets = [_build_engine_packet(s[0], (pool[0] if pool else ""), f"W{i:02d}",
                                            tokens=pool, pref_time=_pref_of(s[1]))
                       for i, (s, pool) in enumerate(toks)]
            _npref = sum(1 for (s, _pool) in toks if _pref_of(s[1]))
            if _npref:
                print(f"  {Fore.CYAN}  時間指定アカウント: {_npref}台（希望枠へ割当、満員なら隣枠へリトライ）{Style.RESET_ALL}")
            try: _dprx = _api0._prx().get("https") or _api0._prx().get("http") or ""
            except Exception: _dprx = getattr(_api0, "proxy", "") or ""
            # ★ 使い捨てトークン対策: 実際に確保できたトークン数(_tok_min)までしか撃たない。
            #   これを超える試行は使用済みトークンの使い回しで必ず無効なので、無駄撃ち（＝サーバ
            #   負荷・COOLDOWN）を避けるため fire_attempts を実トークン数に丸める。
            _single_use = bool(cfg.get("token_single_use", True))
            _eff_attempts = min(_attempts, _tok_min) if (_single_use and toks) else _attempts
            _eff_attempts = max(1, _eff_attempts)
            if _single_use and _eff_attempts < _attempts:
                print(f"  {Fore.YELLOW}  → トークン不足のためリトライを {_attempts}→{_eff_attempts} に自動調整"
                      f"（無効な使い回し発火を抑止）。{_attempts}回撃つにはリード↑かsolve_workers↑。{Style.RESET_ALL}")
            # ★ 発火トリガ: 時刻優先(既定ON)なら、基本は 解禁+grace ちょうどに発火し、検知は
            #   早発(12:59:xx)時のみ前倒しで使う（検知を待たないので過負荷の検知遅れを回避）。
            _time_priority = bool(cfg.get("fire_time_priority", True))
            _grace_ms = float(cfg.get("fire_time_grace_ms", 400))
            _fire_at = (rel_dt.timestamp() + _grace_ms / 1000.0 - _clk_off) \
                       if (aligned and rel_dt is not None) else 0
            if _time_priority and _fire_at:
                _fa = datetime.datetime.fromtimestamp(_fire_at)
                print(f"  {Fore.CYAN}  発火予定: {Style.BRIGHT}{_fa.strftime('%H:%M:%S')}.{int(_fa.microsecond/1000):03d}"
                      f"{Style.NORMAL}（時刻優先・grace{int(_grace_ms)}ms / 検知が早ければ前倒し）{Style.RESET_ALL}", flush=True)
                logger.info(f"発火予定={_fa.strftime('%H:%M:%S')}.{int(_fa.microsecond/1000):03d} "
                            f"時刻優先 grace={int(_grace_ms)}ms eff_attempts={_eff_attempts} "
                            f"時刻補正={-_clk_off*1000:+.0f}ms(PC時計基準)")
            arm = {
                "detect": {"url": detect_url, "cookie": _fire_extract_cookie(_api0._sess),
                           "headers": dict(_api0._hdr()), "impersonate": getattr(_api0, "_imp_target", "") or "",
                           "proxy": _dprx, "interval_ms": _iv,
                           "timeout_ms": int(cfg.get("fire_detect_timeout_ms", 1200)),
                           "max_wait_ms": _mw, "open_min": _open_min,
                           "match_date": target_date},   # ★ bookable-dates に出た瞬間を検知
                "slots": pre_slots,                       # ★ 事前取得した枠で即発火
                "fire_attempts": _eff_attempts,           # ★ 実トークン数まで（無駄撃ち抑止）
                "fire_at_unix": _fire_at,                 # ★ 未検知でもこの時刻に盲撃ち(0=無効)
                "fire_time_priority": _time_priority,     # ★ true=検知を待たず時刻ちょうどに撃つ
                "preferred_times": list(cfg.get("preferred_times", [])),  # ★ [T]優先時間枠
                "retry_remaining_min": int(cfg.get("retry_remaining_min", 20)),  # ★ リトライは残数≥これの枠からランダム
                "fire_on_timeout": False, "spin_lead_ms": 2.0, "warm_interval_ms": 20000,
                "warm_quiet_ms": int(cfg.get("fire_warm_quiet_ms", 6000)),
                "packets": packets,
            }
            # ── 追い撃ちマネージャ（解禁+window秒まで、落選/未発火の台がソルブ→撃ち直しを繰り返す）──
            _rm = None
            _live_logged = set()   # 本隊の結果を即時ログ済みの tag（最後の一覧では重複出力しない）
            if _retry_on:
                _si_of = {id(_s): _k for _k, _s in enumerate(sessions)}
                _engine_si = [_si_of[id(s)] for (s, _pool) in toks]
                _rel_local = rel_dt.timestamp() - _clk_off
                _rm = _FireRetryManager(cfg, logger, sessions, pre_slots, _idx2time, _solve_one,
                                        _solve_workers, _fire_at or _rel_local, _rel_local + _rwin,
                                        _engine_si, _pref_of, target_date, pending=_pending,
                                        chase_pool=chase_pool, relogin_one=_chase_relogin_one)
                _rm.start()
                print(f"  {Fore.CYAN}  追い撃ち: 解禁〜+{_rwin:.0f}秒は落選/未発火の台がソルブし続けて撃ち直します"
                      f"（本隊{len(toks)}台 / 追撃待ち{len(sessions) - len(toks)}台）{Style.RESET_ALL}", flush=True)
                logger.info(f"追い撃ち窓 {_rwin:.0f}s 本隊={len(toks)} 追撃待ち={len(sessions) - len(toks)}")
            try:
                if not packets:
                    out = {"fired": True, "fired_by": "time", "results": []}   # 本隊0台（追い撃ちのみ）
                else:
                    _proc = _fire_sp.Popen([epath], stdin=_fire_sp.PIPE, stdout=_fire_sp.PIPE,
                                           stderr=_fire_sp.PIPE, text=True,
                                           encoding="utf-8", errors="replace")
                    _err_lines = []
                    def _live_log(_o):
                        # 本隊1台ぶんの結果をその場でログへ（全台の完了を待たない）
                        _tag = str(_o.get("tag", ""))
                        _k = int(_tag[1:])
                        if not (0 <= _k < len(toks)):
                            return
                        _sess_t = toks[_k][0]
                        _em = _sess_t[2]
                        _st = _o.get("http_status")
                        _lat = float(_o.get("latency_ms", 0) or 0)
                        _bd = _o.get("body") or ""
                        _att = _o.get("attempts", 1)
                        _done = float(_o.get("fired_at_unix", 0) or 0) + _lat / 1000.0
                        _dstr = (datetime.datetime.fromtimestamp(_done).strftime('%H:%M:%S.%f')[:-3]
                                 if _done > 1e9 else "?")
                        _wm = re.search(r'"slotIndex"\s*:\s*"?(\d+)"?', _bd)
                        _sidx = _wm.group(1) if _wm else str(_o.get("slot_index", ""))
                        _stime = _idx2time.get(_sidx, "")
                        try: _px = getattr(_sess_t[0], "proxy", "") or "direct"
                        except Exception: _px = "?"
                        _err = _o.get("error") or ""
                        if _o.get("won"):
                            try:
                                _fire_win_webhook(target_date, _stime, len(sessions),
                                                  _ALIGN_LEAD, _attempts,
                                                  store_id=cfg.get("store_id", 2))
                            except Exception: pass
                            print(f"  {Fore.GREEN}🎉 当選(本隊) {_tag} {_em} 枠{_stime or '?'} 応答{_dstr} lat={_lat:.0f}ms"
                                  f"{Style.RESET_ALL}", flush=True)
                            logger.info(f"WIN {_tag} {_em} status={_st} 枠={_stime or '?'} 試行{_att}回 応答={_dstr} "
                                        f"lat={_lat:.0f}ms proxy={_px} body={_bd[:120]}")
                        else:
                            logger.info(f"LOSE {_tag} {_em} status={_st} 試行{_att}回 応答={_dstr} lat={_lat:.0f}ms "
                                        f"proxy={_px}{' err=' + _err[:60] if _err else ''} body={_bd[:160]}")
                        _live_logged.add(_tag)
                    def _read_err(_p=_proc):
                        # エンジンは1台撃ち終わるごとに "@@RES {...}" を stderr に出す
                        #   → その場でログ出力 ＋ 落選台を即追い撃ちへ
                        for _ln in _p.stderr:
                            if _ln.startswith("@@RES "):
                                try:
                                    _o = json.loads(_ln[6:])
                                except Exception:
                                    continue
                                try:
                                    if "latency_ms" in _o:      # 新エンジン（結果つき）
                                        _live_log(_o)
                                except Exception:
                                    pass
                                if _rm is not None:
                                    try:
                                        _k = int(str(_o.get("tag", "W-1"))[1:])
                                        if 0 <= _k < len(_engine_si):
                                            _rm.engine_result(_engine_si[_k], bool(_o.get("won")))
                                    except Exception:
                                        pass
                            else:
                                _err_lines.append(_ln)
                    _et = threading.Thread(target=_read_err, daemon=True); _et.start()
                    # ★ stderr は _read_err だけが読む（communicate() に渡すと @@RES 行を取り合って取りこぼす）
                    _proc.stdin.write(json.dumps(arm, ensure_ascii=False)); _proc.stdin.close()
                    _so_box = {}
                    _ot = threading.Thread(target=lambda: _so_box.setdefault("v", _proc.stdout.read()),
                                           daemon=True); _ot.start()
                    _ot.join(timeout=int(_mw/1000) + 20)
                    if _ot.is_alive():
                        _proc.kill(); _ot.join(timeout=3)
                    try: _proc.wait(timeout=3)
                    except Exception: pass
                    _so = _so_box.get("v", "")
                    _et.join(timeout=2)
                    class _R: pass
                    r = _R(); r.stdout = _so or ""; r.stderr = "".join(_err_lines)
                    out = _parse_engine_out(r)
            except Exception as e:
                _stderr = ("".join(_err_lines) if '_err_lines' in dir() else '')[:200]
                print(f"  {Fore.RED}エンジンエラー: {e} → 再試行{Style.RESET_ALL}")
                print(f"  stderr: {_stderr}")
                logger.error(f"engine error: {e}  stderr={_stderr}")
                if _rm is not None:
                    _rm.abort()
                time.sleep(1); continue
            if not (out.get("detected") or out.get("fired")):
                if _rm is not None:
                    _rm.abort()
                print(f"  …未検知（まだ解禁前）。再準備します。")
                logger.info("未検知（まだ解禁前）。再arm。")
                continue
            fired = True
            if _rm is not None:
                _rm.finish()   # 解禁+window秒まで追い撃ちを続けてから集計
            results = out.get("results", [])
            fs = [x.get("fired_at_unix", 0) for x in results if x.get("fired_at_unix")]
            spread = (max(fs) - min(fs)) * 1000 if len(fs) >= 2 else 0.0
            _fby = out.get("fired_by", "detect")
            _hdr = {"detect": "★ 解禁検知", "time": "⏱ 時刻フォールバック発火(検知漏れ)",
                    "trigger": "▶ トリガ発火"}.get(_fby, "★ 発火")
            # 検知時刻（エンジンが解禁を捉えた瞬間）と 検知→発火 の間隔を表示
            _dt = out.get("detected_at_unix", 0) or 0
            _dstr = ""
            if _dt:
                _lt = datetime.datetime.fromtimestamp(_dt)
                _gap = (min(fs) - _dt) * 1000 if fs else 0.0
                _dstr = f"  検知{_lt.strftime('%H:%M:%S')}.{int(_lt.microsecond/1000):03d} → 発火まで{_gap:.0f}ms"
            elif _fby == "time":
                _dstr = f"  {Fore.YELLOW}（検知せず時刻{rel_dt.strftime('%H:%M:%S') if rel_dt else ''}で盲撃ち）{Style.RESET_ALL}"
            print(f"\n{Fore.GREEN}{Style.BRIGHT}{_hdr} → {len(fs)}台 一斉発火 (spread {spread:.2f}ms){_dstr}{Style.RESET_ALL}")
            # ★ .log にも発火詳細を残す（コンソールと同内容・色コード無し）
            _dt_log = ""
            if _dt:
                _lt2 = datetime.datetime.fromtimestamp(_dt)
                _gap2 = (min(fs) - _dt) * 1000 if fs else 0.0
                _dt_log = f"  検知={_lt2.strftime('%H:%M:%S')}.{int(_lt2.microsecond/1000):03d} 検知→発火={_gap2:.0f}ms"
            logger.info(f"FIRE {_hdr} fired_by={_fby} 台数={len(fs)} spread={spread:.2f}ms{_dt_log} "
                        f"target_date={target_date}")
            # 実測タイミング: POST送信〜完了の時刻窓（13:00:00設定でも実際は“検知連動”）
            if fs:
                _snd = datetime.datetime.fromtimestamp(min(fs))
                _comps = [x.get("fired_at_unix", 0) + (x.get("latency_ms", 0) / 1000.0)
                          for x in results if x.get("fired_at_unix")]
                if _comps:
                    _c0 = datetime.datetime.fromtimestamp(min(_comps))
                    _c1 = datetime.datetime.fromtimestamp(max(_comps))
                    _rtmin = min(x.get('latency_ms',0) for x in results)
                    _rtmax = max(x.get('latency_ms',0) for x in results)
                    print(f"  POST送信 {_snd.strftime('%H:%M:%S')}.{int(_snd.microsecond/1000):03d}"
                          f"  → POST完了 {_c0.strftime('%H:%M:%S')}.{int(_c0.microsecond/1000):03d}"
                          f"〜{_c1.strftime('%H:%M:%S')}.{int(_c1.microsecond/1000):03d}"
                          f"（往復 {_rtmin:.0f}〜{_rtmax:.0f}ms）")
                    logger.info(f"POST送信={_snd.strftime('%H:%M:%S')}.{int(_snd.microsecond/1000):03d} "
                                f"完了={_c0.strftime('%H:%M:%S')}.{int(_c0.microsecond/1000):03d}"
                                f"〜{_c1.strftime('%H:%M:%S')}.{int(_c1.microsecond/1000):03d} "
                                f"往復={_rtmin:.0f}〜{_rtmax:.0f}ms")
                    # 発火遅れ = 実POST送信 − 発火予定（同じPC時計同士なので時計ずれの影響なし）
                    if _fire_at and _fby == "time":
                        _late = (min(fs) - _fire_at) * 1000.0
                        _wl = out.get("warm_late")
                        _col = Fore.GREEN if _late < 30 else (Fore.YELLOW if _late < 150 else Fore.RED)
                        print(f"  {_col}発火遅れ {_late:+.0f}ms（予定→POST送信）{Style.RESET_ALL}"
                              + (f"  温め未完了 {_wl}台" if _wl else "")
                              + ("" if "planned_fire_unix" in out else
                                 f"  {Fore.YELLOW}※旧エンジン（v2未導入）{Style.RESET_ALL}"))
                        logger.info(f"発火遅れ={_late:+.0f}ms 温め未完了={_wl} 検知読取={out.get('detect_reads')} "
                                    f"engine={'v2' if 'planned_fire_unix' in out else 'old'}")
            _won = 0; _att_tot = 0; _tok_reused = 0
            for idx, x in enumerate(results):
                body = x.get("body") or ""
                is_won = x.get("won") or ('"result":"won"' in body) or ('"result": "won"' in body)
                st = x.get("http_status")
                nat = x.get("attempts", 1); _att_tot += nat
                _tn = x.get("tokens_n", 1)
                # トークン不足で使い回した試行があるか（使い捨てトークンだと2発目以降が無効の恐れ）
                _reuse = f" {Fore.MAGENTA}[token不足:{_tn}個を{nat}試行で使い回し]{Style.RESET_ALL}" if nat > _tn else ""
                if nat > _tn: _tok_reused += 1
                pair = toks[idx][0][1] if idx < len(toks) else ""
                email = toks[idx][0][2] if idx < len(toks) else ""
                # この台のプロキシと実測往復(ms) — プロキシ別速度ランキング用にログへ出す。
                try:
                    _pxy = getattr(toks[idx][0][0], "proxy", "") or "direct"
                except Exception:
                    _pxy = "?"
                _lat = x.get("latency_ms", 0)
                if is_won:
                    _won += 1
                    # 当選したサーバー応答から slotIndex を拾い、予約時間に変換して記録。
                    _wim = re.search(r'"slotIndex"\s*:\s*"?(\d+)"?', body)
                    _wtime = _idx2time.get(_wim.group(1), "") if _wim else ""
                    _slot_disp = f"{target_date},{_wtime}" if _wtime else target_date
                    try: mark_success(pair, _slot_disp)
                    except Exception: pass
                    _wtxt = f" 枠{_wtime}" if _wtime else ""
                    if x.get("tag") not in _live_logged:
                        try:
                            _fire_win_webhook(target_date, _wtime, len(sessions),
                                              _ALIGN_LEAD, _attempts,
                                              store_id=cfg.get("store_id", 2))
                        except Exception: pass
                        print(f"  {Fore.GREEN}🎉 当選 {x.get('tag')} {email} status={st}{_wtxt} (試行{nat}回目){Style.RESET_ALL}")
                        logger.info(f"WIN {x.get('tag')} {email} status={st} 枠={_wtime or '?'} 試行{nat}回 "
                                    f"lat={_lat:.0f}ms proxy={_pxy} body={body[:120]}")
                else:
                    # token検証失敗の兆候（使い捨てトークンの再利用が弾かれた可能性）を拾う
                    _tokbad = any(k in body for k in ("token", "turnstile", "captcha", "duplicate"))
                    _mark = f" {Fore.MAGENTA}← token検証?{Style.RESET_ALL}" if _tokbad else ""
                    if x.get("tag") not in _live_logged:
                        print(f"  {Fore.YELLOW}✗ {x.get('tag')} status={st} 試行{nat}回 {body[:50]}{_mark}{_reuse}{Style.RESET_ALL}")
                        logger.info(f"LOSE {x.get('tag')} {email} status={st} 試行{nat}回"
                                    f"{' token再利用' if nat > _tn else ''}{' token検証?' if _tokbad else ''} "
                                    f"lat={_lat:.0f}ms proxy={_pxy} body={body[:160]}")
            _avg = (_att_tot / len(results)) if results else 0
            _warn = f"  {Fore.MAGENTA}※{_tok_reused}台がtoken不足で使い回し（使い捨てなら要token_pool↑）{Style.RESET_ALL}" if _tok_reused else ""
            # 落選の内訳（当選率が低い原因切り分け: 席満員 vs トークン拒否 vs クールダウン等）
            from collections import Counter as _Counter
            _codes = dict(_Counter(x.get("http_status") for x in results))
            _nw = sum(1 for x in results if '"not_won"' in (x.get("body") or ""))
            # ★ 当選は除外。当選本文に tokenNonce/ticketToken が入り "token" に誤一致して
            #   拒否カウントを水増しするのを防ぐ（bare "token" は使わない）。
            def _is_won_x(x):
                return bool(x.get("won")) or ('"result":"won"' in (x.get("body") or ""))
            _tokrej = sum(1 for x in results
                          if not _is_won_x(x)
                          and any(k in (x.get("body") or "").lower()
                                  for k in ("turnstile", "captcha", "duplicate",
                                            "invalid token", "invalid_token", "token_invalid")))
            print(f"\n{Fore.GREEN}結果: 当選 {_won} / 発火 {len(results)}  "
                  f"(spread {spread:.2f}ms, 平均{_avg:.1f}試行/台){Style.RESET_ALL}{_warn}")
            print(f"  内訳: status={_codes}  not_won(落選)={_nw}  token拒否疑い={_tokrej}")
            logger.info(f"結果: 当選={_won}/発火={len(results)} spread={spread:.2f}ms 平均試行={_avg:.1f}"
                        f"{' token再利用台=' + str(_tok_reused) if _tok_reused else ''}")
            logger.info(f"内訳: status={_codes} not_won(落選)={_nw} token拒否疑い={_tokrej}")
            if _rm is not None:
                _rs = _rm.stats
                _det = (datetime.datetime.fromtimestamp(_rm.detected_at).strftime('%H:%M:%S.%f')[:-3]
                        if _rm.detected_at else "未検知（残り人数は使わずランダム）")
                print(f"{Fore.GREEN}追い撃ち: 当選 {_rs['won']} / POST {_rs['post']}回  内訳={_rs['codes']}  解禁検知={_det}{Style.RESET_ALL}")
                print(f"{Fore.GREEN}{Style.BRIGHT}合計当選: {_won + _rs['won']}{Style.RESET_ALL}")
                logger.info(f"追い撃ち: 当選={_rs['won']} POST={_rs['post']} 内訳={_rs['codes']} 合計当選={_won + _rs['won']}")
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}停止しました。{Style.RESET_ALL}")
    _pause()


def menu_proxy_tester(cfg: dict):
    """本番前チェック: proxies.txt の各プロキシから解禁判定エンドポイントへ
    実HTTPS往復を測り、速い順にランキング。遅い回線を特定して除外できる。
    先着の勝敗は往復レイテンシでほぼ決まるため、ここで300ms前後に揃えると当選率が上がる。"""
    import statistics as _st
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
    _box("プロキシテスター (本番前チェック)")
    prxs = load_proxies()
    if not prxs:
        _err("proxies.txt が空か存在しません。"); _pause(); return
    base = cfg.get("base_url", "https://mellojoylucky.cn")
    url = f"{base}/api/registrations/bookable-dates"
    _thr = _inp_int("遅いと判定する往復しきい値(ms)", int(cfg.get("proxy_test_threshold_ms", 400)))
    _samples = max(1, _inp_int("1プロキシあたりの計測回数", int(cfg.get("proxy_test_samples", 3))))
    _timeout = 8.0
    _workers = min(len(prxs), max(4, int(cfg.get("solve_workers", 24))))
    print(f"\n  計測先 : {Fore.CYAN}{url}{Style.RESET_ALL}")
    print(f"  {len(prxs)}プロキシ × {_samples}回 を並列{_workers}で計測中...\n", flush=True)

    try:
        import requests as _rq
    except Exception:
        _err("requests が必要です。pip install requests"); _pause(); return

    def _measure(raw):
        label = raw.strip()
        try:
            u = _normalize_proxy_url(raw)
            proxies = {"http": u, "https": u}
        except Exception:
            return (label, None, "parse")
        # host:port をラベルに（突合しやすく）
        try:
            _hp = u.split("://", 1)[1].split("@")[-1]
            label = _hp
        except Exception:
            pass
        lats = []; err = ""
        s = _rq.Session(); s.trust_env = False
        for _ in range(_samples):
            t0 = time.perf_counter()
            try:
                r = s.get(url, proxies=proxies, timeout=_timeout,
                          headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
                dt = (time.perf_counter() - t0) * 1000.0
                if r.status_code < 500:
                    lats.append(dt)
                else:
                    err = f"HTTP{r.status_code}"
            except Exception as e:
                err = type(e).__name__
        return (label, (sorted(lats) if lats else None), err, raw.strip())

    rows = []; dead = []
    with _TPE(max_workers=_workers) as ex:
        futs = [ex.submit(_measure, p) for p in prxs]
        for fu in _asc(futs):
            res = fu.result()
            label, lats, err = res[0], res[1], res[2]
            raw = res[3] if len(res) > 3 else label
            if not lats:
                dead.append((label, err)); continue
            med = _st.median(lats); p95 = lats[max(0, int(len(lats) * 0.95) - 1)]
            rows.append((med, p95, min(lats), len(lats), label, raw))
    rows.sort(key=lambda r: r[0])

    print(f"=== 往復速度ランキング（速い順・中央値） ===")
    print(f"{'#':>3} {'median':>8} {'p95':>8} {'min':>8} {'n':>3}  proxy")
    for i, r in enumerate(rows, 1):
        med, p95, mn, n, label, raw = r
        mark = f"{Fore.GREEN}✓{Style.RESET_ALL}" if med <= _thr else f"{Fore.RED}✗{Style.RESET_ALL}"
        col = Fore.GREEN if med <= _thr else (Fore.YELLOW if med <= _thr * 2 else Fore.RED)
        print(f"{i:>3} {col}{med:>7.0f}m{Style.RESET_ALL} {p95:>7.0f}m {mn:>7.0f}m {n:>3}  {mark} {label}")
    _fast = [r for r in rows if r[0] <= _thr]
    print(f"\n  {Fore.GREEN}{_thr}ms以下(速い)={len(_fast)}{Style.RESET_ALL} / "
          f"{Fore.RED}超過(遅い)={len(rows) - len(_fast)}{Style.RESET_ALL} / "
          f"到達不可={len(dead)}  （全{len(prxs)}）")
    if dead:
        print(f"\n  {Fore.RED}到達不可/失敗（発火プールから除外推奨）:{Style.RESET_ALL}")
        for label, err in dead[:50]:
            print(f"    ✗ {label}  ({err})")

    if _fast:
        _w = input(f"\n  速い{len(_fast)}件を proxies_fast.txt に書き出しますか？ [y/N]: ").strip().lower()
        if _w == "y":
            try:
                _dst = BASE / "proxies_fast.txt"
                with open(_dst, "w", encoding="utf-8") as f:
                    f.write("\n".join(r[5] for r in _fast) + "\n")
                print(f"  {Fore.GREEN}[✓] {len(_fast)}件を書き出し → {_dst}{Style.RESET_ALL}")
                _r = input(f"  これを proxies.txt に反映（上書き）しますか？ 元は proxies.bak に退避 [y/N]: ").strip().lower()
                if _r == "y":
                    try:
                        if PROXIES_F.exists():
                            (BASE / "proxies.bak").write_text(
                                PROXIES_F.read_text(encoding="utf-8"), encoding="utf-8")
                        PROXIES_F.write_text("\n".join(r[5] for r in _fast) + "\n", encoding="utf-8")
                        print(f"  {Fore.GREEN}[✓] proxies.txt を速い{len(_fast)}件に更新（元は proxies.bak）{Style.RESET_ALL}")
                    except Exception as _e:
                        _err(f"proxies.txt 更新失敗: {_e}")
            except Exception as _e:
                _err(f"書き出し失敗: {_e}")
    _pause()


def main():
    # ★ タスクバーに exe 埋め込みアイコンを反映（コンソール窓へ WM_SETICON）
    _set_taskbar_icon()
    # ★ Fix 45: 起動時に仮ロガー設定（cfg 読込前）
    _setup_logging(debug_mode=False)

    # ★ 初回起動: 必要な入力ファイルが無ければ空テンプレを自動生成（既存は上書きしない）
    _new_files = _ensure_data_files()
    if _new_files:
        print(f"{Fore.CYAN}[初回] 入力ファイルの雛形を作成しました: "
              f"{', '.join(_new_files)}{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}  → accounts.txt などに記入してから各モードを実行してください。"
              f"{Style.RESET_ALL}")
        try:
            logging.getLogger().info(f"初回テンプレ生成: {', '.join(_new_files)}")
        except Exception:
            pass

    # Suppress to file only (no console spam)
    for name in ("creator","register","fcfs","worker","cfbypass"):
        logging.getLogger(name).setLevel(logging.DEBUG)

    # Ensure Playwright Chromium is present (downloads on first run if needed)
    _ensure_playwright_browsers()

    cfg = load_cfg()

    # ★ Fix 45: cfg の debug_mode を反映してロガーを再設定
    if cfg.get("debug_mode", False):
        # 既存ハンドラをクリアして debug_mode=True で再設定
        root = logging.getLogger()
        for _h in root.handlers[:]:
            root.removeHandler(_h)
            _h.close()
        _setup_logging(debug_mode=True)
        for name in ("creator","register","fcfs","worker","cfbypass"):
            logging.getLogger(name).setLevel(logging.DEBUG)
        logging.getLogger().info("Debug mode ON — logs/YYYYMMDD_HHMMSS.log に DEBUG ログを記録")

    while True:
        _box(f"MelloJoy BOT  v{APP_VERSION}  [{BUILD_ID}]")

        accs    = load_accounts()
        prxs    = load_proxies()
        succ    = (SUCCESS_F.read_text(encoding="utf-8").count("\n")
                   if SUCCESS_F.exists() else 0)
        created = (CREATED_F.read_text(encoding="utf-8").count("\n")
                   if CREATED_F.exists() else 0)
        names_ok = f"{len(load_names())} names" if NAMES_F.exists() else "names.txt not found"

        print(f"  Accounts: {Fore.WHITE}{len(accs)}{Style.RESET_ALL}  "
              f"Created: {Fore.CYAN}{created}{Style.RESET_ALL}  "
              f"Booked: {Fore.GREEN}{succ}{Style.RESET_ALL}  "
              f"Proxies: {Fore.WHITE}{len(prxs) if prxs else 'none'}{Style.RESET_ALL}  "
              f"Names: {Fore.WHITE}{names_ok}{Style.RESET_ALL}")
        print(f"""
  {Fore.CYAN}[1]{Style.RESET_ALL} Account Creator
  {Fore.CYAN}[2]{Style.RESET_ALL} FCFS — First Come First Served
  {Fore.CYAN}[3]{Style.RESET_ALL} Result Mode      (accountresult.txt → CSV)
  {Fore.CYAN}[4]{Style.RESET_ALL} Settings
  {Fore.CYAN}[5]{Style.RESET_ALL} Turnstile テスト  (デバッグ / トークン受け渡し確認)
  {Fore.CYAN}[6]{Style.RESET_ALL} Fire Engine 本番 (A案・検知→一斉発火・当選保存)
  {Fore.CYAN}[7]{Style.RESET_ALL} プロキシテスター (本番前チェック・往復速度ランキング)
  {Fore.CYAN}[0]{Style.RESET_ALL} Exit
""")
        c = input(f"{Fore.WHITE}> {Style.RESET_ALL}").strip()
        if   c == "1": menu_create_account(cfg)
        elif c == "2": menu_fcfs(cfg)
        elif c == "3": menu_result_mode()
        elif c == "4": cfg = menu_settings(cfg)
        elif c == "5": menu_test_turnstile(cfg)
        elif c == "6": menu_fire_engine_prod(cfg)
        elif c == "7": menu_proxy_tester(cfg)
        elif c == "0":
            print(f"\n{Fore.CYAN}Goodbye.{Style.RESET_ALL}\n")
            # ★ Fix 21: Webhook キューを flush してからプロセスを終了
            #            予約成功直後に終了してもWebhook POSTが欠落しない
            _wh_pool.shutdown(wait=True)
            sys.exit(0)

if __name__ == "__main__":
    main()
