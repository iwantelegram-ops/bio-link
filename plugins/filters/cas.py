"""
plugins/filters/cas.py
──────────────────────
Filter CAS (Combot Anti-Spam):
  - Auto-ban user yang ada di database spammer global CAS
  - Kirim sambutan saat bot masuk grup baru
  - Perintah /wlcas dan /unwlcas untuk whitelist per grup

PINTU BERURUTAN:
  Jika CAS mendeteksi dan mem-ban user → mark_message_handled(cid, mid)
  sebelum memasukkan ke delete_queue, sehingga bio, antispam, dan nexus
  tidak memproses pesan yang sama.

VIP:
  User yang terdaftar di free_per_group sepenuhnya dilewati — tidak dicek
  sama sekali, tidak ada ban, tidak ada log.
"""

import os
import httpx
import time
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode

from database import (
    db, auto_delete_reply, is_admin, delete_queue,
    update_config, save_group_title, remove_group_data,
    TZ_WIB, mark_message_handled, insert_group_action_log,
)

DELAY_NOTIF   = 10
LOG_CHANNEL   = int(os.environ.get("LOG_CHANNEL", 0))
whitelist_col = db["whitelist_per_group"]
free_col      = db["free_per_group"]

_cas_cache: dict[int, tuple[bool, float]] = {}
CAS_TTL = 43200  # 12 jam


# ── CAS API ───────────────────────────────────────────────────────────────────
async def is_cas_banned(user_id: int) -> bool:
    now = time.monotonic()
    hit = _cas_cache.get(user_id)
    if hit and (now - hit[1]) < CAS_TTL:
        return hit[0]
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"https://api.cas.chat/check?user_id={user_id}")
            is_banned = r.status_code == 200 and r.json().get("ok", False)
            _cas_cache[user_id] = (is_banned, now)
            return is_banned
    except Exception:
        return False


