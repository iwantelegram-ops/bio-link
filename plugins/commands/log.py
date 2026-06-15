"""
plugins/commands/log.py
────────────────────────
Logging ke channel owner:
  - Notif saat bot masuk grup baru
  - /list (owner DM) → lihat semua grup aktif
  - Log deteksi alasan pesan dihapus (group=3)

FIXED: Peer id invalid pada log_new_group ditangani lebih baik.
"""

import os
import re
import time
import hashlib
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode, MessageEntityType
from pyrogram.errors import PeerIdInvalid, ChannelInvalid, ChatIdInvalid, FloodWait

from database import (
    config_db, get_config, is_admin, regex_db, messages_db, db,
    GLOBAL_EXPIRY, TZ_WIB,
)
from core.regex_utils import remove_mentions_for_regex, match_with_leet
from plugins.nexus.engine import pipeline_pembersihan

OWNER_ID    = int(os.environ.get("OWNER_ID", 0))
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", 0))

free_col            = db["free_per_group"]
group_regex_db      = db["regex_per_group"]
_log_local_regex_cache: dict[int, tuple[list, float]] = {}

# Cached validity flag so repeated Peer id errors don't spam console.
# FIXED: Sebelumnya False permanen — sekarang pakai retry interval 5 menit
# agar bot otomatis mencoba kembali setelah sesi baru berhasil terhubung ke channel.
_log_channel_valid: bool | None = None
_log_channel_fail_ts: float = 0.0
_LOG_CHANNEL_RETRY_INTERVAL = 300  # 5 menit sebelum retry setelah gagal


