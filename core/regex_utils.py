"""
core/regex_utils.py
───────────────────
Semua logika regex, normalisasi teks, dan pembuatan pola mutasi spam.

Modul ini adalah satu-satunya sumber kebenaran untuk:
  - Normalisasi & leet-speak                   → _normalize_leet, _normalize_strip, simplify
  - Matching regex dengan dual normalisasi      → match_with_leet
  - Parse sintaks user ke regex murni           → parse_simple_regex
  - Pipeline pembersihan teks spam              → pipeline_pembersihan
  - Generasi mutasi liar kata                   → generate_kandidat_mutasi_liar
  - Penyaringan 50% identity                    → saring_dengan_ambang_batas_50
  - Pembangunan interlock regex grup            → build_group_interlock
  - Mutasi panel/display (bridge nexus)         → generate_all_mutations
"""

import re
import unicodedata
import itertools
from pyrogram.enums import MessageEntityType

# ─── Tabel Leetspeak (sumber kebenaran tunggal) ───────────────────────────────
LEET_MAP: dict[str, str] = {
    "0": "o", "1": "i", "3": "e", "4": "a",
    "5": "s", "6": "g", "7": "t", "8": "b",
    "9": "g", "@": "a",
}

# Alias private untuk kompatibilitas mundur
_LEET = LEET_MAP

# Leet map khusus untuk pipeline_pembersihan (angka → huruf, tanpa simbol)
_LEET_ANGKA: dict[str, str] = {
    k: v for k, v in LEET_MAP.items() if k.isdigit()
}

# ═════════════════════════════════════════════════════════════════════════════
#  BAGIAN 1 — parse_simple_regex (sintaks &&, |, (*))
# ═════════════════════════════════════════════════════════════════════════════

def parse_simple_regex(raw: str) -> str:
    """
    Parse sintaks user ke regex murni.

    Sintaks yang didukung:
      kata1 | kata2      → OR: salah satu hadir  →  (kata1|kata2)
      kata1 && kata2     → AND: keduanya wajib   →  (?=.*kata1)(?=.*kata2)
      kata(*)            → wildcard suffix        →  kata\S*
      (*) kata           → wildcard prefix        →  \S*kata

    Prioritas: && lebih tinggi dari |. Campuran && dan | tidak didukung —
    gunakan salah satu operator per ekspresi.

    Raises ValueError jika input kosong atau tidak menghasilkan pola valid.
    """
    import unicodedata as _ucd

    raw = _ucd.normalize("NFKC", raw.strip()).lower()
    if not raw:
        raise ValueError("Input kosong.")

    def _word_to_regex(word: str) -> str:
        """Escape satu kata; (*) → \S* wildcard."""
        word = word.strip()
        if not word:
            return ""
        parts = word.split("(*)")
        return r"\S*".join(re.escape(p) for p in parts)

    if "&&" in raw:
        terms = [t.strip() for t in raw.split("&&") if t.strip()]
        result = []
        for term in terms:
            r = _word_to_regex(term)
            if r:
                result.append(f"(?=.*{r})")
        if not result:
            raise ValueError("Tidak ada term valid setelah split &&.")
        return "".join(result)
    else:
        terms = [t.strip() for t in raw.split("|") if t.strip()]
        alts  = [a for a in (_word_to_regex(t) for t in terms) if a]
        if not alts:
            raise ValueError("Tidak ada term valid setelah split |.")
        return f"({'|'.join(alts)})" if len(alts) > 1 else alts[0]


# ═════════════════════════════════════════════════════════════════════════════
#  BAGIAN 2 — Normalisasi & Matching (dual normalization)
# ═════════════════════════════════════════════════════════════════════════════

def _normalize_leet(text: str) -> str:
  """
  Versi A: replace SEMUA angka ke huruf leet, lalu hapus sisa digit, lalu dedup.
  Cocok untuk: "5An63e3e" → "sange"
  """
  text = unicodedata.normalize("NFKC", text).lower()
  for ch, rep in LEET_MAP.items():
      text = text.replace(ch, rep)
  text = re.sub(r"\d", "", text)
  text = re.sub(r"([a-z])\1+", r"\1", text)
  return text


