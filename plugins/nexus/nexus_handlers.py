"""
plugins/nexus/nexus_handlers.py
────────────────────────────────
Panel Nexus AI — /nexus di DM & Direct Injection Command.

STRUKTUR MENU:
  [Menu Utama Nexus]
  ┌─ ➕ AKTIFKAN DI GRUP
  ├─ 📚 BUKU MANUAL AI  │  🧪 LAB UJI SANDBOX
  ├─ 🔮 GLOBAL REGEX    │  👑 OWNER BOT
  └─ 🔙 MENU UTAMA BOT

  [Sub-menu OWNER BOT]
  ┌─ 📊 RECORD DATA     │  📂 GRUP TERDAFTAR
  ├─ ⚡ PAKSA REKALKULASI │ 🔄 REFRESH METRIK
  ├─ 🧠 LIHAT AI        │  📋 LOG AKTIVITAS
  ├─ 🔬 DEBUG AI (24j)  │  🗑️ RESET INTEGRASI
  ├─ 📱 GANTI USERBOT
  └─ 🔙 KEMBALI KE NEXUS

  [Sub-menu GLOBAL REGEX]
  ┌─ 🧬 VISUALISASI FILTER
  └─ ⚙️ OWNER REGEX
"""

import re
import asyncio
import unicodedata

import pytz
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, ForceReply,
)
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified

from database import (
    nexus_get_kalimat_count,
    nexus_get_regex_count,
    nexus_get_regex_page,
    nexus_get_kalimat_page,
    nexus_get_all_grup,
    nexus_delete_kalimat,
    nexus_delete_kalimat_by_id,
    nexus_delete_regex_by_pola,
    nexus_clear_kalimat,
    nexus_clear_regex,
    nexus_get_all_regex,
    nexus_whitelist_add,
    nexus_whitelist_count,
    nexus_whitelist_page,
    nexus_whitelist_delete_by_id,
    nexus_whitelist_clear,
    nexus_regex_delete_by_id,
    nexus_actlog_get_page,
    nexus_actlog_count,
    nexus_actlog_clear,
    regex_db,
)
from plugins.nexus.engine import (
    pipeline_pembersihan,
    generate_kandidat_mutasi_liar,
    generate_regex_otomatis_async,
)

from plugins.commands.log import log_spam_lokal, log_spam_global, log_sistem
from plugins.nexus.nexus_group import invalidate_nexus_wl_cache
try:
    from nexus.ai_core import nexus_ai_get_full_stats
    _AI_STATS_AVAILABLE = True
except Exception as _e:
    _AI_STATS_AVAILABLE = False
    async def nexus_ai_get_full_stats():
        return {}
    print(f"[nexus_handlers] ai_core import gagal (opsional): {_e}")

import os
OWNER_ID   = int(os.environ.get("OWNER_ID", 0))
TZ_JAKARTA = pytz.timezone("Asia/Jakarta")

_owner_regex_fsm: dict[int, int] = {}
_whitelist_fsm:   dict[int, int] = {}   # FSM untuk input whitelist regex


# ══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _welcome_text() -> str:
    total, antrean = await nexus_get_kalimat_count()
    total_regex    = await nexus_get_regex_count()
    owner_regex_ct = await regex_db.count_documents({})
    wl_ct          = await nexus_whitelist_count()
    return (
        "🤖 **NEXUS AI ENGINE**\n"
        "_Adaptive Regex Engine · Belajar dari Laporan Spam_\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Bukan blacklist statis. Nexus mengurai pola pesan secara kolektif,\n"
        "merakit pertahanan otomatis, dan mengantisipasi manipulasi font,\n"
        "karakter berulang, hingga varian leetspeak secara simultan.\n\n"
        "📊 **STATUS ENGINE — LIVE:**\n"
        f"├─ 📚 `Knowledge Base`      : **{total} kalimat spam**\n"
        f"├─ ⏳ `Antrean Belum Proses` : **{antrean} kalimat baru**\n"
        f"├─ 🔮 `Pola AI (Auto)`      : **{total_regex} interlock regex**\n"
        f"├─ ⚙️ `Pola Manual (Owner)` : **{owner_regex_ct} regex**\n"
        f"├─ 🛡️ `Whitelist Nexus`     : **{wl_ct} pengecualian**\n"
        "└─ 🕛 `Siklus Rekalkulasi`   : **Setiap 00:00 WIB**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "_Engine berjalan otomatis. Pilih panel di bawah:_"
    )


def _main_markup(username_bot: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "➕  Aktifkan di Grup",
            url=f"https://t.me/{username_bot}?startgroup=true&admin=delete_messages+ban_users"
        )],
        [
            InlineKeyboardButton("📚  Manual AI",        callback_data="nx_tutorial"),
            InlineKeyboardButton("🧪  Lab Sandbox",      callback_data="nx_sandbox_hub"),
        ],
        [
            InlineKeyboardButton("🔮  Global Regex",     callback_data="nx_global_regex_menu"),
            InlineKeyboardButton("👑  OWNER BOT",        callback_data="nx_owner_menu"),
        ],
        [InlineKeyboardButton("🔙  Menu Utama Bot",      callback_data="nx_back_main")],
    ])


def _back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 KEMBALI KE MAINFRAME", callback_data="nx_home")
    ]])


def _back_global_regex() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 KEMBALI KE GLOBAL REGEX", callback_data="nx_global_regex_menu")
    ]])