async def _send_log(client: Client, text: str) -> bool:
    """
    Kirim pesan ke LOG_CHANNEL dengan penanganan error yang lebih baik.
    Return True jika berhasil, False jika gagal.
    FIXED: Tidak lagi memblokir permanen — retry otomatis setelah 5 menit.
    """
    global _log_channel_valid, _log_channel_fail_ts
    if not LOG_CHANNEL:
        return False
    if _log_channel_valid is False:
        # Jika sudah melewati retry interval, reset agar bisa dicoba kembali
        if time.time() - _log_channel_fail_ts >= _LOG_CHANNEL_RETRY_INTERVAL:
            _log_channel_valid = None
        else:
            return False  # Masih dalam periode cooldown
    try:
        await client.send_message(
            LOG_CHANNEL, text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        _log_channel_valid = True
        return True
    except (PeerIdInvalid, ChannelInvalid, ChatIdInvalid) as e:
        if _log_channel_valid is None:  # Print hanya sekali per periode gagal
            print(f"[LOG] LOG_CHANNEL tidak valid ({LOG_CHANNEL}): {e}. "
                  f"Akan retry dalam {_LOG_CHANNEL_RETRY_INTERVAL//60} menit.")
        _log_channel_valid = False
        _log_channel_fail_ts = time.time()
        return False
    except FloodWait as e:
        print(f"[LOG] FloodWait {e.value}s — tunda pengiriman log.")
        return False
    except Exception as e:
        print(f"[LOG ERROR] {e}")
        return False


async def _get_local_patterns_log(chat_id: int):
    now = time.monotonic()
    hit = _log_local_regex_cache.get(chat_id)
    if hit and (now - hit[1]) < 300:
        return hit[0]
    patterns = []
    async for doc in group_regex_db.find({"chat_id": chat_id}):
        try:
            patterns.append((re.compile(doc["pattern"], re.IGNORECASE), doc.get("raw", doc["pattern"])))
        except Exception:
            pass
    _log_local_regex_cache[chat_id] = (patterns, now)
    return patterns


# ── LOG 1: Bot masuk grup baru ────────────────────────────────────────────────
@Client.on_message(filters.service, group=10)
async def log_new_group(client: Client, message: Message):
    if not message.new_chat_members or not LOG_CHANNEL:
        return
    me = await client.get_me()
    for member in message.new_chat_members:
        if member.id == me.id:
            chat  = message.chat
            waktu = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")
            text  = (
                "<b>❖ SYSTEM — NODE BARU ❖</b>\n\n"
                "➕ <b>Bot Bergabung ke Grup Baru</b>\n"
                f"◈ <b>Nama Grup:</b> {chat.title}\n"
                f"◈ <b>ID Grup:</b> <code>{chat.id}</code>\n"
                f"◈ <b>Username:</b> @{chat.username if chat.username else '—'}\n"
                f"◈ <b>Waktu:</b> {waktu}\n\n"
                "<i>Sistem firewall telah diintegrasikan pada grup ini.</i>"
            )
            await _send_log(client, text)


# ── LOG 2: /list — daftar semua grup ─────────────────────────────────────────
@Client.on_message(filters.command("list") & filters.private & filters.user(OWNER_ID))
async def list_grup_pengguna(client: Client, message: Message):
    msg = await message.reply("⏳ <i>Menarik data node grup dari server...</i>", parse_mode=ParseMode.HTML)
    grup_list = []
    grup_terhapus_count = 0

    async for doc in config_db.find({}):
        chat_id = doc.get("chat_id")
        if not chat_id:
            continue
        try:
            chat = await client.get_chat(chat_id)
            username = f"@{chat.username}" if chat.username else "—"
            grup_list.append(
                f"◈ <b>{chat.title}</b>\n"
                f"   └ ID: <code>{chat_id}</code> | Link: {username}"
            )
        except Exception:
            await config_db.delete_one({"chat_id": chat_id})
            grup_terhapus_count += 1

    if not grup_list:
        text = "<b>❖ NODE INDEX ❖</b>\n\n📭 <b>Sistem tidak mendeteksi koneksi grup aktif.</b>"
        if grup_terhapus_count:
            text += f"\n\n♻️ <i>Garbage collection: <b>{grup_terhapus_count} node mati</b> dibersihkan.</i>"
        await msg.edit(text, parse_mode=ParseMode.HTML)
        return

    header = (
        "<b>❖ NODE INDEX ❖</b>\n\n"
        f"⚡ <b>Total Grup Dilindungi:</b> <code>{len(grup_list)}</code>\n"
    )
    if grup_terhapus_count:
        header += f"♻️ <i>Garbage collection: <b>{grup_terhapus_count} node mati</b> dibersihkan.</i>\n"
    header += "\n<b>▰▰▰ DAFTAR GRUP AKTIF ▰▰▰</b>\n\n"

    chunks, current_chunk = [], header
    for g in grup_list:
        if len(current_chunk) + len(g) + 2 > 3900:
            chunks.append(current_chunk)
            current_chunk = "<b>📋 LANJUTAN DAFTAR GRUP:</b>\n\n"
        current_chunk += g + "\n\n"
    if current_chunk:
        chunks.append(current_chunk)

    await msg.edit(chunks[0], parse_mode=ParseMode.HTML)
    for extra in chunks[1:]:
        await message.reply(extra, parse_mode=ParseMode.HTML)


# ── LOG 3: Log alasan pesan dihapus (group=3) ────────────────────────────────
@Client.on_message(filters.group & ~filters.service, group=3)
async def log_deletion_trigger(client: Client, message: Message):
    if not message.from_user or not LOG_CHANNEL:
        return

    cid = message.chat.id
    uid = message.from_user.id

    if await is_admin(client, cid, uid):
        return

    if await free_col.find_one({"user_id": uid, "chat_id": cid}):
        return

    content = (message.text or message.caption or "").strip()
    if not content or content.startswith("/"):
        return

    cfg    = await get_config(cid)
    alasan = None
    detail = ""
    now_ts = time.time()
    waktu  = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")
    regex_safe       = remove_mentions_for_regex(message)
    teks_super_clean = pipeline_pembersihan(content)

    # Regex global (Owner Regex)
    async for doc in regex_db.find({}):
        pat_str = doc.get("pattern") or doc.get("pola")
        if not pat_str:
            continue
        try:
            pat = re.compile(pat_str, re.IGNORECASE)
        except Exception:
            continue
        if match_with_leet(pat, regex_safe) or (teks_super_clean and pat.search(teks_super_clean)):
            alasan  = "Filter Owner — Regex Global"
            raw_tag = doc.get("raw", pat.pattern)
            detail  = f"◈ <b>Pola:</b> <code>{raw_tag}</code>"
            break

    # Regex lokal (Group Filter)
    if not alasan:
        for pat, raw_pattern in await _get_local_patterns_log(cid):
            if match_with_leet(pat, regex_safe):
                alasan = "Filter Kata — Regex Grup"
                detail = f"◈ <b>Pola:</b> <code>{raw_pattern}</code>"
                break

    # Anti-duplikasi lokal
    if not alasan and cfg.get("local") is True:
        lokal_record = await messages_db.find_one({
            "chat_id": cid, "msg_id": message.id, "type": "local_track"
        })
        if lokal_record and lokal_record.get("warned") is True:
            alasan = "Anti-Spam Duplikasi Lokal"
            detail = "◈ <b>Alasan:</b> Pesan duplikat dalam satu sesi"

    # Anti-gcast global
    if not alasan and cfg.get("global") is True:
        content_hash = hashlib.md5(content.encode()).hexdigest()
        global_key   = f"glob_{uid}_{content_hash}"
        existing     = await messages_db.find_one({"_id": global_key})
        if existing and (now_ts - existing.get("time", 0)) < GLOBAL_EXPIRY:
            if len(existing.get("locations", [])) >= 2:
                alasan = "Anti-Broadcast Gcast Global"
                locs   = existing.get("locations", [])
                detail = f"◈ <b>Dikirim ke:</b> {len(locs)} grup sekaligus"

    # Bio link
    if not alasan and cfg.get("bio_check") is True:
        try:
            from plugins.filters.bio import _bio_cache
            hit = _bio_cache.get(uid)
            if hit and hit[0] is True:
                alasan = "Bio Link Detector"
                detail = "◈ <b>Alasan:</b> Bio mengandung tautan"
        except ImportError:
            pass

    # Link detector
    if not alasan:
        url_types    = {MessageEntityType.URL, MessageEntityType.TEXT_LINK}
        all_entities = list(message.entities or []) + list(message.caption_entities or [])
        if any(e.type in url_types for e in all_entities):
            alasan = "Link Detector"
            detail = "◈ <b>Alasan:</b> Pesan mengandung tautan aktif"

    # External mention (mention user yang bukan anggota grup)
    # Dicek terakhir karena butuh API call — hanya jika semua kondisi lain tidak cocok
    if not alasan:
        try:
            from plugins.filters.antispam import _is_external_mention
            if await _is_external_mention(client, message):
                alasan = "Mention Pengguna Luar Grup"
                detail = "◈ <b>Alasan:</b> Pesan menyebut user yang bukan anggota grup ini"
        except ImportError:
            pass

    # Hapus silent — user masih dalam masa mute aktif
    # Semua cek di atas tidak cocok tapi pesan dihapus → kemungkinan besar user masih kena mute
    if not alasan and cfg.get("local") is True:
        try:
            from database import get_local_mute
            mute_rec = await get_local_mute(cid, uid)
            if mute_rec.get("muted_until", 0.0) > now_ts:
                alasan = "Hapus Otomatis — User Dalam Masa Mute"
                until_dt = datetime.fromtimestamp(mute_rec["muted_until"], tz=TZ_WIB)
                detail = f"◈ <b>Alasan:</b> User masih di-mute hingga {until_dt.strftime('%H:%M:%S WIB')}"
        except Exception:
            pass

    if not alasan:
        return

    icon_map = {
        "Filter Owner — Regex Global":         "🚫",
        "Filter Kata — Regex Grup":            "🚫",
        "Anti-Spam Duplikasi Lokal":           "🔁",
        "Anti-Broadcast Gcast Global":         "🌐",
        "Bio Link Detector":                   "🔍",
        "Link Detector":                       "🔗",
        "Mention Pengguna Luar Grup":          "👤",
        "Hapus Otomatis — User Dalam Masa Mute": "🔇",
    }
    icon         = icon_map.get(alasan, "⚠️")
    user_mention = f"<a href='tg://user?id={uid}'>{message.from_user.first_name}</a>"

    log_text = (
        "<b>❖ ANTI-SPAM — PESAN DIHAPUS ❖</b>\n"
        f"{icon} <b>Pesan Dieliminasi Otomatis</b>\n"
        "<blockquote>"
        f"◈ <b>User:</b> {user_mention} (<code>{uid}</code>)\n"
        f"◈ <b>Grup:</b> {message.chat.title} (<code>{cid}</code>)\n"
        f"◈ <b>Waktu:</b> {waktu}\n"
        f"◈ <b>Tipe:</b> <code>{alasan}</code>\n"
        f"{detail}\n\n"
        f"<b>Konten:</b> <code>{content[:500]}</code>"
        "</blockquote>"
    )
    await _send_log(client, log_text)


# ── INTEGRASI NEXUS: Fungsi log untuk nexus_group.py ─────────────────────────

async def log_spam_global(client: Client, message: Message, pola: str, indikator: str):
    """Dipanggil oleh Nexus Engine untuk log pelanggaran GLOBAL."""
    uid          = message.from_user.id
    cid          = message.chat.id
    user_mention = f"<a href='tg://user?id={uid}'>{message.from_user.first_name}</a>"
    waktu        = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")

    log_text = (
        "<b>❖ NEXUS AI — FILTER GLOBAL ❖</b>\n"
        "🌐 <b>Pesan Dihapus — Deteksi AI Global</b>\n"
        "<blockquote>"
        f"◈ <b>User:</b> {user_mention} (<code>{uid}</code>)\n"
        f"◈ <b>Grup:</b> {message.chat.title} (<code>{cid}</code>)\n"
        f"◈ <b>Waktu:</b> {waktu}\n"
        f"◈ <b>Indikator:</b> <code>{indikator}</code>\n"
        f"◈ <b>Pola:</b> <code>{pola[:80]}</code>"
        "</blockquote>"
    )
    await _send_log(client, log_text)


async def log_spam_lokal(client: Client, message: Message, pola: str, indikator: str):
    """Dipanggil oleh Nexus Engine untuk log pelanggaran LOKAL (Owner)."""
    uid          = message.from_user.id
    cid          = message.chat.id
    user_mention = f"<a href='tg://user?id={uid}'>{message.from_user.first_name}</a>"
    waktu        = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")

    log_text = (
        "<b>❖ NEXUS AI — FILTER OWNER ❖</b>\n"
        "⚙️ <b>Pesan Dihapus — Filter Manual Owner</b>\n"
        "<blockquote>"
        f"◈ <b>User:</b> {user_mention} (<code>{uid}</code>)\n"
        f"◈ <b>Grup:</b> {message.chat.title} (<code>{cid}</code>)\n"
        f"◈ <b>Waktu:</b> {waktu}\n"
        f"◈ <b>Indikator:</b> <code>{indikator}</code>\n"
        f"◈ <b>Pola:</b> <code>{pola[:80]}</code>"
        "</blockquote>"
    )
    await _send_log(client, log_text)


async def log_sistem(client: Client, judul: str, pesan: str):
    """Log notifikasi sistem ke channel."""
    waktu    = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")
    log_text = (
        f"<b>❖ SYSTEM ALERT ❖</b>\n"
        f"⚡ <b>{judul}</b>\n"
        "<blockquote>"
        f"◈ <b>Waktu:</b> {waktu}\n"
        f"{pesan}"
        "</blockquote>"
    )
    await _send_log(client, log_text)
