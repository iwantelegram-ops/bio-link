"""
plugins/ui/pages.py
───────────────────
Semua fungsi pembuat konten halaman (teks + keyboard).
Tidak ada handler di sini — murni "data layer" untuk UI.

Dipanggil oleh:
  - plugins/ui/handlers_dm.py   (callback & /start)
  - plugins/ui/handlers_fsm.py  (setelah FSM selesai)

FIXED: page_group_log sekarang return HTML murni (bukan marker [BQ]) sehingga
  cb_grp_log bisa menggunakan safe_edit biasa tanpa raw API.
  Ini memperbaiki crash collapsed=True pada Pyrogram 2.0.106.
"""

import os
from datetime import datetime, timezone
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import get_config, db, get_group_action_log_page, TZ_WIB as _TZ_WIB, get_bot_config
from video_call import security_os_get_status, is_userbot_ready

_OWNER_ID        = int(os.environ.get("OWNER_ID", 0))
_CHANNEL_OWNER   = int(os.environ.get("CHANNEL_OWNER", 0))


group_regex_db = db["regex_per_group"]
free_col       = db["free_per_group"]
whitelist_col  = db["whitelist_per_group"]

TOTAL_GUIDE_PAGES = 9


# ─────────────────────────────────────────────────────────────────────────────
#  Halaman Utama — Menu Utama
# ─────────────────────────────────────────────────────────────────────────────
async def _fetch_owner_line(client) -> str:
    parts = []
    try:
        if _OWNER_ID:
            owner = await client.get_users(_OWNER_ID)
            name  = owner.first_name + (f" {owner.last_name}" if owner.last_name else "")
            parts.append(f'👤 By <a href="tg://user?id={_OWNER_ID}">{name}</a>')
    except Exception:
        pass

    if _CHANNEL_OWNER:
        ch_title = ch_link = None
        try:
            # Coba resolve langsung (berhasil jika sesi sudah kenal channel)
            ch      = await client.get_chat(_CHANNEL_OWNER)
            ch_title = ch.title or "Channel"
            ch_uname = getattr(ch, "username", None) or ""
            ch_link  = f"https://t.me/{ch_uname}" if ch_uname else None
        except Exception:
            # Sesi baru belum kenal peer → baca dari cache DB yang disimpan saat startup
            try:
                ch_title = await get_bot_config("channel_owner_title")
                ch_uname = await get_bot_config("channel_owner_username") or ""
                if not ch_title and ch_uname:
                    # Coba resolve via @username dari DB
                    try:
                        ch2 = await client.get_chat(f"@{ch_uname}")
                        ch_title = ch2.title or ch_uname
                    except Exception:
                        ch_title = ch_uname
                ch_link = f"https://t.me/{ch_uname}" if ch_uname else None
            except Exception:
                pass

        if ch_title:
            if ch_link:
                parts.append(f'📢 <a href="{ch_link}">{ch_title}</a>')
            else:
                parts.append(f'📢 {ch_title}')

    return "  ·  ".join(parts) if parts else ""


async def page_start(client):
    me      = await client.get_me()
    add_url = f"t.me/{me.username}?startgroup=true&admin=delete_messages+ban_users"

    owner_line = await _fetch_owner_line(client)
    footer = f"\n<code>{'─' * 26}</code>\n{owner_line}" if owner_line else ""

    text = (
        "🛡️ <b>ANTIGCAST</b>\n"
        "<i>Anti-Spam Engine Cerdas · Powered by Nexus AI</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Sistem pertahanan otomatis untuk grup Telegram.\n"
        "Belajar dari setiap laporan spam dan membangun pertahanan\n"
        "baru secara otomatis setiap tengah malam.\n\n"
        "<b>⚡ 6 LAPIS PERLINDUNGAN:</b>\n"
        "◈ <b>Anti-Spam Lokal</b> — hapus pesan duplikat berulang\n"
        "◈ <b>Anti-GCast</b> — blokir broadcast massal lintas grup\n"
        "◈ <b>Filter Kata AI</b> — regex mutasi otomatis per kata kunci\n"
        "◈ <b>CAS Global</b> — auto-ban 200.000+ spammer terverifikasi\n"
        "◈ <b>Bio Link Detector</b> — filter user dengan link di bio\n"
        "◈ <b>Nexus AI Engine</b> — rebuild pola tiap pukul 00:00 WIB\n\n"
        "🔇 <b>SISTEM HUKUMAN MUTE:</b>\n"
        "<i>10 pelanggaran spam berturut-turut → mute otomatis (berlipat)</i>\n\n"
        "<i>Pilih grup dari <b>⚙️ Kelola Grup</b> untuk mulai mengatur.</i>"
        f"{footer}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕  Pasang di Grup Saya", url=add_url)],
        [
            InlineKeyboardButton("⚙️  Kelola Grup",  callback_data="admin_menu"),
            InlineKeyboardButton("📖  Panduan",       callback_data="guide_1"),
        ],
        [InlineKeyboardButton("🤖  Nexus AI Panel",  callback_data="nx_home")],
    ])
    return text, keyboard


# ─────────────────────────────────────────────────────────────────────────────
#  Panduan Multi-Halaman (9 Halaman · Next/Prev)
# ─────────────────────────────────────────────────────────────────────────────

