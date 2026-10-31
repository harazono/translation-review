#!/usr/bin/env python3
"""
論文翻訳レビュー用ローカルサーバー

日本語原稿と英訳を段落ごとに左右に並べて比較し、英単語をクリックすると
英和辞書 (EJDict, CC0) と英英辞書 (Free Dictionary API) で意味を調べられます。

使い方:
    python3 translation_review.py            # http://127.0.0.1:8000 を開く
    python3 translation_review.py --port 8080 --no-browser

外部パッケージは不要です (Python 3.8+ の標準ライブラリのみ)。
初回起動時に英和辞書データ (約 5MB) を ./ejdict/ にダウンロードします。

Google ドキュメントから原稿を読み込む機能を使う場合のみ、追加で
    pip install google-auth-oauthlib
と、Google Cloud で作成した OAuth クライアント (credentials.json) が必要です (README 参照)。
"""

import argparse
import functools
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
EJDICT_DIR = BASE_DIR / "ejdict"
EJDICT_URL = "https://raw.githubusercontent.com/kujirahand/EJDict/master/src/{}.txt"
FREEDICT_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/{}"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_API = "https://www.googleapis.com/drive/v3/files/"


# ---------------------------------------------------------------------------
# HTTP 取得 (macOS の python.org 版で証明書エラーになる場合は curl にフォールバック)
# ---------------------------------------------------------------------------
def fetch(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "translation-review/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, ssl.SSLError) as e:
        if "CERTIFICATE" not in str(e).upper():
            raise
        out = subprocess.run(["curl", "-sfL", "--max-time", str(timeout), url],
                             capture_output=True, check=True)
        return out.stdout


# ---------------------------------------------------------------------------
# 英和辞書 (EJDict)
# ---------------------------------------------------------------------------
class EJDict:
    def __init__(self):
        self.entries = {}  # headword(lower) -> [meaning, ...]
        self.ready = False

    def ensure_downloaded(self):
        EJDICT_DIR.mkdir(exist_ok=True)
        missing = [c for c in "abcdefghijklmnopqrstuvwxyz" if not (EJDICT_DIR / f"{c}.txt").exists()]
        if not missing:
            return
        print(f"英和辞書 EJDict をダウンロード中 ({len(missing)} ファイル) ...", flush=True)
        for c in missing:
            try:
                (EJDICT_DIR / f"{c}.txt").write_bytes(fetch(EJDICT_URL.format(c), timeout=30))
            except Exception as e:  # noqa: BLE001
                print(f"  {c}.txt の取得に失敗しました: {e}", file=sys.stderr)

    def load(self):
        self.ensure_downloaded()
        for path in sorted(EJDICT_DIR.glob("*.txt")):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "\t" not in line:
                    continue
                head, meaning = line.split("\t", 1)
                for h in head.split(","):
                    h = h.strip().lower()
                    if h:
                        self.entries.setdefault(h, []).append(meaning.strip())
        self.ready = True
        print(f"英和辞書: {len(self.entries):,} 見出し語を読み込みました", flush=True)

    def lookup(self, word):
        return self.entries.get(word.lower(), [])


def lemma_candidates(word):
    """活用形から原形の候補を推定する (簡易版)。"""
    w = word.lower().strip()
    w = re.sub(r"[’']s$", "", w)
    cands = [w]
    rules = [
        ("ies", "y"), ("ied", "y"), ("ier", "y"), ("iest", "y"), ("ily", "y"),
        ("sses", "ss"), ("xes", "x"), ("ches", "ch"), ("shes", "sh"), ("es", "e"), ("es", ""), ("s", ""),
        ("ing", ""), ("ing", "e"), ("ed", ""), ("ed", "e"), ("d", ""),
        ("er", ""), ("er", "e"), ("est", ""), ("est", "e"), ("ly", ""), ("ally", ""),
        ("iness", "y"), ("ness", ""), ("ically", "ic"),
    ]
    for suf, rep in rules:
        if w.endswith(suf) and len(w) - len(suf) >= 2:
            stem = w[: -len(suf)] + rep
            cands.append(stem)
            # stopped -> stop, running -> run
            if suf in ("ing", "ed", "er", "est") and len(stem) >= 3 and stem[-1] == stem[-2]:
                cands.append(stem[:-1])
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 英英辞書 (Free Dictionary API) — 結果はメモリにキャッシュ
# ---------------------------------------------------------------------------
_en_cache = {}


def lookup_english(word):
    word = word.lower()
    if word in _en_cache:
        return _en_cache[word]
    result = {"phonetic": "", "meanings": []}
    try:
        data = json.loads(fetch(FREEDICT_URL.format(urllib.parse.quote(word, safe=""))))
        for entry in data:
            result["phonetic"] = result["phonetic"] or entry.get("phonetic", "")
            for m in entry.get("meanings", []):
                result["meanings"].append({
                    "pos": m.get("partOfSpeech", ""),
                    "defs": [
                        {"def": d.get("definition", ""), "example": d.get("example", "")}
                        for d in m.get("definitions", [])[:4]
                    ],
                    "synonyms": m.get("synonyms", [])[:8],
                })
    except urllib.error.HTTPError as e:
        if e.code != 404:
            result["error"] = f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)
    if "error" not in result:
        _en_cache[word] = result
    return result


EJ = EJDict()


def lookup(query):
    q = re.sub(r"\s+", " ", query.strip())
    res = {"query": q, "ej": [], "en": None, "headword": q, "dict_ready": EJ.ready}
    # 熟語ならそのまま、単語なら原形候補も順に試す
    cands = [q.lower()] if " " in q else lemma_candidates(q)
    for c in cands:
        hits = EJ.lookup(c)
        if hits:
            res["headword"] = c
            res["ej"] = [{"headword": c, "meanings": [s.strip() for s in m.split(" / ")]} for m in hits]
            break
    en_word = res["headword"] if res["ej"] else (cands[0] if cands else q)
    res["en"] = lookup_english(en_word)
    if not res["en"]["meanings"] and " " not in q:
        for c in cands[1:]:
            r = lookup_english(c)
            if r["meanings"]:
                res["en"] = r
                break
    return res


