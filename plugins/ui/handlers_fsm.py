"""
plugins/ui/handlers_fsm.py
──────────────────────────
Menangkap input teks di DM setelah user masuk mode FSM:
  - Input pola regex baru
  - Input ID untuk whitelist CAS
  - Input ID untuk Member VIP
  - Perintah /batal

Bug fix:
  - Setiap handler pop state + cancel task sebelum proses
  - Validasi ketat sebelum edit message (msg_id mungkin stale)
  - Semua Exception tertangkap, tidak ada yang menyebabkan crash
  - /batal selalu clear semua FSM sekaligus
"""

import asyncio
import re
import unicodedata
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified, MessageIdInvalid

from database import db
from plugins.ui.pages import (
    page_regex_list, page_regex_tutorial,
    page_whitelist_text, page_free_list,
    page_cas_panel,
)
from plugins.ui.fsm_state import (
    pending_regex_state, pending_free_state, pending_wl_state,
    clear_all_fsm, _cancel_task,
)
from core.regex_utils import _build_group_interlock, generate_kandidat_mutasi_liar, pipeline_pembersihan
import admin_session as _adm_sess

group_regex_db = db["regex_per_group"]
whitelist_col  = db["whitelist_per_group"]
free_col       = db["free_per_group"]


async def _safe_edit_id(client, chat_id, msg_id, text, keyboard=None):
    """Edit pesan via ID. Gagal silent jika pesan sudah tidak relevan."""
    try:
        await client.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except (MessageNotModified, MessageIdInvalid):
        pass
    except Exception as e:
        print(f"[_safe_edit_id] {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Tangkap semua teks di DM (non-command) → routing ke FSM aktif
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.text & ~filters.command(["start", "batal", "antigcast"]))
async def handle_fsm_input(client, message: Message):
    user_id = message.from_user.id

    regex_state = pending_regex_state.get(user_id)
    if regex_state:
        await _handle_regex_input(client, message, user_id, regex_state)
        return

    free_state = pending_free_state.get(user_id)
    if free_state:
        await _handle_free_input(client, message, user_id, free_state)
        return

    wl_state = pending_wl_state.get(user_id)
    if wl_state:
        await _handle_wl_input(client, message, user_id, wl_state)
        return


