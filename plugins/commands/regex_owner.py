"""
plugins/commands/regex_owner.py
────────────────────────────────
Perintah khusus OWNER untuk kelola regex global:
  /delregex, /infobot
  (NOTE: /addregex sekarang dikendalikan penuh oleh Nexus Engine di nexus_handlers.py)
"""

import re
import os
import unicodedata
from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from database import regex_db
# core.regex_utils parse_simple_regex tidak perlu dipanggil lagi untuk addregex

OWNER_ID = int(os.environ.get("OWNER_ID", 0))

# 🌟 HAPUS "addregex" DARI DAFTAR COMMAND AGAR TIDAK BENTROK
@Client.on_message(
    filters.command(["delregex", "infobot"]) & filters.user(OWNER_ID)
)
async def owner_management(client, message):
    cmd = message.command[0].lower()

    # 🌟 BLOK KODE 'if cmd == "addregex":' SUDAH DIHAPUS TOTAL DI SINI

    if cmd == "delregex":
        if len(message.command) < 2:
            return await message.reply("Gunakan: `/delregex [kata kunci]`")

        raw_to_delete = " ".join(message.command[1:])
        result = await regex_db.delete_one({"raw": raw_to_delete})

        if result.deleted_count:
            await message.reply(f"✅ Filter <code>{raw_to_delete}</code> berhasil dihapus.", parse_mode=ParseMode.HTML)
        else:
            await message.reply(
                f"❌ <b>Data Not Found!</b>\n\n"
                f"Kata <code>{raw_to_delete}</code> tidak ada di database.\n"
                f"Cek dengan <code>/infobot</code>.",
                parse_mode=ParseMode.HTML
            )

    elif cmd == "infobot":
        docs = [doc async for doc in regex_db.find({})]
        if docs:
            lines = "\n".join(f"<code>{doc.get('raw', '—')}</code>" for doc in docs)
            text  = (
                "<b>GLOBAL FIREWALL</b>\n\n"
                f"⚡ Total Entri: <code>{len(docs)}</code>\n\n"
                "<b>KATA KUNCI:</b>\n\n"
                f"{lines}\n\n"
                "<b>Cara hapus:</b>\n"
                "<code>/delregex [kata kunci]</code>"
            )
        else:
            text = "📭 Database kosong."
        await message.reply(text, parse_mode=ParseMode.HTML)
