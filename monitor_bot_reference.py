"""
monitor_bot_reference.py
════════════════════════════════════════════════════════════════════════════════
MANAJER BOT PEMANTAU — Security OS (Multi-Instance, Database-Driven)

ARSITEKTUR:
  Tiap grup Security OS punya 1 bot pemantau sendiri (token berbeda).
  Token disimpan di DB: security_os.monitor_token (diisi saat admin setup).
  File ini menjalankan SEMUA bot pemantau dalam SATU proses — tiap bot
  berjalan sebagai Pyrogram Client tersendiri (instance terpisah).

  ┌────────────────────────────────────────────────────────────────────┐
  │  monitor_bot_reference.py (proses ini)                             │
  │                                                                    │
  │   MonitorInstance(chat_id=grupA, token=tokenA)  ← scan grupA      │
  │   MonitorInstance(chat_id=grupB, token=tokenB)  ← scan grupB      │
  │   MonitorInstance(chat_id=grupC, token=tokenC)  ← scan grupC      │
  │                                                                    │
  │   Semua tulis ke collection bio_profiles dengan field chat_id      │
  └──────────────────────────────┬─────────────────────────────────────┘
                                 │ DB bersama (MONGO_URL / SQLite)
              ┌──────────────────┴──────────────────┐
              ▼                                     ▼
    ┌──────────────────┐                 ┌──────────────────────┐
    │   Bot Utama      │  query          │      Userbot         │
    │  bio_filter      │  bio_profiles   │   (VC kick)          │
    │  (chat_id=grupA) │  {user_id,      │   (chat_id=grupA)    │
    └──────────────────┘   chat_id}      └──────────────────────┘

COLLECTION bio_profiles:
  {
    chat_id    : int,    # ID grup (tiap grup data terpisah)
    user_id    : int,    # ID user
    has_link   : bool,   # True = ada link di bio
    bio        : str,    # isi bio saat dicek
    checked_at : float,  # unix timestamp terakhir dicek
    updated_at : float,  # unix timestamp terakhir berubah status
  }
  Index unik: (chat_id, user_id)

FLOW TOKEN:
  1. Admin aktifkan Security OS di grup → bot utama minta token bot pemantau
  2. Admin kirim token via DM ke bot utama
  3. Bot utama validasi token → simpan ke security_os.monitor_token
  4. Bot utama panggil reload_monitor_instances() (fungsi di file ini)
  5. File ini spawn MonitorInstance baru untuk token/grup tersebut

VARIABEL .env:
  API_ID, API_HASH   — sama dengan bot utama
  MONGO_URL          — HARUS SAMA dengan bot utama (DB bersama)
  MONGO_DB_NAME      — HARUS SAMA dengan bot utama
  CODE_BOT           — HARUS SAMA dengan bot utama
  SCAN_INTERVAL_MINUTES  — interval scan ulang per grup (default: 30)
  BIO_RECHECK_SECS       — jeda minimum re-check user sama (default: 600)
"""

from __future__ import annotations

import os
import re
import time
import asyncio
from pathlib import Path
from datetime import timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.types import Message, ChatMemberUpdated
from pyrogram.raw import functions as raw_fns, types as raw_types
from pyrogram.errors import FloodWait, PeerIdInvalid, ChatAdminRequired

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

# ── Env ───────────────────────────────────────────────────────────────────────
API_ID    = int(os.environ.get("API_ID", 0))
API_HASH  = os.environ.get("API_HASH", "")

SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_INTERVAL_MINUTES", 30))
BIO_RECHECK_SECS      = int(os.environ.get("BIO_RECHECK_SECS", 600))

# ── Pola deteksi link di bio ──────────────────────────────────────────────────
LINK_PATTERN = re.compile(
    r"(@\S+|https?://\S+|t\.me/\S+|bit\.ly/\S+|linktr\.ee/\S+)",
    re.IGNORECASE,
)

TZ_WIB = timezone(timedelta(hours=7))

# ── Database — pakai modul yang sama dengan bot utama ────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent))
from database import db, _init_backend  # noqa: E402

bio_col = db["bio_profiles"]   # Collection hasil scan — dibaca bot utama & userbot
sec_col = db["security_os"]    # Untuk ambil daftar grup + token