_GUIDE_CONTENT = {

    1: (
        "📖 <b>PANDUAN GLOBAL SPAM</b>  <code>[1/{t}]</code>\n"
        "<i>Apa Itu Bot Ini?</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Bot ini adalah <b>sistem keamanan otomatis</b> untuk grup Telegram.\n"
        "Dirancang membasmi spam, promosi liar, dan akun berbahaya — tanpa repot manual.\n\n"
        "<b>🛡️ MODUL PERLINDUNGAN:</b>\n\n"
        "🔁 <b>Anti-Spam Lokal</b>\n"
        "   Hapus pesan duplikat berulang dari satu user.\n\n"
        "🌐 <b>Anti-GCast Global</b>\n"
        "   Blokir pesan broadcast yang disebar ke banyak grup.\n\n"
        "🔤 <b>Filter Kata (Regex)</b>\n"
        "   Larang kata/kalimat promosi spesifik secara akurat.\n\n"
        "🛡️ <b>CAS Protection</b>\n"
        "   Auto-ban dari database 200.000+ spammer terverifikasi.\n\n"
        "🔍 <b>Bio Link Detector</b>\n"
        "   Filter user yang menyimpan link di profil bio mereka.\n\n"
        "🤖 <b>Nexus AI Engine</b>\n"
        "   AI yang belajar dari laporan spam dan merakit pola pertahanan\n"
        "   otomatis setiap hari pukul 00:00 WIB.\n\n"
        "🔇 <b>Sistem Mute Eskalasi</b>\n"
        "   10 pelanggaran spam berturut-turut → mute otomatis.\n"
        "   Berlaku untuk SEMUA jenis spam."
    ),

    2: (
        "📖 <b>PANDUAN GLOBAL SPAM</b>  <code>[2/{t}]</code>\n"
        "<i>Cara Pasang & Mulai</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>✦ 4 LANGKAH AKTIVASI:</b>\n\n"
        "<b>① Tambah ke Grup</b>\n"
        "   Tekan <b>「 ➕ Pasang di Grup Saya 」</b> di menu utama,\n"
        "   lalu pilih grup tujuan.\n\n"
        "<b>② Berikan Akses Admin</b>\n"
        "   Bot butuh 2 izin untuk bekerja optimal:\n"
        "   ◈ <code>Hapus Pesan</code> — agar bisa eksekusi spam\n"
        "   ◈ <code>Batasi Anggota</code> — untuk mute otomatis & CAS auto-ban\n\n"
        "<b>③ Cek Status</b>\n"
        "   Ketik <code>/status</code> di grup untuk melihat semua modul.\n\n"
        "<b>④ Atur via Panel</b>\n"
        "   Ketik <code>/antigcast</code> di grup → bot kirim panel\n"
        "   kontrol lengkap ke DM kamu.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>⚡ Semua filter aktif otomatis.</b>\n"
        "<i>Kamu tinggal menyesuaikan sesuai kebutuhan grup.</i>"
    ),

    3: (
        "📖 <b>PANDUAN GLOBAL SPAM</b>  <code>[3/{t}]</code>\n"
        "<i>Perintah Pengaturan Grup</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>👮 Khusus Admin Grup · Ketik langsung di grup</i>\n\n"
        "<b>✦ TOGGLE ON / OFF:</b>\n\n"
        "<code>/setlocal on</code>  atau  <code>off</code>\n"
        "   Anti-Spam Lokal — hapus pesan duplikat berulang.\n\n"
        "<code>/setglobal on</code>  atau  <code>off</code>\n"
        "   Anti-GCast Global — blokir broadcast massal.\n\n"
        "<code>/setbio on</code>  atau  <code>off</code>\n"
        "   Bio Link Detector — filter user dengan link di bio.\n\n"
        "<b>✦ KONFIGURASI LANJUTAN:</b>\n\n"
        "<code>/setwaktu [menit]</code>\n"
        "   Durasi bot mengingat pesan spam.\n"
        "   Contoh: <code>/setwaktu 30</code> → ingat selama 30 menit.\n\n"
        "<code>/status</code>\n"
        "   Dashboard status semua modul di grup ini.\n\n"
        "<code>/antigcast</code>\n"
        "   Kirim panel kontrol lengkap ke DM kamu.\n"
        "   <i>(Lebih canggih dari semua command di atas)</i>"
    ),

    4: (
        "📖 <b>PANDUAN GLOBAL SPAM</b>  <code>[4/{t}]</code>\n"
        "<i>Perintah /spam — Fitur Inti Nexus AI</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>👮 Khusus Admin Grup</i>\n\n"
        "<b>✦ APA ITU /spam?</b>\n\n"
        "Perintah <code>/spam</code> adalah cara kamu melaporkan pesan berbahaya\n"
        "ke <b>otak AI (Nexus Engine)</b>.\n\n"
        "Setiap laporan dianalisis dan diubah menjadi <b>pola pertahanan\n"
        "otomatis</b> yang berlaku di semua grup pengguna bot ini.\n\n"
        "<b>✦ CARA PAKAI (3 Langkah):</b>\n\n"
        "<b>①</b> Temukan pesan spam di grup.\n"
        "<b>②</b> Tekan lama → pilih <b>Balas (Reply)</b>.\n"
        "<b>③</b> Kirim: <code>/spam</code>\n\n"
        "Bot akan otomatis:\n"
        "◈ Hapus pesan spam dari grup\n"
        "◈ Simpan kontennya ke database Nexus AI\n"
        "◈ Proses pada siklus tengah malam (00:00 WIB)\n\n"
        "<b>✦ KENAPA PENTING?</b>\n\n"
        "Semakin banyak laporan, semakin cerdas AI membangun pola.\n"
        "Ini kontribusi nyata melindungi komunitas Telegram secara kolektif.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>⚠️ Wajib pakai reply.</b>\n"
        "<i>/spam tanpa reply tidak akan diproses.</i>"
    ),

    5: (
        "📖 <b>PANDUAN GLOBAL SPAM</b>  <code>[5/{t}]</code>\n"
        "<i>Filter Kata Khusus Grup</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>👮 Khusus Admin Grup</i>\n\n"
        "<b>✦ PERINTAH DASAR:</b>\n\n"
        "<code>/addgroupregex [kata]</code>\n"
        "   Tambah kata yang dilarang di grup ini.\n\n"
        "<code>/delgroupregex [kata]</code>\n"
        "   Hapus kata dari daftar filter.\n\n"
        "<code>/listgroupregex</code>\n"
        "   Lihat semua kata yang sedang diblokir.\n\n"
        "<b>✦ FORMAT INPUT:</b>\n\n"
        "Pisahkan kata dengan <code>|</code> — semua kata HARUS hadir\n"
        "sekaligus dalam pesan agar filter aktif (AND semantics).\n\n"
        "<b>Blokir 1 kata (dengan deteksi mutasi otomatis):</b>\n"
        "<code>/addgroupregex togel</code>\n"
        "   <i>→ deteksi: togel, t0g3l, togg3l, dll.</i>\n\n"
        "<b>Blokir jika ada 'jual' DAN 'akun' sekaligus:</b>\n"
        "<code>/addgroupregex jual | akun</code>\n\n"
        "<b>Blokir jika ada tiga kata sekaligus:</b>\n"
        "<code>/addgroupregex promo | slot | link</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>⚠️ Tanda | = AND, bukan OR.</b>\n"
        "<i>Setiap kata diproses AI mutasi — variasi huruf & leet terdeteksi otomatis.</i>\n\n"
        "<i>💡 Kelola filter lebih mudah via panel DM: /antigcast</i>"
    ),

    6: (
        "📖 <b>PANDUAN GLOBAL SPAM</b>  <code>[6/{t}]</code>\n"
        "<i>CAS Protection — Anti-Spam Global</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>✦ APA ITU CAS?</b>\n\n"
        "<b>CAS (Combot Anti-SPAM)</b> adalah database global berisi\n"
        "200.000+ akun spammer terverifikasi dari seluruh Telegram.\n\n"
        "Saat user baru masuk grup → bot langsung cek database.\n"
        "Jika terdeteksi → <b>auto-ban otomatis</b>. Tanpa pengecualian.\n\n"
        "<b>✦ WHITELIST CAS (Pengecualian):</b>\n\n"
        "<code>/wlcas</code> + <i>reply</i> ke pesannya\n"
        "   User dikecualikan dari ban CAS di grup ini.\n\n"
        "<code>/wlcas [ID]</code>\n"
        "   Kecualikan berdasarkan User ID langsung.\n\n"
        "<code>/unwlcas</code> + <i>reply</i>  /  <code>/unwlcas [ID]</code>\n"
        "   Cabut pengecualian CAS.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>⚠️ CAS selalu aktif dan tidak bisa dimatikan.</b>\n"
        "<i>Whitelist adalah satu-satunya cara mengecualikan user tertentu.</i>\n\n"
        "<i>💡 Kelola whitelist CAS via panel DM: /antigcast → Grup → CAS</i>"
    ),

    7: (
        "📖 <b>PANDUAN GLOBAL SPAM</b>  <code>[7/{t}]</code>\n"
        "<i>Member VIP — Bypass Semua Filter</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>👮 Khusus Admin Grup</i>\n\n"
        "<b>✦ APA ITU MEMBER VIP?</b>\n\n"
        "User VIP <b>dibebaskan dari semua filter bot</b> di grup tertentu.\n"
        "Cocok untuk trusted member atau yang sering kena false positive.\n\n"
        "<b>✦ PERINTAH:</b>\n\n"
        "<code>/vip</code> + <i>reply</i> ke pesannya\n"
        "   Jadikan user sebagai Member VIP.\n\n"
        "<code>/vip [ID]</code>\n"
        "   Tambahkan berdasarkan User ID.\n\n"
        "<code>/unvip</code> + <i>reply</i>  /  <code>/unvip [ID]</code>\n"
        "   Cabut status VIP.\n\n"
        "<b>✦ EFEK VIP (bebas dari semua ini):</b>\n"
        "◈ Anti-Spam Lokal\n"
        "◈ Anti-GCast Global\n"
        "◈ Filter Kata (Regex)\n"
        "◈ Bio Link Detector\n"
        "◈ CAS Auto-Ban\n"
        "◈ Sistem Mute Eskalasi\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>⚠️ Status VIP hanya berlaku di grup tempat ditetapkan.</b>\n"
        "<i>Tidak berlaku lintas grup.</i>"
    ),

    8: (
        "📖 <b>PANDUAN GLOBAL SPAM</b>  <code>[8/{t}]</code>\n"
        "<i>Nexus AI Panel & Perintah Owner</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>✦ NEXUS AI — UNTUK SEMUA USER:</b>\n\n"
        "Panel Nexus AI dapat diakses langsung dari\n"
        "menu utama bot → tombol <b>🤖 Nexus AI Panel</b>.\n\n"
        "<b>✦ PERINTAH KHUSUS OWNER:</b>\n"
        "<i>⚠️ Hanya pemilik bot — tidak untuk admin grup biasa.</i>\n\n"
        "<code>/addregex [kata1|kata2|kata3]</code>\n"
        "   Tambah pola blokir GLOBAL (berlaku di semua grup).\n"
        "   Nexus AI merakit interlock pattern otomatis.\n\n"
        "<code>/delregex [kata]</code>\n"
        "   Hapus pola blokir global berdasarkan kata kunci.\n\n"
        "<code>/delnexus [kalimat atau pola]</code>\n"
        "   Hapus data spesifik dari database Nexus AI.\n\n"
        "<code>/infobot</code>\n"
        "   Tampilkan semua pola blokir global yang aktif.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🔄 SIKLUS NEXUS AI:</b>\n"
        "Setiap hari pukul <b>00:00 WIB</b>, engine AI memproses semua\n"
        "laporan /spam dan merakit pola pertahanan baru otomatis."
    ),

    9: (
        "📖 <b>PANDUAN GLOBAL SPAM</b>  <code>[9/{t}]</code>\n"
        "🔇 <i>Sistem Mute Eskalasi — Hukuman Spam Otomatis</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>✦ APA ITU SISTEM MUTE ESKALASI?</b>\n\n"
        "Hukuman otomatis yang berlaku jika user spam <b>terus-menerus</b>\n"
        "tanpa jeda pesan bersih, apapun jenis spamnya.\n\n"
        "<b>✦ CARA KERJA:</b>\n\n"
        "📊 <b>Hitungan pelanggaran berturut-turut:</b>\n"
        "◈ Setiap pesan yang dihapus oleh bot (filter apapun) → +1 hitungan\n"
        "◈ Satu pesan bersih (lolos semua filter) → hitungan RESET ke 0\n\n"
        "⚠️ <b>Ambang hukuman:</b>\n"
        "◈ Pelanggaran ke-10 berturut-turut → <b>Mute 5 menit</b>\n\n"
        "📈 <b>Eskalasi (jika masih spam setelah dibuka):</b>\n"
        "◈ Pelanggaran ke-10 berikutnya → <b>Mute 10 menit</b>\n"
        "◈ Lanjut lagi → <b>Mute 20 menit</b>\n"
        "◈ Terus berlipat ganda hingga 80 menit, 160 menit, dst.\n\n"
        "<b>✦ BERLAKU UNTUK SEMUA JENIS SPAM:</b>\n"
        "◈ Filter kata global / grup\n"
        "◈ Anti-spam lokal (duplikat)\n"
        "◈ Anti-GCast global\n"
        "◈ Mention pengguna luar\n"
        "◈ Link dalam pesan\n"
        "◈ Bio link detector\n"
        "◈ Nexus AI detection\n\n"
        "<b>✦ PENGECUALIAN:</b>\n"
        "◈ Admin grup: tidak kena mute\n"
        "◈ Member VIP: tidak kena mute\n"
        "◈ Bot tidak punya hak Batasi Anggota: mute gagal, pesan tetap dihapus\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>💡 Tip:</b> Pastikan bot punya hak <code>Batasi Anggota</code>\n"
        "<i>agar sistem mute bekerja optimal.</i>"
    ),
}


