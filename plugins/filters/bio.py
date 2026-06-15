"""
plugins/filters/bio.py
──────────────────────
Filter deteksi link di bio Telegram user — BOT UTAMA.
Berjalan di group=1 (sebelum antispam.py di group=2).

ARSITEKTUR (Database-driven, per-grup):
  Bot utama TIDAK mengirim /checkbio ke grup.
  Tiap grup punya bot pemantau sendiri (token dari admin).
  Bot pemantau masing-masing grup menulis hasil scan ke bio_profiles
  dengan field chat_id sebagai pemisah antar grup.

  Alur saat ada pesan masuk di grupA:
    1. bio_filter dipanggil → cek konfigurasi bio_check aktif?
    2. Query bio_profiles { chat_id: grupA, user_id: X }
       ← data ini ditulis oleh bot pemantau khusus grupA
    3. has_link=True → hapus pesan + hukuman
    4. has_link=False / data belum ada → lewatkan

  Data bio bersifat PER-GRUP: bot pemantau grupA hanya menulis
  data untuk grupA, bot pemantau grupB hanya untuk grupB.

FALLBACK AMAN:
  Jika data belum ada di DB (bot pemantau belum scan user ini),
  pesan dibiarkan lewat. Tidak ada false positive.
"""

import os
import asyncio
import time
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode

from database import (
    is_admin, delete_queue, get_config,
    db, TZ_WIB,
    mark_message_handled, is_message_handled, insert_group_action_log,
)
from core.punishment import check_and_punish

free_col    = db["free_per_group"]
bio_col     = db["bio_profiles"]    # Ditulis oleh bot pemantau masing-masing grup
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", 0))

# ── In-memory cache: hindari query DB berulang untuk user+grup yang sama ──────
# Key: (chat_id, user_id) — per-grup karena data bio bersifat per-grup
# Value: (has_link: bool, cache_ts: float)
_mem_cache: dict[tuple[int, int], tuple[bool, float]] = {}
_MEM_CACHE_TTL = 300.0  # 5 menit cache di memory


async def _query_bio_for_group(chat_id: int, user_id: int) -> bool | None:
    """
    Query hasil bio dari DB untuk pasangan (chat_id, user_id).
    Data ini KHUSUS grup ini — ditulis oleh bot pemantau grup ini.

    Return:
      True  → ada link di bio (dari DB bot pemantau grup ini)
      False → tidak ada link di bio
      None  → belum ada data → lewatkan (fallback aman)
    """
    now = time.monotonic()
    key = (chat_id, user_id)

    # Memory cache dulu
    cached = _mem_cache.get(key)
    if cached:
        has_link, cache_ts = cached
        if now - cache_ts < _MEM_CACHE_TTL:
            return has_link

    # Query DB
    try:
        doc = await bio_col.find_one({"chat_id": chat_id, "user_id": user_id})
    except Exception as e:
        print(f"[Bio-Filter] Gagal query bio chat={chat_id} uid={user_id}: {e}")
        return None

    if not doc:
        # Bot pemantau grup ini belum scan user ini → lewatkan
        return None

    has_link = doc.get("has_link", False)

    # Update memory cache
    _mem_cache[key] = (has_link, now)
    return has_link


@Client.on_message(filters.group & ~filters.service, group=1)
async def bio_filter(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return

    cid = message.chat.id
    uid = message.from_user.id
    mid = message.id

    if is_message_handled(cid, mid):
        return

    cfg = await get_config(cid)
    if not cfg["bio_check"]:
        return

    if await is_admin(client, cid, uid):
        return

    if await free_col.find_one({"user_id": uid, "chat_id": cid}):
        return

    # ── Query data dari bot pemantau grup ini ─────────────────────────────────
    has_link = await _query_bio_for_group(cid, uid)

    # None = data belum ada di DB → paksa cek langsung via MonitorInstance
    if has_link is None:
        try:
            from monitor_bot_reference import force_check_user
            has_link = await force_check_user(cid, uid)
        except Exception:
            pass
        if has_link is None:
            return

    if has_link:
        mark_message_handled(cid, mid)
        await delete_queue.put((cid, [mid]))
        asyncio.create_task(_log_bio_deletion(client, message))
        try:
            await insert_group_action_log(
                cid, "HAPUS",
                "Link ditemukan di profil bio",
                uid,
                message.from_user.first_name or str(uid),
                (message.text or message.caption or "")[:100],
            )
        except Exception:
            pass
        asyncio.create_task(
            check_and_punish(client, message, "link di bio profil", "")
        )


async def _log_bio_deletion(client: Client, message: Message):
    if not LOG_CHANNEL:
        return

    uid          = message.from_user.id
    cid          = message.chat.id
    user_mention = f"<a href='tg://user?id={uid}'>{message.from_user.first_name}</a>"
    waktu        = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")
    content      = (message.text or message.caption or "").strip()

    # Ambil bio dari DB (data khusus grup ini)
    try:
        doc = await bio_col.find_one({"chat_id": cid, "user_id": uid})
        bio_snippet = doc.get("bio", "(tidak diketahui)")[:150] if doc else "(tidak diketahui)"
    except Exception:
        bio_snippet = "(tidak diketahui)"

    log_text = (
        "<b>❖ BIO LINK DETECTOR ❖</b>\n"
        "🔍 <b>Pesan Dihapus — Tautan di Bio</b>\n"
        "<blockquote>"
        f"◈ <b>User:</b> {user_mention} (<code>{uid}</code>)\n"
        f"◈ <b>Grup:</b> {message.chat.title} (<code>{cid}</code>)\n"
        f"◈ <b>Waktu:</b> {waktu}\n"
        f"◈ <b>Bio:</b> <code>{bio_snippet}</code>\n\n"
        f"<b>Konten pesan:</b> <code>{content[:400]}</code>"
        "</blockquote>"
    )
    try:
        await client.send_message(
            LOG_CHANNEL, log_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[BIO LOG ERROR] {e}")
