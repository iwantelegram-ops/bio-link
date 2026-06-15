"""
nexus/ai_core/bayes.py
───────────────────────
Multinomial Naive Bayes Classifier berbasis n-gram untuk deteksi spam.

Fitur:
  - Character trigram + 4-gram + word bigram
  - Laplace smoothing (alpha konfigurasi)
  - Log-space arithmetic (menghindari underflow float)
  - Online learning: update satu per satu tanpa retrain ulang
  - Serialisasi/deserialisasi via dict (JSON-safe)
"""

from __future__ import annotations

import math
from collections import Counter

from nexus.ai_core.constants import SEED_SPAM_VOCAB, HAM_SEED
from nexus.ai_core.normalizer import extract_features


class NaiveBayesSpamClassifier:
    """
    Multinomial Naive Bayes dengan Laplace smoothing.

    Usage:
        nb = NaiveBayesSpamClassifier()
        nb.seed_from_vocabulary()          # seeding awal
        nb.train("teks spam", True)        # online learning
        prob = nb.predict_proba("teks")    # 0.0–1.0
    """

    def __init__(self, alpha: float = 1.0):
        self.alpha       = alpha
        self.class_count: dict[str, int]     = {"spam": 0, "ham": 0}
        self.feat_count:  dict[str, Counter] = {
            "spam": Counter(),
            "ham":  Counter(),
        }
        self._vocab:     set[str] = set()
        self.vocab_size: int      = 0

    # ── Training ──────────────────────────────────────────────────────────────

    def train(self, text: str, is_spam: bool) -> None:
        """Tambahkan satu contoh ke model (online learning)."""
        if not text or not text.strip():
            return
        label = "spam" if is_spam else "ham"
        self.class_count[label] += 1
        feats = extract_features(text)
        self.feat_count[label].update(feats)
        self._vocab.update(feats)
        self.vocab_size = len(self._vocab)

    def seed_from_vocabulary(self) -> None:
        """
        Seed model awal dari kosakata spam built-in.
        Dipanggil saat tidak ada data training tersimpan.
        """
        for category, tokens in SEED_SPAM_VOCAB.items():
            for token in tokens:
                self.train(token, is_spam=True)
                # Tambah frasa konteks agar model belajar kombinasi
                self.train(f"{token} daftar sekarang", is_spam=True)
                self.train(f"promo {token} gratis", is_spam=True)

        for ham_text in HAM_SEED:
            self.train(ham_text, is_spam=False)

    # ── Prediksi ──────────────────────────────────────────────────────────────

    def predict_proba(self, text: str) -> float:
        """
        Hitung probabilitas spam (0.0 = pasti ham, 1.0 = pasti spam).

        Menggunakan log-space untuk menghindari floating point underflow
        pada dokumen panjang.
        """
        total = sum(self.class_count.values())
        if total < 2:
            return 0.5   # belum cukup data

        feats = extract_features(text)
        if not feats:
            return 0.5

        log_p: dict[str, float] = {}

        for label in ("spam", "ham"):
            cnt = self.class_count[label]
            if cnt == 0:
                log_p[label] = -1e9
                continue

            log_p[label] = math.log(cnt / total)
            total_feats  = sum(self.feat_count[label].values())
            vocab_extra  = self.vocab_size + 1
            denom        = total_feats + self.alpha * vocab_extra

            for f in feats:
                num = self.feat_count[label].get(f, 0) + self.alpha
                log_p[label] += math.log(num / denom)

        # Softmax untuk konversi ke probabilitas
        max_lp = max(log_p.values())
        exp_s  = math.exp(log_p["spam"] - max_lp)
        exp_h  = math.exp(log_p["ham"]  - max_lp)
        denom  = exp_s + exp_h

        if denom == 0:
            return 0.5

        return round(exp_s / denom, 6)

    def predict(self, text: str) -> bool:
        """Prediksi biner: True = spam, False = ham."""
        return self.predict_proba(text) >= 0.5

    # ── Serialisasi ────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Konversi model ke dict (JSON-serializable)."""
        return {
            "alpha":       self.alpha,
            "class_count": self.class_count,
            "feat_spam":   dict(self.feat_count["spam"]),
            "feat_ham":    dict(self.feat_count["ham"]),
            "vocab":       list(self._vocab),
        }

    def from_dict(self, d: dict) -> None:
        """Muat model dari dict."""
        self.alpha       = d.get("alpha", 1.0)
        self.class_count = d.get("class_count", {"spam": 0, "ham": 0})
        self.feat_count  = {
            "spam": Counter(d.get("feat_spam", {})),
            "ham":  Counter(d.get("feat_ham",  {})),
        }
        self._vocab      = set(d.get("vocab", []))
        self.vocab_size  = len(self._vocab)

    # ── Info ──────────────────────────────────────────────────────────────────

    def info(self) -> dict:
        """Ringkasan statistik classifier."""
        return {
            "spam_samples": self.class_count["spam"],
            "ham_samples":  self.class_count["ham"],
            "vocab_size":   self.vocab_size,
            "alpha":        self.alpha,
        }
