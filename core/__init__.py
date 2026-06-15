"""
core/__init__.py
─────────────────
Public API modul core.

Import dari sini untuk semua kebutuhan regex, normalisasi, dan mutasi.
Jangan import langsung dari submodul internal.
"""

from core.regex_utils import (
    # Konstanta
    LEET_MAP,

    # Normalisasi & matching
    normalize_input,
    simplify,
    match_with_leet,
    get_normalized_variants,
    remove_mentions_for_regex,

    # Parse sintaks user → regex
    parse_simple_regex,

    # Pipeline pembersihan spam
    normalisasi_font,
    convert_angka_ke_huruf,
    hapus_karakter_berulang_total,
    pipeline_pembersihan,

    # Generasi mutasi
    generate_kandidat_mutasi_liar,
    saring_dengan_ambang_batas_50,
    hitung_karakter_asli_dalam_pola,
    generate_all_mutations,

    # Interlock builder
    build_group_interlock,
    _build_group_interlock,  # alias kompatibilitas
)

__all__ = [
    "LEET_MAP",
    "normalize_input",
    "simplify",
    "match_with_leet",
    "get_normalized_variants",
    "remove_mentions_for_regex",
    "parse_simple_regex",
    "normalisasi_font",
    "convert_angka_ke_huruf",
    "hapus_karakter_berulang_total",
    "pipeline_pembersihan",
    "generate_kandidat_mutasi_liar",
    "saring_dengan_ambang_batas_50",
    "hitung_karakter_asli_dalam_pola",
    "generate_all_mutations",
    "build_group_interlock",
    "_build_group_interlock",
]
