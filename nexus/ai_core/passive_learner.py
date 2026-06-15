"""
nexus/ai_core/passive_learner.py
──────────────────────────────────
Passive Learner — AI belajar dari SETIAP pesan yang dilihatnya.

Cara kerja:
  SPAM path:
    Saat pesan dihapus (confidence >= AUTO_LEARN_SPAM_THRESHOLD, atau force_learn=True):
    → train Bayes sebagai spam
    → simpan ke nexus_kalimat_db (corpus untuk midnight regenerasi)
    → update adaptive threshold

  HAM path:
    Saat pesan lolos bersih (AI confidence < HAM_LEARN_THRESHOLD):
    → train Bayes sebagai ham (throttled 1 dari HAM_SAMPLE_RATE)
    → TIDAK disimpan ke corpus (corpus hanya untuk spam)

  Rate limiting:
    → Max AUTO_LEARN_PER_HOUR spam auto-learn per jam (mencegah poisoning)
    → Ham learning di-throttle per HAM_SAMPLE_RATE
    → force_learn=True melewati threshold confidence, tapi TETAP kena rate limit

Keamanan:
  → Pesan pendek (< MIN_TEXT_LEN karakter) dilewati
  → Pesan yang sudah dikonfirmasi whitelist tidak di-learn sebagai spam
  → force_learn digunakan untuk konfirmasi rule-based (regex, CategoryDetector)
    yang sudah pasti spam — threshold confidence tidak diperlukan
"""

from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass, field


# ─── Konfigurasi ──────────────────────────────────────────────────────────────

AUTO_LEARN_SPAM_THRESHOLD = 0.70   # min confidence untuk auto-learn spam
                                    # (diturunkan dari 0.85 — pesan sudah dikonfirmasi
                                    #  dihapus, threshold lebih rendah aman)
HAM_LEARN_THRESHOLD       = 0.25   # max confidence untuk belajar sebagai ham
HAM_SAMPLE_RATE           = 8      # belajar ham 1 dari setiap N pesan bersih
AUTO_LEARN_PER_HOUR       = 60     # batas auto-learn spam per jam (anti-poisoning)
MIN_TEXT_LEN              = 12     # minimal panjang teks yang dipelajari


# ─── Statistik ────────────────────────────────────────────────────────────────

@dataclass
class PassiveLearnerStats:
    spam_autolearned:  int   = 0    # total spam yang dipelajari secara otomatis
    spam_force_learned:int   = 0    # total spam yang dipelajari via force_learn
    ham_learned:       int   = 0    # total ham yang dipelajari
    skipped_short:     int   = 0    # dilewati karena teks terlalu pendek
    skipped_ratelimit: int   = 0    # dilewati karena rate limit
    ham_counter:       int   = 0    # counter untuk throttle HAM
    hourly_bucket:     int   = 0    # jumlah auto-learn dalam jam ini
    bucket_reset_at:   float = field(default_factory=time.time)


# ─── Passive Learner ──────────────────────────────────────────────────────────

