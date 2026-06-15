"""
nexus/ai_core/context_filter.py
────────────────────────────────
Context-Aware Filter — mencegah false positive dengan memahami konteks kalimat.

AI yang naif akan ikut menghapus kalimat seperti:
  "waspada ajakan join grup judi!"  ← padahal ini PERINGATAN, bukan spam
  "gimana cara masuk grup belajar?" ← padahal ini pertanyaan biasa
  "video dewasa itu bahaya ya"      ← padahal ini diskusi, bukan promosi

Filter ini menganalisis konteks sebelum dan sesudah kata terlarang
dan menghasilkan MODIFIER yang mengurangi/menambah skor akhir AI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ─── Kata-kata Penegas Konteks ────────────────────────────────────────────────

# Kata negasi/perlawanan yang muncul SEBELUM kata spam
NEGATION_WORDS: frozenset[str] = frozenset({
    "bukan", "tidak", "jangan", "jgn", "ga", "gak", "nggak", "ngga",
    "tanpa", "anti", "hindari", "jauhi", "waspada", "hati-hati", "hati hati",
    "awas", "bahaya", "berbahaya", "larang", "terlarang", "dilarang",
    "tolak", "tolak", "blokir", "block", "report", "laporkan", "laporan",
})

# Konteks pelaporan/diskusi — ini bukan spam, ini orang membahas spam
DISCUSSION_MARKERS: frozenset[str] = frozenset({
    "waspada", "hati-hati", "hati hati", "awas", "lapor", "laporkan",
    "laporan", "jangan percaya", "penipuan seperti ini", "contoh spam",
    "ini spam", "itu spam", "terima spam", "dapat spam", "kena spam",
    "tolong hapus", "minta hapus", "admin hapus", "screenshot", "ss ini",
    "bukti", "tangkap layar", "expose", "share biar tau", "sebar biar tau",
    "penipu", "scammer", "penipu ini", "waspadai", "peringatan",
    "info penting", "waspada grup", "hati2",
})

# Konteks pertanyaan — mengurangi skor spam
QUESTION_STARTERS: frozenset[str] = frozenset({
    "apa", "apakah", "bagaimana", "gimana", "kenapa", "mengapa",
    "kapan", "dimana", "di mana", "siapa", "berapa", "boleh",
    "bisa", "cara", "gimana cara", "bagaimana cara", "ada yang tau",
    "ada yg tau", "tau ga", "tau gak", "ada info", "ada yang",
})

# Kata editorial/komentar yang menunjukkan pembicara sedang MEMBAHAS konten, bukan menyebarkan
EDITORIAL_MARKERS: frozenset[str] = frozenset({
    "katanya", "kata orang", "konon", "kabarnya", "denger-denger",
    "baca di", "lihat di", "nemu", "nemuin", "ketemu", "nangkep",
    "isi pesannya", "bunyi pesannya", "isinya", "pesannya bilang",
    "dikira", "dianggap", "dikategorikan", "termasuk", "disebut",
    "istilah", "definisi", "pengertian", "maksud",
})

# Konteks diri sendiri — menunjukkan AI tidak berlaku
SELF_REFERENCE: frozenset[str] = frozenset({
    "grup ini", "grup kita", "komunitas ini", "di sini", "disini",
    "channel ini", "admin grup ini", "bot ini", "aturan grup",
    "anggota grup ini", "member di sini", "member grup ini",
})


# ─── Pola Regex Konteks ────────────────────────────────────────────────────────

# Kalimat yang MENJELASKAN spam, bukan menyebar spam
RE_REPORTING = re.compile(
    r"\b(ini|itu|tadi|barusan|berikut|contoh|screenshot|ss)\b.{0,30}"
    r"\b(spam|judi|penipuan|bokep|porno|scam|hoax)\b",
    re.IGNORECASE,
)

# Pertanyaan tentang cara bergabung ke grup INI (bukan ajakan ke grup LAIN)
RE_ASKING_JOIN_THIS = re.compile(
    r"\b(gimana|bagaimana|cara|bisa|boleh)\b.{0,20}"
    r"\b(join|gabung|masuk|daftar)\b.{0,20}"
    r"\b(di sini|disini|grup ini|channel ini|sini|ini)\b",
    re.IGNORECASE,
)

# Ajakan ke GRUP LAIN (spam signal — external link atau @username dengan invite word)
RE_EXTERNAL_INVITE = re.compile(
    r"\b(join|gabung|masuk|yuk|ayo|mari|ikutan|daftar|register)\b.{0,60}"
    r"(t\.me/|@[a-zA-Z0-9_]{4,}|https?://|wa\.me/|bit\.ly|s\.id)",
    re.IGNORECASE,
)

RE_EXTERNAL_INVITE_REV = re.compile(
    r"(t\.me/|@[a-zA-Z0-9_]{4,}|https?://|wa\.me/|bit\.ly|s\.id)"
    r".{0,60}\b(join|gabung|masuk|yuk|ayo|mari|ikutan|daftar|register)\b",
    re.IGNORECASE,
)

# Kata "grup" tanpa referensi ke grup saat ini
RE_OTHER_GROUP = re.compile(
    r"\b(grup|group|channel|komunitas|community)\b.{0,30}"
    r"\b(lain|baru|kita punya|kami|mereka|gue|teman|temen)\b",
    re.IGNORECASE,
)


# ─── Hasil Analisis Konteks ───────────────────────────────────────────────────

@dataclass
class ContextResult:
    """Hasil analisis konteks untuk satu pesan."""
    modifier: float          = 0.0    # negatif = kurangi skor, positif = tambah skor
    is_likely_discussion: bool = False  # True = orang sedang MEMBAHAS, bukan menyebar
    is_question: bool         = False  # True = ini pertanyaan polos
    is_external_invite: bool  = False  # True = jelas ajakan ke grup lain
    reasons: list[str]        = field(default_factory=list)

    def apply(self, base_score: float) -> float:
        """Terapkan modifier ke skor dasar. Clamp ke [0.0, 1.0]."""
        return round(max(0.0, min(1.0, base_score + self.modifier)), 4)


# ─── Engine ───────────────────────────────────────────────────────────────────

class ContextFilter:
    """
    Menganalisis konteks kalimat dan menghasilkan modifier skor spam.

    Usage:
        cf     = ContextFilter()
        ctx    = cf.analyze(teks)
        final  = ctx.apply(ai_score)   # skor disesuaikan konteks
    """

    def analyze(self, text: str) -> ContextResult:
        result  = ContextResult()
        t_lower = text.lower().strip()
        words   = set(t_lower.split())

        # ── 1. Deteksi negasi/peringatan ────────────────────────────────────
        neg_hits = words & NEGATION_WORDS
        if neg_hits:
            result.modifier   -= 0.15
            result.is_likely_discussion = True
            result.reasons.append(f"negasi ditemukan: {', '.join(list(neg_hits)[:3])}")

        # ── 2. Konteks diskusi/pelaporan ─────────────────────────────────────
        disc_hits = words & DISCUSSION_MARKERS
        if disc_hits:
            result.modifier           -= 0.20
            result.is_likely_discussion = True
            result.reasons.append(f"konteks laporan/diskusi: {', '.join(list(disc_hits)[:3])}")

        # ── 3. Konteks pertanyaan ─────────────────────────────────────────────
        q_hits = [w for w in QUESTION_STARTERS if w in t_lower]
        if q_hits:
            # Pertanyaan di awal kalimat → pengurang kuat
            if any(t_lower.startswith(w) for w in q_hits):
                result.modifier -= 0.18
            else:
                result.modifier -= 0.08
            result.is_question = True
            result.reasons.append(f"kalimat tanya: {q_hits[0]}")

        # ── 4. Konteks editorial / sedang membahas ────────────────────────────
        ed_hits = words & EDITORIAL_MARKERS
        if ed_hits:
            result.modifier           -= 0.10
            result.is_likely_discussion = True
            result.reasons.append(f"konteks editorial: {', '.join(list(ed_hits)[:2])}")

        # ── 5. Referensi ke grup saat ini (bukan ajakan ke luar) ─────────────
        self_hits = [w for w in SELF_REFERENCE if w in t_lower]
        if self_hits:
            result.modifier -= 0.15
            result.reasons.append(f"merujuk grup sendiri: {self_hits[0]}")

        # ── 6. Deteksi kalimat reporting (regex) ──────────────────────────────
        if RE_REPORTING.search(text):
            result.modifier           -= 0.20
            result.is_likely_discussion = True
            result.reasons.append("kalimat menjelaskan/melaporkan spam")

        # ── 7. Pertanyaan bergabung ke grup INI ──────────────────────────────
        if RE_ASKING_JOIN_THIS.search(text):
            result.modifier   -= 0.25
            result.is_question = True
            result.reasons.append("bertanya cara bergabung grup ini (bukan ajakan)")

        # ── 8. Ajakan KELUAR ke grup/link eksternal ─── TAMBAH skor! ─────────
        if RE_EXTERNAL_INVITE.search(text) or RE_EXTERNAL_INVITE_REV.search(text):
            result.modifier          += 0.20
            result.is_external_invite = True
            result.reasons.append("ajakan ke link/grup eksternal terdeteksi")

        # ── 9. Referensi ke grup lain ─────────────────────────────────────────
        if RE_OTHER_GROUP.search(text):
            result.modifier          += 0.10
            result.is_external_invite = True
            result.reasons.append("menyebut 'grup lain' atau 'grup kami/mereka'")

        # Clamp modifier ke range aman
        result.modifier = round(max(-0.40, min(0.30, result.modifier)), 4)
        return result


# Singleton global
_context_filter: ContextFilter | None = None


def get_context_filter() -> ContextFilter:
    global _context_filter
    if _context_filter is None:
        _context_filter = ContextFilter()
    return _context_filter
