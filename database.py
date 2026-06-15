"""
database.py — Multi-Backend Database Engine
─────────────────────────────────────────────
Otomatis memilih backend terbaik yang tersedia:

  Prioritas:
    1. MONGO_URL di .env  → MongoDB (via motor)
    2. SQLITE_PATH di .env → SQLite lokal (default Termux)

  Saat startup, bot mencoba koneksi MongoDB terlebih dahulu.
  Jika gagal (URL tidak ada / error jaringan / auth gagal),
  otomatis fallback ke SQLite tanpa crash.

  Log backend yang aktif muncul di terminal Termux saat start.

  Semua Collection API identik di kedua backend sehingga
  TIDAK ADA file lain yang perlu diubah.
"""

from __future__ import annotations

import os
import json
import time
import uuid
import asyncio
import aiosqlite
from datetime import datetime, timedelta, timezone
from pyrogram.enums import ChatMemberStatus
from dotenv import load_dotenv
from pathlib import Path as _Path

# Cari .env relatif ke file ini, bukan CWD — aman dijalankan dari direktori manapun
load_dotenv(dotenv_path=_Path(__file__).parent / ".env", override=False)

# ── CODE_BOT: namespace isolasi database per-bot ─────────────────────────────
# Semua nama collection akan di-prefix dengan CODE_BOT.
# Dua bot dengan CODE_BOT sama → pakai database yang sama (berbagi data).
# Dua bot dengan CODE_BOT beda di MongoDB/SQLite yang sama → koleksi terpisah, tidak campur.
# Jika CODE_BOT kosong → nama collection tidak di-prefix (perilaku lama).
import re as _re
_CODE_BOT_RAW = os.environ.get("CODE_BOT", "").strip()
_CODE_BOT     = _re.sub(r"[^a-zA-Z0-9]", "_", _CODE_BOT_RAW).lower().strip("_") if _CODE_BOT_RAW else ""

def _ns(name: str) -> str:
    """Tambahkan CODE_BOT prefix ke nama collection.
    Contoh: CODE_BOT=mybot → 'nexus_kalimat' jadi 'mybot_nexus_kalimat'
    """
    return f"{_CODE_BOT}_{name}" if _CODE_BOT else name


# ══════════════════════════════════════════════════════════════════════════════
# KONFIGURASI
# ══════════════════════════════════════════════════════════════════════════════

MONGO_URL            = os.environ.get("MONGO_URL", "").strip()

# ── Data directory: selalu di home user, berdasarkan CODE_BOT ─────────────────
# Format: ~/.nexusai/<CODE_BOT>/
# Dengan ini data SELALU ditemukan dari direktori manapun bot dijalankan,
# dan CODE_BOT yang sama selalu mengakses data yang sama.
_BOT_KEY    = _CODE_BOT if _CODE_BOT else "_default"
_DATA_DIR   = _Path.home() / ".nexusai" / _BOT_KEY
_DATA_DIR.mkdir(parents=True, exist_ok=True)

# MONGO_DB_NAME: dari .env jika diset, fallback ke "nexusai_<CODE_BOT>"
# Sehingga dua CODE_BOT berbeda otomatis pakai database MongoDB berbeda
_MONGO_DB_DEFAULT = f"nexusai_{_BOT_KEY}"
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "").strip() or _MONGO_DB_DEFAULT

# SQLITE_PATH: dari .env jika diset (harus path absolut),
# fallback ke ~/.nexusai/<CODE_BOT>/nexus_bot.db — selalu ditemukan
_SQLITE_DEFAULT = str(_DATA_DIR / "nexus_bot.db")
_SQLITE_ENV     = os.environ.get("SQLITE_PATH", "").strip()
SQLITE_PATH     = _SQLITE_ENV if _SQLITE_ENV else _SQLITE_DEFAULT

GLOBAL_EXPIRY        = 15
DEFAULT_LOCAL_EXPIRY = 3600
TZ_WIB               = timezone(timedelta(hours=7))

DEFAULT_CONFIG = {
    "local":     True,
    "global":    True,
    "expiry":    DEFAULT_LOCAL_EXPIRY,
    "bio_check": False,
}

# ── In-memory cache ────────────────────────────────────────────────────────────
_config_cache: dict[int, tuple[dict, float]] = {}
_admin_cache:  dict[tuple, tuple[bool, float]] = {}
CONFIG_TTL = 10
ADMIN_TTL  = 120

# ── Delete queue ───────────────────────────────────────────────────────────────
delete_queue: asyncio.Queue = asyncio.Queue()

# ── Handled messages tracker ──────────────────────────────────────────────────
_handled_msgs: dict[tuple[int, int], float] = {}
_HANDLED_TTL = 30.0

# ── Backend state ─────────────────────────────────────────────────────────────
_BACKEND: str = "sqlite"   # "mongo" | "sqlite"
_mongo_db = None           # motor database instance (jika aktif)
_sqlite_conn: aiosqlite.Connection | None = None


# ══════════════════════════════════════════════════════════════════════════════
# BACKEND DETECTION — dipanggil sekali di setup_db()
# ══════════════════════════════════════════════════════════════════════════════

async def _try_mongo(url: str, db_name: str):
    """
    Coba koneksi ke MongoDB. Return motor database object jika berhasil,
    None jika gagal. Timeout 5 detik agar tidak hang di Termux.
    """
    try:
        import dns.resolver
        dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
        dns.resolver.default_resolver.nameservers = ['1.1.1.1', '1.0.0.1']
        import motor.motor_asyncio as motor  # type: ignore
        client = motor.AsyncIOMotorClient(
            url,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=5000,
        )
        # Ping untuk memastikan koneksi benar-benar berhasil
        await client.admin.command("ping")
        return client[db_name]
    except ImportError:
        print("[DB] motor tidak terinstall — skip MongoDB")
        return None
    except Exception as e:
        print(f"[DB] MongoDB gagal: {e}")
        return None


async def _init_backend():
    """
    Tentukan backend aktif dan inisialisasi koneksi.
    Urutan: MongoDB → SQLite.
    """
    global _BACKEND, _mongo_db, _sqlite_conn

    # ── Coba MongoDB ──────────────────────────────────────────────────────────
    if MONGO_URL:
        print(f"[DB] 🔍 Mencoba koneksi MongoDB: {MONGO_URL[:40]}...")
        mongo = await _try_mongo(MONGO_URL, MONGO_DB_NAME)
        if mongo is not None:
            _BACKEND  = "mongo"
            _mongo_db = mongo
            print(f"[DB] ✅ BACKEND AKTIF: MongoDB  (db={MONGO_DB_NAME})")
            return
        print("[DB] ⚠️  MongoDB gagal → fallback ke SQLite")
    else:
        print("[DB] ℹ️  MONGO_URL tidak ditemukan di .env → pakai SQLite")

    # ── Fallback SQLite ───────────────────────────────────────────────────────
    _BACKEND = "sqlite"
    _sqlite_conn = await aiosqlite.connect(SQLITE_PATH, check_same_thread=False)
    await _sqlite_conn.execute("PRAGMA journal_mode=WAL")
    await _sqlite_conn.execute("PRAGMA synchronous=NORMAL")
    _sqlite_conn.row_factory = aiosqlite.Row
    abs_path = os.path.abspath(SQLITE_PATH)
    print(f"[DB] ✅ BACKEND AKTIF: SQLite     (file={abs_path})")


def get_active_backend() -> str:
    """Kembalikan nama backend aktif: 'mongo' atau 'sqlite'."""
    return _BACKEND


# ══════════════════════════════════════════════════════════════════════════════
# JSON ENCODER — handle datetime & bytes (untuk SQLite backend)
# ══════════════════════════════════════════════════════════════════════════════

class _Encoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return {"__dt__": obj.isoformat()}
        if isinstance(obj, bytes):
            return {"__bytes__": obj.hex()}
        return super().default(obj)


def _object_hook(obj: dict):
    if "__dt__" in obj:
        try:
            return datetime.fromisoformat(obj["__dt__"])
        except Exception:
            return obj
    if "__bytes__" in obj:
        try:
            return bytes.fromhex(obj["__bytes__"])
        except Exception:
            return obj
    return obj


def _dumps(obj) -> str:
    return json.dumps(obj, cls=_Encoder, ensure_ascii=False)


def _loads(s: str) -> dict:
    return json.loads(s, object_hook=_object_hook)