def page_guide(page_num: int):
    p    = max(1, min(page_num, TOTAL_GUIDE_PAGES))
    text = _GUIDE_CONTENT[p].format(t=TOTAL_GUIDE_PAGES)

    nav = []
    if p > 1:
        nav.append(InlineKeyboardButton("⏪ Prev", callback_data=f"guide_{p - 1}"))
    nav.append(InlineKeyboardButton(f"· {p}/{TOTAL_GUIDE_PAGES} ·", callback_data="noop"))
    if p < TOTAL_GUIDE_PAGES:
        nav.append(InlineKeyboardButton("Next ⏩", callback_data=f"guide_{p + 1}"))

    keyboard = InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton("🔙  Menu Utama", callback_data="start")],
    ])
    return text, keyboard


# ─────────────────────────────────────────────────────────────────────────────
#  Halaman — Dasbor Kelola Grup
# ─────────────────────────────────────────────────────────────────────────────
async def page_manage(chat_id: int):
    cfg = await get_config(chat_id)

    def flag(key): return "🟢 ON" if cfg[key] else "🔴 OFF"
    def icon(key): return "✅" if cfg[key] else "❌"

    waktu       = cfg["expiry"] // 60
    regex_count = await group_regex_db.count_documents({"chat_id": chat_id})
    free_count  = await free_col.count_documents({"chat_id": chat_id})

    # Ambil status Security OS
    sec_doc    = await security_os_get_status(chat_id)
    sec_on     = sec_doc.get("enabled", False)
    sec_flag   = "🟢 ON" if sec_on else "🔴 OFF"
    sec_icon   = "✅" if sec_on else "❌"
    ub_ready   = is_userbot_ready()
    ub_hint    = "" if ub_ready else " ⚠️"

    text = (
        f"⚙️ <b>CONTROL PANEL</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{icon('local')} <b>Anti-Spam Lokal</b>  —  <code>{flag('local')}</code>\n"
        f"<i>   Hapus pesan duplikat berulang dari 1 user.</i>\n\n"
        f"{icon('global')} <b>Anti-GCast Global</b>  —  <code>{flag('global')}</code>\n"
        f"<i>   Deteksi & hapus pesan broadcast lintas grup.</i>\n\n"
        f"{icon('bio_check')} <b>Bio Link Detector</b>  —  <code>{flag('bio_check')}</code>\n"
        f"<i>   Filter user yang menyimpan link di bio profil.</i>\n\n"
        f"⏱️ <b>Durasi Memori Spam</b>  —  <code>{waktu} menit</code>\n"
        f"<i>   Bot mengingat pesan selama durasi ini.</i>\n\n"
        f"🔤 <b>Filter Kata Khusus</b>  —  <code>{regex_count} aktif</code>\n"
        f"<i>   Blokir promosi spesifik (contoh: 'jual followers').</i>\n\n"
        f"👑 <b>Member VIP</b>  —  <code>{free_count} user</code>\n"
        f"<i>   User yang dibebaskan dari semua filter bot.</i>\n\n"
        f"🔇 <b>Mute Eskalasi</b>  —  <code>🟢 AKTIF</code>\n"
        f"<i>   10 spam berturut-turut → mute otomatis (berlipat).</i>\n\n"
        f"{sec_icon} <b>Security OS</b>  —  <code>{sec_flag}</code>{ub_hint}\n"
        f"<i>   Turunkan user bio-link dari obrolan suara via userbot.</i>\n\n"
        f"<i>Tap tombol di bawah untuk ubah pengaturan secara instan.</i>"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🔁 Lokal: {flag('local')}",   callback_data=f"tgl_local_{chat_id}"),
            InlineKeyboardButton(f"🌐 GCast: {flag('global')}",  callback_data=f"tgl_global_{chat_id}"),
        ],
        [
            InlineKeyboardButton(f"🔍 Bio: {flag('bio_check')}", callback_data=f"tgl_bio_check_{chat_id}"),
            InlineKeyboardButton(f"⏱ {waktu}mnt", callback_data="noop"),
            InlineKeyboardButton("➖", callback_data=f"time_dec_{chat_id}"),
            InlineKeyboardButton("➕", callback_data=f"time_inc_{chat_id}"),
        ],
        [
            InlineKeyboardButton(f"🔤 Filter ({regex_count})", callback_data=f"rgxpanel_{chat_id}"),
            InlineKeyboardButton(f"👑 VIP ({free_count})",     callback_data=f"freelist_{chat_id}"),
        ],
        [
            InlineKeyboardButton("🛡️ CAS",         callback_data=f"cas_panel_{chat_id}"),
            InlineKeyboardButton("📋 Log Aktivitas", callback_data=f"grp_log_{chat_id}_1"),
        ],
        [
            InlineKeyboardButton(f"🔐 Security OS: {sec_flag}", callback_data=f"secos_panel_{chat_id}"),
        ],
        [InlineKeyboardButton("🔙  Daftar Grup", callback_data="admin_menu")],
    ])
    return text, keyboard


