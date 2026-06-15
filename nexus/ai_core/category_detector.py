"""
nexus/ai_core/category_detector.py
────────────────────────────────────
Deteksi spam berdasarkan kategori spesifik — melengkapi Bayes + Feature Scoring.

Kategori yang dideteksi:
  GROUP_INVITE  — ajakan bergabung ke grup/channel lain
  PORN          — konten/undangan dewasa/pornografi
  SCAM          — penipuan finansial, iming-iming palsu
  PROMO_VIRAL   — promosi agresif, broadcast, giveaway palsu
  BIO_PROMO     — ajakan melihat bio/profil untuk promosi (v3.1 baru)

Setiap detektor menghasilkan (hit: bool, confidence: float, reasons: list[str]).
Confidence ini digabungkan oleh CategoryDetector.detect() dan diteruskan ke
NexusAICore untuk pengambilan keputusan akhir.

PENTING: Setiap kategori WAJIB melewati ContextFilter sebelum keputusan final.
Artinya: kata "bokep" dalam kalimat "ini bokep laporan ketua RT" tidak akan
langsung dianggap spam — konteks "laporan" akan mengurangi skornya.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ══════════════════════════════════════════════════════════════════════════════
# KATEGORI 1 — GROUP INVITE SPAM
# Ajakan bergabung ke grup/channel Telegram lain
# ══════════════════════════════════════════════════════════════════════════════

# Kata ajakan bergabung
_INVITE_VERBS = re.compile(
    r"\b(join|gabung|masuk|ikut|ikutan|daftar|register|subscribe|sub|"
    r"follow|folow|ikutin|mampir|mampirin|kunjungi|cek|kepoin|meluncur|"
    r"yuk|ayo|mari|buruan|segera|langsung|cuss|gas|hayu)\b",
    re.IGNORECASE,
)

# Tujuan ajakan (tempat di luar)
_INVITE_DEST = re.compile(
    r"(t\.me/[a-zA-Z0-9_+]+|"           # t.me/namagrup
    r"@[a-zA-Z0-9_]{4,}|"               # @username
    r"https?://[^\s]{5,}|"              # link apapun
    r"wa\.me/[0-9+]+|"                  # wa.me/nomor
    r"bit\.ly/[^\s]+|"                  # bit.ly shortlink
    r"s\.id/[^\s]+|"                    # s.id shortlink
    r"linktr\.ee/[^\s]+|"               # linktree
    r"cutt\.ly/[^\s]+)",                # cutt.ly
    re.IGNORECASE,
)

# Kata destinasi spesifik untuk grup/channel lain
_INVITE_NOUN = re.compile(
    r"\b(grup|group|channel|chanel|komunitas|community|fanbase|squad|"
    r"server|discord|forum|telegram|wa|whatsapp|gc|grub|chanel)\b",
    re.IGNORECASE,
)

# Kata yang menunjukkan "milik orang lain / luar"
_INVITE_EXTERNAL = re.compile(
    r"\b(kami|kita punya|baru|lain|beda|khusus|vip|premium|eksklusif|"
    r"private|privat|secret|rahasia|terbatas|limited|official|resmi|"
    r"teman|temen|gue bikin|ane bikin|saya buat|admin buat)\b",
    re.IGNORECASE,
)


def detect_group_invite(text: str) -> tuple[bool, float, list[str]]:
    """
    Deteksi ajakan bergabung ke grup/channel lain.
    Return: (hit, confidence, reasons)
    """
    reasons: list[str] = []
    score = 0.0

    has_verb = bool(_INVITE_VERBS.search(text))
    has_dest = bool(_INVITE_DEST.search(text))
    has_noun = bool(_INVITE_NOUN.search(text))
    has_ext  = bool(_INVITE_EXTERNAL.search(text))

    # Pola terkuat: ada link/username DAN kata ajakan
    if has_dest and has_verb:
        score += 0.55
        reasons.append("link/username + kata ajakan bergabung")

    # Ada link/username saja (tanpa kata ajakan) → moderate
    elif has_dest:
        score += 0.30
        reasons.append("link/username eksternal ditemukan")

    # Kata ajakan + menyebut grup/channel lain
    if has_verb and has_noun and has_ext:
        score += 0.20
        reasons.append("ajakan + sebutan grup lain")
    elif has_verb and has_noun:
        score += 0.10
        reasons.append("ajakan + kata grup/channel")

    hit = score >= 0.45
    return hit, min(1.0, round(score, 4)), reasons


# ══════════════════════════════════════════════════════════════════════════════
# KATEGORI 2 — KONTEN PORNO / DEWASA
# ══════════════════════════════════════════════════════════════════════════════

_PORN_HARD = re.compile(
    r"\b(bokep|bugil|telanjang|nakal|hot|esek|esek esek|mesum|porno|pornografi|"
    r"xxx|18\+|konten dewasa|film dewasa|video dewasa|gambar bugil|foto bugil|"
    r"colmek|ngentot|memek|kontol|pepek|toket|ngocok|jilat|"
    r"open bo|ob tante|jasa bo|layanan bo|psk|wts|wts tante|"
    r"panggilan|jasa pijat plus|plus plus|happy ending|"
    r"sewa tante|sewa abg|cari tante|tante girang)\b",
    re.IGNORECASE,
)

_PORN_SOFT = re.compile(
    r"\b(dewasa|sensual|erotis|sexy|seksi|montok|body mulus|"
    r"video call mesra|vc mesra|vc seru|vc dewasa|"
    r"mau kenalan|cari teman dekat|teman tidur|teman bobo|"
    r"teman istimewa|cewek cari|cowok cari|jomblo cari)\b",
    re.IGNORECASE,
)

_PORN_ACTION = re.compile(
    r"\b(kirim foto|kirim video|share foto|share video|minta foto|"
    r"vc yuk|video call yuk|mau lihat|mau liat|pengen lihat|"
    r"lihat dulu|preview dulu)\b",
    re.IGNORECASE,
)


def detect_porn(text: str) -> tuple[bool, float, list[str]]:
    """
    Deteksi konten/ajakan pornografi.
    Return: (hit, confidence, reasons)
    """
    reasons: list[str] = []
    score = 0.0

    hard = _PORN_HARD.search(text)
    soft = _PORN_SOFT.search(text)
    act  = _PORN_ACTION.search(text)

    if hard:
        score += 0.65
        reasons.append(f"kata porno eksplisit: {hard.group()[:30]}")

    if soft:
        score += 0.25
        reasons.append(f"kata suggestif: {soft.group()[:30]}")

    if act:
        score += 0.15
        reasons.append(f"ajakan aksi: {act.group()[:30]}")

    # Kombinasi soft + action = lebih kuat
    if soft and act:
        score += 0.10
        reasons.append("kombinasi suggestif + ajakan")

    hit = score >= 0.45
    return hit, min(1.0, round(score, 4)), reasons


# ══════════════════════════════════════════════════════════════════════════════
# KATEGORI 3 — PENIPUAN (SCAM)
# ══════════════════════════════════════════════════════════════════════════════

_SCAM_FINANCIAL = re.compile(
    r"\b(transfer dulu|bayar dp|bayar admin|bayar duluan|bayar dulu|"
    r"minta dp|dp dulu|uang muka dulu|deposit dulu|"
    r"modal receh|modal kecil|tanpa modal|modal minim|"
    r"cuan besar|untung besar|profit besar|penghasilan jutaan|"
    r"kerja santai|kerja dari rumah|gaji besar|gaji jutaan|"
    r"tanpa pengalaman|tanpa skill|tanpa syarat|bebas syarat|"
    r"langsung cair|cair instan|cair 5 menit|proses cepat|"
    r"pasti untung|dijamin untung|garansi untung|100 persen aman|"
    r"skema|piramid|MLM|multi level|downline|upline|rekrut member|"
    r"bonus referral|komisi referral)\b",
    re.IGNORECASE,
)

_SCAM_URGENCY = re.compile(
    r"\b(terbatas|limited|stok habis|kesempatan emas|jangan sampai rugi|"
    r"jangan lewatkan|last chance|kesempatan terakhir|"
    r"hari ini saja|hanya hari ini|hanya malam ini|expired|"
    r"claim sekarang|klaim sekarang|ambil sekarang|hari terakhir)\b",
    re.IGNORECASE,
)

_SCAM_CONTACT = re.compile(
    r"\b(hubungi|wa|whatsapp|chat|dm|inbox|pm)\b.{0,20}"
    r"\b(admin|cs|saya|kami|kita|owner|pemilik)\b",
    re.IGNORECASE,
)

_SCAM_PRIZE = re.compile(
    r"\b(selamat|congratulation|congrats)\b.{0,30}"
    r"\b(menang|terpilih|beruntung|mendapat|memenangkan|hadiah|prize|reward)\b",
    re.IGNORECASE,
)


def detect_scam(text: str) -> tuple[bool, float, list[str]]:
    """
    Deteksi penipuan finansial/investasi bodong.
    Return: (hit, confidence, reasons)
    """
    reasons: list[str] = []
    score = 0.0

    fin = _SCAM_FINANCIAL.search(text)
    urg = _SCAM_URGENCY.search(text)
    cnt = _SCAM_CONTACT.search(text)
    prz = _SCAM_PRIZE.search(text)

    if fin:
        score += 0.45
        reasons.append(f"pola scam finansial: {fin.group()[:30]}")

    if urg:
        score += 0.20
        reasons.append(f"urgensi palsu: {urg.group()[:30]}")

    if cnt and (fin or urg):
        score += 0.15
        reasons.append("instruksi hubungi + scam signal")

    if prz:
        score += 0.50
        reasons.append(f"hadiah/pemenang palsu: {prz.group()[:40]}")

    # Kombinasi kuat: financial + urgency
    if fin and urg:
        score += 0.15
        reasons.append("financial scam + tekanan waktu")

    hit = score >= 0.45
    return hit, min(1.0, round(score, 4)), reasons


# ══════════════════════════════════════════════════════════════════════════════
# KATEGORI 4 — PROMO VIRAL / BROADCAST SPAM
# ══════════════════════════════════════════════════════════════════════════════

_PROMO_SPREAD = re.compile(
    r"\b(broadcast|bc|forward|sebarkan|sebar|viralkan|share ke|kirim ke|"
    r"teruskan|terusin|copy paste|copas|share this|share ke semua|"
    r"kirim ke semua|share ke teman)\b",
    re.IGNORECASE,
)

_PROMO_GIVEAWAY = re.compile(
    r"\b(giveaway|give away|GA|berhadiah|undian|kuis berhadiah|"
    r"kuis berhadiah|lomba berhadiah|doorprize|gift|gratis untuk|"
    r"gratis bagi|dibagi gratis|bagi-bagi|bagi bagi|dibagikan)\b",
    re.IGNORECASE,
)


def detect_promo_viral(text: str) -> tuple[bool, float, list[str]]:
    """
    Deteksi spam promosi/broadcast viral.
    Return: (hit, confidence, reasons)
    """
    reasons: list[str] = []
    score = 0.0

    spread = _PROMO_SPREAD.search(text)
    give   = _PROMO_GIVEAWAY.search(text)

    if spread:
        score += 0.50
        reasons.append(f"instruksi penyebaran: {spread.group()[:30]}")

    if give:
        score += 0.35
        reasons.append(f"giveaway/undian: {give.group()[:30]}")

    hit = score >= 0.45
    return hit, min(1.0, round(score, 4)), reasons


# ══════════════════════════════════════════════════════════════════════════════
# KATEGORI 5 — BIO PROMO (v3.1)
# Ajakan melihat bio/profil Telegram untuk keperluan promosi/spam
# Pola: "cek bio aku ada info", "lihat profil saya", "kunjungi bio admin"
# ══════════════════════════════════════════════════════════════════════════════

_BIO_ACTION = re.compile(
    r"\b(cek|check|lihat|liat|kunjungi|buka|tengok|intip|kepoin|"
    r"klik|tap|tap di|visit|pantengin)\b",
    re.IGNORECASE,
)

_BIO_TARGET = re.compile(
    r"\b(bio|profil|profile|about|about me|deskripsi|info profil|"
    r"akun kami|akun saya|akun aku|akun admin)\b",
    re.IGNORECASE,
)

_BIO_SELF_REF = re.compile(
    r"\b(aku|saya|gue|gw|ane|ana|kami|kita|admin|owner|bot|kita punya|"
    r"punya kami|punya saya|milik kami|milik saya)\b",
    re.IGNORECASE,
)

_BIO_PROMO_SIGNAL = re.compile(
    r"\b(link|info|promo|penawaran|harga|daftar|join|gabung|gratis|diskon|"
    r"konten|video|foto|channel|grup|wa|whatsapp|telegram|invite|"
    r"order|jual|beli|produk|jasa|layanan|undangan|event|acara)\b",
    re.IGNORECASE,
)

# Pola langsung "ada di bio" — sinyal kuat tanpa perlu verb
_BIO_DIRECT = re.compile(
    r"\b(ada di bio|info di bio|link di bio|cek di bio|"
    r"ada di profil|info di profil|link di profil|"
    r"di bio aku|di bio saya|di bio gue|di bio kami|di bio admin|"
    r"di profil aku|di profil saya|di profil kami)\b",
    re.IGNORECASE,
)


def detect_bio_promo(text: str) -> tuple[bool, float, list[str]]:
    """
    Deteksi ajakan melihat bio/profil untuk keperluan spam/promosi.
    Menangkap pola seperti:
      - "cek bio aku ada promo menarik"
      - "lihat profil saya untuk info lebih lanjut"
      - "info ada di bio admin"
      - "kunjungi bio kami"
    Return: (hit, confidence, reasons)
    """
    reasons: list[str] = []
    score = 0.0

    has_action   = bool(_BIO_ACTION.search(text))
    has_target   = bool(_BIO_TARGET.search(text))
    has_self_ref = bool(_BIO_SELF_REF.search(text))
    has_signal   = bool(_BIO_PROMO_SIGNAL.search(text))
    has_direct   = bool(_BIO_DIRECT.search(text))

    # Pola langsung (paling kuat): "ada di bio aku" / "info di bio saya"
    if has_direct:
        score += 0.60
        reasons.append("ajakan langsung lihat bio/profil untuk info")

    # Pola terkuat berikutnya: verb + bio + referensi diri + sinyal promo
    if has_action and has_target and has_self_ref and has_signal:
        score += 0.55
        reasons.append("ajakan lihat bio sendiri dengan sinyal promo")

    # Pola kuat: verb + bio + referensi diri
    elif has_action and has_target and has_self_ref:
        score += 0.40
        reasons.append("ajakan lihat bio/profil sendiri")

    # Pola moderate: bio + referensi diri + sinyal promo (tanpa verb eksplisit)
    elif has_target and has_self_ref and has_signal:
        score += 0.35
        reasons.append("bio dengan sinyal promosi/link")

    # Pola lemah: hanya verb + bio
    elif has_action and has_target:
        score += 0.25
        reasons.append("ajakan lihat bio tanpa referensi jelas")

    hit = score >= 0.45
    return hit, min(1.0, round(score, 4)), reasons


# ══════════════════════════════════════════════════════════════════════════════
# KOORDINATOR UTAMA
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CategoryResult:
    """Hasil deteksi semua kategori untuk satu pesan."""
    category: str         = "NONE"   # kategori utama yang memicu
    hit: bool             = False
    confidence: float     = 0.0
    reasons: list[str]    = field(default_factory=list)
    all_scores: dict      = field(default_factory=dict)

    def as_kata_kunci(self) -> str:
        pct = f"{self.confidence * 100:.0f}%"
        top = self.reasons[0][:60] if self.reasons else "-"
        return f"[CAT-{self.category} {pct}] {top}"

    def as_pola_str(self) -> str:
        return f"[NEXUS_CATEGORY_DETECTOR] category={self.category} confidence={self.confidence:.4f}"


class CategoryDetector:
    """
    Koordinator semua detektor kategori.

    Usage (di nexus_group.py):
        from nexus.ai_core.category_detector import get_category_detector
        cat = get_category_detector()
        result = cat.detect(teks)
        if result.hit:
            matched.append((result.as_kata_kunci(), result.as_pola_str()))
    """

    def detect(self, text: str) -> CategoryResult:
        """
        Jalankan semua detektor dan kembalikan hasil kategori dengan confidence tertinggi.
        """
        results: list[tuple[str, bool, float, list[str]]] = [
            ("GROUP_INVITE", *detect_group_invite(text)),
            ("PORN",         *detect_porn(text)),
            ("SCAM",         *detect_scam(text)),
            ("PROMO_VIRAL",  *detect_promo_viral(text)),
            ("BIO_PROMO",    *detect_bio_promo(text)),
        ]

        all_scores = {cat: conf for cat, _, conf, _ in results}

        # Pilih kategori dengan confidence tertinggi yang melewati threshold
        best = max(results, key=lambda x: x[2])
        cat_name, hit, conf, reasons = best

        return CategoryResult(
            category   = cat_name if hit else "NONE",
            hit        = hit,
            confidence = conf,
            reasons    = reasons,
            all_scores = all_scores,
        )

    def detect_all_hits(self, text: str) -> list[CategoryResult]:
        """
        Kembalikan SEMUA kategori yang hit (untuk keroyokan / multi-category).
        """
        checks = [
            ("GROUP_INVITE", *detect_group_invite(text)),
            ("PORN",         *detect_porn(text)),
            ("SCAM",         *detect_scam(text)),
            ("PROMO_VIRAL",  *detect_promo_viral(text)),
            ("BIO_PROMO",    *detect_bio_promo(text)),
        ]
        all_scores = {cat: conf for cat, _, conf, _ in checks}
        hits = []
        for cat_name, hit, conf, reasons in checks:
            if hit:
                hits.append(CategoryResult(
                    category   = cat_name,
                    hit        = True,
                    confidence = conf,
                    reasons    = reasons,
                    all_scores = all_scores,
                ))
        return hits


# Singleton global
_category_detector: CategoryDetector | None = None


def get_category_detector() -> CategoryDetector:
    global _category_detector
    if _category_detector is None:
        _category_detector = CategoryDetector()
    return _category_detector
