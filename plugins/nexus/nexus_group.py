"""
plugins/nexus/nexus_group.py
─────────────────────────────
Handler grup untuk sistem Nexus AI:
  - /spam  (admin grup) → simpan kalimat ke MongoDB nexus_kalimat
  - Silent filter (group=5) → cocokkan pesan ke nexus_regex, cek whitelist,
    log ke channel detail alasan dihapus / alasan dibebaskan whitelist
  - on_chat_member_updated → track masuk/keluar bot di grup

TIDAK bertabrakan dengan filter refactor karena:
  - Refactor filter: group=2 (antispam.py), group=1 (bio.py), group=-1 (cas.py)
  - Nexus silent filter: group=5 (lebih akhir)
  - /spam command: filter commands grup, tidak overlap dengan command lain

ATURAN KEROYOKAN (v2):
  Jika sebuah pesan dicocokkan oleh 2 atau lebih pola spam BERBEDA sekaligus,
  whitelist Nexus WAJIB KALAH — pesan dihapus tanpa pengecualian.
  Analogi racun: 2+ racun berbeda dalam 1 kalimat → 1 penawar tidak cukup.
  Log ke channel akan menyebutkan semua pola yang terpicu DAN pola mana yang
  belum memiliki penawar whitelist.

v3.1 — Perubahan passive learning:
  - CategoryDetector berjalan PROAKTIF di setiap pesan (group invite, porn,
    scam, promo_viral, bio_promo)
  - ContextFilter mencegah false positive sebelum eksekusi
  - Setiap keputusan (hapus / lolos) diteruskan ke PassiveLearner:
      · Dihapus via regex/kategori → force_learn=True (sudah pasti spam)
      · Dihapus via Bayes+Feature → force_learn=False, pakai confidence
      · Lolos bersih → AI belajar ham (throttled 1/8)
"""

import os
import re
import asyncio
from datetime import datetime, timezone, timedelta

from pyrogram import Client, filters
from pyrogram.types import Message, ChatMemberUpdated
from pyrogram.enums import ChatMemberStatus, ParseMode

from database import (
    nexus_insert_kalimat,
    nexus_get_all_regex,
    nexus_track_grup,
    nexus_remove_grup,
    nexus_whitelist_get_all,
    nexus_actlog_insert,
    insert_group_action_log,
    delete_queue,
    db,
    is_message_handled,
)

_free_col   = db["free_per_group"]
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", 0))
TZ_WIB      = timezone(timedelta(hours=7))

from plugins.nexus.engine import pipeline_pembersihan, generate_regex_otomatis_async
from core.punishment import check_and_punish
import admin_session as _adm_sess

# ── [NEXUS AI CORE v3.1] Import — non-fatal jika tidak ada ───────────────────
try:
    from nexus.ai_core import (
        nexus_ai_auto_detect,
        nexus_ai_learn_spam,
        nexus_ai_passive_observe,
    )
    from nexus.ai_core.category_detector import get_category_detector
    from nexus.ai_core.context_filter    import get_context_filter
    _AI_CORE_AVAILABLE = True
except Exception as _ai_import_err:
    _AI_CORE_AVAILABLE = False
    print(f"[nexus_group] NexusAICore tidak tersedia (opsional): {_ai_import_err}")
# ── [/NEXUS AI CORE v3.1] ────────────────────────────────────────────────────


# ── /spam — Admin grup lapor spam ─────────────────────────────────────────────

@Client.on_message(filters.command("spam") & filters.group)
async def nexus_spam_handler(client: Client, message: Message):
    cid = message.chat.id
    uid = message.from_user.id if message.from_user else None

    from database import is_admin
    if not await is_admin(client, cid, uid):
        return
    if not message.reply_to_message:
        return

    teks_mentah = message.reply_to_message.text or message.reply_to_message.caption
    if not teks_mentah:
        return

    teks_clean = pipeline_pembersihan(teks_mentah)
    if not teks_clean:
        return

    await nexus_insert_kalimat(teks_clean)
    await nexus_track_grup(cid, message.chat.title or str(cid))

    # ── [NEXUS AI CORE] Online learning dari laporan /spam ────────────────────
    if _AI_CORE_AVAILABLE:
        asyncio.create_task(_ai_learn_background(teks_clean))
    # ── [/NEXUS AI CORE] ─────────────────────────────────────────────────────

    try:
        await message.reply_to_message.delete()
        await message.delete()
        notif = await client.send_message(
            chat_id=cid,
            text=(
                f"✅ **Laporan Diterima — Nexus AI**\n"
                f"Pesan berhasil dihapus & konten diamankan ke database.\n"
                f"Engine AI akan memproses pola mutasinya pada siklus 00:00 WIB. ⏳"
            )
        )
        await asyncio.sleep(5)
        await notif.delete()
    except Exception as e:
        print(f"[nexus_group] spam handler error: {e}")