# ─────────────────────────────────────────────────────────────────────────────
#  Halaman — Tutorial & List Regex
# ─────────────────────────────────────────────────────────────────────────────
async def page_regex_tutorial(chat_id: int):
    text = (
        f"🔤 <b>FILTER KATA KHUSUS GRUP</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Blokir pesan yang mengandung kombinasi kata tertentu.\n"
        f"Setiap kata diproses AI mutasi — mendeteksi variasi huruf & leetspeak otomatis.\n\n"
        f"<b>✦ FORMAT INPUT:</b>\n\n"
        f"Pisahkan kata dengan <code> | </code> (tanda pipa)\n"
        f"Semua kata <b>HARUS hadir sekaligus</b> dalam satu pesan\n\n"
        f"<b>📌 1 kata (deteksi mutasi otomatis):</b>\n"
        f"<code>togel</code>\n"
        f"<i>→ mendeteksi: togel, t0g3l, togg3l, t0gel, dll.</i>\n\n"
        f"<b>📌 2 kata — AND (harus ada keduanya):</b>\n"
        f"<code>jual | akun</code>\n"
        f"<i>→ hanya hapus jika ada 'jual' DAN 'akun' bersamaan</i>\n\n"
        f"<b>📌 3 kata — AND (semua wajib ada):</b>\n"
        f"<code>promo | slot | link</code>\n\n"
        f"<b>📌 4 huruf — kapital (huruf wajib ada dalam 1 kata):</b>\n"
        f"<code>boToL | miNYaK</code>\n"
        f"<i>→ maka: huruf b,t dan l wajib ada dlm satu kata di teks di grup, dan m,n,y,k wajib ada dalam target kata minyak di grup. dll.</i>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>⚠️ PENTING:</b> Tanda <code>|</code> = AND (bukan OR)\n"
        f"Semua kata wajib ada bersamaan agar pesan dihapus.\n\n"
        f"<i>Tekan tombol Tambah Filter, lalu ketik kata/kombinasinya.</i>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋  Lihat Filter Tersimpan", callback_data=f"rgxlist_{chat_id}")],
        [InlineKeyboardButton("➕  Tambah Filter Baru",     callback_data=f"rgxadd_{chat_id}")],
        [InlineKeyboardButton("🔙  Kembali ke Panel Grup",  callback_data=f"manage_{chat_id}")],
    ])
    return text, keyboard