# ── Registry instance aktif ───────────────────────────────────────────────────
# chat_id → MonitorInstance yang sedang berjalan
_active_instances: dict[int, "MonitorInstance"] = {}
_instances_lock = asyncio.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# KELAS UTAMA — SATU INSTANCE PER GRUP
# ══════════════════════════════════════════════════════════════════════════════

class MonitorInstance:
    """
    Satu bot pemantau untuk satu grup.
    Punya Pyrogram Client sendiri (token unik per grup).
    Berjalan sebagai background task — scan berkala + event join.
    """

    def __init__(self, chat_id: int, token: str, bot_id: int):
        self.chat_id    = chat_id
        self.token      = token
        self.bot_id     = bot_id
        self._stopped   = False
        self._last_checked: dict[int, float] = {}   # user_id → timestamp

        # Nama session unik per grup agar tidak bentrok antar instance
        session_name = f"monitor_{abs(chat_id)}"
        self.client = Client(
            session_name,
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=token,
        )

        # Task background
        self._scan_task: Optional[asyncio.Task] = None
        self._raw_handler_registered = False

    # ── Start / Stop ──────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Mulai client dan daftarkan handler. Return False jika gagal."""
        try:
            await self.client.start()
            me = await self.client.get_me()
            print(
                f"[Monitor {self.chat_id}] ✅ @{me.username} aktif "
                f"(bot_id={self.bot_id})"
            )
        except Exception as e:
            print(f"[Monitor {self.chat_id}] ❌ Gagal start: {e}")
            return False

        # Daftarkan event handler (join member)
        self._register_handlers()

        # Jalankan background scan loop
        self._scan_task = asyncio.create_task(self._scan_loop())

        return True

    async def stop(self) -> None:
        """Hentikan client dan semua task."""
        self._stopped = True
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        try:
            await self.client.stop()
        except Exception:
            pass
        print(f"[Monitor {self.chat_id}] ⏹ Dihentikan.")

    # ── Bio check & simpan ────────────────────────────────────────────────────

    async def _fetch_bio(self, user_id: int) -> str | None:
        """Ambil bio user via Telegram API. Return None jika gagal."""
        try:
            peer = await self.client.resolve_peer(user_id)
            full = await self.client.invoke(
                raw_fns.users.GetFullUser(id=peer)
            )
            return getattr(full.full_user, "about", None) or ""
        except FloodWait as fw:
            print(
                f"[Monitor {self.chat_id}] FloodWait {fw.value}s "
                f"uid={user_id}"
            )
            await asyncio.sleep(fw.value + 1)
            return None
        except (PeerIdInvalid, KeyError):
            return None
        except Exception as e:
            print(
                f"[Monitor {self.chat_id}] Gagal ambil bio "
                f"uid={user_id}: {e}"
            )
            return None

    async def check_and_save(
        self, user_id: int, force: bool = False
    ) -> bool | None:
        """
        Cek bio user, simpan ke bio_profiles dengan chat_id grup ini.

        Return: True (ada link) | False (tidak) | None (gagal fetch)
        Throttle: skip jika belum BIO_RECHECK_SECS sejak cek terakhir,
                  kecuali force=True.
        """
        now = time.time()

        if not force:
            last = self._last_checked.get(user_id, 0)
            if now - last < BIO_RECHECK_SECS:
                # Kembalikan data dari DB tanpa hit API
                doc = await bio_col.find_one(
                    {"chat_id": self.chat_id, "user_id": user_id}
                )
                return doc.get("has_link", False) if doc else None

        bio_text = await self._fetch_bio(user_id)
        if bio_text is None:
            return None

        has_link = bool(LINK_PATTERN.search(bio_text))
        self._last_checked[user_id] = now

        # Baca dokumen lama untuk deteksi perubahan status
        old_doc     = await bio_col.find_one(
            {"chat_id": self.chat_id, "user_id": user_id}
        )
        old_has_link = old_doc.get("has_link") if old_doc else None
        updated_at   = (
            now
            if old_has_link != has_link
            else (old_doc.get("updated_at", now) if old_doc else now)
        )

        await bio_col.update_one(
            {"chat_id": self.chat_id, "user_id": user_id},
            {"$set": {
                "chat_id":    self.chat_id,
                "user_id":    user_id,
                "has_link":   has_link,
                "bio":        bio_text[:500],
                "checked_at": now,
                "updated_at": updated_at,
            }},
            upsert=True,
        )

        if old_has_link != has_link:
            status = "ADA LINK" if has_link else "HAPUS LINK"
            print(
                f"[Monitor {self.chat_id}] uid={user_id} → {status} "
                f"| bio: {bio_text[:80]!r}"
            )

        return has_link

    # ── Scan semua member grup ────────────────────────────────────────────────

    async def _scan_all_members(self) -> int:
        """
        Iterasi semua member non-bot di grup ini.
        Return jumlah user yang diproses.
        """
        count = 0
        try:
            # Force resolve peer agar sesi baru tidak PEER_ID_INVALID
            try:
                await self.client.get_chat(self.chat_id)
            except Exception:
                pass

            async for member in self.client.get_chat_members(self.chat_id):
                if self._stopped:
                    break
                if member.user is None or member.user.is_bot:
                    continue
                await self.check_and_save(member.user.id)
                count += 1
                await asyncio.sleep(0.35)   # jaga rate limit
        except FloodWait as fw:
            print(
                f"[Monitor {self.chat_id}] FloodWait {fw.value}s "
                "saat scan member"
            )
            await asyncio.sleep(fw.value + 1)
        except Exception as e:
            print(f"[Monitor {self.chat_id}] Error scan member: {e}")
        return count

    # ── Background scan loop ──────────────────────────────────────────────────

    async def _scan_loop(self) -> None:
        """
        Scan berkala tiap SCAN_INTERVAL_MINUTES.
        Langsung scan pertama saat start (delay 10 detik agar client stabil).
        """
        await asyncio.sleep(10)
        while not self._stopped:
            try:
                print(
                    f"[Monitor {self.chat_id}] Mulai scan semua member..."
                )
                n = await self._scan_all_members()
                print(
                    f"[Monitor {self.chat_id}] ✅ Scan selesai "
                    f"— {n} user diproses."
                )
            except Exception as e:
                print(f"[Monitor {self.chat_id}] Error di scan loop: {e}")

            interval = SCAN_INTERVAL_MINUTES * 60
            print(
                f"[Monitor {self.chat_id}] Scan berikutnya "
                f"dalam {SCAN_INTERVAL_MINUTES} menit."
            )
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    # ── Event handlers ────────────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        """Daftarkan handler Pyrogram ke client instance ini."""
        chat_id = self.chat_id
        monitor = self

        # ── User KIRIM PESAN di grup → cek bio (throttle BIO_RECHECK_SECS) ────
        @self.client.on_message(filters.chat(chat_id) & filters.group)
        async def _on_message(client: Client, message: Message):
            user = message.from_user
            if user is None or user.is_bot:
                return
            # force=False → otomatis skip jika sudah dicek < BIO_RECHECK_SECS
            await monitor.check_and_save(user.id, force=False)

        # ── User JOIN grup → langsung cek bio ────────────────────────────────
        @self.client.on_chat_member_updated()
        async def _on_join(client: Client, upd: ChatMemberUpdated):
            if upd.chat.id != chat_id:
                return
            if upd.new_chat_member is None:
                return
            user = upd.new_chat_member.user
            if user is None or user.is_bot:
                return
            print(
                f"[Monitor {chat_id}] User {user.id} join "
                "→ cek bio (force)"
            )
            await monitor.check_and_save(user.id, force=True)

        # ── Perubahan profil user → re-check bio ──────────────────────────────
        @self.client.on_raw_update()
        async def _on_profile_change(client, update, users, chats):
            try:
                user_id = None
                if isinstance(update, raw_types.UpdateUserName):
                    user_id = getattr(update, "user_id", None)
                else:
                    # UpdateUserPhoto dihapus di pyrogram 2.0.106+
                    # Gunakan duck-typing berdasarkan nama tipe
                    type_name = type(update).__name__
                    if "Photo" in type_name or "Profile" in type_name:
                        user_id = getattr(update, "user_id", None)

                if user_id and isinstance(user_id, int) and user_id > 0:
                    # Hanya proses jika user ini dikenal di grup ini
                    known = await bio_col.find_one(
                        {"chat_id": chat_id, "user_id": user_id}
                    )
                    if known:
                        print(
                            f"[Monitor {chat_id}] Profil uid={user_id} "
                            "berubah → re-check"
                        )
                        await monitor.check_and_save(user_id, force=True)
            except Exception as e:
                print(f"[Monitor {chat_id}] raw_update error: {e}")

        self._raw_handler_registered = True


