import os
import re
import io
import html
import base64
import time
import zipfile
import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
import streamlit as st
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ═════════════════════════ Config ═════════════════════════
def secret(key, default=None):
    val = os.environ.get(key)
    if val:
        return val
    try:
        return st.secrets[key]
    except Exception:
        return default


TG_TOKEN = secret("TELEGRAM_TOKEN")
GH_TOKEN = secret("GITHUB_TOKEN")
_uid = secret("ALLOWED_USER_ID")
ALLOWED_USER_ID = int(_uid) if _uid else None

if not TG_TOKEN or not GH_TOKEN:
    st.error("Secrets me TELEGRAM_TOKEN aur GITHUB_TOKEN daalo.")
    st.stop()

API = "https://api.github.com"
HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
SESSION = requests.Session()
MAX_ZIP_BYTES = 20 * 1024 * 1024  # Telegram bot download limit
MAX_SEND_BYTES = 49 * 1024 * 1024  # Telegram bot upload limit
MAX_FILES = 500
PAGE_SIZE = 8  # repos per page
DIR_PAGE = 10  # explorer items per page
LINES_PER_PAGE = 25  # file viewer lines per page
SKIP = ("__MACOSX/", ".DS_Store", ".git/")

to = asyncio.to_thread


def B(text, data):
    return InlineKeyboardButton(text, callback_data=data)


# ═════════════════════════ GitHub helpers (sync) ═════════════════════════
def gh(method, path, **kwargs):
    r = SESSION.request(method, f"{API}{path}", headers=HEADERS, timeout=60, **kwargs)
    if r.status_code >= 400:
        try:
            j = r.json()
            detail = "; ".join(
                e.get("message", str(e)) if isinstance(e, dict) else str(e)
                for e in j.get("errors", [])
            )
            msg = f"{j.get('message', '')} {detail}".strip()
            if not detail:
                msg += f"\nRAW: {r.text[:300]}"
        except Exception:
            msg = r.text[:300]
        raise RuntimeError(f"GitHub {r.status_code}: {msg}")
    return r.json() if r.content else {}


_login = None


def login():
    global _login
    if not _login:
        _login = gh("GET", "/user")["login"]
    return _login


def rp(name):
    return f"/repos/{login()}/{name}"


def qp(path):
    return quote(path, safe="/")


def clean_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return name or "new-repo"


def norm_path(p):
    parts = [x for x in p.strip().strip("/").split("/") if x]
    if not parts or any(x in (".", "..") for x in parts) or parts[0] == ".git":
        raise ValueError("Path galat hai. Example: src/utils/helper.py")
    return "/".join(parts)


def join_path(folder, name):
    return norm_path(f"{folder}/{name}" if folder else name)


def read_zip(data):
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir() or any(s in info.filename for s in SKIP):
                continue
            files[info.filename] = z.read(info)
    if not files:
        raise RuntimeError("Zip khaali hai")
    if len(files) > MAX_FILES:
        raise RuntimeError(f"Zip me {MAX_FILES} se zyada files hain")
    tops = {p.split("/")[0] for p in files}
    if len(tops) == 1 and all("/" in p for p in files):
        files = {p.split("/", 1)[1]: c for p, c in files.items()}
    return files


# ---------- git data API: branch head, commits, tree changes ----------
def head_info(full, branch=None):
    if not branch:
        branch = gh("GET", f"/repos/{full}")["default_branch"]
    ref = gh("GET", f"/repos/{full}/git/ref/heads/{qp(branch)}")
    parent = ref["object"]["sha"]
    base = gh("GET", f"/repos/{full}/git/commits/{parent}")["tree"]["sha"]
    return branch, parent, base


def commit_entries(full, branch, parent, base, entries, message, replace=False):
    tree_sha = None if replace else base
    for i in range(0, len(entries), 100):
        body = {"tree": entries[i:i + 100]}
        if tree_sha:
            body["base_tree"] = tree_sha
        tree_sha = gh("POST", f"/repos/{full}/git/trees", json=body)["sha"]
    commit = gh("POST", f"/repos/{full}/git/commits",
                json={"message": message, "tree": tree_sha, "parents": [parent]})
    gh("PATCH", f"/repos/{full}/git/refs/heads/{qp(branch)}", json={"sha": commit["sha"]})


def push_files(full, branch, files, message, replace=False):
    ref = None
    for _ in range(10):  # naye repo me branch ready hone me thoda time lagta hai
        try:
            ref = head_info(full, branch)
            break
        except RuntimeError:
            time.sleep(1)
    if not ref:
        raise RuntimeError("Branch ready nahi hui, thodi der baad try karo")
    branch, parent, base = ref

    def make_entry(item):
        path, content = item
        try:
            text = content.decode("utf-8")
            if content and "\x00" not in text:
                return {"path": path, "mode": "100644", "type": "blob", "content": text}
        except UnicodeDecodeError:
            pass
        blob = gh("POST", f"/repos/{full}/git/blobs",
                  json={"content": base64.b64encode(content).decode(), "encoding": "base64"})
        return {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}

    with ThreadPoolExecutor(max_workers=6) as pool:
        entries = list(pool.map(make_entry, files.items()))
    commit_entries(full, branch, parent, base, entries, message, replace)


def create_repo(name, private):
    for i in range(1, 8):
        candidate = name if i == 1 else f"{name}-{i}"
        try:
            return gh("POST", "/user/repos",
                      json={"name": candidate, "private": private, "auto_init": True})
        except RuntimeError as e:
            if "already exists" in str(e) and i < 7:
                continue
            raise


def create_from_zip(name, private, files):
    repo = create_repo(name, private)
    push_files(repo["full_name"], repo["default_branch"], files, "Upload via Telegram bot")
    return repo["html_url"], repo["name"]


def update_from_zip(name, files, replace, branch=None):
    repo = gh("GET", rp(name))
    msg = "Replace files via Telegram bot" if replace else "Update files via Telegram bot"
    push_files(repo["full_name"], branch or repo["default_branch"], files, msg, replace)
    return repo["html_url"]


def download_zip(name, branch=None):
    ref = f"/{quote(branch, safe='')}" if branch else ""
    r = SESSION.get(f"{API}{rp(name)}/zipball{ref}", headers=HEADERS, timeout=120)
    r.raise_for_status()
    return r.content