async def _safe_edit(msg, text: str, keyboard=None):
    try:
        await msg.edit(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except Exception as e:
        print(f"[nexus safe_edit] {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT & PURGE
# ══════════════════════════════════════════════════════════════════════════════


@Client.on_message(filters.command("delnexus") & filters.user(OWNER_ID))
async def nexus_del_handler(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply(
            "❌ **Sintaks Salah!**\n"
            "Gunakan: `/delnexus <kalimat asli atau pola interlock (?=.*)>`",
            parse_mode=ParseMode.MARKDOWN,
        )
    input_target = message.text.split(None, 1)[1].strip()

    if input_target.startswith("(?=.*"):
        deleted = await nexus_delete_regex_by_pola(input_target)
        if deleted:
            await message.reply("🗑️ **PURGE AI REGEX BERHASIL**\nPola interlock dieliminasi dari Core Nexus.", parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply("❌ Pola interlock tidak ditemukan di database Nexus.")
    else:
        teks_clean = pipeline_pembersihan(input_target)
        deleted    = await nexus_delete_kalimat(input_target)
        if not deleted and teks_clean != input_target:
            deleted = await nexus_delete_kalimat(teks_clean)
        if deleted:
            await generate_regex_otomatis_async()
            await message.reply(
                f"🗑️ **PURGE & RESYNC BERHASIL**\n📝 `{input_target}` dihancurkan dan regex di-recalculate.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.reply("❌ Kalimat tidak ditemukan di Nexus database.")


@Client.on_message(filters.command("delkalimat") & filters.user(OWNER_ID))
async def nexus_delkalimat_handler(client: Client, message: Message):
    """
    /delkalimat <teks>  — hapus satu kalimat dari Record Data berdasarkan teks asli.
    Alternatif command dari tombol 🗑 di panel Record Data.
    """
    if len(message.command) < 2:
        return await message.reply(
            "❌ **Sintaks Salah!**\n\n"
            "Gunakan: `/delkalimat <teks kalimat spam>`\n\n"
            "💡 _Atau buka panel Record Data → tekan tombol 🗑 di samping kalimat yang ingin dihapus._",
            parse_mode=ParseMode.MARKDOWN,
        )
    teks_target = message.text.split(None, 1)[1].strip()
    teks_clean  = pipeline_pembersihan(teks_target)

    deleted = await nexus_delete_kalimat(teks_target)
    if not deleted and teks_clean and teks_clean != teks_target:
        deleted = await nexus_delete_kalimat(teks_clean)

    if deleted:
        await message.reply(
            f"🗑️ **KALIMAT BERHASIL DIHAPUS**\n\n"
            f"📝 `{teks_target[:200]}`\n\n"
            f"_Kalimat telah dieliminasi dari Record Data Nexus AI._",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.reply(
            f"❌ **Kalimat Tidak Ditemukan**\n\n"
            f"`{teks_target[:200]}`\n\n"
            f"_Pastikan teks sama persis. Cek daftar via panel: Menu Nexus AI → Owner Bot → Record Data._",
            parse_mode=ParseMode.MARKDOWN,
        )


# ══════════════════════════════════════════════════════════════════════════════
# LAB SANDBOX PROCESSOR (RESTORED LOGIC DUPLIKAT/MULTIPLE TRIGGER & ASLI LOG)
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.reply, group=10)
async def nexus_sandbox_processor(client: Client, message: Message):
    if not (
        message.reply_to_message
        and message.reply_to_message.text
        and "🧪 [NEXUS SANDBOX SIMULATION MODE]" in message.reply_to_message.text
    ):
        return
    if not message.text:
        return

    teks_clean = pipeline_pembersihan(message.text)

    triggers = []

    # 1. Validasi Pertama: AI GLOBAL REGEX (Full Interlock AI)
    docs = await nexus_get_all_regex()
    for d in docs:
        pola_target = d.get("pola")
        if not pola_target:
            continue
        try:
            if re.search(pola_target, teks_clean, re.IGNORECASE):
                triggers.append({
                    "tipe": "GLOBAL_AI",
                    "pola": pola_target,
                    "indikator": d.get("kata_kunci", "[AI_PATTERN]")
                })
                # Tidak di-break agar bisa mendeteksi duplikat pelanggaran (AI + Owner)
        except re.error:
            pass

    # 2. Validasi Kedua: OWNER GLOBAL REGEX (Full Interlock Manual)
    async for doc in regex_db.find({}):
        pola_target = doc.get("pola") or doc.get("pattern")
        if not pola_target:
            continue
        try:
            if re.search(pola_target, teks_clean, re.IGNORECASE):
                triggers.append({
                    "tipe": "OWNER_GLOBAL",
                    "pola": pola_target,
                    "indikator": f"[OWNER] {doc.get('raw', '')}"
                })
        except re.error:
            pass

    # ── EKSEKUSI PENANGANAN PESAN & LOG (LOGIKA ASLI) ──
    if triggers:
        for trig in triggers:
            if trig["tipe"] == "GLOBAL_AI":
                asyncio.create_task(log_spam_global(client, message, trig["pola"], f"NEXUS_AI: {trig['indikator']}"))
            elif trig["tipe"] == "OWNER_GLOBAL":
                asyncio.create_task(log_spam_lokal(client, message, trig["pola"], f"NEXUS_OWNER: {trig['indikator']}"))

        from pyrogram.enums import ChatType
        if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            try:
                await message.delete()
            except Exception:
                pass

    hasil = (
        "🧪 **HASIL DIAGNOSA SENSOR FILTER (ACUAN FULL INTERLOCK)**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Input Mentah:** `{message.text}`\n"
        f"🧹 **Hasil Destilasi Core:** `{teks_clean}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    if triggers:
        hasil += f"🚨 **STATUS: SENSOR TERPICU ({len(triggers)} DETEKSI)!**\n\n"
        for idx, t in enumerate(triggers, 1):
            hasil += (
                f"**[{idx}] Deteksi: {t['tipe']}**\n"
                f"🔑 **Matriks ID:** `{t['indikator']}`\n"
                f"💥 **Interlock:** `{t['pola'][:50]}...`\n\n"
            )
        hasil += "📢 _Pesan pemicu ditangani & duplikat log diteruskan sesuai porsi masing-masing._"
    else:
        hasil += "✅ **STATUS: AMAN (LOLOS ACUAN FULL INTERLOCK)**"

    await message.reply(
        hasil,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧪 UJI KEMBALI", callback_data="nx_sandbox_hub")],
            [InlineKeyboardButton("🔙 MENU NEXUS",  callback_data="nx_home")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ══════════════════════════════════════════════════════════════════════════════
# BUILDER CORE INTERLOCK SKEMA
# ══════════════════════════════════════════════════════════════════════════════

def _build_owner_interlock(kata_list: list[str]) -> tuple[str, list[tuple[str, list[str]]], str]:
    """
    Bangun interlock regex dari daftar kata.
    Kapital dari owner DIPERTAHANKAN — diteruskan ke generate_kandidat_mutasi_liar
    sebagai penanda posisi wajib. pipeline_pembersihan hanya dipakai untuk
    validasi (cek kosong), BUKAN sebagai sumber kata ke generator.
    """
    import re as _re
    mutasi_display = []
    lookaheads     = []

    for kata in kata_list:
        # Validasi: pastikan kata tidak kosong setelah dibersihkan
        kata_clean = pipeline_pembersihan(kata)
        if not kata_clean:
            continue

        # Bersihkan simbol tapi JAGA KAPITAL — ini yang dikirim ke generator
        kata_bersih = _re.sub(r"\(?[×xX]\d+\)?", "", kata)
        kata_bersih = _re.sub(r"[^\w]", "", kata_bersih).strip()
        kata_token  = kata_bersih.split()[0] if kata_bersih else ""
        if not kata_token:
            continue

        mutasi = generate_kandidat_mutasi_liar(kata_token)

        if mutasi:
            lookaheads.append(f"(?=.*({'|'.join(mutasi)}))")
            # Simpan token lowercase untuk display/key konsistensi
            mutasi_display.append((kata_token.lower(), mutasi))

    pola = "".join(lookaheads) if lookaheads else ""
    return pola, mutasi_display, ""


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLER — DIRECT ADDREGEX
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command("addregex") & filters.user(OWNER_ID))
async def nexus_direct_add_regex(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply(
            "❌ **Sintaks Salah!**\n"
            "Gunakan: `/addregex kata1|kata2|kata3`\n\n"
            "💡 _Pola lama dimusnahkan. Otomatis menggunakan metode Full Interlock Owner Nexus._",
            parse_mode=ParseMode.MARKDOWN,
        )

    raw_input = message.text.split(None, 1)[1].strip()
    raw_input = unicodedata.normalize("NFKC", raw_input)

    kata_list = [k.strip() for k in raw_input.split("|") if k.strip()]
    if not kata_list:
        return await message.reply("❌ **Gagal:** Input kata tidak valid atau kosong.")

    pola, mutasi_display, _ = _build_owner_interlock(kata_list)

    if not pola:
        return await message.reply("❌ **Gagal Generate:** Kata bersih kosong setelah melewati pipeline destilasi.")

    try:
        re.compile(pola)
    except re.error as e:
        return await message.reply(f"❌ **Regex Error:** Kompilasi interlock gagal.\n`{e}`")

    raw_joined = " | ".join([k for k, _ in mutasi_display])

    await regex_db.update_one(
        {"pola": pola},
        {"$set": {
            "pola":      pola,
            "pattern":   pola,
            "raw":       raw_joined,
            "kata_list": [k for k, _ in mutasi_display],
            "mutasi":    {k: m for k, m in mutasi_display},
        }},
        upsert=True,
    )

    hasil_respon = (
        f"✅ **DIRECT INJECTION SUCCESS!**\n"
        f"⚙️ **Metode:** Owner Interlock System (Command Base)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 **Koleksi Asli:** `{raw_joined}`\n\n"
        f"🔍 **Probabilitas Lolos Mutasi (>=50%):**\n"
    )
    for kata, mutasi in mutasi_display:
        hasil_respon += f"• `{kata}` ➔ `({'|'.join(mutasi)})`\n"

    hasil_respon += f"\n💥 **Full Interlock (Acuan Utama Locked):**\n`{pola}`"

    await message.reply(hasil_respon, parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLER — DIRECT WLREGEX (WHITELIST)
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command("wlregex") & filters.user(OWNER_ID))
async def nexus_direct_add_whitelist(client: Client, message: Message):
    """/wlregex kataA | kataB | kataC — generate whitelist regex seperti /addregex."""
    if len(message.command) < 2:
        return await message.reply(
            "❌ **Sintaks Salah!**\n"
            "Gunakan: `/wlregex kata1|kata2|kata3`\n\n"
            "💡 _Regex whitelist melindungi pesan dari penghapusan meski cocok regex spam._",
            parse_mode=ParseMode.MARKDOWN,
        )

    raw_input = message.text.split(None, 1)[1].strip()
    raw_input = unicodedata.normalize("NFKC", raw_input)
    kata_list = [k.strip() for k in raw_input.split("|") if k.strip()]
    if not kata_list:
        return await message.reply("❌ **Gagal:** Input kata tidak valid atau kosong.")

    pola, mutasi_display, _ = _build_owner_interlock(kata_list)
    if not pola:
        return await message.reply("❌ **Gagal Generate:** Kata kosong setelah pipeline destilasi.")

    try:
        re.compile(pola)
    except re.error as e:
        return await message.reply(f"❌ **Regex Error:** `{e}`")

    raw_joined = " | ".join([k for k, _ in mutasi_display])
    await nexus_whitelist_add(
        pola      = pola,
        raw       = raw_joined,
        kata_list = [k for k, _ in mutasi_display],
        mutasi    = {k: m for k, m in mutasi_display},
    )
    invalidate_nexus_wl_cache()

    hasil = (
        f"🛡️ **WHITELIST INJECTED!**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 **Koleksi Asli:** `{raw_joined}`\n\n"
        f"🔍 **Probabilitas Mutasi (>=50%):**\n"
    )
    for kata, mutasi in mutasi_display:
        hasil += f"• `{kata}` ➔ `({'|'.join(mutasi)})`\n"
    hasil += f"\n🛡️ **Whitelist Interlock:**\n`{pola}`"
    await message.reply(hasil, parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
# FSM — ENGINE INTERLOCK PANEL BUILDER
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.text & ~filters.command([""]) & filters.user(OWNER_ID), group=9)
async def nexus_owner_regex_fsm(client: Client, message: Message):
    # Cek whitelist FSM dulu
    if message.from_user.id in _whitelist_fsm:
        if message.text and message.text.startswith("/"):
            _whitelist_fsm.pop(message.from_user.id, None)  # Clear FSM agar tidak stuck
            return
        msg_id    = _whitelist_fsm.pop(message.from_user.id)
        raw_input = unicodedata.normalize("NFKC", message.text.strip())
        kata_list = [k.strip() for k in raw_input.split("|") if k.strip()]

        if not kata_list:
            try:
                await client.edit_message_text(
                    message.chat.id, msg_id,
                    "❌ **INPUT KOSONG**\n\nKirim minimal satu kata.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Whitelist", callback_data="nx_whitelist_page_1")]]),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            try: await message.delete()
            except Exception: pass
            return

        pola, mutasi_display, _ = _build_owner_interlock(kata_list)
        if not pola:
            try:
                await client.edit_message_text(
                    message.chat.id, msg_id,
                    "❌ **GAGAL GENERATE**\n\nSemua kata kosong setelah normalisasi.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Whitelist", callback_data="nx_whitelist_page_1")]]),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            try: await message.delete()
            except Exception: pass
            return

        try:
            re.compile(pola)
        except re.error as e:
            try:
                await client.edit_message_text(
                    message.chat.id, msg_id,
                    f"❌ **REGEX ERROR**\n\n`{e}`",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Whitelist", callback_data="nx_whitelist_page_1")]]),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            try: await message.delete()
            except Exception: pass
            return

        raw_joined = " | ".join([k for k, _ in mutasi_display])
        await nexus_whitelist_add(
            pola      = pola,
            raw       = raw_joined,
            kata_list = [k for k, _ in mutasi_display],
            mutasi    = {k: m for k, m in mutasi_display},
        )
        invalidate_nexus_wl_cache()
        header = f"🛡️ **`{raw_joined}`** berhasil dikunci ke Whitelist!\n\n"
        await _render_whitelist_page(client, message.chat.id, msg_id, 1, header=header)
        try: await message.delete()
        except Exception: pass
        return

    if message.from_user.id not in _owner_regex_fsm:
        return
    if message.text and message.text.startswith("/"):
        _owner_regex_fsm.pop(message.from_user.id, None)  # Clear FSM agar tidak stuck
        return

    msg_id   = _owner_regex_fsm.pop(message.from_user.id)
    raw_input = unicodedata.normalize("NFKC", message.text.strip())

    kata_list = [k.strip() for k in raw_input.split("|") if k.strip()]
    if not kata_list:
        try:
            await client.edit_message_text(
                message.chat.id, msg_id,
                "❌ **INPUT KOSONG**\n\nKirim minimal satu kata.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 OWNER REGEX", callback_data="nx_owner_regex_page_1")]]),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass
        return

    pola, mutasi_display, _ = _build_owner_interlock(kata_list)

    if not pola:
        try:
            await client.edit_message_text(
                message.chat.id, msg_id,
                "❌ **GAGAL GENERATE**\n\nSemua kata kosong setelah normalisasi.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 OWNER REGEX", callback_data="nx_owner_regex_page_1")]]),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass
        return

    try:
        re.compile(pola)
    except re.error as e:
        try:
            await client.edit_message_text(
                message.chat.id, msg_id,
                f"❌ **REGEX ERROR**\n\nError: `{e}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 OWNER REGEX", callback_data="nx_owner_regex_page_1")]]),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass
        return

    raw_joined = " | ".join([k for k, _ in mutasi_display])

    await regex_db.update_one(
        {"pola": pola},
        {"$set": {
            "pola":      pola,
            "pattern":   pola,
            "raw":       raw_joined,
            "kata_list": [k for k, _ in mutasi_display],
            "mutasi":    {k: m for k, m in mutasi_display},
        }},
        upsert=True,
    )

    header = f"✅ **`{raw_joined}`** Full Interlock berhasil dikunci ke Core Database!\n\n"
    await _render_owner_regex_page(client, message.chat.id, msg_id, 1, header=header)
    try:
        await message.delete()
    except Exception:
        pass


