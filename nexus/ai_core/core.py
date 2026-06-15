"""
nexus/ai_core/core.py
──────────────────────
NexusAICore v3.1 — Koordinator utama semua layer deteksi spam.

Layer deteksi:
  Layer 1: Naive Bayes (bayes.py)
  Layer 2: Feature Scoring (scorer.py)
  Layer 3: Corpus Pattern Mining (miner.py) — tengah malam
  Layer 4: Adaptive Threshold (threshold.py)
  Layer 5: Category Detector (category_detector.py) — GROUP_INVITE, PORN,
           SCAM, PROMO_VIRAL, BIO_PROMO
  Layer 6: Context Filter (context_filter.py)

v3.1 — Perubahan:
  - Tambah kategori BIO_PROMO ke CategoryDetector
  - Bersihkan kode mati di learn()
  - Bobot gabungan: Bayes 25% + Feature 45% + Category 30%
"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from datetime import datetime
from typing import NamedTuple

from database import db as _db

from nexus.ai_core.bayes      import NaiveBayesSpamClassifier
from nexus.ai_core.scorer     import FeatureScorer
from nexus.ai_core.miner      import CorpusPatternMiner
from nexus.ai_core.threshold  import AdaptiveThreshold
from nexus.ai_core.constants  import (
    AI_CORE_VERSION,
    LABEL_SPAM, LABEL_HAM, LABEL_UNCERTAIN,
    DEFAULT_THRESHOLD,
    HIGH_CONFIDENCE_BAYES,
    MIN_CONFIDENCE_DELETE,
    AUTO_DETECT_BONUS,
    CATEGORY_DETECT_WEIGHT,
    CONTEXT_FILTER_ENABLED,
)

# ─── Path File ────────────────────────────────────────────────────────────────
# MODEL_PATH dan LOG_PATH mengikuti _DATA_DIR dari database.py
# yaitu ~/.nexusai/<CODE_BOT>/ — selalu sama dari direktori manapun bot dijalankan
_DIR       = Path(__file__).parent
try:
    from database import _DATA_DIR as _BOT_DATA_DIR
    MODEL_PATH = _BOT_DATA_DIR / "nexus_model.json"
    LOG_PATH   = _BOT_DATA_DIR / "nexus_ai.log"
except Exception:
    # Fallback ke folder script jika database belum diinit
    MODEL_PATH = _DIR / "nexus_model.json"
    LOG_PATH   = _DIR / "nexus_ai.log"

# ─── MongoDB collection untuk model AI ────────────────────────────────────────
# Model AI (Bayes + adaptive) disimpan di MongoDB agar sinkron lintas direktori/HP.
# Key dokumen: {"_id": "nexus_ai_model"}
# Fallback ke file lokal (~/.nexusai/<CODE_BOT>/nexus_model.json) jika MongoDB gagal.
_ai_model_db = _db["nexus_ai_model"]


# ─── Hasil Prediksi ───────────────────────────────────────────────────────────

class SpamResult(NamedTuple):
    is_spam:    bool
    confidence: float
    label:      str
    reasons:    list[str]
    layer:      str

    def __str__(self) -> str:
        tag = "🚨 SPAM" if self.is_spam else "✅ AMAN"
        pct = f"{self.confidence * 100:.1f}%"
        top = self.reasons[0] if self.reasons else "-"
        return f"{tag} [{pct}] via {self.layer} | {top}"

    def as_kata_kunci(self) -> str:
        pct = f"{self.confidence * 100:.0f}%"
        top = self.reasons[0][:60] if self.reasons else "-"
        return f"[AI-AUTO {pct}] {top}"

    def as_pola_str(self) -> str:
        return f"[NEXUS_AI_CORE v{AI_CORE_VERSION}] confidence={self.confidence:.4f}"


# ══════════════════════════════════════════════════════════════════════════════
# NEXUS AI CORE v3.1
# ══════════════════════════════════════════════════════════════════════════════

class NexusAICore:
    """
    Koordinator 6 layer deteksi spam + passive learning.

    Cara pakai:
        from nexus.ai_core import get_nexus_ai, init_nexus_ai
        await init_nexus_ai()
        ai = get_nexus_ai()
        result = ai.auto_detect(teks, metadata)
    """

    VERSION = AI_CORE_VERSION

    def __init__(self):
        self.bayes    = NaiveBayesSpamClassifier()
        self.scorer   = FeatureScorer()
        self.miner    = CorpusPatternMiner()
        self.adaptive = AdaptiveThreshold()
        self._loaded  = False
        self._learn_count = 0

        self.stats = {
            "total_checked":  0,
            "total_spam":     0,
            "total_ham":      0,
            "learn_count":    0,
            "autolearn_spam": 0,
            "autolearn_ham":  0,
            "last_updated":   "",
        }

    # ── Load / Save ──────────────────────────────────────────────────────────

    async def load(self) -> None:
        # ── 1. Coba load dari MongoDB dulu ──────────────────────────────────
        try:
            doc = await _ai_model_db.find_one({"_id": "nexus_ai_model"})
            if doc and "bayes" in doc:
                self.bayes.from_dict(doc.get("bayes", {}))
                self.adaptive.from_dict(doc.get("adaptive", {}))
                self.stats   = doc.get("stats", self.stats)
                self._loaded = True
                self._log(
                    f"[MongoDB] Model v{doc.get('version','?')} dimuat — "
                    f"spam={self.bayes.class_count['spam']} "
                    f"ham={self.bayes.class_count['ham']} "
                    f"vocab={self.bayes.vocab_size}"
                )
                return
        except Exception as e:
            self._log(f"[MongoDB] Gagal load model: {e} — coba file lokal.")

        # ── 2. Fallback ke file lokal ────────────────────────────────────────
        if MODEL_PATH.exists():
            try:
                with open(MODEL_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.bayes.from_dict(data.get("bayes", {}))
                self.adaptive.from_dict(data.get("adaptive", {}))
                self.stats   = data.get("stats", self.stats)
                self._loaded = True
                self._log(
                    f"[File] Model v{data.get('version','?')} dimuat — "
                    f"spam={self.bayes.class_count['spam']} "
                    f"ham={self.bayes.class_count['ham']} "
                    f"vocab={self.bayes.vocab_size}"
                )
                # Migrasi otomatis: upload model lokal ke MongoDB
                await self.save()
                self._log("[Migrasi] Model lokal berhasil diupload ke MongoDB.")
                return
            except Exception as e:
                self._log(f"[File] Gagal load model: {e} — init ulang dari seed.")

        # ── 3. Tidak ada data sama sekali → mulai dari seed ─────────────────
        await self._init_fresh()

    async def _init_fresh(self) -> None:
        self._log("Inisialisasi model baru dari seed vocabulary...")
        self.bayes.seed_from_vocabulary()
        self._loaded = True
        self._log(
            f"Seed selesai — spam={self.bayes.class_count['spam']} "
            f"ham={self.bayes.class_count['ham']}"
        )
        await self.save()

    async def save(self) -> None:
        data = {
            "version":  self.VERSION,
            "saved_at": datetime.utcnow().isoformat(),
            "bayes":    self.bayes.to_dict(),
            "adaptive": self.adaptive.to_dict(),
            "stats":    self.stats,
        }

        # ── Simpan ke MongoDB (utama) ────────────────────────────────────────
        try:
            await _ai_model_db.update_one(
                {"_id": "nexus_ai_model"},
                {"$set": {**data, "_id": "nexus_ai_model"}},
                upsert=True,
            )
        except Exception as e:
            self._log(f"[MongoDB] Gagal simpan model: {e}")

        # ── Simpan ke file lokal (backup) ────────────────────────────────────
        try:
            MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(MODEL_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        except Exception as e:
            self._log(f"[File] Gagal simpan backup model: {e}")

    # ── Prediksi (Bayes + Feature) ───────────────────────────────────────────

    def predict(self, text: str, metadata: dict | None = None) -> SpamResult:
        """
        Prediksi Bayes + Feature saja (tanpa CategoryDetector/ContextFilter).
        Bobot: Bayes 35% + Feature 65%
        """
        if not text or not text.strip():
            return SpamResult(False, 0.0, LABEL_HAM, ["teks kosong"], "SKIP")

        self.stats["total_checked"] += 1

        bayes_prob               = self.bayes.predict_proba(text)
        feat_score, feat_reasons = self.scorer.score(text, metadata)
        combined                 = round(0.35 * bayes_prob + 0.65 * feat_score, 4)
        threshold                = self.adaptive.threshold

        if combined >= threshold:
            label, is_spam = LABEL_SPAM, True
        elif combined >= threshold - 0.10:
            label, is_spam = LABEL_UNCERTAIN, False
        else:
            label, is_spam = LABEL_HAM, False

        if is_spam:
            self.stats["total_spam"] += 1
        else:
            self.stats["total_ham"] += 1

        reasons = list(feat_reasons)
        if bayes_prob >= HIGH_CONFIDENCE_BAYES:
            reasons.insert(0, f"Bayes: {bayes_prob * 100:.0f}% probabilitas spam")
        elif bayes_prob >= 0.5 and not reasons:
            reasons.append(f"Bayes borderline: {bayes_prob * 100:.0f}%")
        if not reasons:
            reasons = ["tidak ada sinyal spam terdeteksi"]

        layer = "BAYES+FEATURE" if bayes_prob >= 0.5 else "FEATURE"
        return SpamResult(is_spam, combined, label, reasons, layer)

    # ── Auto Detect v3.1 (6 Layer) ───────────────────────────────────────────

    def auto_detect(
        self,
        text: str,
        metadata: dict | None = None,
    ) -> SpamResult:
        """
        Deteksi proaktif dengan 6 layer:
          1. Bayes  (25%)
          2. Feature (45%)
          3. CategoryDetector (30%) — GROUP_INVITE, PORN, SCAM, PROMO_VIRAL, BIO_PROMO
          4. ContextFilter — modifier konteks (negasi, pertanyaan, laporan)
          5. Threshold adaptif
          6. MIN_CONFIDENCE_DELETE gate

        Aman terhadap false positive: ContextFilter mengurangi skor
        ketika kalimat hanya membahas/melaporkan spam, bukan menyebarkannya.
        """
        if not text or not text.strip():
            return SpamResult(False, 0.0, LABEL_HAM, ["teks kosong"], "SKIP")

        self.stats["total_checked"] += 1
        reasons: list[str] = []

        # Layer 1 & 2: Bayes + Feature
        bayes_prob               = self.bayes.predict_proba(text)
        feat_score, feat_reasons = self.scorer.score(text, metadata)

        # Layer 3: CategoryDetector
        cat_score = 0.0
        cat_label = ""
        try:
            from nexus.ai_core.category_detector import get_category_detector
            cat_result = get_category_detector().detect(text)
            if cat_result.hit:
                cat_score = cat_result.confidence
                cat_label = cat_result.category
                reasons += cat_result.reasons
        except Exception as e:
            self._log(f"CategoryDetector error (non-fatal): {e}")

        # Skor gabungan v3.1:
        # Bayes 25% + Feature 45% + Category 30%
        bayes_w   = 0.25
        feat_w    = 0.45
        cat_w     = CATEGORY_DETECT_WEIGHT  # 0.30
        combined  = round(
            bayes_w * bayes_prob
            + feat_w * feat_score
            + cat_w  * cat_score,
            4,
        )

        # Layer 4: ContextFilter — modifier konteks
        if CONTEXT_FILTER_ENABLED:
            try:
                from nexus.ai_core.context_filter import get_context_filter
                ctx = get_context_filter().analyze(text)
                if ctx.reasons:
                    reasons += [f"[CTX] {r}" for r in ctx.reasons]
                combined = ctx.apply(combined)
            except Exception as e:
                self._log(f"ContextFilter error (non-fatal): {e}")

        # Layer 5: Adaptive threshold
        threshold = self.adaptive.threshold
        auto_thr  = threshold - AUTO_DETECT_BONUS

        # Susun reasons
        reasons = list(feat_reasons) + reasons
        if bayes_prob >= HIGH_CONFIDENCE_BAYES:
            reasons.insert(0, f"Bayes: {bayes_prob * 100:.0f}% probabilitas spam")
        if cat_label:
            reasons.insert(0, f"Kategori: {cat_label} ({cat_score * 100:.0f}%)")
        if not reasons:
            reasons = ["tidak ada sinyal spam terdeteksi"]

        layer_parts = []
        if bayes_prob >= 0.5:
            layer_parts.append("BAYES")
        if feat_score >= 0.5:
            layer_parts.append("FEATURE")
        if cat_score >= 0.45:
            layer_parts.append(f"CAT:{cat_label}")
        layer = "+".join(layer_parts) if layer_parts else "LOW"

        # Layer 6: MIN_CONFIDENCE_DELETE gate
        is_spam = combined >= auto_thr and combined >= MIN_CONFIDENCE_DELETE

        if is_spam:
            self.stats["total_spam"] += 1
            label = LABEL_SPAM
        elif combined >= auto_thr:
            label = LABEL_UNCERTAIN
            is_spam = False
        else:
            label = LABEL_HAM
            is_spam = False
            self.stats["total_ham"] += 1

        return SpamResult(is_spam, combined, label, reasons, layer + "+AUTO")

    # ── Online Learning ──────────────────────────────────────────────────────

    def learn(self, text: str, is_spam: bool) -> None:
        """Update model dari satu contoh. Panggil dari /spam atau passive learner."""
        if not text or not text.strip():
            return

        self.bayes.train(text, is_spam)

        feat_score, _ = self.scorer.score(text)
        bayes_p        = self.bayes.predict_proba(text)
        combined       = 0.35 * bayes_p + 0.65 * feat_score

        self.adaptive.update(combined, is_spam)

        self._learn_count += 1
        self.stats["learn_count"] += 1
        if is_spam:
            self.stats["autolearn_spam"] += 1
        else:
            self.stats["autolearn_ham"] += 1
        self.stats["last_updated"] = datetime.utcnow().isoformat()

    async def learn_bulk(self, items: list[tuple[str, bool]]) -> int:
        count = 0
        for text, is_spam in items:
            try:
                self.learn(text, is_spam)
                count += 1
            except Exception:
                pass
            if count % 50 == 0:
                await asyncio.sleep(0)
        return count

    # ── Midnight Regeneration ────────────────────────────────────────────────

    async def midnight_regeneration(
        self,
        kalimat_list: list[str],
    ) -> tuple[dict, list[tuple[str, str]]]:
        self._log(f"[MIDNIGHT] Mulai regenerasi dari {len(kalimat_list)} kalimat...")
        await self.learn_bulk([(k, True) for k in kalimat_list])

        patterns: list[tuple[str, str]] = []
        if len(kalimat_list) >= 2:
            try:
                patterns = self.miner.mine(kalimat_list)
                self._log(f"[MIDNIGHT] Miner: {len(patterns)} pola.")
            except Exception as e:
                self._log(f"[MIDNIGHT] Miner error: {e}")

        await self.save()

        summary = {
            "timestamp":   datetime.utcnow().isoformat(),
            "corpus_size": len(kalimat_list),
            "patterns":    len(patterns),
            "threshold":   self.adaptive.threshold,
            **self.stats,
        }
        self._log(
            f"[MIDNIGHT] Selesai — {len(patterns)} pola, "
            f"threshold={self.adaptive.threshold:.3f}"
        )
        return summary, patterns

    # ── Diagnostik ───────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        return {
            "version":     self.VERSION,
            "loaded":      self._loaded,
            "model_path":  str(MODEL_PATH),
            **self.bayes.info(),
            **self.adaptive.info(),
            **self.stats,
        }

    def explain(self, text: str, metadata: dict | None = None) -> str:
        result    = self.predict(text, metadata)
        bayes_p   = self.bayes.predict_proba(text)
        feat_s, _ = self.scorer.score(text, metadata)

        # Category & context
        cat_info = ""
        ctx_info = ""
        try:
            from nexus.ai_core.category_detector import get_category_detector
            cr = get_category_detector().detect(text)
            cat_info = f"\nCategory: {cr.category} ({cr.confidence*100:.1f}%) — {', '.join(cr.reasons[:2])}"
        except Exception:
            pass
        try:
            from nexus.ai_core.context_filter import get_context_filter
            ctx = get_context_filter().analyze(text)
            ctx_info = f"\nContext modifier: {ctx.modifier:+.3f} — {', '.join(ctx.reasons[:2])}"
        except Exception:
            pass

        lines = [
            "━━━━━━ NEXUS AI CORE v3.1 EXPLAIN ━━━━━━",
            f"Input:      {text[:80]}{'...' if len(text) > 80 else ''}",
            f"Bayes:      {bayes_p * 100:.1f}%",
            f"Feature:    {feat_s * 100:.1f}%",
            cat_info,
            ctx_info,
            f"Combined:   {result.confidence * 100:.1f}%",
            f"Threshold:  {self.adaptive.threshold * 100:.1f}%",
            f"Label:      {result.label}",
            f"Reasons:",
        ]
        for r in result.reasons[:5]:
            lines.append(f"  • {r}")
        return "\n".join(l for l in lines if l)

    def _log(self, msg: str) -> None:
        ts    = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        line  = f"[{ts}] {msg}"
        print(f"[NexusAICore] {msg}")
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