# ---------- explorer / file operations ----------
def _ref_q(branch):
    return f"?ref={quote(branch, safe='')}" if branch else ""


def list_dir(repo, path, branch=None):
    p = f"/{qp(path)}" if path else ""
    items = gh("GET", f"{rp(repo)}/contents{p}{_ref_q(branch)}")
    if isinstance(items, dict):
        raise RuntimeError("Ye file hai, folder nahi")
    return sorted(items, key=lambda i: (i["type"] != "dir", i["name"].lower()))


def read_file(repo, path, branch=None):
    item = gh("GET", f"{rp(repo)}/contents/{qp(path)}{_ref_q(branch)}")
    if isinstance(item, list):
        raise RuntimeError("Ye folder hai, file nahi")
    if item.get("encoding") == "base64" and item.get("content"):
        data = base64.b64decode(item["content"])
    else:  # 1 MB se badi ya khaali file
        blob = gh("GET", f"{rp(repo)}/git/blobs/{item['sha']}")
        data = base64.b64decode(blob["content"])
    return data, item["sha"]


def upsert_file(repo, path, content, message, branch=None):
    sha = None
    try:
        item = gh("GET", f"{rp(repo)}/contents/{qp(path)}{_ref_q(branch)}")
        if isinstance(item, dict):
            sha = item["sha"]
    except RuntimeError:
        pass
    body = {"message": message, "content": base64.b64encode(content).decode()}
    if branch:
        body["branch"] = branch
    if sha:
        body["sha"] = sha
    gh("PUT", f"{rp(repo)}/contents/{qp(path)}", json=body)


def create_file_new(repo, path, content, message, branch=None):
    body = {"message": message, "content": base64.b64encode(content).decode()}
    if branch:
        body["branch"] = branch
    try:
        gh("PUT", f"{rp(repo)}/contents/{qp(path)}", json=body)
    except RuntimeError as e:
        if "sha" in str(e).lower():
            raise RuntimeError(f"'{path}' pehle se hai. Edit option use karo.")
        raise


def delete_file(repo, path, message, branch=None):
    item = gh("GET", f"{rp(repo)}/contents/{qp(path)}{_ref_q(branch)}")
    body = {"message": message, "sha": item["sha"]}
    if branch:
        body["branch"] = branch
    gh("DELETE", f"{rp(repo)}/contents/{qp(path)}", json=body)


def _blobs_under(full, base, prefix):
    t = gh("GET", f"/repos/{full}/git/trees/{base}?recursive=1")
    if t.get("truncated"):
        raise RuntimeError("Repo bahut bada hai, ye action abhi support nahi")
    return t["tree"], [e for e in t["tree"] if e["type"] == "blob"
                       and (e["path"] == prefix or e["path"].startswith(prefix + "/"))]


def delete_path(repo, prefix, branch=None):
    full = f"{login()}/{repo}"
    branch, parent, base = head_info(full, branch)
    _, hits = _blobs_under(full, base, prefix)
    if not hits:
        raise RuntimeError("Is path me kuch nahi mila")
    entries = [{"path": e["path"], "mode": e["mode"], "type": "blob", "sha": None} for e in hits]
    commit_entries(full, branch, parent, base, entries, f"Delete {prefix} via Telegram bot")
    return len(hits)


def move_path(repo, old, new, branch=None):
    full = f"{login()}/{repo}"
    branch, parent, base = head_info(full, branch)
    tree, hits = _blobs_under(full, base, old)
    if not hits:
        raise RuntimeError("Purana path nahi mila")
    if new == old or new.startswith(old + "/"):
        raise RuntimeError("Naya path purane ke andar nahi ho sakta")
    for e in tree:
        if e["type"] == "blob" and (e["path"] == new or e["path"].startswith(new + "/")):
            raise RuntimeError(f"'{new}' pehle se hai")
    entries = []
    for e in hits:
        entries.append({"path": e["path"], "mode": e["mode"], "type": "blob", "sha": None})
        entries.append({"path": new + e["path"][len(old):], "mode": e["mode"],
                        "type": "blob", "sha": e["sha"]})
    commit_entries(full, branch, parent, base, entries, f"Move {old} -> {new} via Telegram bot")
    return len(hits)


def search_repo(repo, q, branch=None):
    full = f"{login()}/{repo}"
    branch, _, base = head_info(full, branch)
    ql = q.lower()
    t = gh("GET", f"/repos/{full}/git/trees/{base}?recursive=1")
    names = [e["path"] for e in t["tree"] if e["type"] == "blob" and ql in e["path"].lower()][:12]
    try:  # code search (sirf default branch, naye repo me index hone me time lagta hai)
        res = gh("GET", "/search/code", params={"q": f"{q} repo:{full}", "per_page": 10})
        hits = [i["path"] for i in res.get("items", [])]
    except RuntimeError:
        hits = []
    return names, hits


# ---------- branches ----------
def create_branch(repo, new, from_branch=None):
    full = f"{login()}/{repo}"
    src, parent, _ = head_info(full, from_branch)
    gh("POST", f"/repos/{full}/git/refs", json={"ref": f"refs/heads/{new}", "sha": parent})


def delete_branch(repo, branch):
    gh("DELETE", f"{rp(repo)}/git/refs/heads/{qp(branch)}")


def set_default_branch(repo, branch):
    gh("PATCH", rp(repo), json={"default_branch": branch})


def merge_branch(repo, base, head):
    return gh("POST", f"{rp(repo)}/merges",
              json={"base": base, "head": head, "commit_message": f"Merge {head} into {base} via Telegram bot"})


