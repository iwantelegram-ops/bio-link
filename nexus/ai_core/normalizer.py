"""
nexus/ai_core/normalizer.py
───────────────────────────
Fungsi normalisasi teks, tokenisasi, dan pembuatan n-gram.
Digunakan oleh bayes.py, scorer.py, dan miner.py.
"""

import re
import unicodedata
from nexus.ai_core.constants import LEET_MAP


def normalize_text(text: str, aggressive: bool = False) -> str:
    """
    Normalisasi teks untuk pencocokan spam.

    Langkah:
      1. NFKD decomposition → hilangkan font Unicode mewah (bold, italic, dll)
      2. Buang combining characters (aksen, modifier)
      3. Lowercase
      4. Leet-speak → huruf normal (0→o, 1→i, 3→e, dst)
      5. Deduplikasi huruf berulang (ggggg → gg, bukan g, agar masih terbaca)
      6. (aggressive=True) Hapus tanda baca, normalkan spasi

    aggressive=True cocok untuk feature scoring & tokenisasi.
    aggressive=False cocok untuk regex matching (pertahankan spasi & tanda baca).
    """
    if not text:
        return ""

    # Step 1-2: Unicode normalization
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))

    # Step 3: Lowercase
    t = t.lower()

    # Step 4: Leet-speak substitution
    for ch, rep in LEET_MAP.items():
        t = t.replace(ch, rep)

    # Step 5: Deduplikasi huruf berulang (simpan 2, bukan 1, untuk keterbacaan)
    t = re.sub(r"([a-z])\1{2,}", r"\1\1", t)

    # Step 6: Aggressive cleanup
    if aggressive:
        t = re.sub(r"[^\w\s]", " ", t)
        t = re.sub(r"\s+", " ", t).strip()

    return t


def tokenize(text: str, min_len: int = 2) -> list[str]:
    """
    Tokenisasi kata dari teks setelah normalisasi agresif.
    Filter kata yang terlalu pendek (< min_len).
    """
    t = normalize_text(text, aggressive=True)
    return [w for w in t.split() if len(w) >= min_len]


def char_ngrams(text: str, n: int = 3) -> list[str]:
    """
    Character n-gram dari teks yang dinormalisasi.
    Spasi diganti underscore agar n-gram lintas kata lebih informatif.

    Contoh (n=3): "slot" → ["slo", "lot"]
    """
    t = normalize_text(text, aggressive=True).replace(" ", "_")
    if len(t) < n:
        return [t]
    return [t[i:i + n] for i in range(len(t) - n + 1)]


def word_ngrams(tokens: list[str], n: int = 2) -> list[str]:
    """
    Word n-gram dari list token.

    Contoh (n=2): ["slot", "gacor"] → ["slot gacor"]
    """
    if len(tokens) < n:
        return []
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def extract_features(text: str) -> list[str]:
    """
    Ekstrak semua feature dari teks untuk Naive Bayes:
    - Character trigram (n=3)
    - Character 4-gram (n=4)
    - Unigram kata (prefix W:)
    - Bigram kata (prefix B:)

    Prefix memisahkan namespace feature agar tidak tabrakan.
    """
    tokens = tokenize(text)
    feats: list[str] = []

    feats += char_ngrams(text, 3)
    feats += char_ngrams(text, 4)
    feats += [f"W:{w}" for w in tokens]
    feats += [f"B:{b}" for b in word_ngrams(tokens, 2)]

    return feats
