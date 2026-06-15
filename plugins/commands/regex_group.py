"""
plugins/commands/regex_group.py
────────────────────────────────
Perintah admin grup untuk kelola regex lokal:
  /addgroupregex, /delgroupregex, /listgroupregex

FORMAT INPUT:
  kata | kata | kata
  Pisahkan kata dengan | (tanda pipa). Setiap kata diproses dengan AI mutasi
  dan dirakit menjadi pola interlock AND. Semua kata HARUS hadir sekaligus
  dalam satu pesan agar filter aktif.

  Contoh:
    /addgroupregex togel
    /addgroupregex jual | akun
    /addgroupregex promo | slot | link

  Semantik | adalah AND (bukan OR) — sama seperti sistem owner spam Nexus.
"""

import re
from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from database import db, is_admin, auto_delete_reply
from core.regex_utils import build_group_interlock, generate_kandidat_mutasi_liar, pipeline_pembersihan

# Alias untuk kompatibilitas file lain yang masih import nama lama
_build_group_interlock = build_group_interlock

group_regex_db = db["regex_per_group"]
DELAY_NOTIF    = 10


@Client.on_message(filters.command("addgroupregex") & filters.group)
async def add_group_regex(client: Client, message):
    cid = message.chat.id
    uid = message.from_user.id if message.from_user else None
    if not await is_admin(client, cid, uid):
        return

    if len(message.command) < 2:
        res = await message.reply(
            "<b>❖ FORMAT INPUT ❖</b>\n\n"
            "⚠️ <b>Cara penggunaan:</b>\n"
            "<code>/addgroupregex [kata]</code>\n\n"
            "<b>✦ Contoh — 1 kata:</b>\n"
            "<code>/addgroupregex togel</code>\n\n"
            "<b>✦ Contoh — 2 kata (AND, harus ada keduanya):</b>\n"
            "<code>/addgroupregex jual | akun</code>\n\n"
            "<b>✦ Contoh — 3 kata (AND):</b>\n"
            "<code>/addgroupregex promo | slot | link</code>\n\n"
            "⚠️ Tanda <code>|</code> = AND (semua kata harus ada sekaligus).\n"
            "Setiap kata diproses AI mutasi otomatis.",
            parse_mode=ParseMode.HTML
        )
        return await auto_delete_reply([res, message], delay=DELAY_NOTIF)

    raw_input = " ".join(message.command[1:])

    try:
        pola, kata_list = build_group_interlock(raw_input)
        re.compile(pola)
    except (ValueError, re.error) as e:
        res = await message.reply(
            f"<b>❖ ERROR ❖</b>\n\n"
            f"❌ <b>Input Gagal Diproses!</b>\n"
            f"◈ <b>Input:</b> <code>{raw_input}</code>\n"
            f"◈ <b>Keterangan:</b> <code>{e}</code>",
            parse_mode=ParseMode.HTML
        )
        return await auto_delete_reply([res, message], delay=DELAY_NOTIF)

    raw_display = " | ".join(kata_list) if kata_list else raw_input

    mutasi_map: dict = {}
    # Pisah kata asli dari raw_input agar kapital owner terjaga
    kata_asli_list = [k.strip() for k in raw_input.split("|") if k.strip()]
    for i, kata in enumerate(kata_list):
        # Ambil versi asli (dengan kapital) jika tersedia
        kata_dengan_kapital = kata_asli_list[i] if i < len(kata_asli_list) else kata
        import re as _re4
        kata_bersih = _re4.sub(r"\(?[×xX]\d+\)?", "", kata_dengan_kapital)
        kata_bersih = _re4.sub(r"[^\w]", "", kata_bersih).strip()
        if kata_bersih:
            mutasi_map[kata] = generate_kandidat_mutasi_liar(kata_bersih)

    await group_regex_db.update_one(
        {"chat_id": cid, "pattern": pola},
        {"$set": {
            "chat_id":   cid,
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
        invalidate_local_regex_cache(cid)
    except Exception:
        pass

    kata_str = " + ".join(f"<code>{k}</code>" for k in kata_list)
    res = await message.reply(
        f"<b>❖ FILTER KATA DITAMBAHKAN ❖</b>\n\n"
        f"✅ <b>Filter Khusus Grup Berhasil Tersimpan!</b>\n"
        f"◈ <b>Kata Kunci:</b> {kata_str}\n"
        f"◈ <b>Semantik:</b> Semua kata wajib ada sekaligus (AND)\n"
        f"◈ <b>Mutasi:</b> Otomatis mendeteksi variasi huruf & leet\n\n"
        f"<i>Gunakan /listgroupregex untuk melihat semua filter aktif.</i>",
        parse_mode=ParseMode.HTML
    )
    await auto_delete_reply([res, message], delay=DELAY_NOTIF)


@Client.on_message(filters.command("delgroupregex") & filters.group)
async def del_group_regex(client: Client, message):
    cid = message.chat.id
    uid = message.from_user.id if message.from_user else None
    if not await is_admin(client, cid, uid):
        return

    if len(message.command) < 2:
        res = await message.reply(
            "⚠️ <b>Format Input Salah</b>\n"
            "<b>Format:</b> <code>/delgroupregex [kata]</code>\n\n"
            "Gunakan kata yang sama seperti saat menambahkan.",
            parse_mode=ParseMode.HTML
        )
        return await auto_delete_reply([res, message], delay=DELAY_NOTIF)

    raw_input = " ".join(message.command[1:])

    try:
        pola, kata_list = build_group_interlock(raw_input)
        result = await group_regex_db.delete_one({"chat_id": cid, "pattern": pola})
    except (ValueError, re.error):
        result = None

    if not result or not result.deleted_count:
        raw_display = raw_input.strip()
        result = await group_regex_db.delete_one({"chat_id": cid, "raw": raw_display})

    if result and result.deleted_count:
        try:
            from plugins.filters.antispam import invalidate_local_regex_cache
            invalidate_local_regex_cache(cid)
        except Exception:
            pass
        res = await message.reply(
            f"🗑️ <b>Filter Grup Berhasil Dihapus!</b>\n"
            f"◈ <b>Kata:</b> <code>{raw_input}</code>",
            parse_mode=ParseMode.HTML
        )
    else:
        res = await message.reply(
            "❌ <b>Kata Tidak Ditemukan di Daftar Filter Grup Ini.</b>\n"
            "Gunakan /listgroupregex untuk melihat daftar yang aktif.",
            parse_mode=ParseMode.HTML
        )
    await auto_delete_reply([res, message], delay=DELAY_NOTIF)


@Client.on_message(filters.command("listgroupregex") & filters.group)
async def list_group_regex(client: Client, message):
    cid = message.chat.id
    uid = message.from_user.id if message.from_user else None
    if not await is_admin(client, cid, uid):
        return

    docs = [doc async for doc in group_regex_db.find({"chat_id": cid})]

    if docs:
        lines = "\n".join(f"  ◈ <b>{doc.get('raw', '—')}</b>" for doc in docs)
        text  = (
            "<b>❖ FILTER KATA GRUP ❖</b>\n"
            f"⚡ <b>Total Aktif:</b> <code>{len(docs)} Pola</code>\n\n"
            "<b>▰▰▰ DAFTAR KATA DIBLOKIR ▰▰▰</b>\n"
            f"{lines}\n\n"
            "<i>(Aturan di atas hanya berjalan eksklusif di grup ini)\n"
            "Semua entri menggunakan deteksi mutasi otomatis.</i>"
        )
    else:
        text = (
            "<b>❖ FILTER KATA GRUP ❖</b>\n\n"
            "📭 <b>Daftar filter kata di grup ini masih kosong.</b>\n"
            "Gunakan <code>/addgroupregex kata</code> untuk menambah aturan baru."
        )

    res = await message.reply(text, parse_mode=ParseMode.HTML)
    await auto_delete_reply([res, message], delay=30)