def _normalize_strip(text: str) -> str:
  """
  Versi B: hapus SEMUA digit dulu (anggap noise), lalu leet simbol (@), lalu dedup.
  Cocok untuk: "saaaaaang5eeeee33eee" → "sange"
  """
  text = unicodedata.normalize("NFKC", text).lower()
  text = re.sub(r"\d", "", text)
  for ch, rep in LEET_MAP.items():
      if not ch.isdigit():
          text = text.replace(ch, rep)
  text = re.sub(r"([a-z])\1+", r"\1", text)
  return text


def get_normalized_variants(text: str) -> tuple[str, str]:
  """
  Kembalikan dua versi normalisasi untuk dual matching.
  Returns: (leet_version, strip_version)

  Spasi dan tanda baca TETAP ada di kedua versi (tidak seperti simplify()).
  """
  return _normalize_leet(text), _normalize_strip(text)


def match_with_leet(pattern: re.Pattern, text: str) -> bool:
  """
  Cocokkan compiled regex pattern ke teks dengan dual normalization.
  Return True jika salah satu versi match.
  """
  norm_leet, norm_strip = get_normalized_variants(text)
  return bool(pattern.search(norm_leet) or pattern.search(norm_strip))


def simplify(text: str) -> str:
  """
  Normalisasi agresif: NFKC → lower → leet → dedup → HAPUS non a-z (termasuk spasi).
  Dipakai untuk deteksi pesan duplikat lokal & global.
  JANGAN dipakai untuk regex matching (spasi dihapus = AND logic rusak).
  """
  if not text:
      return ""
  text = unicodedata.normalize("NFKC", text)
  text = text.lower()
  for ch, rep in LEET_MAP.items():
      text = text.replace(ch, rep)
  text = re.sub(r"(.)\1+", r"\1", text)
  return re.sub(r"[^a-z]", "", text)


def normalize_input(text: str) -> str:
  """NFKC + lowercase. Dipakai sebelum simpan pola ke DB."""
  return unicodedata.normalize("NFKC", text).lower()


def remove_mentions_for_regex(message) -> str:
  """Hapus @mention dari teks agar regex tidak salah tembak username."""
  content = message.text or message.caption or ""
  if not message.entities:
      return unicodedata.normalize("NFKC", content)

  clean = content
  for ent in sorted(message.entities, key=lambda x: x.offset, reverse=True):
      if ent.type in (MessageEntityType.MENTION, MessageEntityType.TEXT_MENTION):
          start = ent.offset
          end   = ent.offset + ent.length
          clean = clean[:start] + (" " * ent.length) + clean[end:]

  return unicodedata.normalize("NFKC", clean)


# ═════════════════════════════════════════════════════════════════════════════
#  BAGIAN 3 — Pipeline Pembersihan Teks Spam
# ═════════════════════════════════════════════════════════════════════════════

def normalisasi_font(teks: str) -> str:
  """NFKD normalize + hapus combining characters (font hias, dll)."""
  teks_normal = unicodedata.normalize("NFKD", teks)
  return "".join([c for c in teks_normal if not unicodedata.combining(c)])


def convert_angka_ke_huruf(teks: str) -> str:
  """Konversi angka leet-speak ke huruf (hanya digit, tanpa simbol)."""
  return "".join(_LEET_ANGKA.get(char, char) for char in teks)


def hapus_karakter_berulang_total(teks: str) -> str:
  """Hapus karakter non-spasi berulang: 'aaaaaa' → 'a'."""
  return re.sub(r"([^\s])\1+", r"\1", teks)


def pipeline_pembersihan(teks_mentah: str) -> str:
  """
  Pipeline lengkap pembersihan teks spam sebelum diproses AI/regex.
  Memastikan simbol noise chat seperti (×3) atau [x2] dibersihkan total.
  """
  if not teks_mentah:
      return ""
  t = teks_mentah.lower()
  t = normalisasi_font(t)
  t = convert_angka_ke_huruf(t)
  t = hapus_karakter_berulang_total(t)

  # Hapus simbol perkalian spam khusus Telegram seperti ×3, x3, (x3) sebelum masuk token
  t = re.sub(r"\(?[×xX]\d+\)?", "", t)

  # Hapus semua karakter non-alfabet dan non-angka yang tersisa
  t = re.sub(r"[^\w\s]", "", t)

  return " ".join(t.split())

