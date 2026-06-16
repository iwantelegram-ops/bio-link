"""
plugins/filters/bio.py
──────────────────────
Filter deteksi link di bio Telegram user — BOT UTAMA.
Berjalan di group=1 (sebelum antispam.py di group=2).

ARSITEKTUR (Database-driven + TTL, per-grup):
  Bot utama TIDAK langsung fetch bio dari Telegram API.
  Tiap grup punya bot pemantau sendiri (token dari admin).
  Bot pemantau masing-masing grup menulis hasil scan ke bio_profiles
  dengan field chat_id sebagai pemisah antar grup.
  Data bio ber-TTL 5 menit — MongoDB hapus otomatis setelah expires_at.

  ALUR saat ada pesan masuk di grupA:
    1. bio_filter dipanggil → cek konfigurasi bio_check aktif?
    2. Query bio_profiles { chat_id: grupA, user_id: X }
       ← data ini ditulis oleh bot pemantau khusus grupA
    3a. has_link=True  → hapus pesan + hukuman
    3b. has_link=False → abaikan (biarkan lewat)
    3c. None (data belum ada / sudah expired TTL) →
          lempar user_id ke bot pemantau via force_check_user()
          → bot pemantau fetch bio → simpan ke DB (TTL 5 menit baru)
          → pakai hasilnya

  ALUR saat ada TYPING dari user di grupA:
    1. raw handler bot utama terima UpdateUserTyping
    2. Cek bio_profiles → ada data? pakai langsung (tidak ganggu API)
    3. Tidak ada data? → panggil force_check_user() ke bot pemantau
       → bot pemantau fetch bio → simpan DB → catat hasil di memory cache

DATA FLOW PER-GRUP:
  Bot pemantau grupA hanya tulis data chat_id=grupA.
  Bot utama hanya baca data chat_id=grupA saat ada event di grupA.
  Data hilang otomatis setelah 5 menit (MongoDB TTL index).
  Saat user aktif → data selalu fresh karena di-refresh setiap aksi.

FALLBACK AMAN:
  Jika force_check_user gagal (instance tidak aktif / error) → lewatkan.
  Tidak ada false positive.
"""

import os
import asyncio
import time
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from pyrogram.raw import types as raw_types

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
# Cache ini hanya dipakai saat data BARU saja di-fetch (< 5 menit).
# Setelah TTL DB habis, cache ini juga akan expired (TTL sama = 300 detik).
_mem_cache: dict[tuple[int, int], tuple[bool, float]] = {}
# TTL cache HARUS ikut BIO_TTL_SECS — jika user hapus link dari bio,
# cache expired bersamaan dengan data DB sehingga bot baca data fresh.
_MEM_CACHE_TTL = float(os.environ.get("BIO_TTL_SECS", 300))

# ── Throttle typing handler bot utama ─────────────────────────────────────────
# Untuk mencegah force_check_user dipanggil terlalu sering dari sisi bot utama.
# Key: (chat_id, user_id), Value: timestamp terakhir trigger
_typing_trigger_ts: dict[tuple[int, int], float] = {}
_TYPING_TRIGGER_COOLDOWN = 60.0  # minimal 60 detik antar trigger dari bot utama


async def _query_bio_for_group(chat_id: int, user_id: int) -> bool | None:
    """
    Query hasil bio dari DB untuk pasangan (chat_id, user_id).
    Data ini KHUSUS grup ini — ditulis oleh bot pemantau grup ini.
    Data ber-TTL 5 menit — jika expired, MongoDB sudah hapus → return None.

    Return:
      True  → ada link di bio (data masih valid di DB)
      False → tidak ada link di bio (data masih valid di DB)
      None  → belum ada data / sudah expired → perlu force_check_user
    """
    now = time.monotonic()
    key = (chat_id, user_id)

    # Memory cache dulu — untuk menghindari query DB berulang dalam 5 menit
    cached = _mem_cache.get(key)
    if cached:
        has_link, cache_ts = cached
        if now - cache_ts < _MEM_CACHE_TTL:
            return has_link
        else:
            # Cache expired → hapus
            del _mem_cache[key]

    # Query DB
    try:
        doc = await bio_col.find_one({"chat_id": chat_id, "user_id": user_id})
    except Exception as e:
        print(f"[Bio-Filter] Gagal query bio chat={chat_id} uid={user_id}: {e}")
        return None

    if not doc:
        # Belum ada data atau sudah dihapus TTL MongoDB → perlu fresh check
        return None

    has_link = doc.get("has_link", False)

    # Update memory cache
    _mem_cache[key] = (has_link, now)
    return has_link