# ---------- editing helpers (pure) ----------
def apply_line_edit(src, msg):
    """N: text | N-M: text | +N: text (N ke baad insert) | -N | -N-M (delete).
    Return (naya_text, first_line_index)."""
    lines = src.split("\n")
    first, sep, rest = msg.partition("\n")
    m = re.match(r"^-(\d+)(?:-(\d+))?\s*$", first)
    if m:
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if not (1 <= a <= b <= len(lines)):
            raise ValueError(f"Line range galat hai (file me {len(lines)} lines hain)")
        del lines[a - 1:b]
        return "\n".join(lines), max(a - 2, 0)
    m = re.match(r"^(\+)?(\d+)(?:-(\d+))?:\s?(.*)$", first)
    if not m:
        raise ValueError(
            "Format galat hai.\n"
            "5: naya text  → line 5 replace\n"
            "5-7: naya text  → lines 5 se 7 replace\n"
            "+5: text  → line 5 ke baad insert (0 = sabse upar)\n"
            "-5 ya -5-7  → line delete"
        )
    ins, a = m.group(1), int(m.group(2))
    b = int(m.group(3) or a)
    new_lines = [m.group(4)] + (rest.split("\n") if sep else [])
    if ins:
        if not (0 <= a <= len(lines)):
            raise ValueError(f"Line number galat hai (file me {len(lines)} lines hain)")
        lines[a:a] = new_lines
        return "\n".join(lines), a
    if not (1 <= a <= b <= len(lines)):
        raise ValueError(f"Line range galat hai (file me {len(lines)} lines hain)")
    lines[a - 1:b] = new_lines
    return "\n".join(lines), a - 1


def apply_find_replace(src, msg):
    if "\n===>\n" not in msg:
        raise ValueError("Format:\nPURANA TEXT\n===>\nNAYA TEXT")
    old, new = msg.split("\n===>\n", 1)
    n = src.count(old)
    if not old or n == 0:
        raise ValueError("Ye text file me nahi mila")
    return src.replace(old, new), n


def token_status():
    r = SESSION.get(f"{API}/user", headers=HEADERS, timeout=30)
    if not r.ok:
        return f"❌ Token problem: {r.status_code}\n{r.text[:200]}"
    lines = [f"✅ GitHub user: {r.json().get('login')}"]
    scopes = r.headers.get("X-OAuth-Scopes")
    if scopes is None:
        lines.append("Token type: fine-grained (Administration + Contents write chahiye)")
    else:
        lines.append(f"Scopes: {scopes or '-'}")
        lines.append("Repo banana/edit: " + ("✅" if "repo" in scopes else "❌ 'repo' scope nahi hai"))
        lines.append("Repo delete: " + ("✅" if "delete_repo" in scopes else "❌ 'delete_repo' scope nahi hai"))
    return "\n".join(lines)


# ═════════════════════════ Telegram helpers ═════════════════════════
def _reg(context, store, key_src, value):
    key = hashlib.md5(key_src.encode()).hexdigest()[:8]
    context.user_data.setdefault(store, {})[key] = value
    return key


def rid(context, name):  # repo id (callback data 64 byte limited hoti hai)
    return _reg(context, "names", name, name)


def rname(context, key):
    return context.user_data.get("names", {}).get(key)


def pid(context, repo, path):  # path id
    return _reg(context, "paths", f"{repo}/{path}", (repo, path))


def pget(context, key):
    return context.user_data.get("paths", {}).get(key)


def bid(context, repo, branch):  # branch id
    return _reg(context, "bnames", f"{repo}@{branch}", (repo, branch))


def bget(context, key):
    return context.user_data.get("bnames", {}).get(key)


def cur_branch(context, repo):
    return context.user_data.get("branch", {}).get(repo)


def set_cur_branch(context, repo, branch):
    d = context.user_data.setdefault("branch", {})
    if branch:
        d[repo] = branch
    else:
        d.pop(repo, None)


async def show(update, text, rows=None, html_mode=False):
    markup = InlineKeyboardMarkup(rows) if rows else None
    kw = {"parse_mode": "HTML"} if html_mode else {}
    if not html_mode:
        text = text[:4000]
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True, **kw)
        except BadRequest:
            pass  # "message is not modified"
    else:
        await update.effective_message.reply_text(
            text, reply_markup=markup, disable_web_page_preview=True, **kw)


async def guard(update):
    uid = update.effective_user.id
    if not ALLOWED_USER_ID:
        await update.effective_message.reply_text(
            "🔐 Delete/edit jaise features ke liye bot ko lock karna zaroori hai.\n\n"
            f"Teri Telegram ID: {uid}\n\n"
            "Streamlit Secrets me ye line add karo, phir app reboot karo:\n"
            f'ALLOWED_USER_ID = "{uid}"'
        )
        return False
    return uid == ALLOWED_USER_ID


MENU_ROWS = [
    [B("📦 Naya repo (zip bhejo)", "new")],
    [B("📂 Mere repos", "list:1"), B("⚙️ Settings", "set")],
    [B("🔑 Token check", "check"), B("❓ Help", "help")],
]
HOME = [[B("🏠 Menu", "menu")]]

HELP_TEXT = (
    "❓ Help\n\n"
    "📦 Naya repo: zip bhejo. Caption me naam (optional) aur 'private'/'public' likh sakte ho.\n\n"
    "📂 Har repo me (VS Code jaisa):\n"
    "• 📁 Explorer: folders kholo, files dekho (line numbers ke saath)\n"
    "• ✏️ Edit: line edit, find & replace, poora content, ya file upload\n"
    "• ➕ Naya file, 📤 Upload, 🔀 Rename/Move, 🗑 Delete (file ya folder)\n"
    "• 🔎 Search: file ka naam + code ke andar\n"
    "• 🌿 Branches: banao, switch, merge, default, delete\n"
    "• 🕒 Commits + diff, file history\n"
    "• ⬆️ Zip se update (Merge/Replace), 📥 Download\n"
    "• Public/Private, Rename, Description, Archive, Delete\n\n"
    "Line edit format:\n"
    "5: naya text\n5-7: naya text\n+5: insert\n-5 ya -5-7: delete\n\n"
    "Find & replace format:\nPURANA\n===>\nNAYA\n\n"
    "Commands: /menu /repos /open naam /delete naam /check /id /cancel"
)


async def send_menu(update, note=""):
    await show(update, f"{note}🤖 GitHub Manager\nZip bhejo ya neeche se option chuno.", MENU_ROWS)


