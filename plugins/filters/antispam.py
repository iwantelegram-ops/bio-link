"""
plugins/filters/antispam.py
────────────────────────────
Filter utama pesan grup:
  1. Regex global & lokal  (Owner Regex — TANPA pengaruh Whitelist Nexus)
  2. External mention
  3. Link detector
  4. Anti duplikasi lokal (per user per grup)
  5. Anti duplikasi global (anti-gcast lintas grup)

SISTEM MUTE ESKALASI (terpusat di core/punishment.py):
  • 10 pelanggaran spam APAPUN berturut-turut (per user per grup) → mute 5 menit
  • Setiap pelanggaran berikutnya (tanpa pesan bersih) → durasi 2× lipat
  • Pesan bersih (lolos semua filter, group=10) → reset hitungan + level hukuman
  • Berlaku untuk SEMUA jenis spam: regex, mention, link, duplikat, gcast, bio, nexus

PASSIVE LEARNING (v3.1):
  • Penghapusan via regex global/lokal → force_learn=True ke AI (konfirmasi pasti spam)
  • Passive learning dilakukan fire-and-forget agar tidak memperlambat filter utama

PINTU BERURUTAN:
  Setiap kali filter ini memutuskan hapus pesan → mark_message_handled(cid, mid)
  dipanggil agar filter berikutnya (nexus group=5) tidak memproses ulang.
"""

import os
import re
import time
import asyncio
import hashlib
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.enums import MessageEntityType, ParseMode
from pyrogram.errors import UserNotParticipant, PeerIdInvalid, RPCError
from rapidfuzz import fuzz

LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", 0))

from database import (
    messages_db, regex_db, get_config, is_admin, db,
    delete_queue, GLOBAL_EXPIRY, TZ_WIB, auto_delete_reply,
    mark_message_handled, is_message_handled,
    get_local_mute, reset_local_mute,
    insert_group_action_log,
)
from core.regex_utils import simplify, remove_mentions_for_regex, match_with_leet
from core.punishment import check_and_punish
from plugins.nexus.engine import pipeline_pembersihan

group_regex_db = db["regex_per_group"]
free_col       = db["free_per_group"]

# ── Cache regex ───────────────────────────────────────────────────────────────
_regex_cache:     list  = []
_regex_cache_ts:  float = 0.0
_local_regex_cache: dict[int, tuple[list, float]] = {}
REGEX_TTL = 300

_URL_ENTITY_TYPES = {MessageEntityType.URL, MessageEntityType.TEXT_LINK}


def _has_url_entity(message) -> bool:
    entities = list(message.entities or []) + list(message.caption_entities or [])
    return any(e.type in _URL_ENTITY_TYPES for e in entities)


async def _get_global_patterns():
    """Return list of (compiled_pattern, raw_display_str) untuk regex global owner."""
    global _regex_cache, _regex_cache_ts
    now = time.monotonic()
    if now - _regex_cache_ts < REGEX_TTL:
        return _regex_cache
    patterns = []
    async for doc in regex_db.find({"pattern": {"$exists": True}}):
        try:
            raw = doc.get("raw") or doc.get("pattern", "")
            patterns.append((re.compile(doc["pattern"], re.IGNORECASE), raw))
        except Exception:
            pass
    _regex_cache = patterns
    _regex_cache_ts = now
    return _regex_cache


async def _get_local_patterns(chat_id: int):
    """Return list of (compiled_pattern, raw_display_str) untuk regex lokal grup."""
    now = time.monotonic()
    hit = _local_regex_cache.get(chat_id)
    if hit and (now - hit[1]) < REGEX_TTL:
        return hit[0]
    patterns = []
    async for doc in group_regex_db.find({"chat_id": chat_id}):
        try:
            raw = doc.get("raw") or doc.get("pattern", "")
            patterns.append((re.compile(doc["pattern"], re.IGNORECASE), raw))
        except Exception:
            pass
    _local_regex_cache[chat_id] = (patterns, now)
    return patterns


def invalidate_local_regex_cache(chat_id: int) -> None:
    """Hapus cache pattern lokal agar filter baru/terhapus langsung aktif."""
    _local_regex_cache.pop(chat_id, None)


