"""
plugins/commands/antigcast_group.py
─────────────────────────────────────
Handler /antigcast yang dikirim di GRUP maupun DM.
Mengirim panel kontrol ke DM user, dengan cooldown per grup.

Perubahan (v2 — anti-spam):
  - Rate limiting per-user di DM (/start & /antigcast):
      · Cooldown 10 detik per user — cegah spam DM command.
  - Rate limiting per-grup DAN per-user di grup:
      · Per-grup: cooldown 15 detik (seperti sebelumnya).
      · Per-user: cooldown 30 detik — cegah satu user spam di banyak grup.
  - /start & /antigcast di DM dipindahkan ke sini (dari handlers_dm.py).
  - Handler DM juga menerima cooldown sehingga tidak bisa di-spam.
"""

import asyncio
import time
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from pyrogram.errors import UserIsBlocked, PeerIdInvalid, InputUserDeactivated

# ── Cooldown state ─────────────────────────────────────────────────────────────
# Per-grup: kapan grup boleh lagi menerima /antigcast
_group_cooldown: dict[int, float] = {}
# Per-user: kapan user boleh lagi kirim /antigcast (mencakup semua grup + DM)
_user_cooldown:  dict[int, float] = {}

GROUP_CD_SEC = 15   # detik cooldown per grup
USER_CD_SEC  = 30   # detik cooldown per user (anti-spam lintas chat)
DM_CD_SEC    = 10   # detik cooldown command DM per user


async def _auto_delete(*messages: Message, delay: int = 15):
    await asyncio.sleep(delay)
    for msg in messages:
        try:
            await msg.delete()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  /start & /antigcast di DM
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command(["start", "antigcast"]) & filters.private)
async def antigcast_dm_handler(client: Client, message: Message):
    user = message.from_user
    if not user:
        return

    now    = time.monotonic()
    uid    = user.id
    dm_key = (uid, "dm")

    # Rate limit DM — cegah spam command di chat pribadi
    if now < _user_cooldown.get(dm_key, 0):
        try:
            await message.delete()
        except Exception:
            pass
        return

    _user_cooldown[dm_key] = now + DM_CD_SEC

    try:
        from plugins.ui.pages import page_start
        text, keyboard = await page_start(client)
        await message.reply(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[antigcast_dm] Error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  /antigcast di GRUP
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("antigcast") & filters.group)
async def antigcast_group_handler(client: Client, message: Message):
    chat_id = message.chat.id
    user    = message.from_user
    if not user:
        return

    now     = time.monotonic()
    user_id = user.id

    # Rate limit 1: per-grup
    if now < _group_cooldown.get(chat_id, 0):
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Rate limit 2: per-user (cegah spam di banyak grup sekaligus)
    if now < _user_cooldown.get(user_id, 0):
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Set kedua cooldown
    _group_cooldown[chat_id] = now + GROUP_CD_SEC
    _user_cooldown[user_id]  = now + USER_CD_SEC

    try:
        from plugins.ui.pages import page_start
        text, keyboard = await page_start(client)

        await client.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        notif = await message.reply(
            "📬 <b>Control Panel dikirim ke DM kamu!</b>\n"
            "<i>Cek pesan pribadi dari bot untuk membuka pengaturan grup.</i>",
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(_auto_delete(notif, message, delay=10))

    except (UserIsBlocked, PeerIdInvalid, InputUserDeactivated):
        me        = await client.get_me()
        start_url = f"https://t.me/{me.username}?start=true"

        fallback = await message.reply(
            "🤖 <b>AntiGcast — Anti-Spam Bot</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Untuk membuka <b>Control Panel</b>, kamu perlu memulai\n"
            "percakapan dengan bot ini di chat pribadi terlebih dahulu.\n\n"
            "Klik tombol di bawah → tekan <b>START</b> → ketik /antigcast lagi.\n\n"
            "<i>⏳ Pesan ini terhapus otomatis dalam 15 detik.</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀  Buka & Start Bot", url=start_url)],
            ]),
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(_auto_delete(fallback, message, delay=GROUP_CD_SEC))

    except Exception as e:
        print(f"[antigcast_group] Error: {e}")
        try:
            await message.delete()
        except Exception:
            pass