# ---------------------------------------------------------------------------
# Google Drive (OAuth) — Google ドキュメントをプレーンテキストで取得
# ---------------------------------------------------------------------------
class DriveError(Exception):
    pass


_drive_lock = threading.Lock()
DOC_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{20,})|^([A-Za-z0-9_-]{20,})$")


def drive_libs_available():
    try:
        import google_auth_oauthlib.flow  # noqa: F401
        import google.auth.transport.requests  # noqa: F401
        return True
    except ImportError:
        return False


def drive_status():
    return {
        "libs": drive_libs_available(),
        "credentials": CREDENTIALS_FILE.exists(),
        "authorized": TOKEN_FILE.exists(),
        "token_time": TOKEN_FILE.stat().st_mtime if TOKEN_FILE.exists() else 0,
    }


def load_credentials():
    """保存済みトークンを読み込む (期限切れなら更新)。ログインが必要なら None。"""
    if not drive_libs_available():
        raise DriveError("Google ライブラリがありません。`pip install google-auth-oauthlib` を実行してサーバーを再起動してください。")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), DRIVE_SCOPES)
    except ValueError:
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)
            return creds
        except Exception:  # noqa: BLE001  (取り消し・期限切れのリフレッシュトークン)
            return None
    return None


def save_credentials(creds):
    TOKEN_FILE.write_text(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)


# ログインは「画面のリンク → Google の同意画面 → このサーバーの / に code 付きで戻る」流れ。
# 同意画面の URL を出したときの flow を、戻ってくるまで保持しておく。
_pending_auth = {}


def start_auth(port):
    if not CREDENTIALS_FILE.exists():
        raise DriveError(f"{CREDENTIALS_FILE.name} がありません。README の手順で OAuth クライアントを作成し、スクリプトと同じフォルダに置いてください。")
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(str(CREDENTIALS_FILE), DRIVE_SCOPES,
                                         redirect_uri=f"http://localhost:{port}/")
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    _pending_auth.clear()
    _pending_auth.update(flow=flow, state=state)
    return url


def finish_auth(params):
    if "error" in params:
        raise DriveError(f"Google のログインが完了しませんでした ({params['error']})")
    flow = _pending_auth.get("flow")
    if not flow or params.get("state") != _pending_auth.get("state"):
        raise DriveError("ログインの有効期限が切れました。翻訳レビューの画面からもう一度「Drive から読み込む」を押してください。")
    flow.fetch_token(code=params.get("code", ""))
    save_credentials(flow.credentials)
    _pending_auth.clear()


def _api_error(r):
    try:
        return r.json()["error"]["message"]
    except Exception:  # noqa: BLE001
        return f"HTTP {r.status_code}"


def fetch_google_doc(session, ref):
    m = DOC_ID_RE.search(ref.strip())
    if not m:
        raise DriveError("Google ドキュメントの URL または ID として認識できません")
    file_id = m.group(1) or m.group(2)
    meta = session.get(DRIVE_API + file_id,
                       params={"fields": "name,mimeType,modifiedTime", "supportsAllDrives": "true"})
    if meta.status_code == 404:
        raise DriveError("ファイルが見つかりません (URL と、ログイン中のアカウントのアクセス権を確認してください)")
    if not meta.ok:
        raise DriveError(_api_error(meta))
    info = meta.json()
    if info.get("mimeType") != "application/vnd.google-apps.document":
        raise DriveError(f"「{info.get('name')}」は Google ドキュメントではありません")
    r = session.get(DRIVE_API + file_id + "/export", params={"mimeType": "text/plain"})
    if not r.ok:
        raise DriveError(_api_error(r))
    text = r.content.decode("utf-8-sig")
    # Google ドキュメントの段落は 1 改行区切りで書き出されるので、空行区切りに揃える
    # (段落内の改行 Shift+Enter は \v で書き出されるので、段落の中の改行として残す)
    paras = [p.strip().replace("\v", "\n") for p in re.split(r"\r?\n", text)]
    return {"name": info.get("name", ""), "modified": info.get("modifiedTime", ""),
            "text": "\n\n".join(p for p in paras if p)}