async def page_regex_list(chat_id: int, page: int = 1):
    from core.regex_utils import generate_kandidat_mutasi_liar, pipeline_pembersihan

    LIMIT  = 5
    offset = (page - 1) * LIMIT
    total  = await group_regex_db.count_documents({"chat_id": chat_id})
    docs   = [doc async for doc in group_regex_db.find({"chat_id": chat_id}).sort("_id", -1).skip(offset).limit(LIMIT)]
    total_pages = max(1, (total + LIMIT - 1) // LIMIT)

    if docs:
        body        = ""
        del_buttons = []
        for local_i, doc in enumerate(docs):
            global_idx = offset + local_i
            raw        = doc.get("raw", "—")
            pola_full  = doc.get("pola", doc.get("pattern", ""))
            kata_list  = doc.get("kata_list", [])
            mutasi_map = doc.get("mutasi", {})

            if not kata_list and raw != "—":
                kata_list = [k.strip() for k in raw.split("|") if k.strip()]

            body += f"🔑 <b>[LOKAL-{global_idx + 1}]</b>\n"
            body += "📝 <b>Koleksi Asli:</b> " + ", ".join(f"<code>{k}</code>" for k in kata_list) + "\n"

            if mutasi_map:
                body += "🔍 <b>Probabilitas Lolos Mutasi (≥50%):</b>\n"
                for kata in kata_list:
                    mutasi = mutasi_map.get(kata, [])
                    if mutasi:
                        preview = "|".join(mutasi[:3])
                        body += f"• <code>{kata}</code> ➔ <code>{preview}</code>{'...' if len(mutasi) > 3 else ''}\n"
            elif kata_list:
                body += "🔍 <b>Probabilitas Lolos Mutasi (≥50%):</b>\n"
                for kata in kata_list:
                    kata_c = pipeline_pembersihan(kata)
                    if kata_c:
                        mutasi = generate_kandidat_mutasi_liar(kata_c.split()[0])
                        preview = "|".join(mutasi[:3])
                        body += f"• <code>{kata}</code> ➔ <code>{preview}</code>{'...' if len(mutasi) > 3 else ''}\n"

            if pola_full:
                short_pola = pola_full[:80] + ("..." if len(pola_full) > 80 else "")
                body += f"💥 <b>Full Interlock:</b>\n<code>{short_pola}</code>\n"

            body += "──────────────────────────\n"

            doc_id = str(doc["_id"])
            del_buttons.append([InlineKeyboardButton(
                f"🗑  Hapus: {raw[:35]}",
                callback_data=f"rgxdel_{chat_id}_{doc_id}"
            )])

        content = (
            f"⚡ <b>Aktif: {total} pola</b>  ·  Hal {page}/{total_pages}\n\n"
            f"{body}"
            f"<i>Tap 🗑 di bawah untuk hapus filter secara instan.</i>"
        )
    else:
        content     = (
            "📭 <b>Belum ada filter kata.</b>\n\n"
            "<i>Tambahkan kata terlarang dengan tombol di bawah.</i>"
        )
        del_buttons = []

    text = (
        f"🔤 <b>FILTER KATA LOKAL</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{content}\n"
    )

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⏪ Sebelumnya", callback_data=f"rgxlist_{chat_id}_{page - 1}"))
    if (offset + LIMIT) < total:
        nav.append(InlineKeyboardButton("Selanjutnya ⏩", callback_data=f"rgxlist_{chat_id}_{page + 1}"))

    keyboard_rows = del_buttons.copy()
    if nav:
        keyboard_rows.append(nav)
    keyboard_rows += [
        [InlineKeyboardButton("➕  Tambah Filter Baru",      callback_data=f"rgxadd_{chat_id}")],
        [InlineKeyboardButton("🔙  Kembali ke Panduan Regex", callback_data=f"rgxpanel_{chat_id}")],
    ]
    return text, InlineKeyboardMarkup(keyboard_rows)


# ─────────────────────────────────────────────────────────────────────────────
#  Halaman — CAS Panel
# ─────────────────────────────────────────────────────────────────────────────
async def page_whitelist_text(chat_id: int) -> str:
    ids = [str(doc["user_id"]) async for doc in whitelist_col.find({"chat_id": chat_id})]
    if not ids:
        return (
            "🛡️ <b>WHITELIST CAS</b>\n"
            f"<code>Grup: {chat_id}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📭 <b>Daftar pengecualian masih kosong.</b>\n\n"
            "<i>User di whitelist kebal terhadap ban otomatis CAS,\n"
            "meskipun namanya ada di database global.</i>"
        )
    lines = "\n".join(f"  ◈ <code>{i}</code>" for i in ids)
    return (
        f"🛡️ <b>WHITELIST CAS</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ <b>Total dikecualikan:</b> <code>{len(ids)} user</code>\n\n"
        f"{lines}\n\n"
        f"<i>User-user di atas terbebas dari deteksi CAS.</i>"
    )


async def page_cas_panel(chat_id: int):
    ids      = [str(doc["user_id"]) async for doc in whitelist_col.find({"chat_id": chat_id})]
    wl_count = len(ids)
    text = (
        f"🛡️ <b>CAS PROTECTION</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>CAS (Combot Anti-SPAM)</b> adalah database global berisi\n"
        f"200.000+ akun spammer terverifikasi dari seluruh Telegram.\n\n"
        f"Saat user baru masuk → bot langsung cek database.\n"
        f"Jika terdeteksi → <b>auto-ban otomatis</b>.\n\n"
        f"<b>📋 WHITELIST CAS:</b>\n"
        f"User di whitelist akan <b>kebal</b> dari ban CAS meskipun\n"
        f"namanya tercatat di database global.\n\n"
        f"⚡ <b>Total whitelist:</b> <code>{wl_count} user</code>\n\n"
        f"<i>Pilih operasi di bawah ini.</i>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅  Tambah Whitelist CAS",   callback_data=f"wl_cas_{chat_id}")],
        [InlineKeyboardButton("❌  Hapus Whitelist CAS",    callback_data=f"unwl_cas_{chat_id}")],
        [InlineKeyboardButton("📋  Lihat Daftar Whitelist", callback_data=f"view_wl_{chat_id}")],
        [InlineKeyboardButton("🔙  Kembali ke Panel Grup",  callback_data=f"manage_{chat_id}")],
    ])
    return text, keyboard


