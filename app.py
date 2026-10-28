import os
import re
import io
import base64
import time
import zipfile
import asyncio
import threading

import requests
import streamlit as st
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters


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
_uid = secret("ALLOWED_USER_ID")  # optional: set karoge to sirf aap use kar paoge
ALLOWED_USER_ID = int(_uid) if _uid else None

API = "https://api.github.com"
HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
MAX_ZIP_BYTES = 20 * 1024 * 1024  # Telegram bot download limit
MAX_FILES = 500
SKIP = ("__MACOSX/", ".DS_Store", ".git/")


def gh(method, path, **kwargs):
    r = requests.request(method, f"{API}{path}", headers=HEADERS, timeout=60, **kwargs)
    if r.status_code >= 400:
        try:
            j = r.json()
            detail = "; ".join(
                e.get("message", str(e)) if isinstance(e, dict) else str(e)
                for e in j.get("errors", [])
            )
            msg = f"{j.get('message', '')} {detail}".strip() + f"\nRAW: {r.text[:400]}"
        except Exception:
            msg = r.text
        raise RuntimeError(f"GitHub {r.status_code}: {msg}")
    return r.json()


def clean_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return name or "new-repo"


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
    # agar sab kuch ek hi top folder ke andar hai to wo folder hata do
    tops = {p.split("/")[0] for p in files}
    if len(tops) == 1 and all("/" in p for p in files):
        files = {p.split("/", 1)[1]: c for p, c in files.items()}
    return files


def create_repo_and_push(name, private, files):
    repo = None
    for i in range(1, 8):
        candidate = name if i == 1 else f"{name}-{i}"
        try:
            repo = gh("POST", "/user/repos", json={"name": candidate, "private": private, "auto_init": True})
            break
        except RuntimeError as e:
            if "already exists" in str(e) and i < 7:
                continue
            raise
    full, branch = repo["full_name"], repo["default_branch"]

    ref = None
    for _ in range(10):  # auto_init ke baad branch ready hone me thoda time lagta hai
        try:
            ref = gh("GET", f"/repos/{full}/git/ref/heads/{branch}")
            break
        except RuntimeError:
            time.sleep(1)
    if not ref:
        raise RuntimeError("Repo bana par branch ready nahi hui, thodi der baad dekho")

    parent = ref["object"]["sha"]
    base_tree = gh("GET", f"/repos/{full}/git/commits/{parent}")["tree"]["sha"]

    tree = []
    for path, content in files.items():
        blob = gh("POST", f"/repos/{full}/git/blobs",
                  json={"content": base64.b64encode(content).decode(), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    new_tree = gh("POST", f"/repos/{full}/git/trees", json={"base_tree": base_tree, "tree": tree})
    commit = gh("POST", f"/repos/{full}/git/commits",
                json={"message": "Upload via Telegram bot", "tree": new_tree["sha"], "parents": [parent]})
    gh("PATCH", f"/repos/{full}/git/refs/heads/{branch}", json={"sha": commit["sha"]})
    return repo["html_url"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "Zip bhejo, repo ban jayega.\n\n"
        "Caption me kuch likhoge to wo repo ka naam hoga, warna zip ka naam.\n"
        "Caption me 'private' likhoge to repo private banega (default public)."
    )


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    r = await asyncio.to_thread(lambda: requests.get(f"{API}/user", headers=HEADERS, timeout=30))
    scopes = r.headers.get("X-OAuth-Scopes", "(nahi mila - fine-grained token?)")
    login = r.json().get("login") if r.ok else None
    await update.message.reply_text(
        f"GitHub status: {r.status_code}\nuser: {login}\nscopes: {scopes}"
        + ("" if r.ok else f"\n{r.text[:300]}")
    )


async def handle_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    doc = update.message.document
    if not doc.file_name.lower().endswith(".zip"):
        await update.message.reply_text("Sirf .zip file bhejo.")
        return
    if doc.file_size and doc.file_size > MAX_ZIP_BYTES:
        await update.message.reply_text("Zip 20 MB se badi hai, Telegram bot itni download nahi kar sakta.")
        return

    caption = (update.message.caption or "").strip()
    words = caption.split()
    private = "private" in [w.lower() for w in words]
    words = [w for w in words if w.lower() not in ("public", "private")]
    if words:
        name = clean_name(" ".join(words))
    else:
        name = clean_name(re.sub(r"-(master|main)$", "", doc.file_name[:-4], flags=re.I))

    msg = await update.message.reply_text(f"'{name}' bana raha hu...")
    try:
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        files = await asyncio.to_thread(read_zip, data)
        url = await asyncio.to_thread(create_repo_and_push, name, private, files)
        await msg.edit_text(f"Ho gaya ({len(files)} files, {'private' if private else 'public'})\n{url}")
    except Exception as e:
        err = str(e)
        if "already exists" in err:
            err += "\nIs naam ka repo pehle se hai. Caption me dusra naam likho."
        await msg.edit_text(f"Error: {err}")


def run_bot():
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_zip))
    app.run_polling(stop_signals=None, close_loop=False)


@st.cache_resource
def start_bot_once():
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    return t


start_bot_once()
st.title("Telegram -> GitHub Zip Bot")
st.write("Bot chal raha hai. Telegram par zip bhejo.")