def drive_fetch(refs, port):
    with _drive_lock:
        creds = load_credentials()
        if not creds:
            return {"auth_url": start_auth(port)}
    from google.auth.transport.requests import AuthorizedSession
    session = AuthorizedSession(creds)
    session.request = functools.partial(session.request, timeout=30)
    out = {}
    for key, ref in refs.items():
        if not ref or not ref.strip():
            continue
        try:
            out[key] = fetch_google_doc(session, ref)
        except DriveError as e:
            out[key] = {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            out[key] = {"error": f"取得に失敗しました: {e}"}
    return out


# ---------------------------------------------------------------------------
# HTTP サーバー
# ---------------------------------------------------------------------------
MAX_BODY = 1 << 20  # POST 本文の上限 (1MB)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _host_allowed(self):
        """DNS リバインディング対策: ループバックで待ち受けているときは Host ヘッダーを検査する。"""
        allowed = self.server.allowed_hosts
        return allowed is None or self.headers.get("Host", "") in allowed

    def _origin_allowed(self):
        """CSRF 対策: 他サイトのページからの POST を拒否する。"""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        allowed = self.server.allowed_hosts
        host = urllib.parse.urlparse(origin).netloc
        return host in allowed if allowed is not None else host == self.headers.get("Host", "")

    def do_GET(self):
        if not self._host_allowed():
            self._send(403, b"forbidden", "text/plain")
            return
        url = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/" and ("code" in params or "error" in params):
            self._oauth_callback(params)
        elif url.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/lookup":
            q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
            if not q.strip():
                self._send(400, b'{"error":"empty"}', "application/json")
                return
            self._json(lookup(q))
        elif url.path == "/api/drive/status":
            self._json(drive_status())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/drive/fetch":
            self._send(404, b"not found", "text/plain")
            return
        # application/json を必須にすると、他サイトからの送信には CORS のプリフライトが必要になり、ここに届かない
        if (not self._host_allowed() or not self._origin_allowed()
                or not self.headers.get("Content-Type", "").startswith("application/json")):
            self._send(403, b"forbidden", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, b"too large", "text/plain")
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            refs = {k: str(body.get(k, "")) for k in ("ja", "en")}
            self._json(drive_fetch(refs, self.server.server_address[1]))
        except DriveError as e:
            self._json({"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"取得に失敗しました: {e}"})

    def _oauth_callback(self, params):
        try:
            with _drive_lock:
                finish_auth(params)
            msg, ok = "Google へのログインが完了しました。このタブを閉じて、翻訳レビューの画面に戻ってください（自動で読み込みが始まります）。", True
        except Exception as e:  # noqa: BLE001
            msg, ok = str(e), False
        html = (f"<!doctype html><meta charset='utf-8'><title>翻訳レビュー</title>"
                f"<body style='font-family:sans-serif;padding:40px;line-height:1.8'>"
                f"<h2>{'✅' if ok else '⚠'} {'ログイン完了' if ok else 'ログイン失敗'}</h2><p>{html_escape(msg)}</p>"
                f"<script>{'setTimeout(()=>window.close(),1500)' if ok else ''}</script>")
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


PAGE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>翻訳レビュー</title>
<style>
  :root {
    --bg: #f7f7f5; --panel: #fff; --text: #1d1d1f; --muted: #6b6b70; --line: #e3e3e0;
    --accent: #2f6fdf; --accent-soft: #e8f0fd; --hl: #fff6d6; --ok: #2e8b57; --warn: #b26a00;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.7 -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", sans-serif; }
  header { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; gap: 12px;
           padding: 10px 16px; background: var(--panel); border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  header h1 { font-size: 16px; margin: 0 8px 0 0; }
  .tabs button { border: 1px solid var(--line); background: var(--panel); padding: 5px 14px; cursor: pointer; font: inherit; }
  .tabs button:first-child { border-radius: 6px 0 0 6px; }
  .tabs button:last-child { border-radius: 0 6px 6px 0; border-left: none; }
  .tabs button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .spacer { flex: 1; }
  .btn { border: 1px solid var(--line); background: var(--panel); border-radius: 6px; padding: 5px 12px;
         cursor: pointer; font: inherit; font-size: 13px; }
  .btn:hover { background: var(--accent-soft); }
  .btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  a.btn { display: inline-block; text-decoration: none; }
  select, input[type=search] { font: inherit; font-size: 13px; padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px; }
  .status { font-size: 13px; color: var(--muted); }
  .status.warn { color: var(--warn); }

  main { display: flex; gap: 0; min-height: calc(100vh - 54px); }
  #content { flex: 1; min-width: 0; padding: 16px; }
  aside { width: 320px; flex-shrink: 0; border-left: 1px solid var(--line); background: var(--panel);
          padding: 14px; position: sticky; top: 54px; height: calc(100vh - 54px); overflow: auto; }
  aside h2 { font-size: 14px; margin: 14px 0 8px; display: flex; align-items: center; gap: 6px; }
  aside h2:first-child { margin-top: 0; }

  /* 入力タブ */
  .inputs { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .inputs label { display: flex; justify-content: space-between; align-items: center; font-weight: 600; margin-bottom: 6px; }
  .inputs textarea { width: 100%; height: calc(100vh - 190px); resize: vertical; padding: 12px; font: inherit;
                     border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
  .hint { color: var(--muted); font-size: 13px; margin: 10px 0 0; }
  .drive { display: grid; grid-template-columns: auto 1fr 1fr auto; gap: 8px; align-items: center;
           background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin-bottom: 14px; }
  .drive input { font: inherit; font-size: 13px; padding: 5px 8px; border: 1px solid var(--line); border-radius: 6px; min-width: 0; }
  .drive .note { grid-column: 1 / -1; }
  .btn:disabled { opacity: .5; cursor: wait; }
  header { flex-wrap: nowrap; white-space: nowrap; }
  #driveMsg { flex: 0 1 380px; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .drive .note.warn { color: var(--warn); }

  /* 比較タブ */
  table { width: 100%; border-collapse: separate; border-spacing: 0; background: var(--panel);
          border: 1px solid var(--line); border-radius: 8px; overflow: hidden; table-layout: fixed; }
  th { background: #fafaf8; text-align: left; font-size: 13px; color: var(--muted);
       padding: 8px 12px; border-bottom: 1px solid var(--line); z-index: 2; }
  td { vertical-align: top; padding: 12px; border-bottom: 1px solid var(--line); word-wrap: break-word; }
  tr:last-child td { border-bottom: none; }
  td.num { width: 46px; color: var(--muted); font-size: 12px; text-align: right; padding-right: 4px; }
  td.chk { width: 44px; text-align: center; }
  th.num { width: 46px; } th.chk { width: 44px; }
  tbody tr:hover td { background: var(--hl); }
  tr.done td { opacity: .55; }
  tr.done td.chk { opacity: 1; }
  /* 前の版との差分 */
  .difftools { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; font-size: 13px; }
  .difftools label { display: flex; align-items: center; gap: 4px; cursor: pointer; }
  ins { background: #d8f3dc; text-decoration: none; border-radius: 2px; }
  del { background: #fde2e2; color: #a33a3a; text-decoration: line-through; border-radius: 2px; }
  del + ins { margin-left: 2px; }
  del.para { display: block; margin-bottom: 6px; padding: 2px 4px; }
  tr.changed td.num { box-shadow: inset 3px 0 0 #e0a800; }
  tr.filtered { display: none; }
  td.en { font-family: Charter, "Iowan Old Style", Georgia, serif; font-size: 16px; }
  .w { cursor: pointer; border-radius: 3px; }
  .w.active { background: var(--accent-soft); outline: 1px solid var(--accent); }
  .w:hover { background: var(--accent-soft); }
  .w.saved { border-bottom: 2px solid var(--accent); cursor: pointer; }
  .w.flash { background: #ffe08a; }
  .empty { color: var(--muted); padding: 40px; text-align: center; }

  /* 辞書ポップアップ */
  #pop { position: absolute; z-index: 50; width: 420px; max-height: 60vh; overflow: auto; background: var(--panel);
         border: 1px solid var(--line); border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,.15); padding: 14px 16px; display: none; }
  #pop .head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; }
  #pop .head b { font-size: 18px; }
  #pop .phon { color: var(--muted); font-size: 13px; }
  #pop .close { margin-left: auto; cursor: pointer; border: none; background: none; font-size: 18px; color: var(--muted); }
  #pop h3 { font-size: 12px; color: var(--muted); margin: 10px 0 4px; text-transform: uppercase; letter-spacing: .04em; }
  #pop ol { margin: 0; padding-left: 20px; font-size: 14px; }
  #pop li { margin: 2px 0; }
  #pop .pos { display: inline-block; font-size: 11px; background: var(--accent-soft); color: var(--accent); border-radius: 4px; padding: 0 6px; margin: 6px 0 2px; }
  #pop .ex { color: var(--muted); font-style: italic; font-size: 13px; }
  #pop .links { display: flex; gap: 10px; flex-wrap: wrap; font-size: 13px; margin-top: 10px; }
  #pop .links a { color: var(--accent); }
  #pop .save { display: flex; gap: 6px; margin-top: 12px; }
  #pop .save input { flex: 1; font: inherit; font-size: 13px; padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px; }
  #pop .from { font-size: 12px; color: var(--muted); }

  /* 単語帳 */
  #wordlist { list-style: none; margin: 0; padding: 0; }
  #wordlist li { padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
  #wordlist .row1 { display: flex; align-items: center; gap: 6px; }
  #wordlist .word { font-weight: 600; cursor: pointer; color: var(--accent); }
  #wordlist .cnt { color: var(--muted); font-size: 11px; }
  #wordlist .del { margin-left: auto; border: none; background: none; color: var(--muted); cursor: pointer; }
  #wordlist input { width: 100%; margin-top: 4px; font: inherit; font-size: 13px; padding: 3px 6px; border: 1px solid transparent; border-radius: 4px; background: #fafaf8; }
  #wordlist input:focus { border-color: var(--line); background: #fff; outline: none; }
  .progress { height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; margin: 4px 0 2px; }
  .progress div { height: 100%; background: var(--ok); width: 0; transition: width .2s; }
  .small { font-size: 12px; color: var(--muted); }
  .hidden { display: none !important; }
</style>
</head>
<body>
<header>
  <h1>翻訳レビュー</h1>
  <div class="tabs"><button data-tab="input">入力</button><button data-tab="compare">比較</button></div>
  <label class="small">分割:
    <select id="split">
      <option value="para">空行で段落分割</option>
      <option value="line">1行ずつ</option>
    </select>
  </label>
  <span id="status" class="status"></span>
  <span class="spacer"></span>
  <span id="driveMsg" class="status"></span>
  <button class="btn hidden" id="driveReload" title="入力欄の URL から両方の原稿を取り直します">↻ Drive から再読み込み</button>
  <input id="search" type="search" placeholder="英単語を検索…" size="16">
</header>
<main>
  <div id="content">
    <section id="tab-input">
      <div class="drive">
        <b class="small">Google ドキュメント</b>
        <input id="urlJa" placeholder="日本語原稿の URL (https://docs.google.com/document/d/…)">
        <input id="urlEn" placeholder="英訳の URL">
        <button class="btn primary" id="driveLoad">Drive から読み込む</button>
        <div class="note small" id="driveNote"></div>
      </div>
      <div class="inputs">
        <div>
          <label>日本語原稿 <span><input type="file" id="fileJa" accept=".txt,.md,.tex" hidden><button class="btn" onclick="fileJa.click()">ファイルを開く</button></span></label>
          <textarea id="ja" placeholder="日本語の原稿を貼り付け（段落は空行で区切る）"></textarea>
        </div>
        <div>
          <label>英訳 <span><input type="file" id="fileEn" accept=".txt,.md,.tex" hidden><button class="btn" onclick="fileEn.click()">ファイルを開く</button></span></label>
          <textarea id="en" placeholder="Paste the English translation here (separate paragraphs with blank lines)"></textarea>
        </div>
      </div>
      <p class="hint">入力内容はブラウザに自動保存されます。段落数を揃えておくと左右が対応して表示されます。
        <button class="btn primary" id="toCompare" style="margin-left:8px">比較表示へ →</button></p>
    </section>
    <section id="tab-compare" class="hidden">
      <div class="difftools hidden" id="difftools">
        <label><input type="checkbox" id="showDiff"> 前の版との差分を表示</label>
        <label><input type="checkbox" id="onlyChanged"> 変更のある段落だけ表示</label>
        <span id="diffInfo" class="small"></span>
        <button class="btn" id="resetDiff" title="今の版を比較の基準にして、差分の表示を消します">差分をリセット</button>
      </div>
      <table>
        <thead><tr><th class="num">#</th><th>日本語</th><th>English</th><th class="chk" title="確認済み">✓</th></tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </section>
  </div>
  <aside>
    <h2>使い方</h2>
    <div class="small">英文の単語を<b>クリック</b>すると辞書が開き、もう一度クリックすると閉じます。熟語は<b>ドラッグ選択</b>で引けます。下線付きの単語は単語帳に登録済みです。</div>
    <h2>レビュー進捗</h2>
    <div class="progress"><div id="prog"></div></div>
    <div class="small" id="progText"></div>
    <h2>単語帳 <span class="spacer"></span><button class="btn" id="exportCsv">CSV 出力</button></h2>
    <div class="small" style="margin-bottom:6px">「論文での訳」は自由に編集できます。</div>
    <ul id="wordlist"></ul>
  </aside>
</main>

<div id="pop"></div>

<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem('tr_' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('tr_' + k, JSON.stringify(v)); } catch {} },
};

let words = store.get('words', {});       // key(lower) -> {word, note, ej, added}
let done = store.get('done', {});         // hash -> true
let currentWord = null;

// ---------- 入力 ----------
$('#ja').value = store.get('ja', '');
$('#en').value = store.get('en', '');
$('#split').value = store.get('split', 'para');
for (const id of ['ja', 'en']) {
  $('#' + id).addEventListener('input', e => store.set(id, e.target.value));
}
$('#split').addEventListener('change', e => { store.set('split', e.target.value); render(); });
for (const [inp, ta] of [['#fileJa', '#ja'], ['#fileEn', '#en']]) {
  $(inp).addEventListener('change', async e => {
    const f = e.target.files[0]; if (!f) return;
    const text = await f.text();
    setBase(ta.slice(1), $(ta).value, text);
    $(ta).value = text; store.set(ta.slice(1), text); e.target.value = '';
  });
}

// ---------- タブ ----------
function showTab(name) {
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $('#tab-input').classList.toggle('hidden', name !== 'input');
  $('#tab-compare').classList.toggle('hidden', name !== 'compare');
  store.set('tab', name);
  if (name === 'compare') render();
  hidePop();
}
document.querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
$('#toCompare').addEventListener('click', () => showTab('compare'));

// ---------- 比較表示 ----------
function splitText(t) {
  t = t.replace(/\r\n?/g, '\n').trim();
  if (!t) return [];
  return $('#split').value === 'line'
    ? t.split('\n').filter(s => s.trim())
    : t.split(/\n\s*\n/).map(s => s.trim()).filter(Boolean);
}
function hash(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return 'h' + h; }

function wrapWords(text) {
  // 英単語を span で囲む（ハイフン・アポストロフィ込み）。
  // 先にエスケープすると &amp; などの中の文字列まで単語扱いされるので、分割してから個別にエスケープする
  const re = /[A-Za-z][A-Za-z'’\-]*[A-Za-z]|[A-Za-z]/g;
  let html = '', last = 0, m;
  while ((m = re.exec(text))) {
    const w = m[0], k = w.toLowerCase().replace(/[’']s$/, '');
    html += plain(text.slice(last, m.index))
      + `<span class="w${words[k] ? ' saved' : ''}" data-k="${esc(k)}">${esc(w)}</span>`;
    last = m.index + w.length;
  }
  return html + plain(text.slice(last));
}

const plain = (t) => esc(t).replace(/\n/g, '<br>');

function render() {
  const ja = splitText($('#ja').value), en = splitText($('#en').value);
  const n = Math.max(ja.length, en.length);
  const tb = $('#rows');
  renderDiffTools();
  if (!n) { tb.innerHTML = '<tr><td colspan="4" class="empty">「入力」タブで原稿と英訳を貼り付けてください</td></tr>'; updateStatus(0, 0, 0); return; }
  const useDiff = base && showDiff;
  const aj = useDiff && base.ja != null ? alignParas(splitText(base.ja), ja) : null;
  const ae = useDiff && base.en != null ? alignParas(splitText(base.en), en) : null;
  let html = '', nChanged = 0;
  for (let i = 0; i < n; i++) {
    const j = ja[i] ?? '', e = en[i] ?? '';
    const h = hash(j + '\u0000' + e);
    const cj = diffCell(aj, i, j, plain), ce = diffCell(ae, i, e, wrapWords);
    const changed = cj.changed || ce.changed;
    if (changed) nChanged++;
    const cls = [done[h] ? 'done' : '', changed ? 'changed' : '', useDiff && onlyChanged && !changed ? 'filtered' : ''].join(' ');
    html += `<tr data-h="${h}" class="${cls}">
      <td class="num">${i + 1}</td>
      <td class="ja">${cj.html}</td>
      <td class="en">${ce.html}</td>
      <td class="chk"><input type="checkbox" ${done[h] ? 'checked' : ''}></td></tr>`;
  }
  tb.innerHTML = html;
  if (useDiff) $('#diffInfo').textContent = `${base.time} の版から ${nChanged} 段落に変更があります`;
  updateStatus(ja.length, en.length, n);
}

// ---------- 前の版との差分 ----------
// 読み込みで内容が変わったとき、変わる直前の版を「比較の基準」として保存する
let base = store.get('base', null);   // {ja, en, time}
let showDiff = store.get('showDiff', true);
let onlyChanged = false;

function setBase(key, oldText, newText) {
  if (oldText === newText || !oldText.trim()) return;
  base = base || { ja: null, en: null };
  base[key] = oldText;
  base.time = new Date().toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  store.set('base', base);
}
function renderDiffTools() {
  $('#difftools').classList.toggle('hidden', !base);
  $('#showDiff').checked = showDiff;
  $('#onlyChanged').checked = onlyChanged;
  $('#onlyChanged').disabled = !showDiff;
  if (!showDiff) $('#diffInfo').textContent = '';
}
$('#showDiff').addEventListener('change', e => { showDiff = e.target.checked; store.set('showDiff', showDiff); render(); });
$('#onlyChanged').addEventListener('change', e => { onlyChanged = e.target.checked; render(); });
$('#resetDiff').addEventListener('click', () => { base = null; onlyChanged = false; store.set('base', null); render(); });

// 最長共通部分列で一致した要素の添字の組 [[i, j], ...] を返す
function lcsPairs(a, b) {
  const n = a.length, m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push([i, j]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
    else j++;
  }
  return out;
}

// 段落の対応付け: 新しい各段落が same / mod (旧段落あり) / new のどれか。
// 消えた段落は、その直後の段落 (末尾なら最後の段落) に付けて表示する。
function alignParas(olds, news) {
  const res = news.map(() => ({ status: 'new', old: null, dels: [] }));
  const trailing = [];
  let pi = 0, pj = 0;
  for (const [i, j] of lcsPairs(olds, news).concat([[olds.length, news.length]])) {
    const gapOld = olds.slice(pi, i), gapNew = j - pj;
    for (let k = 0; k < Math.min(gapNew, gapOld.length); k++) { res[pj + k].status = 'mod'; res[pj + k].old = gapOld[k]; }
    const removed = gapOld.slice(gapNew);
    if (removed.length) (j < news.length ? res[j].dels : trailing).push(...removed);
    if (j < news.length) res[j].status = 'same';
    pi = i + 1; pj = j + 1;
  }
  return { res, trailing };
}

// 英語は単語、日本語は文字単位で差分を取り、<ins>/<del> で囲んだ HTML を返す
const TOKEN_RE = /[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|\s+|[^]/gu;
function diffHtml(oldT, newT, fmt) {
  const a = oldT.match(TOKEN_RE) || [], b = newT.match(TOKEN_RE) || [];
  if (a.length * b.length > 4e6) return `<ins>${fmt(newT)}</ins>`;  // 極端に長い段落は差分を省略
  let html = '', pi = 0, pj = 0, same = '';
  const flush = () => { if (same) { html += fmt(same); same = ''; } };
  for (const [i, j] of lcsPairs(a, b).concat([[a.length, b.length]])) {
    if (i > pi || j > pj) flush();
    if (i > pi) html += `<del>${plain(a.slice(pi, i).join(''))}</del>`;
    if (j > pj) html += `<ins>${fmt(b.slice(pj, j).join(''))}</ins>`;
    if (j < b.length) same += b[j];
    pi = i + 1; pj = j + 1;
  }
  flush();
  return html;
}

function diffCell(al, i, text, fmt) {
  if (!al || i >= al.res.length) return { html: fmt(text), changed: false };
  const r = al.res[i];
  const dels = r.dels.concat(i === al.res.length - 1 ? al.trailing : []);
  let html = dels.map(d => `<del class="para">${plain(d)}</del>`).join('');
  if (r.status === 'mod') html += diffHtml(r.old, text, fmt);
  else if (r.status === 'new') html += `<ins>${fmt(text)}</ins>`;
  else html += fmt(text);
  return { html, changed: r.status !== 'same' || dels.length > 0 };
}
function updateStatus(nj, ne, n) {
  const s = $('#status');
  if (nj !== ne) { s.textContent = `⚠ 段落数が一致しません（日本語 ${nj} / 英語 ${ne}）`; s.className = 'status warn'; }
  else { s.textContent = n ? `${n} 段落` : ''; s.className = 'status'; }
  const rows = [...document.querySelectorAll('#rows tr[data-h]')];
  const c = rows.filter(r => done[r.dataset.h]).length;
  $('#prog').style.width = rows.length ? (100 * c / rows.length) + '%' : '0';
  $('#progText').textContent = rows.length ? `${c} / ${rows.length} 段落を確認済み` : '—';
}
$('#rows').addEventListener('change', e => {
  if (e.target.type !== 'checkbox') return;
  const tr = e.target.closest('tr'); const h = tr.dataset.h;
  if (e.target.checked) done[h] = true; else delete done[h];
  tr.classList.toggle('done', e.target.checked);
  store.set('done', done);
  updateStatus(...countParas());
});
function countParas() {
  const nj = splitText($('#ja').value).length, ne = splitText($('#en').value).length;
  return [nj, ne, Math.max(nj, ne)];
}

// ---------- 辞書 ----------
// 単語クリックで辞書を開き、同じ単語をもう一度クリックで閉じる
let popAnchor = null;
$('#rows').addEventListener('click', e => {
  const w = e.target.closest('.w');
  if (!w || e.detail > 1 || !window.getSelection().isCollapsed) return;
  if (popAnchor === w && $('#pop').style.display === 'block') hidePop();
  else lookup(w.textContent, w);
});
$('#rows').addEventListener('mouseup', e => {
  if (!e.target.closest('td.en')) return;
  setTimeout(() => {
    const sel = window.getSelection(); const t = sel.toString().trim();
    if (!t || sel.isCollapsed || e.detail > 1) return;
    if (!/^[A-Za-z][A-Za-z'’\- ]*$/.test(t) || t.split(/\s+/).length > 6) return;
    const r = sel.getRangeAt(0).getBoundingClientRect();
    lookup(t, null, r);
  }, 0);
});
$('#search').addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.value.trim()) lookup(e.target.value.trim(), e.target);
});

async function lookup(q, anchor, rect) {
  const pop = $('#pop');
  if (popAnchor) popAnchor.classList.remove('active');
  popAnchor = anchor && anchor.classList.contains('w') ? anchor : null;
  if (popAnchor) popAnchor.classList.add('active');
  placePop(anchor ? anchor.getBoundingClientRect() : rect);
  pop.innerHTML = `<div class="head"><b>${esc(q)}</b><button class="close" onclick="hidePop()">×</button></div><div class="small">検索中…</div>`;
  pop.style.display = 'block';
  try {
    const r = await fetch('/api/lookup?q=' + encodeURIComponent(q));
    const d = await r.json();
    currentWord = d;
    renderPop(d);
  } catch (err) {
    pop.innerHTML += `<div class="small">取得に失敗しました: ${esc(err)}</div>`;
  }
}

function renderPop(d) {
  const key = d.query.toLowerCase().replace(/[’']s$/, '');
  const saved = words[key];
  let h = `<div class="head"><b>${esc(d.query)}</b>`;
  if (d.headword && d.headword !== d.query.toLowerCase()) h += `<span class="from">→ ${esc(d.headword)}</span>`;
  if (d.en && d.en.phonetic) h += `<span class="phon">${esc(d.en.phonetic)}</span>`;
  h += `<button class="close" onclick="hidePop()">×</button></div>`;

  h += `<h3>英和 (EJDict)</h3>`;
  if (d.ej.length) {
    for (const ent of d.ej) h += `<ol>${ent.meanings.slice(0, 12).map(m => `<li>${esc(m).replace(/『|』/g, '')}</li>`).join('')}</ol>`;
  } else h += `<div class="small">${d.dict_ready ? '見つかりませんでした' : '辞書を読み込み中です。少し待ってから再検索してください'}</div>`;

  h += `<h3>英英 (Free Dictionary)</h3>`;
  if (d.en && d.en.meanings.length) {
    for (const m of d.en.meanings) {
      h += `<span class="pos">${esc(m.pos)}</span><ol>` + m.defs.map(x =>
        `<li>${esc(x.def)}${x.example ? `<div class="ex">“${esc(x.example)}”</div>` : ''}</li>`).join('') + '</ol>';
      if (m.synonyms.length) h += `<div class="small">類義語: ${m.synonyms.map(esc).join(', ')}</div>`;
    }
  } else h += `<div class="small">${d.en && d.en.error ? '取得エラー: ' + esc(d.en.error) : '見つかりませんでした'}</div>`;

  const qq = encodeURIComponent(d.query);
  h += `<div class="links">
    <a href="https://ejje.weblio.jp/content/${qq}" target="_blank" rel="noopener noreferrer">Weblio</a>
    <a href="https://eow.alc.co.jp/search?q=${qq}" target="_blank" rel="noopener noreferrer">英辞郎</a>
    <a href="https://dictionary.cambridge.org/dictionary/english/${qq}" target="_blank" rel="noopener noreferrer">Cambridge</a>
    <a href="https://www.google.com/search?q=%22${qq}%22+site%3Ascholar.google.com" target="_blank" rel="noopener noreferrer">用例検索</a></div>`;

  const firstJa = d.ej.length ? d.ej[0].meanings[0].replace(/『|』/g, '') : '';
  h += `<div class="save"><input id="noteIn" placeholder="論文での訳（例: 頑健な）" value="${esc(saved ? saved.note : '')}">
    <button class="btn primary" id="saveBtn">${saved ? '更新' : '単語帳に追加'}</button></div>`;
  $('#pop').innerHTML = h;
  $('#saveBtn').onclick = () => {
    words[key] = { word: d.query, note: $('#noteIn').value.trim() || firstJa, ej: firstJa, added: saved ? saved.added : Date.now() };
    store.set('words', words);
    renderWordlist(); markSaved(); hidePop();
  };
  $('#noteIn').addEventListener('keydown', e => { if (e.key === 'Enter') $('#saveBtn').click(); });
}

function placePop(r) {
  const pop = $('#pop');
  if (!r) r = { left: 100, bottom: 100, top: 100 };
  const w = 420;
  let left = Math.min(r.left + window.scrollX, window.scrollX + document.documentElement.clientWidth - w - 12);
  pop.style.left = Math.max(8, left) + 'px';
  pop.style.top = (r.bottom + window.scrollY + 6) + 'px';
}
function hidePop() {
  $('#pop').style.display = 'none';
  if (popAnchor) popAnchor.classList.remove('active');
  popAnchor = null;
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') hidePop(); });
document.addEventListener('mousedown', e => {
  if (!e.target.closest('#pop') && !e.target.closest('.w') && !e.target.closest('#search')) hidePop();
});

// ---------- 単語帳 ----------
function occurrences(k) { return document.querySelectorAll(`#rows .w[data-k="${CSS.escape(k)}"]`).length; }
function markSaved() {
  document.querySelectorAll('#rows .w').forEach(s => s.classList.toggle('saved', !!words[s.dataset.k]));
}
function renderWordlist() {
  const list = Object.entries(words).sort((a, b) => a[0].localeCompare(b[0]));
  $('#wordlist').innerHTML = list.length ? list.map(([k, v]) => `
    <li data-k="${esc(k)}"><div class="row1"><span class="word">${esc(v.word)}</span>
      <span class="cnt">${occurrences(k) ? '本文中 ' + occurrences(k) + ' 回' : ''}</span>
      <button class="del" title="削除">✕</button></div>
      <input value="${esc(v.note)}" placeholder="論文での訳"></li>`).join('')
    : '<li class="small">まだ登録がありません</li>';
}
$('#wordlist').addEventListener('click', e => {
  const li = e.target.closest('li[data-k]'); if (!li) return;
  const k = li.dataset.k;
  if (e.target.classList.contains('del')) {
    delete words[k]; store.set('words', words); renderWordlist(); markSaved();
  } else if (e.target.classList.contains('word')) {
    const hits = [...document.querySelectorAll(`#rows .w[data-k="${CSS.escape(k)}"]`)];
    if (hits.length) {
      showTabIfNeeded();
      hits[0].scrollIntoView({ block: 'center', behavior: 'smooth' });
      hits.forEach(s => { s.classList.add('flash'); setTimeout(() => s.classList.remove('flash'), 1500); });
    } else lookup(words[k].word, e.target);
  }
});
$('#wordlist').addEventListener('change', e => {
  const li = e.target.closest('li[data-k]');
  if (li && e.target.tagName === 'INPUT') { words[li.dataset.k].note = e.target.value; store.set('words', words); }
});
function showTabIfNeeded() { if ($('#tab-compare').classList.contains('hidden')) showTab('compare'); }
$('#exportCsv').addEventListener('click', () => {
  const q = s => '"' + String(s ?? '').replace(/"/g, '""') + '"';
  const rows = [['word', 'translation_in_paper', 'ejdict_first_meaning', 'occurrences']]
    .concat(Object.entries(words).sort((a, b) => a[0].localeCompare(b[0])).map(([k, v]) => [v.word, v.note, v.ej, occurrences(k)]));
  const csv = '﻿' + rows.map(r => r.map(q).join(',')).join('\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  a.download = 'wordlist.csv'; a.click();
});

// ---------- Google Drive ----------
$('#urlJa').value = store.get('urlJa', '');
$('#urlEn').value = store.get('urlEn', '');
function updateReloadBtn() {
  $('#driveReload').classList.toggle('hidden', !($('#urlJa').value.trim() || $('#urlEn').value.trim()));
}
for (const id of ['urlJa', 'urlEn']) {
  $('#' + id).addEventListener('input', e => { store.set(id, e.target.value.trim()); updateReloadBtn(); });
}
function driveMsg(text, warn) {
  const m = $('#driveMsg'); m.textContent = text; m.title = text; m.className = 'status' + (warn ? ' warn' : '');
  if (warn) { $('#driveNote').textContent = '⚠ ' + text; $('#driveNote').className = 'note small warn'; }
}

async function loadDrive() {
  const ja = $('#urlJa').value.trim(), en = $('#urlEn').value.trim();
  if (!ja && !en) { showTab('input'); driveMsg('Google ドキュメントの URL を入力してください', true); return; }
  const btns = [$('#driveLoad'), $('#driveReload')];
  btns.forEach(b => b.disabled = true);
  driveMsg('Drive から取得中…');
  try {
    const d = await (await fetch('/api/drive/fetch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ja, en }),
    })).json();
    if (d.error) { driveMsg(d.error, true); return; }
    if (d.auth_url) { waitForLogin(d.auth_url); return; }
    const ok = [], ng = [];
    for (const [k, label] of [['ja', '日本語'], ['en', '英語']]) {
      const x = d[k]; if (!x) continue;
      if (x.error) { ng.push(`${label}: ${x.error}`); continue; }
      setBase(k, $('#' + k).value, x.text);
      $('#' + k).value = x.text; store.set(k, x.text);
      ok.push(`${label}「${x.name}」`);
    }
    const time = new Date().toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
    driveMsg(ng.length ? ng.join(' / ') : `${ok.join('・')} を読み込みました (${time})`, ng.length > 0);
    if (!$('#tab-compare').classList.contains('hidden')) { render(); markSaved(); renderWordlist(); }
    if (!ng.length) checkDrive();
  } catch (err) {
    driveMsg('取得に失敗しました: ' + err, true);
  } finally {
    btns.forEach(b => b.disabled = false);
  }
}
// ログインが必要なとき: リンクを出し、token.json が更新されたら自動で読み込み直す
let loginTimer = null;
async function waitForLogin(url) {
  showTab('input');
  driveMsg('Google へのログインが必要です。入力タブのリンクからログインしてください');
  const note = $('#driveNote');
  note.className = 'note small';
  note.innerHTML = `<a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Google にログインする（新しいタブ）</a>
    　ログインと許可が終わると、自動で読み込みます。`;
  const before = (await (await fetch('/api/drive/status')).json()).token_time;
  clearInterval(loginTimer);
  const started = Date.now();
  loginTimer = setInterval(async () => {
    if (Date.now() - started > 10 * 60 * 1000) { clearInterval(loginTimer); driveMsg('ログインを待つ時間が過ぎました。もう一度「Drive から読み込む」を押してください', true); return; }
    try {
      const s = await (await fetch('/api/drive/status')).json();
      if (s.token_time && s.token_time !== before) { clearInterval(loginTimer); loadDrive(); }
    } catch {}
  }, 2000);
}
$('#driveLoad').addEventListener('click', loadDrive);
$('#driveReload').addEventListener('click', loadDrive);

async function checkDrive() {
  try {
    const s = await (await fetch('/api/drive/status')).json();
    $('#driveNote').className = 'note small' + (s.libs && s.credentials ? '' : ' warn');
    $('#driveNote').innerHTML = !s.libs
      ? '⚠ Drive 機能を使うには <code>pip install google-auth-oauthlib</code> を実行してサーバーを再起動してください。'
      : !s.credentials
      ? '⚠ <code>credentials.json</code> がありません。README の「Google ドキュメントから読み込む」の手順で用意してください。'
      : s.authorized ? 'Google アカウントで認証済みです。段落は Google ドキュメントの段落単位で分割されます。'
      : '初回の読み込み時に Google へのログイン用リンクが表示されます。';
  } catch {}
}

showTab(store.get('tab', 'input'));
renderWordlist();
updateReloadBtn();
checkDrive();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="論文翻訳レビュー用ローカルサーバー")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="起動時にブラウザを開かない")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    # 指定ポートが使用中なら次のポートを試す
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit(f"ポート {args.port}〜{args.port + 19} がすべて使用中です。--port で別の番号を指定してください。")
    # ループバックで待ち受けるときは、自分宛ての Host ヘッダー以外を拒否する (None は検査しない)
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    server.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"} if loopback else None

    threading.Thread(target=EJ.load, daemon=True).start()
    url = f"http://{args.host}:{port}/"
    print(f"翻訳レビューを起動しました: {url}  (Ctrl+C で終了)", flush=True)
    if not loopback:
        print("警告: 他の PC からもアクセスできる設定です。Google にログイン済みの場合、"
              "アクセスした人があなたのアカウントで Drive のドキュメントを読めます。", file=sys.stderr)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")


if __name__ == "__main__":
    main()