# ── [NEXUS AI CORE] Background helpers (fire-and-forget) ─────────────────────

async def _ai_learn_background(teks: str) -> None:
    """Background: update AI Core dari laporan /spam tanpa block handler."""
    try:
        await nexus_ai_learn_spam(teks)
    except Exception as e:
        print(f"[nexus_group] AI learn background error (non-fatal): {e}")


async def _ai_passive_background(
    teks: str,
    executed_as_spam: bool,
    confidence: float,
    force_learn: bool = False,
) -> None:
    """
    Background: passive learning setelah setiap keputusan filter.

    Args:
        teks:             teks pesan
        executed_as_spam: True jika pesan dihapus sebagai spam
        confidence:       skor confidence AI
        force_learn:      True untuk penghapusan rule-based (regex, CategoryDetector)
                          — melewati threshold confidence, langsung belajar
    """
    if not _AI_CORE_AVAILABLE:
        return
    try:
        await nexus_ai_passive_observe(teks, executed_as_spam, confidence, force_learn)
    except Exception as e:
        print(f"[nexus_group] passive observe error (non-fatal): {e}")

# ── [/NEXUS AI CORE] ─────────────────────────────────────────────────────────


# ── Cache pattern & whitelist ──────────────────────────────────────────────────

_nexus_regex_cache: list[tuple] = []
_nexus_regex_cache_ts: float    = 0.0

_nexus_wl_cache: list[dict] = []
_nexus_wl_cache_ts: float   = 0.0

_NEXUS_REGEX_TTL = 300  # 5 menit


async def _get_nexus_patterns() -> list[tuple]:
    """Return list of (compiled_pattern, kata_kunci, pola_str)."""
    global _nexus_regex_cache, _nexus_regex_cache_ts
    import time
    now = time.monotonic()
    if now - _nexus_regex_cache_ts < _NEXUS_REGEX_TTL:
        return _nexus_regex_cache

    docs   = await nexus_get_all_regex()
    result = []
    for d in docs:
        pola_str   = d.get("pola", "")
        kata_kunci = d.get("kata_kunci", "")
        try:
            result.append((re.compile(pola_str, re.IGNORECASE), kata_kunci, pola_str))
        except re.error:
            pass
    _nexus_regex_cache    = result
    _nexus_regex_cache_ts = now
    return result


async def _get_whitelist_docs() -> list[dict]:
    global _nexus_wl_cache, _nexus_wl_cache_ts
    import time
    now = time.monotonic()
    if now - _nexus_wl_cache_ts < _NEXUS_REGEX_TTL:
        return _nexus_wl_cache
    _nexus_wl_cache    = await nexus_whitelist_get_all()
    _nexus_wl_cache_ts = now
    return _nexus_wl_cache


def invalidate_nexus_wl_cache():
    """
    Paksa cache whitelist kadaluarsa.
    Dipanggil dari nexus_handlers.py setelah operasi tambah/hapus/clear whitelist.
    """
    global _nexus_wl_cache_ts
    _nexus_wl_cache_ts = 0.0


# ── Parser pola interlock ──────────────────────────────────────────────────────

def _count_lookaheads(pola: str) -> int:
    return len(re.findall(r"\(\?=\.\*", pola))


