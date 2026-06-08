#!/usr/bin/env python3
"""Source Text — local study web app over the full canonical corpus.

A small, dependency-free (stdlib http.server) local server that reads
data/build/source-text.translations.sqlite directly. Brings the prototype's
click-a-word study experience to the WHOLE Bible:

  - pick a book + chapter; read every verse across the 11 translations
    (public-domain shown plainly; the 4 copyrighted ones tagged "internal");
  - each verse shows its original-language interlinear (Greek LTR / Hebrew RTL
    with prefix/root/suffix morphemes), every word clickable;
  - click a Greek/Hebrew word -> side panel with its Strong's lexicon definition
    and every occurrence across the whole corpus; click an occurrence -> jump
    there in the reader.

Read-only (opens the DB with mode=ro); no writes, no runtime AI, local only.

Usage:
    python3 scripts/view/app.py [--port 8780] [--open]
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PROJECT = Path(__file__).resolve().parents[2]
# DB path is env-overridable so the deployed instance can read a downloaded copy.
DB = Path(os.environ.get("SOURCE_TEXT_DB", str(PROJECT / "data" / "build" / "source-text.translations.sqlite")))
# HTTP Basic Auth gate. Two ways to configure (both optional; unset both = open locally):
#   AUTH_USER / AUTH_PASS  - a single shared login (back-compat with the first deploy).
#   AUTH_USERS             - a per-tester roster, either JSON {"user": "pass", ...} or
#                            "user:pass,user:pass". Supersedes/extends the single pair so
#                            each tester gets a revocable login and the request is
#                            attributable (used by the feedback slice later).
AUTH_USER = os.environ.get("AUTH_USER")
AUTH_PASS = os.environ.get("AUTH_PASS")


def _load_auth_users() -> dict:
    raw = (os.environ.get("AUTH_USERS") or "").strip()
    users: dict[str, str] = {}
    if raw.startswith("{"):
        try:
            users = {str(k): str(v) for k, v in json.loads(raw).items()}
        except Exception:
            users = {}
    elif raw:
        for pair in raw.split(","):
            u, sep, pw = pair.strip().partition(":")
            if sep and u:
                users[u] = pw
    if AUTH_USER and AUTH_PASS:
        users.setdefault(AUTH_USER, AUTH_PASS)
    return users


AUTH_USERS = _load_auth_users()

# Feedback capture (beta): append-only JSONL, kept separate from the read-only
# corpus DB. On Render set FEEDBACK_PATH=/var/data/feedback.jsonl (persistent disk);
# locally it defaults under data/. FEEDBACK_ADMIN may read it in-app.
FEEDBACK_PATH = Path(os.environ.get("FEEDBACK_PATH", str(PROJECT / "data" / "feedback.jsonl")))
FEEDBACK_ADMIN = os.environ.get("FEEDBACK_ADMIN", "noah")
_fb_lock = threading.Lock()

# Display order: formal -> amplified -> dynamic -> paraphrase, then by tradition.
TRANS_ORDER = ["NASB", "NKJV", "KJV", "ASV", "YLT", "AMP", "NIV", "NLT", "GNT", "MSG",
               "TPT", "DRC", "CPDV", "JPS", "BRENTON"]
# Tradition grouping for the translation picker (the project's 4-tradition framing).
TRADITION = {
    "NASB": "Protestant", "NKJV": "Protestant", "KJV": "Protestant", "ASV": "Protestant",
    "YLT": "Protestant", "AMP": "Protestant", "NIV": "Protestant", "NLT": "Protestant",
    "GNT": "Protestant", "MSG": "Protestant", "TPT": "Protestant",
    "DRC": "Catholic", "CPDV": "Catholic", "JPS": "Jewish", "BRENTON": "Orthodox",
}
# Display-only short labels where the internal code is long/awkward. The DB code
# stays the stable join key (provenance, psalm remap); only the UI label changes.
DISPLAY_ABBR = {"BRENTON": "LXX"}
OCC_CAP = 120

_local = threading.local()


def con() -> sqlite3.Connection:
    c = getattr(_local, "con", None)
    if c is None:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
        c.row_factory = sqlite3.Row
        _local.con = c
    return c


def base_strong(value: str) -> str:
    value = (value or "").strip().split("_", 1)[0]
    m = re.match(r"^([GH])(\d+)([A-Za-z]*)$", value)
    return f"{m.group(1)}{int(m.group(2)):04d}" if m else value


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #

def q_books() -> list[dict]:
    c = con()
    # chapter count per present book from the verse layer
    chap = {r["book_code"]: r["mx"] for r in c.execute(
        "SELECT book_code, MAX(chapter) mx FROM translation_verse GROUP BY book_code")}
    out = []
    for r in c.execute("SELECT canonical_name, osis_code, order_index, testament FROM book ORDER BY order_index"):
        if r["osis_code"] in chap:
            out.append({"name": r["canonical_name"], "osis": r["osis_code"],
                        "testament": r["testament"], "chapters": chap[r["osis_code"]]})
    return out


def q_translations() -> list[dict]:
    """Registry for the translation picker: code, name, internal flag, tradition, and
    the set of book osis codes each translation actually covers. The coverage set lets
    the UI show a graceful 'X does not cover {book}' note for partial translations
    (TPT lacks 15 OT books) rather than a silent gap."""
    c = con()
    cover: dict[str, list] = {}
    for r in c.execute("SELECT t.code, tv.book_code FROM translation_verse tv "
                       "JOIN translation t ON t.id=tv.translation_id GROUP BY t.code, tv.book_code"):
        cover.setdefault(r["code"], []).append(r["book_code"])
    order = {code: i for i, code in enumerate(TRANS_ORDER)}
    out = []
    for r in c.execute("SELECT code, name, rights_status rs FROM translation"):
        out.append({"code": r["code"], "abbr": DISPLAY_ABBR.get(r["code"], r["code"]),
                    "name": r["name"], "internal": r["rs"] != "public-domain",
                    "tradition": TRADITION.get(r["code"], "Other"),
                    "books": sorted(cover.get(r["code"], []))})
    out.sort(key=lambda x: order.get(x["code"], 99))
    return out


def _chapter_pus(c, osis: str, ch: int):
    rows = c.execute(
        "SELECT passage_unit_id pu, MAX(is_title) is_title, MIN(verse) verse, MAX(verse_end) vend "
        "FROM translation_verse WHERE book_code=? AND chapter=? GROUP BY passage_unit_id "
        "ORDER BY MAX(is_title) DESC, MIN(verse)", (osis, ch)).fetchall()
    return rows


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def q_originals(c, pus: list[str]) -> dict:
    """passage_unit -> ordered list of interlinear tokens (Greek or Hebrew)."""
    if not pus:
        return {}
    ph = _placeholders(len(pus))
    toks = c.execute(
        f"SELECT id, passage_unit_id pu, token_index, surface_text, transliteration, direction, "
        f"source_release_id rel FROM source_surface_token WHERE passage_unit_id IN ({ph}) "
        f"ORDER BY passage_unit_id, token_index", pus).fetchall()
    # Greek token-level dStrong + gloss
    gk = {}
    for r in c.execute(
        f"SELECT t.id, li.identifier_value strong, l.gloss FROM source_surface_token t "
        f"JOIN token_lemma tl ON tl.surface_token_id=t.id "
        f"JOIN lemma_identifier li ON li.id=tl.lemma_identifier_id AND li.identifier_system='dStrong' "
        f"LEFT JOIN lemma l ON l.id=li.lemma_id "
        f"WHERE t.passage_unit_id IN ({ph})", pus):
        gk[r["id"]] = (r["strong"], r["gloss"])
    # Hebrew morphemes per token
    morphs: dict[str, list] = {}
    for r in c.execute(
        f"SELECT m.surface_token_id tid, m.morpheme_index, m.surface_text, m.role, m.gloss, "
        f"li.identifier_value strong FROM source_morpheme m "
        f"JOIN source_surface_token t ON t.id=m.surface_token_id "
        f"LEFT JOIN token_lemma tl ON tl.morpheme_id=m.id "
        f"LEFT JOIN lemma_identifier li ON li.id=tl.lemma_identifier_id "
        f"WHERE t.passage_unit_id IN ({ph}) ORDER BY m.surface_token_id, m.morpheme_index", pus):
        morphs.setdefault(r["tid"], []).append(
            {"surface": r["surface_text"], "role": r["role"], "gloss": r["gloss"] or "",
             "strong": r["strong"] or ""})
    out: dict[str, dict] = {}
    for t in toks:
        pu = t["pu"]
        lang = "hbo" if t["direction"] == "rtl" else "grc"
        entry = out.setdefault(pu, {"lang": lang, "tokens": []})
        if t["id"] in morphs:  # Hebrew
            ms = morphs[t["id"]]
            root = next((m["strong"] for m in ms if m["role"] == "root" and m["strong"]), "")
            gloss = " ".join(m["gloss"] for m in ms if m["gloss"])
            entry["tokens"].append({"surface": t["surface_text"], "translit": t["transliteration"] or "",
                                    "strong": root, "gloss": gloss, "morphemes": ms})
        else:  # Greek
            strong, gloss = gk.get(t["id"], ("", ""))
            entry["tokens"].append({"surface": t["surface_text"], "translit": t["transliteration"] or "",
                                    "strong": strong or "", "gloss": gloss or "", "morphemes": None})
    return out


_meta_cache = {}


def _trans_meta(c) -> dict:
    if not _meta_cache:
        for r in c.execute("SELECT code, name, display_allowed da, rights_status rs FROM translation"):
            _meta_cache[r["code"]] = {"name": r["name"], "display_allowed": r["da"],
                                      "internal": r["rs"] != "public-domain"}
    return _meta_cache


_divergent_cache = set()
_divergent_loaded = [False]


def _divergent_codes(c) -> set:
    if not _divergent_loaded[0]:
        _divergent_cache.update(r[0] for r in c.execute(
            "SELECT DISTINCT t.code FROM psalm_verse_map m JOIN translation t ON t.id=m.translation_id"))
        _divergent_loaded[0] = True
    return _divergent_cache


def _psalm_remap(c, pus: list[str]) -> dict:
    """{(code, english_pu): realigned_text} for the Vulgate/LXX translations,
    pulling each translation's text from the source passage_unit(s) it actually
    stores, concatenated in order (a subdivided superscription -> one title row)."""
    ph = _placeholders(len(pus))
    rows = c.execute(
        f"SELECT t.code, m.english_pu, tv.text FROM psalm_verse_map m "
        f"JOIN translation t ON t.id=m.translation_id "
        f"JOIN translation_verse tv ON tv.translation_id=m.translation_id AND tv.passage_unit_id=m.source_pu "
        f"WHERE m.english_pu IN ({ph}) ORDER BY m.ord", pus).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault((r["code"], r["english_pu"]), []).append(r["text"])
    return {k: " ".join(v) for k, v in out.items()}


def _apply_psalm_remap(c, tx: dict, pus: list[str]):
    """In-place: for the Vulgate/LXX translations whose Psalm numbering diverges,
    replace their natively-numbered rows with text realigned to English numbering via
    the TVTMS map. Where a Psalm does NOT diverge (e.g. Ps 1-8) there is no map entry,
    so the native row is already English-aligned and is kept as-is (otherwise those
    translations would silently vanish on the non-divergent Psalms)."""
    divergent = _divergent_codes(c)
    if not divergent:
        return
    remap = _psalm_remap(c, pus)  # {(code, english_pu): realigned_text}
    replaced: dict[str, set] = {}  # english_pu -> codes that have a remap replacement
    for (code, epu) in remap:
        replaced.setdefault(epu, set()).add(code)
    # drop a divergent translation's native row only where a remap will replace it
    for pu in tx:
        drop = replaced.get(pu)
        if drop:
            tx[pu] = [row for row in tx[pu] if not (row["code"] in divergent and row["code"] in drop)]
    meta = _trans_meta(c)
    for (code, english_pu), text in remap.items():
        m = meta.get(code, {"name": code, "display_allowed": 0, "internal": True})
        tx.setdefault(english_pu, []).append(
            {"code": code, "name": m["name"], "display_allowed": m["display_allowed"],
             "internal": m["internal"], "text": text})


def q_chapter(osis: str, ch: int) -> dict:
    c = con()
    pu_rows = _chapter_pus(c, osis, ch)
    pus = [r["pu"] for r in pu_rows]
    if not pus:
        return {"verses": []}
    ph = _placeholders(len(pus))
    # translations per pu
    tx: dict[str, list] = {}
    for r in c.execute(
        f"SELECT tv.passage_unit_id pu, t.code, t.name, t.display_allowed da, t.rights_status rs, tv.text, tv.verse_end vend "
        f"FROM translation_verse tv JOIN translation t ON t.id=tv.translation_id "
        f"WHERE tv.passage_unit_id IN ({ph})", pus):
        tx.setdefault(r["pu"], []).append(
            {"code": r["code"], "name": r["name"], "display_allowed": r["da"],
             "internal": r["rs"] != "public-domain", "text": r["text"], "verse_end": r["vend"]})
    if osis == "Ps":
        _apply_psalm_remap(c, tx, pus)
    originals = q_originals(c, pus)
    bookname = c.execute("SELECT canonical_name FROM book WHERE osis_code=?", (osis,)).fetchone()
    verses = []
    for r in pu_rows:
        pu = r["pu"]
        rows = tx.get(pu, [])
        order = {code: i for i, code in enumerate(TRANS_ORDER)}
        rows.sort(key=lambda x: (order.get(x["code"], 99), x["code"]))
        # The row label is the verse number only; merged paraphrase blocks (MSG/GNT/
        # TPT) carry their range as a per-translation badge, so the row stays aligned
        # to the verse spine even when one translation merges verses here.
        verses.append({
            "pu": pu, "verse": r["verse"], "is_title": bool(r["is_title"]),
            "label": "title" if r["is_title"] else f'{r["verse"]}',
            "translations": rows, "original": originals.get(pu),
        })
    return {"book": bookname["canonical_name"] if bookname else osis, "osis": osis,
            "chapter": ch, "verses": verses}


def _lex_for(c, strong: str):
    """Resolve a dStrong to its lexicon entry (exact, else any sharing the base)."""
    base = base_strong(strong)
    row = c.execute(
        "SELECT le.headword, le.transliteration, es.gloss, es.domain, le.entry_text, lx.code, lx.notes "
        "FROM lexicon_entry le JOIN lexicon lx ON lx.id=le.lexicon_id "
        "LEFT JOIN entry_sense es ON es.lexicon_entry_id=le.id "
        "WHERE le.id LIKE ? OR le.id LIKE ? ORDER BY (le.id = ?) DESC, le.id LIMIT 1",
        (f"le:%:{strong}", f"le:%:{base}%", f"le:TBESG:{strong}")).fetchone()
    if not row:
        return None
    entry = row["entry_text"] or ""
    if len(entry) > 2400:
        entry = entry[:2400].rsplit(" ", 1)[0] + " …"
    gated = (row["notes"] or "").endswith("=0")
    return {"headword": row["headword"] or "", "translit": row["transliteration"] or "",
            "gloss": row["gloss"] or "", "pos": row["domain"] or "", "lexicon": row["code"],
            "entry": format_lex_html(entry), "gated": gated}


def q_word(strong: str) -> dict:
    c = con()
    occ = c.execute(
        "SELECT pu.id pu, pu.canonical_start ref, t.surface_text surf, t.direction dir FROM token_lemma tl "
        "JOIN lemma_identifier li ON li.id=tl.lemma_identifier_id "
        "JOIN source_surface_token t ON t.id=tl.surface_token_id "
        "JOIN passage_unit pu ON pu.id=t.passage_unit_id WHERE li.identifier_value=? "
        "UNION ALL "
        "SELECT pu.id, pu.canonical_start, t.surface_text, t.direction FROM token_lemma tl "
        "JOIN lemma_identifier li ON li.id=tl.lemma_identifier_id "
        "JOIN source_morpheme m ON m.id=tl.morpheme_id "
        "JOIN source_surface_token t ON t.id=m.surface_token_id "
        "JOIN passage_unit pu ON pu.id=t.passage_unit_id WHERE li.identifier_value=?",
        (strong, strong)).fetchall()
    items = [{"pu": r["pu"], "ref": r["ref"], "surf": r["surf"]} for r in occ]
    return {"strong": strong, "def": _lex_for(c, strong), "total": len(items), "items": items[:OCC_CAP]}


def q_verse(pu: str) -> dict:
    c = con()
    meta = c.execute("SELECT canonical_start ref FROM passage_unit WHERE id=?", (pu,)).fetchone()
    txd: dict[str, list] = {pu: []}
    for r in c.execute(
            "SELECT t.code, t.name, t.display_allowed da, t.rights_status rs, tv.text FROM translation_verse tv "
            "JOIN translation t ON t.id=tv.translation_id WHERE tv.passage_unit_id=?", (pu,)):
        txd[pu].append({"code": r["code"], "name": r["name"], "display_allowed": r["da"],
                        "internal": r["rs"] != "public-domain", "text": r["text"]})
    if pu.startswith("pu:Ps."):
        _apply_psalm_remap(c, txd, [pu])
    tx = txd.get(pu, [])
    order = {code: i for i, code in enumerate(TRANS_ORDER)}
    tx.sort(key=lambda x: (order.get(x["code"], 99), x["code"]))
    orig = q_originals(c, [pu]).get(pu)
    return {"pu": pu, "ref": meta["ref"] if meta else pu, "translations": tx, "original": orig}


# --------------------------------------------------------------------------- #
# Lexicon HTML formatting (entry text is trusted local lexicon, not user input)
# --------------------------------------------------------------------------- #

def format_lex_html(s: str) -> str:
    if not s:
        return ""
    B1, B2, R1, R2, Y1, Y2, NL = "\x01", "\x02", "\x05", "\x06", "\x07", "\x08", "\x0b"
    s = re.sub(r"<ref[^>]*>(.*?)</ref>", R1 + r"\1" + R2, s, flags=re.S)
    s = re.sub(r"<re>(.*?)</re>", Y1 + r"\1" + Y2, s, flags=re.S)
    s = re.sub(r"<note>(.*?)</note>", Y1 + r"\1" + Y2, s, flags=re.S)
    s = s.replace("<b>", B1).replace("</b>", B2)
    s = re.sub(r"<BR\s*/?>", NL, s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s*__\s*", NL, s)
    s = re.sub(NL + r"\s*([IVXLC]+\.|\d+\.|\([^)]{1,5}\))", NL + B1 + r"\1" + B2 + " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = html.escape(s)
    for a, b in ((B1, "<strong>"), (B2, "</strong>"), (R1, '<span class="r">'), (R2, "</span>"),
                 (Y1, '<span class="syn">'), (Y2, "</span>"), (NL, "<br>")):
        s = s.replace(a, b)
    s = re.sub(r"(?:<br>\s*){2,}", "<br>", s).strip()
    return s[4:] if s.startswith("<br>") else s


# --------------------------------------------------------------------------- #
# Feedback (append-only JSONL on a writable path; corpus DB stays read-only)
# --------------------------------------------------------------------------- #

def record_feedback(user, data: dict) -> None:
    ch = data.get("chapter")
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user": user or "",
        "comment": (data.get("comment") or "").strip()[:4000],
        "book": (data.get("book") or "")[:32],
        "chapter": int(ch) if str(ch).isdigit() else None,
        "ref": (data.get("ref") or "")[:64],
        "pu": (data.get("pu") or "")[:64],
        "strong": (data.get("strong") or "")[:16],
        "word": (data.get("word") or "")[:64],
    }
    FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _fb_lock:
        with open(FEEDBACK_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_feedback(limit: int = 200) -> list:
    if not FEEDBACK_PATH.exists():
        return []
    out = []
    for ln in FEEDBACK_PATH.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    out.reverse()  # newest first
    return out


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _auth_user(self):
        """Return the authenticated username (str) or None to deny.

        With no roster configured the gate is off (local use) and this returns ""
        (an anonymous, allowed user). A configured roster requires valid Basic creds.
        """
        if not AUTH_USERS:
            return ""  # gate off (local use)
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return None
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
        except Exception:
            return None
        expected = AUTH_USERS.get(user)
        if expected is None:
            secrets.compare_digest(pw, pw)  # even out timing for unknown users
            return None
        return user if secrets.compare_digest(pw, expected) else None

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":   # unauthenticated liveness check (Render)
            self._send(200, b"ok", "text/plain")
            return
        if self._auth_user() is None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Source Text"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        qs = parse_qs(u.query)
        try:
            if u.path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/books":
                self._json(q_books())
            elif u.path == "/api/translations":
                self._json(q_translations())
            elif u.path == "/api/chapter":
                self._json(q_chapter(qs["book"][0], int(qs["ch"][0])))
            elif u.path == "/api/word":
                self._json(q_word(qs["strong"][0]))
            elif u.path == "/api/verse":
                self._json(q_verse(qs["pu"][0]))
            elif u.path == "/api/feedback":  # admin-only read
                if self._auth_user() != FEEDBACK_ADMIN:
                    self._send(403, json.dumps({"error": "forbidden"}).encode(), "application/json")
                else:
                    self._json({"items": read_feedback()})
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")

    def do_POST(self):
        u = urlparse(self.path)
        user = self._auth_user()
        if user is None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Source Text"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if u.path != "/api/feedback":
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 16384:
                self._send(400, json.dumps({"error": "bad size"}).encode(), "application/json")
                return
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            if not (data.get("comment") or "").strip():
                self._send(400, json.dumps({"error": "empty"}).encode(), "application/json")
                return
            record_feedback(user, data)
            self._json({"ok": True})
        except Exception as exc:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")


CSS = """
:root{--ink:#1c1c1c;--muted:#6f6f6f;--faint:#9a9a9a;--line:#e7e7e7;--bg:#fff;--rootbg:#eef2f5;--accent:#2a5d8f;--internal:#b06a00;--sel:#fff3cd;--tbscale:1}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;line-height:1.5;-webkit-font-smoothing:antialiased}
.topbar{position:sticky;top:0;z-index:8;background:rgba(255,255,255,.94);backdrop-filter:saturate(180%) blur(8px);border-bottom:1px solid var(--line);padding:10px 20px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.topbar h1{font-size:14px;font-weight:600;margin:0 14px 0 0;letter-spacing:.01em}
.topbar select,.topbar button{font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--ink);cursor:pointer}
.topbar .nav{margin-left:auto;display:flex;gap:6px;align-items:center}
.topbar .nav .pg{color:var(--faint);font-size:12px;min-width:74px;text-align:center}
.pgshort{display:none}
.wrap{max-width:880px;margin:0 auto;padding:28px 24px 160px}
.chapter-title{font-size:24px;font-weight:600;margin:6px 0 22px}
.verse{display:grid;grid-template-columns:34px 1fr;gap:8px;padding:18px 0;border-top:1px solid var(--line)}
.verse:first-of-type{border-top:none}
.verse.target{background:var(--sel);border-radius:8px;margin:0 -10px;padding:18px 10px}
.vno{font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums;padding-top:3px;font-weight:600}
.vno.title{font-size:10px;letter-spacing:.06em;text-transform:uppercase}
.interlinear{display:flex;flex-wrap:wrap;gap:5px;margin:0 0 14px}
.interlinear.rtl{direction:rtl}
.w{border:1px solid var(--line);border-radius:7px;padding:6px 8px;min-width:54px;text-align:center;background:#fff;cursor:pointer;transition:border-color .12s,box-shadow .12s}
.w:hover{border-color:var(--accent);box-shadow:0 1px 6px rgba(42,93,143,.13)}
.w .orig{font-size:18px;line-height:1.3}
.grc{font-family:"New Athena Unicode","Times New Roman",Georgia,serif}
.hbo{font-family:"SBL Hebrew","Taamey Frank CLM","Times New Roman",serif;direction:rtl;font-size:20px}
.w .tr{font-size:10px;color:var(--muted);font-style:italic;margin-top:2px}
.w .gl{font-size:12px;margin-top:2px;color:#333}
.w .meta{font-size:9px;color:var(--faint);font-family:ui-monospace,Menlo,monospace;margin-top:2px}
.heb .orig{display:flex;flex-direction:row-reverse;gap:2px;justify-content:center;flex-wrap:wrap}
.m{padding:0 1px}.m-root{background:var(--rootbg);border-radius:3px}
.tx{display:grid;grid-template-columns:74px 1fr;gap:12px;padding:6px 0}
.tx-code{font-weight:600;font-size:12px}
.tx-code .tag{display:block;font-weight:600;font-size:9px;color:var(--internal);text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
.tx-text{font-size:15px;line-height:1.55}
.tx-text.internal{color:#555}
.empty{color:var(--faint);font-size:14px;padding:40px 0}
.hint{color:var(--faint);font-size:12px;margin:0 0 20px}
.hint-seen .hint{display:none}              /* intro shows on first visit only */
.tbtn-ic{display:none;line-height:0}        /* layers glyph on the Translations button (mobile) */
.psnote{background:#fbf6ee;border:1px solid #ead9bd;border-radius:8px;padding:10px 13px;color:#7a6024;font-size:12.5px;line-height:1.5;margin:0 0 22px}
/* word panel */
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.18);opacity:0;pointer-events:none;transition:opacity .15s;z-index:9}
#scrim.open{opacity:1;pointer-events:auto}
#panel{position:fixed;top:0;right:0;height:100%;width:392px;max-width:92vw;background:#fff;border-left:1px solid var(--line);box-shadow:-8px 0 30px rgba(0,0,0,.08);transform:translateX(100%);transition:transform .18s ease;z-index:10;overflow-y:auto;padding:24px 22px 48px}
#panel.open{transform:translateX(0)}
#panel .close{position:absolute;top:14px;right:16px;border:none;background:none;font-size:22px;color:var(--faint);cursor:pointer}
#panel .pw{font-size:30px;margin:6px 0 2px}
#panel .ptr{color:var(--muted);font-style:italic;font-size:14px}
#panel .pmeta{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--faint);margin-top:8px}
#panel .pgloss{font-size:16px;margin:14px 0 0;font-weight:600}
#panel .pentry{font-size:13px;color:#333;line-height:1.55;margin:12px 0 0}
#panel .pentry strong{font-weight:600;color:var(--ink)}#panel .pentry .r{color:var(--accent)}#panel .pentry .syn{color:var(--muted);font-style:italic}
#panel .pmorph{margin:12px 0 0;font-size:13px}
#panel .pmorph div{padding:3px 0;border-top:1px solid var(--line)}
#panel h4{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin:22px 0 8px}
#panel .occ{font-size:13px;line-height:1.7}
#panel .occ .o{display:flex;justify-content:space-between;gap:10px;padding:3px 0;border-top:1px solid #f0f0f0;cursor:pointer}
#panel .occ .o:hover .oref{text-decoration:underline}
#panel .occ .oref{color:var(--accent);white-space:nowrap}
#panel .occ .osurf{font-family:"New Athena Unicode","SBL Hebrew","Times New Roman",serif;color:var(--muted)}
#panel .rights{font-size:11px;color:var(--internal);margin-top:10px;line-height:1.5}
#panel .loading{color:var(--faint);font-size:13px;margin-top:20px}
footer{color:var(--faint);font-size:12px;border-top:1px solid var(--line);padding-top:18px;margin-top:40px}
/* feedback */
.fbbtn{font:inherit;font-size:12px;padding:5px 9px;border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--muted);cursor:pointer}
.fbbtn:hover{color:var(--ink);border-color:var(--accent)}
#fb{position:fixed;right:18px;bottom:18px;width:340px;max-width:92vw;background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.16);transform:translateY(12px);opacity:0;pointer-events:none;transition:opacity .15s,transform .15s;z-index:11;padding:16px}
#fb.open{opacity:1;transform:none;pointer-events:auto}
#fb .close{position:absolute;top:8px;right:10px;border:none;background:none;font-size:20px;color:var(--faint);cursor:pointer}
#fb h3{margin:0 0 3px;font-size:14px;font-weight:600}
#fb .ctx{font-size:12px;color:var(--faint);margin:0 0 10px}
#fb textarea{width:100%;min-height:84px;font:inherit;font-size:14px;padding:8px;border:1px solid var(--line);border-radius:8px;resize:vertical;color:var(--ink)}
#fb .row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:10px}
#fb .note{font-size:11px;color:var(--faint)}
#fb button.send{font:inherit;font-size:13px;padding:6px 13px;border:none;border-radius:8px;background:var(--accent);color:#fff;cursor:pointer}
#fb button.send:disabled{opacity:.5;cursor:default}
#fb .done{font-size:13px;color:var(--accent);padding:8px 2px}
/* translation picker */
#tpanel{position:fixed;top:52px;left:20px;z-index:12;background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.16);padding:10px;max-height:74vh;overflow-y:auto;width:286px;max-width:92vw;display:none}
#tpanel.open{display:block}
.tp-actions{display:flex;gap:6px;margin-bottom:6px}
.tp-actions button{flex:1;font:inherit;font-size:12px;padding:5px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--muted);cursor:pointer}
.tp-actions button:hover{color:var(--ink);border-color:var(--accent)}
.tp-group{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--faint);margin:11px 4px 3px}
.tp-item{display:flex;align-items:center;gap:9px;padding:6px 5px;border-radius:7px;cursor:pointer}
.tp-item:hover{background:#f6f6f6}
.tp-item input{width:16px;height:16px;flex:none;accent-color:var(--accent)}
.tp-code{font-weight:600;font-size:13px;width:46px;flex:none}
.tp-name{color:var(--muted);font-size:12px}
.tp-name em{color:var(--internal);font-style:normal}
/* range badge, continuation marker, partial-coverage note */
.rng{display:inline-block;font-size:9px;font-weight:600;color:var(--accent);background:#eef2f7;border-radius:4px;padding:1px 4px;margin-left:5px;vertical-align:middle;font-variant-numeric:tabular-nums}
.tx.cont .tx-cont{color:var(--faint);font-size:12px;font-style:italic;padding:6px 0}
.ncnote{color:var(--muted);font-size:12.5px;background:#fafafa;border:1px dashed var(--line);border-radius:8px;padding:8px 12px;margin:0 0 18px}
/* mobile reading header: reclaim vertical space; >=38px tap targets + 16px controls (no iOS zoom) */
@media (max-width:600px){
  .topbar{padding:7px 12px;gap:7px}
  .topbar h1{display:none}                                      /* brand hidden by default on phone */
  body.show-brand .topbar h1{display:block;font-size:13px;margin:0 6px 0 0}
  .topbar select{font-size:calc(16px*var(--tbscale));padding:6px 8px;min-height:calc(38px*var(--tbscale));flex:1 1 auto;min-width:0}
  .topbar #chap{flex:0 1 auto;max-width:36%}
  .topbar #tbtn{font-size:calc(13px*var(--tbscale));padding:7px 9px;min-height:calc(38px*var(--tbscale));display:inline-flex;align-items:center;gap:4px}
  .topbar #tbtn .tbtn-full{display:none}   /* mobile: show just the count so row 1 stays to the two selects */
  .topbar #tbtn .tbtn-ic{display:inline-flex}   /* ...with a layers glyph so the count reads as versions */
  .topbar #fbopen{display:none}                                 /* Feedback -> floating launcher */
  .topbar .nav{flex-basis:100%;margin-left:0;justify-content:flex-end;min-height:38px;gap:8px}
  .topbar .nav button{min-width:46px;min-height:calc(38px*var(--tbscale));font-size:calc(16px*var(--tbscale))}
  .topbar .nav .pg{min-width:0;font-size:calc(12px*var(--tbscale))}
  body.lbl-chap .pgfull{display:none} body.lbl-chap .pgshort{display:inline}
  body.lbl-off .topbar .nav .pg{display:none}
  /* single-row option: one line, selects shrink, page label goes short */
  body.onerow .topbar{flex-wrap:nowrap}
  body.onerow .topbar .nav{flex-basis:auto;margin-left:auto}
  body.onerow #tbtn .tbtn-full{display:none}
  body.onerow .pgfull{display:none} body.onerow .pgshort{display:inline}
  #fb textarea{font-size:16px}
  #fb button.send{font-size:15px;padding:8px 15px}
  .tp-item{padding:8px 5px}
  body #fbfloat{display:inline-flex}   /* beat the later base #fbfloat{display:none} (equal-specificity source-order) */
}
/* floating feedback launcher (mobile only; desktop keeps the header button) */
#fbfloat{display:none;position:fixed;left:14px;bottom:16px;z-index:11;align-items:center;gap:6px;font:inherit;font-size:13px;padding:8px 13px;min-height:38px;border:1px solid var(--line);border-radius:20px;background:rgba(255,255,255,.94);backdrop-filter:saturate(180%) blur(8px);color:var(--muted);box-shadow:0 3px 14px rgba(0,0,0,.10);cursor:pointer}
/* header dev bar (gated by ?dev): tune the mobile header on-device, no redeploy */
#hdrdev{display:none;position:fixed;left:0;right:0;bottom:0;z-index:30;background:#1c1c1c;color:#eee;font:inherit;font-size:12px;padding:8px 10px;gap:8px;flex-wrap:wrap;align-items:center;box-shadow:0 -4px 20px rgba(0,0,0,.3)}
#hdrdev.open{display:flex}
#hdrdev b{font-weight:600;color:#fff;margin-right:2px}
#hdrdev .seg{display:flex;border:1px solid #444;border-radius:7px;overflow:hidden}
#hdrdev .seg button{font:inherit;font-size:12px;padding:6px 10px;min-height:34px;border:none;background:#2a2a2a;color:#bbb;cursor:pointer}
#hdrdev .seg button.on{background:var(--accent);color:#fff}
#hdrdev .cfg{margin-left:auto;color:#9a9a9a;font-family:ui-monospace,Menlo,monospace;font-size:11px}
"""

JS = r"""
const $ = s => document.querySelector(s);
let BOOKS = [], cur = {osis:null, ch:1, name:''}, targetVerse = null, lastWord = null;
let TRANS = [], SEL = new Set(), lastData = null, ABBR = {};
const panel=$('#panel'), scrim=$('#scrim'), body=$('#pbody');
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function disp(c){return ABBR[c]||c;}  // short display label for a translation code

async function init(){
  loadHdr();
  [BOOKS, TRANS] = await Promise.all([
    fetch('/api/books').then(r=>r.json()), fetch('/api/translations').then(r=>r.json())]);
  ABBR={}; TRANS.forEach(t=>ABBR[t.code]=t.abbr||t.code);
  SEL = loadSel(); buildPicker(); updateTbtn();
  const bsel=$('#book');
  let og=null,lastT=null;
  for(const b of BOOKS){
    if(b.testament!==lastT){ lastT=b.testament; og=document.createElement('optgroup'); og.label=({OT:'Old Testament',NT:'New Testament',DC:'Deuterocanon',AP:'Appendix'})[b.testament]||b.testament; bsel.appendChild(og); }
    const o=document.createElement('option'); o.value=b.osis; o.textContent=b.name; (og||bsel).appendChild(o);
  }
  bsel.value='John'; onBook();
  bsel.addEventListener('change',onBook);
  $('#chap').addEventListener('change',()=>{cur.ch=+$('#chap').value; load();});
  $('#prev').addEventListener('click',()=>{if(cur.ch>1){cur.ch--;syncChap();load();}});
  $('#next').addEventListener('click',()=>{const b=BOOKS.find(x=>x.osis===cur.osis); if(cur.ch<b.chapters){cur.ch++;syncChap();load();}});
  $('#pclose').addEventListener('click',close); scrim.addEventListener('click',close);
  $('#tbtn').addEventListener('click',e=>{e.stopPropagation(); $('#tpanel').classList.toggle('open');});
  document.addEventListener('click',e=>{ if(!e.target.closest('#tpanel')&&!e.target.closest('#tbtn')) $('#tpanel').classList.remove('open'); });
  $('#fbopen').addEventListener('click',openFb); $('#fbclose').addEventListener('click',closeFb);
  $('#fbsend').addEventListener('click',sendFb);
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){close();closeFb();$('#tpanel').classList.remove('open');}});
  $('#fbfloat').addEventListener('click',openFb);
  setupHdrDev(); applyHdr();
}

/* ---- translation picker ---- */
function loadSel(){ try{ const s=JSON.parse(localStorage.getItem('st.sel'));
  if(Array.isArray(s)&&s.length) return new Set(s.filter(c=>TRANS.some(t=>t.code===c))); }catch(e){}
  return new Set(TRANS.map(t=>t.code)); }
function persistSel(){ try{ localStorage.setItem('st.sel', JSON.stringify([...SEL])); }catch(e){} }
function updateTbtn(){ $('#tbtn').innerHTML='<span class=tbtn-ic><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg></span><span class=tbtn-full>Translations </span><span class=tbtn-n>'+SEL.size+'/'+TRANS.length+'</span>'; }
function buildPicker(){
  const byTrad={}; for(const t of TRANS){ (byTrad[t.tradition]=byTrad[t.tradition]||[]).push(t); }
  let h='<div class=tp-actions><button type=button id=tpall>All</button><button type=button id=tpnone>None</button></div>';
  for(const trad of ['Protestant','Catholic','Jewish','Orthodox','Other']){
    if(!byTrad[trad]) continue;
    h+='<div class=tp-group>'+trad+'</div>';
    for(const t of byTrad[trad])
      h+='<label class=tp-item><input type=checkbox value="'+esc(t.code)+'"'+(SEL.has(t.code)?' checked':'')+
         '><span class=tp-code>'+esc(disp(t.code))+'</span><span class=tp-name>'+esc(t.name)+(t.internal?' · <em>internal</em>':'')+'</span></label>';
  }
  const w=$('#tpanel'); w.innerHTML=h;
  w.querySelectorAll('input[type=checkbox]').forEach(cb=>cb.addEventListener('change',()=>{
    cb.checked?SEL.add(cb.value):SEL.delete(cb.value); afterSel(); }));
  $('#tpall').addEventListener('click',()=>{ SEL=new Set(TRANS.map(t=>t.code)); syncPicker(); afterSel(); });
  $('#tpnone').addEventListener('click',()=>{ SEL=new Set(); syncPicker(); afterSel(); });
}
function syncPicker(){ $('#tpanel').querySelectorAll('input[type=checkbox]').forEach(cb=>cb.checked=SEL.has(cb.value)); }
function afterSel(){ persistSel(); updateTbtn(); if(lastData) render(lastData); }
function onBook(){ cur.osis=$('#book').value; const b=BOOKS.find(x=>x.osis===cur.osis); cur.name=b.name; cur.ch=1;
  const cs=$('#chap'); cs.innerHTML=''; for(let i=1;i<=b.chapters;i++){const o=document.createElement('option');o.value=i;o.textContent=i;cs.appendChild(o);} load(); }
function syncChap(){ $('#chap').value=cur.ch; }
function setNav(){ const b=BOOKS.find(x=>x.osis===cur.osis); $('#pg').innerHTML='<span class=pgfull>'+esc(cur.name)+' '+cur.ch+' / '+b.chapters+'</span><span class=pgshort>'+cur.ch+'/'+b.chapters+'</span>'; }

async function load(){
  setNav();
  $('#reader').innerHTML='<div class="empty">Loading…</div>';
  const data = await (await fetch('/api/chapter?book='+encodeURIComponent(cur.osis)+'&ch='+cur.ch)).json();
  render(data);
  if(targetVerse){ const tv=targetVerse; targetVerse=null;
    requestAnimationFrame(()=>{ const el=document.querySelector('[data-pu="'+CSS.escape(tv)+'"]'); if(el){el.classList.add('target'); el.scrollIntoView({block:'center'});} });
  } else window.scrollTo(0,0);
}
function tokenCell(t,lang){
  let orig;
  if(lang==='hbo' && t.morphemes){ orig='<div class="orig hbo">'+t.morphemes.map(m=>'<span class="m m-'+esc(m.role)+'">'+esc(m.surface)+'</span>').join('')+'</div>'; }
  else orig='<div class="orig grc">'+esc(t.surface)+'</div>';
  return '<div class="w" data-strong="'+esc(t.strong)+'">'+orig+
    (t.translit?'<div class="tr">'+esc(t.translit)+'</div>':'')+
    (t.gloss?'<div class="gl">'+esc(t.gloss)+'</div>':'')+
    (t.strong?'<div class="meta">'+esc(t.strong)+'</div>':'')+'</div>';
}
function render(data){
  lastData=data;
  $('#ctitle').textContent=(data.book||cur.name||'')+' '+(data.chapter||cur.ch||'');
  if(!data.verses||!data.verses.length){ $('#reader').innerHTML='<div class="empty">No text for this chapter.</div>'; return; }
  let h='';
  if(data.osis==='Ps'){ h+='<div class="psnote">DRC, CPDV (Vulgate) and Brenton (Septuagint) number the Psalms differently; their text here is realigned to the English/Hebrew numbering via a TVTMS versification map, so every translation shows the same psalm.</div>'; }
  if(!SEL.size){ $('#reader').innerHTML=h+'<div class="empty">No translations selected. Open <strong>Translations</strong> above to choose some.</div>'; return; }
  // genuine coverage gaps only: a selected translation that covers other books of this
  // testament but not this one (TPT lacks 15 OT books). Skip structural OT/NT absences
  // (e.g. JPS/LXX have no NT) so the note doesn't nag on every chapter of the other testament.
  const osisT={}; BOOKS.forEach(b=>osisT[b.osis]=b.testament); const bookT=osisT[data.osis];
  const miss=TRANS.filter(t=>SEL.has(t.code) && !(t.books||[]).includes(data.osis)
                 && (t.books||[]).some(bk=>osisT[bk]===bookT));
  if(miss.length) h+='<div class="ncnote">'+miss.map(t=>esc(disp(t.code))).join(', ')+(miss.length===1?' does':' do')+' not cover '+esc(data.book)+'.</div>';
  // continuation map: a merged paraphrase block (verse_end) also covers later verses
  const conti={};
  for(const v of data.verses) for(const tr of v.translations) if(tr.verse_end&&tr.verse_end>v.verse)
    for(let k=v.verse+1;k<=tr.verse_end;k++){ (conti[k]=conti[k]||[]).push({code:tr.code,label:v.verse+'–'+tr.verse_end}); }
  for(const v of data.verses){
    h+='<div class="verse" data-pu="'+esc(v.pu)+'"><div class="vno'+(v.is_title?' title':'')+'">'+esc(v.label)+'</div><div>';
    if(v.original && v.original.tokens.length){
      h+='<div class="interlinear '+(v.original.lang==='hbo'?'rtl':'')+'">'+v.original.tokens.map(t=>tokenCell(t,v.original.lang)).join('')+'</div>';
    }
    const shown=v.translations.filter(tr=>SEL.has(tr.code));
    for(const tr of shown){
      const badge=tr.verse_end?'<span class="rng">'+v.verse+'–'+tr.verse_end+'</span>':'';
      h+='<div class="tx"><div class="tx-code">'+esc(disp(tr.code))+badge+(tr.internal?'<span class="tag">internal</span>':'')+'</div>'+
         '<div class="tx-text'+(tr.internal?' internal':'')+'">'+esc(tr.text)+'</div></div>';
    }
    const shownCodes=new Set(shown.map(t=>t.code));
    for(const x of (conti[v.verse]||[])) if(SEL.has(x.code)&&!shownCodes.has(x.code))
      h+='<div class="tx cont"><div class="tx-code">'+esc(disp(x.code))+'</div><div class="tx-cont">part of '+esc(x.label)+' ↑</div></div>';
    h+='</div></div>';
  }
  $('#reader').innerHTML=h;
}

document.addEventListener('click',async e=>{
  const w=e.target.closest('.w[data-strong]');
  if(w && w.getAttribute('data-strong')){ showWord(w.getAttribute('data-strong')); return; }
  const o=e.target.closest('.o[data-pu]');
  if(o){ jumpTo(o.getAttribute('data-pu')); }
});
async function showWord(strong){
  body.innerHTML='<div class="loading">Loading…</div>'; panel.classList.add('open'); scrim.classList.add('open');
  const w=await (await fetch('/api/word?strong='+encodeURIComponent(strong))).json();
  lastWord={strong:strong, headword:(w.def&&w.def.headword)?w.def.headword:strong};
  const heb=strong[0]==='H';
  let h='<div class="pw '+(heb?'hbo':'grc')+'">'+esc(w.def&&w.def.headword?w.def.headword:strong)+'</div>';
  if(w.def&&w.def.translit) h+='<div class="ptr">'+esc(w.def.translit)+'</div>';
  const dm=[strong]; if(w.def&&w.def.pos)dm.push(esc(w.def.pos)); if(w.def&&w.def.lexicon)dm.push(esc(w.def.lexicon));
  h+='<div class="pmeta">'+dm.join(' · ')+'</div>';
  if(w.def){
    if(w.def.gloss)h+='<div class="pgloss">'+esc(w.def.gloss)+'</div>';
    if(w.def.entry)h+='<div class="pentry">'+w.def.entry+'</div>';
    if(w.def.gated)h+='<div class="rights">Hebrew brief definition (TBESH / Abridged BDB) — internal use; licensing to be cleared before any public display.</div>';
  } else h+='<div class="pentry">No lexicon entry for this Strong’s number.</div>';
  h+='<h4>Appears '+w.total+' time'+(w.total===1?'':'s')+(w.total>w.items.length?' (showing '+w.items.length+')':'')+'</h4><div class="occ">';
  for(const o of w.items){ h+='<div class="o" data-pu="'+esc(o.pu)+'"><span class="oref">'+esc(o.ref)+' ›</span><span class="osurf">'+esc(o.surf)+'</span></div>'; }
  h+='</div>';
  body.innerHTML=h; panel.scrollTop=0;
}
function jumpTo(pu){ // pu:OSIS.ch.vs(.title)
  const m=pu.match(/^pu:(.+)\.(\d+)\.(\d+|title)$/); if(!m) return;
  const osis=m[1], ch=+m[2];
  targetVerse=pu; close();
  cur.osis=osis; const b=BOOKS.find(x=>x.osis===osis); cur.name=b?b.name:osis; cur.ch=ch;
  $('#book').value=osis; onBookKeepChap();
}
function onBookKeepChap(){ const b=BOOKS.find(x=>x.osis===cur.osis); const cs=$('#chap'); cs.innerHTML='';
  for(let i=1;i<=b.chapters;i++){const o=document.createElement('option');o.value=i;o.textContent=i;cs.appendChild(o);} $('#chap').value=cur.ch; load(); }
function close(){ panel.classList.remove('open'); scrim.classList.remove('open'); lastWord=null; }
function openFb(){ const w=lastWord;
  $('#fbctx').textContent='On '+(cur.name||'')+' '+(cur.ch||'')+(w?(' · '+(w.headword||w.strong)):'');
  $('#fbmsg').value=''; $('#fbbody').style.display=''; $('#fbdone').style.display='none'; $('#fbsend').disabled=false;
  $('#fb').classList.add('open'); setTimeout(()=>$('#fbmsg').focus(),60); }
function closeFb(){ $('#fb').classList.remove('open'); }
async function sendFb(){ const c=$('#fbmsg').value.trim(); if(!c) return; $('#fbsend').disabled=true; const w=lastWord;
  try{ await fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({comment:c,book:cur.osis,chapter:cur.ch,ref:(cur.name||'')+' '+(cur.ch||''),strong:w?w.strong:'',word:w?(w.headword||''):'',pu:targetVerse||''})});
    $('#fbbody').style.display='none'; $('#fbdone').style.display=''; setTimeout(closeFb,1200);
  }catch(e){ $('#fbsend').disabled=false; } }