async def _is_external_mention(client: Client, message) -> bool:
    if not message.entities:
        return False
    content = message.text or message.caption or ""
    cid = message.chat.id
    for entity in message.entities:
        target = None
        if entity.type == MessageEntityType.MENTION:
            target = content[entity.offset:entity.offset + entity.length].lstrip("@").lower()
        elif entity.type == MessageEntityType.TEXT_MENTION and getattr(entity, "user", None):
            target = entity.user.id
        elif entity.type in (MessageEntityType.URL, MessageEntityType.TEXT_LINK):
            url = (content[entity.offset:entity.offset + entity.length]
                   if entity.type == MessageEntityType.URL else entity.url)
            if url.startswith("tg://user?id="):
                try:
                    target = int(url.split("=")[1])
                except Exception:
                    pass
        if target:
            if isinstance(target, str) and target in ["botfather", "telegram"]:
                continue
            try:
                await client.get_chat_member(cid, target)
            except (UserNotParticipant, PeerIdInvalid, RPCError):
                return True
    return False


# ── Passive learning helper — fire-and-forget ─────────────────────────────────

def _trigger_passive_learn_spam(text: str, confidence: float = 1.0) -> None:
    """
    Trigger passive learning sebagai spam secara fire-and-forget.
    Selalu menggunakan force_learn=True karena berasal dari regex (konfirmasi pasti spam).
    Tidak menunggu hasil — jangan panggil await di sini.
    """
    try:
        from nexus.ai_core import nexus_ai_passive_observe
        asyncio.create_task(
            nexus_ai_passive_observe(text, True, confidence, force_learn=True)
        )
    except Exception:
        pass  # Non-fatal — passive learning opsional


