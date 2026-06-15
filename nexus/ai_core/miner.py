"""
nexus/ai_core/miner.py
───────────────────────
Corpus Pattern Miner — menambang pola regex cerdas dari korpus spam.

Teknik yang digunakan:
  - TF-IDF scoring untuk memilih kata paling informatif
  - Co-occurrence counting pasangan & triple kata kunci
  - Threshold adaptif berdasarkan ukuran korpus
  - Mutation pattern generation (prefix/suffix/wildcard)

Ini melengkapi engine.py yang sudah ada — bukan menggantikannya.
Hasil (regex, label) langsung kompatibel dengan nexus_save_regex_bulk().
"""

from __future__ import annotations

import re
import math
from collections import Counter

from nexus.ai_core.normalizer import tokenize


def _build_mutation_pattern(kata: str) -> str:
    r"""
    Buat pola regex dengan toleransi mutasi untuk menangani obfuskasi.

    Strategi:
      - Kata pendek (≤ 4 char): exact match saja
      - Kata menengah (5–7 char): exact | prefix+wildcard+suffix
      - Kata panjang (≥ 8 char): exact | prefix_half+wildcard+suffix_half

    PENTING — \b word boundary wajib:
      Semua pola dibungkus \b...\b sehingga hanya cocok sebagai token
      penuh, bukan sebagai substring di dalam kata lain.
      Wildcard \w* sudah aman di dalam \b...\b karena tidak melewati spasi.

    Semua case-insensitive (re.IGNORECASE ditangani saat compile).
    """
    if not kata:
        return ""
    exact = re.escape(kata)

    if len(kata) <= 4:
        return rf"\b{exact}\b"

    if len(kata) <= 7:
        prefix = re.escape(kata[:2])
        suffix = re.escape(kata[-2:])
        return rf"\b(?:{exact}|{prefix}\w*{suffix})\b"

    # Kata panjang: split di tengah
    mid    = len(kata) // 2
    prefix = re.escape(kata[:mid])
    suffix = re.escape(kata[mid:])
    return rf"\b(?:{exact}|{prefix}\w*{suffix})\b"