class PassiveLearner:
    """
    Mengamati setiap keputusan filter dan melatih AI secara diam-diam.

    Tidak pernah di-call secara langsung dari luar; dihubungkan via bridge.py.
    """

    def __init__(self):
        self.stats = PassiveLearnerStats()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _check_rate_limit(self) -> bool:
        """Return True jika masih dalam batas rate limit. False = skip."""
        now = time.time()
        # Reset bucket setiap jam
        if now - self.stats.bucket_reset_at >= 3600:
            self.stats.hourly_bucket = 0
            self.stats.bucket_reset_at = now

        if self.stats.hourly_bucket >= AUTO_LEARN_PER_HOUR:
            self.stats.skipped_ratelimit += 1
            return False
        return True

    def _valid_text(self, text: str) -> bool:
        """Return True jika teks layak dipelajari."""
        if not text or len(text.strip()) < MIN_TEXT_LEN:
            self.stats.skipped_short += 1
            return False
        return True

    # ── API Utama ─────────────────────────────────────────────────────────────

    async def observe_spam_executed(
        self,
        text: str,
        confidence: float,
        save_to_corpus: bool = True,
        force_learn: bool = False,
    ) -> bool:
        """
        Dipanggil saat pesan BERHASIL dihapus sebagai spam.

        Args:
            text:           teks asli pesan
            confidence:     skor confidence AI (0.0 – 1.0)
            save_to_corpus: jika True, simpan ke nexus_kalimat_db
            force_learn:    jika True, melewati pengecekan threshold confidence
                            (digunakan untuk konfirmasi rule-based: regex, CategoryDetector)

        Return True jika berhasil belajar, False jika dilewati.
        """
        if not self._valid_text(text):
            return False

        # Cek confidence — bisa di-skip dengan force_learn=True
        if not force_learn and confidence < AUTO_LEARN_SPAM_THRESHOLD:
            return False

        if not self._check_rate_limit():
            return False

        try:
            from nexus.ai_core.bridge import get_nexus_ai
            ai = get_nexus_ai()
            if not ai._loaded:
                return False

            # Train Bayes
            ai.learn(text, is_spam=True)

            # Simpan ke corpus untuk midnight regenerasi
            if save_to_corpus:
                try:
                    from database import nexus_insert_kalimat
                    from plugins.nexus.engine import pipeline_pembersihan
                    clean = pipeline_pembersihan(text)
                    if clean:
                        await nexus_insert_kalimat(clean)
                except Exception as e:
                    print(f"[PassiveLearner] corpus insert error (non-fatal): {e}")

            # Update statistik
            self.stats.spam_autolearned += 1
            self.stats.hourly_bucket    += 1
            if force_learn:
                self.stats.spam_force_learned += 1

            # Auto-save model setiap 25 auto-learn
            if self.stats.spam_autolearned % 25 == 0:
                await ai.save()
                src = "force" if force_learn else f"conf={confidence:.2f}"
                print(
                    f"[PassiveLearner] 💾 Auto-save model setelah "
                    f"{self.stats.spam_autolearned} spam auto-learn ({src})"
                )

            return True

        except Exception as e:
            print(f"[PassiveLearner] observe_spam error (non-fatal): {e}")
            return False

    def observe_ham_passed(self, text: str, confidence: float) -> bool:
        """
        Dipanggil saat pesan LOLOS sebagai ham (bukan spam).

        Ham learning di-throttle karena pesan ham jauh lebih banyak
        dari spam — jika semua dipelajari, model akan bias ke ham.

        Return True jika belajar, False jika di-throttle/skip.
        """
        if not self._valid_text(text):
            return False

        if confidence > HAM_LEARN_THRESHOLD:
            return False   # Masih terlalu mencurigakan untuk dijadikan ham

        # Throttle: hanya 1 dari HAM_SAMPLE_RATE pesan
        self.stats.ham_counter += 1
        if self.stats.ham_counter % HAM_SAMPLE_RATE != 0:
            return False

        try:
            from nexus.ai_core.bridge import get_nexus_ai
            ai = get_nexus_ai()
            if not ai._loaded:
                return False

            ai.learn(text, is_spam=False)
            self.stats.ham_learned += 1
            return True

        except Exception as e:
            print(f"[PassiveLearner] observe_ham error (non-fatal): {e}")
            return False

    def get_stats(self) -> dict:
        return {
            "spam_autolearned":         self.stats.spam_autolearned,
            "spam_force_learned":       self.stats.spam_force_learned,
            "ham_learned":              self.stats.ham_learned,
            "skipped_short":            self.stats.skipped_short,
            "skipped_ratelimit":        self.stats.skipped_ratelimit,
            "hourly_spam_learned":      self.stats.hourly_bucket,
            "hourly_limit":             AUTO_LEARN_PER_HOUR,
            "ham_sample_rate":          HAM_SAMPLE_RATE,
            "min_confidence_autolearn": AUTO_LEARN_SPAM_THRESHOLD,
        }


# ─── Singleton ────────────────────────────────────────────────────────────────

_passive_learner: PassiveLearner | None = None


def get_passive_learner() -> PassiveLearner:
    global _passive_learner
    if _passive_learner is None:
        _passive_learner = PassiveLearner()
    return _passive_learner