/* ---- mobile header prefs + dev bar (gated by ?dev) ---- */
const HDR={onerow:1,brand:0,label:'off',scale:1};  // baked mobile default (Noah 2026-06-07): single row, no brand, no nav label (chapter reads from the dropdown)
function loadHdr(){ try{Object.assign(HDR,JSON.parse(localStorage.getItem('st.hdr')||'{}'));}catch(e){} }
function applyHdr(){
  const b=document.body.classList;
  b.toggle('onerow',!!HDR.onerow); b.toggle('show-brand',!!HDR.brand);
  b.toggle('lbl-chap',HDR.label==='chap'); b.toggle('lbl-off',HDR.label==='off');
  document.documentElement.style.setProperty('--tbscale',HDR.scale);
  const cfg=$('#hdrcfg'); if(cfg) cfg.textContent='row='+(HDR.onerow?'1':'2')+'  brand='+(HDR.brand?'on':'off')+'  label='+HDR.label+'  scale='+(+HDR.scale).toFixed(2);
  document.querySelectorAll('#hdrdev button[data-k]').forEach(btn=>{ const k=btn.dataset.k,v=btn.dataset.v; let on=false;
    if(k==='onerow')on=(HDR.onerow?'1':'0')===v; else if(k==='brand')on=(HDR.brand?'1':'0')===v; else if(k==='label')on=HDR.label===v;
    btn.classList.toggle('on',on); });
}
function saveHdr(){ try{localStorage.setItem('st.hdr',JSON.stringify(HDR));}catch(e){} applyHdr(); }
function setupHdrDev(){
  if(!/[?&]dev\b/.test(location.search)) return;  // tuner shows only with ?dev in the URL; never persisted, so normal use is clean
  $('#hdrdev').classList.add('open');
  $('#hdrdev').addEventListener('click',e=>{ const btn=e.target.closest('button[data-k]'); if(!btn)return;
    const k=btn.dataset.k,v=btn.dataset.v;
    if(k==='onerow')HDR.onerow=v==='1'?1:0;
    else if(k==='brand')HDR.brand=v==='1'?1:0;
    else if(k==='label')HDR.label=v;
    else if(k==='scale')HDR.scale=Math.min(1.4,Math.max(1,+(HDR.scale+(v==='up'?0.04:-0.04)).toFixed(2)));
    saveHdr(); });
}
init();
"""

PAGE = (
    "<!doctype html><html lang=en><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Source Text</title><style>" + CSS + "</style>"
    "<script>try{if(localStorage.getItem('st.hintseen'))document.documentElement.classList.add('hint-seen');else localStorage.setItem('st.hintseen','1');}catch(e){}</script>"
    "</head><body>"
    "<div id=scrim></div><aside id=panel><button id=pclose class=close>&times;</button><div id=pbody></div></aside>"
    "<div id=fb><button id=fbclose class=close>&times;</button>"
    "<div id=fbbody><h3>Send feedback</h3><div class=ctx id=fbctx></div>"
    "<textarea id=fbmsg placeholder='What is working, what is confusing, what is missing...'></textarea>"
    "<div class=row><span class=note>Goes only to Noah.</span><button class=send id=fbsend>Send</button></div></div>"
    "<div id=fbdone class=done style='display:none'>Thanks, sent.</div></div>"
    "<div class=topbar><h1>Source Text</h1>"
    "<select id=book></select><select id=chap></select>"
    "<button id=tbtn class=fbbtn>Translations</button>"
    "<button id=fbopen class=fbbtn>Feedback</button>"
    "<span class=nav><button id=prev>&larr;</button><span class=pg id=pg></span><button id=next>&rarr;</button></span>"
    "</div><div id=tpanel></div>"
    "<button id=fbfloat>Feedback</button>"
    "<div id=hdrdev><b>Header</b>"
    "<span class=seg><button data-k=onerow data-v=0>2-row</button><button data-k=onerow data-v=1>1-row</button></span>"
    "<span class=seg><button data-k=brand data-v=0>No brand</button><button data-k=brand data-v=1>Brand</button></span>"
    "<span class=seg><button data-k=label data-v=full>Full</button><button data-k=label data-v=chap>Ch</button><button data-k=label data-v=off>Off</button></span>"
    "<span class=seg><button data-k=scale data-v=down>A-</button><button data-k=scale data-v=up>A+</button></span>"
    "<span class=cfg id=hdrcfg></span></div>"
    "<div class=wrap><h2 class=chapter-title id=ctitle></h2>"
    "<p class=hint>Click any Greek or Hebrew word for its Strong’s definition and every occurrence across the whole corpus. "
    "Use <em>Translations</em> to choose which versions appear; public-domain ones show plainly, copyrighted ones are tagged "
    "<em>internal</em> (personal use).</p>"
    "<div id=reader></div>"
    "<footer>Local study app over source-text.translations.sqlite — 15 translations across 4 traditions, full Greek NT + Hebrew OT with Strong’s. "
    "Sources: STEPBible TAGNT/TAHOT/TBESG/TBESH (CC BY 4.0); public-domain translations; copyrighted translations "
    "(NASB/NIV/NLT/NKJV/AMP/MSG/GNT/TPT) internal.</footer></div>"
    "<script>" + JS + "</script></body></html>"
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()
    if not DB.exists():
        raise SystemExit(f"DB missing: {DB}\nRun scripts/ingest/build_canonical.py (local) "
                         f"or the start script's R2 download (deploy).")
    # Render (and other hosts) inject $PORT; presence of it means bind all interfaces.
    port = int(os.environ.get("PORT", args.port))
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    gate = f"ON ({len(AUTH_USERS)} user{'' if len(AUTH_USERS) == 1 else 's'})" if AUTH_USERS else "OFF (open)"
    print(f"Source Text study app -> {url}  auth={gate}  db={DB}  (Ctrl-C to stop)")
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
