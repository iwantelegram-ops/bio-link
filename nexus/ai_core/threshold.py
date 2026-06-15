"""
nexus/ai_core/threshold.py
───────────────────────────
Adaptive Threshold Engine — kalibrasi threshold deteksi spam secara otomatis.

Cara kerja:
  - Menyimpan distribusi skor dari sampel yang sudah dikonfirmasi (spam/ham)
  - Setiap 10 sampel baru, threshold dikalibrasi ulang ke titik tengah
    antara rata-rata skor spam dan rata-rata skor ham
  - Threshold di-clamp antara MIN_THRESHOLD dan MAX_THRESHOLD
  - Serialisasi via dict (JSON-safe), simpan max 500 skor terakhir per kelas
"""

from __future__ import annotations

from nexus.ai_core.constants import DEFAULT_THRESHOLD

# Batas threshold yang diizinkan (menghindari threshold terlalu ekstrem)
MIN_THRESHOLD = 0.35
MAX_THRESHOLD = 0.80

# Kalibrasi setiap N sampel baru
CALIBRATE_EVERY = 10


class AdaptiveThreshold:
    """
    Threshold yang berevolusi dari data konfirmasi nyata.

    Usage:
        at = AdaptiveThreshold()
        at.update(score=0.82, is_spam=True)
        threshold = at.threshold  # float
    """

    def __init__(self):
        self.spam_scores: list[float] = []
        self.ham_scores:  list[float] = []
        self._threshold:  float = DEFAULT_THRESHOLD
        self._update_count: int = 0

    @property
    def threshold(self) -> float:
        return self._threshold

    def update(self, score: float, is_spam: bool) -> None:
        """
        Tambahkan satu data point konfirmasi dan kalibrasi ulang jika perlu.

        score   — skor gabungan (0.0–1.0) yang dihitung oleh NexusAICore
        is_spam — apakah pesan benar-benar spam (dikonfirmasi oleh admin/sistem)
        """
        if is_spam:
            self.spam_scores.append(float(score))
        else:
            self.ham_scores.append(float(score))

        self._update_count += 1

        if self._update_count % CALIBRATE_EVERY == 0:
            self._calibrate()

    def _calibrate(self) -> None:
        """
        Hitung ulang threshold sebagai titik tengah distribusi skor.

        Jika hanya ada satu kelas, biarkan threshold tidak berubah.
        """
        if not self.spam_scores or not self.ham_scores:
            return

        spam_mean = sum(self.spam_scores) / len(self.spam_scores)
        ham_mean  = sum(self.ham_scores)  / len(self.ham_scores)

        # Titik tengah sebagai threshold optimal sederhana
        new_threshold = (spam_mean + ham_mean) / 2.0

        # Clamp ke range yang aman
        self._threshold = round(
            max(MIN_THRESHOLD, min(MAX_THRESHOLD, new_threshold)),
            3,
        )

    def reset(self) -> None:
        """Reset ke threshold default."""
        self.spam_scores   = []
        self.ham_scores    = []
        self._threshold    = DEFAULT_THRESHOLD
        self._update_count = 0

    def info(self) -> dict:
        """Ringkasan statistik threshold."""
        return {
            "threshold":      self._threshold,
            "spam_samples":   len(self.spam_scores),
            "ham_samples":    len(self.ham_scores),
            "spam_mean":      round(sum(self.spam_scores) / max(1, len(self.spam_scores)), 3),
            "ham_mean":       round(sum(self.ham_scores)  / max(1, len(self.ham_scores)),  3),
            "update_count":   self._update_count,
        }

    # ── Serialisasi ────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Konversi ke dict (JSON-serializable). Simpan max 500 skor terakhir."""
        return {
            "threshold":    self._threshold,
            "spam_scores":  self.spam_scores[-500:],
            "ham_scores":   self.ham_scores[-500:],
            "update_count": self._update_count,
        }

    def from_dict(self, d: dict) -> None:
        """Muat dari dict."""
        self._threshold    = float(d.get("threshold", DEFAULT_THRESHOLD))
        self.spam_scores   = [float(x) for x in d.get("spam_scores", [])]
        self.ham_scores    = [float(x) for x in d.get("ham_scores",  [])]
        self._update_count = int(d.get("update_count", 0))
