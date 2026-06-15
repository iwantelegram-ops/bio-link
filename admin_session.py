"""
admin_session.py
────────────────
Sistem sesi admin berbasis HMAC-SHA256.

Tujuan:
  Pastikan panel DM hanya bisa dipakai oleh user yang SAAT INI masih
  admin di grup — bukan mantan admin yang DM-nya masih terbuka.

Cara kerja:
  1. Saat admin menekan tombol "Kelola Grup" → open_session() dipanggil.
     Token HMAC dibuat dari (user_id, chat_id, timestamp) dan disimpan
     di memory + diverifikasi ulang ke Telegram API.
  2. Setiap callback sensitif memanggil verify_admin_session().
     - Jika token kedaluwarsa (default 1 jam) → sesi ditolak.
     - Jika user tidak lagi admin di Telegram → sesi dicabut + ditolak.
  3. on_admin_demoted(user_id, chat_id) dipanggil dari nexus_group.py
     saat Telegram melaporkan admin di-demosi → token langsung dihapus.

Keamanan:
  - Token HMAC-SHA256, kunci dari SESSION_SECRET env var.
  - Tidak ada state yang disimpan ke disk atau DB (in-memory only).
  - Token per-grup: satu user bisa punya sesi di banyak grup sekaligus.
"""

import os
import time
import hmac
import hashlib
from typing import Optional

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, PeerIdInvalid

# ── Konfigurasi ───────────────────────────────────────────────────────────────
_SECRET   = os.environ.get("SESSION_SECRET", "default-secret-GANTI-INI").encode()
_TTL      = 3600   # detik — token kedaluwarsa setelah 1 jam tidak aktif
_REFRESH  = 300    # detik — verifikasi ulang ke Telegram tiap 5 menit

# ── Store in-memory ───────────────────────────────────────────────────────────
# Format: _sessions[(user_id, chat_id)] = {
#   "token":      str,
#   "issued_at":  float,
#   "last_check": float,   # kapan terakhir Telegram API diverifikasi
# }
_sessions: dict[tuple[int, int], dict] = {}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _make_token(user_id: int, chat_id: int, ts: float) -> str:
    """Buat HMAC-SHA256 token dari (user_id, chat_id, timestamp)."""
    msg = f"{user_id}:{chat_id}:{ts}".encode()
    return hmac.new(_SECRET, msg, hashlib.sha256).hexdigest()


async def _is_still_admin(client: Client, user_id: int, chat_id: int) -> bool:
    """Tanya Telegram API secara langsung — apakah user masih admin?"""
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except (UserNotParticipant, ChatAdminRequired, PeerIdInvalid):
        return False
    except Exception:
        # Gagal jaringan dll — jangan langsung cabut sesi, biarkan TTL habis
        return True


# ── Public API ────────────────────────────────────────────────────────────────

async def open_session(client: Client, user_id: int, chat_id: int) -> bool:
    """
    Buka sesi baru untuk user di grup ini.

    Dipanggil saat admin memilih grup di panel (cb_manage).
    Verifikasi status admin ke Telegram sebelum membuat token.

    Return True jika berhasil, False jika user bukan admin.
    """
    if not await _is_still_admin(client, user_id, chat_id):
        return False

    ts = time.time()
    token = _make_token(user_id, chat_id, ts)
    _sessions[(user_id, chat_id)] = {
        "token":      token,
        "issued_at":  ts,
        "last_check": ts,
    }
    return True


async def verify_admin_session(
    client: Client,
    user_id: int,
    chat_id: int,
) -> bool:
    """
    Verifikasi sesi admin yang sedang aktif.

    Pemeriksaan:
      1. Token ada di _sessions.
      2. Token belum melewati TTL (_TTL detik sejak diterbitkan).
      3. Jika sudah _REFRESH detik sejak verifikasi terakhir → tanya Telegram.

    Return True jika valid, False jika harus ditolak.
    """
    key  = (user_id, chat_id)
    sess = _sessions.get(key)

    if not sess:
        return False

    now = time.time()

    # Cek TTL
    if now - sess["issued_at"] > _TTL:
        _sessions.pop(key, None)
        return False

    # Refresh periodik ke Telegram
    if now - sess["last_check"] > _REFRESH:
        if not await _is_still_admin(client, user_id, chat_id):
            _sessions.pop(key, None)
            return False
        sess["last_check"] = now

    return True


def on_admin_demoted(user_id: int, chat_id: int) -> None:
    """
    Cabut sesi secara paksa saat admin di-demosi di sebuah grup.

    Dipanggil dari nexus_group.py ChatMemberUpdated handler.
    Tidak perlu await — operasi synchronous sederhana.
    """
    _sessions.pop((user_id, chat_id), None)


async def start_cleanup_task() -> None:
    """
    Background task: hapus sesi kedaluwarsa setiap 10 menit.
    Panggil sekali dari antigcast.py main() dengan asyncio.create_task().
    """
    import asyncio
    while True:
        await asyncio.sleep(600)
        now     = time.time()
        expired = [k for k, v in _sessions.items() if now - v["issued_at"] > _TTL]
        for k in expired:
            _sessions.pop(k, None)
        if expired:
            print(f"[admin_session] cleanup: {len(expired)} sesi kedaluwarsa dihapus.")
