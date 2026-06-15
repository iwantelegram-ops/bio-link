"""
video_call.py — Userbot Security OS
════════════════════════════════════════════════════════════════════════════════
Modul userbot Pyrogram yang berjalan berdampingan dengan bot biasa (antigcast.py).

ARSITEKTUR (Database-driven — tidak ada komunikasi di grup):
  ┌─────────────────────────────────────────────────────────────┐
  │  Bot Pemantau (monitor_bot_reference.py)                    │
  │  Scan semua member → simpan bio_profiles ke DB bersama      │
  └────────────────────────┬────────────────────────────────────┘
                           │ DB bersama (MONGO_URL / SQLite sama)
           ┌───────────────┴───────────────────────┐
           ▼                                       ▼
  ┌────────────────┐                    ┌──────────────────────┐
  │   Bot Utama    │  query bio_profiles│      Userbot (ini)   │
  │  (pesan grup)  │  → hapus jika link │  (obrolan suara/VC)  │
  └────────────────┘                    └──────────────────────┘
                                               │ kick dari VC
                                               ↓ (jika has_link)

ATURAN UTAMA:
  - Userbot TIDAK mengirim /checkbio ke grup — query DB langsung.
  - Bot pemantau mengisi bio_profiles secara berkala & saat user join.
  - Userbot hanya memantau obrolan SUARA — pesan/typing ditangani bot biasa.
  - Semua data disimpan ke DB (MongoDB/SQLite) via db[] seperti bot asli.
  - Logika penyimpanan asli tidak diubah sama sekali.

FLOW STARTUP:
  1. antigcast.py start → bot biasa aktif
  2. start_userbot(app) dipanggil → cek session userbot
  3a. Session ada → userbot langsung aktif
  3b. Session tidak ada → bot masuk mode tunggu (log di console),
      owner kirim /otp <kode> ke bot via DM → userbot login → session disimpan

VARIABEL .env BARU:
  USERBOT_PHONE — nomor HP akun userbot (format: +62xxx)
                  Jika kosong → Security OS tidak tersedia, bot berjalan normal.
"""

from __future__ import annotations

import os
import asyncio
import time
import re as _re
from pathlib import Path as _Path

from pyrogram import Client as _Client, filters as _filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message as _Message, ChatMemberUpdated as _ChatMemberUpdated
from pyrogram.errors import (
    FloodWait,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    SessionPasswordNeeded,
    PhoneNumberInvalid,
    UserAlreadyParticipant,
)
from dotenv import load_dotenv

load_dotenv(dotenv_path=_Path(__file__).parent / ".env", override=False)

# ── Env ───────────────────────────────────────────────────────────────────────
API_ID        = int(os.environ.get("API_ID", 0))
API_HASH      = os.environ.get("API_HASH", "")
OWNER_ID      = int(os.environ.get("OWNER_ID", 0))
USERBOT_PHONE = os.environ.get("USERBOT_PHONE", "").strip()

_BOT_DIR    = _Path(__file__).resolve().parent
_UB_SESSION = str(_BOT_DIR / "userbot_security_os")

# ── State global ──────────────────────────────────────────────────────────────
userbot: _Client | None = None   # instance userbot Pyrogram
_bot_ref: _Client | None = None  # referensi bot biasa (untuk kirim peringatan)
_ub_ready: bool = False
_ub_self_id: int = 0             # user_id akun userbot agar tidak kick diri sendiri

# ── OTP flow state ────────────────────────────────────────────────────────────
_otp_event: asyncio.Event | None = None
_otp_value: str = ""

# ── Rate limit per grup — minimum jeda antar pengecekan ──────────────────────
_last_vc_check: dict[int, float] = {}
_VC_CHECK_INTERVAL = 15.0   # detik minimum antar scan VC per grup

# ── Pelacak user yang sedang diproses (hindari double-kick) ──────────────────
_processing_kick: set[tuple[int, int]] = set()   # {(chat_id, user_id)}

# ── Cache bio per user per grup (TTL 10 menit) ───────────────────────────────
# Key: (chat_id, user_id) — cache TIDAK pernah dipakai lintas grup.
_bio_cache: dict[tuple[int, int], tuple[bool, float]] = {}
_BIO_CACHE_TTL = 600.0

# ── Penanda pesan jawaban bot pemantau ───────────────────────────────────────
_pending_checks: dict[tuple[int, int], int] = {}

# ── Mapping call_id → chat_id untuk UpdateGroupCallParticipants ──────────────
# Dideklarasikan di sini (global) agar _on_vc_update bisa mengaksesnya.
_call_id_to_chat: dict[int, int] = {}

# ── Global semaphore — batasi concurrent /checkbio ke seluruh Telegram API ───
# Maks 3 query paralel di seluruh sistem (lintas semua grup).
# Diinisialisasi lazy di start_userbot().
_api_semaphore: asyncio.Semaphore | None = None
_API_CONCURRENCY = 3   # konservatif: 3 checkbio parallel max

# ── Per-grup semaphore — batasi checkbio berurutan per grup ──────────────────
# Setiap grup punya semaphore sendiri: maks 1 /checkbio berjalan di waktu yg sama
# per grup. Ini agar bot pemantau di grup A tidak dibanjiri pertanyaan serentak.
_group_semaphores: dict[int, asyncio.Semaphore] = {}

def _get_group_semaphore(chat_id: int) -> asyncio.Semaphore:
    """1 slot per grup — /checkbio diproses satu per satu per grup."""
    if chat_id not in _group_semaphores:
        _group_semaphores[chat_id] = asyncio.Semaphore(1)
    return _group_semaphores[chat_id]

# ── Per-grup antrean notifikasi (warn) ───────────────────────────────────────
# Notifikasi kick dikumpulkan per grup, lalu dikirim dengan jeda.
# Mencegah bot utama mengirim 10 pesan beruntun ke grup dalam 1 detik.
_warn_queues: dict[int, asyncio.Queue] = {}
_warn_workers: dict[int, asyncio.Task] = {}

# Jeda minimum antar pesan warn dalam 1 grup (detik)
_WARN_INTERVAL = 2.5

def _get_warn_queue(chat_id: int) -> asyncio.Queue:
    """Dapatkan / buat antrean warn untuk grup ini."""
    if chat_id not in _warn_queues:
        _warn_queues[chat_id] = asyncio.Queue()
    return _warn_queues[chat_id]

async def _warn_worker(chat_id: int) -> None:
    """
    Worker per-grup: ambil user_id dari antrean, kirim peringatan, tunggu jeda.
    Berjalan sampai antrean kosong, lalu berhenti (worker-on-demand).
    """
    q = _get_warn_queue(chat_id)
    while True:
        try:
            user_id = q.get_nowait()
        except asyncio.QueueEmpty:
            break
        try:
            await _do_send_warning(chat_id, user_id)
        except Exception as e:
            print(f"[UB-Warn] Worker error uid={user_id} grup={chat_id}: {e}")
        q.task_done()
        if not q.empty():
            await asyncio.sleep(_WARN_INTERVAL)
    # Worker selesai — hapus referensi agar bisa dibuat ulang
    _warn_workers.pop(chat_id, None)

