"""
plugins/commands/settings.py
─────────────────────────────
Perintah pengaturan bot di grup (admin only):
  /setlocal, /setglobal, /setbio, /setwaktu, /status
"""

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from database import get_config, update_config, is_admin, auto_delete_reply, DEFAULT_LOCAL_EXPIRY

DELAY_NOTIF = 10


@Client.on_message(
    filters.command(["setlocal", "setglobal", "setbio", "setwaktu", "status"]) & filters.group
)
async def group_settings_handler(client: Client, message):
    cid = message.chat.id
    uid = message.from_user.id if message.from_user else None

    if not await is_admin(client, cid, uid):
        return

    cmd = message.command[0].lower()
    cfg = await get_config(cid)

    if cmd == "setlocal":
        if len(message.command) < 2 or message.command[1].lower() not in ["on", "off"]:
            res = await message.reply("⚠️ Format salah. Contoh: <code>/setlocal on</code>", parse_mode=ParseMode.HTML)
            return await auto_delete_reply([res, message], delay=DELAY_NOTIF)
        val = message.command[1].lower() == "on"
        await update_config(cid, "local", val)
        icon = "🟢" if val else "🔴"
        res  = await message.reply(f"🛡️ Anti-Spam Lokal → {icon} <b>{'ON' if val else 'OFF'}</b>", parse_mode=ParseMode.HTML)
        await auto_delete_reply([res, message], delay=DELAY_NOTIF)

    elif cmd == "setglobal":
        if len(message.command) < 2 or message.command[1].lower() not in ["on", "off"]:
            res = await message.reply("⚠️ Format salah. Contoh: <code>/setglobal on</code>", parse_mode=ParseMode.HTML)
            return await auto_delete_reply([res, message], delay=DELAY_NOTIF)
        val = message.command[1].lower() == "on"
        await update_config(cid, "global", val)
        icon = "🟢" if val else "🔴"
        res  = await message.reply(f"🌐 Anti-Gcast Global → {icon} <b>{'ON' if val else 'OFF'}</b>", parse_mode=ParseMode.HTML)
        await auto_delete_reply([res, message], delay=DELAY_NOTIF)

    elif cmd == "setbio":
        if len(message.command) < 2 or message.command[1].lower() not in ["on", "off"]:
            res = await message.reply("⚠️ Format salah. Contoh: <code>/setbio on</code>", parse_mode=ParseMode.HTML)
            return await auto_delete_reply([res, message], delay=DELAY_NOTIF)
        val = message.command[1].lower() == "on"
        await update_config(cid, "bio_check", val)
        icon = "🟢" if val else "🔴"
        res  = await message.reply(f"🔍 Deteksi Bio Link → {icon} <b>{'ON' if val else 'OFF'}</b>", parse_mode=ParseMode.HTML)
        await auto_delete_reply([res, message], delay=DELAY_NOTIF)

    elif cmd == "setwaktu":
        if len(message.command) < 2 or not message.command[1].isdigit():
            res = await message.reply("⚠️ Format salah. Contoh: <code>/setwaktu 15</code>", parse_mode=ParseMode.HTML)
            return await auto_delete_reply([res, message], delay=DELAY_NOTIF)
        mnt = max(1, int(message.command[1]))
        await update_config(cid, "expiry", mnt * 60)
        res = await message.reply(f"⏱️ Durasi memori spam → <code>{mnt} menit</code>", parse_mode=ParseMode.HTML)
        await auto_delete_reply([res, message], delay=DELAY_NOTIF)

    elif cmd == "status":
        def _i(v): return "🟢" if v else "🔴"
        expiry_min = cfg.get("expiry", DEFAULT_LOCAL_EXPIRY) // 60
        res = await message.reply(
            f"<b>🛡️ Status Keamanan Grup</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{_i(cfg['local'])} Anti-Spam Lokal\n"
            f"{_i(cfg['global'])} Anti-Gcast Global\n"
            f"{_i(cfg['bio_check'])} Deteksi Bio Link\n"
            f"⏱️ Memori spam: <code>{expiry_min} mnt</code>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<i>Panel lengkap: /antigcast</i>",
            parse_mode=ParseMode.HTML
        )
        await auto_delete_reply([res, message], delay=10)