def _update_mem_cache(chat_id: int, user_id: int, has_link: bool) -> None:
    """Update memory cache setelah force_check_user berhasil."""
    _mem_cache[(chat_id, user_id)] = (has_link, time.monotonic())


# ── Handler pesan masuk ────────────────────────────────────────────────────────

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

    # ── Step 1: Cek data dari DB (data bot pemantau grup ini) ─────────────────
    has_link = await _query_bio_for_group(cid, uid)

    # ── Step 2: Tidak ada data → lempar ke bot pemantau ───────────────────────
    if has_link is None:
        try:
            from monitor_bot_reference import force_check_user
            has_link = await force_check_user(cid, uid)
            # Update memory cache dengan hasil fresh
            if has_link is not None:
                _update_mem_cache(cid, uid, has_link)
        except Exception:
            pass
        if has_link is None:
            return  # Fallback aman: biarkan lewat jika tidak bisa cek

    # ── Step 3: Eksekusi jika ada link ────────────────────────────────────────
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


# ── Handler typing dari bot utama ─────────────────────────────────────────────
# Bot utama juga pasang raw handler typing agar:
#   - User baru mengetik tapi belum pernah kirim pesan → bot pemantau belum
#     scan → data tidak ada di DB → bot utama trigger force_check_user
#   - Setelah data expired TTL (5 menit tidak aktif) → user typing → fresh check

@Client.on_raw_update(group=1)
async def bio_typing_handler(client: Client, update, users, chats):
    """
    Handler typing di bot utama.
    Tujuan: jika user typing tapi data bio tidak ada di DB (belum pernah dicek
    atau sudah expired TTL), bot utama trigger force_check_user ke bot pemantau.
    Hasilnya dicatat di memory cache sehingga saat pesan masuk, bio_filter
    sudah punya data tanpa perlu force_check_user lagi.
    """
    try:
        if not isinstance(update, raw_types.UpdateUserTyping):
            return

        user_id = getattr(update, "user_id", None)
        if not user_id or not isinstance(user_id, int) or user_id <= 0:
            return

        # UpdateUserTyping tidak selalu membawa chat_id secara langsung.
        # Kita tidak tahu grup mana yang di-typing → skip tanpa chat_id.
        # Handler ini bergantung pada bot pemantau yang sudah tahu konteks grupnya.
        # Bot utama hanya bisa trigger berdasarkan pesan masuk (bio_filter di atas).
        # Raw handler typing di bot utama untuk future use / supergroup context.
        peer = getattr(update, "peer", None)
        if peer is None:
            return

        # Ambil chat_id dari peer jika tersedia
        chat_id = None
        if hasattr(peer, "chat_id"):
            chat_id = -peer.chat_id
        elif hasattr(peer, "channel_id"):
            chat_id = int(f"-100{peer.channel_id}")

        if not chat_id:
            return

        # Throttle: jangan trigger terlalu sering dari bot utama
        now = time.monotonic()
        key = (chat_id, user_id)
        last_trigger = _typing_trigger_ts.get(key, 0)
        if now - last_trigger < _TYPING_TRIGGER_COOLDOWN:
            return
        _typing_trigger_ts[key] = now

        # Cek konfigurasi bio_check aktif di grup ini
        try:
            cfg = await get_config(chat_id)
            if not cfg.get("bio_check"):
                return
        except Exception:
            return

        # Cek data di DB dulu — jika ada, tidak perlu trigger
        has_link = await _query_bio_for_group(chat_id, user_id)
        if has_link is not None:
            return  # Data sudah ada dan masih valid

        # Data tidak ada / expired → trigger bot pemantau
        try:
            from monitor_bot_reference import force_check_user
            result = await force_check_user(chat_id, user_id)
            if result is not None:
                _update_mem_cache(chat_id, user_id, result)
                print(
                    f"[Bio-Typing] uid={user_id} chat={chat_id} "
                    f"→ pre-fetch bio, has_link={result}"
                )
        except Exception:
            pass

    except Exception as e:
        print(f"[Bio-Typing] Error raw handler: {e}")


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
