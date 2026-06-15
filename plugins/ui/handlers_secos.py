"""
plugins/ui/handlers_secos.py
─────────────────────────────
Callback handlers untuk panel Security OS.

Callback patterns:
  secos_panel_{chat_id}       — buka panel Security OS
  secos_on_{chat_id}          — aktifkan Security OS
  secos_off_{chat_id}         — nonaktifkan Security OS
  secos_setmon_{chat_id}      — mulai FSM input token bot pemantau
  secos_setuserbot_{chat_id}  — mulai FSM input nomor HP userbot baru

FSM "setmon":
  Admin mengirim token bot pemantau via DM.
  Handler ini memvalidasi token, join bot pemantau ke grup, simpan ke DB.

FSM "setuserbot":
  Admin mengirim nomor HP userbot baru via DM.
  Handler ini menghentikan userbot lama, login userbot baru via OTP,
  lalu mengaktifkan kembali voice chat monitor.

Tidak ada perubahan pada logika bot asli.
"""

import re
import asyncio

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, Message,
)
from pyrogram.enums import ParseMode

from plugins.ui.pages import page_security_os, page_manage
from plugins.ui.handlers_dm import safe_edit, _deny_session
import admin_session as _adm_sess

# ── FSM state untuk input token bot pemantau ─────────────────────────────────
# Format: { user_id: {"chat_id": int, "msg_id": int, "_task": Task|None} }
_pending_setmon: dict[int, dict] = {}

WAIT_TIMEOUT = 120   # detik — batas tunggu input token


# ─────────────────────────────────────────────────────────────────────────────
#  Buka panel Security OS
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^secos_panel_(-?\d+)$"))
async def cb_secos_panel(client: Client, cb: CallbackQuery):
    await cb.answer()
    chat_id = int(re.search(r"(-?\d+)$", cb.data).group(1))
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        return await _deny_session(cb)

    text, keyboard = await page_security_os(chat_id, client)
    await safe_edit(cb.message, text, keyboard)


# ─────────────────────────────────────────────────────────────────────────────
#  Aktifkan Security OS
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^secos_on_(-?\d+)$"))
async def cb_secos_on(client: Client, cb: CallbackQuery):
    await cb.answer("Memeriksa syarat...")
    chat_id = int(re.search(r"(-?\d+)$", cb.data).group(1))
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        return await _deny_session(cb)

    from video_call import security_os_enable, check_activation_prerequisites
    from database import insert_group_action_log

    # ── Periksa semua syarat sebelum mengaktifkan ────────────────────────────
    all_ok, messages = await check_activation_prerequisites(client, chat_id)

    if not all_ok:
        # Ada blocker — tampilkan panduan, jangan aktifkan
        msg_text = "\n\n".join(messages)
        await safe_edit(
            cb.message,
            f"🚫 <b>Security OS belum bisa diaktifkan.</b>\n\n"
            f"Selesaikan langkah berikut terlebih dahulu:\n\n"
            f"{msg_text}\n\n"
            f"<i>Setelah semua syarat terpenuhi, tekan Aktifkan lagi.</i>",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙  Kembali ke Panel", callback_data=f"secos_panel_{chat_id}")
            ]])
        )
        return

    # ── Semua syarat wajib terpenuhi — aktifkan ──────────────────────────────
    await security_os_enable(chat_id)
    await insert_group_action_log(
        chat_id, "SECOS", "Security OS diaktifkan oleh admin",
        user_id, str(user_id),
    )

    text, keyboard = await page_security_os(chat_id, client)

    # Jika ada warnings (misal bot pemantau belum join), tampilkan di atas panel
    if messages:
        warning_text = "\n\n".join(messages)
        text = f"⚠️ <b>Catatan:</b>\n{warning_text}\n\n━━━━━━━━━━━━━━━━━━━━━━━━\n{text}"

    await safe_edit(cb.message, text, keyboard)