# ─────────────────────────────────────────────────────────────────────────────
#  Main filter (group=2)
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.group & ~filters.service, group=2)
async def main_antispam_filter(client, message):
    if not message.from_user:
        return
    cid, uid, mid = message.chat.id, message.from_user.id, message.id

    if is_message_handled(cid, mid):
        return

    if await is_admin(client, cid, uid):
        return

    if await free_col.find_one({"user_id": uid, "chat_id": cid}):
        return

    content = (message.text or message.caption or "").strip()
    if not content or content.startswith("/"):
        return

    is_short = (1 <= len(content) <= 3) or content.isdigit()
    cfg      = await get_config(cid)
    now_ts   = time.time()
    now_dt   = datetime.now(TZ_WIB)
    norm     = simplify(content)
    regex_safe      = remove_mentions_for_regex(message)
    teks_super_clean = pipeline_pembersihan(content)

    # 1. Regex global (Owner Regex)
    for pat, raw in await _get_global_patterns():
        if match_with_leet(pat, regex_safe) or (teks_super_clean and pat.search(teks_super_clean)):
            mark_message_handled(cid, mid)
            await delete_queue.put((cid, [mid]))
            asyncio.create_task(insert_group_action_log(
                cid, "HAPUS", f"Filter kata global – {raw[:60]}",
                uid, message.from_user.first_name or str(uid), content[:100],
            ))
            asyncio.create_task(check_and_punish(client, message, "filter kata global", content[:100]))
            # [PASSIVE LEARNING v3.1] Regex global konfirmasi pasti spam → force_learn
            _trigger_passive_learn_spam(content, confidence=1.0)
            return

    # 2. Regex lokal (Group Filter)
    for pat, raw in await _get_local_patterns(cid):
        if match_with_leet(pat, regex_safe) or (teks_super_clean and pat.search(teks_super_clean)):
            mark_message_handled(cid, mid)
            await delete_queue.put((cid, [mid]))
            asyncio.create_task(insert_group_action_log(
                cid, "HAPUS", f"Filter kata grup – {raw[:60]}",
                uid, message.from_user.first_name or str(uid), content[:100],
            ))
            asyncio.create_task(check_and_punish(client, message, "filter kata grup", content[:100]))
            # [PASSIVE LEARNING v3.1] Regex lokal grup → force_learn
            _trigger_passive_learn_spam(content, confidence=1.0)
            return

    # 3. External mention
    if await _is_external_mention(client, message):
        mark_message_handled(cid, mid)
        await delete_queue.put((cid, [mid]))
        asyncio.create_task(insert_group_action_log(
            cid, "HAPUS", "Mention pengguna luar grup",
            uid, message.from_user.first_name or str(uid), content[:100],
        ))
        asyncio.create_task(check_and_punish(client, message, "mention pengguna luar", content[:100]))
        return

    # 3.5 Link detector
    if _has_url_entity(message):
        mark_message_handled(cid, mid)
        await delete_queue.put((cid, [mid]))
        asyncio.create_task(insert_group_action_log(
            cid, "HAPUS", "Link terdeteksi dalam pesan",
            uid, message.from_user.first_name or str(uid), content[:100],
        ))
        asyncio.create_task(check_and_punish(client, message, "link dalam pesan", content[:100]))
        return

    # ─────────────────────────────────────────────────────────────────────────
    # 4. Anti duplikasi lokal
    # ─────────────────────────────────────────────────────────────────────────
    if cfg.get("local") is True and not message.via_bot and not is_short:

        # Cek apakah user sedang dalam masa mute
        mute_rec = await get_local_mute(cid, uid)
        if mute_rec.get("muted_until", 0.0) > now_ts:
            mark_message_handled(cid, mid)
            await delete_queue.put((cid, [mid]))
            return

        # Cari duplikat di riwayat lokal user
        matched_old = None
        async for old in messages_db.find(
            {"chat_id": cid, "user_id": uid, "type": "local_track"}
        ).sort("time", -1).limit(10):
            old_norm = old.get("norm_txt", "")
            if not old_norm:
                continue
            if fuzz.ratio(norm, old_norm) >= 90:
                if (now_ts - old["time"]) < cfg["expiry"]:
                    matched_old = old
                    break

        if matched_old is not None:
            mark_message_handled(cid, mid)
            await delete_queue.put((cid, [matched_old["msg_id"], mid]))
            asyncio.create_task(insert_group_action_log(
                cid, "HAPUS", "Pesan duplikat berulang",
                uid, message.from_user.first_name or str(uid), content[:100],
            ))

            # Sistem hukuman terpusat — mute jika 10× berturut-turut
            asyncio.create_task(check_and_punish(
                client, message, "spam duplikat lokal", content[:100]
            ))

            # Peringatan singkat jika belum pernah diwarnai (hanya 1 kali)
            if not matched_old.get("warned", False):
                msg_warn = await message.reply(
                    f"{message.from_user.mention} jangan kirim pesan yang sama",
                    parse_mode=ParseMode.HTML,
                )
                asyncio.create_task(auto_delete_reply([msg_warn], delay=5))

            # Perbarui rekaman pesan di DB
            await messages_db.delete_one({"_id": matched_old["_id"]})
            new_id = f"loc_{cid}_{uid}_{hashlib.md5(content.encode()).hexdigest()}_{int(now_ts*1000)}"
            await messages_db.insert_one({
                "_id": new_id,
                "time": now_ts,
                "msg_id": mid,
                "chat_id": cid,
                "user_id": uid,
                "norm_txt": norm,
                "type": "local_track",
                "createdAt": now_dt,
                "warned": True,
            })
            return

        # Pesan bersih lokal → simpan ke DB
        new_id = f"loc_{cid}_{uid}_{mid}_{int(now_ts * 1000)}"
        await messages_db.insert_one({
            "_id": new_id,
            "time": now_ts,
            "msg_id": mid,
            "chat_id": cid,
            "user_id": uid,
            "norm_txt": norm,
            "type": "local_track",
            "createdAt": now_dt,
            "warned": False,
        })
        all_docs = [d async for d in messages_db.find(
            {"chat_id": cid, "user_id": uid, "type": "local_track"}
        ).sort("time", -1)]
        if len(all_docs) > 5:
            old_ids = [d["_id"] for d in all_docs[5:]]
            await messages_db.delete_many({"_id": {"$in": old_ids}})

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Anti duplikasi global (gcast)
    # ─────────────────────────────────────────────────────────────────────────
    if cfg.get("global") is True and not is_short:
        content_hash = hashlib.md5(content.encode()).hexdigest()
        global_key   = f"glob_{uid}_{content_hash}"
        existing     = await messages_db.find_one({"_id": global_key})

        if existing and (now_ts - existing["time"]) < GLOBAL_EXPIRY:
            locs = existing.get("locations", [])

            # Selalu perbarui/tambahkan entri cid+mid saat ini agar mid terkini tercatat
            # (jika already_tracked, mid lama diganti mid baru agar pesan terkini ikut dihapus)
            locs = [loc for loc in locs if loc[0] != cid]
            locs.append([cid, mid])
            await messages_db.update_one(
                {"_id": global_key},
                {"$set": {
                    "locations": locs,
                    "time": now_ts,
                    "createdAt": now_dt,
                }},
            )

            unique_chats = {loc[0] for loc in locs}
            if len(unique_chats) > 1:
                n_chats = len(unique_chats)
                for loc_cid, loc_mid in locs:
                    t_cfg = await get_config(loc_cid)
                    if t_cfg.get("global") is True:
                        mark_message_handled(loc_cid, loc_mid)
                        await delete_queue.put((loc_cid, [loc_mid]))
                        # FIX Bug 2: catat ke group_action_log tiap grup yang terdampak
                        asyncio.create_task(insert_group_action_log(
                            loc_cid, "HAPUS",
                            f"Anti-duplikat gcast global – dikirim ke {n_chats} grup sekaligus",
                            uid, message.from_user.first_name or str(uid), content[:100],
                        ))
                        # Hitung punishment gcast hanya di grup yang aktif global.
                        # Setiap grup menghitung sendiri (per user per grup).
                        # Grup yang mematikan global (global=False) TIDAK dihapus
                        # pesannya dan TIDAK dihitung punishment-nya.
                        if loc_cid == cid:
                            asyncio.create_task(check_and_punish(
                                client, message, "anti-gcast global", content[:100]
                            ))
                        else:
                            asyncio.create_task(_gcast_punish_other_group(
                                client, loc_cid, uid, content[:100]
                            ))
        else:
            await messages_db.update_one(
                {"_id": global_key},
                {"$set": {
                    "time": now_ts,
                    "createdAt": now_dt,
                    "locations": [[cid, mid]],
                }},
                upsert=True,
            )


