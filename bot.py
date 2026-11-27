import os, re, asyncio, uuid
from collections import OrderedDict
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait, UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton as Btn
from motor.motor_asyncio import AsyncIOMotorClient

# ---------- CONFIG ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
DB_CHANNEL = int(os.environ["DB_CHANNEL"])
ADMINS = [int(x) for x in os.environ.get("ADMINS", "").split()]
FSUB = os.environ.get("FSUB_CHANNEL", "").strip()      # @username ya -100id (optional)
FSUB_LINK = os.environ.get("FSUB_LINK", "").strip()    # join link (optional)
AUTO_DELETE = int(os.environ.get("AUTO_DELETE", 600))  # seconds, 0 = off
CAPTION = os.environ.get("CAPTION", "")                # extra caption (optional)
PORT = int(os.environ.get("PORT", 8080))
PER_PAGE = 8
if FSUB.lstrip("-").isdigit():
    FSUB = int(FSUB)

app = Client("autofilter", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
db = AsyncIOMotorClient(MONGO_URI)["moviebot"]
files, users = db["files"], db["users"]
cache = OrderedDict()  # key -> (user_id, query)


# ---------- HELPERS ----------
def fmt_size(b):
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}TB"


def pattern(q):
    words = [re.escape(w) for w in re.split(r"[\s._-]+", q) if w]
    return "".join(f"(?=.*{w})" for w in words)


async def save_user(uid):
    await users.update_one({"_id": uid}, {"$setOnInsert": {"banned": False}}, upsert=True)


async def is_banned(uid):
    u = await users.find_one({"_id": uid})
    return bool(u and u.get("banned"))


async def joined(uid):
    if not FSUB:
        return True
    try:
        await app.get_chat_member(FSUB, uid)
        return True
    except UserNotParticipant:
        return False
    except Exception:
        return True  # bot admin nahi hai to block mat karo


def join_link():
    if FSUB_LINK:
        return FSUB_LINK
    return f"https://t.me/{str(FSUB).lstrip('@')}"


async def delete_later(msg, sec):
    if sec <= 0:
        return
    await asyncio.sleep(sec)
    try:
        await msg.delete()
    except Exception:
        pass


async def deliver(uid, msg_id):
    doc = await files.find_one({"msg_id": msg_id})
    name = doc["name"] if doc else "File"
    cap = f"📁 <b>{name}</b>"
    if CAPTION:
        cap += f"\n\n{CAPTION}"
    if AUTO_DELETE:
        cap += f"\n\n⏳ Ye file {AUTO_DELETE // 60} min me delete ho jayegi. Save kar lo."
    sent = await app.copy_message(uid, DB_CHANNEL, msg_id, caption=cap)
    asyncio.create_task(delete_later(sent, AUTO_DELETE))


