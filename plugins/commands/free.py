"""
plugins/commands/free.py
─────────────────────────
Perintah admin grup untuk bebaskan user VIP dari semua filter:
  /vip [reply atau ID]
  /unvip [reply atau ID]
"""

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode

from database import db, is_admin, auto_delete_reply

DELAY  = 10
free_col = db["free_per_group"]


def _resolve(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if len(message.command) > 1:
        try:
            return int(message.command[1])
        except ValueError:
            pass
    return None


@Client.on_message(filters.command("vip") & filters.group)
async def cmd_vip(client: Client, message: Message):
    cid = message.chat.id
    uid = message.from_user.id if message.from_user else None
    if not await is_admin(client, cid, uid):
        return

    target = _resolve(message)
    if target is None:
        res = await message.reply(
            "⚠️ Cara pakai: reply pesan user atau <code>/vip ID</code>",
            parse_mode=ParseMode.HTML
        )
        return await auto_delete_reply([res, message], delay=DELAY)

    await free_col.update_one(
        {"user_id": target, "chat_id": cid},
        {"$set": {"user_id": target, "chat_id": cid}},
        upsert=True,
    )
    res = await message.reply(
        f"👑 <code>{target}</code> kini menjadi Member VIP — bebas dari semua filter di grup ini.",
        parse_mode=ParseMode.HTML
    )
    await auto_delete_reply([res, message], delay=DELAY)


@Client.on_message(filters.command("unvip") & filters.group)
async def cmd_unvip(client: Client, message: Message):
    cid = message.chat.id
    uid = message.from_user.id if message.from_user else None
    if not await is_admin(client, cid, uid):
        return

    target = _resolve(message)
    if target is None:
        res = await message.reply(
            "⚠️ Cara pakai: reply pesan user atau <code>/unvip ID</code>",
            parse_mode=ParseMode.HTML
        )
        return await auto_delete_reply([res, message], delay=DELAY)

    result = await free_col.delete_one({"user_id": target, "chat_id": cid})
    text = (
        f"🗑️ <code>{target}</code> sudah bukan Member VIP — kembali difilter."
        if result.deleted_count else
        f"❌ <code>{target}</code> tidak ada di daftar Member VIP grup ini."
    )
    res = await message.reply(text, parse_mode=ParseMode.HTML)
    await auto_delete_reply([res, message], delay=DELAY)