async def _render_owner_regex_page(client, chat_id: int, msg_id: int, page: int, header: str = ""):
    limit  = 5
    offset = (page - 1) * limit
    total  = await regex_db.count_documents({})
    docs   = [doc async for doc in regex_db.find({}).sort("_id", -1).skip(offset).limit(limit)]
    total_pages = max(1, (total + limit - 1) // limit)

    if docs:
        body = ""
        del_buttons = []
        for local_i, doc in enumerate(docs):
            global_idx = offset + local_i
            raw        = doc.get("raw", "—")
            pola_full  = doc.get("pola", doc.get("pattern", ""))
            kata_list  = doc.get("kata_list", [])
            mutasi_map = doc.get("mutasi", {})

            if not kata_list and raw != "—":
                kata_list = [k.strip() for k in raw.split("|") if k.strip()]

            jalur_tag = f"[OWNER-{global_idx + 1}]"
            body += f"🔑 **ID Jalur:** `{jalur_tag}`\n"
            body += "📝 **Koleksi Asli:** " + ", ".join(f"`{k}`" for k in kata_list) + "\n"

            if mutasi_map:
                body += "🔍 **Probabilitas Lolos Mutasi (>=50%):**\n"
                for kata in kata_list:
                    mutasi = mutasi_map.get(kata, generate_kandidat_mutasi_liar(kata))
                    body  += f"• `{kata}` ➔ `({'|'.join(mutasi)})`\n"
            elif kata_list:
                body += "🔍 **Probabilitas Lolos Mutasi (>=50%):**\n"
                for kata in kata_list:
                    import re as _re2
                    kata_b = _re2.sub(r"[^\w]", "", kata).strip()
                    if kata_b:
                        mutasi = generate_kandidat_mutasi_liar(kata_b)
                        body  += f"• `{kata}` ➔ `({'|'.join(mutasi)})`\n"

            if pola_full:
                body += f"💥 **Full Interlock (Acuan Utama):**\n`{pola_full}`\n"
            body += "──────────────────────────\n"

            doc_id = str(doc["_id"])
            del_buttons.append([InlineKeyboardButton(f"🗑 Hapus: {raw[:40]}", callback_data=f"nx_owner_rgx_del_{doc_id}")])

        content = f"⚡ Total Owner Regex: **{total} pola**\n\n{body}"
    else:
        content    = "📭 **Belum ada Owner Regex.**"
        del_buttons = []

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⏪ SEBELUMNYA", callback_data=f"nx_owner_regex_page_{page-1}"))
    if (offset + limit) < total:
        nav.append(InlineKeyboardButton("SELANJUTNYA ⏩", callback_data=f"nx_owner_regex_page_{page+1}"))

    rows = del_buttons.copy()
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("➕ Tambah Regex Baru", callback_data=f"nx_owner_rgx_add_{page}")])
    rows.append([InlineKeyboardButton("🔙 KEMBALI", callback_data="nx_global_regex_menu")])

    text = (f"⚙️ **OWNER REGEX — HAL {page}/{total_pages}**\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n{header}{content}")
    try:
        await client.edit_message_text(chat_id, msg_id, text[:4000], reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print(f"[_render_owner_regex_page] {e}")


async def _render_whitelist_page(client, chat_id: int, msg_id: int, page: int, header: str = ""):
    limit  = 5
    offset = (page - 1) * limit
    docs, total = await nexus_whitelist_page(page, limit)
    total_pages = max(1, (total + limit - 1) // limit)

    if docs:
        body        = ""
        del_buttons = []
        for local_i, doc in enumerate(docs):
            global_idx = offset + local_i
            raw        = doc.get("raw", "—")
            pola_full  = doc.get("pola", "")
            kata_list  = doc.get("kata_list", [])
            mutasi_map = doc.get("mutasi", {})

            if not kata_list and raw != "—":
                kata_list = [k.strip() for k in raw.split("|") if k.strip()]

            body += f"🛡️ **[WL-{global_idx + 1}]**\n"
            body += "📝 **Kata Aman:** " + ", ".join(f"`{k}`" for k in kata_list) + "\n"
            if mutasi_map:
                body += "🔍 **Pola Mutasi (>=50%):**\n"
                for kata in kata_list:
                    mutasi = mutasi_map.get(kata, generate_kandidat_mutasi_liar(kata))
                    body  += f"• `{kata}` ➔ `({'|'.join(mutasi)})`\n"
            elif kata_list:
                body += "🔍 **Pola Mutasi (>=50%):**\n"
                for kata in kata_list:
                    import re as _re3
                    kata_b = _re3.sub(r"[^\w]", "", kata).strip()
                    if kata_b:
                        mutasi = generate_kandidat_mutasi_liar(kata_b)
                        body  += f"• `{kata}` ➔ `({'|'.join(mutasi)})`\n"
            if pola_full:
                body += f"🛡️ **Whitelist Interlock:**\n`{pola_full}`\n"
            body += "──────────────────────────\n"
            del_buttons.append([
                InlineKeyboardButton(f"🗑 Hapus: {raw[:40]}", callback_data=f"nx_wl_del_{str(doc['_id'])}")
            ])

        content = f"🛡️ Total Whitelist: **{total} pola**\n\n{body}"
    else:
        content     = "📭 **Belum ada Whitelist Regex.**\n\n_Gunakan `/wlregex kata1|kata2` atau tombol ➕ di bawah._"
        del_buttons = []

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⏪ Sebelumnya", callback_data=f"nx_whitelist_page_{page-1}"))
    if (offset + limit) < total:
        nav.append(InlineKeyboardButton("Selanjutnya ⏩", callback_data=f"nx_whitelist_page_{page+1}"))

    rows = del_buttons.copy()
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("➕ Tambah Whitelist Baru", callback_data=f"nx_wl_add_{page}")])
    rows.append([InlineKeyboardButton("🗑️ Hapus Semua Whitelist", callback_data="nx_wl_clear_confirm")])
    rows.append([InlineKeyboardButton("🔙 Kembali ke Nexus",      callback_data="nx_home")])

    text = (
        f"🛡️ **WHITELIST NEXUS — HAL {page}/{total_pages}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"_Pesan yang cocok pola whitelist tidak akan dihapus meski melanggar regex spam._\n\n"
        f"{header}{content}"
    )
    try:
        await client.edit_message_text(
            chat_id, msg_id, text[:4000],
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        print(f"[_render_whitelist_page] {e}")


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK ROUTER
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^nx_"))
async def nexus_callback_router(client: Client, cq: CallbackQuery):
    data    = cq.data
    user_id = cq.from_user.id

    try:
        await cq.answer()
    except Exception:
        pass

    if data == "nx_back_main":
        try:
            await cq.answer()
        except Exception:
            pass
        from plugins.ui.pages import page_start
        from pyrogram.enums import ParseMode as _PM
        text, keyboard = await page_start(client)
        try:
            await cq.message.edit(text, reply_markup=keyboard, parse_mode=_PM.HTML, disable_web_page_preview=True)
        except Exception:
            pass
        return

    elif data in ("nx_home", "nx_refresh"):
        try:
            await cq.answer()
        except Exception:
            pass
        me   = await client.get_me()
        text = await _welcome_text()
        await _safe_edit(cq.message, text, _main_markup(me.username))

    elif data == "nx_tutorial":
        await _safe_edit(
            cq.message,
            "📚 **NEXUS AI — CARA KERJA MESIN**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "**🧩 TAHAP 1 — INPUT & PREPROCESSING**\n"
            "Admin lapor spam via `/spam` (reply ke pesan).\n"
            "Bot membersihkan teks: normalisasi Unicode NFKD, strip font palsu,\n"
            "hapus karakter berulang, normalisasi leet (0→o, 3→e, @→a, dll),\n"
            "dedup kata — menghasilkan teks bersih di Knowledge Base.\n\n"
            "**📊 TAHAP 2 — ANALISIS POLA KOLEKTIF**\n"
            "Setiap malam (00:00 WIB), engine membandingkan semua kalimat spam\n"
            "dan menghitung pasangan kata yang sering muncul bersamaan.\n"
            "Pasangan yang muncul ≥2× di korpus berbeda dianggap pola spam.\n\n"
            "**🧬 TAHAP 3 — GENERASI MUTASI**\n"
            "Tiap kata kunci diledakkan menjadi ratusan varian mutasi:\n"
            "◈ Substitusi leet (togel → t0gel, t0g3l, t0gg3l, dll)\n"
            "◈ Penyisipan karakter berulang (to.g.el, to_g_e_l, dll)\n"
            "◈ Kombinasi huruf besar-kecil & karakter separator\n\n"
            "**🔮 TAHAP 4 — INTERLOCK REGEX**\n"
            "Semua mutasi dikompilasi jadi satu regex AND lookahead:\n"
            "`(?=.*(t0gel|togel|...))(?=.*(judi|jvdi|...))`\n"
            "Artinya: hanya cocok jika **semua kata** hadir bersamaan.\n"
            "False positive sangat rendah — spammer tidak bisa lolos dengan\n"
            "mengganti 1–2 karakter saja.\n\n"
            "**🧠 TAHAP 5 — AI CORE (AUGMENTASI)**\n"
            "Selain regex, Nexus AI Core menjalankan analisis TF-IDF +\n"
            "Naive Bayes untuk mengekstrak kata-kata yang secara statistik\n"
            "paling membedakan spam dari percakapan normal.\n"
            "Pola hasil AI Core ditambahkan ke database sebagai lapisan kedua.\n\n"
            "**⚙️ TAHAP 6 — OWNER MANUAL REGEX**\n"
            "Owner bisa menambah regex manual via `/addregex` atau panel.\n"
            "Input kata diproses pipeline yang sama (mutasi + interlock)\n"
            "dan disimpan terpisah sebagai lapisan prioritas tinggi.\n\n"
            "**🛡️ WHITELIST NEXUS**\n"
            "Pola yang diketahui false positive bisa diwhitelist:\n"
            "bot tidak akan menghapus pesan yang cocok whitelist,\n"
            "meski cocok dengan pola spam.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "_Gunakan 🧪 Lab Sandbox untuk menguji kalimat secara live._",
            _back_main(),
        )

    elif data == "nx_sandbox_hub":
        await _safe_edit(
            cq.message,
            "🧪 **NEXUS SANDBOX LAB**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Uji coba kalimat terhadap **semua lapisan filter aktif**:\n"
            "◈ Pola AI otomatis (Interlock Regex dari Knowledge Base)\n"
            "◈ Owner Manual Regex (pola yang ditambah secara manual)\n"
            "◈ Whitelist Nexus (pola yang dikecualikan dari deteksi)\n\n"
            "Hasilnya menampilkan:\n"
            "✓ Apakah kalimat terdeteksi sebagai spam\n"
            "✓ Pola mana yang mencocokkan\n"
            "✓ Apakah tertahan oleh whitelist\n\n"
            "_Tekan **Mulai Simulasi**, lalu balas (reply) prompt bot dengan\n"
            "kalimat yang ingin kamu uji._",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀  Mulai Simulasi",  callback_data="nx_pancing_sandbox")],
                [InlineKeyboardButton("🔙  Kembali",         callback_data="nx_home")],
            ]),
        )

    elif data == "nx_pancing_sandbox":
        await client.send_message(
            chat_id=cq.message.chat.id,
            text="🧪 [NEXUS SANDBOX SIMULATION MODE]\nBalas (reply) pesan ini dengan kalimat yang ingin diuji:",
            reply_markup=ForceReply(selective=True),
        )
        await cq.message.delete()

    elif data == "nx_owner_menu":
        if user_id != OWNER_ID:
            try:
                await cq.answer(
                    "🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.",
                    show_alert=True
                )
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        await _safe_edit(
            cq.message,
            "👑 **PANEL KHUSUS OWNER BOT**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Akses kontrol penuh ke dalam core system Nexus AI.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📊  Record Data",      callback_data="nx_records_page_1"),
                    InlineKeyboardButton("📂  Grup Terdaftar",   callback_data="nx_list_grup"),
                ],
                [
                    InlineKeyboardButton("⚡  Paksa Kalkulasi",  callback_data="nx_force_calc"),
                    InlineKeyboardButton("🔄  Refresh Metrik",   callback_data="nx_refresh"),
                ],
                [
                    InlineKeyboardButton("🧠  Lihat AI",         callback_data="nx_lihat_ai"),
                    InlineKeyboardButton("📋  Log Aktivitas",    callback_data="nx_actlog_page_1"),
                ],
                [
                    InlineKeyboardButton("🔬  Debug AI (24j)",  callback_data="nx_ai_debug_page_1"),
                    InlineKeyboardButton("🗑️  Reset Integrasi", callback_data="nx_menu_reset"),
                ],
                [InlineKeyboardButton("📱  Ganti Userbot",       callback_data="nx_setuserbot")],
                [InlineKeyboardButton("🔙  Kembali ke Nexus",   callback_data="nx_home")],
            ])
        )

    elif data == "nx_setuserbot":
        if user_id != OWNER_ID:
            try:
                await cq.answer(
                    "🔒 Fitur ini hanya untuk Owner bot.",
                    show_alert=True
                )
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        # Import FSM setuserbot dari handlers_secos dan mulai dengan chat_id=0
        # chat_id=0 → konteks owner panel (bukan per-grup)
        from plugins.ui.handlers_secos import (
            _pending_setuserbot, _cancel_setuserbot_task,
            _setuserbot_timeout, WAIT_TIMEOUT_UB,
        )
        from plugins.ui.handlers_dm import safe_edit
        _cancel_setuserbot_task(user_id)
        _pending_setuserbot[user_id] = {
            "chat_id": 0,       # 0 = konteks owner, bukan per-grup
            "msg_id":  cq.message.id,
            "_task":   None,
        }
        await safe_edit(
            cq.message,
            "📱 <b>GANTI USERBOT</b>\n"
            "<i>Diakses dari Owner Bot Panel</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Kirim <b>nomor HP</b> akun userbot baru ke sini.\n\n"
            "<b>📋 LANGKAH-LANGKAH:</b>\n\n"
            "<b>1️⃣ Siapkan akun Telegram</b>\n"
            "   Gunakan akun biasa (bukan bot) yang akan dijadikan userbot.\n\n"
            "<b>2️⃣ Kirim nomor HP ke sini</b>\n"
            "   Format internasional: <code>+628123456789</code>\n\n"
            "<b>3️⃣ Masukkan OTP</b>\n"
            "   Telegram akan mengirim kode OTP ke nomor tersebut.\n"
            "   Kirim kode via DM bot dengan format: <code>/otp &lt;kode&gt;</code>\n\n"
            "<b>4️⃣ Adminkan userbot ke grup</b>\n"
            "   Setelah login berhasil, jadikan userbot admin dengan izin\n"
            "   <code>Kelola Obrolan Video</code> di setiap grup Security OS.\n\n"
            "⚠️ <b>Userbot lama akan diputus dan session-nya dihapus.</b>\n\n"
            f"<i>⏳ Batas waktu input: {WAIT_TIMEOUT_UB // 60} menit.</i>\n"
            "<i>Kirim /batal untuk membatalkan.</i>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫  Batalkan", callback_data="nx_owner_menu")]
            ])
        )
        task = asyncio.create_task(
            _setuserbot_timeout(user_id, 0, cq.message, cq._client)
        )
        if user_id in _pending_setuserbot:
            _pending_setuserbot[user_id]["_task"] = task

    elif data == "nx_list_grup":
        if user_id != OWNER_ID:
            try:
                await cq.answer(
                    "🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.",
                    show_alert=True
                )
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        grups = await nexus_get_all_grup()
        text  = "📂 **GRUP YANG DIAWASI NEXUS AI:**\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        if grups:
            for idx, g in enumerate(grups, 1):
                text += f"**{idx}.** 👥 {g['judul']}\n┗─ ID: `{g['chat_id']}`\n"
        else:
            text += "_Belum ada grup yang terdaftar._"
        await _safe_edit(cq.message, text, _back_main())

    elif data == "nx_lihat_ai":
        if user_id != OWNER_ID:
            try:
                await cq.answer(
                    "🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.",
                    show_alert=True
                )
            except Exception:
                pass
            return
        try:
            await cq.answer("⏳ Mengambil data AI...")
        except Exception:
            pass
        try:
            s = await nexus_ai_get_full_stats()
        except Exception as e:
            await _safe_edit(
                cq.message,
                f"❌ **Gagal ambil data AI**\n`{e}`",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 KEMBALI", callback_data="nx_owner_menu")]]),
            )
            return

        if "error" in s:
            await _safe_edit(
                cq.message,
                f"⚠️ **NEXUS AI CORE**\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n_{s['error']}_",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 KEMBALI", callback_data="nx_owner_menu")]]),
            )
            return

        # Format statistik model
        total_check  = s.get("total_checked", 0)
        total_spam   = s.get("total_spam", 0)
        total_ham    = s.get("total_ham", 0)
        learn_count  = s.get("learn_count", 0)
        vocab_size   = s.get("vocab_size", 0)
        spam_samples = s.get("spam_samples", 0)
        ham_samples  = s.get("ham_samples", 0)
        last_upd     = s.get("last_updated", "-") or "-"
        if last_upd and last_upd != "-":
            last_upd = last_upd[:16].replace("T", " ")  # format rapi

        thr     = s.get("threshold_detail", {})
        thr_val = thr.get("threshold", s.get("threshold", "?"))
        thr_sp  = thr.get("spam_mean", "?")
        thr_hm  = thr.get("ham_mean", "?")

        version = s.get("version", "?")
        loaded  = "✅ Aktif" if s.get("loaded") else "❌ Belum"

        akurasi = "-"
        if total_check > 0:
            akurasi = f"{(total_spam / total_check * 100):.1f}% terdeteksi spam"

        text = (
            "🧠 **NEXUS AI CORE — STATUS & AKTIVITAS**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔖 **Versi Model:** `v{version}`\n"
            f"⚡ **Status:** {loaded}\n"
            f"🕐 **Terakhir Diperbarui:** `{last_upd}`\n\n"
            "📊 **STATISTIK DETEKSI:**\n"
            f"├─ Total Diperiksa : `{total_check:,}`\n"
            f"├─ Terdeteksi Spam : `{total_spam:,}`\n"
            f"├─ Terdeteksi Aman : `{total_ham:,}`\n"
            f"└─ Rasio           : `{akurasi}`\n\n"
            "🎓 **ONLINE LEARNING:**\n"
            f"├─ Total Laporan   : `{learn_count:,}` kali belajar\n"
            f"├─ Sampel Spam     : `{spam_samples:,}`\n"
            f"└─ Sampel Ham      : `{ham_samples:,}`\n\n"
            "📚 **MODEL NAIVE BAYES:**\n"
            f"└─ Vocab Size      : `{vocab_size:,}` token\n\n"
            "🎯 **ADAPTIVE THRESHOLD:**\n"
            f"├─ Threshold Aktif : `{thr_val}`\n"
            f"├─ Rata-rata Spam  : `{thr_sp}`\n"
            f"└─ Rata-rata Ham   : `{thr_hm}`\n"
        )

        # Log aktivitas terbaru
        logs = s.get("recent_log", [])
        if logs:
            text += "\n📋 **LOG AKTIVITAS TERBARU:**\n"
            text += "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            for line in logs[-10:]:  # tampilkan 10 terakhir
                # Singkat baris log agar tidak terlalu panjang
                short = line[11:] if len(line) > 11 else line  # potong timestamp awal
                text += f"`{short[:90]}`\n"
        else:
            text += "\n_📋 Log belum ada. Bot baru dijalankan atau log kosong._\n"

        # Potong agar tidak melebihi limit Telegram 4096
        if len(text) > 3900:
            text = text[:3900] + "\n…_(dipotong)_"

        await _safe_edit(
            cq.message,
            text,
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="nx_lihat_ai")],
                [InlineKeyboardButton("🔙 KEMBALI", callback_data="nx_owner_menu")],
            ]),
        )

    elif data == "nx_global_regex_menu":
        try:
            await cq.answer()
        except Exception:
            pass
        ai_ct    = await nexus_get_regex_count()
        owner_ct = await regex_db.count_documents({})
        wl_ct    = await nexus_whitelist_count()
        await _safe_edit(
            cq.message,
            "🔮 **GLOBAL REGEX — SUB MENU**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🧬 **AI Interlock Pattern:** `{ai_ct} pola`\n"
            f"⚙️ **Owner Manual Regex:** `{owner_ct} pola`\n"
            f"🛡️ **Whitelist Nexus:** `{wl_ct} pola`\n\n"
            "Pilih panel yang ingin dibuka:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🧬 VISUALISASI FILTER", callback_data="nx_regex_page_1")],
                [InlineKeyboardButton("⚙️ OWNER REGEX",        callback_data="nx_owner_regex_page_1")],
                [InlineKeyboardButton("🛡️ WHITELIST NEXUS",   callback_data="nx_whitelist_page_1")],
                [InlineKeyboardButton("🔙 KEMBALI KE MAINFRAME", callback_data="nx_home")],
            ]),
        )

    elif data.startswith("nx_regex_page_"):
        try:
            await cq.answer()
        except Exception:
            pass
        cp    = int(data.split("_")[-1])
        rows, total = await nexus_get_regex_page(cp, 5)
        limit = 5
        off   = (cp - 1) * limit

        if not rows:
            await _safe_edit(cq.message, "🧬 **VISUALISASI FILTER**\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n_Mainframe belum memiliki koleksi pola interlock._", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 KEMBALI", callback_data="nx_global_regex_menu")]]))
            return

        text = f"🧬 **MAPS INTELLIGENCE PATTERN SENSOR (HAL {cp}/{(total+limit-1)//limit})**\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for row in rows:
            pola_full = row["pola"]
            indikator = row["kata_kunci"]
            kata_raw  = indikator.split("]", 1)[-1] if "]" in indikator else indikator
            kata_list = [k.strip() for k in kata_raw.split("+") if k.strip()]
            jalur_tag = indikator.split("]")[0] + "]" if "]" in indikator else "[?]"

            text += f"🔑 **ID Jalur:** `{jalur_tag}`\n"
            text += "📝 **Koleksi Asli:** " + ", ".join(f"`{k}`" for k in kata_list) + "\n"
            text += "🔍 **Probabilitas Lolos Mutasi (>=50%):**\n"
            for kata in kata_list:
                mutasi = generate_kandidat_mutasi_liar(kata)
                text  += f"• `{kata}` ➔ `({'|'.join(mutasi)})`\n"
            text += f"💥 **Full Interlock (Acuan Utama):**\n`{pola_full}`\n"
            text += "──────────────────────────\n"

        nav = []
        if cp > 1:
            nav.append(InlineKeyboardButton("⏪ SEBELUMNYA", callback_data=f"nx_regex_page_{cp-1}"))
        if (off + limit) < total:
            nav.append(InlineKeyboardButton("SELANJUTNYA ⏩", callback_data=f"nx_regex_page_{cp+1}"))

        rows_kb = []
        if nav:
            rows_kb.append(nav)
        rows_kb.append([InlineKeyboardButton("🔙 KEMBALI", callback_data="nx_global_regex_menu")])
        await _safe_edit(cq.message, text[:3900], InlineKeyboardMarkup(rows_kb))

    elif data.startswith("nx_owner_regex_page_"):
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        page = int(data.split("_")[-1])
        await _render_owner_regex_page(client, cq.message.chat.id, cq.message.id, page)

    elif data.startswith("nx_owner_rgx_add_"):
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        page = int(data.split("_")[-1])
        _owner_regex_fsm[user_id] = cq.message.id
        await _safe_edit(
            cq.message,
            "⚙️ **MODE INPUT OWNER REGEX AKTIF**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Kirim kata-kata yang ingin diblokir, pisahkan dengan tanda `|`",
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Batalkan", callback_data=f"nx_owner_regex_page_{page}")]])
        )

    elif data.startswith("nx_owner_rgx_del_"):
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.", show_alert=True)
            except Exception:
                pass
            return
        # Sama persis pola whitelist nexus — pakai nexus_regex_delete_by_id
        # yang handle MongoDB (ObjectId) maupun SQLite (str) secara otomatis
        obj_id  = data[len("nx_owner_rgx_del_"):]
        deleted = await nexus_regex_delete_by_id(obj_id)
        await cq.answer("🗑 Dihapus." if deleted else "⚠️ Tidak ditemukan.", show_alert=False)
        await _render_owner_regex_page(client, cq.message.chat.id, cq.message.id, 1)

    elif data.startswith("nx_records_page_"):
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        cp    = int(data.split("_")[-1])
        limit = 10
        rows, total = await nexus_get_kalimat_page(cp, limit)
        off   = (cp - 1) * limit
        total_pages = max(1, (total + limit - 1) // limit)

        text = f"📋 **RECORD DATA NEXUS DB — HAL {cp}/{total_pages}**\n"
        text += f"_(Total: {total} kalimat · Ketik /delkalimat untuk hapus via command)_\n\n"
        del_buttons = []
        for idx, row in enumerate(rows, start=(off + 1)):
            icon  = "⏳" if row["status_proses"] == 0 else "✅"
            cuplikan = row["teks"][:60] + ("…" if len(row["teks"]) > 60 else "")
            text += f"`[{idx}]` {icon} `{cuplikan}`\n"
            del_buttons.append([
                InlineKeyboardButton(
                    f"🗑  [{idx}] {cuplikan[:35]}",
                    callback_data=f"nx_rec_del_{row['_id']}_{cp}"
                )
            ])

        nav = []
        if cp > 1:
            nav.append(InlineKeyboardButton("⏪ PREV", callback_data=f"nx_records_page_{cp-1}"))
        if (off + limit) < total:
            nav.append(InlineKeyboardButton("NEXT ⏩", callback_data=f"nx_records_page_{cp+1}"))

        rows_kb = del_buttons.copy()
        if nav:
            rows_kb.append(nav)
        rows_kb.append([InlineKeyboardButton("🔙 KEMBALI", callback_data="nx_owner_menu")])
        await _safe_edit(cq.message, text[:4000], InlineKeyboardMarkup(rows_kb))

    elif data.startswith("nx_rec_del_"):
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Hanya Owner.", show_alert=True)
            except Exception:
                pass
            return
        # format: nx_rec_del_<oid>_<page>
        parts   = data[len("nx_rec_del_"):].rsplit("_", 1)
        oid_str = parts[0]
        cp      = int(parts[1]) if len(parts) > 1 else 1
        deleted = await nexus_delete_kalimat_by_id(oid_str)
        await cq.answer("🗑 Kalimat dihapus." if deleted else "⚠️ Data tidak ditemukan.", show_alert=False)
        # Refresh halaman yang sama (bisa jadi sudah berkurang, clamp ke total_pages baru)
        limit = 10
        _, total_after = await nexus_get_kalimat_page(1, 1)
        total_pages = max(1, (total_after + limit - 1) // limit)
        cp = min(cp, total_pages)
        rows, total = await nexus_get_kalimat_page(cp, limit)
        off = (cp - 1) * limit
        text = f"📋 **RECORD DATA NEXUS DB — HAL {cp}/{total_pages}**\n"
        text += f"_(Total: {total} kalimat · Ketik /delkalimat untuk hapus via command)_\n\n"
        del_buttons = []
        for idx, row in enumerate(rows, start=(off + 1)):
            icon     = "⏳" if row["status_proses"] == 0 else "✅"
            cuplikan = row["teks"][:60] + ("…" if len(row["teks"]) > 60 else "")
            text    += f"`[{idx}]` {icon} `{cuplikan}`\n"
            del_buttons.append([
                InlineKeyboardButton(
                    f"🗑  [{idx}] {cuplikan[:35]}",
                    callback_data=f"nx_rec_del_{row['_id']}_{cp}"
                )
            ])
        nav = []
        if cp > 1:
            nav.append(InlineKeyboardButton("⏪ PREV", callback_data=f"nx_records_page_{cp-1}"))
        if (off + limit) < total:
            nav.append(InlineKeyboardButton("NEXT ⏩", callback_data=f"nx_records_page_{cp+1}"))
        rows_kb = del_buttons.copy()
        if nav:
            rows_kb.append(nav)
        rows_kb.append([InlineKeyboardButton("🔙 KEMBALI", callback_data="nx_owner_menu")])
        await _safe_edit(cq.message, text[:4000], InlineKeyboardMarkup(rows_kb))

    elif data.startswith("nx_whitelist_page_"):
        if user_id != OWNER_ID:
            try:
                await cq.answer("⛔ Hanya Owner!", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        page = int(data.split("_")[-1])
        await _render_whitelist_page(client, cq.message.chat.id, cq.message.id, page)

    elif data.startswith("nx_wl_add_"):
        if user_id != OWNER_ID:
            try:
                await cq.answer("⛔ Hanya Owner!", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        page = int(data.split("_")[-1])
        _whitelist_fsm[user_id] = cq.message.id
        await _safe_edit(
            cq.message,
            "🛡️ **MODE INPUT WHITELIST AKTIF**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Kirim kata-kata yang ingin **diputihkan**, pisahkan dengan `|`\n\n"
            "💡 _Contoh: `sini | di`_",
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Batalkan", callback_data=f"nx_whitelist_page_{page}")]])
        )

    elif data.startswith("nx_wl_del_"):
        if user_id != OWNER_ID:
            try:
                await cq.answer("⛔ Hanya Owner!", show_alert=True)
            except Exception:
                pass
            return
        obj_id  = data[len("nx_wl_del_"):]
        deleted = await nexus_whitelist_delete_by_id(obj_id)
        if deleted:
            invalidate_nexus_wl_cache()
        await cq.answer("🗑 Dihapus." if deleted else "⚠️ Tidak ditemukan.", show_alert=False)
        await _render_whitelist_page(client, cq.message.chat.id, cq.message.id, 1)

    elif data == "nx_wl_clear_confirm":
        if user_id != OWNER_ID:
            try:
                await cq.answer("⛔ Hanya Owner!", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        await _safe_edit(
            cq.message,
            "⚠️ **KONFIRMASI HAPUS SEMUA WHITELIST**\n\nSemua pola whitelist akan dihapus permanen. Lanjutkan?",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Ya, Hapus Semua", callback_data="nx_wl_clear_exec")],
                [InlineKeyboardButton("🚫 Batal",           callback_data="nx_whitelist_page_1")],
            ])
        )

    elif data == "nx_wl_clear_exec":
        if user_id != OWNER_ID:
            try:
                await cq.answer("⛔ Hanya Owner!", show_alert=True)
            except Exception:
                pass
            return
        n = await nexus_whitelist_clear()
        invalidate_nexus_wl_cache()
        try:
            await cq.answer(f"🗑 {n} whitelist dihapus.", show_alert=True)
        except Exception:
            pass
        await _render_whitelist_page(client, cq.message.chat.id, cq.message.id, 1)

    elif data == "nx_force_calc":
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.", show_alert=True)
            except Exception:
                pass
            return
        await generate_regex_otomatis_async()
        me   = await client.get_me()
        text = await _welcome_text()
        await _safe_edit(cq.message, text, _main_markup(me.username))
        try:
            await cq.answer("⚡ Rekalkulasi Paksa Sukses!", show_alert=True)
        except Exception:
            pass

    elif data == "nx_menu_reset":
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        await _safe_edit(
            cq.message,
            "⚠️ **MAINFRAME PURGE MEMORY ZONE**\n━━━━━━━━━━━━━━━━━━━━━━━━━━\nPilih partisi memori yang akan dihancurkan:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ PURGE KALIMAT + AI REGEX", callback_data="nx_c_kalimat")],
                [InlineKeyboardButton("🧹 FLUSH AI REGEX SAJA",      callback_data="nx_c_regex")],
                [InlineKeyboardButton("🔙 URUNGKAN PLAN",            callback_data="nx_home")],
            ]),
        )

    elif data == "nx_c_kalimat":
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        await nexus_clear_kalimat()
        me   = await client.get_me()
        text = await _welcome_text()
        await _safe_edit(cq.message, text, _main_markup(me.username))

    elif data == "nx_c_regex":
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Fitur ini aktif & berfungsi normal.\nHanya Owner bot yang bisa mengaksesnya.", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        await nexus_clear_regex()
        me   = await client.get_me()
        text = await _welcome_text()
        await _safe_edit(cq.message, text, _main_markup(me.username))

    elif data.startswith("nx_actlog_page_"):
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Hanya Owner!", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        page  = int(data.split("_")[-1])
        limit = 5
        rows, total = await nexus_actlog_get_page(page, limit)
        total_pages = max(1, (total + limit - 1) // limit)

        if not rows:
            await _safe_edit(
                cq.message,
                "📋 **LOG AKTIVITAS NEXUS AI**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "_Belum ada aktivitas yang tercatat._\n\n"
                "Log akan muncul setelah bot menghapus spam pertama.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Kembali", callback_data="nx_owner_menu"),
                ]]),
            )
            return

        IKON = {"HAPUS": "🗑️", "WHITELIST": "🛡️", "KEROYOKAN": "☠️"}

        text = (
            f"📋 **LOG AKTIVITAS NEXUS AI**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Total: **{total}** entri   |   Hal. **{page}/{total_pages}**\n\n"
        )
        for entry in rows:
            aksi   = entry.get("aksi", "?")
            ikon   = IKON.get(aksi, "•")
            ts     = entry.get("ts")
            if hasattr(ts, "strftime"):
                waktu = ts.strftime("%d/%m %H:%M")
            else:
                waktu = str(ts)[:16] if ts else "?"
            uname  = entry.get("user_name", "?")[:20]
            ctitle = entry.get("chat_title", "?")[:22]
            alasan = entry.get("alasan", "")[:60]
            conf   = entry.get("confidence", 0.0)
            konten = entry.get("content", "")[:60]

            conf_str = f"  AI: **{conf*100:.0f}%**" if conf > 0 else ""
            text += (
                f"{ikon} **{aksi}** — `{waktu}`{conf_str}\n"
                f"👤 {uname}  📌 {ctitle}\n"
                f"🔑 _{alasan}_\n"
                f"💬 `{konten}`\n"
                f"─────────────────────\n"
            )

        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"nx_actlog_page_{page-1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"nx_actlog_page_{page+1}"))

        rows_kb = []
        if nav:
            rows_kb.append(nav)
        rows_kb.append([InlineKeyboardButton("🧹 Hapus Semua Log", callback_data="nx_actlog_clear_confirm")])
        rows_kb.append([InlineKeyboardButton("🔙 Kembali", callback_data="nx_owner_menu")])
        await _safe_edit(cq.message, text[:4000], InlineKeyboardMarkup(rows_kb))

    elif data == "nx_actlog_clear_confirm":
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Hanya Owner!", show_alert=True)
            except Exception:
                pass
            return
        try:
            await cq.answer()
        except Exception:
            pass
        await _safe_edit(
            cq.message,
            "⚠️ **HAPUS SEMUA LOG AKTIVITAS?**\n\nSeluruh riwayat tindakan bot akan dihapus permanen.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Ya, Hapus", callback_data="nx_actlog_clear_exec")],
                [InlineKeyboardButton("🚫 Batal",     callback_data="nx_actlog_page_1")],
            ]),
        )

    elif data == "nx_actlog_clear_exec":
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Hanya Owner!", show_alert=True)
            except Exception:
                pass
            return
        n = await nexus_actlog_clear()
        try:
            await cq.answer(f"🧹 {n} entri log dihapus.", show_alert=True)
        except Exception:
            pass
        await _safe_edit(
            cq.message,
            "📋 **LOG AKTIVITAS NEXUS AI**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Log telah dibersihkan._",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Kembali", callback_data="nx_owner_menu"),
            ]]),
        )

    elif data.startswith("nx_ai_debug_page_"):
        if user_id != OWNER_ID:
            try:
                await cq.answer("🔒 Hanya Owner!", show_alert=True)
            except Exception:
                pass
            return
        try:
            page = int(data.split("_")[-1])
            if page < 1:
                page = 1
        except (ValueError, IndexError):
            page = 1

        from database import ai_debug_log_get_page as _get_ai_log
        docs, total = await _get_ai_log(page, per_page=5)
        total_pages = max(1, (total + 4) // 5)

        if not docs and page == 1:
            await _safe_edit(
                cq.message,
                "🔬 **DEBUG AI — LOG 24 JAM**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "_Belum ada aktivitas AI dalam 24 jam terakhir._\n\n"
                "_Log muncul saat AI mendeteksi spam, belajar dari laporan /spam, "
                "atau setelah cron midnight berjalan._",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Kembali", callback_data="nx_owner_menu"),
                ]]),
            )
            return

        text = (
            "🔬 **DEBUG AI — LOG 24 JAM TERAKHIR**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📄 Hal **{page}/{total_pages}** | Total: **{total}** aksi\n\n"
        )
        for entry in docs:
            aksi       = entry.get("aksi", "?")
            confidence = entry.get("confidence", 0.0)
            ringkasan  = entry.get("ringkasan", "")[:100]
            ts_raw     = entry.get("ts", 0)
            try:
                from datetime import datetime as _dt, timezone as _tz_mod
                dt    = _dt.fromtimestamp(ts_raw, tz=_tz_mod.utc).astimezone(TZ_JAKARTA)
                waktu = dt.strftime("%d/%m %H:%M WIB")
            except Exception:
                waktu = str(ts_raw)

            pct      = f"{confidence * 100:.0f}%"
            conf_str = f" | `{pct}`" if confidence > 0 else ""
            text += f"**{aksi}**{conf_str}\n"
            text += f"🕐 `{waktu}`\n"
            if ringkasan:
                text += f"_{ringkasan}_\n"
            text += "─────────────────────\n"

        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"nx_ai_debug_page_{page-1}"))
        nav.append(InlineKeyboardButton("🔄 Refresh", callback_data="nx_ai_debug_page_1"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"nx_ai_debug_page_{page+1}"))

        rows_kb = []
        if nav:
            rows_kb.append(nav)
        rows_kb.append([InlineKeyboardButton("🔙 Kembali", callback_data="nx_owner_menu")])
        await _safe_edit(cq.message, text[:4000], InlineKeyboardMarkup(rows_kb))

    else:

        try:
            await cq.answer()
        except Exception:
            pass