async def build_page(key, page):
    uid, q = cache[key]
    flt = {"name": {"$regex": pattern(q), "$options": "i"}}
    total = await files.count_documents(flt)
    rows = await files.find(flt).sort("name", 1).skip(page * PER_PAGE).limit(PER_PAGE).to_list(PER_PAGE)
    btns = [[Btn(f"[{fmt_size(r['size'])}] {r['name'][:48]}", callback_data=f"f_{r['msg_id']}")] for r in rows]
    pages = max(1, -(-total // PER_PAGE))
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Back", callback_data=f"pg_{key}_{page - 1}"))
    nav.append(Btn(f"{page + 1}/{pages}", callback_data="noop"))
    if (page + 1) * PER_PAGE < total:
        nav.append(Btn("Next ➡️", callback_data=f"pg_{key}_{page + 1}"))
    btns.append(nav)
    return f"🎬 <b>{q}</b> — {total} result(s)", InlineKeyboardMarkup(btns), total


# ---------- INDEXING ----------
@app.on_message(filters.chat(DB_CHANNEL) & (filters.document | filters.video))
async def index_file(_, m):
    media = m.document or m.video
    name = media.file_name or (m.caption or "unknown")[:60]
    await files.update_one(
        {"msg_id": m.id},
        {"$set": {"msg_id": m.id, "name": name, "size": media.file_size}},
        upsert=True,
    )


# ---------- USER ----------
@app.on_message(filters.command("start") & filters.private)
async def start(_, m):
    uid = m.from_user.id
    await save_user(uid)
    if await is_banned(uid):
        return await m.reply("🚫 Tum banned ho.")
    if len(m.command) > 1 and m.command[1].startswith("file_"):
        if not await joined(uid):
            btn = [[Btn("📢 Join Channel", url=join_link())],
                   [Btn("🔄 Try Again", url=f"https://t.me/{app.me.username}?start={m.command[1]}")]]
            return await m.reply("Pehle channel join karo 👇", reply_markup=InlineKeyboardMarkup(btn))
        return await deliver(uid, int(m.command[1][5:]))
    await m.reply("Movie ka naam bhejo, main file de dunga 🎬")


@app.on_message(filters.text & ~filters.regex(r"^/") & (filters.private | filters.group))
async def search(_, m):
    if not m.from_user:
        return
    uid, q = m.from_user.id, m.text.strip()
    if len(q) < 3 or await is_banned(uid):
        return
    await save_user(uid)
    key = uuid.uuid4().hex[:8]
    cache[key] = (uid, q)
    if len(cache) > 500:
        cache.popitem(last=False)
    text, markup, total = await build_page(key, 0)
    if total == 0:
        if m.chat.type.name == "PRIVATE":
            await m.reply("Kuch nahi mila 😕 spelling check karo.")
        return
    r = await m.reply(text, reply_markup=markup, quote=True)
    if m.chat.type.name != "PRIVATE":
        asyncio.create_task(delete_later(r, AUTO_DELETE))


@app.on_callback_query(filters.regex(r"^pg_"))
async def paginate(_, cq):
    _, key, page = cq.data.split("_")
    if key not in cache:
        return await cq.answer("Search purani ho gayi, dobara search karo.", show_alert=True)
    if cache[key][0] != cq.from_user.id:
        return await cq.answer("Ye tumhari search nahi hai 😅", show_alert=True)
    text, markup, _ = await build_page(key, int(page))
    await cq.message.edit_text(text, reply_markup=markup)


@app.on_callback_query(filters.regex(r"^f_\d+$"))
async def send_file(_, cq):
    uid, msg_id = cq.from_user.id, int(cq.data[2:])
    if await is_banned(uid):
        return await cq.answer("Tum banned ho.", show_alert=True)
    link = f"https://t.me/{app.me.username}?start=file_{msg_id}"
    if not await joined(uid):
        btn = [[Btn("📢 Join Channel", url=join_link())], [Btn("🔄 Try Again", url=link)]]
        try:
            await app.send_message(uid, "Pehle channel join karo 👇", reply_markup=InlineKeyboardMarkup(btn))
            return await cq.answer("PM check karo — pehle channel join karo.", show_alert=True)
        except Exception:
            return await cq.answer("Pehle bot ko PM me /start karo, aur channel join karo.", show_alert=True)
    try:
        await deliver(uid, msg_id)
        await cq.answer("File bhej di ✅ PM check karo")
    except Exception:
        await cq.answer("Pehle bot ko PM me /start karo, phir button dabao.", show_alert=True)


@app.on_callback_query(filters.regex(r"^noop$"))
async def noop(_, cq):
    await cq.answer()


# ---------- ADMIN ----------
admin = filters.user(ADMINS)


@app.on_message(filters.command("stats") & admin)
async def stats(_, m):
    await m.reply(f"👥 Users: {await users.count_documents({})}\n📁 Files: {await files.count_documents({})}")


@app.on_message(filters.command("ban") & admin)
async def ban(_, m):
    if len(m.command) < 2:
        return await m.reply("/ban user_id")
    await users.update_one({"_id": int(m.command[1])}, {"$set": {"banned": True}}, upsert=True)
    await m.reply("Banned ✅")


@app.on_message(filters.command("unban") & admin)
async def unban(_, m):
    if len(m.command) < 2:
        return await m.reply("/unban user_id")
    await users.update_one({"_id": int(m.command[1])}, {"$set": {"banned": False}})
    await m.reply("Unbanned ✅")


@app.on_message(filters.command("delete") & admin)
async def delete(_, m):
    if len(m.command) < 2:
        return await m.reply("/delete file name (index se hata dega)")
    q = " ".join(m.command[1:])
    res = await files.delete_many({"name": {"$regex": pattern(q), "$options": "i"}})
    await m.reply(f"{res.deleted_count} file(s) index se hata di ✅")


@app.on_message(filters.command("broadcast") & admin & filters.reply)
async def broadcast(_, m):
    ok = fail = 0
    status = await m.reply("Broadcast shuru...")
    async for u in users.find({"banned": {"$ne": True}}):
        try:
            await m.reply_to_message.copy(u["_id"])
            ok += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            fail += 1
    await status.edit_text(f"Done ✅ Sent: {ok} | Failed: {fail}")


# ---------- WEB (Render free ke liye) ----------
async def web_server():
    r = web.Application()
    r.add_routes([web.get("/", lambda req: web.Response(text="alive"))])
    runner = web.AppRunner(r)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()


async def main():
    await web_server()
    await app.start()
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