# ─────────────────────────────────────────────────────────────────────────────
#  Nonaktifkan Security OS
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^secos_off_(-?\d+)$"))
async def cb_secos_off(client: Client, cb: CallbackQuery):
    await cb.answer("Menonaktifkan...")
    chat_id = int(re.search(r"(-?\d+)$", cb.data).group(1))
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        return await _deny_session(cb)

    from video_call import security_os_disable
    from database import insert_group_action_log

    await security_os_disable(chat_id)
    await insert_group_action_log(
        chat_id, "SECOS", "Security OS dinonaktifkan oleh admin",
        user_id, str(user_id),
    )

    text, keyboard = await page_security_os(chat_id, client)
    await safe_edit(cb.message, text, keyboard)


# ─────────────────────────────────────────────────────────────────────────────
#  Pasang bot pemantau — mulai FSM input token
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^secos_setmon_(-?\d+)$"))
async def cb_secos_setmon(client: Client, cb: CallbackQuery):
    await cb.answer()
    chat_id = int(re.search(r"(-?\d+)$", cb.data).group(1))
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        return await _deny_session(cb)

    # Batalkan FSM lama jika ada
    _cancel_setmon_task(user_id)

    _pending_setmon[user_id] = {
        "chat_id": chat_id,
        "msg_id":  cb.message.id,
        "_task":   None,
    }

    # Ambil username userbot untuk tombol adminkan
    ub_uname = ""
    try:
        from video_call import userbot as _ub
        if _ub:
            _ub_me = await _ub.get_me()
            ub_uname = _ub_me.username or ""
    except Exception:
        pass

    ub_admin_url = (
        f"tg://resolve?domain={ub_uname}&action=addadmin"
        if ub_uname else None
    )

    buttons = [
        [InlineKeyboardButton("🚫  Batalkan", callback_data=f"secos_panel_{chat_id}")]
    ]
    if ub_admin_url:
        buttons.insert(0, [
            InlineKeyboardButton(
                f"👑  Adminkan @{ub_uname} (Userbot) ke Grup",
                url=ub_admin_url,
            )
        ])

    ub_admin_text = (
        f"\n\n<b>4️⃣ Adminkan Userbot ke Grup. ( tidak wajib jika hanya untuk menyalakan bio cek di typingan grup )</b>\n"
        f"   Tekan tombol di bawah untuk memberi hak admin ke "
        f"<b>@{ub_uname}</b> (userbot) di grup ini.\n"
        f"   Izin minimal: <code>Kelola Obrolan Video</code>."
        if ub_uname else
        f"\n\n<b>4️⃣ Adminkan Userbot ke Grup. ( tidak wajib jika hanya untuk menyalakan bio cek di typingan grup )</b>\n"
        f"   Setelah bot pemantau terpasang, cari userbot di daftar member grup\n"
        f"   dan jadikan admin dengan izin <code>Kelola Obrolan Video</code>."
    )

    await safe_edit(
        cb.message,
        f"🤖 <b>PASANG BOT PEMANTAU</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Kirim <b>token</b> bot pemantau ke sini.\n\n"
        f"<b>📋 LANGKAH-LANGKAH:</b>\n\n"
        f"<b>1️⃣ Buat Bot via @BotFather</b>\n"
        f"   Buka @BotFather → <code>/newbot</code> → ikuti instruksi → salin token.\n\n"
        f"<b>2️⃣ Kirim token ke sini</b>\n"
        f"   Token berbentuk: <code>123456789:ABCdef...</code>\n"
        f"   Bot pemantau akan otomatis di-deploy & dipasang ke grup ini.\n\n"
        f"<b>3️⃣ Jadikan Bot Pemantau Member Grup</b>\n"
        f"   Bot Otomatis Bekerja, Atau Tambahkan sebagai <b>admin</b>\n"
        f"   di grup agar lebih akurat (opsional)."
        f"{ub_admin_text}\n\n"
        f"◈ 1 bot pemantau hanya boleh dipakai di <b>1 grup</b>.\n"
        f"◈ Token bot lama otomatis digantikan jika kamu memasukkan token baru.\n\n"
        f"<i>⏳ Batas waktu input: {WAIT_TIMEOUT // 60} menit.</i>\n"
        f"<i>Kirim /batal untuk membatalkan.</i>",
        InlineKeyboardMarkup(buttons)
    )

    # Timeout task
    task = asyncio.create_task(
        _setmon_timeout(user_id, chat_id, cb.message, client)
    )
    if user_id in _pending_setmon:
        _pending_setmon[user_id]["_task"] = task