# ══════════════════════════════════════════════════════════════════════════════
# MANAJER INSTANCE — LOAD / RELOAD / STOP
# ══════════════════════════════════════════════════════════════════════════════

async def _load_instances_from_db() -> None:
    """
    Baca semua grup Security OS aktif dari DB.
    Untuk tiap grup yang punya monitor_token → spawn MonitorInstance.
    Dipanggil saat startup.
    """
    try:
        docs = await sec_col.find({"enabled": True}).to_list(None)
    except Exception as e:
        print(f"[MonitorMgr] Gagal baca security_os dari DB: {e}")
        return

    for doc in docs:
        chat_id = doc.get("chat_id")
        token   = doc.get("monitor_token", "").strip()
        bot_id  = doc.get("monitor_bot_id", 0)

        if not chat_id or not token or not bot_id:
            continue

        await _spawn_instance(chat_id, token, bot_id)


async def _spawn_instance(
    chat_id: int, token: str, bot_id: int
) -> bool:
    """
    Spawn MonitorInstance baru untuk chat_id.
    Jika sudah ada instance untuk chat_id ini, skip (tidak dobel).
    Return True jika berhasil di-start.
    """
    async with _instances_lock:
        existing = _active_instances.get(chat_id)
        if existing and not existing._stopped:
            # Cek apakah token berubah
            if existing.token == token:
                return True   # Sudah jalan, token sama → skip
            # Token berubah → stop lama, spawn baru
            print(
                f"[MonitorMgr] Token berubah untuk grup {chat_id} "
                "→ restart instance"
            )
            await existing.stop()

        instance = MonitorInstance(chat_id, token, bot_id)
        ok = await instance.start()
        if ok:
            _active_instances[chat_id] = instance
        return ok


