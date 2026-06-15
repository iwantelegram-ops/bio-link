"""
plugins/nexus/engine.py
────────────────────────
Nexus AI Core Engine — generator regex otomatis + scheduler.

Semua fungsi teks/mutasi ada di core.regex_utils. File ini hanya berisi:
  - generate_regex_otomatis_async  → kalkulasi ulang pola AI dari korpus
  - cron_midnight_scheduler        → loop scheduler tengah malam
"""

import asyncio
from datetime import datetime
from itertools import combinations
from collections import Counter
import pytz

from database import (
    nexus_get_all_kalimat,
    nexus_save_regex_bulk,
    nexus_mark_all_processed,
)
from core.regex_utils import pipeline_pembersihan, generate_kandidat_mutasi_liar

TZ_JAKARTA = pytz.timezone("Asia/Jakarta")


# ── Dual-Track Collective Pattern Engine ─────────────────────────────────────

async def generate_regex_otomatis_async():
    ts = datetime.now(TZ_JAKARTA).strftime("%H:%M:%S")
    print(f"\n[{ts}] ⏳ NEXUS: STARTING AI REGEX RE-CALCULATION...")

    semua_kalimat = await nexus_get_all_kalimat()

    if len(semua_kalimat) < 2:
        await nexus_save_regex_bulk([])
        print(f"[{ts}] ⚠️  NEXUS: Kurang dari 2 kalimat, regex dikosongkan.")
        return

    jalur_A: list = []
    jalur_B: list = []
    for kalimat in semua_kalimat:
    # 1. Jalankan pipeline_pembersihan terlebih dahulu untuk membuang karakter sampah/kurung angka
        kalimat_bersih = pipeline_pembersihan(kalimat)

    # 2. Split dari hasil kalimat yang sudah bersih total
        words = list(set(kalimat_bersih.split()))

    # 3. Pastikan token yang diambil hanya berisi karakter alfabet (menolak kurung atau angka counter)
        words = [w for w in words if w.isalpha()]

        kata_pendek = [w for w in words if len(w) in [1, 2]]
        kata_normal = [w for w in words if len(w) >= 3]

    # ... sisa kode di bawahnya tetap sama

        if kata_pendek and len(kata_normal) >= 2:
            for kp in kata_pendek:
                for i, j in combinations(kata_normal, 2):
                    s = sorted([i, j])
                    jalur_A.append((kp, s[0], s[1]))

        if len(kata_normal) >= 2:
            for i, j in combinations(kata_normal, 2):
                s = sorted([i, j])
                jalur_B.append(tuple(s))

        await asyncio.sleep(0.01)

    hitung_A  = Counter(jalur_A)
    hitung_B  = Counter(jalur_B)
    threshold = 2
    pola_list: list[tuple[str, str]] = []

    for (kp, kn1, kn2), count in hitung_A.items():
        if count >= threshold:
            lkp  = generate_kandidat_mutasi_liar(kp)
            lkn1 = generate_kandidat_mutasi_liar(kn1)
            lkn2 = generate_kandidat_mutasi_liar(kn2)
            pola = (f"(?=.*({'|'.join(lkp)}))"
                    f"(?=.*({'|'.join(lkn1)}))"
                    f"(?=.*({'|'.join(lkn2)}))")
            pola_list.append((pola, f"[A] {kp} + {kn1} + {kn2}"))
        await asyncio.sleep(0.005)

    for (kn1, kn2), count in hitung_B.items():
        if count >= threshold:
            lkn1 = generate_kandidat_mutasi_liar(kn1)
            lkn2 = generate_kandidat_mutasi_liar(kn2)
            pola = (f"(?=.*({'|'.join(lkn1)}))"
                    f"(?=.*({'|'.join(lkn2)}))")
            pola_list.append((pola, f"[B] {kn1} + {kn2}"))
        await asyncio.sleep(0.005)

    # ── [NEXUS AI CORE] Augmentasi TF-IDF (otak tambahan) ────────────────────
    try:
        from nexus.ai_core import nexus_ai_midnight_run
        _, ai_pola_extra = await nexus_ai_midnight_run(semua_kalimat)
        if ai_pola_extra:
            pola_list.extend(ai_pola_extra)
            print(f"[NexusAICore] +{len(ai_pola_extra)} pola TF-IDF dari AI Core.")
    except Exception as _ai_err:
        print(f"[NexusAICore] Augmentasi dilewati (non-fatal): {_ai_err}")
    # ── [/NEXUS AI CORE] ─────────────────────────────────────────────────────

    await nexus_save_regex_bulk(pola_list)
    await nexus_mark_all_processed()

    ts2 = datetime.now(TZ_JAKARTA).strftime("%H:%M:%S")
    print(f"[{ts2}] ✅ NEXUS: RE-CALCULATED — {len(pola_list)} pola tersimpan.\n")


# ── Background Scheduler ─────────────────────────────────────────────────────

async def cron_midnight_scheduler(client=None):
    """Jalankan sebagai asyncio.create_task() saat bot start."""
    try:
        while True:
            now = datetime.now(TZ_JAKARTA)
            if now.hour == 0 and now.minute == 0:
                await generate_regex_otomatis_async()
                await asyncio.sleep(60)
            await asyncio.sleep(30)

    except asyncio.CancelledError:
        ts = datetime.now(TZ_JAKARTA).strftime("%H:%M:%S")
        print(f"[{ts}] 💤 [Nexus] Cron midnight scheduler berhasil dihentikan dengan aman.")
        raise

    except Exception as e:
        print(f"[Nexus] Error tidak terduga di scheduler: {e}")