# ─────────────────────────────────────────────────────────────────────────────
#  Handler regex FSM
# ─────────────────────────────────────────────────────────────────────────────
async def _handle_regex_input(client, message: Message, user_id: int, state: dict):
    # Simpan raw_asli dengan kapital utuh — JANGAN .lower() di sini
    # karena kapital dari owner dipakai sebagai penanda posisi wajib di generator
    raw_asli = unicodedata.normalize("NFKC", message.text.strip())
    raw      = raw_asli.lower()  # versi lowercase hanya untuk _build_group_interlock
    chat_id  = state["chat_id"]
    msg_id   = state["msg_id"]

    _cancel_task(pending_regex_state.pop(user_id, None))

    try:
        pola, kata_list = _build_group_interlock(raw)
        re.compile(pola)
    except (ValueError, re.error) as e:
        err = await message.reply(
            f"❌ <b>ERROR</b>\n\n"
            f"Input tidak dikenali:\n<code>{raw}</code>\n"
            f"<b>Keterangan:</b> <code>{e}</code>\n\n"
            f"<i>Contoh: <code>togel</code> atau <code>jual | akun</code></i>",
            parse_mode=ParseMode.HTML,
        )
        await asyncio.sleep(6)
        try:
            await err.delete()
            await message.delete()
        except Exception:
            pass
        return

    raw_display = " | ".join(kata_list) if kata_list else raw

    # Pisahkan kata dari raw_asli agar kapital tetap terjaga
    # raw_asli bisa "bAkSo | lonTOng" → split | → ["bAkSo", "lonTOng"]
    kata_asli_list = [k.strip() for k in raw_asli.split("|") if k.strip()]

    mutasi_map: dict = {}
    for i, kata in enumerate(kata_list):
        # Ambil versi asli (dengan kapital) jika tersedia
        kata_dengan_kapital = kata_asli_list[i] if i < len(kata_asli_list) else kata
        # Bersihkan simbol tapi JANGAN lowercase — kapital harus sampai ke generator
        import re as _re
        kata_bersih_asli = _re.sub(r"\(?[×xX]\d+\)?", "", kata_dengan_kapital)
        kata_bersih_asli = _re.sub(r"[^\w]", "", kata_bersih_asli).strip().split()[0] if kata_bersih_asli.strip() else ""
        if kata_bersih_asli:
            mutasi_map[kata] = generate_kandidat_mutasi_liar(kata_bersih_asli)

    await group_regex_db.update_one(
        {"chat_id": chat_id, "pattern": pola},
        {"$set": {
            "chat_id":   chat_id,
            "pattern":   pola,
            "pola":      pola,
            "raw":       raw_display,
            "kata_list": kata_list,
            "mutasi":    mutasi_map,
        }},
        upsert=True,
    )

    try:
        from plugins.filters.antispam import invalidate_local_regex_cache
        invalidate_local_regex_cache(chat_id)
    except Exception:
        pass

    text, keyboard = await page_regex_list(chat_id, 1)
    kata_str = " + ".join(f"<code>{k}</code>" for k in kata_list) if kata_list else f"<code>{raw}</code>"
    header = (
        f"✅ <b>Filter Kata Berhasil Ditambahkan!</b>\n"
        f"◈ <b>Kata Kunci:</b> {kata_str}\n"
        f"◈ <b>Deteksi mutasi otomatis aktif</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    await _safe_edit_id(client, message.chat.id, msg_id, header + text, keyboard)

    try:
        await message.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Handler free/VIP FSM
# ─────────────────────────────────────────────────────────────────────────────
async def _handle_free_input(client, message: Message, user_id: int, state: dict):
    raw = message.text.strip()
    if not raw.isdigit():
        return

    target_id = int(raw)
    chat_id   = state["chat_id"]
    msg_id    = state["msg_id"]

    _cancel_task(pending_free_state.pop(user_id, None))

    await free_col.update_one(
        {"user_id": target_id, "chat_id": chat_id},
        {"$set": {"user_id": target_id, "chat_id": chat_id}},
        upsert=True,
    )

    text, keyboard = await page_free_list(chat_id)
    header = (
        f"✅ <b>User <code>{target_id}</code> berhasil dijadikan Member VIP!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    await _safe_edit_id(client, message.chat.id, msg_id, header + text, keyboard)

    try:
        await message.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Handler whitelist CAS FSM
# ─────────────────────────────────────────────────────────────────────────────
async def _handle_wl_input(client, message: Message, user_id: int, state: dict):
    raw = message.text.strip()
    if not raw.lstrip("-").isdigit():
        err = await message.reply(
            "❌ <b>ID TIDAK VALID</b>\n\n"
            "System hanya menerima angka numerik Telegram.\n"
            "Contoh valid: <code>123456789</code>",
            parse_mode=ParseMode.HTML,
        )
        await asyncio.sleep(5)
        try:
            await err.delete()
            await message.delete()
        except Exception:
            pass
        return

    target_id = int(raw)
    action    = state["action"]
    chat_id   = state["chat_id"]
    msg_id    = state["msg_id"]

    _cancel_task(pending_wl_state.pop(user_id, None))

    if action == "wl":
        await whitelist_col.update_one(
            {"user_id": target_id, "chat_id": chat_id},
            {"$set": {"status": "whitelisted"}},
            upsert=True,
        )
        result_text = (
            f"✅ <b>Otorisasi Whitelist Diterima!</b>\n"
            f"◈ <b>User ID:</b> <code>{target_id}</code> telah dikecualikan."
        )
    else:
        res = await whitelist_col.delete_one({"user_id": target_id, "chat_id": chat_id})
        result_text = (
            f"🗑️ <b>Whitelist Berhasil Dicabut!</b>\n"
            f"◈ <b>User ID:</b> <code>{target_id}</code> akan kembali dipantau."
        ) if res.deleted_count else (
            f"❌ <b>Data Tidak Ditemukan!</b>\n"
            f"ID <code>{target_id}</code> tidak terdaftar di sistem pengecualian."
        )

    wl_text = await page_whitelist_text(chat_id)
    await _safe_edit_id(
        client, message.chat.id, msg_id,
        f"{result_text}\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n{wl_text}",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙  Kembali ke CAS Panel", callback_data=f"cas_panel_{chat_id}")]
        ]),
    )

    try:
        await message.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  /batal — batalkan FSM aktif manapun
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("batal") & filters.private)
async def cancel_fsm(client, message: Message):
    user_id = message.from_user.id

    regex_state = pending_regex_state.get(user_id)
    free_state  = pending_free_state.get(user_id)
    wl_state    = pending_wl_state.get(user_id)

    clear_all_fsm(user_id)

    if regex_state:
        chat_id = regex_state["chat_id"]
        msg_id  = regex_state["msg_id"]
        text, keyboard = await page_regex_tutorial(chat_id)
        await _safe_edit_id(
            client, message.chat.id, msg_id,
            "✅ <b>Operasi Dibatalkan.</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + text,
            keyboard
        )

    elif free_state:
        chat_id = free_state["chat_id"]
        msg_id  = free_state["msg_id"]
        text, keyboard = await page_free_list(chat_id)
        await _safe_edit_id(
            client, message.chat.id, msg_id,
            "✅ <b>Operasi Dibatalkan.</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + text,
            keyboard
        )

    elif wl_state:
        chat_id = wl_state["chat_id"]
        msg_id  = wl_state["msg_id"]
        text, keyboard = await page_cas_panel(chat_id)
        await _safe_edit_id(
            client, message.chat.id, msg_id,
            "✅ <b>Operasi Dibatalkan.</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + text,
            keyboard
        )

    else:
        res = await message.reply(
            "ℹ️ <b>Sistem:</b> Tidak ada sesi operasi aktif.",
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(3)
        try:
            await res.delete()
        except Exception:
            pass

    try:
        await message.delete()
    except Exception:
        pass