def _extract_lookahead_groups(pola: str) -> list[str]:
    groups = []
    i = 0
    while i < len(pola):
        idx = pola.find("(?=.*(", i)
        if idx == -1:
            break
        depth       = 0
        j           = idx + 6
        start_inner = j
        while j < len(pola):
            if pola[j] == "(":
                depth += 1
            elif pola[j] == ")":
                if depth == 0:
                    groups.append(pola[start_inner:j])
                    i = j + 1
                    break
                depth -= 1
            j += 1
        else:
            break
    return groups


def _parse_alts(group_str: str) -> frozenset[str]:
    s = group_str.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    return frozenset(a.strip() for a in s.split("|") if a.strip())


# ── Logika Whitelist Nexus ─────────────────────────────────────────────────────

def _alt_is_covered_by_wl_set(sp_alt: str, wl_set: frozenset[str]) -> bool:
    """
    Cek apakah satu alternatif spam (sp_alt) dilindungi oleh salah satu
    alternatif whitelist di wl_set.

    Dua cara cocok:
      1. Exact match  — string identik (cepat, kasus normal)
      2. Regex-match  — WL alt dipakai sebagai pola, sp_alt sebagai subjek.
         Jika wl_alt cocok di dalam string sp_alt, berarti WL punya cakupan
         yang lebih luas atau setara → spam alt dianggap terlindungi.

    Contoh: sp_alt=r'\bkon\S*' vs wl_alt=r'\bkon.*'
      → re.search(r'\bkon.*', r'\bkon\S*') cocok → terlindungi.
    """
    if sp_alt in wl_set:
        return True
    for wl_alt in wl_set:
        try:
            if re.search(wl_alt, sp_alt):
                return True
        except re.error:
            pass
    return False


def _sp_set_covered(sp_set: frozenset[str], wl_set: frozenset[str]) -> bool:
    """Return True jika SEMUA alternatif dalam sp_set dilindungi wl_set."""
    return all(_alt_is_covered_by_wl_set(sp_alt, wl_set) for sp_alt in sp_set)


def _is_whitelisted(spam_pola: str, whitelist_docs: list[dict]) -> tuple[bool, dict | None]:
    """
    Cek apakah pola spam dilindungi Whitelist Nexus.
    Whitelist hanya berlaku untuk pola dari nexus_regex_db (bukan CategoryDetector).
    Pola kategori ([CAT-*]) TIDAK diperiksa ke whitelist — langsung dieksekusi.
    """
    # Pola dari CategoryDetector atau AI tidak memiliki whitelist (langsung hapus)
    if spam_pola.startswith("[NEXUS_CATEGORY_DETECTOR]") or spam_pola.startswith("[NEXUS_AI_CORE"):
        return False, None

    spam_count = _count_lookaheads(spam_pola)
    if spam_count == 0:
        return False, None

    spam_groups = _extract_lookahead_groups(spam_pola)
    if len(spam_groups) != spam_count:
        return False, None

    spam_alts = [_parse_alts(g) for g in spam_groups]
    spam_alts = [s for s in spam_alts if s]
    if len(spam_alts) != spam_count:
        return False, None

    for doc in whitelist_docs:
        wl_pola = doc.get("pola", "")
        if not wl_pola:
            continue

        wl_count = _count_lookaheads(wl_pola)

        if wl_count == 0:
            if spam_count != 1:
                continue
            wl_alts = [_parse_alts(wl_pola)]
        else:
            if wl_count != spam_count:
                continue
            wl_groups = _extract_lookahead_groups(wl_pola)
            if len(wl_groups) != wl_count:
                continue
            wl_alts = [_parse_alts(g) for g in wl_groups]
            wl_alts = [s for s in wl_alts if s]
            if len(wl_alts) != wl_count:
                continue

        used        = [False] * len(wl_alts)
        all_matched = True
        for sp_set in spam_alts:
            found = False
            for idx, wl_set in enumerate(wl_alts):
                if not used[idx] and _sp_set_covered(sp_set, wl_set):
                    used[idx] = True
                    found     = True
                    break
            if not found:
                all_matched = False
                break

        if all_matched:
            return True, doc

    return False, None


# ── Helper log ke channel ──────────────────────────────────────────────────────