def _enqueue_warning(chat_id: int, user_id: int) -> None:
    """Masukkan user_id ke antrean warn grup. Spawn worker jika belum ada."""
    q = _get_warn_queue(chat_id)
    q.put_nowait(user_id)
    # Spawn worker hanya jika tidak ada yang berjalan
    existing = _warn_workers.get(chat_id)
    if existing is None or existing.done():
        task = asyncio.create_task(_warn_worker(chat_id))
        _warn_workers[chat_id] = task

# ── Throttle scan grup aktif — cegah spawn task tak terbatas ─────────────────
# Maks grup yang di-scan paralel per siklus monitor (10 detik).
_MAX_PARALLEL_GROUP_SCANS = 4


def _get_api_semaphore() -> asyncio.Semaphore:
    """Lazy-init semaphore di dalam event loop yang aktif."""
    global _api_semaphore
    if _api_semaphore is None:
        _api_semaphore = asyncio.Semaphore(_API_CONCURRENCY)
    return _api_semaphore


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE HELPERS — pakai db[] dari database.py (logika asli TIDAK diubah)
# ══════════════════════════════════════════════════════════════════════════════

def _get_db():
    """Lazy import untuk menghindari circular import saat modul pertama di-load."""
    from database import db, save_bot_config, get_bot_config
    return db, save_bot_config, get_bot_config


async def _sec_os_get(chat_id: int) -> dict:
    """
    Ambil dokumen Security OS untuk satu grup dari DB.

    Schema:
      chat_id        : int   — ID grup Telegram
      enabled        : bool  — apakah Security OS aktif untuk grup ini
      monitor_token  : str   — token bot pemantau (disimpan di DB)
      monitor_bot_id : int   — user_id Telegram bot pemantau
      monitor_chat   : int   — chat_id grup (sama dengan chat_id, redundan tapi eksplisit)
    """
    db, _, _ = _get_db()
    doc = await db["security_os"].find_one({"chat_id": chat_id})
    if doc is None:
        doc = {
            "chat_id":        chat_id,
            "enabled":        False,
            "monitor_token":  "",
            "monitor_bot_id": 0,
            "monitor_chat":   chat_id,
        }
    return doc


async def _sec_os_save(doc: dict) -> None:
    db, _, _ = _get_db()
    # Exclude _id dari $set — MongoDB tidak izinkan update field immutable _id
    payload = {k: v for k, v in doc.items() if k != "_id"}
    await db["security_os"].update_one(
        {"chat_id": doc["chat_id"]},
        {"$set": payload},
        upsert=True,
    )


async def _sec_os_set_enabled(chat_id: int, enabled: bool) -> None:
    doc = await _sec_os_get(chat_id)
    doc["enabled"] = enabled
    await _sec_os_save(doc)


async def _sec_os_set_monitor(chat_id: int, token: str, bot_id: int) -> None:
    doc = await _sec_os_get(chat_id)
    doc["monitor_token"]  = token
    doc["monitor_bot_id"] = bot_id
    doc["monitor_chat"]   = chat_id
    await _sec_os_save(doc)


# ── Session userbot ke/dari MongoDB ──────────────────────────────────────────

async def _save_ub_session() -> None:
    """Simpan .session userbot ke MongoDB (sama polanya dengan bot biasa)."""
    import base64
    _, save_bot_config, _ = _get_db()
    try:
        from database import get_active_backend
        if get_active_backend() != "mongo":
            return
        path = _UB_SESSION + ".session"
        if not _Path(path).exists():
            return
        with open(path, "rb") as f:
            raw = f.read()
        await save_bot_config("ub_session_data", base64.b64encode(raw).decode())
        print("[UB] ✅ Session userbot disimpan ke MongoDB.")
    except Exception as e:
        print(f"[UB] ⚠️  Gagal simpan session ke MongoDB: {e}")