def _resolve_target(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if len(message.command) > 1:
        try:
            return int(message.command[1])
        except ValueError:
            pass
    return None


# ── /wlcas ──────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("wlcas") & filters.group)
async def add_whitelist(client: Client, message: Message):
    cid = message.chat.id
    if not await is_admin(client, cid, message.from_user.id if message.from_user else None):
        return

    target_id = _resolve_target(message)
    if target_id is None:
        res = await message.reply(
            "⚠️ <b>Target Tidak Ditemukan!</b>\n\n"
            "📌 <b>Cara penggunaan:</b>\n"
            "① Reply pesan user → <code>/wlcas</code>\n"
            "② Kirim ID langsung → <code>/wlcas 123456789</code>",
            parse_mode=ParseMode.HTML
        )
        return await auto_delete_reply([res, message], delay=DELAY_NOTIF)

    await whitelist_col.update_one(
        {"user_id": target_id, "chat_id": cid},
        {"$set": {"status": "whitelisted"}},
        upsert=True,
    )
    res = await message.reply(
        f"✅ <b>Whitelist CAS Berhasil!</b>\n\n"
        f"👤 <b>User ID:</b> <code>{target_id}</code>\n\n"
        f"<i>User ini tidak akan ter-ban oleh CAS di grup ini.</i>",
        parse_mode=ParseMode.HTML
    )
    await auto_delete_reply([res, message], delay=DELAY_NOTIF)


# ── /unwlcas ─────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("unwlcas") & filters.group)
async def remove_whitelist(client: Client, message: Message):
    cid = message.chat.id
    if not await is_admin(client, cid, message.from_user.id if message.from_user else None):
        return

    target_id = _resolve_target(message)
    if target_id is None:
        res = await message.reply(
            "⚠️ <b>Target Tidak Ditemukan!</b>\n\n"
            "📌 <b>Cara penggunaan:</b>\n"
            "① Reply pesan user → <code>/unwlcas</code>\n"
            "② Kirim ID langsung → <code>/unwlcas 123456789</code>",
            parse_mode=ParseMode.HTML
        )
        return await auto_delete_reply([res, message], delay=DELAY_NOTIF)

    result = await whitelist_col.delete_one({"user_id": target_id, "chat_id": cid})
    text = (
        f"🗑️ <b>Un-Whitelist Berhasil!</b>\n\n"
        f"👤 <b>User ID:</b> <code>{target_id}</code> sekarang kembali dicek oleh CAS."
    ) if result.deleted_count else (
        f"❌ <b>ID Tidak Ada di Whitelist!</b>\n\n"
        f"👤 <b>User ID:</b> <code>{target_id}</code>"
    )
    res = await message.reply(text, parse_mode=ParseMode.HTML)
    await auto_delete_reply([res, message], delay=DELAY_NOTIF)


# ── CAS Auto-Mod ──────────────────────────────────────────────────────────────
@Client.on_message(filters.group & ~filters.service, group=-1)
async def cas_auto_mod(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return

    uid     = message.from_user.id
    cid     = message.chat.id
    mid     = message.id

    if await whitelist_col.find_one({"user_id": uid, "chat_id": cid}):
        return

    # VIP: bebas dari semua filter — tidak ada ban, tidak ada log
    if await free_col.find_one({"user_id": uid, "chat_id": cid}):
        return

    if await is_admin(client, cid, uid):
        return

    if await is_cas_banned(uid):
        try:
            await client.ban_chat_member(cid, uid)
            mark_message_handled(cid, mid)
            await delete_queue.put((cid, [mid]))

            waktu = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")

            # Notifikasi publik ke grup
            alert = await client.send_message(
                cid,
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃      🛡️  <b>CAS ANTI-SPAM</b>       ┃\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"🚫 <b>User terdeteksi & di-ban otomatis!</b>\n\n"
                f"👤 <b>User:</b> {message.from_user.mention}\n"
                f"🆔 <b>ID:</b> <code>{uid}</code>\n\n"
                f"⚠️ <b>Alasan:</b> Terdeteksi di database spammer global CAS.\n\n"
                f"<i>CAS memproteksi dari 200.000+ spammer yang sudah diverifikasi.</i>",
                parse_mode=ParseMode.HTML
            )
            await auto_delete_reply([alert], delay=DELAY_NOTIF)

            # Log ke channel owner
            await _log_cas_ban(client, message, waktu)
            # Log ke per-grup action log
            try:
                await insert_group_action_log(
                    cid, "BAN",
                    "Ban otomatis CAS – spammer global terverifikasi",
                    uid,
                    message.from_user.first_name or str(uid),
                    (message.text or message.caption or "")[:100],
                )
            except Exception:
                pass

        except Exception as e:
            print(f"⚠️  CAS-Error [chat={cid}]: {e}")


async def _log_cas_ban(client: Client, message: Message, waktu: str):
    """Kirim log ban CAS ke LOG_CHANNEL dengan format seragam."""
    if not LOG_CHANNEL:
        return

    uid          = message.from_user.id
    cid          = message.chat.id
    user_mention = f"<a href='tg://user?id={uid}'>{message.from_user.first_name}</a>"
    content      = (message.text or message.caption or "—").strip()

    log_text = (
        "<b>❖ CAS ANTI-SPAM ❖</b>\n"
        "🚫 <b>User Di-Ban — Spammer Global Terverifikasi</b>\n"
        "<blockquote expandable>"
        f"◈ <b>User:</b> {user_mention} (<code>{uid}</code>)\n"
        f"◈ <b>Grup:</b> {message.chat.title} (<code>{cid}</code>)\n"
        f"◈ <b>Waktu:</b> {waktu}\n"
        "◈ <b>Sumber:</b> Database CAS (cas.chat)\n"
        "◈ <b>Aksi:</b> Ban permanen + hapus pesan\n\n"
        f"<b>Konten:</b> <code>{content[:500]}</code>"
        "</blockquote>"
    )
    try:
        await client.send_message(
            LOG_CHANNEL, log_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[CAS LOG ERROR] {e}")


# ── Bot masuk grup → init config ──────────────────────────────────────────────
@Client.on_message(filters.service, group=0)
async def handle_bot_join(client: Client, message: Message):
    if not message.new_chat_members:
        return
    for member in message.new_chat_members:
        if member.id == (await client.get_me()).id:
            await update_config(message.chat.id, "local", True)
            try:
                chat = await client.get_chat(message.chat.id)
                await save_group_title(message.chat.id, chat.title or str(message.chat.id))
            except Exception:
                pass
            await message.reply(
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃   🛡️  <b>BOT ANTI-GCAST AKTIF!</b>   ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                "✅ <b>Bot berhasil bergabung dan aktif!</b>\n\n"
                "━━━ 🔰 <b>PROTEKSI AKTIF</b> ━━━\n\n"
                "🔁 Anti Spam Lokal    → <code>🟢 ON</code>\n"
                "🌐 Anti Gcast Global  → <code>🟢 ON</code>\n"
                "🔍 Bio Link Detector  → <code>🔴 OFF</code>\n"
                "🛡️ CAS Anti-Spam      → <code>🟢 SELALU</code>\n\n"
                "━━━ ⚙️ <b>KONFIGURASI</b> ━━━\n\n"
                "<i>Gunakan <code>/antigcast</code> untuk panel kontrol interaktif via DM.</i>",
                parse_mode=ParseMode.HTML
            )


# ── Pantau perubahan status bot di grup ──────────────────────────────────────
@Client.on_chat_member_updated()
async def handle_bot_status_change(client: Client, update):
    try:
        me = await client.get_me()
        if not update.new_chat_member or update.new_chat_member.user.id != me.id:
            return

        from pyrogram.enums import ChatMemberStatus
        new_status = update.new_chat_member.status
        chat_id    = update.chat.id

        if new_status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
            await remove_group_data(chat_id)

        elif new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
            await update_config(chat_id, "local", True)
            try:
                chat = await client.get_chat(chat_id)
                await save_group_title(chat_id, chat.title or str(chat_id))
            except Exception:
                pass

    except Exception as e:
        print(f"[handle_bot_status_change] {e}")


# ── Nama grup berubah → perbarui title di database ───────────────────────────
@Client.on_message(filters.group & filters.service)
async def handle_chat_title_change(client: Client, message: Message):
    try:
        if message.new_chat_title:
            await save_group_title(message.chat.id, message.new_chat_title)
    except Exception as e:
        print(f"[handle_chat_title_change] {e}")