# ─────────────────────────────────────────────────────────────────────────────
#  Halaman — Free/VIP User List
# ─────────────────────────────────────────────────────────────────────────────
async def page_free_list(chat_id: int):
    docs = [doc async for doc in free_col.find({"chat_id": chat_id})]
    if docs:
        lines = "\n".join(f"  ◈ <code>{doc['user_id']}</code>" for doc in docs)
        body = (
            f"⚡ <b>Total:</b> <code>{len(docs)} user</code>\n\n"
            f"{lines}\n\n"
            f"<i>User ini bebas dari semua filter bot di grup ini.</i>"
        )
        del_buttons = [
            [InlineKeyboardButton(
                f"🗑  Unvip: {doc['user_id']}",
                callback_data=f"freedel_{chat_id}_{i}"
            )]
            for i, doc in enumerate(docs)
        ]
    else:
        body = (
            "📭 <b>Belum ada Member VIP.</b>\n\n"
            "<i>Tambahkan user trusted dengan tombol di bawah.</i>"
        )
        del_buttons = []

    text = (
        f"👑 <b>MEMBER VIP</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{body}\n"
    )
    keyboard_rows = del_buttons + [
        [InlineKeyboardButton("➕  Tambah Member VIP",     callback_data=f"freeadd_{chat_id}")],
        [InlineKeyboardButton("🔙  Kembali ke Panel Grup", callback_data=f"manage_{chat_id}")],
    ]
    return text, InlineKeyboardMarkup(keyboard_rows)