async def _setmon_timeout(user_id: int, chat_id: int, msg, client: Client):
    await asyncio.sleep(WAIT_TIMEOUT)
    if user_id not in _pending_setmon:
        return
    _pending_setmon.pop(user_id, None)
    try:
        text, keyboard = await page_security_os(chat_id, client)
        await safe_edit(msg, "⏰ <b>Timeout.</b> Input token dibatalkan.\n\n" + text, keyboard)
    except Exception:
        pass


def _cancel_setmon_task(user_id: int):
    state = _pending_setmon.pop(user_id, None)
    if state and state.get("_task"):
        task = state["_task"]
        if not task.done():
            task.cancel()


# ─────────────────────────────────────────────────────────────────────────────
#  FSM: tangkap input token bot pemantau dari DM owner
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(
    filters.private & filters.text,
    group=50,  # setelah handler FSM asli (group default), sebelum OTP handler (99)
)
async def handle_setmon_input(client: Client, message: Message):
    user_id = message.from_user.id

    state = _pending_setmon.get(user_id)
    if not state:
        return  # bukan dalam mode setmon — lewati

    text = (message.text or "").strip()

    # Perintah /batal — batalkan
    if text.lower() in ("/batal", "/cancel"):
        _cancel_setmon_task(user_id)
        chat_id = state["chat_id"]
        msg_id  = state["msg_id"]
        try:
            page_text, keyboard = await page_security_os(chat_id, client)
            await client.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text="✅ <b>Dibatalkan.</b>\n\n" + page_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Validasi format token (digit:alfanumerik)
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", text):
        try:
            err = await message.reply(
                "❌ Format token tidak valid.\n"
                "Token Telegram bot berbentuk: <code>123456789:ABCdef...</code>\n\n"
                "Coba lagi atau kirim /batal untuk membatalkan.",
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(5)
            await err.delete()
        except Exception:
            pass
        return

    # Token valid — proses
    chat_id = state["chat_id"]
    msg_id  = state["msg_id"]
    _cancel_setmon_task(user_id)

    # Hapus pesan token dari DM (keamanan)
    try:
        await message.delete()
    except Exception:
        pass

    # Tampilkan loading
    try:
        await client.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg_id,
            text="⏳ <b>Memvalidasi token & menyiapkan bot pemantau...</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    # Setup bot pemantau
    from video_call import setup_monitor_bot
    ok, result_msg = await setup_monitor_bot(chat_id, text, client)

    # Ambil username userbot untuk tombol adminkan (ditampilkan di pesan sukses)
    _ub_uname_success = ""
    try:
        from video_call import userbot as _ub2
        if _ub2:
            _ub_me2 = await _ub2.get_me()
            _ub_uname_success = _ub_me2.username or ""
    except Exception:
        pass

    if ok:
        page_text, keyboard = await page_security_os(chat_id, client)
        ub_admin_note = (
            f"\n👑 Jangan lupa adminkan userbot <b>@{_ub_uname_success}</b> "
            f"ke grup dengan izin <code>Kelola Obrolan Video</code>.\n"
            if _ub_uname_success else
            "\n👑 Jangan lupa adminkan userbot ke grup (izin: Kelola Obrolan Video).\n"
        )
        # Tambah tombol adminkan userbot jika username tersedia
        if _ub_uname_success:
            from pyrogram.types import InlineKeyboardButton as _IKB
            existing_buttons = keyboard.inline_keyboard if keyboard else []
            ub_btn_row = [_IKB(
                f"👑  Adminkan @{_ub_uname_success} ke Grup",
                url=f"tg://resolve?domain={_ub_uname_success}&action=addadmin",
            )]
            from pyrogram.types import InlineKeyboardMarkup as _IKM
            keyboard = _IKM([ub_btn_row] + existing_buttons)

        final_text = (
            f"✅ <b>Bot pemantau berhasil dikonfigurasi!</b>\n"
            f"{result_msg}\n\n"
            f"⚠️ <b>Langkah selanjutnya:</b>\n"
            f"1️⃣ Tambahkan bot pemantau ke grup secara manual (Kelola Grup → Tambah Member).\n"
            f"   Bot akan <b>dikenali otomatis</b> saat masuk ke grup.\n"
            f"2️⃣ Jadikan bot pemantau sebagai <b>admin grup</b>.\n"
            f"3️⃣{ub_admin_note}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n{page_text}"
        )
    else:
        page_text, keyboard = await page_security_os(chat_id, client)
        final_text = (
            f"❌ <b>Gagal memasang bot pemantau.</b>\n"
            f"{result_msg}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n{page_text}"
        )

    try:
        await client.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg_id,
            text=final_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[secos_setmon] edit error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Ganti Userbot — FSM input nomor HP baru
# ─────────────────────────────────────────────────────────────────────────────

# Format: { user_id: {\"chat_id\": int, \"msg_id\": int, \"_task\": Task|None} }
_pending_setuserbot: dict[int, dict] = {}

WAIT_TIMEOUT_UB = 180   # detik — batas tunggu input nomor userbot


@Client.on_callback_query(filters.regex(r"^secos_setuserbot_(-?\d+)$"))
async def cb_secos_setuserbot(client: Client, cb: CallbackQuery):
    """Tombol 'Ganti Userbot' — mulai FSM input nomor HP baru."""
    await cb.answer()
    chat_id = int(re.search(r"(-?\d+)$", cb.data).group(1))
    user_id = cb.from_user.id

    if not await _adm_sess.verify_admin_session(client, user_id, chat_id):
        return await _deny_session(cb)

    # Batalkan FSM lama jika ada
    _cancel_setuserbot_task(user_id)

    _pending_setuserbot[user_id] = {
        "chat_id": chat_id,
        "msg_id":  cb.message.id,
        "_task":   None,
    }

    await safe_edit(
        cb.message,
        f"📱 <b>GANTI USERBOT</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Kirim <b>nomor HP</b> akun userbot baru ke sini.\n\n"
        f"<b>📋 LANGKAH-LANGKAH:</b>\n\n"
        f"<b>1️⃣ Siapkan akun Telegram</b>\n"
        f"   Gunakan akun biasa (bukan bot) yang akan dijadikan userbot.\n\n"
        f"<b>2️⃣ Kirim nomor HP ke sini</b>\n"
        f"   Format internasional: <code>+628123456789</code>\n\n"
        f"<b>3️⃣ Masukkan OTP</b>\n"
        f"   Telegram akan mengirim kode OTP ke nomor tersebut.\n"
        f"   Kirim kode via DM bot dengan format: <code>/otp &lt;kode&gt;</code>\n\n"
        f"<b>4️⃣ Adminkan userbot ke grup</b>\n"
        f"   Setelah login berhasil, jadikan userbot admin dengan izin\n"
        f"   <code>Kelola Obrolan Video</code> di setiap grup Security OS.\n\n"
        f"⚠️ <b>Userbot lama akan diputus dan session-nya dihapus.</b>\n\n"
        f"<i>⏳ Batas waktu input: {WAIT_TIMEOUT_UB // 60} menit.</i>\n"
        f"<i>Kirim /batal untuk membatalkan.</i>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫  Batalkan", callback_data=f"secos_panel_{chat_id}")]
        ])
    )

    task = asyncio.create_task(
        _setuserbot_timeout(user_id, chat_id, cb.message, client)
    )
    if user_id in _pending_setuserbot:
        _pending_setuserbot[user_id]["_task"] = task


