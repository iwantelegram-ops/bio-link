"""
nexus/ai_core/scorer.py
────────────────────────
Feature Scoring Engine — 15+ sinyal heuristik untuk deteksi spam.

Setiap sinyal diberi bobot (weight). Skor akhir dihitung dengan rumus
probabilitas komplementer:  P(spam) = 1 - Π(1 - w_i)
Rumus ini mencegah satu sinyal lemah mendominasi hasil.
"""

from __future__ import annotations

from nexus.ai_core.constants import (
    SEED_SPAM_VOCAB,
    RE_URL, RE_PHONE_ID, RE_MONEY, RE_PERCENT,
    RE_EMOJI, RE_REPEATING, RE_CAPS_WORD,
    RE_HASHTAG, RE_MENTION_EXT, RE_LEET,
)
from nexus.ai_core.normalizer import normalize_text, tokenize


class FeatureScorer:
    """
    Menghitung skor spam berdasarkan 15+ sinyal fitur.

    Usage:
        scorer = FeatureScorer()
        score, reasons = scorer.score(text, metadata)
        # score: 0.0 (aman) – 1.0 (pasti spam)
        # reasons: list[str] alasan deteksi
    """

    # Bobot per sinyal (disesuaikan dari pola spam Telegram Indonesia)
    _WEIGHTS: dict[str, float] = {
        "url_single":          0.25,
        "url_multiple":        0.50,
        "phone_id":            0.35,
        "money_pattern":       0.25,
        "percent_pattern":     0.20,
        "gambling_keyword":    0.55,
        "investment_keyword":  0.45,
        "sell_account":        0.40,
        "promo_pattern":       0.30,
        "pinjol_keyword":      0.45,
        "gcast_pattern":       0.40,
        "shortlink_domain":    0.50,
        "emoji_dense":         0.15,
        "repeating_chars":     0.20,
        "caps_heavy":          0.15,
        "hashtag_spam":        0.25,
        "external_mention":    0.30,
        "leet_obfuscation":    0.35,
        "forward_external":    0.20,
        "bio_has_link":        0.35,
        "short_msg_with_link": 0.30,
        "multi_exclamation":   0.15,
    }

    def score(
        self,
        text: str,
        metadata: dict | None = None,
    ) -> tuple[float, list[str]]:
        """
        Hitung skor spam dan kumpulkan alasan deteksi.

        metadata (opsional):
            is_forwarded  (bool) — pesan di-forward dari luar
            has_bio_link  (bool) — user punya link di bio
            is_new_user   (bool) — user baru bergabung

        Returns:
            (score: float 0.0–1.0, reasons: list[str])
        """
        if not text or not text.strip():
            return 0.0, []

        meta    = metadata or {}
        reasons: list[str] = []
        weights: list[float] = []

        norm_aggressive = normalize_text(text, aggressive=True)
        norm_soft       = normalize_text(text, aggressive=False)
        tokens          = tokenize(text)

        def hit(key: str, reason: str, override: float | None = None) -> None:
            w = override if override is not None else self._WEIGHTS.get(key, 0.2)
            weights.append(w)
            reasons.append(reason)

        # ── Sinyal 1: URL / Link ──────────────────────────────────────────────
        urls = RE_URL.findall(text)
        url_list = [u[0] if isinstance(u, tuple) else u for u in urls]
        if len(url_list) == 1:
            hit("url_single", f"link terdeteksi: {url_list[0][:50]}")
        elif len(url_list) >= 2:
            hit("url_multiple", f"{len(url_list)} link dalam satu pesan")

        # ── Sinyal 2: Domain shortlink / spam terkenal ────────────────────────
        SPAM_DOMAINS = [
            "wa.me", "bit.ly", "s.id/", "t.me/", "linktr.ee",
            "tinyurl", "cutt.ly", "rebrand.ly", "short.gg",
            "gg.gg", "rb.gy",
        ]
        for dom in SPAM_DOMAINS:
            if dom in text.lower():
                hit("shortlink_domain", f"domain shortlink spam: {dom}")
                break

        # ── Sinyal 3: Nomor HP Indonesia ─────────────────────────────────────
        phones = RE_PHONE_ID.findall(text)
        if phones:
            hit("phone_id", f"nomor HP Indonesia: {phones[0][:15]}")

        # ── Sinyal 4: Nominal Uang ────────────────────────────────────────────
        if RE_MONEY.search(text):
            hit("money_pattern", "menyebut nominal uang rupiah")

        # ── Sinyal 5: Persentase Keuntungan ──────────────────────────────────
        if RE_PERCENT.search(text):
            hit("percent_pattern", "menyebut angka persentase")

        # ── Sinyal 6: Keyword Judi / Slot ─────────────────────────────────────
        gambling_hits = [
            t for t in SEED_SPAM_VOCAB["judi_slot"]
            if t in norm_aggressive
        ]
        if gambling_hits:
            hit("gambling_keyword",
                f"keyword judi/slot: {', '.join(gambling_hits[:3])}")

        # ── Sinyal 7: Keyword Investasi Bodong ───────────────────────────────
        invest_hits = [
            t for t in SEED_SPAM_VOCAB["investasi_bodong"]
            if t in norm_aggressive
        ]
        if invest_hits:
            hit("investment_keyword",
                f"keyword investasi: {', '.join(invest_hits[:2])}")

        # ── Sinyal 8: Jual Akun ───────────────────────────────────────────────
        sell_hits = [
            t for t in SEED_SPAM_VOCAB["jual_akun"]
            if t in norm_aggressive
        ]
        if sell_hits:
            hit("sell_account",
                f"keyword jual akun: {', '.join(sell_hits[:2])}")

        # ── Sinyal 9: Pinjol ─────────────────────────────────────────────────
        pinjol_hits = [
            t for t in SEED_SPAM_VOCAB["pinjol_judol"]
            if t in norm_aggressive
        ]
        if pinjol_hits:
            hit("pinjol_keyword",
                f"keyword pinjol: {', '.join(pinjol_hits[:2])}")

        # ── Sinyal 10: GCast / Broadcast Massal ──────────────────────────────
        gcast_hits = [
            t for t in SEED_SPAM_VOCAB["gcast_spam"]
            if t in norm_aggressive
        ]
        if gcast_hits:
            hit("gcast_pattern", "pola broadcast/forward massal")

        # ── Sinyal 11: Promo Umum (butuh >= 2 kata) ──────────────────────────
        promo_count = sum(
            1 for t in SEED_SPAM_VOCAB["promosi_viral"]
            if t in norm_aggressive
        )
        if promo_count >= 2:
            hit("promo_pattern", f"{promo_count} kata promo terdeteksi")

        # ── Sinyal 12: Emoji Padat ────────────────────────────────────────────
        emojis = RE_EMOJI.findall(text)
        if len(emojis) >= 5:
            hit("emoji_dense", f"{len(emojis)} emoji dalam satu pesan")

        # ── Sinyal 13: Karakter Berulang (obfuskasi) ──────────────────────────
        reps = RE_REPEATING.findall(text)
        if len(reps) >= 2:
            hit("repeating_chars", "karakter berulang — kemungkinan obfuskasi")

        # ── Sinyal 14: Huruf Kapital Berlebihan ───────────────────────────────
        cap_words   = RE_CAPS_WORD.findall(text)
        word_count  = max(1, len(tokens))
        caps_ratio  = len(cap_words) / word_count
        if caps_ratio >= 0.40 and len(cap_words) >= 3:
            hit("caps_heavy", f"{len(cap_words)} kata huruf kapital semua")

        # ── Sinyal 15: Hashtag Spam ───────────────────────────────────────────
        hashtags = RE_HASHTAG.findall(text)
        if len(hashtags) >= 3:
            hit("hashtag_spam", f"{len(hashtags)} hashtag sekaligus")

        # ── Sinyal 16: Mention Eksternal ──────────────────────────────────────
        mentions = RE_MENTION_EXT.findall(text)
        if len(mentions) >= 2:
            hit("external_mention",
                f"mention {len(mentions)} username: {', '.join(mentions[:2])}")

        # ── Sinyal 17: Leet-speak Obfuskasi ──────────────────────────────────
        leet_count  = len(RE_LEET.findall(text))
        total_alnum = sum(1 for c in text if c.isalnum())
        if total_alnum > 0 and (leet_count / total_alnum) >= 0.25:
            hit("leet_obfuscation",
                f"rasio leet-speak tinggi ({leet_count}/{total_alnum})")

        # ── Sinyal 18: Banyak Tanda Seru ─────────────────────────────────────
        excl = text.count("!")
        if excl >= 3:
            hit("multi_exclamation", f"{excl} tanda seru")

        # ── Sinyal 19: Pesan Pendek + Ada Link (pola promo instan) ───────────
        if len(tokens) <= 5 and url_list:
            hit("short_msg_with_link", "pesan sangat pendek + link — pola promo instan")

        # ── Sinyal 20: Metadata (dari Telegram message object) ───────────────
        if meta.get("is_forwarded"):
            hit("forward_external", "pesan di-forward dari sumber luar")

        if meta.get("has_bio_link"):
            hit("bio_has_link", "bio user mengandung link")

        # ── Agregasi Skor (probabilitas komplementer) ─────────────────────────
        # P(spam) = 1 - Π(1 - w_i)
        # Rumus ini: setiap sinyal lemah tetap berkontribusi, tidak ada dominasi
        if not weights:
            return 0.0, []

        p_not_spam = 1.0
        for w in weights:
            p_not_spam *= (1.0 - min(w, 0.99))

        final_score = round(1.0 - p_not_spam, 4)
        return min(final_score, 0.9999), reasons