async def _gcast_punish_other_group(
    client,
    chat_id: int,
    user_id: int,
    konten: str,
) -> None:
    """
    Hitung punishment gcast untuk user di grup lain (bukan grup pendeteksi).
    Dipanggil hanya jika grup tersebut aktif global (global=True).
    Menggunakan increment langsung tanpa objek message penuh karena
    kita tidak memiliki message object untuk grup lain.
    """
    from database import get_local_mute, increment_local_spam, apply_local_mute
    from core.punishment import do_mute, SPAM_MUTE_THRESHOLD
    import time as _time
    now_ts = _time.time()
    mute_rec = await get_local_mute(chat_id, user_id)
    if mute_rec.get("muted_until", 0.0) > now_ts:
        return
    updated = await increment_local_spam(chat_id, user_id)
    consec  = updated.get("consec_spam", 1)
    if consec < SPAM_MUTE_THRESHOLD:
        return
    duration_secs, _level = await apply_local_mute(chat_id, user_id)
    duration_min = duration_secs // 60
    await do_mute(client, chat_id, user_id, duration_secs)
    try:
        from database import insert_group_action_log
        await insert_group_action_log(
            chat_id, "MUTE",
            f"Mute {duration_min} menit – anti-gcast global 10× berturut-turut",
            user_id, str(user_id), konten,
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  group=10 — Tracker pesan bersih
#  Berjalan SETELAH semua filter (CAS=-1, bio=1, antispam=2, nexus=5).
#  Jika pesan tidak ditangani oleh filter manapun → reset hitungan spam.
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.group & ~filters.service, group=10)
async def _clean_message_tracker(client, message):
    """Reset hitungan spam saat pesan lolos semua filter (pesan bersih)."""
    if not message.from_user or message.from_user.is_bot:
        return
    cid = message.chat.id
    mid = message.id
    uid = message.from_user.id

    if not is_message_handled(cid, mid):
        asyncio.create_task(_reset_mute_async(cid, uid))


async def _reset_mute_async(chat_id: int, user_id: int) -> None:
    """Reset hitungan spam dan level hukuman untuk user yang kirim pesan bersih."""
    try:
        await reset_local_mute(chat_id, user_id)
    except Exception:
        pass