async def _log_nexus_deleted(
    client:     Client,
    message:    Message,
    kata_kunci: str,
    pola_str:   str,
    content:    str,
) -> None:
    if not LOG_CHANNEL:
        return

    uid          = message.from_user.id
    cid          = message.chat.id
    user_mention = f"<a href='tg://user?id={uid}'>{message.from_user.first_name}</a>"
    waktu        = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")

    kata_display = kata_kunci.split("]", 1)[-1].strip() if "]" in kata_kunci else kata_kunci

    # Label sumber deteksi
    if "[CAT-" in kata_kunci:
        sumber = "🏷️ CategoryDetector"
    elif "[AI-AUTO" in kata_kunci:
        sumber = "🤖 AI Core (Bayes+Feature)"
    else:
        sumber = "📋 Regex Database"

    text = (
        "<b>❖ NEXUS AI — PESAN DIHAPUS ❖</b>\n\n"
        f"🗑️ <b>Eksekusi Filter: {sumber}</b>\n"
        f"◈ <b>User:</b> {user_mention} (<code>{uid}</code>)\n"
        f"◈ <b>Grup:</b> {message.chat.title} (<code>{cid}</code>)\n"
        f"◈ <b>Waktu:</b> <code>{waktu}</code>\n\n"
        "<b>▰▰▰ PEMICU SPAM AI ▰▰▰</b>\n"
        f"🔑 <b>Kata Kunci Pemicu:</b> <code>{kata_display}</code>\n"
        f"💥 <b>Pola Interlock:</b>\n<code>{pola_str[:300]}</code>\n\n"
        "<b>▰▰▰ KONTEN PESAN ▰▰▰</b>\n"
        f"<code>{content[:400]}</code>"
    )
    try:
        await client.send_message(
            LOG_CHANNEL, text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[nexus_group] log_deleted error: {e}")


async def _log_nexus_whitelist_spared(
    client:       Client,
    message:      Message,
    kata_kunci:   str,
    spam_pola:    str,
    wl_doc:       dict,
    content:      str,
) -> None:
    if not LOG_CHANNEL:
        return

    uid          = message.from_user.id
    cid          = message.chat.id
    user_mention = f"<a href='tg://user?id={uid}'>{message.from_user.first_name}</a>"
    waktu        = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")

    kata_display = kata_kunci.split("]", 1)[-1].strip() if "]" in kata_kunci else kata_kunci
    wl_raw       = wl_doc.get("raw", "—")
    wl_kata_list = wl_doc.get("kata_list", [])
    wl_pola      = wl_doc.get("pola", "")
    wl_kata_str  = ", ".join(f"<code>{k}</code>" for k in wl_kata_list) if wl_kata_list else f"<code>{wl_raw}</code>"

    text = (
        "<b>❖ NEXUS AI — PESAN DIAMANKAN WHITELIST ❖</b>\n\n"
        "🛡️ <b>Pesan Lolos Penghapusan (Dilindungi Whitelist Nexus)</b>\n"
        f"◈ <b>User:</b> {user_mention} (<code>{uid}</code>)\n"
        f"◈ <b>Grup:</b> {message.chat.title} (<code>{cid}</code>)\n"
        f"◈ <b>Waktu:</b> <code>{waktu}</code>\n\n"
        "<b>▰▰▰ POLA SPAM AI YANG TERPICU ▰▰▰</b>\n"
        f"🔑 <b>Kata Kunci Spam:</b> <code>{kata_display}</code>\n"
        f"💥 <b>Pola Interlock Spam:</b>\n<code>{spam_pola[:300]}</code>\n\n"
        "<b>▰▰▰ DILINDUNGI OLEH WHITELIST NEXUS ▰▰▰</b>\n"
        f"🛡️ <b>Kata Aman:</b> {wl_kata_str}\n"
        f"📝 <b>Raw Whitelist:</b> <code>{wl_raw}</code>\n"
        f"🔒 <b>Pola Whitelist:</b>\n<code>{wl_pola[:300]}</code>\n\n"
        "<b>▰▰▰ KONTEN PESAN ▰▰▰</b>\n"
        f"<code>{content[:400]}</code>"
    )
    try:
        await client.send_message(
            LOG_CHANNEL, text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[nexus_group] log_whitelist_spared error: {e}")


async def _log_nexus_keroyok(
    client:              Client,
    message:             Message,
    semua_pola:          list[tuple[str, str]],
    pola_tanpa_penawar:  list[tuple[str, str]],
    content:             str,
) -> None:
    if not LOG_CHANNEL:
        return

    uid          = message.from_user.id
    cid          = message.chat.id
    user_mention = f"<a href='tg://user?id={uid}'>{message.from_user.first_name}</a>"
    waktu        = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")
    jumlah_racun = len(semua_pola)

    daftar_racun = ""
    for i, (kk, ps) in enumerate(semua_pola, 1):
        kk_display = kk.split("]", 1)[-1].strip() if "]" in kk else kk
        daftar_racun += (
            f"<b>Racun #{i}:</b> <code>{kk_display}</code>\n"
            f"<code>{ps[:200]}</code>\n\n"
        )

    if pola_tanpa_penawar:
        daftar_tanpa_penawar = ""
        for i, (kk, ps) in enumerate(pola_tanpa_penawar, 1):
            kk_display = kk.split("]", 1)[-1].strip() if "]" in kk else kk
            daftar_tanpa_penawar += (
                f"<b>Pola #{i}:</b> <code>{kk_display}</code>\n"
                f"<code>{ps[:200]}</code>\n\n"
            )
        info_penawar = (
            "<b>▰▰▰ RACUN TANPA PENAWAR ▰▰▰</b>\n"
            f"⚗️ <b>{len(pola_tanpa_penawar)} dari {jumlah_racun} pola tidak memiliki "
            f"Whitelist Nexus:</b>\n\n"
            f"{daftar_tanpa_penawar}"
        )
    else:
        info_penawar = (
            "<b>▰▰▰ STATUS PENAWAR ▰▰▰</b>\n"
            "⚗️ <b>Catatan:</b> Semua pola memiliki whitelist individual, "
            "namun <b>ATURAN KEROYOKAN</b> membatalkan semua perlindungan karena "
            f"{jumlah_racun} racun berbeda menyerang secara bersamaan. "
            "Diperlukan satu whitelist tunggal yang mencakup SEMUA kata sasaran.\n\n"
        )

    text = (
        "<b>❖ NEXUS AI — PESAN DIHAPUS (ATURAN KEROYOKAN) ❖</b>\n\n"
        "☠️ <b>Eksekusi Filter: MULTI-PATTERN AMBUSH</b>\n"
        f"◈ <b>User:</b> {user_mention} (<code>{uid}</code>)\n"
        f"◈ <b>Grup:</b> {message.chat.title} (<code>{cid}</code>)\n"
        f"◈ <b>Waktu:</b> <code>{waktu}</code>\n\n"
        f"⚠️ <b>{jumlah_racun} pola spam berbeda mendeteksi kecocokan sekaligus!</b>\n"
        "<i>Whitelist tidak berlaku — satu penawar tidak bisa menetralkan "
        "semua racun sekaligus.</i>\n\n"
        "<b>▰▰▰ SEMUA POLA YANG TERPICU ▰▰▰</b>\n"
        f"{daftar_racun}"
        f"{info_penawar}"
        "<b>▰▰▰ KONTEN PESAN ▰▰▰</b>\n"
        f"<code>{content[:400]}</code>"
    )
    try:
        await client.send_message(
            LOG_CHANNEL, text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[nexus_group] log_keroyok error: {e}")


# ─── Helper: ekstrak confidence dari kata_kunci ───────────────────────────────

def _extract_confidence(kata_kunci: str) -> float:
    """Ekstrak nilai confidence dari string kata_kunci format TAG xx% ..."""
    for tag in ["[AI-AUTO ", "[CAT-"]:
        if tag in kata_kunci:
            try:
                rest = kata_kunci.split(tag, 1)[1]
                # Format: "AI-AUTO 72%" atau "CAT-PORN 65%"
                pct_str = rest.split("%]")[0].split(" ")[-1]
                return float(pct_str) / 100.0
            except Exception:
                pass
    return 0.0


def _is_category_based(kata_kunci: str) -> bool:
    """Return True jika deteksi berasal dari CategoryDetector (rule-based)."""
    return "[CAT-" in kata_kunci


# ── Silent Filter ──────────────────────────────────────────────────────────────

@Client.on_message(filters.group & ~filters.service, group=5)
async def nexus_silent_filter(client: Client, message: Message):
    if not message.from_user:
        return

    cid = message.chat.id
    uid = message.from_user.id
    mid = message.id

    # ── PINTU BERURUTAN: Cek apakah filter sebelumnya sudah menangani ─────────
    if is_message_handled(cid, mid):
        return

    # VIP: bebas dari seluruh filter Nexus AI
    try:
        if await _free_col.find_one({"user_id": uid, "chat_id": cid}):
            return
    except Exception as e:
        print(f"[nexus_silent_filter] gagal cek VIP: {e}")
        return

    content = (message.text or message.caption or "").strip()
    if not content or content.startswith("/"):
        return

    teks_clean = pipeline_pembersihan(content)
    if not teks_clean:
        return

    patterns       = await _get_nexus_patterns()
    whitelist_docs = await _get_whitelist_docs()

    # ── Fase 1: Kumpulkan SEMUA pola regex yang cocok ─────────────────────────
    matched: list[tuple[str, str]] = []

    for compiled, kata_kunci, pola_str in patterns:
        if compiled.search(teks_clean):
            matched.append((kata_kunci, pola_str))

    # ── [NEXUS AI v3.1] Fase 2: CategoryDetector — deteksi proaktif ──────────
    #
    # CategoryDetector berjalan SETIAP pesan, mendeteksi:
    #   GROUP_INVITE, PORN, SCAM, PROMO_VIRAL, BIO_PROMO
    #
    # ContextFilter digunakan SEBELUM keputusan untuk mencegah false positive.
    # CategoryDetector hanya menambah ke matched[] jika regex belum menangkap.
    # ─────────────────────────────────────────────────────────────────────────
    if not matched and _AI_CORE_AVAILABLE:
        try:
            cat_result = get_category_detector().detect(content)
            if cat_result.hit and cat_result.confidence >= 0.60:
                # Terapkan ContextFilter untuk mencegah false positive
                ctx      = get_context_filter().analyze(content)
                adj_conf = ctx.apply(cat_result.confidence)

                # Hanya eksekusi jika:
                # - Skor setelah konteks masih >= 0.55
                # - Bukan kalimat diskusi/pelaporan
                # - Bukan kalimat pertanyaan polos
                if (
                    adj_conf >= 0.55
                    and not ctx.is_likely_discussion
                    and not ctx.is_question
                ):
                    from nexus.ai_core.category_detector import CategoryResult
                    adj_result = CategoryResult(
                        category   = cat_result.category,
                        hit        = True,
                        confidence = adj_conf,
                        reasons    = cat_result.reasons + [
                            r for r in ctx.reasons
                            if not r.startswith("[CTX]")
                        ],
                        all_scores = cat_result.all_scores,
                    )
                    matched.append((adj_result.as_kata_kunci(), adj_result.as_pola_str()))
        except Exception as _cat_err:
            print(f"[nexus_silent_filter] CategoryDetector error (non-fatal): {_cat_err}")

    # ── [NEXUS AI v3.1] Fase 3: AI Bayes+Feature — hanya jika belum ada hit ──
    #
    # AI adalah "otak tambahan" — aktif ketika regex DAN CategoryDetector
    # tidak menangkap spam. auto_detect() sudah mencakup 6 layer deteksi.
    # ─────────────────────────────────────────────────────────────────────────
    if not matched and _AI_CORE_AVAILABLE:
        try:
            _meta = {
                "is_forwarded": bool(
                    getattr(message, "forward_from", None)
                    or getattr(message, "forward_from_chat", None)
                ),
                "has_bio_link": False,
            }
            _ai_result = await nexus_ai_auto_detect(
                content,
                metadata=_meta,
                min_confidence=0.72,
            )
            if _ai_result is not None:
                matched.append((_ai_result.as_kata_kunci(), _ai_result.as_pola_str()))
        except Exception as _ai_err:
            print(f"[nexus_silent_filter] AI Core auto-detect error (non-fatal): {_ai_err}")
    # ── [/NEXUS AI v3.1] ─────────────────────────────────────────────────────

    # ── Tidak ada yang terdeteksi → pesan aman ────────────────────────────────
    if not matched:
        # [PASSIVE LEARNING] Catat sebagai HAM — AI belajar dari pesan bersih
        if _AI_CORE_AVAILABLE:
            asyncio.create_task(_ai_passive_background(content, False, 0.0))
        return

    # ── Fase 4: Tentukan nasib pesan ──────────────────────────────────────────

    if len(matched) == 1:
        # ── Kasus NORMAL: tepat 1 pola cocok → cek whitelist ─────────────────
        kata_kunci, pola_str = matched[0]

        try:
            is_safe, wl_doc = _is_whitelisted(pola_str, whitelist_docs)
        except Exception as e:
            print(f"[nexus_silent_filter] error whitelist check: {e}")
            is_safe, wl_doc = True, None   # fail-safe

        if is_safe:
            asyncio.create_task(_log_nexus_whitelist_spared(
                client, message, kata_kunci, pola_str, wl_doc or {}, content
            ))
            asyncio.create_task(nexus_actlog_insert(
                aksi       = "WHITELIST",
                user_id    = uid,
                user_name  = (message.from_user.first_name or str(uid))[:60],
                chat_id    = cid,
                chat_title = (message.chat.title or str(cid))[:60],
                alasan     = kata_kunci[:200],
                confidence = 0.0,
                content    = content,
            ))
            # [PASSIVE LEARNING] Pesan dilindungi whitelist = ham
            if _AI_CORE_AVAILABLE:
                asyncio.create_task(_ai_passive_background(content, False, 0.0))
            return   # Dilindungi whitelist → biarkan pesan

        # Tidak ada whitelist → hapus + log
        _conf        = _extract_confidence(kata_kunci)
        _force       = _is_category_based(kata_kunci)  # CategoryDetector = rule-based = force
        await delete_queue.put((cid, [message.id]))
        asyncio.create_task(check_and_punish(client, message, "Nexus AI", content[:100]))
        asyncio.create_task(_log_nexus_deleted(
            client, message, kata_kunci, pola_str, content
        ))
        asyncio.create_task(nexus_actlog_insert(
            aksi       = "HAPUS",
            user_id    = uid,
            user_name  = (message.from_user.first_name or str(uid))[:60],
            chat_id    = cid,
            chat_title = (message.chat.title or str(cid))[:60],
            alasan     = kata_kunci[:200],
            confidence = _conf,
            content    = content,
        ))
        # FIXED: Tulis juga ke group_action_log agar muncul di panel "Log Aktivitas"
        asyncio.create_task(insert_group_action_log(
            cid, "HAPUS",
            f"Nexus AI: {(kata_kunci.split(']', 1)[-1].strip() if ']' in kata_kunci else kata_kunci)[:80]}",
            uid, (message.from_user.first_name or str(uid))[:50],
            content[:100],
        ))
        # [PASSIVE LEARNING] Pesan dihapus → AI auto-learn spam
        # force_learn=True untuk CategoryDetector (rule-based, sudah pasti spam)
        if _AI_CORE_AVAILABLE:
            asyncio.create_task(_ai_passive_background(content, True, _conf, _force))
        return

    # ── Kasus KEROYOKAN: 2+ pola berbeda cocok sekaligus ─────────────────────
    pola_tanpa_penawar: list[tuple[str, str]] = []
    for kata_kunci, pola_str in matched:
        try:
            is_safe_individual, _ = _is_whitelisted(pola_str, whitelist_docs)
        except Exception:
            is_safe_individual = False
        if not is_safe_individual:
            pola_tanpa_penawar.append((kata_kunci, pola_str))

    # Jika SEMUA pola punya penawar whitelist → pesan aman, tidak perlu dihapus
    if not pola_tanpa_penawar:
        _kk_gabung = " + ".join(kk for kk, _ in matched[:3])
        _ps_gabung = " | ".join(ps for _, ps in matched[:2])
        asyncio.create_task(_log_nexus_whitelist_spared(
            client, message, _kk_gabung, _ps_gabung, {}, content
        ))
        asyncio.create_task(nexus_actlog_insert(
            aksi       = "WHITELIST",
            user_id    = uid,
            user_name  = (message.from_user.first_name or str(uid))[:60],
            chat_id    = cid,
            chat_title = (message.chat.title or str(cid))[:60],
            alasan     = f"Keroyokan ({len(matched)} pola) semua terlindungi: {_kk_gabung[:180]}",
            confidence = 0.0,
            content    = content,
        ))
        if _AI_CORE_AVAILABLE:
            asyncio.create_task(_ai_passive_background(content, False, 0.0))
        return   # Semua pola dilindungi whitelist → biarkan pesan

    # Minimal 1 pola tidak punya penawar → hapus
    await delete_queue.put((cid, [message.id]))
    asyncio.create_task(check_and_punish(client, message, "Nexus AI (multi-pola)", content[:100]))

    asyncio.create_task(_log_nexus_keroyok(
        client, message, matched, pola_tanpa_penawar, content
    ))

    _alasan_keroyok = " + ".join(
        (kk.split("]", 1)[-1].strip() if "]" in kk else kk)[:50]
        for kk, _ in matched[:3]
    )
    # Confidence rata-rata dari semua pola matched
    _conf_keroyok = sum(_extract_confidence(kk) for kk, _ in matched) / len(matched)
    # Keroyokan dengan apapun (termasuk CategoryDetector) = force_learn
    _any_category = any(_is_category_based(kk) for kk, _ in matched)

    asyncio.create_task(nexus_actlog_insert(
        aksi       = "KEROYOKAN",
        user_id    = uid,
        user_name  = (message.from_user.first_name or str(uid))[:60],
        chat_id    = cid,
        chat_title = (message.chat.title or str(cid))[:60],
        alasan     = f"{len(matched)} pola: {_alasan_keroyok}",
        confidence = _conf_keroyok,
        content    = content,
    ))
    # FIXED: Tulis juga ke group_action_log agar muncul di panel "Log Aktivitas"
    asyncio.create_task(insert_group_action_log(
        cid, "HAPUS",
        f"Nexus AI multi-pola ({len(matched)}): {_alasan_keroyok[:80]}",
        uid, (message.from_user.first_name or str(uid))[:50],
        content[:100],
    ))
    # [PASSIVE LEARNING] Keroyokan = spam sangat meyakinkan → auto-learn paksa
    if _AI_CORE_AVAILABLE:
        asyncio.create_task(
            _ai_passive_background(
                content, True,
                max(0.90, _conf_keroyok),
                force_learn=True,
            )
        )


# ── Tracking bot masuk/keluar grup ────────────────────────────────────────────

@Client.on_chat_member_updated(group=8)
async def nexus_tracking_grup(client: Client, update: ChatMemberUpdated):
    try:
        from pyrogram.enums import ChatType
        me = await client.get_me()
        if not update.new_chat_member or update.new_chat_member.user.id != me.id:
            return

        try:
            chat = await client.get_chat(update.chat.id)
            if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
                return
        except Exception:
            return

        chat_id    = update.chat.id
        new_status = update.new_chat_member.status

        if new_status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
            await nexus_remove_grup(chat_id)
        elif new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
            await nexus_track_grup(chat_id, update.chat.title or str(chat_id))
    except Exception as e:
        print(f"[nexus_tracking_grup] {e}")


@Client.on_chat_member_updated(group=9)
async def nexus_track_admin_demotion(client: Client, update: ChatMemberUpdated):
    """
    Deteksi admin yang di-demosi dan cabut sesi DM-nya.
    group=9 — jalan setelah nexus_tracking_grup (group=8).
    """
    try:
        if not update.old_chat_member or not update.new_chat_member:
            return

        old_status = update.old_chat_member.status
        new_status = update.new_chat_member.status

        was_admin = old_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
        now_admin = new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

        if was_admin and not now_admin:
            demoted_uid = update.new_chat_member.user.id
            chat_id     = update.chat.id
            _adm_sess.on_admin_demoted(demoted_uid, chat_id)
    except Exception as e:
        print(f"[nexus_track_admin_demotion] {e}")