# ─────────────────────────────────────────────────────────────────────────────
#  Halaman — Log Aktivitas Per Grup
# FIXED: Return HTML murni — cb_grp_log sekarang pakai safe_edit (bukan edit_with_bq)
# Ini memperbaiki crash collapsed=True di Pyrogram 2.0.106
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_ts(ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(ts, tz=_TZ_WIB)
        return dt.strftime("%H:%M · %d %b %Y")
    except Exception:
        return "—"


async def page_group_log(chat_id: int, page: int = 1):
    """
    Return (text_html, keyboard).
    FIXED: Menggunakan HTML biasa dengan <blockquote> standar.
    Tidak ada lagi marker [BQ] — tidak perlu edit_with_bq (yang crash karena collapsed=True).
    """
    PER_PAGE = 10
    docs, total = await get_group_action_log_page(chat_id, page, PER_PAGE)

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page        = max(1, min(page, total_pages))

    if not docs:
        text = (
            "📋 <b>LOG AKTIVITAS</b>\n"
            f"<code>Grup: {chat_id}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📭 <b>Belum ada aktivitas tercatat.</b>\n\n"
            "Log muncul saat bot menghapus pesan, mute, atau ban user.\n"
            "<i>Log tersimpan selama 7 hari.</i>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙  Kembali ke Panel Grup", callback_data=f"manage_{chat_id}")],
        ])
        return text, keyboard

    _ICON = {"HAPUS": "🗑", "MUTE": "🔇", "BAN": "⛔", "KICK-VC": "🎤", "SECOS": "🔐"}

    entries = []
    for d in docs:
        icon   = _ICON.get(d.get("aksi", ""), "▸")
        aksi   = d.get("aksi", "?")
        alasan = d.get("alasan", "—")
        nama   = d.get("user_name", "?")
        uid    = d.get("user_id", "?")
        ts_str = _fmt_ts(d.get("ts", 0))
        konten = d.get("konten", "").strip()

        inner = f"👤 {nama} ({uid})\n📌 {alasan}"
        if konten:
            inner += f"\n📨 {konten[:80]}"

        entry = (
            f"{icon} <b>{aksi}</b> · {ts_str}\n"
            f"<blockquote>{inner}</blockquote>"
        )
        entries.append(entry)

    body = "\n\n".join(entries)

    text = (
        "📋 <b>LOG AKTIVITAS</b>\n"
        f"<code>Grup: {chat_id}  ·  Hal {page}/{total_pages}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{body}\n\n"
        f"<i>Menampilkan {len(docs)} dari {total} log (7 hari terakhir).</i>"
    )

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⏪ Sebelumnya", callback_data=f"grp_log_{chat_id}_{page - 1}"))
    nav.append(InlineKeyboardButton(f"· {page}/{total_pages} ·", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Berikutnya ⏩", callback_data=f"grp_log_{chat_id}_{page + 1}"))

    keyboard = InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton("🔙  Kembali ke Panel Grup", callback_data=f"manage_{chat_id}")],
    ])
    return text, keyboard