# ═════════════════════════════════════════════════════════════════════════════
#  BAGIAN 4 — Generasi Mutasi Kata (50% Identity Smart-Gap Engine)
# ═════════════════════════════════════════════════════════════════════════════

def hitung_karakter_asli_dalam_pola(pola_regex: str) -> int:
  """
  Menghitung jumlah POSISI huruf asli yang wajib hadir dalam pola regex.
  Menghitung posisi, bukan huruf unik — dua huruf sama dianggap berbeda.

  Aturan penghitungan:
    - Setiap huruf literal (a-z)        → +1 posisi
    - Setiap karakter class [...]        → +1 posisi (mewakili 1 karakter asli)
    - \\S*, \\S+, \\b, \\w*, dll          → 0 (bukan huruf asli, diabaikan)
  """
  if not pola_regex:
      return 0

  count = 0
  i = 0
  while i < len(pola_regex):
      ch = pola_regex[i]
      if ch == "\\":
          # Sequence \\X atau \\X* → bukan huruf asli, lewati
          i += 2
          if i < len(pola_regex) and pola_regex[i] in "*+?":
              i += 1
      elif ch == "[":
          # Karakter class [s5S] = 1 posisi huruf asli
          count += 1
          while i < len(pola_regex) and pola_regex[i] != "]":
              i += 1
          i += 1  # lewati ']'
      elif ch.isalpha():
          # Huruf literal = 1 posisi huruf asli
          count += 1
          i += 1
      else:
          i += 1

  return count

def saring_dengan_ambang_batas_50(kata_asli: str, daftar_kandidat: list) -> list:
  """
  Saringan Logika Terbalik (Ide User):
  Pola diurutkan dari yang TERPANJANG ke TERPENDEK.
  Pola pendek akan bertanya apakah pola panjang bisa mewakilinya.
  Jika tidak mewakili, pola pendek di-SAVE. Jika mewakili, pola pendek DIHAPUS.
  """
  # Minimal ceil(n/2) posisi hidup = benar-benar ≥50%
  # Contoh: n=5 → ceil(5/2)=3 (60%), bukan 5//2=2 (40%)
  syarat_minimal = (len(kata_asli) + 1) // 2

  lolos_awal = [p for p in daftar_kandidat if hitung_karakter_asli_dalam_pola(p) >= syarat_minimal]
  if not lolos_awal:
      return [f"\\b{re.escape(kata_asli)}\\b"]

  # 1. URUTKAN DARI YANG TERPANJANG (Kebalikan dari versi lama)
  pola_terurut = sorted(list(set(lolos_awal)), key=len, reverse=True)
  pola_efisien = []

  for pola_kandidat in pola_terurut:
      # Jika database masih kosong, pola terpanjang pertama langsung otomatis save
      if not pola_efisien:
          pola_efisien.append(pola_kandidat)
          continue

      is_diwakili = False

      # Pola pendek (pola_kandidat) bertanya kepada semua pola panjang yang sudah di-save
      for pola_panjang in pola_efisien:
          try:
              test_panjang = pola_panjang.replace("\\b", "")
              test_pendek = pola_kandidat.replace("\\b", "")

              # Uji apakah pola panjang bisa mewakili struktur pola pendek
              if re.search(test_pendek, test_panjang) or test_pendek in test_panjang:
                  # Jika pola panjang ternyata bisa mewakili pola pendek, tandai!
                  is_diwakili = True
                  break
          except Exception:
              continue

      # JIKA TIDAK DIWAKILI oleh pola panjang, MAKA POLA PENDEK DI-SAVE!
      if not is_diwakili:
          pola_efisien.append(pola_kandidat)

  return sorted(list(set(pola_efisien)))

