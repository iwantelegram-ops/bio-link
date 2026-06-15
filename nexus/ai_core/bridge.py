"""
nexus/ai_core/bridge.py
────────────────────────
Jembatan integrasi antara NexusAICore dan sistem nexus.

v3.1 — Perubahan:
  nexus_ai_passive_observe()   — tambah parameter force_learn untuk konfirmasi
                                  rule-based (regex, CategoryDetector, bio filter)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from nexus.ai_core.core import NexusAICore, SpamResult, LOG_PATH


# ─── Log ke Database ──────────────────────────────────────────────────────────

async def _ai_log(
    aksi:       str,
    label:      str   = "-",
    confidence: float = 0.0,
    ringkasan:  str   = "",
    chat_id:    int   = 0,
) -> None:
    try:
        from database import ai_debug_log_insert
        await ai_debug_log_insert(
            aksi=aksi, label=label, confidence=confidence,
            ringkasan=ringkasan, chat_id=chat_id,
        )
    except Exception:
        pass


# ─── Singleton ────────────────────────────────────────────────────────────────

_instance:  NexusAICore | None  = None
_init_lock: asyncio.Lock | None = None


def _get_init_lock() -> asyncio.Lock:
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


def get_nexus_ai() -> NexusAICore:
    global _instance
    if _instance is None:
        _instance = NexusAICore()
    return _instance


async def init_nexus_ai() -> NexusAICore:
    global _instance
    async with _get_init_lock():
        if _instance is None:
            _instance = NexusAICore()
        if not _instance._loaded:
            await _instance.load()
            try:
                ver = _instance.VERSION
                sp  = _instance.bayes.class_count.get("spam", 0)
                hm  = _instance.bayes.class_count.get("ham", 0)
                await _ai_log(
                    aksi="⚡ MODEL DIMUAT",
                    confidence=float(_instance.adaptive.threshold),
                    ringkasan=f"AI v{ver} | Spam: {sp} | Ham: {hm} | Vocab: {_instance.bayes.vocab_size}",
                )
            except Exception:
                pass
    return _instance


# ─── Fungsi Integrasi ─────────────────────────────────────────────────────────

async def nexus_ai_auto_detect(
    text: str,
    metadata: dict | None = None,
    min_confidence: float = 0.70,
) -> SpamResult | None:
    """
    Auto-detect spam proaktif (6 layer v3.0).
    Return SpamResult jika spam terdeteksi, None jika aman.
    """
    ai = get_nexus_ai()
    if not ai._loaded:
        try:
            await ai.load()
        except Exception as e:
            print(f"[NexusAI bridge] load error: {e}")
            return None

    try:
        result = ai.auto_detect(text, metadata)
        if result.is_spam and result.confidence >= min_confidence:
            top    = result.reasons[0][:80] if result.reasons else "-"
            cid    = int(metadata.get("chat_id", 0)) if metadata and str(metadata.get("chat_id", "0")).lstrip("-").isdigit() else 0
            await _ai_log(
                aksi="🚨 AUTO DETECT SPAM",
                label=result.label,
                confidence=result.confidence,
                ringkasan=f"{top} | Layer: {result.layer}",
                chat_id=cid,
            )
            return result
        return None
    except Exception as e:
        print(f"[NexusAI bridge] auto_detect error: {e}")
        return None


async def nexus_ai_learn_spam(text: str) -> None:
    """Update model dari laporan /spam admin (online learning — spam)."""
    ai = get_nexus_ai()
    if not ai._loaded:
        try:
            await ai.load()
        except Exception:
            return
    try:
        ai.learn(text, is_spam=True)
        await _ai_log(aksi="📚 BELAJAR SPAM", label="SPAM", ringkasan=f"Teks: {text[:70]}")
        if ai._learn_count % 20 == 0:
            await ai.save()
    except Exception as e:
        print(f"[NexusAI bridge] learn_spam error: {e}")


async def nexus_ai_learn_ham(text: str) -> None:
    """
    Update model dari pesan yang dikonfirmasi HAM (bukan spam).
    Dipanggil oleh PassiveLearner — jangan panggil langsung dari handler.
    """
    ai = get_nexus_ai()
    if not ai._loaded:
        try:
            await ai.load()
        except Exception:
            return
    try:
        ai.learn(text, is_spam=False)
    except Exception as e:
        print(f"[NexusAI bridge] learn_ham error: {e}")


async def nexus_ai_passive_observe(
    text: str,
    executed_as_spam: bool,
    confidence: float,
    force_learn: bool = False,
) -> None:
    """
    Entry point untuk passive learning — dipanggil dari filter.

    Args:
        text:             teks pesan
        executed_as_spam: True jika pesan berhasil dihapus sebagai spam
        confidence:       skor confidence AI terakhir untuk pesan ini
        force_learn:      True untuk konfirmasi rule-based (regex, CategoryDetector,
                          bio filter) — melewati threshold confidence, tetap kena
                          rate limit anti-poisoning
    """
    try:
        from nexus.ai_core.passive_learner import get_passive_learner
        pl = get_passive_learner()

        if executed_as_spam:
            learned = await pl.observe_spam_executed(
                text,
                confidence,
                save_to_corpus=True,
                force_learn=force_learn,
            )
            if learned:
                src = "FORCE" if force_learn else f"{confidence:.2f}"
                await _ai_log(
                    aksi="🧠 AUTO-LEARN SPAM",
                    label="SPAM",
                    confidence=confidence,
                    ringkasan=f"PassiveLearner [{src}]: {text[:60]}",
                )
        else:
            pl.observe_ham_passed(text, confidence)

    except Exception as e:
        print(f"[NexusAI bridge] passive_observe error (non-fatal): {e}")


async def nexus_ai_category_detect(text: str) -> dict:
    """
    Deteksi kategori saja (tanpa Bayes/Feature) — untuk debugging/panel.
    Return dict dengan semua skor kategori.
    """
    try:
        from nexus.ai_core.category_detector import get_category_detector
        cd  = get_category_detector()
        res = cd.detect(text)
        return {
            "category":   res.category,
            "hit":        res.hit,
            "confidence": res.confidence,
            "reasons":    res.reasons,
            "all_scores": res.all_scores,
        }
    except Exception as e:
        return {"error": str(e)}


async def nexus_ai_midnight_run(
    kalimat_list: list[str],
) -> tuple[dict, list[tuple[str, str]]]:
    ai = get_nexus_ai()
    if not ai._loaded:
        try:
            await ai.load()
        except Exception as e:
            print(f"[NexusAI bridge] load error: {e}")
            return {}, []
    try:
        summary, patterns = await ai.midnight_regeneration(kalimat_list)
        await _ai_log(
            aksi="🌙 MIDNIGHT RUN",
            confidence=float(summary.get("threshold", 0.0)),
            ringkasan=(
                f"Korpus: {len(kalimat_list)} kalimat | "
                f"Pola baru: {len(patterns)} | "
                f"Threshold: {summary.get('threshold', '?'):.3f}"
            ),
        )
        return summary, patterns
    except Exception as e:
        print(f"[NexusAI bridge] midnight error: {e}")
        return {}, []


async def nexus_ai_get_stats() -> dict:
    ai = get_nexus_ai()
    if not ai._loaded:
        return {"error": "model belum di-load"}
    return ai.get_stats()


async def nexus_ai_passive_stats() -> dict:
    """Statistik passive learner — jumlah auto-learn, ham, rate limit, dll."""
    try:
        from nexus.ai_core.passive_learner import get_passive_learner
        return get_passive_learner().get_stats()
    except Exception as e:
        return {"error": str(e)}


async def nexus_ai_explain(text: str) -> str:
    ai = get_nexus_ai()
    if not ai._loaded:
        await ai.load()
    try:
        return ai.explain(text)
    except Exception as e:
        return f"[NexusAI] error explain: {e}"


def nexus_ai_get_recent_log(n: int = 30) -> list[str]:
    try:
        if not LOG_PATH.exists():
            return []
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:] if l.strip()]
    except Exception as e:
        return [f"[ERROR baca log] {e}"]


async def nexus_ai_get_full_stats() -> dict:
    ai = get_nexus_ai()
    if not ai._loaded:
        try:
            await ai.load()
        except Exception as e:
            return {"error": f"Model gagal dimuat: {e}"}
    try:
        stats    = ai.get_stats()
        thr_inf  = ai.adaptive.info()
        logs     = nexus_ai_get_recent_log(20)
        pl_stats = await nexus_ai_passive_stats()
        return {
            **stats,
            "threshold_detail": thr_inf,
            "passive_learner":  pl_stats,
            "recent_log":       logs,
        }
    except Exception as e:
        return {"error": f"Gagal ambil stats: {e}"}