# ══════════════════════════════════════════════════════════════════════════════
# SQLITE HELPERS (internal)
# ══════════════════════════════════════════════════════════════════════════════

async def _get_sqlite() -> aiosqlite.Connection:
    global _sqlite_conn
    if _sqlite_conn is None:
        _sqlite_conn = await aiosqlite.connect(SQLITE_PATH, check_same_thread=False)
        await _sqlite_conn.execute("PRAGMA journal_mode=WAL")
        await _sqlite_conn.execute("PRAGMA synchronous=NORMAL")
        _sqlite_conn.row_factory = aiosqlite.Row
    return _sqlite_conn


def _tbl(name: str) -> str:
    return "col_" + name.replace("-", "_").replace(" ", "_")


async def _ensure_table(conn: aiosqlite.Connection, name: str):
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {_tbl(name)} (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT    UNIQUE,
            data   TEXT    NOT NULL
        )
    """)
    await conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# QUERY MATCHING — MongoDB-style (untuk SQLite backend)
# ══════════════════════════════════════════════════════════════════════════════

def _match(doc: dict, query: dict) -> bool:
    if not query:
        return True
    for key, val in query.items():
        doc_val = doc.get(key)
        if isinstance(val, dict):
            for op, op_val in val.items():
                if op == "$exists":
                    if bool(op_val) != (key in doc):
                        return False
                elif op == "$ne":
                    if doc_val == op_val:
                        return False
                elif op == "$gt":
                    if not (doc_val is not None and doc_val > op_val):
                        return False
                elif op == "$lt":
                    if not (doc_val is not None and doc_val < op_val):
                        return False
                elif op == "$in":
                    if doc_val not in op_val:
                        return False
        else:
            if doc_val != val:
                return False
    return True


def _apply_update(doc: dict, update: dict, is_insert: bool = False) -> dict:
    result = dict(doc)
    if "$set" in update:
        result.update(update["$set"])
    if "$setOnInsert" in update and is_insert:
        result.update(update["$setOnInsert"])
    if "$unset" in update:
        for k in update["$unset"]:
            result.pop(k, None)
    if "$push" in update:
        for k, v in update["$push"].items():
            if k not in result or not isinstance(result[k], list):
                result[k] = []
            result[k].append(v)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# RESULT OBJECTS
# ══════════════════════════════════════════════════════════════════════════════

class DeleteResult:
    def __init__(self, count: int = 0):
        self.deleted_count = count


class UpdateResult:
    def __init__(self, matched: int = 0, modified: int = 0, upserted_id=None):
        self.matched_count  = matched
        self.modified_count = modified
        self.upserted_id    = upserted_id


# ══════════════════════════════════════════════════════════════════════════════
# ASYNC CURSOR — mimic motor cursor API
# ══════════════════════════════════════════════════════════════════════════════

class AsyncCursor:
    """
    Unified cursor untuk SQLite dan MongoDB.
    SQLite: load semua data lalu filter in-memory.
    MongoDB: delegasi ke motor cursor dengan sort/skip/limit native.
    """

    def __init__(self, col_name: str, query: dict):
        self._col      = col_name
        self._query    = query
        self._sort_key: str | None = None
        self._sort_dir: int        = 1
        self._skip_n:   int        = 0
        self._limit_n:  int | None = None
        self._docs:     list[dict] | None = None
        self._pos:      int        = 0
        # MongoDB motor cursor (lazy)
        self._mongo_cur = None

    def sort(self, key: str, direction: int = 1) -> "AsyncCursor":
        self._sort_key = key
        self._sort_dir = direction
        return self

    def skip(self, n: int) -> "AsyncCursor":
        self._skip_n = n
        return self

    def limit(self, n: int) -> "AsyncCursor":
        self._limit_n = n
        return self

    # ── SQLite path ───────────────────────────────────────────────────────────
    async def _load_sqlite(self):
        conn = await _get_sqlite()
        tbl  = _tbl(self._col)
        await _ensure_table(conn, self._col)
        async with conn.execute(f"SELECT id, data FROM {tbl} ORDER BY id") as cur:
            rows = await cur.fetchall()
        docs = []
        for row in rows:
            try:
                d = _loads(row["data"])
                if "_id" not in d:
                    d["_id"] = str(row["id"])
                if _match(d, self._query):
                    docs.append(d)
            except Exception:
                pass
        if self._sort_key:
            docs.sort(
                key=lambda d: (d.get(self._sort_key) or ""),
                reverse=(self._sort_dir == -1),
            )
        docs = docs[self._skip_n:]
        if self._limit_n is not None:
            docs = docs[:self._limit_n]
        self._docs = docs

    # ── MongoDB path ──────────────────────────────────────────────────────────
    async def _load_mongo(self):
        col  = _mongo_db[self._col]
        cur  = col.find(self._query)
        if self._sort_key:
            cur = cur.sort(self._sort_key, self._sort_dir)
        if self._skip_n:
            cur = cur.skip(self._skip_n)
        if self._limit_n is not None:
            cur = cur.limit(self._limit_n)
        docs = []
        async for doc in cur:
            doc["_id"] = str(doc["_id"])
            docs.append(doc)
        self._docs = docs

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._docs is None:
            if _BACKEND == "mongo":
                await self._load_mongo()
            else:
                await self._load_sqlite()
        if self._pos >= len(self._docs):
            raise StopAsyncIteration
        doc       = self._docs[self._pos]
        self._pos += 1
        return doc

    async def to_list(self, length: int | None = None) -> list[dict]:
        if self._docs is None:
            if _BACKEND == "mongo":
                await self._load_mongo()
            else:
                await self._load_sqlite()
        if length is not None:
            return self._docs[:length]
        return list(self._docs)


# ══════════════════════════════════════════════════════════════════════════════
# COLLECTION — unified API untuk MongoDB dan SQLite
# ══════════════════════════════════════════════════════════════════════════════

class Collection:
    def __init__(self, name: str):
        self.name = name

    # ── find_one ──────────────────────────────────────────────────────────────

    async def find_one(self, query: dict = {}) -> dict | None:
        if _BACKEND == "mongo":
            try:
                doc = await _mongo_db[self.name].find_one(query)
                if doc:
                    doc["_id"] = str(doc["_id"])
                return doc
            except Exception as e:
                print(f"[DB:mongo] find_one error {self.name}: {e}")
                return None
        # SQLite
        conn = await _get_sqlite()
        tbl  = _tbl(self.name)
        await _ensure_table(conn, self.name)
        async with conn.execute(f"SELECT id, data FROM {tbl} ORDER BY id") as cur:
            rows = await cur.fetchall()
        for row in rows:
            try:
                d = _loads(row["data"])
                if "_id" not in d:
                    d["_id"] = str(row["id"])
                if _match(d, query):
                    return d
            except Exception:
                pass
        return None

    # ── find ──────────────────────────────────────────────────────────────────

    def find(self, query: dict = {}) -> AsyncCursor:
        return AsyncCursor(self.name, query)

    # ── update_one ────────────────────────────────────────────────────────────

    async def update_one(
        self, filter_q: dict, update: dict, upsert: bool = False
    ) -> UpdateResult:
        if _BACKEND == "mongo":
            try:
                r = await _mongo_db[self.name].update_one(filter_q, update, upsert=upsert)
                return UpdateResult(r.matched_count, r.modified_count, str(r.upserted_id) if r.upserted_id else None)
            except Exception as e:
                print(f"[DB:mongo] update_one error {self.name}: {e}")
                return UpdateResult()
        # SQLite
        conn = await _get_sqlite()
        tbl  = _tbl(self.name)
        await _ensure_table(conn, self.name)
        found_id, found_doc = None, None
        async with conn.execute(f"SELECT id, data FROM {tbl} ORDER BY id") as cur:
            rows = await cur.fetchall()
        for row in rows:
            try:
                d = _loads(row["data"])
                if "_id" not in d:
                    d["_id"] = str(row["id"])
                if _match(d, filter_q):
                    found_id  = row["id"]
                    found_doc = d
                    break
            except Exception:
                pass
        if found_doc is not None:
            new_doc = _apply_update(found_doc, update, is_insert=False)
            await conn.execute(f"UPDATE {tbl} SET data=? WHERE id=?", (_dumps(new_doc), found_id))
            await conn.commit()
            return UpdateResult(matched=1, modified=1)
        if upsert:
            new_doc = {}
            new_doc.update(filter_q)
            new_doc = _apply_update(new_doc, update, is_insert=True)
            doc_id  = str(new_doc.get("_id") or uuid.uuid4().hex)
            new_doc["_id"] = doc_id
            await conn.execute(
                f"INSERT OR REPLACE INTO {tbl} (doc_id, data) VALUES (?, ?)",
                (doc_id, _dumps(new_doc))
            )
            await conn.commit()
            return UpdateResult(matched=0, modified=0, upserted_id=doc_id)
        return UpdateResult()

    # ── update_many ───────────────────────────────────────────────────────────

    async def update_many(self, filter_q: dict, update: dict) -> UpdateResult:
        if _BACKEND == "mongo":
            try:
                r = await _mongo_db[self.name].update_many(filter_q, update)
                return UpdateResult(r.matched_count, r.modified_count)
            except Exception as e:
                print(f"[DB:mongo] update_many error {self.name}: {e}")
                return UpdateResult()
        # SQLite
        conn = await _get_sqlite()
        tbl  = _tbl(self.name)
        await _ensure_table(conn, self.name)
        async with conn.execute(f"SELECT id, data FROM {tbl}") as cur:
            rows = await cur.fetchall()
        modified = 0
        for row in rows:
            try:
                d = _loads(row["data"])
                if "_id" not in d:
                    d["_id"] = str(row["id"])
                if not filter_q or _match(d, filter_q):
                    new_doc = _apply_update(d, update, is_insert=False)
                    await conn.execute(f"UPDATE {tbl} SET data=? WHERE id=?", (_dumps(new_doc), row["id"]))
                    modified += 1
            except Exception:
                pass
        if modified:
            await conn.commit()
        return UpdateResult(matched=modified, modified=modified)

    # ── insert_one ────────────────────────────────────────────────────────────

    async def insert_one(self, doc: dict) -> UpdateResult:
        if _BACKEND == "mongo":
            try:
                d = dict(doc)
                d.pop("_id", None)
                r = await _mongo_db[self.name].insert_one(d)
                return UpdateResult(upserted_id=str(r.inserted_id))
            except Exception as e:
                print(f"[DB:mongo] insert_one error {self.name}: {e}")
                return UpdateResult()
        # SQLite
        conn   = await _get_sqlite()
        tbl    = _tbl(self.name)
        await _ensure_table(conn, self.name)
        doc_id = str(doc.get("_id") or uuid.uuid4().hex)
        d      = dict(doc)
        d["_id"] = doc_id
        try:
            await conn.execute(
                f"INSERT OR IGNORE INTO {tbl} (doc_id, data) VALUES (?, ?)",
                (doc_id, _dumps(d))
            )
            await conn.commit()
        except Exception:
            pass
        return UpdateResult(upserted_id=doc_id)

    # ── delete_one ────────────────────────────────────────────────────────────

    async def delete_one(self, query: dict) -> DeleteResult:
        if _BACKEND == "mongo":
            try:
                r = await _mongo_db[self.name].delete_one(query)
                return DeleteResult(r.deleted_count)
            except Exception as e:
                print(f"[DB:mongo] delete_one error {self.name}: {e}")
                return DeleteResult()
        # SQLite
        conn = await _get_sqlite()
        tbl  = _tbl(self.name)
        await _ensure_table(conn, self.name)
        async with conn.execute(f"SELECT id, data FROM {tbl} ORDER BY id") as cur:
            rows = await cur.fetchall()
        for row in rows:
            try:
                d = _loads(row["data"])
                if "_id" not in d:
                    d["_id"] = str(row["id"])
                if _match(d, query):
                    await conn.execute(f"DELETE FROM {tbl} WHERE id=?", (row["id"],))
                    await conn.commit()
                    return DeleteResult(1)
            except Exception:
                pass
        return DeleteResult(0)

    # ── delete_many ───────────────────────────────────────────────────────────

    async def delete_many(self, query: dict = {}) -> DeleteResult:
        if _BACKEND == "mongo":
            try:
                r = await _mongo_db[self.name].delete_many(query)
                return DeleteResult(r.deleted_count)
            except Exception as e:
                print(f"[DB:mongo] delete_many error {self.name}: {e}")
                return DeleteResult()
        # SQLite
        conn = await _get_sqlite()
        tbl  = _tbl(self.name)
        await _ensure_table(conn, self.name)
        async with conn.execute(f"SELECT id, data FROM {tbl}") as cur:
            rows = await cur.fetchall()
        to_del = []
        for row in rows:
            try:
                d = _loads(row["data"])
                if "_id" not in d:
                    d["_id"] = str(row["id"])
                if not query or _match(d, query):
                    to_del.append(row["id"])
            except Exception:
                to_del.append(row["id"])
        for rid in to_del:
            await conn.execute(f"DELETE FROM {tbl} WHERE id=?", (rid,))
        if to_del:
            await conn.commit()
        return DeleteResult(len(to_del))

    # ── insert_many ───────────────────────────────────────────────────────────

    async def insert_many(self, docs: list[dict]) -> None:
        if not docs:
            return
        if _BACKEND == "mongo":
            try:
                clean = [{k: v for k, v in d.items() if k != "_id"} for d in docs]
                await _mongo_db[self.name].insert_many(clean, ordered=False)
            except Exception as e:
                print(f"[DB:mongo] insert_many error {self.name}: {e}")
            return
        # SQLite
        conn = await _get_sqlite()
        tbl  = _tbl(self.name)
        await _ensure_table(conn, self.name)
        for doc in docs:
            doc_id = str(doc.get("_id") or uuid.uuid4().hex)
            d      = dict(doc)
            d["_id"] = doc_id
            try:
                await conn.execute(
                    f"INSERT OR IGNORE INTO {tbl} (doc_id, data) VALUES (?, ?)",
                    (doc_id, _dumps(d))
                )
            except Exception:
                pass
        await conn.commit()

    # ── count_documents ───────────────────────────────────────────────────────

    async def count_documents(self, query: dict = {}) -> int:
        if _BACKEND == "mongo":
            try:
                if query:
                    return await _mongo_db[self.name].count_documents(query)
                return await _mongo_db[self.name].estimated_document_count()
            except Exception as e:
                print(f"[DB:mongo] count_documents error {self.name}: {e}")
                return 0
        # SQLite
        conn = await _get_sqlite()
        tbl  = _tbl(self.name)
        await _ensure_table(conn, self.name)
        if not query:
            async with conn.execute(f"SELECT COUNT(*) FROM {tbl}") as cur:
                row = await cur.fetchone()
                return row[0] if row else 0
        async with conn.execute(f"SELECT data FROM {tbl}") as cur:
            rows = await cur.fetchall()
        return sum(
            1 for row in rows
            if _match(_loads(row["data"]), query)
        )

    # ── create_index ──────────────────────────────────────────────────────────

    async def create_index(
        self,
        keys,
        unique: bool = False,
        sparse: bool = False,
        expireAfterSeconds: int | None = None,
    ):
        """
        SQLite: no-op (tidak perlu index eksplisit).
        MongoDB: buat index asli via motor.
        """
        if _BACKEND == "mongo":
            try:
                from pymongo import ASCENDING, DESCENDING  # type: ignore
                if isinstance(keys, str):
                    keys = [(keys, ASCENDING)]
                await _mongo_db[self.name].create_index(
                    keys,
                    unique=unique,
                    sparse=sparse,
                    expireAfterSeconds=expireAfterSeconds,
                )
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# DB — dict-like container, mimic motor client["db"]["collection"]
# ══════════════════════════════════════════════════════════════════════════════

class DB:
    def __getitem__(self, name: str) -> Collection:
        return Collection(_ns(name))


db = DB()

# ── Named collections (backward compat) ───────────────────────────────────────
config_db          = db["status"]
messages_db        = db["seen_messages"]
regex_db           = db["regex_list"]
nexus_kalimat_db   = db["nexus_kalimat"]
nexus_regex_db     = db["nexus_regex"]
nexus_grup_db      = db["nexus_grup"]
nexus_whitelist_db = db["nexus_whitelist"]
nexus_actlog_db    = db["nexus_actlog"]
group_action_log_db = db["group_action_log"]
bot_config_db      = db["bot_config"]


# ══════════════════════════════════════════════════════════════════════════════
# HANDLED MESSAGES TRACKER
# ══════════════════════════════════════════════════════════════════════════════

def mark_message_handled(chat_id: int, msg_id: int) -> None:
    _handled_msgs[(chat_id, msg_id)] = time.time()
    if len(_handled_msgs) > 2000:
        cutoff = time.time() - _HANDLED_TTL
        stale  = [k for k, ts in _handled_msgs.items() if ts < cutoff]
        for k in stale:
            _handled_msgs.pop(k, None)


def is_message_handled(chat_id: int, msg_id: int) -> bool:
    key = (chat_id, msg_id)
    ts  = _handled_msgs.get(key)
    if ts is None:
        return False
    if time.time() - ts > _HANDLED_TTL:
        _handled_msgs.pop(key, None)
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# SETUP — init backend + tabel + background cleanup
# ══════════════════════════════════════════════════════════════════════════════

async def _cleanup_seen_messages():
    """Background task: hapus seen_messages lebih dari 24 jam, jalan setiap 1 jam."""
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            break
        try:
            cutoff = time.time() - 86400
            if _BACKEND == "mongo":
                # PENTING: gunakan _ns() agar cleanup hanya menyentuh namespace CODE_BOT yang aktif
                await _mongo_db[_ns("seen_messages")].delete_many({"time": {"$lt": cutoff}})
            else:
                conn = await _get_sqlite()
                # _ns() sudah diterapkan saat tabel dibuat di setup_db(); pakai nama yang sama
                ns_col = _ns("seen_messages")
                tbl    = _tbl(ns_col)
                await _ensure_table(conn, ns_col)
                async with conn.execute(f"SELECT id, data FROM {tbl}") as cur:
                    rows = await cur.fetchall()
                deleted = 0
                for row in rows:
                    try:
                        d  = _loads(row["data"])
                        ts = d.get("time", 0)
                        if isinstance(ts, (int, float)) and ts < cutoff:
                            await conn.execute(f"DELETE FROM {tbl} WHERE id=?", (row["id"],))
                            deleted += 1
                    except Exception:
                        pass
                if deleted:
                    await conn.commit()
                    prefix = f"[{_CODE_BOT}] " if _CODE_BOT else ""
                    print(f"[DB] {prefix}cleanup: {deleted} seen_messages expired dihapus")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[DB] cleanup error: {e}")



async def _migrate_legacy_data():
    """
    Migrasi otomatis data lama (tanpa CODE_BOT prefix) ke namespace aktif.
    Dijalankan sekali saat startup jika CODE_BOT aktif.
    Hanya menyalin dokumen yang BELUM ada di namespace baru — tidak menimpa.
    Aman dijalankan berulang kali.
    """
    if not _CODE_BOT:
        return

    # Daftar semua collection yang perlu dicek
    _COLLECTIONS = [
        "status", "seen_messages", "regex_list",
        "regex_per_group", "whitelist_per_group", "free_per_group",
        "nexus_kalimat", "nexus_regex", "nexus_grup", "nexus_whitelist",
        "nexus_actlog", "local_mute", "group_action_log",
        "ai_debug_log", "dm_users", "nexus_ai_model", "bot_config",
    ]

    migrated_total = 0

    if _BACKEND == "mongo":
        for col_name in _COLLECTIONS:
            old_col = _mongo_db[col_name]          # collection lama tanpa prefix
            new_col = _mongo_db[_ns(col_name)]     # collection baru dengan prefix

            # Skip jika nama sama (tidak ada prefix)
            if col_name == _ns(col_name):
                continue

            try:
                old_count = await old_col.count_documents({})
                if old_count == 0:
                    continue

                new_count = await new_col.count_documents({})
                if new_count > 0:
                    # Sudah ada data di namespace baru, skip
                    continue

                # Copy semua dokumen dari old ke new
                docs = []
                async for doc in old_col.find({}):
                    docs.append(doc)

                if docs:
                    try:
                        await new_col.insert_many(docs, ordered=False)
                        migrated_total += len(docs)
                        print(f"[Migrasi] {col_name} → {_ns(col_name)}: {len(docs)} dokumen dipindah")
                    except Exception as e:
                        print(f"[Migrasi] {col_name}: sebagian gagal ({e})")

            except Exception as e:
                print(f"[Migrasi] Error cek {col_name}: {e}")

    elif _BACKEND == "sqlite":
        conn = await _get_sqlite()
        for col_name in _COLLECTIONS:
            new_col = _ns(col_name)
            if col_name == new_col:
                continue

            try:
                # Cek apakah tabel lama ada
                async with conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (_tbl(col_name),)
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    continue

                # Cek jumlah data di tabel lama
                async with conn.execute(f"SELECT COUNT(*) FROM {_tbl(col_name)}") as cur:
                    old_count = (await cur.fetchone())[0]
                if old_count == 0:
                    continue

                # Cek tabel baru sudah ada data?
                await _ensure_table(conn, new_col)
                async with conn.execute(f"SELECT COUNT(*) FROM {_tbl(new_col)}") as cur:
                    new_count = (await cur.fetchone())[0]
                if new_count > 0:
                    continue

                # Copy
                async with conn.execute(f"SELECT data FROM {_tbl(col_name)}") as cur:
                    rows = await cur.fetchall()

                for row in rows:
                    try:
                        await conn.execute(
                            f"INSERT OR IGNORE INTO {_tbl(new_col)} (id, data) VALUES (?, ?)",
                            (str(uuid.uuid4()), row["data"] if isinstance(row, dict) else row[0])
                        )
                    except Exception:
                        pass

                await conn.commit()
                migrated_total += old_count
                print(f"[Migrasi] SQLite {col_name} → {new_col}: {old_count} baris dipindah")

            except Exception as e:
                print(f"[Migrasi] SQLite error {col_name}: {e}")

    if migrated_total > 0:
        print(f"[Migrasi] ✅ Total {migrated_total} dokumen berhasil dimigrasikan ke namespace [{_CODE_BOT}]")
    else:
        print(f"[Migrasi] ✅ Namespace [{_CODE_BOT}] sudah up-to-date, tidak ada migrasi diperlukan")


async def _migrate_sqlite_to_mongo():
    """
    Migrasi data dari SQLite lokal ke MongoDB saat backend aktif adalah MongoDB
    dan file SQLite lokal masih ada dan berisi data.

    Alur:
      1. Cek apakah file SQLite ada dan tidak kosong.
      2. Untuk setiap collection, ambil semua dokumen dari SQLite.
      3. Untuk setiap dokumen, cek apakah sudah ada di MongoDB (berdasarkan _id atau doc_id).
         - Jika belum ada → insert ke MongoDB.
         - Jika sudah ada (duplikat) → skip (data MongoDB diutamakan).
      4. Setelah semua collection selesai dan SQLite sudah kosong total → log selesai.
    """
    import os as _os
    import json as _json

    if _BACKEND != "mongo" or _mongo_db is None:
        return  # Hanya jalan jika backend aktif adalah MongoDB

    sqlite_path = SQLITE_PATH
    if not _os.path.exists(sqlite_path):
        return  # Tidak ada file SQLite, skip

    # Cek apakah file SQLite punya data sama sekali
    try:
        import aiosqlite as _aiosqlite
        async with _aiosqlite.connect(sqlite_path) as sq:
            sq.row_factory = _aiosqlite.Row
            async with sq.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = [r[0] for r in await cur.fetchall()]
        if not tables:
            return  # SQLite kosong, skip
    except Exception as e:
        print(f"[Migrasi SQLite→Mongo] Gagal buka SQLite: {e}")
        return

    _COLLECTIONS = [
        "status", "seen_messages", "regex_list",
        "regex_per_group", "whitelist_per_group", "free_per_group",
        "nexus_kalimat", "nexus_regex", "nexus_grup", "nexus_whitelist",
        "local_mute", "group_action_log",
        "nexus_actlog", "ai_debug_log", "dm_users", "nexus_ai_model", "bot_config",
    ]

    total_migrated = 0
    total_skipped  = 0

    print("[Migrasi SQLite→Mongo] 🔄 Ditemukan data SQLite lokal, mulai migrasi...")

    try:
        async with _aiosqlite.connect(sqlite_path) as sq:
            sq.row_factory = _aiosqlite.Row

            for col_name in _COLLECTIONS:
                # Coba kedua kemungkinan nama tabel: dengan prefix dan tanpa prefix
                candidates = list({_tbl(_ns(col_name)), _tbl(col_name)})
                for tbl in candidates:
                    # Cek apakah tabel ini ada di SQLite
                    async with sq.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
                    ) as cur:
                        exists = await cur.fetchone()
                    if not exists:
                        continue

                    async with sq.execute(f"SELECT id, doc_id, data FROM {tbl}") as cur:
                        rows = await cur.fetchall()

                    if not rows:
                        continue

                    mongo_col = _mongo_db[_ns(col_name)]
                    inserted  = 0
                    skipped   = 0

                    for row in rows:
                        try:
                            raw = row["data"] if "data" in row.keys() else None
                            if not raw:
                                continue
                            doc = _json.loads(raw) if isinstance(raw, str) else raw

                            # Tentukan _id untuk cek duplikat
                            doc_id = row["doc_id"] if "doc_id" in row.keys() else None
                            if doc_id:
                                doc.setdefault("_id", doc_id)

                            filter_q = {"_id": doc["_id"]} if "_id" in doc else None

                            if filter_q:
                                existing = await mongo_col.find_one(filter_q)
                                if existing:
                                    skipped += 1
                                    continue

                            await mongo_col.insert_one(doc)
                            inserted += 1

                        except Exception:
                            skipped += 1
                            continue

                    if inserted:
                        print(f"[Migrasi SQLite→Mongo] ✅ {tbl} → {_ns(col_name)}: {inserted} dokumen dipindah, {skipped} duplikat dilewati")
                    total_migrated += inserted
                    total_skipped  += skipped

    except Exception as e:
        print(f"[Migrasi SQLite→Mongo] ❌ Error: {e}")
        return

    if total_migrated > 0:
        print(f"[Migrasi SQLite→Mongo] ✅ Selesai. Total {total_migrated} dokumen dipindah, {total_skipped} duplikat dilewati.")
        print(f"[Migrasi SQLite→Mongo] ℹ️  File SQLite ({sqlite_path}) tetap ada sebagai backup.")
        print(f"[Migrasi SQLite→Mongo] ℹ️  Hapus manual jika sudah yakin data aman di MongoDB.")
    else:
        print(f"[Migrasi SQLite→Mongo] ✅ Semua data SQLite sudah ada di MongoDB ({total_skipped} duplikat). Tidak ada yang perlu dipindah.")


async def setup_db():
    """
    Inisialisasi backend (MongoDB atau SQLite) dan mulai background cleanup.
    Wajib dipanggil sekali di antigcast.py saat startup.
    """
    await _init_backend()

    if _BACKEND == "sqlite":
        conn = await _get_sqlite()
        for col_name in [
            "status", "seen_messages", "regex_list",
            "regex_per_group", "whitelist_per_group", "free_per_group",
            "nexus_kalimat", "nexus_regex", "nexus_grup", "nexus_whitelist",
            "local_mute", "group_action_log",
            "nexus_actlog", "ai_debug_log", "dm_users", "nexus_ai_model", "bot_config",
            "security_os", "security_os_monitors",
        ]:
            await _ensure_table(conn, _ns(col_name))

    # ── Migrasi SQLite lokal → MongoDB (jika backend aktif Mongo & SQLite ada) ─
    await _migrate_sqlite_to_mongo()

    asyncio.create_task(_cleanup_seen_messages())

    # ── Migrasi data lama (tanpa CODE_BOT prefix) ke namespace aktif ─────────
    if _CODE_BOT:
        await _migrate_legacy_data()

    # ── Banner detail startup ─────────────────────────────────────────────────
    sep = "─" * 52
    print(f"\n╔{sep}╗")
    print(f"║{'  DATABASE INFO':^52}║")
    print(f"╠{sep}╣")

    if _BACKEND == "mongo":
        url_display = MONGO_URL[:45] + "…" if len(MONGO_URL) > 45 else MONGO_URL
        print(f"║  Backend   : MongoDB (cloud/server)              ║")
        print(f"║  URL       : {url_display:<39}║")
        print(f"║  DB Name   : {MONGO_DB_NAME:<39}║")
    else:
        abs_path = os.path.abspath(SQLITE_PATH)
        path_display = abs_path[-45:] if len(abs_path) > 45 else abs_path
        print(f"║  Backend   : SQLite (lokal / Termux)             ║")
        print(f"║  File      : {path_display:<39}║")

    print(f"╠{sep}╣")

    if _CODE_BOT:
        print(f"║  CODE_BOT  : [{_CODE_BOT}]")
        print(f"║  Namespace : semua koleksi pakai prefix [{_CODE_BOT}_…]")
        print(f"║  Akses DB  : bot lain dengan CODE_BOT sama")
        print(f"║              → berbagi data yang sama ✅")
        print(f"║              bot lain dengan CODE_BOT beda")
        print(f"║              → data terpisah, tidak campur ✅")
    else:
        print(f"║  CODE_BOT  : (tidak diset / kosong)")
        print(f"║  ⚠️  PERINGATAN: tanpa CODE_BOT, semua bot yang")
        print(f"║     pakai DB yang sama akan BERBAGI data!")
        print(f"║     Isi CODE_BOT di .env untuk isolasi data.")

    print(f"╚{sep}╝\n")


async def save_bot_config(key: str, value) -> None:
    """
    Simpan setting bot ke DB secara persisten.
    Dipakai untuk cache info channel (title, username) agar dikenal lintas sesi.
    """
    try:
        await bot_config_db.update_one(
            {"_id": key},
            {"$set": {"_id": key, "value": value}},
            upsert=True,
        )
    except Exception as e:
        print(f"[DB] save_bot_config error ({key}): {e}")


async def get_bot_config(key: str, default=None):
    """Ambil setting bot dari DB. Return default jika tidak ada."""
    try:
        doc = await bot_config_db.find_one({"_id": key})
        if doc is not None:
            return doc.get("value", default)
    except Exception as e:
        print(f"[DB] get_bot_config error ({key}): {e}")
    return default


async def reset_code_bot_data(code_bot: str) -> tuple[int, list[str]]:
    """
    Hapus semua data dari namespace CODE_BOT yang diberikan.
    Cocok untuk perintah /reset — membersihkan SEMUA data satu bot.

    Return: (total_dokumen_dihapus, daftar_koleksi_yang_dibersihkan)
    """
    import re as _re2
    safe   = _re2.sub(r"[^a-zA-Z0-9]", "_", code_bot.strip()).lower().strip("_")
    prefix = f"{safe}_" if safe else ""

    _ALL_COLS = [
        "status", "seen_messages", "regex_list",
        "regex_per_group", "whitelist_per_group", "free_per_group",
        "nexus_kalimat", "nexus_regex", "nexus_grup", "nexus_whitelist",
        "nexus_actlog", "local_mute", "group_action_log",
        "ai_debug_log", "dm_users", "nexus_ai_model", "bot_config",
    ]

    cleared: list[str] = []
    total   = 0

    if _BACKEND == "mongo":
        for col_name in _ALL_COLS:
            ns = f"{prefix}{col_name}" if prefix else col_name
            try:
                r = await _mongo_db[ns].delete_many({})
                if r.deleted_count > 0:
                    total += r.deleted_count
                    cleared.append(f"{ns} ({r.deleted_count})")
            except Exception as e:
                print(f"[reset] MongoDB error {ns}: {e}")

    elif _BACKEND == "sqlite":
        conn = await _get_sqlite()
        for col_name in _ALL_COLS:
            ns  = f"{prefix}{col_name}" if prefix else col_name
            tbl = _tbl(ns)
            try:
                async with conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    continue
                async with conn.execute(f"SELECT COUNT(*) FROM {tbl}") as cur:
                    cnt = (await cur.fetchone())[0]
                await conn.execute(f"DELETE FROM {tbl}")
                if cnt > 0:
                    total += cnt
                    cleared.append(f"{ns} ({cnt})")
            except Exception as e:
                print(f"[reset] SQLite error {ns}: {e}")
        await conn.commit()

    # Bersihkan cache in-memory jika namespace aktif yang direset
    if safe == _CODE_BOT:
        _config_cache.clear()
        _admin_cache.clear()

    return total, cleared


async def close_db():
    """Tutup koneksi database dengan bersih saat shutdown."""
    global _sqlite_conn, _mongo_db
    if _BACKEND == "sqlite" and _sqlite_conn is not None:
        try:
            await _sqlite_conn.close()
            _sqlite_conn = None
            print("[DB] SQLite connection ditutup.")
        except Exception as e:
            print(f"[DB] Error tutup SQLite: {e}")
    elif _BACKEND == "mongo" and _mongo_db is not None:
        try:
            _mongo_db.client.close()
            _mongo_db = None
            print("[DB] MongoDB connection ditutup.")
        except Exception as e:
            print(f"[DB] Error tutup MongoDB: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def get_config(chat_id: int) -> dict:
    now = time.monotonic()
    hit = _config_cache.get(chat_id)
    if hit and (now - hit[1]) < CONFIG_TTL:
        return hit[0]
    doc = await config_db.find_one({"chat_id": chat_id})
    cfg = dict(DEFAULT_CONFIG)
    if doc:
        for k in DEFAULT_CONFIG:
            if k in doc:
                cfg[k] = doc[k]
    _config_cache[chat_id] = (cfg, now)
    return cfg


async def update_config(chat_id: int, key: str, value) -> None:
    await config_db.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, key: value}},
        upsert=True,
    )
    _config_cache.pop(chat_id, None)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN CACHE
# ══════════════════════════════════════════════════════════════════════════════

async def is_admin(client, chat_id: int, user_id) -> bool:
    if not user_id:
        return False
    now = time.monotonic()
    key = (chat_id, user_id)
    hit = _admin_cache.get(key)
    if hit and (now - hit[1]) < ADMIN_TTL:
        return hit[0]
    try:
        member = await client.get_chat_member(chat_id, user_id)
        result = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        result = False
    _admin_cache[key] = (result, now)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# AUTO DELETE / DELETE WORKER
# ══════════════════════════════════════════════════════════════════════════════

async def auto_delete_reply(msgs: list, delay: int = 5) -> None:
    await asyncio.sleep(delay)
    for m in msgs:
        try:
            await m.delete()
        except Exception:
            pass


async def delete_worker(client) -> None:
    # Tunggu sampai client benar-benar terkoneksi sebelum mulai memproses
    for _ in range(60):
        if getattr(client, "is_connected", False):
            break
        await asyncio.sleep(1.0)

    pending: dict[int, list[int]] = {}

    async def flush():
        if not getattr(client, "is_connected", False):
            return
        failed: dict[int, list[int]] = {}
        for cid, mids in list(pending.items()):
            if not mids:
                continue
            try:
                await client.delete_messages(cid, mids)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Simpan kembali agar dicoba lagi di iterasi berikutnya
                failed[cid] = mids
        pending.clear()
        pending.update(failed)

    while True:
        try:
            cid, mids = await asyncio.wait_for(delete_queue.get(), timeout=0.3)
            pending.setdefault(cid, []).extend(mids)
            delete_queue.task_done()
            while not delete_queue.empty():
                try:
                    cid2, mids2 = delete_queue.get_nowait()
                    pending.setdefault(cid2, []).extend(mids2)
                    delete_queue.task_done()
                except asyncio.QueueEmpty:
                    break
            await flush()
        except asyncio.TimeoutError:
            if pending:
                await flush()
        except asyncio.CancelledError:
            # Flush sisa sebelum berhenti
            if pending:
                try:
                    await flush()
                except Exception:
                    pass
            break
        except Exception:
            # Cegah worker mati diam-diam akibat exception tak terduga
            await asyncio.sleep(0.5)


# ══════════════════════════════════════════════════════════════════════════════
# GROUP HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def get_my_admin_groups(client, user_id: int) -> list:
    """
    Kembalikan semua GRUP (bukan channel) dari config_db (berbagi via CODE_BOT).
    Menggunakan judul tersimpan jika bot token ini tidak ada di grup tersebut,
    sehingga dua bot dengan CODE_BOT yang sama melihat daftar grup yang sama.

    FIX: simpan chat_type saat bisa akses → filter channel saat tidak bisa akses.
    """
    from pyrogram.enums import ChatType
    result = []
    async for doc in config_db.find({}):
        chat_id = doc.get("chat_id")
        if not chat_id:
            continue
        title = doc.get("title") or str(chat_id)
        chat_accessible = False
        try:
            chat = await client.get_chat(chat_id)
            if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
                # Channel atau tipe lain → simpan tipe agar filter bekerja saat offline
                await config_db.update_one(
                    {"chat_id": chat_id},
                    {"$set": {"chat_type": chat.type.name}},
                )
                continue
            title = chat.title or title
            chat_accessible = True
            # Perbarui judul + chat_type tersimpan di database
            await config_db.update_one(
                {"chat_id": chat_id},
                {"$set": {"title": title, "chat_type": chat.type.name}},
            )
        except Exception:
            # Bot tidak bisa akses chat (sesi baru / bot lain) — periksa tipe tersimpan
            stored_type = (doc.get("chat_type") or "").upper()
            if stored_type == "CHANNEL":
                continue  # skip channel yang tersimpan di DB
            if not doc.get("title"):
                continue  # belum ada judul tersimpan, lewati

            # FIX Bug 1: Verifikasi apakah bot masih anggota grup.
            # Jika bot sudah dikeluarkan (UserNotParticipant / ChannelPrivate /
            # ChatForbidden), hapus data dari DB agar grup tidak muncul lagi.
            try:
                from pyrogram.errors import (
                    UserNotParticipant, ChannelPrivate, ChatForbidden,
                    ChatIdInvalid, PeerIdInvalid,
                )
                me = await client.get_me()
                await client.get_chat_member(chat_id, me.id)
                # Berhasil → bot masih di grup, lanjutkan
            except Exception as _ve:
                _err_cls = type(_ve).__name__
                if _err_cls in (
                    "UserNotParticipant", "ChannelPrivate", "ChatForbidden",
                    "ChatIdInvalid", "PeerIdInvalid",
                ):
                    # Bot sudah tidak ada di grup → bersihkan DB
                    await remove_group_data(chat_id)
                    continue
                # Error lain (jaringan, FloodWait, dsb.) → percayai data DB

        if chat_accessible:
            if await is_admin(client, chat_id, user_id):
                result.append({"id": chat_id, "title": title})
        else:
            # Bot tidak bisa akses sekarang — tetap verifikasi apakah user adalah admin
            if await is_admin(client, chat_id, user_id):
                result.append({"id": chat_id, "title": title})
    return result


async def save_group_title(chat_id: int, title: str) -> None:
    await config_db.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "title": title}},
        upsert=True,
    )
    _config_cache.pop(chat_id, None)


async def remove_group_data(chat_id: int) -> None:
    await config_db.delete_one({"chat_id": chat_id})
    _config_cache.pop(chat_id, None)
    keys_to_remove = [k for k in _admin_cache if k[0] == chat_id]
    for k in keys_to_remove:
        _admin_cache.pop(k, None)
    print(f"[DB] Data grup {chat_id} dihapus (bot dikeluarkan).")


# ══════════════════════════════════════════════════════════════════════════════
# NEXUS DATABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def nexus_insert_kalimat(teks: str) -> bool:
    try:
        await nexus_kalimat_db.update_one(
            {"teks": teks},
            {
                "$setOnInsert": {
                    "teks":          teks,
                    "status_proses": 0,
                    "created_at":    datetime.now(TZ_WIB),
                }
            },
            upsert=True,
        )
        doc = await nexus_kalimat_db.find_one({"teks": teks})
        return doc is not None
    except Exception:
        return False


async def nexus_get_all_kalimat() -> list[str]:
    return [doc["teks"] async for doc in nexus_kalimat_db.find({})]


async def nexus_get_kalimat_count() -> tuple[int, int]:
    total   = await nexus_kalimat_db.count_documents({})
    antrean = await nexus_kalimat_db.count_documents({"status_proses": 0})
    return total, antrean


async def nexus_mark_all_processed():
    await nexus_kalimat_db.update_many({}, {"$set": {"status_proses": 1}})


async def nexus_delete_kalimat(teks: str) -> bool:
    result = await nexus_kalimat_db.delete_one({"teks": teks})
    return result.deleted_count > 0


async def nexus_delete_kalimat_by_id(id_str: str) -> bool:
    try:
        if _BACKEND == "mongo":
            from bson import ObjectId  # type: ignore
            result = await nexus_kalimat_db.delete_one({"_id": ObjectId(id_str)})
        else:
            result = await nexus_kalimat_db.delete_one({"_id": str(id_str)})
        return result.deleted_count > 0
    except Exception:
        return False


async def nexus_save_regex_bulk(pola_list: list[tuple[str, str]]):
    await nexus_regex_db.delete_many({})
    if pola_list:
        docs = [
            {
                "pola":       p,
                "kata_kunci": k,
                "created_at": datetime.now(TZ_WIB),
            }
            for p, k in pola_list
        ]
        await nexus_regex_db.insert_many(docs)


async def nexus_get_all_regex() -> list[dict]:
    return [
        {"pola": d["pola"], "kata_kunci": d["kata_kunci"]}
        async for d in nexus_regex_db.find({})
    ]


async def nexus_get_regex_count() -> int:
    return await nexus_regex_db.count_documents({})


async def nexus_delete_regex_by_pola(pola: str) -> bool:
    result = await nexus_regex_db.delete_one({"pola": pola})
    return result.deleted_count > 0


async def nexus_get_regex_page(page: int, limit: int = 5) -> tuple[list[dict], int]:
    total  = await nexus_regex_db.count_documents({})
    offset = (page - 1) * limit
    rows   = [
        {"pola": d["pola"], "kata_kunci": d["kata_kunci"]}
        async for d in nexus_regex_db.find({}).sort("_id", -1).skip(offset).limit(limit)
    ]
    return rows, total


async def nexus_get_kalimat_page(page: int, limit: int = 10) -> tuple[list[dict], int]:
    total  = await nexus_kalimat_db.count_documents({})
    offset = (page - 1) * limit
    rows   = [
        {
            "_id":           d["_id"],
            "teks":          d["teks"],
            "status_proses": d.get("status_proses", 0),
        }
        async for d in nexus_kalimat_db.find({}).sort("_id", -1).skip(offset).limit(limit)
    ]
    return rows, total


async def nexus_track_grup(chat_id: int, judul: str):
    await nexus_grup_db.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "judul": judul, "is_group": True}},
        upsert=True,
    )


async def nexus_remove_grup(chat_id: int):
    await nexus_grup_db.delete_one({"chat_id": chat_id})


async def nexus_get_all_grup() -> list[dict]:
    return [
        {"chat_id": d["chat_id"], "judul": d.get("judul", str(d["chat_id"]))}
        async for d in nexus_grup_db.find({"is_group": True})
    ]


async def nexus_clear_kalimat():
    await nexus_kalimat_db.delete_many({})
    await nexus_regex_db.delete_many({})


async def nexus_clear_regex():
    await nexus_regex_db.delete_many({})


# ══════════════════════════════════════════════════════════════════════════════
# NEXUS WHITELIST HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def nexus_whitelist_add(pola: str, raw: str, kata_list: list, mutasi: dict) -> bool:
    try:
        await nexus_whitelist_db.update_one(
            {"pola": pola},
            {
                "$set": {
                    "pola":       pola,
                    "raw":        raw,
                    "kata_list":  kata_list,
                    "mutasi":     mutasi,
                    "created_at": datetime.now(TZ_WIB),
                }
            },
            upsert=True,
        )
        return True
    except Exception:
        return False


async def nexus_whitelist_get_all() -> list[dict]:
    return [doc async for doc in nexus_whitelist_db.find({})]


async def nexus_whitelist_count() -> int:
    return await nexus_whitelist_db.count_documents({})


async def nexus_regex_delete_by_id(object_id) -> bool:
    """Hapus satu pola dari regex_db (Owner Regex) berdasarkan _id dokumen."""
    try:
        if _BACKEND == "mongo":
            from bson import ObjectId  # type: ignore
            result = await regex_db.delete_one({"_id": ObjectId(str(object_id))})
        else:
            result = await regex_db.delete_one({"_id": str(object_id)})
        return result.deleted_count > 0
    except Exception:
        return False


async def nexus_whitelist_delete_by_id(object_id) -> bool:
    try:
        if _BACKEND == "mongo":
            from bson import ObjectId  # type: ignore
            result = await nexus_whitelist_db.delete_one({"_id": ObjectId(str(object_id))})
        else:
            result = await nexus_whitelist_db.delete_one({"_id": str(object_id)})
        return result.deleted_count > 0
    except Exception:
        return False


async def nexus_whitelist_page(page: int, limit: int = 5) -> tuple[list[dict], int]:
    total  = await nexus_whitelist_db.count_documents({})
    offset = (page - 1) * limit
    rows   = [
        doc
        async for doc in nexus_whitelist_db.find({}).sort("_id", -1).skip(offset).limit(limit)
    ]
    return rows, total


async def nexus_whitelist_clear() -> int:
    result = await nexus_whitelist_db.delete_many({})
    return result.deleted_count


# ══════════════════════════════════════════════════════════════════════════════
# NEXUS ACTION LOG HELPERS
# ══════════════════════════════════════════════════════════════════════════════
# Menyimpan riwayat tindakan bot (hapus, whitelist spared, keroyokan)
# agar bisa dipantau langsung dari panel bot tanpa buka LOG_CHANNEL.
# Maksimum 500 entri — entri terlama otomatis dihapus saat melewati batas.
# ══════════════════════════════════════════════════════════════════════════════

_ACTLOG_MAX = 500


async def nexus_actlog_insert(
    aksi:        str,    # "HAPUS" | "WHITELIST" | "KEROYOKAN"
    user_id:     int,
    user_name:   str,
    chat_id:     int,
    chat_title:  str,
    alasan:      str,    # kata kunci / layer AI
    confidence:  float,  # 0.0 jika bukan AI
    content:     str,    # cuplikan pesan (maks 200 char)
) -> None:
    try:
        doc = {
            "aksi":       aksi,
            "user_id":    user_id,
            "user_name":  user_name,
            "chat_id":    chat_id,
            "chat_title": chat_title,
            "alasan":     alasan[:200],
            "confidence": round(confidence, 4),
            "content":    content[:200],
            "ts":         datetime.now(TZ_WIB),
        }
        await nexus_actlog_db.insert_one(doc)
        # Pangkas jika melebihi batas
        total = await nexus_actlog_db.count_documents({})
        if total > _ACTLOG_MAX:
            # Hapus 50 entri terlama sekaligus
            oldest = [
                d["_id"]
                async for d in nexus_actlog_db.find({}).sort("_id", 1).limit(50)
            ]
            if oldest:
                await nexus_actlog_db.delete_many({"_id": {"$in": oldest}})
    except Exception as e:
        print(f"[DB] actlog_insert error (non-fatal): {e}")


async def nexus_actlog_get_page(page: int, limit: int = 5) -> tuple[list[dict], int]:
    total  = await nexus_actlog_db.count_documents({})
    offset = (page - 1) * limit
    rows   = [
        doc
        async for doc in nexus_actlog_db.find({}).sort("_id", -1).skip(offset).limit(limit)
    ]
    return rows, total


async def nexus_actlog_count() -> int:
    return await nexus_actlog_db.count_documents({})


async def nexus_actlog_clear() -> int:
    result = await nexus_actlog_db.delete_many({})
    return result.deleted_count


# ══════════════════════════════════════════════════════════════════════════════
# AI DEBUG LOG — 24h TTL
# Log internal aktivitas nexus/ai_core/ untuk dipantau owner.
# Data lama otomatis dihapus saat fungsi get_page dipanggil.
# ══════════════════════════════════════════════════════════════════════════════

_ai_debug_db = db["ai_debug_log"]


async def ai_debug_log_insert(
    aksi:       str,
    label:      str   = "-",
    confidence: float = 0.0,
    ringkasan:  str   = "",
    chat_id:    int   = 0,
) -> None:
    """Simpan satu entri log debug AI. Non-blocking, gagal diam-diam."""
    try:
        ts_now = int(datetime.now(timezone.utc).timestamp())
        doc = {
            "ts":         ts_now,
            "aksi":       aksi[:40],
            "label":      label[:16],
            "confidence": round(float(confidence), 4),
            "ringkasan":  ringkasan[:180],
            "chat_id":    int(chat_id),
        }
        await _ai_debug_db.insert_one(doc)
    except Exception as e:
        print(f"[DB] ai_debug_log_insert error: {e}")


async def ai_debug_log_get_page(
    page:     int = 1,
    per_page: int = 5,
) -> tuple[list[dict], int]:
    """
    Ambil halaman log debug AI (24 jam terakhir), urut terbaru dulu.
    Sekaligus membersihkan entri yang lebih dari 24 jam.
    Returns: (docs_halaman_ini, total_dalam_24j)
    """
    try:
        cutoff = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())

        # Load semua, lakukan filtering dan cleanup di Python
        # (aman untuk kedua backend SQLite & MongoDB)
        all_docs = await _ai_debug_db.find({}).to_list(None)

        # Pisahkan yang lama — hapus dari DB
        old_ids  = [d["_id"] for d in all_docs if d.get("ts", 0) < cutoff and "_id" in d]
        for oid in old_ids:
            try:
                await _ai_debug_db.delete_one({"_id": oid})
            except Exception:
                pass

        # Filter 24 jam terakhir, sort terbaru dulu
        recent = [d for d in all_docs if d.get("ts", 0) >= cutoff]
        recent.sort(key=lambda d: d.get("ts", 0), reverse=True)

        total = len(recent)
        start = (page - 1) * per_page
        return recent[start : start + per_page], total

    except Exception as e:
        print(f"[DB] ai_debug_log_get_page error: {e}")
        return [], 0


# ══════════════════════════════════════════════════════════════════════════════
# DM USER REGISTRY — untuk broadcast notifikasi shutdown/maintenance
# ══════════════════════════════════════════════════════════════════════════════

_dm_users_db = db["dm_users"]


async def register_dm_user(user_id: int) -> None:
    """Catat user yang pernah berinteraksi dengan bot via DM."""
    try:
        ts_now = int(datetime.now(timezone.utc).timestamp())
        await _dm_users_db.update_one(
            {"user_id": user_id},
            {"$set": {"user_id": user_id, "ts": ts_now}},
            upsert=True,
        )
    except Exception as e:
        print(f"[DB] register_dm_user error: {e}")


async def get_all_dm_users() -> list[int]:
    """Ambil semua user_id yang terdaftar untuk broadcast shutdown."""
    try:
        docs = await _dm_users_db.find({}).to_list(None)
        return [d["user_id"] for d in docs if isinstance(d.get("user_id"), int)]
    except Exception as e:
        print(f"[DB] get_all_dm_users error: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# LOCAL MUTE TRACKER — anti-duplikasi eskalasi hukuman
# ══════════════════════════════════════════════════════════════════════════════
# Schema per dokumen:
#   _id         : "lmute_{chat_id}_{user_id}"
#   chat_id     : int
#   user_id     : int
#   consec_spam : int    — hitungan duplikat berturut-turut (reset jika pesan bersih)
#   mute_level  : int    — level eskalasi; 0 = 5 mnt, 1 = 10 mnt, 2 = 20 mnt, dst.
#   muted_until : float  — unix timestamp akhir mute; 0.0 jika tidak sedang mute
#   updated_at  : float  — unix timestamp terakhir update
# ══════════════════════════════════════════════════════════════════════════════

local_mute_db = db["local_mute"]

_BASE_MUTE_SECONDS = 5 * 60   # 5 menit


def _mute_duration_seconds(mute_level: int) -> int:
    """Durasi mute dalam detik berdasarkan level eskalasi (2^level × 5 menit)."""
    return _BASE_MUTE_SECONDS * (2 ** mute_level)


async def get_local_mute(chat_id: int, user_id: int) -> dict:
    """Ambil atau buat rekaman mute untuk user di grup tertentu."""
    key = f"lmute_{chat_id}_{user_id}"
    doc = await local_mute_db.find_one({"_id": key})
    if doc is None:
        doc = {
            "_id":         key,
            "chat_id":     chat_id,
            "user_id":     user_id,
            "consec_spam": 0,
            "mute_level":  0,
            "muted_until": 0.0,
            "updated_at":  time.time(),
        }
    return doc


async def _save_local_mute(doc: dict) -> None:
    doc["updated_at"] = time.time()
    await local_mute_db.update_one(
        {"_id": doc["_id"]},
        {"$set": doc},
        upsert=True,
    )


async def increment_local_spam(chat_id: int, user_id: int) -> dict:
    """Tambah hitungan spam berturut-turut. Kembalikan dokumen terupdate."""
    doc = await get_local_mute(chat_id, user_id)
    doc["consec_spam"] = doc.get("consec_spam", 0) + 1
    await _save_local_mute(doc)
    return doc


async def apply_local_mute(chat_id: int, user_id: int) -> tuple[int, int]:
    """
    Terapkan mute berdasarkan level saat ini.
    Kembalikan (durasi_detik, level_yang_dipakai).
    Setelah dipanggil: consec_spam TIDAK direset ke 0 melainkan tetap di ambang
    (SPAM_MUTE_THRESHOLD) agar setelah mute habis, 1 pelanggaran berikutnya
    langsung memicu mute level berikutnya — hitungan punishment dilanjutkan,
    bukan dimulai dari awal. Restart bot tidak mempengaruhi ini karena
    consec_spam dan muted_until tersimpan persisten di database.
    """
    _SPAM_MUTE_THRESHOLD = 10   # harus sama dengan SPAM_MUTE_THRESHOLD di punishment.py
    doc      = await get_local_mute(chat_id, user_id)
    level    = doc.get("mute_level", 0)
    duration = _mute_duration_seconds(level)
    doc["muted_until"] = time.time() + duration
    # Pertahankan consec_spam di ambang agar setelah mute habis langsung
    # mute lagi pada pelanggaran pertama (bukan harus 10x dari awal lagi).
    doc["consec_spam"] = _SPAM_MUTE_THRESHOLD
    doc["mute_level"]  = level + 1
    await _save_local_mute(doc)
    return duration, level


async def reset_local_mute(chat_id: int, user_id: int) -> None:
    """
    Reset hitungan dan level hukuman (dipanggil saat pesan bersih diterima).
    Jika belum ada rekaman, tidak melakukan apa-apa.
    """
    key = f"lmute_{chat_id}_{user_id}"
    doc = await local_mute_db.find_one({"_id": key})
    if doc is None:
        return
    # Hanya reset jika ada sesuatu yang perlu direset
    if doc.get("consec_spam", 0) == 0 and doc.get("mute_level", 0) == 0:
        return
    doc["consec_spam"] = 0
    doc["mute_level"]  = 0
    await _save_local_mute(doc)


# ══════════════════════════════════════════════════════════════════════════════
# GROUP ACTION LOG — log aksi per grup (hapus/mute/ban), TTL 7 hari
# ══════════════════════════════════════════════════════════════════════════════

async def insert_group_action_log(
    chat_id:   int,
    aksi:      str,   # "HAPUS" | "MUTE" | "BAN"
    alasan:    str,   # bahasa sederhana untuk admin
    user_id:   int,
    user_name: str,
    konten:    str = "",
) -> None:
    """Simpan satu entri log aksi ke group_action_log. Non-blocking, gagal diam-diam."""
    try:
        doc = {
            "chat_id":   chat_id,
            "ts":        time.time(),
            "aksi":      aksi[:20],
            "alasan":    alasan[:120],
            "user_id":   user_id,
            "user_name": user_name[:50],
            "konten":    konten[:100],
        }
        await group_action_log_db.insert_one(doc)
    except Exception as e:
        print(f"[DB] insert_group_action_log error (non-fatal): {e}")


async def get_group_action_log_page(
    chat_id:  int,
    page:     int = 1,
    per_page: int = 10,
) -> tuple[list[dict], int]:
    """
    Ambil halaman log aksi grup (7 hari terakhir), urut terbaru dulu.
    Sekaligus bersihkan entri > 7 hari.
    """
    try:
        cutoff = time.time() - (7 * 86400)
        all_docs = await group_action_log_db.find({"chat_id": chat_id}).to_list(None)
        # Hapus yang sudah expired
        old_ids = [d["_id"] for d in all_docs if d.get("ts", 0) < cutoff and "_id" in d]
        for oid in old_ids:
            try:
                await group_action_log_db.delete_one({"_id": oid})
            except Exception:
                pass
        recent = [d for d in all_docs if d.get("ts", 0) >= cutoff]
        recent.sort(key=lambda d: d.get("ts", 0), reverse=True)
        total  = len(recent)
        start  = (page - 1) * per_page
        return recent[start:start + per_page], total
    except Exception as e:
        print(f"[DB] get_group_action_log_page error: {e}")
        return [], 0
