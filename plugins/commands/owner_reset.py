"""
plugins/commands/owner_reset.py
────────────────────────────────
Perintah /reset CODE_BOT — khusus owner via DM.

Menghapus SEMUA data cloud (MongoDB / SQLite) yang tersimpan di namespace
CODE_BOT yang diberikan. Butuh konfirmasi sebelum eksekusi.

Contoh: /reset mybot
"""

import os
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

from database import reset_code_bot_data

_OWNER_ID = int(os.environ.get("OWNER_ID", 0))

# FSM sederhana: simpan code_bot yang menunggu konfirmasi per user
_pending_reset: dict[int, str] = {}


@Client.on_message(
    filters.command("reset") & filters.private & filters.user(_OWNER_ID)
)
async def cmd_reset(client: Client, message: Message):
    """
    /reset CODE_BOT — hapus semua data satu namespace CODE_BOT.
    Hanya bisa dipakai owner via DM bot.
    """
    if not _OWNER_ID:
        return

    if len(message.command) < 2:
        return await message.reply(
            "❌ <b>Format Salah</b>\n\n"
            "Gunakan: <code>/reset CODE_BOT</code>\n\n"
            "<b>Contoh:</b> <code>/reset mybot</code>\n\n"
            "Perintah ini akan menghapus <b>SEMUA data cloud</b> (pengaturan grup, "
            "filter kata, database Nexus AI, log aktivitas, dll) yang tersimpan "
            "dengan namespace CODE_BOT tersebut.\n\n"
            "<i>⚠️ Tidak bisa dibatalkan setelah dikonfirmasi!</i>",
            parse_mode=ParseMode.HTML,
        )

    code_bot = message.command[1].strip().lower()
    _pending_reset[message.from_user.id] = code_bot

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⚠️ YA, HAPUS SEMUA DATA",
                callback_data=f"resetdb_yes_{code_bot}",
            ),
            InlineKeyboardButton("❌ Batal", callback_data="resetdb_no"),
        ]
    ])

    await message.reply(
        f"⚠️ <b>KONFIRMASI RESET DATABASE</b>\n\n"
        f"Namespace yang akan direset:\n"
        f"<code>CODE_BOT = {code_bot}</code>\n\n"
        f"<b>Data yang akan dihapus secara permanen:</b>\n"
        f"  ◈ Pengaturan semua grup (local/global/bio/waktu)\n"
        f"  ◈ Filter kata & regex per grup\n"
        f"  ◈ Whitelist & VIP user\n"
        f"  ◈ Database Nexus AI (kalimat + regex terlatih)\n"
        f"  ◈ Log aktivitas & riwayat aksi\n"
        f"  ◈ Cache pesan seen & mute tracking\n"
        f"  ◈ Semua setting bot tersimpan\n\n"
        f"<b>Ini tidak bisa dibatalkan!</b>",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


@Client.on_callback_query(
    filters.regex(r"^resetdb_yes_(.+)$") & filters.user(_OWNER_ID)
)
async def cb_reset_confirm(client, cb):
    await cb.answer("⏳ Menghapus data...")

    code_bot = cb.matches[0].group(1)

    # Validasi: pastikan ini permintaan yang masih aktif
    if _pending_reset.get(cb.from_user.id) != code_bot:
        return await cb.message.edit(
            "❌ Sesi reset sudah kedaluwarsa atau tidak cocok.\n"
            "Ulangi perintah /reset CODE_BOT.",
            parse_mode=ParseMode.HTML,
        )

    _pending_reset.pop(cb.from_user.id, None)

    await cb.message.edit(
        f"⏳ <b>Menghapus semua data [{code_bot}]...</b>\n"
        f"<i>Mohon tunggu...</i>",
        parse_mode=ParseMode.HTML,
    )

    try:
        total, cleared = await reset_code_bot_data(code_bot)
    except Exception as e:
        return await cb.message.edit(
            f"❌ <b>Error saat reset:</b>\n<code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )

    if cleared:
        detail = "\n".join(f"  ◈ {c}" for c in cleared)
    else:
        detail = "  <i>(tidak ada data ditemukan untuk namespace ini)</i>"

    await cb.message.edit(
        f"✅ <b>RESET SELESAI</b>\n\n"
        f"<b>Namespace:</b> <code>{code_bot}</code>\n"
        f"<b>Total dihapus:</b> <code>{total} dokumen/baris</code>\n\n"
        f"<b>Koleksi yang dibersihkan:</b>\n{detail}\n\n"
        f"<i>Bot akan berjalan dengan database kosong untuk namespace ini. "
        f"Grup perlu ditambahkan ulang, dan Nexus AI perlu dilatih ulang.</i>",
        parse_mode=ParseMode.HTML,
    )


@Client.on_callback_query(
    filters.regex(r"^resetdb_no$") & filters.user(_OWNER_ID)
)
async def cb_reset_cancel(client, cb):
    await cb.answer("Reset dibatalkan.")
    _pending_reset.pop(cb.from_user.id, None)
    await cb.message.edit(
        "✅ <b>Reset dibatalkan.</b>\n\n<i>Semua data tetap aman.</i>",
        parse_mode=ParseMode.HTML,
    )
