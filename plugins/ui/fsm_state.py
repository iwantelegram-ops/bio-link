"""
plugins/ui/fsm_state.py
────────────────────────
Satu tempat untuk semua dict FSM state DAN timeout helper.

Perbaikan:
  - Setiap FSM entry menyimpan asyncio.Task timeout-nya sendiri
  - Cancel task lama otomatis sebelum set state baru
  - Helper cancel_fsm_for_user() dipakai bersama handlers_dm & handlers_fsm
"""

import asyncio
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

# ── State dicts ───────────────────────────────────────────────────────────────
# Setiap entry: { "action": ..., "chat_id": int, "msg_id": int, "_task": Task }

pending_regex_state: dict[int, dict] = {}
pending_free_state:  dict[int, dict] = {}
pending_wl_state:    dict[int, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cancel_task(state: dict | None):
    """Cancel timeout task yang tersimpan di state, jika ada."""
    if state and "_task" in state:
        task: asyncio.Task = state["_task"]
        if task is not None and not task.done():
            task.cancel()


def clear_all_fsm(user_id: int):
    """Batalkan & bersihkan semua FSM aktif milik user ini."""
    _cancel_task(pending_regex_state.pop(user_id, None))
    _cancel_task(pending_free_state.pop(user_id, None))
    _cancel_task(pending_wl_state.pop(user_id, None))


def start_regex_fsm(user_id: int, chat_id: int, msg_id: int) -> asyncio.Task:
    """Daftarkan FSM regex; kembalikan task yang harus diisi caller."""
    clear_all_fsm(user_id)
    pending_regex_state[user_id] = {
        "action":  "add",
        "chat_id": chat_id,
        "msg_id":  msg_id,
        "_task":   None,   # diisi caller setelah create_task
    }
    return pending_regex_state[user_id]


def start_free_fsm(user_id: int, chat_id: int, msg_id: int) -> dict:
    clear_all_fsm(user_id)
    pending_free_state[user_id] = {
        "action":  "add",
        "chat_id": chat_id,
        "msg_id":  msg_id,
        "_task":   None,
    }
    return pending_free_state[user_id]


def start_wl_fsm(user_id: int, action: str, chat_id: int, msg_id: int) -> dict:
    clear_all_fsm(user_id)
    pending_wl_state[user_id] = {
        "action":  action,
        "chat_id": chat_id,
        "msg_id":  msg_id,
        "_task":   None,
    }
    return pending_wl_state[user_id]


# ── Timeout coroutines ────────────────────────────────────────────────────────

async def _regex_timeout_coro(user_id: int, chat_id: int, msg):
    await asyncio.sleep(30)
    if user_id not in pending_regex_state:
        return
    pending_regex_state.pop(user_id, None)
    try:
        await msg.edit(
            "<b>❖ ＴＩＭＥＯＵＴ ❖</b>\n\n"
            "Waktu pengisian habis. Sesi dibatalkan otomatis.\n\n"
            "<i>Tekan tombol di bawah untuk mengulang.</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙  Kembali ke Panduan", callback_data=f"rgxpanel_{chat_id}")]
            ]),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


async def free_fsm_timeout(user_id: int, chat_id: int, msg):
    await asyncio.sleep(30)
    if user_id not in pending_free_state:
        return
    pending_free_state.pop(user_id, None)
    try:
        await msg.edit(
            "<b>❖ ＴＩＭＥＯＵＴ ❖</b>\n\n"
            "Waktu pengisian habis. Sesi dibatalkan otomatis.\n\n"
            "<i>Tekan tombol di bawah untuk mengulang.</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙  Kembali ke Daftar VIP", callback_data=f"freelist_{chat_id}")]
            ]),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


async def _wl_timeout_coro(user_id: int, chat_id: int, msg):
    await asyncio.sleep(30)
    if user_id not in pending_wl_state:
        return
    pending_wl_state.pop(user_id, None)
    try:
        await msg.edit(
            "<b>❖ ＴＩＭＥＯＵＴ ❖</b>\n\n"
            "Waktu pengisian habis. Sesi dibatalkan otomatis.\n\n"
            "<i>Tekan tombol di bawah untuk mengulang.</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙  Kembali ke CAS Panel", callback_data=f"cas_panel_{chat_id}")]
            ]),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


def spawn_regex_timeout(user_id: int, chat_id: int, msg) -> asyncio.Task:
    task = asyncio.create_task(_regex_timeout_coro(user_id, chat_id, msg))
    if user_id in pending_regex_state:
        pending_regex_state[user_id]["_task"] = task
    return task


def spawn_free_timeout(user_id: int, chat_id: int, msg) -> asyncio.Task:
    task = asyncio.create_task(free_fsm_timeout(user_id, chat_id, msg))
    if user_id in pending_free_state:
        pending_free_state[user_id]["_task"] = task
    return task


def spawn_wl_timeout(user_id: int, chat_id: int, msg) -> asyncio.Task:
    task = asyncio.create_task(_wl_timeout_coro(user_id, chat_id, msg))
    if user_id in pending_wl_state:
        pending_wl_state[user_id]["_task"] = task
    return task