async def _restore_ub_session() -> bool:
    """Pulihkan .session userbot dari MongoDB jika file lokal tidak ada."""
    import base64
    _, _, get_bot_config = _get_db()
    try:
        from database import get_active_backend
        if get_active_backend() != "mongo":
            return False
        path = _UB_SESSION + ".session"
        if _Path(path).exists():
            return False
        saved = await get_bot_config("ub_session_data")
        if not saved:
            return False
        with open(path, "wb") as f:
            f.write(base64.b64decode(saved.encode()))
        print("[UB] ✅ Session userbot dipulihkan dari MongoDB.")
        return True
    except Exception as e:
        print(f"[UB] ⚠️  Gagal pulihkan session: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# OTP LOGIN FLOW
# Saat session belum ada:
#   bot biasa → kirim instruksi ke OWNER_ID
#   owner → balas OTP
#   bot biasa → teruskan ke receive_otp_from_bot()
#   userbot → login dengan OTP
# ══════════════════════════════════════════════════════════════════════════════

def receive_otp_from_bot(text: str) -> None:
    """Dipanggil dari handler bot biasa saat owner membalas OTP/2FA."""
    global _otp_value
    _otp_value = text.strip()
    if _otp_event and not _otp_event.is_set():
        _otp_event.set()


def register_otp_handler(bot: _Client) -> None:
    """
    Pasang handler di bot biasa untuk menangkap OTP dari owner.
    Owner harus mengirim perintah: /otp <kode>
    Handler ini HANYA aktif saat _otp_event belum di-set (sedang menunggu OTP).
    Menggunakan group=99 agar tidak bentrok dengan handler asli bot.
    """

    @bot.on_message(
        _filters.private & _filters.user(OWNER_ID) & _filters.text,
        group=99,
    )
    async def _catch_otp(_client: _Client, msg: _Message):
        txt = (msg.text or "").strip()

        # Tangkap format /otp <kode> dari owner
        if txt.lower().startswith("/otp "):
            otp_code = txt[5:].strip()
            if otp_code:
                if _otp_event and not _otp_event.is_set():
                    # Sedang menunggu OTP -> teruskan ke login flow
                    receive_otp_from_bot(otp_code)
                    await msg.reply(
                        f"\u2705 <b>OTP diterima:</b> <code>{otp_code}</code>\n"
                        "Mencoba login userbot...",
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await msg.reply(
                        "\u26a0\ufe0f Bot tidak sedang menunggu OTP. "
                        "Pastikan userbot belum login atau restart bot terlebih dahulu.",
                        parse_mode=ParseMode.HTML,
                    )
            else:
                await msg.reply(
                    "\u274c Format salah. Gunakan: <code>/otp 12345</code>",
                    parse_mode=ParseMode.HTML,
                )


async def _prompt_owner(bot: _Client, html_msg: str) -> str:
    """
    Tunggu OTP dari owner (maks 10 menit).
    Owner harus mengirim /otp <kode> ke bot ini secara DM.
    Return teks OTP, atau "" jika timeout.
    """
    global _otp_event, _otp_value
    _otp_event = asyncio.Event()
    _otp_value = ""

    # Log ke console — owner harus kirim /otp sendiri ke bot
    print("[UB-OTP] Menunggu owner kirim OTP via DM bot dengan format: /otp <kode>")

    try:
        await asyncio.wait_for(_otp_event.wait(), timeout=600.0)
        return _otp_value
    except asyncio.TimeoutError:
        print("[UB-OTP] Timeout menunggu OTP dari owner (10 menit). Restart bot untuk mencoba lagi.")
        return ""


async def _do_login(bot: _Client) -> bool:
    """
    Login userbot dengan flow OTP interaktif.
    Owner harus mengirim /otp <kode> ke bot ini via DM.
    Return True jika berhasil, False jika gagal/timeout.
    """
    global userbot

    if not USERBOT_PHONE:
        print("[UB] ⚠️  USERBOT_PHONE tidak diset — Security OS tidak tersedia.")
        return False

    print("[UB] 🔄 Session userbot belum ada. Meminta kode OTP ke Telegram...")
    print(f"[UB] 📱 Nomor: {USERBOT_PHONE}")
    print("[UB] ⏳ Kirim OTP via DM bot dengan format: /otp <kode>")

    # Buat client userbot (mode user, bukan bot)
    ub = _Client(_UB_SESSION, api_id=API_ID, api_hash=API_HASH)

    try:
        await ub.connect()
    except Exception as e:
        print(f"[UB] Gagal connect: {e}")
        return False

    # Minta kode OTP ke Telegram
    try:
        sent = await ub.send_code(USERBOT_PHONE)
    except PhoneNumberInvalid:
        print(f"[UB] \u274c USERBOT_PHONE tidak valid: '{USERBOT_PHONE}' — periksa format di .env (contoh: +628123456789)")
        await ub.disconnect()
        return False
    except FloodWait as fw:
        print(f"[UB] FloodWait {fw.value}s saat send_code.")
        await asyncio.sleep(fw.value)
        await ub.disconnect()
        return False
    except Exception as e:
        print(f"[UB] Gagal send_code: {e}")
        await ub.disconnect()
        return False

    # Tampilkan petunjuk di console — owner harus kirim /otp sendiri ke bot
    phone_hint = (
        USERBOT_PHONE[:3] + "****" + USERBOT_PHONE[-3:]
        if len(USERBOT_PHONE) > 6 else "****"
    )
    print(f"[UB-OTP] \U0001f510 OTP Telegram dikirim ke {phone_hint}")
    print("[UB-OTP] Kirim OTP ke bot via DM dengan format: /otp <kode>")
    print("[UB-OTP] Menunggu owner kirim OTP... (timeout 10 menit)")
    otp = await _prompt_owner(bot, "")

    if not otp:
        await ub.disconnect()
        return False

    # Sign in dengan OTP
    try:
        await ub.sign_in(USERBOT_PHONE, sent.phone_code_hash, otp)

    except PhoneCodeInvalid:
        print("[UB-OTP] \u274c OTP salah. Restart bot untuk mencoba lagi.")
        await ub.disconnect()
        return False

    except PhoneCodeExpired:
        print("[UB-OTP] \u274c OTP sudah kadaluarsa. Restart bot untuk mencoba lagi.")
        await ub.disconnect()
        return False

    except SessionPasswordNeeded:
        # Akun menggunakan 2FA
        print("[UB-OTP] \U0001f511 Akun menggunakan 2FA. Kirim password via DM bot: /otp <password>")
        print("[UB-OTP] Menunggu password 2FA dari owner... (timeout 10 menit)")
        pw = await _prompt_owner(bot, "")
        if not pw:
            await ub.disconnect()
            return False
        try:
            await ub.check_password(pw)
        except Exception as e2:
            print(f"[UB-OTP] \u274c Password 2FA salah: {e2} — Restart bot untuk mencoba lagi.")
            await ub.disconnect()
            return False

    except Exception as e:
        print(f"[UB] Gagal sign_in: {e}")
        await ub.disconnect()
        return False

    # Login berhasil — userbot sudah connected via connect()+sign_in()
    # JANGAN panggil start() lagi, karena client sudah connected
    userbot = ub
    await _save_ub_session()

    try:
        me = await ub.get_me()
        _ub_self_id_val = me.id
        print(f"[UB] \u2705 Userbot Security OS berhasil login! Akun: {me.first_name} (id={me.id})")
        print("[UB] \U0001f6e1\ufe0f Security OS siap dikonfigurasi di panel grup.")
        return True, _ub_self_id_val
    except Exception as e:
        print(f"[UB] ⚠️  Login berhasil tapi gagal get_me: {e}")
        return True, 0


# ══════════════════════════════════════════════════════════════════════════════
# USERBOT — START & STOP
# ══════════════════════════════════════════════════════════════════════════════

async def start_userbot(bot: _Client) -> None:
    """
    Entry point dipanggil dari antigcast.py setelah bot biasa aktif.
    Non-blocking — langsung return setelah create_task background loop.
    """
    global userbot, _bot_ref, _ub_ready, _ub_self_id
    _bot_ref = bot

    # Inisialisasi semaphore di dalam event loop yang aktif
    _get_api_semaphore()

    # Pasang OTP handler di bot biasa (sebelum apapun)
    register_otp_handler(bot)

    # Pasang handler auto-kenali bot pemantau saat masuk grup
    register_monitor_join_handler(bot)

    # Coba pulihkan session dari MongoDB (setelah Railway redeploy)
    await _restore_ub_session()

    session_file = _UB_SESSION + ".session"

    if _Path(session_file).exists():
        # Session tersedia — coba langsung start
        try:
            ub = _Client(_UB_SESSION, api_id=API_ID, api_hash=API_HASH)
            await ub.start()
            me = await ub.get_me()
            userbot    = ub
            _ub_self_id = me.id
            _ub_ready  = True
            print(f"[UB] ✅ Userbot aktif: {me.first_name} (id={me.id})")
            await _save_ub_session()
            # Log berapa grup Security OS yang sudah terdaftar di DB
            await _log_registered_groups()
            # Jalankan loop monitor voice chat di background
            asyncio.create_task(_voice_chat_monitor_loop())
            return
        except Exception as e:
            print(f"[UB] ⚠️  Session ada tapi gagal start ({type(e).__name__}): {e}")
            # Hapus session rusak agar bisa login ulang
            try:
                _Path(session_file).unlink(missing_ok=True)
            except Exception:
                pass

    # Tidak ada session / session rusak
    if not USERBOT_PHONE:
        print("[UB] ℹ️  USERBOT_PHONE tidak diset — Security OS tidak tersedia.")
        return

    print("[UB] ℹ️  Session userbot tidak ada → mulai OTP login flow...")
    result = await _do_login(bot)

    # _do_login sekarang return (ok, self_id) — userbot sudah connected, JANGAN start() lagi
    if isinstance(result, tuple):
        ok, self_id = result
    else:
        ok, self_id = result, 0

    if ok and userbot:
        try:
            # Userbot sudah connected via connect()+sign_in() — set state langsung
            _ub_self_id = self_id
            _ub_ready   = True
            await _log_registered_groups()
            asyncio.create_task(_voice_chat_monitor_loop())
        except Exception as e:
            print(f"[UB] Gagal aktivasi setelah login: {e}")
    else:
        print("[UB] ❌ Login userbot gagal — Security OS tidak aktif.")


async def stop_userbot() -> None:
    """Hentikan userbot dengan bersih. Dipanggil dari graceful_shutdown()."""
    global userbot, _ub_ready
    _ub_ready = False
    if userbot:
        try:
            await userbot.stop()
            print("[UB] ✅ Userbot berhenti dengan bersih.")
        except Exception as e:
            print(f"[UB] stop error: {e}")
        userbot = None


# ══════════════════════════════════════════════════════════════════════════════
# VOICE CHAT MONITOR LOOP
# Polling ringan per-grup, hanya mengamati obrolan SUARA.
# Pesan/typing tetap sepenuhnya di tangan bot biasa (tidak disentuh).
# ══════════════════════════════════════════════════════════════════════════════


async def _log_registered_groups() -> None:
    """
    Saat startup, log berapa grup Security OS yang sudah tersimpan di MongoDB,
    lalu lakukan warm-up BERTAHAP (staggered) — resolve peer setiap grup dengan
    jeda kecil agar userbot tidak memicu FloodWait karena mengakses
    banyak grup sekaligus saat redeploy.
    """
    db, _, _ = _get_db()
    try:
        total  = await db["security_os"].count_documents({})
        active = await db["security_os"].count_documents({"enabled": True})
        print(
            f"[UB] 📋 Security OS DB: {total} grup terdaftar, "
            f"{active} aktif — semua dikenali otomatis dari MongoDB."
        )
    except Exception as e:
        print(f"[UB] ⚠️  Tidak bisa baca hitungan grup dari DB: {e}")
        return

    # ── Warm-up bertahap: resolve peer setiap grup dengan jeda ───────────────
    # Mencegah userbot "hadir" di banyak grup sekaligus saat redeploy,
    # yang bisa memicu FloodWait atau deteksi anomali Telegram.
    _STARTUP_STAGGER = 3.0   # detik jeda antar grup
    try:
        docs = await db["security_os"].find({}, {"chat_id": 1}).to_list(None)
    except Exception:
        return

    if not docs:
        return

    print(f"[UB] ⏳ Startup stagger: warm-up {len(docs)} grup "
          f"(jeda {_STARTUP_STAGGER}s per grup)...")
    for i, doc in enumerate(docs):
        if not userbot or not _ub_ready:
            break
        chat_id = doc.get("chat_id")
        if not chat_id:
            continue
        try:
            await userbot.resolve_peer(chat_id)
        except FloodWait as fw:
            print(f"[UB-Startup] FloodWait {fw.value}s saat resolve grup {chat_id} — menunggu...")
            await asyncio.sleep(fw.value + 1)
        except Exception:
            pass   # Grup mungkin dihapus/userbot tidak ada — lewati
        if i < len(docs) - 1:
            await asyncio.sleep(_STARTUP_STAGGER)

    print("[UB] ✅ Startup stagger selesai — userbot siap.")

async def _voice_chat_monitor_loop() -> None:
    """
    Background task — pasang handler raw update untuk menangkap
    UpdateGroupCallParticipants secara event-driven.

    ── CARA KERJA: MENURUNKAN USER BIO-LINK DARI OBROLAN SUARA ─────────────
    1. Userbot menjadi member grup (bukan peserta VC).
    2. Telegram API secara otomatis mengirim UpdateGroupCallParticipants
       ke semua member grup setiap ada user yang JOIN obrolan suara/video.
       ➜ Ini adalah perilaku resmi Telegram API — tidak memerlukan join VC.
    3. Setiap user yang join VC dicek: apakah bio-nya mengandung link?
       • Cek cache in-memory dulu (TTL 10 menit).
       • Jika tidak ada cache → query bio_profiles di DB (diisi bot pemantau).
    4. Jika has_link=True → userbot memanggil phone.EditGroupCallParticipant
       (muted=True, video_stopped=True) → user diturunkan dari obrolan suara.
    5. Bot biasa mengirim peringatan di grup lalu menghapus pesan setelah 10 detik.

    ── PEMANTAUAN VIDEO CALL TANPA JOIN VC ──────────────────────────────────
    Userbot TIDAK perlu join/masuk ke obrolan suara atau video call grup.
    Cukup jadi member grup biasa — Telegram tetap mengirimkan raw update
    UpdateGroupCallParticipants untuk seluruh member grup tersebut.
    Ini sesuai dengan aturan Telegram API (MTProto):
      • UpdateGroupCallParticipants dikirim ke semua subscriber channel/supergroup,
        bukan hanya peserta aktif VC.
      • phone.EditGroupCallParticipant dapat dipanggil oleh admin/moderator
        yang memiliki izin "Kelola Obrolan Video" tanpa harus berada di dalam VC.
    Dengan demikian admin bisa memantau siapa yang masuk/keluar video call
    grup tanpa ikut join ke obrolan suara tersebut.
    """
    print("[UB] \U0001f3a4 Voice chat monitor dimulai (event-driven).")

    if not userbot:
        return

    @userbot.on_raw_update()
    async def _on_vc_update(client, update, users, chats):
        if not _ub_ready:
            return
        try:
            from pyrogram.raw.types import (
                UpdateGroupCallParticipants,
                UpdateGroupCall,
                GroupCallParticipant,
            )
        except ImportError:
            return

        # ── Tangkap voice chat baru dimulai → daftarkan call_id ──────────────
        if isinstance(update, UpdateGroupCall):
            chat_id_raw = getattr(update, "chat_id", None)
            if chat_id_raw:
                # Telegram kirim chat_id sebagai angka positif untuk channel/supergroup
                chat_id_neg = int(f"-100{chat_id_raw}") if chat_id_raw > 0 else chat_id_raw
                call_obj = getattr(update, "call", None)
                if call_obj:
                    call_id = getattr(call_obj, "id", None)
                    if call_id:
                        # Cek apakah grup ini Security OS aktif
                        sec = await _sec_os_get(chat_id_neg)
                        if sec.get("enabled") and sec.get("monitor_bot_id"):
                            _call_id_to_chat[call_id] = chat_id_neg
                            print(f"[UB-VC] Voice chat dimulai di grup {chat_id_neg} (call_id={call_id})")
            return

        if not isinstance(update, UpdateGroupCallParticipants):
            return

        call_id = update.call.id
        chat_id = _call_id_to_chat.get(call_id)
        if not chat_id:
            return

        sec_doc = await _sec_os_get(chat_id)
        if not sec_doc.get("enabled"):
            return

        # ARSITEKTUR DB-DRIVEN: monitor_bot_id tidak wajib untuk query bio.
        # Userbot langsung baca collection bio_profiles yang diisi bot pemantau.
        # Catatan: Security OS tetap membutuhkan bot pemantau untuk mengisi DB,
        # tapi userbot tidak perlu tahu monitor_bot_id untuk cek bio.
        monitor_id = sec_doc.get("monitor_bot_id", 0)  # dipertahankan untuk logging

        for p in update.participants:
            if not isinstance(p, GroupCallParticipant):
                continue
            if getattr(p, "left", False):
                continue  # user keluar — skip

            peer = getattr(p, "peer", None)
            if peer is None:
                continue
            uid = getattr(peer, "user_id", None)
            if not uid or uid == _ub_self_id:
                continue

            key = (chat_id, uid)
            if key in _processing_kick:
                continue

            # Cek in-memory cache dulu (TTL 10 menit)
            cached = _bio_cache.get(key)
            if cached:
                has_link, cache_ts = cached
                if time.monotonic() - cache_ts < _BIO_CACHE_TTL:
                    if has_link:
                        _processing_kick.add(key)
                        asyncio.create_task(
                            _execute_kick(chat_id, uid, update.call)
                        )
                    continue

            # Query DB (bot pemantau sudah mengisi bio_profiles)
            _processing_kick.add(key)
            asyncio.create_task(
                _query_monitor_then_kick(chat_id, uid, monitor_id, update.call)
            )

    # Warmup: isi _call_id_to_chat dari grup Security OS yang sudah punya VC aktif
    await _warmup_active_calls()

    # Jaga task tetap hidup
    while _ub_ready and userbot:
        await asyncio.sleep(30)
    print("[UB] \U0001f507 Voice chat monitor berhenti.")


_MAX_PARALLEL_GROUP_SCANS = 3  # dipertahankan untuk kompatibilitas


async def _warmup_active_calls() -> None:
    """
    Saat startup, cari grup Security OS aktif yang sudah punya voice chat
    berjalan dan isi _call_id_to_chat agar event pertama langsung dikenali.
    """
    if not userbot:
        return
    db, _, _ = _get_db()
    try:
        docs = await db["security_os"].find({"enabled": True}).to_list(None)
    except Exception:
        return

    from pyrogram.raw import functions as _rf
    for doc in docs:
        chat_id = doc.get("chat_id")
        if not chat_id:
            continue
        try:
            chat_peer = await userbot.resolve_peer(chat_id)
            full = await userbot.invoke(_rf.channels.GetFullChannel(channel=chat_peer))
            call_obj = getattr(full.full_chat, "call", None)
            if call_obj:
                _call_id_to_chat[call_obj.id] = chat_id
                print(f"[UB-VC] Warmup: grup {chat_id} punya voice chat aktif (call_id={call_obj.id})")
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
        except Exception:
            pass
        await asyncio.sleep(2)


async def _scan_active_groups() -> None:
    """Stub — arsitektur lama (polling). Tidak dipakai lagi."""
    pass


async def _check_one_group(sec_doc: dict) -> None:
    """Stub — arsitektur lama (polling). Tidak dipakai lagi."""
    pass


# ══════════════════════════════════════════════════════════════════════════════
# KOMUNIKASI USERBOT ↔ BOT PEMANTAU (DI DALAM GRUP)
#
# Mekanisme:
#   1. Userbot mengirim `/checkbio <user_id>` ke bot pemantau DI GRUP ITU SENDIRI
#      via pesan grup (mention bot pemantau agar hanya ia yang merespons)
#   2. Userbot memantau pesan baru di grup, menunggu jawaban dari bot pemantau
#   3. Bot pemantau menjawab: "HAS_LINK" atau "NO_LINK"
#   4. Userbot memproses jawaban
#
# Catatan keamanan:
#   - Pesan /checkbio dikirim sebagai pesan grup biasa (userbot sebagai member).
#   - Bot pemantau HARUS sudah join di grup itu agar bisa menerima & membalas.
#   - Jika bot pemantau tidak ada di grup, tidak ada jawaban → tidak ada eksekusi.
# ══════════════════════════════════════════════════════════════════════════════

async def _query_monitor_then_kick(
    chat_id: int,
    user_id: int,
    monitor_bot_id: int,
    call_input,
) -> None:
    """
    Query hasil bio dari DB (ditulis bot pemantau) → kick jika has_link=True.

    ARSITEKTUR BARU (Database-driven):
      Tidak ada komunikasi ke grup. Userbot langsung query collection
      bio_profiles yang diisi bot pemantau secara berkala.
      Ini instan (< 10ms) dan tidak pernah flood Telegram API.

    Fallback aman: jika data tidak ada di DB → tidak kick.
    """
    try:
        has_link = await _query_bio_from_db(chat_id, user_id)

        # Simpan ke in-memory cache agar event VC berikutnya lebih cepat
        _bio_cache[(chat_id, user_id)] = (
            has_link if has_link is not None else False,
            time.monotonic()
        )

        if has_link:
            await _execute_kick(chat_id, user_id, call_input)
        else:
            _processing_kick.discard((chat_id, user_id))

    except Exception as e:
        print(f"[UB-Query] Error uid={user_id} chat={chat_id}: {e}")
        _processing_kick.discard((chat_id, user_id))


async def _query_bio_from_db(chat_id: int, user_id: int) -> bool | None:
    """
    Baca hasil cek bio dari bio_profiles untuk pasangan (chat_id, user_id).
    Data ini ditulis oleh bot pemantau KHUSUS grup chat_id.

    Return:
      True  → ada link di bio
      False → tidak ada link di bio
      None  → data belum ada (bot pemantau belum scan user ini) → tidak kick
    """
    from monitor_bot_reference import query_bio as _query_bio, force_check_user
    result = await _query_bio(chat_id, user_id)
    if result is None:
        # Data belum ada di DB → paksa cek langsung via MonitorInstance
        print(
            f"[UB-Bio] Data bio chat={chat_id} uid={user_id} "
            "belum ada di DB — force check via MonitorInstance"
        )
        result = await force_check_user(chat_id, user_id)
        if result is None:
            print(
                f"[UB-Bio] chat={chat_id} uid={user_id} "
                "instance tidak aktif — skip kick"
            )
        else:
            print(
                f"[UB-Bio] chat={chat_id} uid={user_id} "
                f"force_check has_link={result}"
            )
    else:
        print(
            f"[UB-Bio] chat={chat_id} uid={user_id} "
            f"has_link={result}"
        )
    return result


# ── _get_monitor_username dipertahankan untuk kebutuhan setup_monitor_bot ─────
# (tidak dipakai lagi untuk checkbio, tapi masih dipakai di panel Security OS)
_monitor_username_cache: dict[int, str] = {}


async def _get_monitor_username(monitor_bot_id: int) -> str:
    """Ambil username bot pemantau (cache di memory). Masih dipakai di panel UI."""
    if monitor_bot_id in _monitor_username_cache:
        return _monitor_username_cache[monitor_bot_id]
    try:
        if userbot:
            user = await userbot.get_users(monitor_bot_id)
            uname = user.username or str(monitor_bot_id)
        else:
            uname = str(monitor_bot_id)
    except Exception:
        uname = str(monitor_bot_id)
    _monitor_username_cache[monitor_bot_id] = uname
    return uname


# ══════════════════════════════════════════════════════════════════════════════
# EKSEKUSI: KICK DARI VOICE CHAT + PERINGATAN
# ══════════════════════════════════════════════════════════════════════════════

async def _execute_kick(chat_id: int, user_id: int, call_input) -> None:
    """
    Turunkan user dari voice chat, lalu antrekan peringatan ke grup.

    Kick (turun dari VC) dilakukan langsung — itu satu API call dan harus cepat.
    Peringatan teks TIDAK langsung dikirim, melainkan dimasukkan ke antrean
    per-grup (_enqueue_warning) yang mengirim dengan jeda _WARN_INTERVAL detik.
    Ini mencegah bot utama mengirim banyak pesan beruntun ke grup dalam waktu
    singkat saat banyak user dikick sekaligus.
    """
    try:
        await _kick_from_voice(chat_id, user_id, call_input)
        # Antrekan notifikasi — tidak langsung kirim
        _enqueue_warning(chat_id, user_id)
    except Exception as e:
        print(f"[UB-Exec] Error saat kick uid={user_id} di grup {chat_id}: {e}")
    finally:
        _processing_kick.discard((chat_id, user_id))


async def _kick_from_voice(chat_id: int, user_id: int, call_input) -> None:
    """
    Turunkan user dari obrolan suara/video call menggunakan raw API Telegram.

    ── ALUR PENURUNAN USER DENGAN BIO-LINK ─────────────────────────────────
    Fungsi ini dipanggil oleh _execute_kick() setelah _query_monitor_then_kick()
    mengkonfirmasi bahwa user memiliki link di bio (has_link=True dari DB).

    Metode API: phone.EditGroupCallParticipant (MTProto)
      • Ini adalah endpoint resmi Telegram untuk memodifikasi peserta VC.
      • Parameter yang diset: muted=True, volume=0, video_stopped=True,
        video_paused=True, presentation_paused=True.
      • Efek: user di-mute dan video/screen-share-nya dihentikan paksa,
        sehingga user secara efektif "diturunkan" dari obrolan suara.
      • Userbot harus punya izin "Kelola Obrolan Video" (manage_video_chats)
        di grup agar API call ini berhasil.
      • Userbot TIDAK perlu berada di dalam VC — cukup jadi admin grup
        dengan izin tersebut.

    Setelah penurunan berhasil, _execute_kick() mengantrekan notifikasi
    teks ke grup via _enqueue_warning() dengan jeda antar pesan.
    """
    if not userbot:
        return
    try:
        from pyrogram.raw import functions as _rf
        peer = await userbot.resolve_peer(user_id)
        await userbot.invoke(
            _rf.phone.EditGroupCallParticipant(
                call=call_input,
                participant=peer,
                muted=True,
                volume=0,
                raise_hand=False,
                video_stopped=True,
                video_paused=True,
                presentation_paused=True,
            )
        )
        print(f"[UB-VC] ✅ User {user_id} diturunkan dari voice chat grup {chat_id}")
    except FloodWait as fw:
        print(f"[UB-VC] FloodWait {fw.value}s saat kick uid={user_id} — menunggu & retry...")
        await asyncio.sleep(fw.value + 1)
        # Coba sekali lagi setelah FloodWait
        try:
            from pyrogram.raw import functions as _rf2
            peer2 = await userbot.resolve_peer(user_id)
            await userbot.invoke(
                _rf2.phone.EditGroupCallParticipant(
                    call=call_input,
                    participant=peer2,
                    muted=True,
                    volume=0,
                    raise_hand=False,
                    video_stopped=True,
                    video_paused=True,
                    presentation_paused=True,
                )
            )
            print(f"[UB-VC] ✅ Retry kick uid={user_id} di grup {chat_id} berhasil")
        except Exception as e2:
            print(f"[UB-VC] Retry kick uid={user_id} gagal: {e2}")
    except Exception as e:
        print(f"[UB-VC] Gagal kick uid={user_id} dari voice chat: {e}")


async def _do_send_warning(chat_id: int, user_id: int) -> None:
    """
    Bot biasa mengirim peringatan di grup kepada user yang diturunkan.
    Juga mencatat ke group_action_log (pakai fungsi asli database.py).

    DIPANGGIL OLEH _warn_worker — tidak langsung, selalu via _enqueue_warning().
    FloodWait ditangani di sini: tunggu dan coba ulang sekali.
    """
    if not _bot_ref:
        return
    try:
        from database import insert_group_action_log

        # Ambil nama user
        name = str(user_id)
        try:
            u = await _bot_ref.get_users(user_id)
            name = u.first_name or str(user_id)
        except Exception:
            pass

        mention = f"<a href='tg://user?id={user_id}'>{name}</a>"

        # Kirim peringatan di grup via bot biasa — tangani FloodWait
        warn_msg = (
            f"🔇 {mention} diturunkan dari obrolan suara.\n"
            f"<i>Hapus link/privatkan bio Anda untuk dapat "
            f"naik kembali ke obrolan suara.</i>"
        )
        sent_warn = None
        try:
            sent_warn = await _bot_ref.send_message(chat_id, warn_msg, parse_mode=ParseMode.HTML)
        except FloodWait as fw_warn:
            print(f"[UB-Warn] FloodWait {fw_warn.value}s saat kirim warn ke grup {chat_id} — menunggu...")
            await asyncio.sleep(fw_warn.value + 1)
            try:
                sent_warn = await _bot_ref.send_message(chat_id, warn_msg, parse_mode=ParseMode.HTML)
            except Exception as e2:
                print(f"[UB-Warn] Retry warn gagal uid={user_id}: {e2}")

        # Hapus pesan peringatan otomatis setelah 10 detik
        if sent_warn:
            async def _auto_delete_warn(msg=sent_warn):
                await asyncio.sleep(10)
                try:
                    await msg.delete()
                except Exception:
                    pass
            asyncio.create_task(_auto_delete_warn())

        # Catat ke log aktivitas grup (fungsi asli database.py)
        await insert_group_action_log(
            chat_id,
            "KICK-VC",
            "Security OS: link di bio, dikeluarkan dari voice chat",
            user_id,
            name[:50],
        )

        # Hapus cache bio user ini agar bisa naik lagi setelah benahi bio
        _bio_cache.pop((chat_id, user_id), None)

    except Exception as e:
        print(f"[UB-Warn] Gagal kirim peringatan uid={user_id}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SETUP BOT PEMANTAU
# Dipanggil dari handler UI saat admin memasukkan token bot pemantau baru.
# ══════════════════════════════════════════════════════════════════════════════

async def change_userbot(
    new_phone: str,
    bot: _Client,
) -> tuple[bool, str]:
    """
    Ganti akun userbot dengan nomor HP baru.

    ── ALUR ────────────────────────────────────────────────────────────────
    1. Hentikan userbot lama (jika aktif).
    2. Hapus session lama dari disk dan DB.
    3. Tulis USERBOT_PHONE baru ke variabel global dan file .env (jika ada).
    4. Mulai OTP login flow untuk nomor baru — owner kirim /otp <kode> via DM.
    5. Setelah login berhasil, simpan session baru dan aktifkan voice monitor.

    Dipanggil dari handler UI secos_setuserbot_{chat_id} di handlers_secos.py.
    Return: (berhasil: bool, pesan_hasil: str)
    """
    global userbot, _ub_ready, _ub_self_id, USERBOT_PHONE

    # ── 1. Validasi format nomor ─────────────────────────────────────────
    clean_phone = new_phone.strip()
    if not _re.match(r"^\+\d{7,15}$", clean_phone):
        return False, (
            "Format nomor tidak valid. Gunakan format internasional, "
            "contoh: <code>+628123456789</code>"
        )

    # ── 2. Hentikan userbot lama ─────────────────────────────────────────
    _ub_ready = False
    if userbot:
        try:
            await userbot.stop()
        except Exception:
            pass
        userbot = None
    _ub_self_id = 0

    # Hapus session lama dari disk
    session_file = _UB_SESSION + ".session"
    try:
        _Path(session_file).unlink(missing_ok=True)
    except Exception:
        pass

    # Hapus session lama dari DB
    try:
        db, _, _ = _get_db()
        await db["userbot_session"].delete_many({})
    except Exception:
        pass

    # ── 3. Set nomor baru ────────────────────────────────────────────────
    USERBOT_PHONE = clean_phone

    # Perbarui .env jika file ada (best-effort)
    env_path = _Path(__file__).parent / ".env"
    if env_path.exists():
        try:
            env_text = env_path.read_text()
            import re as _re2
            if _re2.search(r"^USERBOT_PHONE\s*=", env_text, _re2.MULTILINE):
                env_text = _re2.sub(
                    r"^(USERBOT_PHONE\s*=).*$",
                    rf"\g<1>{clean_phone}",
                    env_text,
                    flags=_re2.MULTILINE,
                )
            else:
                env_text += f"\nUSERBOT_PHONE={clean_phone}\n"
            env_path.write_text(env_text)
        except Exception as e:
            print(f"[UB-Change] Gagal update .env: {e} (tidak fatal)")

    # ── 4. Login dengan nomor baru ───────────────────────────────────────
    print(f"[UB-Change] 🔄 Ganti userbot → nomor baru: {clean_phone}")
    result = await _do_login(bot)

    if isinstance(result, tuple):
        ok, self_id = result
    else:
        ok, self_id = result, 0

    if not ok or not userbot:
        return False, (
            "Login userbot baru gagal. Pastikan nomor benar dan OTP dikirim "
            "via DM bot dengan format <code>/otp &lt;kode&gt;</code>."
        )

    # ── 5. Aktifkan ──────────────────────────────────────────────────────
    _ub_self_id = self_id
    _ub_ready   = True
    try:
        me = await userbot.get_me()
        uname = me.username or me.first_name or str(me.id)
    except Exception:
        uname = "userbot baru"

    await _log_registered_groups()
    asyncio.create_task(_voice_chat_monitor_loop())

    print(f"[UB-Change] ✅ Userbot berhasil diganti → @{uname} (id={self_id})")
    return True, (
        f"✅ Userbot berhasil diganti ke <b>@{uname}</b> (id: <code>{self_id}</code>).\n"
        f"Voice chat monitor sudah aktif kembali."
    )


async def setup_monitor_bot(
    chat_id: int,
    token: str,
    inviter_bot: _Client,
) -> tuple[bool, str]:
    """
    Validasi token bot pemantau dan simpan ke DB.
    Bot pemantau TIDAK langsung di-join ke grup — admin menambahkannya manual.
    Saat bot pemantau masuk ke grup, handler on_chat_member_updated akan
    mengenalinya otomatis dari DB.

    Jika grup ini sudah punya bot pemantau LAMA (token berbeda),
    bot lama di-kick dulu dari grup sebelum yang baru disimpan.

    Return: (berhasil: bool, pesan_hasil: str)
    """
    import httpx

    db, _, _ = _get_db()

    # ── 1. Validasi token via Telegram getMe ─────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10) as hc:
            resp = await hc.get(f"https://api.telegram.org/bot{token}/getMe")
            data = resp.json()
        if not data.get("ok"):
            desc = data.get("description", "unknown error")
            return False, f"Token tidak valid: {desc}"
        info           = data["result"]
        monitor_bot_id = int(info["id"])
        monitor_uname  = info.get("username", str(monitor_bot_id))
    except Exception as e:
        return False, f"Gagal menghubungi Telegram API: {e}"

    # ── 2. Pastikan bot pemantau belum dipakai grup lain ─────────────────────
    mon_col  = db["security_os_monitors"]
    existing = await mon_col.find_one({"monitor_bot_id": monitor_bot_id})
    if existing:
        existing_chat = int(existing.get("chat_id", 0))
        if existing_chat != chat_id:
            return False, (
                f"Bot @{monitor_uname} sudah terdaftar di grup lain "
                f"(<code>{existing_chat}</code>).\n"
                f"1 bot pemantau hanya boleh digunakan di 1 grup."
            )
        # Bot pemantau sudah terdaftar di grup ini — update saja (token baru)

    # ── 2b. Kick bot pemantau LAMA jika token berbeda ────────────────────────
    old_doc    = await _sec_os_get(chat_id)
    old_mon_id = old_doc.get("monitor_bot_id", 0)
    if old_mon_id and old_mon_id != monitor_bot_id:
        old_uname = _monitor_username_cache.get(old_mon_id, f"id:{old_mon_id}")
        try:
            await inviter_bot.ban_chat_member(chat_id, old_mon_id)
            await asyncio.sleep(1)
            await inviter_bot.unban_chat_member(chat_id, old_mon_id)
            print(f"[SecOS] Bot lama @{old_uname} ({old_mon_id}) di-kick dari grup {chat_id}")
        except Exception as e_kick:
            print(f"[SecOS] Kick bot lama gagal (mungkin sudah tidak ada): {e_kick}")
        # Hapus entri lama dari monitor index
        await mon_col.delete_one({"monitor_bot_id": old_mon_id})
        _monitor_username_cache.pop(old_mon_id, None)

    # ── 3. Simpan ke DB — bot pemantau dikonfigurasi, belum harus join ───────
    await _sec_os_set_monitor(chat_id, token, monitor_bot_id)

    # Index global: 1 bot pemantau → 1 grup
    await mon_col.update_one(
        {"monitor_bot_id": monitor_bot_id},
        {"$set": {"monitor_bot_id": monitor_bot_id, "chat_id": chat_id}},
        upsert=True,
    )

    # Cache username
    _monitor_username_cache[monitor_bot_id] = monitor_uname

    print(f"[SecOS] Bot pemantau @{monitor_uname} ({monitor_bot_id}) dikonfigurasi untuk grup {chat_id}")
    print(f"[SecOS] Menunggu @{monitor_uname} ditambahkan ke grup secara manual...")

    # ── Langsung spawn instance bot pemantau baru ─────────────────────────────
    # Instance ini akan mulai scan berkala setelah bot pemantau join ke grup.
    # Tidak perlu restart proses — instance jalan dalam proses yang sama.
    try:
        from monitor_bot_reference import spawn_monitor_for_group
        asyncio.create_task(
            spawn_monitor_for_group(chat_id, token, monitor_bot_id)
        )
        print(f"[SecOS] MonitorInstance untuk grup {chat_id} di-spawn.")
    except Exception as e_spawn:
        print(f"[SecOS] Gagal spawn MonitorInstance: {e_spawn}")
        # Tidak fatal — instance akan di-load ulang saat restart proses

    return True, (
        f"Bot @{monitor_uname} berhasil dikonfigurasi.\n"
        f"Sekarang tambahkan <b>@{monitor_uname}</b> ke grup secara manual,\n"
        f"dan bot akan dikenali otomatis saat masuk."
    )


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — fungsi yang dipanggil dari luar modul ini
# ══════════════════════════════════════════════════════════════════════════════

async def security_os_enable(chat_id: int) -> None:
    """Aktifkan Security OS untuk grup ini."""
    await _sec_os_set_enabled(chat_id, True)
    # Reset cache bio agar semua user diperiksa ulang saat fitur diaktifkan
    keys_to_del = [k for k in _bio_cache if k[0] == chat_id]
    for k in keys_to_del:
        _bio_cache.pop(k, None)


async def security_os_disable(chat_id: int) -> None:
    """Nonaktifkan Security OS untuk grup ini."""
    await _sec_os_set_enabled(chat_id, False)
    # Hentikan instance bot pemantau untuk grup ini
    try:
        from monitor_bot_reference import stop_monitor_for_group
        await stop_monitor_for_group(chat_id)
    except Exception as e:
        print(f"[SecOS] Gagal stop MonitorInstance grup {chat_id}: {e}")


async def security_os_get_status(chat_id: int) -> dict:
    """Ambil status Security OS untuk grup. Return dict dokumen DB."""
    return await _sec_os_get(chat_id)


def is_userbot_ready() -> bool:
    """Return True jika userbot sudah login dan siap memantau."""
    return _ub_ready and userbot is not None


async def check_monitor_is_member(client: _Client, chat_id: int) -> bool:
    """
    Cek apakah bot pemantau sudah menjadi anggota (atau admin) di grup.

    Menggunakan bot utama (client) untuk get_chat_member karena userbot mungkin
    tidak selalu ada di grup target.

    Return True jika bot pemantau sudah ada di grup, False jika belum.
    """
    sec_doc = await _sec_os_get(chat_id)
    monitor_bot_id = sec_doc.get("monitor_bot_id", 0)
    if not monitor_bot_id:
        return False

    try:
        from pyrogram.enums import ChatMemberStatus
        member = await client.get_chat_member(chat_id, monitor_bot_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except PeerIdInvalid:
        # Peer belum dikenal sesi ini — force resolve dulu via get_chat
        try:
            await client.get_chat(chat_id)
            from pyrogram.enums import ChatMemberStatus
            member = await client.get_chat_member(chat_id, monitor_bot_id)
            return member.status in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            )
        except Exception as e2:
            print(f"[SecOS] check_monitor_is_member error chat={chat_id}: {e2}")
            return False
    except Exception as e:
        # USER_NOT_PARTICIPANT atau error lain → belum jadi anggota
        print(f"[SecOS] check_monitor_is_member error chat={chat_id}: {e}")
        return False


async def check_activation_prerequisites(
    client: _Client,
    chat_id: int,
) -> tuple[bool, list[str]]:
    """
    Periksa syarat wajib sebelum Security OS boleh diaktifkan.

    Syarat WAJIB (memblokir aktivasi):
      1. Userbot sudah online
      2. Bot pemantau sudah dikonfigurasi di DB

    Syarat OPSIONAL (warning saja, tidak memblokir):
      3. Bot pemantau sudah jadi anggota grup
         (bisa diaktifkan dulu, bot dikenali otomatis saat masuk)

    Return: (syarat_wajib_terpenuhi: bool, daftar_pesan: list[str])
    """
    blockers: list[str] = []
    warnings: list[str] = []

    # ── Syarat wajib 1: userbot online ───────────────────────────────────────
    if not is_userbot_ready():
        blockers.append(
            "⚠️ <b>Userbot belum online.</b>\n"
            "└ Pastikan <code>USERBOT_PHONE</code> sudah diisi di <code>.env</code> "
            "dan bot sudah di-restart. Kemudian kirim OTP yang dikirim Telegram ke HP Anda."
        )

    # ── Syarat wajib 2: bot pemantau sudah dikonfigurasi di DB ───────────────
    sec_doc = await _sec_os_get(chat_id)
    has_monitor_config = bool(sec_doc.get("monitor_bot_id", 0))

    if not has_monitor_config:
        blockers.append(
            "🤖 <b>Bot pemantau belum dikonfigurasi.</b>\n"
            "└ Buat bot baru via @BotFather, salin tokennya, lalu tekan "
            "<b>🤖 Pasang Bot Pemantau</b> dan masukkan token tersebut.\n"
            "   Setelah token disimpan, tambahkan bot pemantau ke grup secara manual."
        )
    else:
        # ── Warning opsional: bot pemantau belum join grup ───────────────────
        is_member = await check_monitor_is_member(client, chat_id)
        if not is_member:
            monitor_bot_id = sec_doc.get("monitor_bot_id", 0)
            uname = _monitor_username_cache.get(monitor_bot_id, f"id:{monitor_bot_id}")
            warnings.append(
                f"ℹ️ <b>Bot pemantau @{uname} belum ada di grup.</b>\n"
                f"└ Tambahkan ke grup agar fitur checkbio berfungsi.\n"
                f"   Bot akan dikenali otomatis saat masuk.\n"
                f"   <i>(Security OS tetap bisa diaktifkan sekarang.)</i>"
            )

    all_ok = len(blockers) == 0
    # Blockers dulu, lalu warnings — caller menampilkan semuanya
    return all_ok, blockers + warnings


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-KENALI BOT PEMANTAU SAAT DITAMBAHKAN KE GRUP
# Saat bot pemantau masuk ke grup, cocokkan dengan DB → log konfirmasi.
# group=10 — jalan setelah handler nexus (8, 9) tapi tidak mengganggu mereka.
# ══════════════════════════════════════════════════════════════════════════════

def register_monitor_join_handler(bot: _Client) -> None:
    """
    Pasang handler on_chat_member_updated di bot utama untuk mendeteksi
    bot pemantau yang baru ditambahkan ke grup.
    Dipanggil dari start_userbot() setelah bot biasa aktif.
    """

    @bot.on_chat_member_updated(group=10)
    async def _on_monitor_joined(client: _Client, update: _ChatMemberUpdated):
        try:
            from pyrogram.enums import ChatMemberStatus

            new = update.new_chat_member
            if not new or not new.user or not new.user.is_bot:
                return  # bukan bot → skip

            # Hanya tangkap event JOIN (bukan kick/ban/promote)
            if new.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
                return

            bot_id  = new.user.id
            chat_id = update.chat.id

            # Cek apakah bot ini adalah bot pemantau yang terdaftar untuk grup ini
            sec_doc = await _sec_os_get(chat_id)
            registered_monitor_id = sec_doc.get("monitor_bot_id", 0)

            if not registered_monitor_id or registered_monitor_id != bot_id:
                return  # bukan bot pemantau kita → skip

            uname = new.user.username or str(bot_id)
            _monitor_username_cache[bot_id] = uname

            print(f"[SecOS] ✅ Bot pemantau @{uname} ({bot_id}) terdeteksi masuk grup {chat_id} — dikenali otomatis.")

            # Jika Security OS sudah enabled, tidak perlu lakukan apa-apa lagi
            # Jika belum enabled, beri tahu di console saja
            if not sec_doc.get("enabled", False):
                print(f"[SecOS] ℹ️  Security OS grup {chat_id} belum diaktifkan. Aktifkan via panel.")

        except Exception as e:
            print(f"[SecOS] _on_monitor_joined error: {e}")