class CorpusPatternMiner:
    """
    Menambang pola regex dari korpus kalimat spam menggunakan TF-IDF.

    Usage:
        miner = CorpusPatternMiner()
        patterns = miner.mine(kalimat_list)
        # patterns: list[(regex_str, label_str)]
    """

    def __init__(
        self,
        tfidf_threshold: float = 0.08,
        min_token_len:   int   = 3,
    ):
        self.tfidf_threshold = tfidf_threshold
        self.min_token_len   = min_token_len

    # ── TF-IDF ────────────────────────────────────────────────────────────────

    def _compute_tfidf(
        self,
        corpus: list[list[str]],
    ) -> dict[str, float]:
        """
        Hitung skor TF-IDF rata-rata tiap term di seluruh korpus.

        Returns:
            dict {term: avg_tfidf_score}
        """
        n_docs = len(corpus)
        if n_docs == 0:
            return {}

        # Document frequency
        df: Counter[str] = Counter()
        for doc in corpus:
            df.update(set(doc))

        # TF-IDF per dokumen, lalu rata-rata
        tfidf: dict[str, float] = {}
        for doc in corpus:
            tf    = Counter(doc)
            total = max(1, len(doc))
            for term, count in tf.items():
                tf_val  = count / total
                idf_val = math.log((n_docs + 1) / (df[term] + 1)) + 1.0
                score   = tf_val * idf_val
                tfidf[term] = tfidf.get(term, 0.0) + score

        for term in tfidf:
            tfidf[term] /= n_docs

        return tfidf

    # ── Mining Utama ──────────────────────────────────────────────────────────

    def mine(self, kalimat_list: list[str]) -> list[tuple[str, str]]:
        """
        Terima daftar kalimat spam mentah.
        Kembalikan list (regex_pola, label) siap simpan ke nexus_regex.

        Pola yang dihasilkan kompatibel dengan format engine.py:
          - Format pasangan [B]: "(?=.*(pola1))(?=.*(pola2))"
          - Format triple  [AI-T]: "(?=.*(p1))(?=.*(p2))(?=.*(p3))"
          - Format solo dominan [AI-S]: "\b(pola)\b"
        """
        if len(kalimat_list) < 2:
            return []

        # Tokenisasi & bersihkan
        cleaned_corpus: list[list[str]] = []
        for kalimat in kalimat_list:
            tokens = [
                t for t in tokenize(kalimat)
                if len(t) >= self.min_token_len
            ]
            if tokens:
                cleaned_corpus.append(tokens)

        if len(cleaned_corpus) < 2:
            return []

        # Hitung TF-IDF
        tfidf = self._compute_tfidf(cleaned_corpus)

        # Ambil kata kunci signifikan
        key_terms = {
            t for t, s in tfidf.items()
            if s >= self.tfidf_threshold
        }

        if len(key_terms) < 2:
            return []

        # Threshold adaptif: makin banyak dokumen, makin ketat
        threshold = max(2, len(cleaned_corpus) // 6)

        # Hitung co-occurrence pasangan dan triple
        pair_count:   Counter = Counter()
        triple_count: Counter = Counter()

        for doc in cleaned_corpus:
            keys_in_doc = [t for t in doc if t in key_terms]
            n = len(keys_in_doc)

            # Pasangan (2 kata)
            for i in range(n):
                for j in range(i + 1, n):
                    pair = tuple(sorted([keys_in_doc[i], keys_in_doc[j]]))
                    pair_count[pair] += 1

            # Triple (3 kata) — hanya jika ada cukup kata kunci
            if n >= 3:
                for i in range(n):
                    for j in range(i + 1, n):
                        for k in range(j + 1, n):
                            triple = tuple(sorted([
                                keys_in_doc[i],
                                keys_in_doc[j],
                                keys_in_doc[k],
                            ]))
                            triple_count[triple] += 1

        pola_list: list[tuple[str, str]] = []

        # ── Pola Pasangan ─────────────────────────────────────────────────────
        for (w1, w2), cnt in pair_count.most_common():
            if cnt < threshold:
                continue
            m1   = _build_mutation_pattern(w1)
            m2   = _build_mutation_pattern(w2)
            pola = f"(?=.*({m1}))(?=.*({m2}))"
            try:
                re.compile(pola, re.IGNORECASE)
                pola_list.append((pola, f"[AI-B] {w1}+{w2} (×{cnt})"))
            except re.error:
                pass

        # ── Pola Triple ────────────────────────────────────────────────────────
        for (w1, w2, w3), cnt in triple_count.most_common():
            if cnt < threshold:
                continue
            m1   = _build_mutation_pattern(w1)
            m2   = _build_mutation_pattern(w2)
            m3   = _build_mutation_pattern(w3)
            pola = f"(?=.*({m1}))(?=.*({m2}))(?=.*({m3}))"
            try:
                re.compile(pola, re.IGNORECASE)
                pola_list.append((pola, f"[AI-T] {w1}+{w2}+{w3} (×{cnt})"))
            except re.error:
                pass

        # ── Pola Solo Dominan ─────────────────────────────────────────────────
        # Hanya untuk kata dengan TF-IDF sangat tinggi (3× threshold)
        solo_threshold = self.tfidf_threshold * 3
        for term, score in sorted(tfidf.items(), key=lambda x: -x[1]):
            if score < solo_threshold:
                break
            m    = _build_mutation_pattern(term)
            pola = rf"\b({m})\b"
            try:
                re.compile(pola, re.IGNORECASE)
                pola_list.append((pola, f"[AI-S] {term} ({score:.3f})"))
            except re.error:
                pass

        return pola_list