def generate_kandidat_mutasi_liar(kata: str) -> list:
  r"""
  Generator Mutasi Pola Otomatis:
  Menjamin MINIMAL 50% huruf asli dari kata WAJIB ADA di dalam regex.
  Pola di bawah 50% otomatis diblokir sejak awal.

  Fitur Kapital (Owner/Admin):
    Huruf KAPITAL dalam input asli menandai posisi yang WAJIB ADA di semua pola.
    Pola yang tidak memiliki huruf kapital di posisinya akan dieliminasi.
    Contoh:
      bantaL  → posisi 5 (l) wajib ada → pola tanpa l di akhir dihapus
      lonTOng → posisi 3 (t) dan 4 (o) wajib ada
      bUaYA   → posisi 1 (u), 3 (y), 4 (a) wajib ada
  """
  if not kata:
      return []
  import re

  # ─── Catat posisi kapital SEBELUM normalisasi ───
  # Hapus konfiks counter seperti (x3), ×3, atau X5 dari salinan asli dulu
  kata_asli = re.sub(r"\(?[×xX]\d+\)?", "", kata)
  kata_asli = re.sub(r"[^\w]", "", kata_asli).strip()

  # Posisi huruf kapital dalam string asli (setelah dibersihkan simbol)
  posisi_kapital: set[int] = {
      i for i, ch in enumerate(kata_asli) if ch.isupper()
  }

  # Normalisasi ke lowercase untuk proses mutasi
  kata = kata_asli.lower()

  if not kata:
      return []

  n = len(kata)
  kandidat = set()
  VOKAL = "aiueo"

  # Minimal ceil(n/2) huruf hidup = benar-benar ≥50%
  batas_minimal_hidup = (n + 1) // 2

  kandidat.add(f"\\b{re.escape(kata)}\\b")

  # Smart Anchor untuk huruf pertama
  huruf_depan = kata[0]
  MAP_DEPAN = {
      'k': '[kqxcKQXCIi1]', 'c': '[cxCX]', 's': '[s5S]',
      'g': '[g69G]', 'b': '[b8B]', 't': '[t7T]'
  }
  anchor_depan = MAP_DEPAN.get(huruf_depan, re.escape(huruf_depan))

  import itertools
  for sisa_mask in itertools.product([0, 1], repeat=n-1):
      mask = (1,) + sisa_mask  # Huruf pertama selalu hidup

      # ─── Constraint Kapital: posisi kapital WAJIB mask=1 ───
      # Jika ada posisi kapital yang mask-nya 0, skip kombinasi ini
      if any(mask[i] == 0 for i in posisi_kapital if i < n):
          continue

      # Kunci Ambang Batas 50%
      if sum(mask) < batas_minimal_hidup:
          continue

      pola_parts = []
      for i in range(n):
          char = kata[i]
          if i == 0:
              pola_parts.append(anchor_depan)
          elif mask[i] == 1:
              pola_parts.append(re.escape(char))
          else:
              if char in VOKAL:
                  pola_parts.append(f"[{re.escape(char)}\\S]*")
              else:
                  pola_parts.append("\\S*")

      raw_pattern = "".join(pola_parts)

      pola_final = raw_pattern
      if not pola_final.startswith("\\S*"):
          pola_final = "\\b" + pola_final
      if not pola_final.endswith("\\S*"):
          pola_final = pola_final + "\\b"

      kandidat.add(pola_final)

  # ─── Filter Kapital Post-Generate ───
  # STEP 4: buang pola yang huruf kapitalnya tidak hadir sebagai literal.
  # Harus dilakukan SEBELUM saring 50%.
  if posisi_kapital:
      huruf_kapital_wajib = [kata[i] for i in sorted(posisi_kapital) if i < n]

      def _ekstrak_literal(pola: str) -> list:
          """Kumpulkan huruf literal wajib dari pola (kiri ke kanan).
          Skip: \\b, \\S*, \\S+, karakter class yang mengandung \\S."""
          hasil = []
          p = pola.replace("\\b", "")
          i = 0
          while i < len(p):
              if p[i] == "\\" and i + 1 < len(p):
                  i += 2
                  if i < len(p) and p[i] in "*+?":
                      i += 1
              elif p[i] == "[":
                  j = i + 1; isi = ""
                  while j < len(p) and p[j] != "]":
                      isi += p[j]; j += 1
                  if "\\S" not in isi:
                      h = re.sub(r"[^a-z]", "", isi.lower())
                      if h: hasil.append(h[0])
                  i = j + 1
                  if i < len(p) and p[i] in "*+?": i += 1
              elif p[i].isalpha():
                  hasil.append(p[i].lower()); i += 1
              else:
                  i += 1
          return hasil

      def _lolos_kapital(pola: str) -> bool:
          lits = _ekstrak_literal(pola)
          idx = 0
          for h in huruf_kapital_wajib:
              found = False
              while idx < len(lits):
                  if lits[idx] == h: idx += 1; found = True; break
                  idx += 1
              if not found: return False
          return True

      kandidat = {p for p in kandidat if _lolos_kapital(p)}
      if not kandidat:
          kandidat = {f"\\b{re.escape(kata)}\\b"}

  # STEP 5: Saring ambang batas 50%
  return saring_dengan_ambang_batas_50(kata, list(kandidat))