async def show_list(update, context, page):
    repos = await to(gh, "GET",
                     f"/user/repos?per_page={PAGE_SIZE}&page={page}&sort=updated&affiliation=owner")
    if not repos and page == 1:
        return await show(update, "Abhi koi repo nahi hai.", HOME)
    rows = [
        [B(f"{'🔒' if r['private'] else '🌐'} {r['name']}"[:60], f"repo:{rid(context, r['name'])}")]
        for r in repos
    ]
    nav = []
    if page > 1:
        nav.append(B("◀️ Pichla", f"list:{page - 1}"))
    if len(repos) == PAGE_SIZE:
        nav.append(B("Agla ▶️", f"list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("🏠 Menu", "menu")])
    await show(update, f"📂 Tumhare repos (page {page})", rows)


async def show_repo(update, context, name, note=""):
    r = await to(gh, "GET", rp(name))
    k = rid(context, r["name"])
    root = pid(context, r["name"], "")
    br = cur_branch(context, r["name"]) or r["default_branch"]
    vis = "🔒 Private" if r["private"] else "🌐 Public"
    arch = " • 🗄 Archived" if r["archived"] else ""
    text = (f"{note}📦 {r['full_name']}\n{vis}{arch}\n🌿 {br}\n"
            f"📝 {r['description'] or '-'}\n{r['html_url']}")
    rows = [
        [B("📁 Explorer", f"ex:{root}:0"), B("ℹ️ Info", f"info:{k}")],
        [B("🕒 Commits", f"commits:{k}"), B("🌿 Branches", f"br:{k}")],
        [B("🔎 Search", f"sr:{k}"), B("⬆️ Zip se update", f"upd:{k}")],
        [B("🌐 Public karo" if r["private"] else "🔒 Private karo", f"vis:{k}"),
         B("✏️ Rename", f"ren:{k}")],
        [B("📝 Description", f"desc:{k}"), B("📥 Download zip", f"dl:{k}")],
        [B("🗂 Unarchive" if r["archived"] else "🗄 Archive", f"arch:{k}"),
         B("🗑 Delete repo", f"del:{k}")],
        [B("⬅️ Repos", "list:1"), B("🏠 Menu", "menu")],
    ]
    await show(update, text, rows)


# ═════════════════════════ Explorer / file viewer ═════════════════════════
async def show_dir(update, context, repo, path, page=0, note=""):
    br = cur_branch(context, repo)
    items = await to(list_dir, repo, path, br)
    pk = pid(context, repo, path)
    rk = rid(context, repo)
    pages = max(1, -(-len(items) // DIR_PAGE))
    page = max(0, min(page, pages - 1))
    rows = []
    for it in items[page * DIR_PAGE:(page + 1) * DIR_PAGE]:
        cp = f"{path}/{it['name']}" if path else it["name"]
        ck = pid(context, repo, cp)
        if it["type"] == "dir":
            rows.append([B(f"📁 {it['name']}"[:60], f"ex:{ck}:0")])
        else:
            rows.append([B(f"📄 {it['name']}"[:60], f"fv:{ck}:0")])
    nav = []
    if page > 0:
        nav.append(B("◀️", f"ex:{pk}:{page - 1}"))
    if page < pages - 1:
        nav.append(B("▶️", f"ex:{pk}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("➕ Naya file", f"nf:{pk}"), B("📤 Upload file", f"up:{pk}")])
    rows.append([B("🔎 Search", f"sr:{rk}"), B("🌿 Branch", f"br:{rk}")])
    if path:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        rows.append([B("⬆️ Upar", f"ex:{pid(context, repo, parent)}:0"),
                     B("🔀 Rename/Move", f"dm:{pk}"), B("🗑 Folder", f"dd:{pk}")])
    rows.append([B("⬅️ Repo", f"repo:{rk}")])
    where = f"{repo}/{path}" if path else repo
    text = (f"{note}📁 {where}\n🌿 {br or 'default'} • {len(items)} items"
            f"{f' • page {page + 1}/{pages}' if pages > 1 else ''}"
            f"{'' if items else chr(10) + '(khaali)'}")
    await show(update, text, rows)


async def show_file(update, context, repo, path, page=0, note=""):
    br = cur_branch(context, repo)
    data, _ = await to(read_file, repo, path, br)
    pk = pid(context, repo, path)
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    ppk = pid(context, repo, parent)
    head = (f"{html.escape(note)}📄 <b>{html.escape(path)}</b>\n"
            f"🌿 {html.escape(br or 'default')} • {len(data)} bytes")
    try:
        text = data.decode("utf-8")
        is_text = "\x00" not in text
    except UnicodeDecodeError:
        is_text = False
    rows = []
    if is_text:
        lines = text.split("\n")
        total = len(lines)
        pages = max(1, -(-total // LINES_PER_PAGE))
        page = max(0, min(page, pages - 1))
        start = page * LINES_PER_PAGE
        chunk = lines[start:start + LINES_PER_PAGE]
        code = "\n".join(f"{start + i + 1:>3}│ {ln[:100]}" for i, ln in enumerate(chunk))
        esc = html.escape(code)
        while len(esc) > 3200:
            code = code[:int(len(code) * 0.8)]
            esc = html.escape(code)
        head += (f" • {total} lines\nLines {start + 1}-{start + len(chunk)} "
                 f"(page {page + 1}/{pages})\n<pre>{esc}</pre>")
        nav = []
        if page > 0:
            nav.append(B("◀️", f"fv:{pk}:{page - 1}"))
        if page < pages - 1:
            nav.append(B("▶️", f"fv:{pk}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([B("✏️ Edit", f"fe:{pk}"), B("📥 Download", f"fd:{pk}")])
    else:
        head += "\n(binary file, preview nahi hai)"
        rows.append([B("📤 File se replace", f"fr:{pk}"), B("📥 Download", f"fd:{pk}")])
    rows.append([B("🕒 History", f"fh:{pk}"), B("🔀 Rename/Move", f"fm:{pk}")])
    rows.append([B("🗑 Delete", f"fx:{pk}"), B("⬅️ Folder", f"ex:{ppk}:0")])
    await show(update, head, rows, html_mode=True)


async def show_branches(update, context, repo, note=""):
    r = await to(gh, "GET", rp(repo))
    default = r["default_branch"]
    bs = await to(gh, "GET", f"{rp(repo)}/branches?per_page=30")
    cur = cur_branch(context, repo) or default
    rk = rid(context, repo)
    rows = []
    for b in bs:
        label = f"{'✅' if b['name'] == cur else '🌿'} {b['name']}{' ⭐' if b['name'] == default else ''}"
        rows.append([B(label[:60], f"bm:{bid(context, repo, b['name'])}")])
    rows.append([B("➕ Nayi branch", f"bnew:{rk}")])
    rows.append([B("⬅️ Repo", f"repo:{rk}")])
    await show(update, f"{note}🌿 Branches ({repo})\n✅ abhi use ho rahi • ⭐ default", rows)


# ═════════════════════════ Button routers ═════════════════════════
FS_ACTS = {"ex", "fv", "fe", "fel", "fef", "fer", "fr", "fd", "fx", "fxy", "fm", "dm",
           "fh", "nf", "up", "dd", "ddy"}
BR_ACTS = {"bm", "bsw", "bdef", "bmg", "bdel", "bdely"}
STALE = "⚠️ Ye menu purana ho gaya. Repos dubara kholo."


async def route_fs(update, context, parts):
    act, pk = parts[0], parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    ud = context.user_data
    entry = pget(context, pk)
    if not entry:
        return await show(update, STALE, [[B("📂 Repos", "list:1")]])
    repo, path = entry
    br = cur_branch(context, repo)
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    back_file = [[B("⬅️ Wapas", f"fv:{pk}:0")]]
    back_dir = [[B("⬅️ Wapas", f"ex:{pk}:0")]]

    if act == "ex":
        ud.pop("pending", None)
        return await show_dir(update, context, repo, path, page)
    if act == "fv":
        ud.pop("pending", None)
        return await show_file(update, context, repo, path, page)

    if act == "fe":
        ud.pop("pending", None)
        return await show(
            update, f"✏️ Edit: {path}\n\nKaise edit karna hai?",
            [[B("📝 Line edit", f"fel:{pk}"), B("🔁 Find & Replace", f"fef:{pk}")],
             [B("📄 Pura content (text)", f"fer:{pk}")],
             [B("📤 File upload se replace", f"fr:{pk}")],
             [B("⬅️ Wapas", f"fv:{pk}:0")]],
        )
    if act == "fel":
        ud["pending"] = {"action": "edit_lines", "repo": repo, "path": path}
        return await show(
            update,
            "📝 Line edit mode ON\n\nEk message me ek command bhejo:\n"
            "5: naya text → line 5 replace\n"
            "5-7: naya text → lines 5-7 replace\n"
            "+5: text → line 5 ke baad insert (0 = upar)\n"
            "-5 ya -5-7 → line delete\n\n"
            "Har edit ek commit banata hai. Band karne ke liye Wapas dabao.",
            back_file,
        )
    if act == "fef":
        ud["pending"] = {"action": "edit_find", "repo": repo, "path": path}
        return await show(
            update,
            "🔁 Find & Replace mode ON\n\nAise bhejo:\n\nPURANA TEXT\n===>\nNAYA TEXT\n\n"
            "Saari matching jagah badal jayengi. Band karne ke liye Wapas dabao.",
            back_file,
        )
    if act == "fer":
        ud["pending"] = {"action": "edit_full", "repo": repo, "path": path}
        return await show(update, "📄 Naya poora content text me bhejo (max ~4000 chars).\n"
                                  "Bada file ho to File upload se replace use karo.", back_file)
    if act == "fr":
        ud["pending"] = {"action": "replace", "repo": repo, "path": path}
        return await show(update, f"📤 '{path}' ki jagah nayi file bhejo (document ki tarah).", back_file)

    if act == "fd":
        await update.callback_query.answer("File la raha hu...")
        data, _ = await to(read_file, repo, path, br)
        await context.bot.send_document(update.effective_chat.id, io.BytesIO(data),
                                        filename=path.rsplit("/", 1)[-1])
        return

    if act == "fx":
        return await show(update, f"🗑 '{path}' delete karni hai?",
                          [[B("✅ Haan, delete", f"fxy:{pk}"), B("❌ Cancel", f"fv:{pk}:0")]])
    if act == "fxy":
        await to(delete_file, repo, path, f"Delete {path} via Telegram bot", br)
        return await show_dir(update, context, repo, parent, 0, f"🗑 '{path}' delete ho gayi\n\n")

    if act in ("fm", "dm"):
        ud["pending"] = {"action": "move", "repo": repo, "path": path, "is_dir": act == "dm"}
        return await show(update, f"🔀 '{path}' ka naya poora path bhejo.\nExample: src/utils/new_name.py",
                          back_dir if act == "dm" else back_file)

    if act == "fh":
        q = f"?path={quote(path)}&per_page=10" + (f"&sha={quote(br)}" if br else "")
        cs = await to(gh, "GET", f"{rp(repo)}/commits{q}")
        rk = rid(context, repo)
        rows = [[B(f"{c['sha'][:7]} {c['commit']['message'].splitlines()[0]}"[:60],
                   f"cd:{rk}:{c['sha'][:7]}")] for c in cs]
        rows.append([B("⬅️ Wapas", f"fv:{pk}:0")])
        return await show(update, f"🕒 History: {path}", rows)

    if act == "nf":
        ud["pending"] = {"action": "newfile", "repo": repo, "dir": path}
        where = path or "root"
        return await show(
            update,
            f"➕ Naya file ({where})\n\nPehli line me file ka naam, baaki me content:\n\n"
            "main.py\nprint('hello')\n\nSubfolder bhi chalega: utils/helper.py",
            back_dir,
        )
    if act == "up":
        ud["pending"] = {"action": "upload", "repo": repo, "dir": path}
        return await show(update, f"📤 Ab file bhejo (document ki tarah). Wo '{path or 'root'}' me jayegi.",
                          back_dir)

    if act == "dd":
        return await show(update, f"🗑 Folder '{path}' aur uski saari files delete karni hain?\n⚠️ Ek commit me hoga.",
                          [[B("✅ Haan, delete", f"ddy:{pk}"), B("❌ Cancel", f"ex:{pk}:0")]])
    if act == "ddy":
        n = await to(delete_path, repo, path, br)
        return await show_dir(update, context, repo, parent, 0, f"🗑 '{path}' delete ({n} files)\n\n")


async def route_bs(update, context, parts):
    act, key = parts[0], parts[1]
    entry = bget(context, key)
    if not entry:
        return await show(update, STALE, [[B("📂 Repos", "list:1")]])
    repo, branch = entry
    rk = rid(context, repo)
    r = await to(gh, "GET", rp(repo))
    default = r["default_branch"]
    cur = cur_branch(context, repo) or default

    if act == "bm":
        rows = [
            [B("✅ Is branch pe kaam karo", f"bsw:{key}")],
            [B("⭐ Default banao", f"bdef:{key}"), B(f"🔀 Merge → {cur}"[:40], f"bmg:{key}")],
            [B("🗑 Delete branch", f"bdel:{key}")],
            [B("⬅️ Branches", f"br:{rk}")],
        ]
        return await show(update, f"🌿 {branch}\n(repo: {repo})", rows)
    if act == "bsw":
        set_cur_branch(context, repo, None if branch == default else branch)
        return await show_branches(update, context, repo, f"✅ Ab '{branch}' use ho rahi hai\n\n")
    if act == "bdef":
        await to(set_default_branch, repo, branch)
        set_cur_branch(context, repo, None)
        return await show_branches(update, context, repo, f"⭐ '{branch}' ab default hai\n\n")
    if act == "bmg":
        if branch == cur:
            raise RuntimeError("Branch khud me merge nahi ho sakti")
        res = await to(merge_branch, repo, cur, branch)
        note = "ℹ️ Pehle se up-to-date tha\n\n" if not res else f"🔀 '{branch}' → '{cur}' merge ho gaya\n\n"
        return await show_branches(update, context, repo, note)
    if act == "bdel":
        return await show(update, f"🗑 Branch '{branch}' delete karni hai?",
                          [[B("✅ Haan, delete", f"bdely:{key}"), B("❌ Cancel", f"bm:{key}")]])
    if act == "bdely":
        await to(delete_branch, repo, branch)
        if cur_branch(context, repo) == branch:
            set_cur_branch(context, repo, None)
        return await show_branches(update, context, repo, f"🗑 '{branch}' delete ho gayi\n\n")


async def route(update, context, parts):
    act = parts[0]
    arg = parts[1] if len(parts) > 1 else None
    ud = context.user_data

    if act == "menu":
        ud.pop("pending", None)
        return await send_menu(update)
    if act == "new":
        return await show(update, "📦 Bas zip file bhejo.\nCaption me repo ka naam likh sakte ho "
                                  "(aur 'private' ya 'public').", HOME)
    if act == "list":
        return await show_list(update, context, int(arg or 1))
    if act == "help":
        return await show(update, HELP_TEXT, HOME)
    if act == "check":
        return await show(update, await to(token_status), HOME)
    if act in ("set", "setvis"):
        if act == "setvis":
            context.bot_data["default_private"] = not context.bot_data.get("default_private", False)
        private = context.bot_data.get("default_private", False)
        return await show(
            update,
            f"⚙️ Settings\n\nNaye repo ki default visibility: {'🔒 Private' if private else '🌐 Public'}\n"
            "(Caption me public/private likhoge to wahi chalega.)",
            [[B("🔁 Visibility badlo", "setvis")], [B("🏠 Menu", "menu")]],
        )
    if act in FS_ACTS:
        return await route_fs(update, context, parts)
    if act in BR_ACTS:
        return await route_bs(update, context, parts)

    # ----- repo-level actions (arg = repo key) -----
    name = rname(context, arg)
    if not name:
        return await show(update, STALE, [[B("📂 Repos", "list:1")]])
    back = [[B("⬅️ Wapas", f"repo:{arg}")]]
    br = cur_branch(context, name)

    if act == "repo":
        ud.pop("pending", None)
        return await show_repo(update, context, name)

    if act == "info":
        r = await to(gh, "GET", rp(name))
        text = (
            f"ℹ️ {r['full_name']}\n"
            f"{'🔒 Private' if r['private'] else '🌐 Public'}"
            f"{' • 🗄 Archived' if r['archived'] else ''}\n"
            f"📝 {r['description'] or '-'}\n"
            f"💻 {r['language'] or '-'}\n"
            f"⭐ {r['stargazers_count']}   🍴 {r['forks_count']}   🐞 {r['open_issues_count']}\n"
            f"📦 {r['size']} KB\n"
            f"🌿 default: {r['default_branch']}\n"
            f"🕒 Last push: {r['pushed_at']}\n"
            f"{r['html_url']}"
        )
        return await show(update, text, back)

    if act == "commits":
        q = "?per_page=10" + (f"&sha={quote(br)}" if br else "")
        cs = await to(gh, "GET", f"{rp(name)}/commits{q}")
        rows = [[B(f"{c['sha'][:7]} {c['commit']['message'].splitlines()[0]}"[:60],
                   f"cd:{arg}:{c['sha'][:7]}")] for c in cs]
        rows.append(back[0])
        return await show(update, f"🕒 Latest commits ({br or 'default'})\nCommit pe tap karo = diff", rows)

    if act == "cd":
        c = await to(gh, "GET", f"{rp(name)}/commits/{parts[2]}")
        files = c.get("files", [])
        tag = {"added": "A", "modified": "M", "removed": "D", "renamed": "R"}
        flines = [f"{tag.get(f['status'], '?')} {f['filename']} (+{f['additions']} -{f['deletions']})"
                  for f in files[:15]]
        patch = "\n\n".join(f"── {f['filename']}\n{f.get('patch', '(binary)')}" for f in files)
        esc = html.escape(patch)
        while len(esc) > 2000:
            patch = patch[:int(len(patch) * 0.8)]
            esc = html.escape(patch)
        author = (c.get("commit", {}).get("author") or {})
        title = c["commit"]["message"].splitlines()[0]
        text = (f"<b>{html.escape(title)}</b>\n{c['sha'][:7]} • {html.escape(author.get('name', '?'))} • "
                f"{author.get('date', '')[:10]}\n{len(files)} files\n"
                + html.escape("\n".join(flines)) + f"\n<pre>{esc}</pre>")
        return await show(update, text, [[B("⬅️ Commits", f"commits:{arg}")]], html_mode=True)

    if act == "br":
        ud.pop("pending", None)
        return await show_branches(update, context, name)

    if act == "bnew":
        ud["pending"] = {"action": "newbranch", "repo": name}
        return await show(update, f"➕ Nayi branch ka naam bhejo.\n'{br or 'default'}' se banegi.",
                          [[B("❌ Cancel", f"br:{arg}")]])

    if act == "sr":
        ud["pending"] = {"action": "search", "repo": name}
        return await show(update, "🔎 Kya dhundna hai? File ka naam ya code ka text bhejo.", back)

    if act == "vis":
        r = await to(gh, "GET", rp(name))
        if r["private"]:  # private -> public: pehle confirm
            return await show(
                update, f"⚠️ '{name}' public hoga, sab log dekh payenge. Pakka?",
                [[B("✅ Haan, public karo", f"visy:{arg}")], [B("❌ Cancel", f"repo:{arg}")]],
            )
        await to(gh, "PATCH", rp(name), json={"private": True})
        return await show_repo(update, context, name, "✅ Ab private hai\n\n")

    if act == "visy":
        await to(gh, "PATCH", rp(name), json={"private": False})
        return await show_repo(update, context, name, "✅ Ab public hai\n\n")

    if act == "arch":
        r = await to(gh, "GET", rp(name))
        await to(gh, "PATCH", rp(name), json={"archived": not r["archived"]})
        return await show_repo(update, context, name,
                               "✅ Unarchive ho gaya\n\n" if r["archived"] else "✅ Archive ho gaya\n\n")

    if act == "ren":
        ud["pending"] = {"action": "rename", "repo": name}
        return await show(update, f"✏️ '{name}' ka naya naam text me bhejo.", back)

    if act == "desc":
        ud["pending"] = {"action": "desc", "repo": name}
        return await show(update, f"📝 '{name}' ka naya description bhejo.\n(Hatane ke liye sirf - bhejo)", back)

    if act == "upd":
        return await show(
            update,
            f"⬆️ '{name}' ({br or 'default'} branch) ko zip se update karna hai.\n\n"
            "➕ Merge: zip ki files add/overwrite hongi, baaki purani rahengi.\n"
            "♻️ Replace: purani saari files hat ke sirf zip ki files rahengi.",
            [[B("➕ Merge", f"updm:{arg}:merge"), B("♻️ Replace", f"updm:{arg}:replace")],
             [B("❌ Cancel", f"repo:{arg}")]],
        )

    if act == "updm":
        ud["pending"] = {"action": "update", "repo": name, "replace": parts[2] == "replace"}
        return await show(update, f"📤 Ab '{name}' ke liye zip file bhejo.", back)

    if act == "dl":
        await update.callback_query.answer("Zip la raha hu...")
        data = await to(download_zip, name, br)
        if len(data) > MAX_SEND_BYTES:
            return await show(update, "❌ Zip 50 MB se badi hai, Telegram se nahi bhej sakta.", back)
        await context.bot.send_document(update.effective_chat.id, io.BytesIO(data), filename=f"{name}.zip")
        return

    if act == "del":
        ud["pending"] = {"action": "delete", "repo": name}
        return await show(
            update,
            f"🗑 '{name}' delete karna hai?\n\n⚠️ Ye undo nahi hota.\n"
            f"Confirm karne ke liye repo ka naam bilkul aise type karke bhejo:\n\n{name}",
            [[B("❌ Cancel", f"repo:{arg}")]],
        )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.callback_query.answer()
    try:
        await route(update, context, update.callback_query.data.split(":"))
    except Exception as e:
        await show(update, f"❌ Error: {e}", HOME)


# ═════════════════════════ Text input (pending actions) ═════════════════════════
FILE_ACTIONS = ("update", "upload", "replace")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    ud = context.user_data
    p = ud.get("pending")
    if not p:
        return await send_menu(update)
    act, name = p["action"], p["repo"]
    if act in FILE_ACTIONS:
        return await update.message.reply_text("📤 Yaha file (document) chahiye, text nahi. /cancel se ruk sakte ho.")
    raw = update.message.text
    text = raw.strip()
    br = cur_branch(context, name)
    ud.pop("pending", None)
    try:
        if act == "rename":
            new = clean_name(text)
            await to(gh, "PATCH", rp(name), json={"name": new})
            await show_repo(update, context, new, f"✅ Rename ho gaya: {new}\n\n")

        elif act == "desc":
            await to(gh, "PATCH", rp(name), json={"description": "" if text == "-" else text})
            await show_repo(update, context, name, "✅ Description update ho gaya\n\n")

        elif act == "delete":
            if text != name:
                return await show(update, "❌ Naam match nahi hua, delete cancel kar diya.", HOME)
            try:
                await to(gh, "DELETE", rp(name))
            except RuntimeError as e:
                hint = ("\n\n💡 Token me 'delete_repo' scope tick karke naya token banao."
                        if "403" in str(e) or "admin" in str(e).lower() else "")
                return await show(update, f"❌ {e}{hint}", HOME)
            await show(update, f"🗑 '{name}' delete ho gaya.",
                       [[B("📂 Repos", "list:1"), B("🏠 Menu", "menu")]])

        elif act == "newbranch":
            new = re.sub(r"[^A-Za-z0-9._/-]+", "-", text).strip("-/.")
            if not new:
                raise ValueError("Branch ka naam galat hai")
            await to(create_branch, name, new, br)
            set_cur_branch(context, name, new)
            await show_branches(update, context, name, f"✅ '{new}' ban gayi aur ab use ho rahi hai\n\n")

        elif act == "search":
            names, hits = await to(search_repo, name, text, br)
            paths = list(dict.fromkeys(names + hits))[:15]
            rk = rid(context, name)
            if not paths:
                return await show(update, f"🔎 '{text}': kuch nahi mila.",
                                  [[B("🔎 Dubara", f"sr:{rk}"), B("⬅️ Repo", f"repo:{rk}")]])
            rows = [[B(f"📄 {pth}"[:60], f"fv:{pid(context, name, pth)}:0")] for pth in paths]
            rows.append([B("🔎 Dubara search", f"sr:{rk}"), B("⬅️ Repo", f"repo:{rk}")])
            await show(update, f"🔎 '{text}'\n{len(names)} naam match • {len(hits)} code match", rows)

        elif act == "newfile":
            fname, _, content = raw.partition("\n")
            path = join_path(p["dir"], fname.strip())
            await to(create_file_new, name, path, content.encode(), f"Create {path} via Telegram bot", br)
            await show_file(update, context, name, path, 0, "✅ File ban gayi\n\n")

        elif act in ("edit_lines", "edit_find", "edit_full"):
            path = p["path"]
            if act == "edit_full":
                data, line, note = raw.encode(), 0, "✅ Content update ho gaya\n\n"
            else:
                src = (await to(read_file, name, path, br))[0].decode("utf-8")
                if act == "edit_lines":
                    newsrc, line = apply_line_edit(src, raw)
                    note = "✅ Line edit commit ho gaya (edit mode ON, agla command bhejo)\n\n"
                else:
                    newsrc, n = apply_find_replace(src, raw)
                    line, note = 0, f"✅ {n} jagah replace hua (edit mode ON)\n\n"
                data = newsrc.encode()
            await to(upsert_file, name, path, data, f"Edit {path} via Telegram bot", br)
            if act != "edit_full":
                ud["pending"] = p  # edit mode chalu rakho
            await show_file(update, context, name, path, line // LINES_PER_PAGE, note)

        elif act == "move":
            new = norm_path(text)
            n = await to(move_path, name, p["path"], new, br)
            if p.get("is_dir"):
                await show_dir(update, context, name, new, 0, f"✅ Move ho gaya ({n} files)\n\n")
            else:
                await show_file(update, context, name, new, 0, "✅ Move/Rename ho gaya\n\n")

    except Exception as e:
        if act in ("edit_lines", "edit_find", "edit_full"):
            ud["pending"] = p  # galti ho to dobara try kar sako
        await show(update, f"❌ {e}", HOME)


# ═════════════════════════ Commands ═════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update):
        context.user_data.pop("pending", None)
        await send_menu(update)


async def cmd_repos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update):
        try:
            await show_list(update, context, 1)
        except Exception as e:
            await show(update, f"❌ Error: {e}", HOME)


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        return await show(update, "Use: /open repo-naam", HOME)
    try:
        await show_repo(update, context, context.args[0])
    except Exception as e:
        await show(update, f"❌ {e}", HOME)


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.args:
        return await show(update, "Use: /delete repo-naam", HOME)
    name = context.args[0]
    try:
        await to(gh, "GET", rp(name))
    except Exception as e:
        return await show(update, f"❌ {e}", HOME)
    context.user_data["pending"] = {"action": "delete", "repo": name}
    await show(update, f"🗑 '{name}' delete karne ke liye naam type karke bhejo:\n\n{name}\n\n/cancel se ruk sakte ho.")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update):
        await show(update, await to(token_status), HOME)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update):
        await show(update, HELP_TEXT, HOME)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update):
        context.user_data.pop("pending", None)
        await send_menu(update, "Cancel ho gaya.\n\n")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Teri Telegram ID: {update.effective_user.id}")


# ═════════════════════════ File / zip uploads ═════════════════════════
async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    doc = update.message.document
    fname = doc.file_name or "file"
    ud = context.user_data
    p = ud.get("pending")

    # ----- single file upload / replace / edit_full via file -----
    if p and p["action"] in ("upload", "replace", "edit_full"):
        ud.pop("pending", None)
        if doc.file_size and doc.file_size > MAX_ZIP_BYTES:
            return await update.message.reply_text("File 20 MB se badi hai, Telegram bot download nahi kar sakta.")
        msg = await update.message.reply_text("⏳ Upload ho raha hai...")
        try:
            repo = p["repo"]
            br = cur_branch(context, repo)
            tg_file = await doc.get_file()
            data = bytes(await tg_file.download_as_bytearray())
            path = join_path(p["dir"], fname) if p["action"] == "upload" else p["path"]
            await to(upsert_file, repo, path, data, f"Upload {path} via Telegram bot", br)
            await msg.edit_text("✅ Upload ho gaya")
            await show_file(update, context, repo, path, 0)
        except Exception as e:
            await msg.edit_text(f"❌ Error: {e}")
        return

    # ----- zip: naya repo ya existing repo update -----
    if not fname.lower().endswith(".zip"):
        return await update.message.reply_text(
            "Naya repo ke liye .zip bhejo. Single file upload ke liye pehle Explorer me 📤 Upload dabao.")
    if doc.file_size and doc.file_size > MAX_ZIP_BYTES:
        return await update.message.reply_text("Zip 20 MB se badi hai, Telegram bot itni download nahi kar sakta.")

    ud.pop("pending", None)
    updating = bool(p and p["action"] == "update")

    words = (update.message.caption or "").split()
    lw = [w.lower() for w in words]
    if "private" in lw:
        private = True
    elif "public" in lw:
        private = False
    else:
        private = context.bot_data.get("default_private", False)
    words = [w for w in words if w.lower() not in ("public", "private")]
    if words:
        name = clean_name(" ".join(words))
    else:
        name = clean_name(re.sub(r"-(master|main)$", "", fname[:-4], flags=re.I))

    msg = await update.message.reply_text(
        f"⏳ '{p['repo']}' update ho raha hai..." if updating else f"⏳ '{name}' ban raha hai..."
    )
    try:
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        files = await to(read_zip, data)
        if updating:
            url = await to(update_from_zip, p["repo"], files, p["replace"], cur_branch(context, p["repo"]))
            text = f"✅ '{p['repo']}' update ho gaya ({len(files)} files)\n{url}"
        else:
            url, final = await to(create_from_zip, name, private, files)
            text = f"✅ Ho gaya: {final} ({len(files)} files, {'🔒 private' if private else '🌐 public'})\n{url}"
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(
            [[B("📂 Repos", "list:1"), B("🏠 Menu", "menu")]]))
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


# ═════════════════════════ Run bot inside Streamlit ═════════════════════════
async def post_init(app):
    await app.bot.set_my_commands([
        ("menu", "Main menu"),
        ("repos", "Mere repos"),
        ("open", "Repo kholo: /open naam"),
        ("delete", "Repo delete karo"),
        ("check", "Token check"),
        ("id", "Meri Telegram ID"),
        ("cancel", "Cancel"),
        ("help", "Help"),
    ])


def run_bot():
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TG_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("repos", cmd_repos))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(stop_signals=None, close_loop=False)


@st.cache_resource
def start_bot_once():
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    return t


start_bot_once()
st.title("Telegram -> GitHub Manager Bot")
st.write("Bot chal raha hai. Telegram par /menu kholo.")
if not ALLOWED_USER_ID:
    st.warning("ALLOWED_USER_ID set nahi hai. Bot ko /id bhejo aur wo ID Secrets me daalo.")
