"""
nexus/ai_core/constants.py
──────────────────────────
Semua konstanta, kosakata seed, dan pola regex cepat untuk Nexus AI Core.
Tidak ada logika di sini — hanya data.

LEET_MAP diimpor dari core.regex_utils sebagai sumber kebenaran tunggal.

v3.1 — Tambah seed vocabulary:
  bio_promo_spam — ajakan lihat bio/profil untuk spam
"""

import re
from core.regex_utils import LEET_MAP

# ─── Versi Model ──────────────────────────────────────────────────────────────
AI_CORE_VERSION = "3.1"

# ─── Seed Vocabulary ──────────────────────────────────────────────────────────
SEED_SPAM_VOCAB: dict[str, list[str]] = {
    "judi_slot": [
        "togel", "slot", "judi", "gacor", "rtp", "jackpot", "maxwin",
        "scatter", "pragmatic", "pg soft", "bonus slot", "daftar slot",
        "link slot", "zeus", "mahjong", "gates of olympus", "sweet bonanza",
        "starlight princess", "spaceman", "live rtp", "rtp live",
        "bocoran rtp", "pola slot", "jam gacor", "modal receh",
        "jp maxwin", "freespin", "x500", "x1000", "wild", "spin",
        "gacor hari ini", "bocoran pola", "anti rungkad",
    ],
    "investasi_bodong": [
        "investasi", "profit", "passive income", "penghasilan", "cuan",
        "modal kecil", "tanpa modal", "binary", "forex", "trading",
        "crypto", "bitcoin", "ethereum", "mining", "airdrop",
        "roi", "return", "persen per hari", "bunga harian",
        "rekrutmen", "downline", "member baru", "join sekarang",
        "daftar gratis", "klik link", "gabung sekarang",
        "penghasilan pasif", "kerja dari rumah", "bisnis online",
    ],
    "jual_akun": [
        "jual akun", "beli akun", "akun sultan", "akun premium",
        "saldo e-wallet", "saldo dana", "saldo ovo", "saldo gopay",
        "akun verified", "akun pro", "harga murah", "stok terbatas",
        "fast respon", "amanah", "trusted", "jual murah",
        "jual saldo", "top up murah", "reseller",
    ],
    "promosi_viral": [
        "klik link", "hubungi kami", "wa kami", "chat kami",
        "dm untuk info", "harga spesial", "promo hari ini",
        "terbatas", "buruan", "segera", "order sekarang",
        "pengiriman cepat", "garansi", "resmi", "official",
        "diskon besar", "flash sale", "giveaway", "hadiah",
        "pemenang", "selamat anda menang", "klaim hadiah",
        "info lebih lanjut", "hubungi admin",
    ],
    "gcast_spam": [
        "broadcast", "forward", "sebarkan", "share ke grup",
        "kirim ke semua", "teruskan pesan", "sebar", "viralkan",
        "copy paste", "share this",
    ],
    "pinjol_judol": [
        "pinjaman online", "pinjol", "pinjam uang", "kredit instan",
        "cair cepat", "tanpa jaminan", "bunga rendah",
        "koperasi", "kta kilat", "limit tinggi",
        "pinjaman tanpa ribet", "dana darurat",
    ],
    "shortlink_spam": [
        "wa.me", "whatsapp.com/", "t.me/", "bit.ly", "tinyurl",
        "s.id/", "linktr.ee", "linktree", "cutt.ly", "rebrand.ly",
        "short.gg", "gg.gg", "rb.gy",
    ],
    "group_invite_spam": [
        "yuk gabung grup", "ayo join grup", "gabung sekarang di",
        "bergabung di grup", "join channel kami", "masuk grup kami",
        "klik link gabung", "link grup", "link channel",
        "invite link", "grup profit", "channel profit",
        "komunitas kami", "bergabunglah bersama kami",
        "jangan ketinggalan gabung", "buruan gabung",
        "mampir ke grup", "kepoin channel", "cek channel kami",
    ],
    "konten_porno": [
        "bokep", "bugil", "telanjang", "open bo", "ob tante",
        "jasa pijat plus", "happy ending", "sewa tante",
        "video dewasa", "foto bugil", "konten 18+",
        "layanan dewasa", "tante girang",
        "video hot", "foto hot", "vc mesra", "vc dewasa",
        "teman bobo", "teman tidur",
    ],
    "penipuan_scam": [
        "transfer dulu", "bayar dp", "bayar admin dulu",
        "modal receh untung", "pasti untung", "dijamin untung",
        "selamat anda terpilih", "selamat anda menang hadiah",
        "claim hadiah anda", "klik untuk klaim",
        "skema bisnis", "rekrut member",
        "komisi referral besar", "langsung cair tanpa ribet",
        "bunga 0 persen", "tanpa agunan tanpa survey",
    ],
    "bio_promo_spam": [
        "cek bio aku", "cek bio saya", "cek bio gue", "cek bio admin",
        "lihat bio aku", "lihat bio saya", "lihat bio kami",
        "liat bio aku", "liat bio saya", "liat bio admin",
        "kunjungi bio kami", "kunjungi profil kami",
        "ada di bio aku", "ada di bio saya", "ada di bio admin",
        "info di bio", "link di bio", "cek di bio",
        "ada di profil saya", "ada di profil kami",
        "info di profil", "link di profil",
        "kepoin bio aku", "intip bio saya",
    ],
}