def generate_all_mutations(pola: str) -> tuple[list, str]:
  """
  Jembatan panel Nexus — hasilkan tampilan mutasi dan regex gabungan.

  Dipakai oleh handlers_fsm.py untuk visualisasi saat owner menambah
  Owner Regex. Menggunakan LEET_MAP extended (termasuk b, c, l, s, z, dll)
  karena tujuannya menampilkan semua kemungkinan karakter pengganti,
  bukan sekadar normalisasi.

  Return:
    (mutasi_display: list[tuple[char, pola]], raw_joined: str)
  """
  pola = pola.strip().lower().replace(" ", "")

  # Extended leet untuk keperluan display/visualisasi
  _LEET_DISPLAY: dict[str, str] = {
      "a": r"[aA4@]", "b": r"[bB8]", "c": r"[cC]", "e": r"[eE3]",
      "g": r"[gG69]", "i": r"[iI1!l|]", "l": r"[lL1|iI]", "o": r"[oO0]",
      "s": r"[sS5$]", "t": r"[tT7]", "z": r"[zZ2]",
  }

  mutasi_display = []
  regex_parts    = []

  for char in pola:
      if char in _LEET_DISPLAY:
          r_pola = _LEET_DISPLAY[char]
          regex_parts.append(r_pola + r"+")
          mutasi_display.append((char, r_pola))
      elif char.isalpha() or char.isdigit():
          r_pola = f"[{char.lower()}{char.upper()}]"
          regex_parts.append(r_pola + r"+")
          mutasi_display.append((char, r_pola))
      else:
          esc_char = re.escape(char)
          regex_parts.append(esc_char + r"*")
          mutasi_display.append((char, esc_char))

  raw_joined = r"".join(regex_parts)
  return mutasi_display, raw_joined


# ═════════════════════════════════════════════════════════════════════════════
#  BAGIAN 5 — Interlock Regex Builder (untuk grup & owner)
# ═════════════════════════════════════════════════════════════════════════════

def build_group_interlock(raw_input: str) -> tuple[str, list[str]]:
  """
  Parse 'kata | kata | kata' dan rakit interlock pola ala nexus owner.

  Setiap kata diproses pipeline_pembersihan lalu generate_kandidat_mutasi_liar
  menghasilkan lookahead (?=.*(alternasi_mutasi)).
  Pola akhir adalah gabungan semua lookahead — cocok hanya jika SEMUA kata
  hadir sekaligus (AND semantics).

  Return:
    (pola_regex: str, kata_bersih_list: list[str])

  Raises:
    ValueError jika input kosong atau tidak menghasilkan kata valid.
  """
  kata_list = [k.strip() for k in raw_input.split("|") if k.strip()]
  if not kata_list:
      raise ValueError("Input kosong — minimal satu kata.")

  lookaheads       = []
  kata_bersih_list = []

  for kata in kata_list:
      # Simpan versi asli (dengan kapital) untuk diteruskan ke generator mutasi
      kata_asli_bersih = re.sub(r"\(?[×xX]\d+\)?", "", kata)
      kata_asli_bersih = re.sub(r"[^\w]", "", kata_asli_bersih).strip()
      kata_asli_token  = kata_asli_bersih.split()[0] if kata_asli_bersih else ""
      kata_clean = pipeline_pembersihan(kata)
      if not kata_clean or not kata_asli_token:
          continue
      # Teruskan string asli (dengan info kapital) ke generator
      mutasi     = generate_kandidat_mutasi_liar(kata_asli_token)
      if mutasi:
          alts = "|".join(mutasi)
          lookaheads.append(f"(?=.*({alts}))")
          kata_bersih_list.append(kata_asli_token.lower())

  if not lookaheads:
      raise ValueError("Semua kata kosong atau tidak menghasilkan mutasi valid.")

  pola = "".join(lookaheads)
  return pola, kata_bersih_list


# Alias untuk kompatibilitas mundur (regex_group.py & handlers_fsm.py lama)
_build_group_interlock = build_group_interlock