async def _setuserbot_timeout(user_id: int, chat_id: int, msg, client: Client):
    await asyncio.sleep(WAIT_TIMEOUT_UB)
    if user_id not in _pending_setuserbot:
        return
    _pending_setuserbot.pop(user_id, None)
    try:
        if chat_id == 0:
            # Dipanggil dari owner panel — kembali ke nx_owner_menu
            await safe_edit(
                msg,
                "⏰ <b>Timeout.</b> Input nomor dibatalkan.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Owner Bot Panel", callback_data="nx_owner_menu")]])
            )
        else:
            text, keyboard = await page_security_os(chat_id, client)
            await safe_edit(msg, "⏰ <b>Timeout.</b> Input nomor dibatalkan.\n\n" + text, keyboard)
    except Exception:
        pass


def _cancel_setuserbot_task(user_id: int):
    state = _pending_setuserbot.pop(user_id, None)
    if state and state.get("_task"):
        task = state["_task"]
        if not task.done():
            task.cancel()


# ─────────────────────────────────────────────────────────────────────────────
#  FSM: tangkap input nomor HP userbot baru dari DM owner
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(
    filters.private & filters.text,
    group=51,   # setelah setmon handler (50), sebelum OTP handler (99)
)
async def handle_setuserbot_input(client: Client, message: Message):
    user_id = message.from_user.id

    state = _pending_setuserbot.get(user_id)
    if not state:
        return  # bukan dalam mode setuserbot — lewati

    text = (message.text or "").strip()
    chat_id = state["chat_id"]
    msg_id  = state["msg_id"]

    # Perintah /batal
    if text.lower() in ("/batal", "/cancel"):
        _cancel_setuserbot_task(user_id)
        try:
            if chat_id == 0:
                # Dipanggil dari owner panel
                await client.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=msg_id,
                    text="✅ <b>Dibatalkan.</b>",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Owner Bot Panel", callback_data="nx_owner_menu")]]),
                    parse_mode=ParseMode.HTML,
                )
            else:
                page_text, keyboard = await page_security_os(chat_id, client)
                await client.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=msg_id,
                    text="✅ <b>Dibatalkan.</b>\n\n" + page_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Validasi format nomor internasional
    if not re.match(r"^\+\d{7,15}$", text):
        try:
            err = await message.reply(
                "❌ Format nomor tidak valid.\n"
                "Gunakan format internasional: <code>+628123456789</code>\n\n"
                "Coba lagi atau kirim /batal untuk membatalkan.",
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(5)
            await err.delete()
        except Exception:
            pass
        return

    # Nomor valid — proses
    _cancel_setuserbot_task(user_id)

    # Hapus pesan nomor (keamanan)
    try:
        await message.delete()
    except Exception:
        pass

    # Tampilkan loading
    try:
        await client.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg_id,
            text=(
                "⏳ <b>Menghentikan userbot lama & memulai login userbot baru...</b>\n\n"
                "Telegram akan mengirim OTP ke nomor tersebut.\n"
                "Kirim kode via DM bot: <code>/otp &lt;kode&gt;</code>\n\n"
                "<i>Tunggu hingga proses login selesai (maks 10 menit).</i>"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    # Ganti userbot
    from video_call import change_userbot
    ok, result_msg = await change_userbot(text, client)

    if chat_id == 0:
        # Dipanggil dari owner panel — tidak ada page_security_os, redirect ke owner menu
        if ok:
            final_text = (
                f"{result_msg}\n\n"
                f"⚠️ <b>Langkah selanjutnya:</b>\n"
                f"1️⃣ Pastikan userbot sudah jadi admin di setiap grup Security OS\n"
                f"   dengan izin <code>Kelola Obrolan Video</code>."
            )
        else:
            final_text = (
                f"❌ <b>Gagal mengganti userbot.</b>\n"
                f"{result_msg}"
            )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Owner Bot Panel", callback_data="nx_owner_menu")]])
    else:
        page_text, keyboard = await page_security_os(chat_id, client)
        if ok:
            final_text = (
                f"{result_msg}\n\n"
                f"⚠️ <b>Langkah selanjutnya:</b>\n"
                f"1️⃣ Pastikan userbot sudah jadi admin di setiap grup Security OS\n"
                f"   dengan izin <code>Kelola Obrolan Video</code>.\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n{page_text}"
            )
        else:
            final_text = (
                f"❌ <b>Gagal mengganti userbot.</b>\n"
                f"{result_msg}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n{page_text}"
            )

    try:
        await client.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg_id,
            text=final_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[secos_setuserbot] edit error: {e}")