# Token spam flat (untuk fast lookup O(1))
FLAT_SPAM_TOKENS: frozenset[str] = frozenset(
    token.lower()
    for tokens in SEED_SPAM_VOCAB.values()
    for token in tokens
)

# ─── Regex Pola Cepat (precompiled) ──────────────────────────────────────────
RE_URL = re.compile(
    r"(https?://|www\.|t\.me/|wa\.me/|bit\.ly/|tinyurl\.com|linktr\.ee|s\.id/|"
    r"cutt\.ly|rebrand\.ly|short\.gg|gg\.gg|rb\.gy|"
    r"[a-z0-9\-]{3,}\.[a-z]{2,6}(/[^\s]*)?)",
    re.IGNORECASE,
)

RE_PHONE_ID = re.compile(
    r"(\+62|08)[0-9][\s\-]?[0-9]{3,4}[\s\-]?[0-9]{3,4}[\s\-]?[0-9]{0,4}"
)

RE_MONEY = re.compile(
    r"\b(rp\.?|idr)\s*[\d\.,]+|\b\d+[\d\.,]*\s*(juta|ribu|rb|jt|k)\b",
    re.IGNORECASE,
)

RE_PERCENT = re.compile(r"\b\d+\s*%|\bpersen\b", re.IGNORECASE)

RE_EMOJI = re.compile(
    r"[\U0001F300-\U0001F9FF"
    r"\U00002600-\U000027BF"
    r"\U0001FA00-\U0001FA9F"
    r"\U00002702-\U000027B0]"
)

RE_REPEATING   = re.compile(r"(.)\1{3,}")
RE_CAPS_WORD   = re.compile(r"\b[A-Z]{3,}\b")
RE_HASHTAG     = re.compile(r"#\w+")
RE_MENTION_EXT = re.compile(r"@[a-zA-Z0-9_]{4,}")
RE_LEET        = re.compile(r"[013457890@]")

# ─── Contoh Kalimat HAM untuk seeding ─────────────────────────────────────────
HAM_SEED: list[str] = [
    "halo selamat pagi semua",
    "ada yang mau tanya soal python tidak",
    "terima kasih atas informasinya",
    "oke siap akan saya coba nanti",
    "boleh minta tolong bantu saya",
    "diskusi hari ini sangat bermanfaat",
    "kapan jadwal meetingnya mas",
    "saya sudah baca dokumentasinya",
    "apakah ada yang bisa bantu saya memahami ini",
    "bagaimana cara membuat fitur ini bekerja",
    "update terbaru sudah dirilis kemarin",
    "selamat malam semua semoga sehat selalu",
    "ada pertanyaan soal materi tadi",
    "mau berbagi artikel menarik tentang teknologi",
    "jadwal maintenance server besok jam berapa",
    "siapa yang bisa jelasin tentang database ini",
    "terimakasih sudah membantu kemarin",
    "hasil diskusi tadi sangat berguna",
    "ada yang punya referensi buku bagus",
    "mari kita lanjutkan diskusinya besok",
    "selamat datang di grup ini",
    "mohon maaf ada yang bisa dibantu",
    "ok paham terima kasih penjelasannya",
    "nanti saya coba dulu ya",
    "semangat semuanya hari ini",
    "waspada ada yang coba ajak join grup judi di luar",
    "hati-hati ada yang nyebar link mencurigakan tadi",
    "tadi ada yang broadcast spam, sudah dilaporkan ke admin",
    "gimana cara join diskusi di grup ini ya?",
    "apakah boleh share artikel di sini?",
    "ini contoh pesan scam yang beredar, jangan klik",
    "admin tolong hapus pesan spam tadi",
    "screenshot spam yang masuk untuk bukti laporan",
]

# ─── Label Output ─────────────────────────────────────────────────────────────
LABEL_SPAM      = "SPAM"
LABEL_HAM       = "HAM"
LABEL_UNCERTAIN = "UNCERTAIN"

# ─── Threshold Default ────────────────────────────────────────────────────────
DEFAULT_THRESHOLD         = 0.55
AUTO_DETECT_BONUS         = 0.05
HIGH_CONFIDENCE_BAYES     = 0.70
MIN_CONFIDENCE_DELETE     = 0.70

AUTO_LEARN_CONFIDENCE     = 0.70   # sync dengan passive_learner.AUTO_LEARN_SPAM_THRESHOLD
CATEGORY_DETECT_WEIGHT    = 0.30
CONTEXT_FILTER_ENABLED    = True
