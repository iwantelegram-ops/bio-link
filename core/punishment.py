"""
core/punishment.py
──────────────────
Sistem Hukuman Terpusat — berlaku untuk SEMUA jenis pelanggaran spam.

CARA KERJA:
  • Setiap deteksi spam (apapun jenisnya) memanggil check_and_punish().
  • Setelah 10 pelanggaran berturut-turut di grup yang sama → mute 5 menit.
  • Jika user masih spam setelah muted lagi → durasi 2× lipat (10, 20, 40, ... menit).
  • Setelah mute habis: hitungan spam TETAP di angka 10 (tidak direset ke 0),
    sehingga 1 pelanggaran berikutnya langsung memicu mute level berikutnya.
    Restart bot (Termux mati/hidup) tidak mereset hitungan karena data tersimpan
    persisten di database.
  • Pesan bersih (lolos semua filter, group=10) → reset hitungan + level hukuman.
  • Berlaku per user per grup — tidak campur antar grup.
  • Gcast: hanya grup yang mengaktifkan global detection yang menghitung punishment.

API Publik:
  check_and_punish(client, message, spam_type, konten) → bool
    Tambah hitungan. Terapkan mute jika ambang tercapai.
    Return True jika mute diterapkan, False jika belum/sudah muted.
"""

import os
import asyncio
import time
from datetime import datetime, timedelta, timezone

from pyrogram.enums import ParseMode
from pyrogram.errors import ChatAdminRequired, UserAdminInvalid
from pyrogram.types import ChatPermissions

from database import (
    get_local_mute, increment_local_spam, apply_local_mute,
    auto_delete_reply, insert_group_action_log, TZ_WIB,
)

LOG_CHANNEL         = int(os.environ.get("LOG_CHANNEL", 0))
SPAM_MUTE_THRESHOLD = 10   # Jumlah pelanggaran sebelum mute diterapkan


async def do_mute(client, chat_id: int, user_id: int, duration_seconds: int) -> bool:
    """Mute user di grup menggunakan until_date Telegram. Return True jika berhasil."""
    until_dt = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
    try:
        await client.restrict_chat_member(
            chat_id,
            user_id,
            ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False,
            ),
            until_date=until_dt,
        )
        return True
    except (ChatAdminRequired, UserAdminInvalid):
        return False
    except Exception:
        return False


async def check_and_punish(
    client,
    message,
    spam_type: str,
    konten: str = "",
) -> bool:
    """
    Dipanggil oleh setiap filter setelah mendeteksi spam.
    Menambah hitungan pelanggaran berturut-turut per user per grup.
    Jika mencapai ambang (10) → terapkan mute.
    Return True jika mute diterapkan, False jika tidak.
    """
    cid    = message.chat.id
    uid    = message.from_user.id
    now_ts = time.time()

    mute_rec = await get_local_mute(cid, uid)

    # Jika user masih dalam masa mute → jangan tambah hitungan / mute lagi
    if mute_rec.get("muted_until", 0.0) > now_ts:
        return False

    updated = await increment_local_spam(cid, uid)
    consec  = updated.get("consec_spam", 1)

    if consec < SPAM_MUTE_THRESHOLD:
        return False

    # Ambang tercapai → terapkan mute eskalasi
    duration_secs, _level = await apply_local_mute(cid, uid)
    duration_min          = duration_secs // 60

    muted_ok = await do_mute(client, cid, uid, duration_secs)
    if not muted_ok:
        return False

    # Beri tahu grup (pesan singkat, hapus 10 detik)
    try:
        notif = await client.send_message(
            cid,
            f"{message.from_user.mention} di-mute {duration_min} menit "
            f"karena {spam_type} berulang.",
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(auto_delete_reply([notif], delay=10))
    except Exception:
        pass

    # Log ke channel + per-grup action log (non-blocking)
    asyncio.create_task(_log_mute(
        client, message, duration_min, cid, uid, spam_type, konten
    ))

    return True


async def _log_mute(
    client,
    message,
    duration_min: int,
    cid: int,
    uid: int,
    spam_type: str,
    konten: str,
) -> None:
    """Log aksi mute ke group action log dan LOG_CHANNEL."""
    user_name = message.from_user.first_name or str(uid)

    try:
        await insert_group_action_log(
            cid, "MUTE",
            f"Mute {duration_min} menit – {spam_type} 10× berturut-turut",
            uid, user_name, konten,
        )
    except Exception:
        pass

    if not LOG_CHANNEL:
        return

    waktu        = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")
    user_mention = f"<a href='tg://user?id={uid}'>{user_name}</a>"

    log_text = (
        "<b>❖ ANTI-SPAM — MUTE DITERAPKAN ❖</b>\n"
        "🔇 <b>User Di-Mute Otomatis</b>\n"
        "<blockquote>"
        f"◈ <b>User:</b> {user_mention} (<code>{uid}</code>)\n"
        f"◈ <b>Grup:</b> {message.chat.title} (<code>{cid}</code>)\n"
        f"◈ <b>Waktu:</b> {waktu}\n"
        f"◈ <b>Durasi:</b> {duration_min} menit\n"
        f"◈ <b>Alasan:</b> {spam_type} — 10× berturut-turut\n\n"
        f"<b>Konten:</b> <code>{konten[:300]}</code>"
        "</blockquote>"
    )
    try:
        from pyrogram.enums import ParseMode as _PM
        await client.send_message(
            LOG_CHANNEL, log_text,
            parse_mode=_PM.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[PUNISHMENT LOG ERROR] {e}")