async def _stop_instance(chat_id: int) -> None:
    """
    Hentikan MonitorInstance untuk chat_id (jika ada).
    Dipanggil saat Security OS dinonaktifkan atau token dihapus.
    """
    async with _instances_lock:
        instance = _active_instances.pop(chat_id, None)
    if instance:
        await instance.stop()


# ── PUBLIC API — dipanggil dari video_call.py / handler UI ───────────────────

async def reload_monitor_instances() -> None:
    """
    Reload ulang semua instance dari DB.
    Panggil ini setelah admin menambah/mengubah/menonaktifkan bot pemantau
    agar perubahan langsung berlaku tanpa restart proses.
    Fungsi ini safe dipanggil berkali-kali (idempotent).
    """
    try:
        docs = await sec_col.find({}).to_list(None)
    except Exception as e:
        print(f"[MonitorMgr] reload: gagal baca DB: {e}")
        return

    db_chat_ids: set[int] = set()

    for doc in docs:
        chat_id = doc.get("chat_id")
        enabled = doc.get("enabled", False)
        token   = doc.get("monitor_token", "").strip()
        bot_id  = doc.get("monitor_bot_id", 0)

        if not chat_id:
            continue

        if enabled and token and bot_id:
            db_chat_ids.add(chat_id)
            await _spawn_instance(chat_id, token, bot_id)
        else:
            # Grup dinonaktifkan atau token dihapus → stop instance
            if chat_id in _active_instances:
                await _stop_instance(chat_id)

    # Stop instance untuk chat_id yang sudah tidak ada di DB sama sekali
    async with _instances_lock:
        stale = [
            cid for cid in list(_active_instances.keys())
            if cid not in db_chat_ids
        ]
    for cid in stale:
        await _stop_instance(cid)

    print(
        f"[MonitorMgr] Reload selesai — "
        f"{len(db_chat_ids)} grup aktif, "
        f"{len(_active_instances)} instance berjalan."
    )