# ─────────────────────────────────────────────────────────────────────────────
#  Halaman — Security OS Panel
# ─────────────────────────────────────────────────────────────────────────────

async def page_security_os(chat_id: int, client=None):
    """
    Halaman panel Security OS.
    Menampilkan status 3 syarat aktivasi dan tombol aktifkan/nonaktifkan.

    client: opsional — jika diberikan, akan cek keanggotaan bot pemantau di grup
            secara real-time. Jika None, hanya cek status dari DB.
    """
    from video_call import (
        security_os_get_status, is_userbot_ready,
        check_monitor_is_member, _monitor_username_cache,
    )

    sec_doc   = await security_os_get_status(chat_id)
    enabled   = sec_doc.get("enabled", False)
    mon_id    = sec_doc.get("monitor_bot_id", 0)
    has_mon   = bool(mon_id)
    ub_ready  = is_userbot_ready()

    # Cek keanggotaan bot pemantau di grup (real-time jika client tersedia)
    if has_mon and client:
        mon_in_group = await check_monitor_is_member(client, chat_id)
    else:
        mon_in_group = False   # tidak bisa cek tanpa client

    mon_uname = _monitor_username_cache.get(mon_id, f"id:{mon_id}") if mon_id else "—"

    # ── Status label per syarat ───────────────────────────────────────────────
    flag    = "🟢 AKTIF" if enabled else "🔴 NONAKTIF"
    ub_st   = "✅ Online" if ub_ready else "❌ Offline — set USERBOT_PHONE di .env"

    if not has_mon:
        mon_st = "❌ Belum dibuat — tekan Pasang Bot Pemantau"
    elif not mon_in_group:
        mon_st = f"⚠️ @{mon_uname} belum join grup"
    else:
        mon_st = f"✅ @{mon_uname} sudah di grup"

    # Semua syarat terpenuhi?
    all_ready = ub_ready and has_mon and mon_in_group

    text = (
        f"🔐 <b>SECURITY OS</b>\n"
        f"<code>Grup: {chat_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>📌 APA INI?</b>\n"
        f"Security OS mengawasi <b>obrolan suara (voice chat)</b> grup.\n"
        f"Saat user naik ke obrolan suara, bot pemantau memeriksa bio profil mereka.\n"
        f"Jika bio mengandung link → user <b>diturunkan otomatis</b> dan mendapat peringatan.\n\n"
        f"<b>📊 STATUS SYARAT AKTIVASI:</b>\n"
        f"  {'✅' if ub_ready else '❌'} Userbot      : <code>{ub_st}</code>\n"
        f"  {'✅' if has_mon else '❌'} Bot Pemantau : <code>{mon_st}</code>\n"
        f"  {'✅' if (has_mon and mon_in_group) else '❌'} Di Grup      : "
        f"<code>{'✅ Sudah jadi anggota' if (has_mon and mon_in_group) else '❌ Belum join — tambahkan manual'}</code>\n\n"
        f"<b>🔐 Security OS  : <code>{flag}</code></b>\n\n"
        f"<b>⚙️ CARA KERJA SINGKAT:</b>\n"
        f"◈ Tiap grup punya bot pemantau <b>masing-masing</b>.\n"
        f"◈ Bot pemantau hanya menjawab di grupnya sendiri.\n"
        f"◈ Jika user mempriv bio untuk grup tertentu → bot pemantau di grup itu\n"
        f"   tidak akan melihat link → user <b>tidak diturunkan</b> di grup tersebut.\n"
        f"◈ 1 bot pemantau hanya boleh dipakai di <b>1 grup</b>.\n"
    )

    buttons = []

    # Tombol pasang/ganti bot pemantau
    label_mon = "🔄  Ganti Bot Pemantau" if has_mon else "🤖  Pasang Bot Pemantau"
    buttons.append([
        InlineKeyboardButton(label_mon, callback_data=f"secos_setmon_{chat_id}")
    ])

    # Tombol aktifkan / nonaktifkan
    if enabled:
        buttons.append([
            InlineKeyboardButton("🔴  Nonaktifkan Security OS", callback_data=f"secos_off_{chat_id}")
        ])
    else:
        # Tombol aktifkan selalu ditampilkan; validasi syarat dilakukan saat diklik
        lbl = "🟢  Aktifkan Security OS" if all_ready else "🟢  Aktifkan (cek syarat dulu)"
        buttons.append([
            InlineKeyboardButton(lbl, callback_data=f"secos_on_{chat_id}")
        ])

    buttons.append([
        InlineKeyboardButton("🔙  Kembali ke Panel Grup", callback_data=f"manage_{chat_id}")
    ])

    keyboard = InlineKeyboardMarkup(buttons)
    return text, keyboard
