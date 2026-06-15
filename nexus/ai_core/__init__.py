"""
nexus/ai_core/__init__.py
──────────────────────────
Public API untuk nexus/ai_core/.

Import hanya dari sini untuk integrasi ke file lain.
Jangan import langsung dari submodul internal (bayes.py, scorer.py, dll).

v3.0 — Exports baru:
  nexus_ai_learn_ham         — belajar pesan bersih (ham)
  nexus_ai_passive_observe   — observe setiap pesan, auto-learn jika yakin
  nexus_ai_passive_stats     — statistik passive learner
  nexus_ai_category_detect   — deteksi kategori saja (group invite, porn, scam)
"""

from nexus.ai_core.bridge import (
    get_nexus_ai,
    init_nexus_ai,
    nexus_ai_auto_detect,
    nexus_ai_learn_spam,
    nexus_ai_learn_ham,
    nexus_ai_passive_observe,
    nexus_ai_passive_stats,
    nexus_ai_category_detect,
    nexus_ai_midnight_run,
    nexus_ai_get_stats,
    nexus_ai_explain,
    nexus_ai_get_recent_log,
    nexus_ai_get_full_stats,
)
from nexus.ai_core.core import SpamResult, NexusAICore

__all__ = [
    # Singleton & init
    "get_nexus_ai",
    "init_nexus_ai",
    # Integrasi utama
    "nexus_ai_auto_detect",
    "nexus_ai_learn_spam",
    "nexus_ai_learn_ham",
    "nexus_ai_passive_observe",
    "nexus_ai_passive_stats",
    "nexus_ai_category_detect",
    "nexus_ai_midnight_run",
    # Diagnostik
    "nexus_ai_get_stats",
    "nexus_ai_explain",
    "nexus_ai_get_recent_log",
    "nexus_ai_get_full_stats",
    # Tipe
    "SpamResult",
    "NexusAICore",
]