async def spawn_monitor_for_group(
    chat_id: int, token: str, bot_id: int
) -> bool:
    """
    Spawn instance langsung untuk satu grup — dipanggil dari setup_monitor_bot()
    di video_call.py segera setelah token baru tersimpan ke DB.
    Tidak perlu reload semua instance.
    Return True jika berhasil.
    """
    return await _spawn_instance(chat_id, token, bot_id)


async def stop_monitor_for_group(chat_id: int) -> None:
    """
    Stop instance untuk satu grup — dipanggil saat Security OS dinonaktifkan.
    """
    await _stop_instance(chat_id)


def get_active_instance_count() -> int:
    """Return jumlah instance bot pemantau yang sedang berjalan."""
    return len(_active_instances)


def get_active_chat_ids() -> list[int]:
    """Return daftar chat_id yang sedang dipantau."""
    return list(_active_instances.keys())


# ══════════════════════════════════════════════════════════════════════════════
# QUERY BIO — DIPANGGIL OLEH bio.py DAN video_call.py
# ══════════════════════════════════════════════════════════════════════════════

async def force_check_user(chat_id: int, user_id: int) -> bool | None:
    """
    Paksa re-check bio user via MonitorInstance aktif untuk grup ini.
    Dipanggil oleh bio.py / video_call.py saat data belum ada di DB (None).

    Return:
      True  → ada link di bio
      False → tidak ada link
      None  → instance tidak aktif atau gagal fetch
    """
    instance = _active_instances.get(chat_id)
    if instance is None:
        return None
    try:
        return await instance.check_and_save(user_id, force=True)
    except Exception as e:
        print(f"[MonitorQuery] force_check_user chat={chat_id} uid={user_id}: {e}")
        return None


async def query_bio(chat_id: int, user_id: int) -> bool | None:
    """
    Baca hasil cek bio dari DB untuk pasangan (chat_id, user_id).
    Data ini ditulis oleh MonitorInstance grup yang bersangkutan.

    Return:
      True  → ada link di bio
      False → tidak ada link di bio
      None  → data belum ada (bot pemantau belum scan user ini) → lewatkan
    """
    try:
        doc = await bio_col.find_one(
            {"chat_id": chat_id, "user_id": user_id}
        )
    except Exception as e:
        print(
            f"[MonitorQuery] Gagal query bio "
            f"chat={chat_id} uid={user_id}: {e}"
        )
        return None

    if not doc:
        return None

    has_link   = doc.get("has_link", False)
    checked_at = doc.get("checked_at", 0)
    data_age   = int(time.time() - checked_at)

    if data_age > SCAN_INTERVAL_MINUTES * 60 * 3:
        # Data lebih tua dari 3x interval scan — catat saja, tetap pakai
        print(
            f"[MonitorQuery] Data bio chat={chat_id} uid={user_id} "
            f"sudah {data_age//60} menit lama."
        )

    return has_link


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP — ENTRY POINT (jalankan sebagai proses terpisah)
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    """
    Entry point — jalankan manajer bot pemantau sebagai proses mandiri.
    Semua instance bot pemantau dikelola di sini.
    """
    if not API_ID or not API_HASH:
        print("❌ API_ID / API_HASH tidak diset di .env")
        return

    # Init DB (sama polanya dengan bot utama)
    await _init_backend()
    print("[MonitorMgr] DB berhasil di-inisialisasi.")

    # Load semua instance dari DB
    await _load_instances_from_db()

    n = get_active_instance_count()
    print(
        f"[MonitorMgr] ✅ {n} instance bot pemantau aktif. "
        "Tekan Ctrl+C untuk berhenti."
    )

    if n == 0:
        print(
            "[MonitorMgr] Tidak ada bot pemantau aktif saat startup.\n"
            "  → Aktifkan Security OS di grup via bot utama untuk menambah bot pemantau."
        )

    # Jaga proses tetap hidup — instance tiap bot punya event loop sendiri
    try:
        while True:
            await asyncio.sleep(60)
            # Heartbeat: log jumlah instance aktif setiap 1 jam
    except asyncio.CancelledError:
        pass
    finally:
        # Graceful shutdown semua instance
        print("[MonitorMgr] Shutdown — menghentikan semua instance...")
        for instance in list(_active_instances.values()):
            await instance.stop()
        print("[MonitorMgr] Semua instance dihentikan.")


if __name__ == "__main__":
    asyncio.run(main())
