#!/usr/bin/env python3
"""WebUI untuk genspark-signup — jalankan dengan SATU perintah:

    py webui.py

Buka http://127.0.0.1:8765 di browser (dibuka otomatis). Setting diubah lewat
form, disimpan ke .env, lalu `signup.py` dijalankan sebagai proses anak di
belakang layar. Log ditayangkan langsung, hasil terbaca dari accounts.json.

Hanya pustaka standar — tanpa install apa pun. Mode manual (captcha/OTP diketik)
belum didukung: pakai CAPTCHA_PROVIDER otomatis + EMAIL_SOURCE=emailnator.

Port: argumen pertama, atau env WEBUI_PORT (default 8765).  --no-browser untuk
tidak membuka tab otomatis.
"""
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import proxies  # dipakai agar hitungan proxy di UI sama dengan hitungan signup.py

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")
ACCOUNTS = os.path.join(SCRIPT_DIR, "accounts.json")
AKUN_FILE = os.path.join(SCRIPT_DIR, "akun.txt")
PROXY_FILE = os.path.join(SCRIPT_DIR, "proxy.txt")
SIGNUP = os.path.join(SCRIPT_DIR, "signup.py")

# Nilai awal tampilan kalau kunci tak ada di .env. "" berarti "pakai default
# signup.py" dan barisnya tidak ditulis ke .env.
DEFAULTS = {
    "CAPTCHA_PROVIDER": "manual",
    "CAPTCHA_KEY": "",
    "CAPTCHA_TRIES": "3",
    "EMAIL_SOURCE": "file",
    "EMAIL_COUNT": "1",
    "EMAIL_TRIES": "3",
    "OTP_TIMEOUT": "300",
    "PROXY": "",
    "PROXY_TRIES": "4",
    "TIMEOUT": "",
    "MAIL_TIMEOUT": "30",
    "PASSWORD": "",
    "WORKERS": "6",
    "TAB_DELAY": "3",
    "FREE_CREDIT": "200",
    "DUMP_MIN_CREDIT": "2000",
    "DUMP_TARGET": "1",
    "DUMP_MAX_TRIES": "10",
    "DUMP_WORKERS": "3",
    "DUMP_CREDIT_WAIT": "25",
}

# Skema form: (grup, tab, keterangan, [(key, tipe, label, opsi, hint, level)]).
# tab   : "setting" (halaman Setting) | "dump" (tampil di kartu mode Dump)
# level : "req" = wajib diisi | "" = biasa | "adv" = disembunyikan di "Lanjutan"
GROUPS = [
    ("Akun", "setting", "Yang dipakai untuk semua akun yang dibuat.", [
        ("PASSWORD", "password", "Password akun", None,
         "Minimal 8 karakter dengan huruf besar, huruf kecil, angka, dan simbol.",
         "req"),
    ]),
    ("Sumber email", "setting",
     "Dari mana alamat email diambil, dan bagaimana OTP dibaca.", [
        ("EMAIL_SOURCE", "select", "Ambil email dari", ["file", "emailnator"],
         "file = daftar di akun.txt. emailnator = alamat dan OTP otomatis.", "req"),
        ("EMAIL_COUNT", "number", "Jumlah akun dibuat", None,
         "Hanya berlaku untuk emailnator.", ""),
        ("EMAIL_TRIES", "number", "Batas tukar alamat", None,
         "Berapa kali ganti alamat kalau email sudah terpakai.", "adv"),
        ("OTP_TIMEOUT", "number", "Tunggu OTP (detik)", None,
         "Berapa lama menunggu kode masuk sebelum menyerah.", "adv"),
    ]),
    ("Captcha", "setting",
     "Siapa yang menjawab captcha saat pendaftaran.", [
        ("CAPTCHA_PROVIDER", "select", "Dijawab oleh",
         ["manual", "2captcha", "capsolver", "anticaptcha", "capmonster",
          "rucaptcha", "azcaptcha"],
         "manual = gambar captcha muncul di panel proses, Anda yang mengetik.",
         "req"),
        ("CAPTCHA_KEY", "password", "API key solver", None,
         "Kosongkan kalau memilih manual.", ""),
        ("CAPTCHA_TRIES", "number", "Batas salah baca", None,
         "Berapa kali solver boleh keliru sebelum akun dilewati.", "adv"),
    ]),
    ("Proxy dan jaringan", "setting",
     "Kosongkan semuanya untuk memakai koneksi langsung.", [
        ("PROXY", "textarea", "Daftar proxy", None,
         "Satu per baris atau dipisah koma. Bisa juga ditaruh di proxy.txt.", ""),
        ("PROXY_TRIES", "number", "Batas ganti proxy", None,
         "Berapa proxy dicoba sebelum akun dilewati.", "adv"),
        ("TIMEOUT", "text", "Batas waktu request (detik)", None,
         "Kosongkan untuk otomatis: 5 lewat proxy, 30 langsung.", "adv"),
        ("MAIL_TIMEOUT", "number", "Batas waktu inbox (detik)", None,
         "Isi minimal 20. Emailnator sering lambat merespons.", "adv"),
    ]),
    ("Kecepatan", "setting",
     "Naikkan hati-hati. Terlalu cepat memicu penolakan dari sisi Genspark.", [
        ("WORKERS", "number", "Akun diproses serentak", None,
         "Dipaksa jadi 1 kalau captcha dijawab manual.", ""),
        ("TAB_DELAY", "number", "Jeda antar tab (detik)", None,
         "Jarak waktu sebelum tab checkout berikutnya dibuka.", "adv"),
        ("FREE_CREDIT", "number", "Batas credit gratis", None,
         "Credit di atas angka ini dianggap kuota langganan, bukan bonus.",
         "adv"),
    ]),
    ("Aturan dump", "dump", "", [
        ("DUMP_MIN_CREDIT", "number", "Simpan kalau credit minimal", None,
         "Akun di bawah angka ini tetap dilaporkan, tapi tidak disimpan.", ""),
        ("DUMP_TARGET", "number", "Berhenti setelah dapat", None,
         "Jumlah akun lolos yang dicari.", ""),
        ("DUMP_MAX_TRIES", "number", "Batas percobaan", None,
         "Pengaman supaya proses tidak berjalan tanpa akhir.", ""),
        ("DUMP_WORKERS", "number", "Diproses serentak", None,
         "Dipaksa jadi 1 kalau captcha dijawab manual.", "adv"),
        ("DUMP_CREDIT_WAIT", "number", "Tunggu credit (detik)", None,
         "Credit menyusul beberapa saat setelah signup.", "adv"),
    ]),
]

# --------------------------------------------------------------------------
# .env baca/tulis
# --------------------------------------------------------------------------

def read_env():
    """Parsing .env persis seperti signup.load_env, dibaur dengan DEFAULTS."""
    cfg = dict(DEFAULTS)
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def flatten(value):
    """Nilai banyak baris -> satu baris berkoma, duplikat dan baris kosong dibuang.

    proxies.parse() memecah baik koma maupun baris, jadi koma aman dipakai
    sebagai pemisah di .env yang formatnya satu kunci per baris.
    """
    text = str(value)
    if "\n" not in text and "\r" not in text:
        return text.strip()
    seen, out = set(), []
    for part in text.replace(",", "\n").splitlines():
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return ",".join(out)


def save_env(values):
    """Tulis nilai ke .env, pertahankan baris lain + urutan + komentar.

    Nilai kosong = kunci dihapus dari .env, supaya signup.py memakai
    defaultnya (cocok dengan keterangan "kosongkan untuk otomatis").

    Satu kunci = satu baris, jadi nilai dari <textarea> yang berisi banyak
    baris HARUS dirapatkan lebih dulu. Tanpa ini newline mentah ikut tertulis
    dan baris ke-2 dan seterusnya jadi baris tanpa "=" yang dibuang diam-diam
    oleh read_env maupun signup.load_env -- proxy hilang tanpa pesan error.
    """
    values = {k: flatten(v) for k, v in values.items()}
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()

    out, seen = [], set()
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in values:
                seen.add(k)
                if values[k] != "":
                    out.append(f"{k}={values[k]}")
                continue
        out.append(line)

    for k, v in values.items():
        if k not in seen and v != "":
            out.append(f"{k}={v}")

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


# --------------------------------------------------------------------------
# accounts.json + info file
# --------------------------------------------------------------------------

def read_accounts():
    if os.path.exists(ACCOUNTS):
        try:
            with open(ACCOUNTS, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def count_lines(path, skip_comments=True):
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if skip_comments and line.startswith("#"):
                continue
            n += 1
    return n


def count_proxies(env):
    """Berapa proxy yang benar-benar akan dipakai signup.py.

    Sengaja memakai proxies.load persis seperti signup.py, jadi angka di layar
    tak bisa berbeda dari kenyataan. Dulu di sini count_lines(proxy.txt) saja,
    sehingga proxy yang diisi lewat form -- yang tersimpan sebagai kunci PROXY
    di .env -- selalu terbaca 0 dan pengguna menyangka isinya tak masuk.
    Baris rusak membuat proxies.load melempar ValueError; -1 dipakai sebagai
    penanda "tak terbaca" supaya UI bisa memberi tahu, bukan diam saja.
    """
    try:
        return len(proxies.load(env.get("PROXY", ""), PROXY_FILE))
    except (ValueError, OSError):
        return -1


# --------------------------------------------------------------------------
# Proses anak + buffer log
# --------------------------------------------------------------------------

CONFIG_KEYS = set(DEFAULTS)


class Run:
    def __init__(self):
        self.proc = None
        self.mode = None
        self.lock = threading.Lock()
        self.cond = threading.Condition()
        self.seq = 0
        self.log = []          # list[str], index = seq-1
        self.pending = None    # dict {"kind","email",...} atau None
        self.pending_seq = 0


RUN = Run()

ASK_MARKER = "__WEBUI_ASK__ "


def _reader(proc, run):
    for raw in iter(proc.stdout.readline, ""):
        if raw == "":
            break
        line = raw.rstrip("\r\n")
        with run.cond:
            if line.startswith(ASK_MARKER):
                try:
                    run.pending = json.loads(line[len(ASK_MARKER):])
                except json.JSONDecodeError:
                    run.pending = None
                run.pending_seq += 1
            else:
                run.seq += 1
                run.log.append(line)
            run.cond.notify_all()
    proc.stdout.close()
    try:
        proc.stdin.close()
    except Exception:
        pass
    with run.cond:
        run.pending = None      # proses mati -> jangan tampilkan prompt basi
        run.pending_seq += 1
        run.cond.notify_all()   # bangunkan poller supaya tahu proses selesai


def start(mode, args=()):
    with RUN.lock:
        if RUN.proc is not None and RUN.proc.poll() is None:
            return False, "masih berjalan — hentikan dulu"
        # -u: print() disiram langsung (tanpanya stdout di-buffer saat di-pipe,
        # log baru muncul saat proses selesai)
        cmd = [sys.executable, "-u", SIGNUP]
        if mode != "signup":
            cmd.append(mode)        # "credit" | "dump"
        cmd += list(args)           # mis. daftar email untuk refresh sebagian
        env = os.environ.copy()
        # setting diatur lewat .env, bukan env var proses induk webui
        for k in CONFIG_KEYS:
            env.pop(k, None)
        env["GENSPARK_WEBUI"] = "1"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        RUN.proc = subprocess.Popen(
            cmd, cwd=SCRIPT_DIR, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, creationflags=flags)
        RUN.mode = mode
        RUN.seq = 0
        RUN.log = []
        RUN.pending = None
        RUN.pending_seq = 0
        threading.Thread(target=_reader, args=(RUN.proc, RUN),
                         daemon=True).start()
        return True, None


def stop():
    with RUN.lock:
        if RUN.proc is not None and RUN.proc.poll() is None:
            RUN.proc.terminate()
            try:
                RUN.proc.stdin.close()
            except Exception:
                pass
            return True
        return False


def is_running():
    return RUN.proc is not None and RUN.proc.poll() is None


def wait_log(after, timeout=25.0):
    """Long-poll: tunggu baris baru (seq > after). Return (lines, latest_seq)."""
    with RUN.cond:
        if RUN.seq <= after and is_running():
            RUN.cond.wait(timeout)
        latest = RUN.seq
        start = after + 1
        lines = RUN.log[start - 1:latest] if start <= latest else []
        return lines, latest


def wait_pending(after, timeout=25.0):
    """Long-poll: tunggu pending berubah (pending_seq > after)."""
    with RUN.cond:
        if RUN.pending_seq <= after and is_running():
            RUN.cond.wait(timeout)
        return RUN.pending, RUN.pending_seq


def answer(value):
    """Jawab prompt captcha/OTP yang sedang tertunda di subprocess."""
    value = value.replace("\r", "").replace("\n", "")
    with RUN.lock:
        proc = RUN.proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False, "tidak ada proses berjalan"
    with RUN.cond:
        if RUN.pending is None:
            return False, "tidak ada pertanyaan aktif"
        RUN.pending = None
        RUN.pending_seq += 1
        RUN.cond.notify_all()
    try:
        proc.stdin.write(value + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError) as ex:
        return False, f"gagal kirim: {ex}"
    return True, None


def refresh_credit(emails=()):
    """Segarkan credit lewat `signup.py credit [email ...]`.

    Email divalidasi terhadap accounts.json dulu: argumen ini masuk ke
    command line, jadi jangan pernah meneruskan apa pun yang tak dikenal.
    Daftar kosong -> segarkan semua akun.
    """
    acc = read_accounts()
    if not acc:
        return False, "accounts.json kosong"
    if emails:
        known = [e for e in emails if e in acc]
        if not known:
            return False, "akun tak ditemukan di accounts.json"
    else:
        known = []          # kosong = semua
    return start("credit", known)


# Batas key per permintaan. Tiap key = satu request ke Genspark, jadi tanpa
# batas satu tempelan salah bisa jadi ribuan request sekaligus.
MAX_KEYS = 200


def split_keys(teks):
    """Teks tempelan -> daftar key unik. Baris kosong dan '#' dilewati.

    Pemisahnya baris, koma, spasi, dan titik-koma sekaligus: orang menempel
    dari CSV, dari kolom spreadsheet, atau dari file .txt, dan ketiganya harus
    sama-sama jalan tanpa perlu dirapikan dulu.
    """
    keys, seen = [], []
    for baris in (teks or "").splitlines():
        baris = baris.strip()
        if not baris or baris.startswith("#"):
            continue
        for bagian in baris.replace(",", " ").replace(";", " ").split():
            bagian = bagian.strip().strip('"').strip("'")
            if bagian and not bagian.startswith("#") and bagian not in seen:
                seen.append(bagian)
                keys.append(bagian)
    return keys


def check_keys_bulk(teks):
    """Cek credit banyak API key sekaligus. Mengembalikan (daftar, error).

    signup diimpor di sini, bukan di atas: impor itu membaca .env dan proxy.txt
    saat modul dimuat, dan WebUI harus tetap bisa dibuka walau .env belum ada.

    Kunci TIDAK pernah masuk log, disk, atau argv -- karena itu fungsi ini
    memanggil signup langsung alih-alih `signup.py key <kunci>`.
    """
    keys = split_keys(teks)
    if not keys:
        return None, "Belum ada API key. Tempel key atau pilih file."
    if len(keys) > MAX_KEYS:
        return None, (f"{len(keys)} key sekaligus terlalu banyak; "
                      f"batasnya {MAX_KEYS}. Pecah jadi beberapa bagian.")
    try:
        import signup
    except Exception as ex:
        return None, f"signup.py tak bisa dimuat: {ex}"

    cid_ke_email = {v.get("cogen_id"): e for e, v in read_accounts().items()
                    if v.get("cogen_id")}

    def one(i, key):
        # cogen_id dibaca lokal dari isi key: key yang sudah dicabut pun masih
        # bisa ditelusuri milik siapa.
        milik = cid_ke_email.get(signup.key_cogen_id(key))
        try:
            info = signup.credit_by_key(key, proxy=signup.PROXY_POOL.next())
        except Exception as ex:
            # INVALID hanya untuk key yang benar-benar ditolak Genspark.
            # Gangguan jaringan -> GAGAL, supaya akun sehat tak divonis mati.
            st = "INVALID" if isinstance(ex, signup.KeyDitolak) else "GAGAL"
            # str(ex) dari credit_by_key sengaja tak memuat kunci
            return i, {"status": st, "email": milik,
                       "tersimpan": bool(milik), "plan": None, "credit": None,
                       "error": str(ex)[:160]}
        # float dari /me dipertahankan di sini; pembulatan ke bawah hanya
        # dipakai saat menulis accounts.json.
        credit = float(info.get("credit_balance") or 0)
        return i, {"status": "HIDUP" if credit > 0 else "HABIS",
                   "email": milik or info.get("email"),
                   "tersimpan": bool(milik),
                   "plan": info.get("plan"), "credit": credit, "error": None}

    hasil = {}
    workers = max(1, min(getattr(signup, "WORKERS", 6), len(keys)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, i, k) for i, k in enumerate(keys)]
        for fut in as_completed(futs):
            i, r = fut.result()
            hasil[i] = r
    # urutan tempelan dipertahankan supaya bisa dicocokkan dengan file sumber
    return [dict(hasil[i], idx=i + 1) for i in range(len(keys))], None


def delete_account(email):
    """Hapus satu akun dari accounts.json. Ditolak kalau proses berjalan --
    signup.py menulis balik file itu dari salinan di memorinya, jadi hapus
    yang tumpang tindih dengan run aktif akan hilang lagi (lost update)."""
    with RUN.lock:
        if is_running():
            return False, "hentikan proses dulu sebelum hapus akun"
        acc = read_accounts()
        if email not in acc:
            return False, "akun tak ditemukan"
        del acc[email]
        with open(ACCOUNTS, "w", encoding="utf-8") as f:
            json.dump(acc, f, indent=2, ensure_ascii=False)
        return True, None


def state():
    env = read_env()
    return {
        "settings": env,
        "accounts": read_accounts(),
        "running": is_running(),
        "mode": RUN.mode if is_running() else None,
        "akun_lines": count_lines(AKUN_FILE),
        "proxy_lines": count_proxies(env),
        "log_seq": RUN.seq,
        "pid": RUN.proc.pid if is_running() else None,
    }


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

EYE_SVG = (
    '<svg class="eo" viewBox="0 0 24 24" width="16" height="16" fill="none" '
    'stroke="currentColor" stroke-width="1.8"><path d="M1 12s4-7 11-7 11 7 11 7'
    '-4 7-11 7-11-7-11-7Z"/><circle cx="12" cy="12" r="3"/></svg>'
    '<svg class="ec" hidden viewBox="0 0 24 24" width="16" height="16" fill="none" '
    'stroke="currentColor" stroke-width="1.8"><path d="M3 3l18 18M10.6 10.6a3 3 0 0 0 '
    '4.24 4.24M9.9 5.1A11 11 0 0 1 23 12s-1.4 2.5-4 4.4M6.1 6.1C3.6 7.9 2 10 2 10'
    's4 7 11 7c1.1 0 2.1-.15 3-.4"/></svg>')


def field_html(key, ftype, label, options, hint, level=""):
    """Satu baris form: label, kontrol, keterangan. id selalu = key."""
    hid = f"h-{key}" if hint else ""
    desc = f' aria-describedby="{hid}"' if hid else ""
    if ftype == "select":
        opts = "".join(f'<option value="{o}">{o}</option>' for o in options)
        ctl = f'<select name="{key}" id="{key}"{desc}>{opts}</select>'
    elif ftype == "textarea":
        ctl = (f'<textarea name="{key}" id="{key}" rows="3" spellcheck="false" '
               f'autocomplete="off" placeholder="host:port:user:pass"{desc}>'
               '</textarea>')
    elif ftype == "password":
        ctl = (f'<div class="pwrap"><input type="password" name="{key}" '
               f'id="{key}" autocomplete="new-password"{desc}>'
               f'<button type="button" class="eye" tabindex="-1" '
               f'aria-label="Tampilkan atau sembunyikan isi" '
               f'onclick="toggleEye(\'{key}\')">{EYE_SVG}</button></div>')
    elif ftype == "number":
        ctl = f'<input type="number" name="{key}" id="{key}" inputmode="numeric"{desc}>'
    else:
        ctl = f'<input type="text" name="{key}" id="{key}" autocomplete="off"{desc}>'
    tag = ' <span class="req">wajib</span>' if level == "req" else ""
    hint_html = f'<p class="hint" id="{hid}">{hint}</p>' if hint else ""
    return (f'<label class="lab" for="{key}">{label}{tag}</label>'
            f'{ctl}{hint_html}')


PAGE = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>genspark-signup</title>
<style>
  /* Kertas dingin + tinta slate. Aksi utama teal; merah hanya untuk merusak
     (stop, hapus); amber hanya untuk "menunggu Anda". Satu warna satu arti. */
  :root{
    color-scheme:light;
    --paper:#F4F6F8; --card:#FFFFFF; --sunken:#EDF0F3;
    --ink:#16202B; --ink-2:#4A5A6A; --ink-3:#5F7182;
    --line:#DDE3E9; --line-2:#C6D0D9;
    --act:#0B6E5F; --act-hi:#0E8A76; --act-soft:#E4F1EE;
    --warn:#8A5A00; --warn-soft:#FDF3DF; --warn-line:#E8C87A;
    --bad:#B3261E; --bad-soft:#FCEDEC;
    --term:#131B24; --term-ink:#C9D6E2; --term-dim:#7F93A6;
    --sans:system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    --r:10px; --shadow:0 1px 2px rgba(22,32,43,.06),0 1px 1px rgba(22,32,43,.04);
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--paper);color:var(--ink);
       font:15px/1.5 var(--sans);
       -webkit-font-smoothing:antialiased}
  /* Semua data mesin -- email, key, angka, pid -- diset mono dan tabular. */
  code,.mono{font-family:var(--mono);font-size:.86em;
             font-variant-numeric:tabular-nums}
  :focus-visible{outline:2px solid var(--act);outline-offset:2px;
                 border-radius:4px}

  /* ---------- kepala ---------- */
  .top{background:var(--card);border-bottom:1px solid var(--line);
       padding:14px 24px;display:flex;align-items:center;gap:18px;
       flex-wrap:wrap}
  .brand{display:flex;flex-direction:column;gap:2px;margin-right:auto}
  .brand h1{margin:0;font-size:16px;font-weight:650;letter-spacing:-.01em}
  .brand p{margin:0;font-size:12.5px;color:var(--ink-3)}
  .meters{display:flex;gap:16px;align-items:center}
  .meter{display:flex;flex-direction:column;line-height:1.25}
  .meter b{font:600 15px/1.2 var(--mono);font-variant-numeric:tabular-nums}
  .meter span{font-size:11px;color:var(--ink-3);letter-spacing:.02em}

  /* status: titik + teks, bukan pil berwarna penuh */
  .stat{display:inline-flex;align-items:center;gap:8px;font-size:13px;
        font-weight:550;color:var(--ink-2);white-space:nowrap}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--line-2);
       flex:none}
  .stat.is-run .dot{background:var(--act);
       animation:pulse 1.6s ease-in-out infinite}
  .stat.is-run{color:var(--act)}
  .stat.is-ask .dot{background:var(--warn)}
  .stat.is-ask{color:var(--warn)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  @media(prefers-reduced-motion:reduce){.stat.is-run .dot{animation:none}}

  /* ---------- tab ---------- */
  .tabs{background:var(--card);border-bottom:1px solid var(--line);
        padding:0 24px;display:flex;gap:2px;overflow-x:auto}
  .tab{background:none;border:0;border-bottom:2px solid transparent;
       color:var(--ink-3);font:550 14px/1 var(--sans);padding:13px 14px;
       cursor:pointer;white-space:nowrap;display:flex;align-items:center;
       gap:7px;border-radius:0}
  .tab:hover{color:var(--ink)}
  .tab.on{color:var(--ink);border-bottom-color:var(--act);font-weight:650}
  .pip{width:7px;height:7px;border-radius:50%;background:var(--warn)}

  /* ---------- kerangka ---------- */
  .wrap{max-width:1240px;margin:0 auto;padding:22px 24px 56px}
  .view{display:none}
  .view.on{display:block}
  .split{display:grid;grid-template-columns:minmax(340px,5fr) 7fr;gap:18px;
         align-items:start}
  @media(max-width:1000px){.split{grid-template-columns:1fr}}
  .narrow{max-width:760px}

  .card{background:var(--card);border:1px solid var(--line);
        border-radius:var(--r);padding:20px;box-shadow:var(--shadow)}
  .card + .card{margin-top:18px}
  .card h2{margin:0;font-size:15px;font-weight:650;letter-spacing:-.01em}
  .card .blurb{margin:5px 0 0;font-size:13px;color:var(--ink-2);
               max-width:60ch}
  .card h2 + .blurb{margin-bottom:16px}

  /* ---------- pemilih mode ---------- */
  .modes{display:grid;gap:10px;margin:16px 0}
  .mode{display:grid;grid-template-columns:auto 1fr;gap:12px;
        border:1px solid var(--line-2);border-radius:var(--r);padding:14px;
        cursor:pointer;background:var(--card)}
  .mode:hover{border-color:var(--act);background:var(--act-soft)}
  .mode input{margin:3px 0 0;accent-color:var(--act);width:16px;height:16px}
  .mode strong{display:block;font-size:14px;font-weight:600}
  .mode p{margin:3px 0 0;font-size:12.5px;color:var(--ink-2);line-height:1.45}
  .mode.pick{border-color:var(--act);background:var(--act-soft);
             box-shadow:inset 0 0 0 1px var(--act)}
  .mode-extra{margin-top:14px;padding-top:16px;
              border-top:1px dashed var(--line-2)}
  .mode-extra .grid{margin-top:10px}

  /* ---------- kontrol ---------- */
  .btn{font:550 14px/1 var(--sans);padding:10px 16px;border-radius:8px;
       border:1px solid var(--line-2);background:var(--card);color:var(--ink);
       cursor:pointer;display:inline-flex;align-items:center;gap:7px}
  .btn:hover{border-color:var(--ink-3);background:var(--sunken)}
  .btn.go{background:var(--act);border-color:var(--act);color:#fff;
          font-weight:600}
  .btn.go:hover{background:var(--act-hi);border-color:var(--act-hi)}
  .btn.stop{color:var(--bad);border-color:var(--line-2)}
  .btn.stop:hover{background:var(--bad-soft);border-color:var(--bad)}
  .btn.tiny{padding:6px 11px;font-size:12.5px}
  .btn[hidden]{display:none}
  .btn[disabled]{opacity:.45;cursor:not-allowed}
  .btn[disabled]:hover{background:var(--card);border-color:var(--line-2)}
  .btn.go[disabled]:hover{background:var(--act)}

  .bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .hr{height:1px;background:var(--line);margin:16px 0}

  .lab{display:block;font-size:12.5px;font-weight:550;color:var(--ink-2);
       margin-bottom:5px}
  .req{font-size:10.5px;font-weight:600;color:var(--act);
       text-transform:uppercase;letter-spacing:.05em;margin-left:5px}
  .hint{margin:5px 0 0;font-size:12px;color:var(--ink-3);line-height:1.45}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px 18px}
  @media(max-width:620px){.grid{grid-template-columns:1fr}}
  .field.full{grid-column:1/-1}

  input[type=text],input[type=password],input[type=number],input[type=date],
  select,textarea{
       width:100%;background:var(--card);border:1px solid var(--line-2);
       color:var(--ink);border-radius:8px;padding:9px 11px;
       font:15px/1.4 var(--sans)}
  input[type=number],input[type=date]{font-variant-numeric:tabular-nums}
  textarea{font:13px/1.55 var(--mono);resize:vertical;min-height:76px}
  input:focus,select:focus,textarea:focus{outline:none;border-color:var(--act);
       box-shadow:0 0 0 3px var(--act-soft)}
  input::placeholder,textarea::placeholder{color:var(--ink-3);opacity:1}
  input[type=checkbox]{width:16px;height:16px;accent-color:var(--act);
       cursor:pointer;padding:0;vertical-align:middle}
  select{appearance:none;padding-right:34px;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 8' fill='none' stroke='%234A5A6A' stroke-width='1.6'%3E%3Cpath d='M1 1.5 6 6.5 11 1.5'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 12px center;
    background-size:11px}
  .pwrap{position:relative}
  .pwrap input{padding-right:40px}
  .eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);
       background:none;border:0;padding:5px;color:var(--ink-3);cursor:pointer;
       display:flex;border-radius:5px}
  .eye:hover{color:var(--ink);background:var(--sunken)}

  fieldset{border:0;margin:0;padding:0}
  fieldset + fieldset{margin-top:12px;padding-top:22px;
                      border-top:1px solid var(--line)}
  legend{padding:0;font-size:11px;font-weight:700;color:var(--act);
         text-transform:uppercase;letter-spacing:.08em}
  legend + .blurb{margin:6px 0 14px}
  fieldset .grid{margin-top:12px}
  fieldset .blurb + .grid{margin-top:0}

  .adv{margin-top:14px}
  .adv > summary{font-size:12.5px;font-weight:550;color:var(--ink-2);
       cursor:pointer;list-style:none;display:inline-flex;align-items:center;
       gap:6px;padding:5px 0}
  .adv > summary::-webkit-details-marker{display:none}
  .adv > summary::before{content:"";width:0;height:0;
       border:4px solid transparent;border-left-color:var(--ink-3);
       margin-right:1px}
  .adv[open] > summary::before{transform:rotate(90deg)}
  .adv > summary:hover{color:var(--ink)}
  .adv .grid{margin-top:12px;padding-left:14px;
             border-left:2px solid var(--line)}

  .say{font-size:13px;min-height:1.35em;color:var(--ink-2)}
  .say.ok{color:var(--act)} .say.err{color:var(--bad)}
  .say:empty{min-height:0}

  .tip{border:1px solid var(--line);background:var(--sunken);
       border-radius:8px;padding:11px 13px;font-size:12.5px;
       color:var(--ink-2);line-height:1.5}
  .tip b{color:var(--ink);font-weight:600}

  /* ---------- panel proses (satu-satunya blok gelap) ---------- */
  .runcard{background:var(--term);border:1px solid var(--term);
           border-radius:var(--r);padding:0;overflow:hidden;
           box-shadow:var(--shadow)}
  .runhead{display:flex;align-items:center;gap:10px;padding:12px 16px;
           border-bottom:1px solid #24313E;color:var(--term-dim);
           font-size:12.5px;flex-wrap:wrap}
  .runhead h2{color:#E8EEF4;font-size:13px;font-weight:600;margin:0}
  .runhead .btn{background:#1D2833;border-color:#2E3D4B;color:var(--term-ink);
                margin-left:auto}
  .runhead .btn:hover{background:#26333F;border-color:#42525F}
  .runhead .btn.stop{color:#FF9C93;border-color:#2E3D4B}
  .runhead .btn.stop:hover{background:#3A1F1D;border-color:#8A3A33}
  .runhead .btn[disabled]:hover{background:#1D2833;border-color:#2E3D4B}
  .log{height:min(60vh,540px);min-height:280px;overflow:auto;padding:14px 16px;
       font:12.5px/1.65 var(--mono);color:var(--term-ink);white-space:pre-wrap;
       word-break:break-word;scroll-behavior:smooth}
  .log .idle{color:var(--term-dim);font-style:italic}
  .log div{padding:1px 0}
  @media(prefers-reduced-motion:reduce){.log{scroll-behavior:auto}}
  .jump{display:block;width:100%;border:0;border-top:1px solid #24313E;
        background:#1D2833;color:var(--term-ink);font:550 12px/1 var(--sans);
        padding:9px;cursor:pointer;border-radius:0}
  .jump:hover{background:#26333F}
  /* kelas di atas menyetel display, jadi atribut hidden harus dimenangkan
     kembali secara eksplisit -- kalau tidak, tombolnya selalu tampak */
  .jump[hidden]{display:none}

  /* prompt captcha/OTP: satu-satunya blok amber di halaman */
  .ask{background:var(--warn-soft);border-bottom:1px solid var(--warn-line);
       padding:14px 16px}
  .ask .who{display:block;font-size:12.5px;font-weight:650;color:var(--warn);
            margin-bottom:9px}
  .ask .who span{font-family:var(--mono);font-weight:500}
  .ask img{display:block;max-width:230px;border-radius:6px;background:#fff;
           border:1px solid var(--warn-line);margin-bottom:10px}
  .ask .bar input{max-width:230px;font-family:var(--mono);
                  letter-spacing:.08em}

  /* ---------- hasil ---------- */
  .sift{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;
        background:var(--sunken);border:1px solid var(--line);
        border-radius:8px;padding:14px;margin-bottom:16px}
  .sift .f{display:flex;flex-direction:column}
  .sift .f[hidden]{display:none}
  .sift .lab{margin-bottom:4px;font-size:11.5px}
  .sift input,.sift select{min-width:132px;padding:7px 10px;font-size:13.5px}
  .sift select{padding-right:32px}
  .tally{margin-left:auto;align-self:center;font-size:13px;color:var(--ink-2)}
  .tally b{font-family:var(--mono);font-variant-numeric:tabular-nums;
           color:var(--ink);font-weight:600}

  .picked-bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;
       background:var(--act-soft);border:1px solid var(--act);
       border-radius:8px;padding:10px 14px;margin-bottom:14px}
  .picked-bar .n{font-size:13px;font-weight:600;color:var(--act);
                 margin-right:2px}
  .picked-bar[hidden]{display:none}

  .tbl-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  thead th{text-align:left;padding:10px 12px;font-size:11.5px;font-weight:650;
       color:var(--ink-2);text-transform:uppercase;letter-spacing:.04em;
       background:var(--sunken);border-bottom:1px solid var(--line);
       position:sticky;top:0;white-space:nowrap;z-index:1}
  tbody td{padding:11px 12px;border-bottom:1px solid var(--line);
       vertical-align:middle}
  tbody tr:last-child td{border-bottom:0}
  tbody tr:hover{background:var(--sunken)}
  tbody tr.picked{background:var(--act-soft)}
  .c-mail{font-family:var(--mono);font-size:12.5px;word-break:break-all;
          min-width:190px}
  .c-num{font-family:var(--mono);font-variant-numeric:tabular-nums;
         text-align:right;white-space:nowrap}
  th.c-num{text-align:right}
  .c-when{font-family:var(--mono);font-size:11.5px;color:var(--ink-2);
          white-space:nowrap}
  .c-when .sub{color:var(--ink-3);font-size:11px}
  .c-act{white-space:nowrap;text-align:right}
  .tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11.5px;
       font-weight:550;background:var(--sunken);color:var(--ink-2);
       border:1px solid var(--line-2);white-space:nowrap}
  .tag.good{background:var(--act-soft);color:var(--act);border-color:#A9D6CC}
  /* amber, bukan merah: merah dipakai khusus untuk aksi merusak (hapus/stop).
     Ini butuh perhatian, tapi bukan tombol berbahaya. */
  .tag.bad{background:#FDF3E0;color:var(--warn);border-color:#E8CFA0}

  /* Hasil cek API key. Dipisah dari .say supaya bisa memuat beberapa baris
     data tanpa terlihat seperti notifikasi sesaat. */
  .keyout{margin-top:12px;padding:11px 13px;border-radius:var(--r);
          border:1px solid var(--line);background:var(--sunken);font-size:14px}
  .keyout.ok{background:var(--act-soft);border-color:#A9D6CC}
  .keyout.err{background:var(--bad-soft);border-color:#F0C4C1;color:var(--bad)}
  .keyout b{font-family:var(--mono);font-variant-numeric:tabular-nums}
  .keyout .sub{display:block;margin-top:3px;font-size:12.5px;color:var(--ink-2)}
  /* "tak dikenal" di tabel hasil key -- .idle hanya berlaku di panel log */
  .nil{color:var(--ink-3);font-style:italic}

  .keycell{display:flex;align-items:center;gap:4px}
  .keytext{font-family:var(--mono);font-size:12px;max-width:210px;
       overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
       color:var(--ink-2)}
  .keytext.show{white-space:normal;word-break:break-all;max-width:320px;
                color:var(--ink)}
  .icon{background:none;border:1px solid transparent;padding:5px;
        border-radius:6px;color:var(--ink-3);cursor:pointer;
        display:inline-flex;line-height:1}
  .icon:hover{color:var(--act);background:var(--act-soft);
              border-color:#A9D6CC}
  .icon.risk:hover{color:var(--bad);background:var(--bad-soft);
                   border-color:#E8B4B0}
  .icon[disabled]{opacity:.35;cursor:not-allowed}
  .icon[disabled]:hover{color:var(--ink-3);background:none;
                        border-color:transparent}

  .void{text-align:center;padding:44px 20px;color:var(--ink-2)}
  .void h3{margin:0 0 6px;font-size:14px;font-weight:600;color:var(--ink)}
  .void p{margin:0 auto;font-size:13px;max-width:42ch;line-height:1.55}
  .void .btn{margin-top:16px}

  /* Layar sempit: tabel jadi kartu bertumpuk, tak ada scroll horizontal. */
  @media(max-width:760px){
    .tbl-wrap{border:0;overflow:visible}
    table,tbody,tr,td{display:block;width:100%}
    thead{display:none}
    tbody tr{border:1px solid var(--line);border-radius:8px;padding:6px 4px;
             margin-bottom:10px;background:var(--card)}
    tbody td{border:0;padding:7px 12px;display:grid;
             grid-template-columns:88px 1fr;gap:12px;align-items:center}
    tbody td::before{content:attr(data-h);font-size:11px;font-weight:650;
             color:var(--ink-3);text-transform:uppercase;letter-spacing:.04em}
    tbody td.c-num{text-align:left}
    tbody td.c-act{text-align:left}
    .keytext{max-width:none;white-space:normal;word-break:break-all}
    .tally{margin-left:0;width:100%}
  }
</style>
</head>
<body>
<header class="top">
  <div class="brand">
    <h1>genspark-signup</h1>
    <p>Pilih mode, tekan mulai, ikuti prosesnya di panel kanan.</p>
  </div>
  <div class="meters">
    <div class="meter"><b id="cnt-akun">0</b><span>email siap</span></div>
    <div class="meter"><b id="cnt-proxy">0</b><span>proxy</span></div>
    <div class="meter"><b id="cnt-acc">0</b><span>akun jadi</span></div>
  </div>
  <span id="stat" class="stat"><i class="dot"></i><span id="stat-text">Diam</span></span>
  <button class="btn tiny stop" id="btn-stop-top" onclick="stopRun()" hidden>
    Hentikan</button>
</header>

<nav class="tabs">
  <button class="tab on" data-tab="jalan" onclick="showTab('jalan')">
    Jalankan<i class="pip" id="pip" hidden></i></button>
  <button class="tab" data-tab="hasil" onclick="showTab('hasil')">Hasil</button>
  <button class="tab" data-tab="setting" onclick="showTab('setting')">Setting</button>
</nav>

<main class="wrap">

  <!-- ================= Jalankan ================= -->
  <div class="view on" id="view-jalan">
   <div class="split">
    <div>
      <section class="card">
        <h2>Jalankan</h2>
        <p class="blurb">Setting tersimpan otomatis setiap kali Anda menekan
          mulai.</p>

        <div class="modes" id="modes">
          <label class="mode pick" data-mode="signup">
            <input type="radio" name="mode" value="signup" checked>
            <div>
              <strong>Buat akun</strong>
              <p>Daftar, buka tab checkout Stripe untuk diisi kartu manual,
                lalu ambil API key. Akun yang sudah berbayar dilewati, tidak
                ditagih dua kali.</p>
            </div>
          </label>

          <label class="mode" data-mode="dump">
            <input type="radio" name="mode" value="dump">
            <div>
              <strong>Dump akun</strong>
              <p>Buat akun dari email sementara dan simpan yang creditnya lolos
                ambang. Tanpa checkout, tanpa kartu, tanpa tagihan. Perlu
                sumber email emailnator.</p>
            </div>
          </label>

          <label class="mode" data-mode="credit">
            <input type="radio" name="mode" value="credit">
            <div>
              <strong>Cek credit</strong>
              <p>Login ke setiap akun yang sudah ada lalu baca ulang sisa
                creditnya. Tidak membuat akun baru.</p>
            </div>
          </label>
        </div>

        <div class="mode-extra" id="dump-extra" hidden>
          <span class="lab">Aturan dump</span>
          __FORM_DUMP__
        </div>

        <div class="bar" style="margin-top:18px">
          <button class="btn go" id="btn-go" onclick="startRun()">Mulai</button>
          <button class="btn stop" id="btn-stop" onclick="stopRun()" disabled>
            Hentikan</button>
        </div>
        <p class="say" id="say-jalan"></p>
      </section>

      <section class="card">
        <div class="tip" id="mode-tip"></div>
      </section>
    </div>

    <section class="runcard">
      <div class="runhead">
        <h2>Proses</h2>
        <span id="run-meta"></span>
        <button class="btn tiny" onclick="clearLog()">Bersihkan</button>
      </div>
      <div id="ask" hidden></div>
      <div class="log" id="log"><span class="idle">Belum ada proses dijalankan.</span></div>
      <button class="jump" id="jump" hidden onclick="toBottom()">
        Lompat ke baris terbaru</button>
    </section>
   </div>
  </div>

  <!-- ================= Hasil ================= -->
  <div class="view" id="view-hasil">
    <section class="card">
      <h2>Cek credit dari API key</h2>
      <p class="blurb">Tempel API key <code>gsk-</code> &mdash; satu atau
        banyak sekaligus, satu per baris &mdash; atau pilih file berisi daftar
        key. Tak perlu password, tak perlu login, dan key-nya tidak disimpan ke
        mana pun.</p>
      <label class="lab" for="key-in">API key</label>
      <textarea id="key-in" spellcheck="false" autocomplete="off"
                placeholder="gsk-...&#10;gsk-...&#10;&#10;satu per baris; baris berawalan # dilewati"
                oninput="countKeys()" style="min-height:96px"></textarea>
      <div class="bar" style="margin-top:10px">
        <button class="btn tiny" id="btn-key" onclick="checkKey()">Cek credit</button>
        <button class="btn tiny" onclick="pickKeyFile()">Pilih file...</button>
        <button class="btn tiny" onclick="clearKey()">Bersihkan</button>
        <span class="tally" id="key-n"></span>
      </div>
      <!-- file dibaca di browser lalu diisikan ke textarea, jadi server tak
           perlu parser multipart sama sekali -->
      <input type="file" id="key-file" accept=".txt,.csv,.log,text/*" hidden
             onchange="readKeyFile(this)">
      <div id="key-out" class="keyout" hidden></div>
      <div id="key-table"></div>
    </section>

    <section class="card">
      <h2>Akun tersimpan</h2>
      <p class="blurb">Dibaca dari <code>accounts.json</code> dan diperbarui
        sendiri setiap beberapa detik.</p>

      <div class="sift">
        <div class="f">
          <label class="lab" for="f-cmin">Credit minimal</label>
          <input type="number" id="f-cmin" placeholder="mis. 2000"
                 oninput="applyFilter()">
        </div>
        <div class="f">
          <label class="lab" for="f-cmax">Credit maksimal</label>
          <input type="number" id="f-cmax" placeholder="tanpa batas"
                 oninput="applyFilter()">
        </div>
        <div class="f">
          <label class="lab" for="f-time">Dibuat</label>
          <select id="f-time" onchange="applyFilter()">
            <option value="">Kapan saja</option>
            <option value="1">24 jam terakhir</option>
            <option value="7">7 hari terakhir</option>
            <option value="30">30 hari terakhir</option>
            <option value="custom">Rentang sendiri</option>
          </select>
        </div>
        <div class="f" id="f-range" hidden>
          <span class="lab">Dari dan sampai</span>
          <div class="bar" style="gap:6px">
            <input type="date" id="f-from" aria-label="Tanggal mulai"
                   onchange="applyFilter()">
            <input type="date" id="f-to" aria-label="Tanggal akhir"
                   onchange="applyFilter()">
          </div>
        </div>
        <div class="f">
          <span class="lab">&nbsp;</span>
          <button class="btn tiny" onclick="resetFilter()">Reset filter</button>
        </div>
        <span class="tally" id="tally"></span>
      </div>

      <div class="picked-bar" id="picked-bar" hidden>
        <span class="n" id="picked-n"></span>
        <button class="btn tiny" onclick="copyKeys('sel')">Copy API key</button>
        <button class="btn tiny" onclick="copyKeys('sel','csv')"
                title="email, api_key, plan, credit">Copy CSV</button>
        <button class="btn tiny" id="btn-ref-sel"
                onclick="refreshCredit('sel')">Cek ulang credit</button>
        <button class="btn tiny stop" id="btn-del-sel"
                onclick="deleteSelected()">Hapus</button>
        <button class="btn tiny" style="margin-left:auto"
                onclick="clearSel()">Batal pilih</button>
      </div>

      <div class="bar" style="margin-bottom:14px">
        <button class="btn tiny" onclick="copyKeys('all')">Copy semua API key</button>
        <button class="btn tiny" onclick="copyKeys('all','csv')"
                title="email, api_key, plan, credit">Copy semua CSV</button>
        <button class="btn tiny" onclick="toggleAllKeys()" id="btn-reveal">
          Tampilkan API key</button>
        <button class="btn tiny" id="btn-ref-all" onclick="refreshCredit('all')"
                title="Login ke tiap akun lalu baca ulang creditnya">
          Cek ulang semua credit</button>
      </div>
      <p class="say" id="say-hasil"></p>
      <div id="accounts"><p class="void">Memuat…</p></div>
    </section>
  </div>

  <!-- ================= Setting ================= -->
  <div class="view narrow" id="view-setting">
    <section class="card">
      <h2>Setting</h2>
      <p class="blurb">Disimpan ke <code>.env</code>. Field yang dikosongkan
        akan memakai nilai bawaan.</p>
      <div id="cfg">__FORM__</div>
      <div class="hr"></div>
      <div class="bar">
        <button class="btn go" onclick="save(true)">Simpan setting</button>
        <p class="say" id="say-setting"></p>
      </div>
    </section>
  </div>

</main>

<script>
const $ = id => document.getElementById(id);
const esc = s => (s??'').toString().replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const jget  = async u => (await fetch(u)).json();
const jpost = async (u,b) => (await fetch(u,{method:'POST',body:b})).json();

const MODE_LABEL={signup:'Buat akun', dump:'Dump akun', credit:'Cek credit'};
const MODE_TIP={
  signup:'Tab checkout Stripe terbuka di browser Anda. Isi kartu di sana, '+
         'proses lanjut sendiri setelah pembayaran diterima.',
  dump:'Butuh sumber email <b>emailnator</b>. Ubah di tab Setting kalau '+
       'sekarang masih memakai daftar dari akun.txt.',
  credit:'Setiap akun di Hasil akan dilogin satu per satu, jadi ini butuh '+
         'waktu beberapa detik per akun.'};

let curTab='jalan', curMode='signup';
let lastSeq=0, lastPendingSeq=0, stickBottom=true;
let ACC={}, RUNNING=false, showKeys=false, lastAccSig='';
const SEL=new Set();

// Status ditulis dari dua poller dengan irama berbeda (state tiap 2.5s,
// pending tiap perubahan). Simpan keduanya lalu gambar di satu tempat, supaya
// yang cepat tidak menghapus pesan "menunggu jawaban" dari yang lambat.
let RUN_INFO={running:false}, ASKING=null;

/* ---------------- tab ---------------- */
function showTab(name){
  curTab=name;
  document.querySelectorAll('.view').forEach(v=>
    v.classList.toggle('on', v.id==='view-'+name));
  document.querySelectorAll('.tab').forEach(b=>
    b.classList.toggle('on', b.dataset.tab===name));
  paintStatus();          // pip hanya relevan saat tab Jalankan tak dilihat
  try{ localStorage.setItem('tab', name); }catch(e){}
}

function say(msg, kind){
  const el=$('say-'+curTab) || $('say-jalan');
  if(el){ el.textContent=msg||''; el.className='say '+(kind||''); }
}

/* ---------------- pemilih mode ---------------- */
function pickMode(m){
  curMode=m;
  document.querySelectorAll('.mode').forEach(el=>{
    const on=el.dataset.mode===m;
    el.classList.toggle('pick', on);
    const r=el.querySelector('input'); if(r) r.checked=on;
  });
  $('dump-extra').hidden = m!=='dump';
  $('mode-tip').innerHTML=MODE_TIP[m]||'';
  $('btn-go').textContent='Mulai '+MODE_LABEL[m].toLowerCase();
  try{ localStorage.setItem('mode', m); }catch(e){}
}
document.querySelectorAll('.mode').forEach(el=>
  el.addEventListener('change', ()=>pickMode(el.dataset.mode)));

function toggleEye(key){
  const inp=$(key); if(!inp) return;
  const btn=inp.parentElement.querySelector('.eye');
  const show=inp.type==='password';
  inp.type=show?'text':'password';
  btn.querySelector('.eo').hidden=show;
  btn.querySelector('.ec').hidden=!show;
}

/* ---------------- status ---------------- */
// Satu-satunya penulis ke indikator status. Menunggu jawaban menang atas
// "sedang berjalan": itu yang butuh tindakan pengguna.
function paintStatus(){
  const s=RUN_INFO;
  const st=$('stat'), tx=$('stat-text');
  if(ASKING){
    st.className='stat is-ask';
    tx.textContent='Menunggu jawaban Anda';
  }else{
    st.className='stat'+(s.running?' is-run':'');
    tx.textContent = s.running
      ? (MODE_LABEL[s.mode]||s.mode)+' sedang berjalan' : 'Diam';
  }
  // penanda di tab hanya perlu saat panel prosesnya tak sedang dilihat
  $('pip').hidden = !ASKING || curTab==='jalan';
  $('run-meta').textContent = s.running && s.pid ? 'pid '+s.pid : '';
  $('btn-go').disabled=s.running;
  $('btn-stop').disabled=!s.running;
  $('btn-stop-top').hidden=!s.running;
  $('btn-ref-all').disabled=s.running||!Object.keys(ACC).length;
}

function renderRun(s){
  RUNNING=!!s.running;
  RUN_INFO=s;
  paintStatus();
}

/* ---------------- log ---------------- */
function nearBottom(el){ return el.scrollHeight-el.scrollTop-el.clientHeight<40; }
function toBottom(){
  const el=$('log');
  el.scrollTop=el.scrollHeight; stickBottom=true; $('jump').hidden=true;
}
function clearLog(){
  $('log').innerHTML='<span class="idle">Log dibersihkan.</span>';
  $('jump').hidden=true; stickBottom=true;
}
$('log').addEventListener('scroll', ()=>{
  stickBottom=nearBottom($('log'));
  $('jump').hidden=stickBottom;
});

async function pollLog(){
  for(;;){
    try{
      const d=await jget('/api/log?after='+lastSeq);
      if(d.lines && d.lines.length){
        const box=$('log');
        const idle=box.querySelector('.idle'); if(idle) idle.remove();
        const frag=document.createDocumentFragment();
        for(const t of d.lines){
          const div=document.createElement('div');
          div.textContent=t; frag.appendChild(div);
        }
        box.appendChild(frag);
        lastSeq=d.seq;
        // jangan rebut scroll kalau pengguna sedang membaca ke atas
        if(stickBottom) box.scrollTop=box.scrollHeight;
        else $('jump').hidden=false;
      }
    }catch(e){}
    await new Promise(r=>setTimeout(r,400));
  }
}

/* ---------------- captcha / OTP ---------------- */
function renderPending(p){
  const box=$('ask');
  ASKING=p||null;
  paintStatus();
  if(!p){ box.hidden=true; box.innerHTML=''; return; }
  box.hidden=false;
  const captcha=p.kind==='captcha';
  box.className='ask';
  box.innerHTML=
    '<span class="who">'+(captcha?'Ketik captcha untuk ':'Masukkan kode OTP untuk ')+
      '<span>'+esc(p.email)+'</span></span>'+
    (captcha?'<img src="'+esc(p.image)+'" alt="Gambar captcha">':'')+
    '<div class="bar"><input type="text" id="ask-input" autocomplete="off" '+
      'aria-label="Jawaban" placeholder="'+(captcha?'Jawaban captcha':'Kode OTP')+'">'+
    '<button type="button" class="btn go" id="ask-send">Kirim</button></div>';
  $('ask-send').onclick=sendAnswer;
  $('ask-input').addEventListener('keydown', e=>{
    if(e.key==='Enter') sendAnswer();
  });
  $('ask-input').focus();
}

async function sendAnswer(){
  const inp=$('ask-input');
  if(!inp) return;
  inp.disabled=true;
  const j=await jpost('/api/answer', new URLSearchParams({value:inp.value}));
  if(!j.ok){
    say(j.error||'Jawaban gagal dikirim.','err');
    if($('ask-input')) $('ask-input').disabled=false;
  }
}

async function pollPending(){
  for(;;){
    try{
      const d=await jget('/api/pending?after='+lastPendingSeq);
      lastPendingSeq=d.seq;
      renderPending(d.pending);
    }catch(e){}
    await new Promise(r=>setTimeout(r,400));
  }
}

/* ---------------- state ---------------- */
let cfgFilled=false;

async function loadState(){
  try{
    const s=await jget('/api/state');
    // Isi form SEKALI saja, saat halaman pertama dibuka. Kalau diisi ulang
    // tiap poll (2.5s), angka yang baru diketik pengguna akan tertimpa balik
    // oleh nilai lama dari .env begitu field kehilangan fokus. Menjaga
    // activeElement saja tidak cukup: klik ke mana pun langsung melepas fokus.
    if(!cfgFilled){
      cfgFilled=true;
      const cfg=s.settings;
      document.querySelectorAll('#cfg [name], #dump-extra [name]').forEach(el=>{
        if(!(el.name in cfg)) return;
        const v=cfg[el.name]??'';
        // .env cuma bisa satu baris per kunci, jadi daftar disimpan berkoma.
        // Di textarea dikembalikan satu per baris supaya enak dibaca/diedit.
        el.value = el.tagName==='TEXTAREA' ? v.split(',').join('\n') : v;
      });
    }
    $('cnt-akun').textContent=s.akun_lines;
    // -1 = proxies.load menolak isinya; jangan tampilkan "-1 proxy"
    $('cnt-proxy').textContent = s.proxy_lines<0 ? '!' : s.proxy_lines;
    $('cnt-proxy').title = s.proxy_lines<0
      ? 'Ada baris proxy yang formatnya tak dikenali. Periksa di tab Setting.'
      : 'proxy dari .env + proxy.txt, duplikat dibuang';
    $('cnt-acc').textContent=Object.keys(s.accounts||{}).length;
    renderRun(s);
    renderAccounts(s.accounts, s.running);
  }catch(e){}
}

async function pollState(){
  for(;;){ await loadState(); await new Promise(r=>setTimeout(r,2500)); }
}

/* ---------------- jalan / berhenti ---------------- */
async function save(loud){
  const data=new URLSearchParams();
  // kirim SEMUA field dari kedua form: server menghapus kunci yang dikirim
  // kosong, jadi mengirim sebagian saja akan menghapus setting yang lain
  document.querySelectorAll('#cfg [name], #dump-extra [name]').forEach(el=>
    data.append(el.name, el.value));
  const j=await jpost('/api/save', data);
  if(loud || !j.ok)
    say(j.ok?'Setting tersimpan.':'Gagal menyimpan: '+(j.error||''),
        j.ok?'ok':'err');
  return j.ok;
}

// Aturan dump ada di tab Jalankan, tapi tombol Simpan cuma di tab Setting.
// Simpan sendiri begitu field selesai diubah, supaya nilainya tidak hilang
// hanya karena pengguna tak menyeberang tab untuk menekan Simpan.
document.addEventListener('change', (ev)=>{
  const el=ev.target;
  if(!el.name || !el.closest('#dump-extra')) return;
  save(false).then(ok=>{
    if(ok) say('Aturan dump disimpan.','ok');
  });
});

async function startRun(){
  const pw=$('PASSWORD');
  if(pw && !pw.value.trim()){
    // pindah tab dulu: say() menulis ke panel tab yang sedang aktif
    showTab('setting');
    say('Password akun belum diisi. Isi di sini, lalu tekan mulai lagi.','err');
    pw.focus();
    return;
  }
  if(!await save(false)) return;
  const j=await jpost('/api/start?mode='+curMode);
  if(!j.ok){ say(j.error||'Proses gagal dimulai.','err'); return; }
  $('log').innerHTML=''; lastSeq=0; stickBottom=true; $('jump').hidden=true;
  say(MODE_LABEL[curMode]+' dimulai.','ok');
}

async function stopRun(){
  await jpost('/api/stop');
  say('Permintaan berhenti dikirim.');
}

/* ---------------- tabel hasil ---------------- */
const ICON_COPY='<svg viewBox="0 0 24 24" width="15" height="15" fill="none" '+
  'stroke="currentColor" stroke-width="1.8"><rect x="9" y="9" width="12" height="12" rx="2"/>'+
  '<path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
const ICON_EYE='<svg viewBox="0 0 24 24" width="15" height="15" fill="none" '+
  'stroke="currentColor" stroke-width="1.8"><path d="M1 12s4-7 11-7 11 7 11 7'+
  '-4 7-11 7-11-7-11-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
const ICON_REF='<svg viewBox="0 0 24 24" width="15" height="15" fill="none" '+
  'stroke="currentColor" stroke-width="1.8"><path d="M21 12a9 9 0 1 1-3-6.7"/>'+
  '<path d="M21 3v6h-6"/></svg>';
const ICON_DEL='<svg viewBox="0 0 24 24" width="15" height="15" fill="none" '+
  'stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M10 11v6M14 11v6'+
  'M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13M9 7V4h6v3"/></svg>';

// accounts.json menyimpan created_at sebagai "YYYY-MM-DD HH:MM:SS" waktu lokal
function parseTs(s){
  if(!s) return null;
  const d=new Date(String(s).replace(' ','T'));
  return isNaN(d) ? null : d;
}
const numId = n => (Number(n)||0).toLocaleString('id-ID');

function filtered(){
  const cmin=$('f-cmin').value.trim(), cmax=$('f-cmax').value.trim();
  const mode=$('f-time').value;
  const lo=cmin===''?null:Number(cmin), hi=cmax===''?null:Number(cmax);
  let from=null, to=null;
  if(mode==='custom'){
    if($('f-from').value) from=new Date($('f-from').value+'T00:00:00');
    if($('f-to').value)   to  =new Date($('f-to').value+'T23:59:59');
  }else if(mode){
    from=new Date(Date.now()-Number(mode)*864e5);
  }
  return Object.entries(ACC).filter(([email,v])=>{
    const c=Number(v.credit);
    if(lo!==null && !(c>=lo)) return false;
    if(hi!==null && !(c<=hi)) return false;
    if(from||to){
      const t=parseTs(v.created_at);
      if(!t) return false;                       // tanpa tanggal -> tak lolos
      if(from && t<from) return false;
      if(to && t>to) return false;
    }
    return true;
  }).sort((a,b)=>(b[1].created_at||'').localeCompare(a[1].created_at||''));
}

function applyFilter(){
  $('f-range').hidden = $('f-time').value!=='custom';
  renderRows();
}

function resetFilter(){
  ['f-cmin','f-cmax','f-from','f-to'].forEach(k=>$(k).value='');
  $('f-time').value='';
  applyFilter();
}

function toggleAllKeys(){
  showKeys=!showKeys;
  $('btn-reveal').textContent=showKeys?'Sembunyikan API key':'Tampilkan API key';
  renderRows();
}

function clearSel(){ SEL.clear(); renderRows(); }

// Tiga keadaan, bukan dua: true = sudah dinyalakan, false = dicoba tapi gagal
// (perlu perhatian), undefined = akun dibuat sebelum fitur ini ada, jadi
// statusnya memang belum diketahui -- jangan ditampilkan seolah bermasalah.
// data_retention_disabled adalah nama lama dengan arti TERBALIK; akun lama
// tak dibaca lagi supaya tak pernah dilaporkan keliru, dan akan terisi
// sendiri saat cek credit berikutnya.
function retTag(v){
  const r=v.data_retention_on;
  if(r===true)  return ' <span class="tag good" title="AI data retention '+
    'sudah dinyalakan">retention on</span>';
  if(r===false) return ' <span class="tag bad" title="Gagal menyalakan AI data '+
    'retention. Tekan Cek ulang credit untuk mencoba lagi.">retention mati</span>';
  return '';
}

function renderRows(){
  const el=$('accounts'), rows=filtered();
  const total=Object.keys(ACC).length;

  if(!total){
    el.innerHTML='<div class="void"><h3>Belum ada akun tersimpan</h3>'+
      '<p>Akun yang berhasil dibuat akan muncul di sini, lengkap dengan API '+
      'key dan sisa creditnya.</p>'+
      '<button class="btn go" onclick="showTab(\'jalan\')">Buat akun '+
      'pertama</button></div>';
    $('tally').textContent=''; syncSelUI(); return;
  }

  const sum=rows.reduce((s,[,v])=>s+(Number(v.credit)||0),0);
  $('tally').innerHTML='Menampilkan <b>'+rows.length+'</b> dari <b>'+total+
    '</b> akun'+(rows.length?' · total credit <b>'+numId(sum)+'</b>':'');

  if(!rows.length){
    el.innerHTML='<div class="void"><h3>Tidak ada yang cocok</h3>'+
      '<p>Filter yang aktif menyaring habis semua akun. Longgarkan '+
      'batas creditnya atau ubah rentang tanggal.</p>'+
      '<button class="btn" onclick="resetFilter()">Reset filter</button></div>';
    syncSelUI(); return;
  }

  let h='<div class="tbl-wrap"><table><thead><tr>'+
    '<th style="width:34px"><input type="checkbox" id="ck-all" '+
      'aria-label="Pilih semua yang tampil"></th>'+
    '<th>Email</th><th>Plan</th><th class="c-num">Credit</th>'+
    '<th>API key</th><th>Bayar</th><th>Dibuat</th>'+
    '<th class="c-act">Aksi</th></tr></thead><tbody>';

  for(const [email,v] of rows){
    const full=v.api_key||'';
    const shown=showKeys?full:(full?full.slice(0,16)+'…':'');
    const on=SEL.has(email);
    const plan=v.plan||'';
    const pay=v.payment_status||'';
    h+='<tr'+(on?' class="picked"':'')+'>'+
      '<td data-h="Pilih"><input type="checkbox" class="ck-row" data-email="'+
        esc(email)+'"'+(on?' checked':'')+' aria-label="Pilih '+esc(email)+'"></td>'+
      '<td class="c-mail" data-h="Email">'+esc(email)+'</td>'+
      '<td data-h="Plan">'+(plan?'<span class="tag good">'+esc(plan)+
        '</span>':'<span class="tag">—</span>')+retTag(v)+'</td>'+
      '<td class="c-num" data-h="Credit">'+
        (v.credit==null||v.credit===''?'—':numId(v.credit))+'</td>'+
      '<td data-h="API key"><div class="keycell">'+
        '<span class="keytext'+(showKeys?' show':'')+'" data-full="'+esc(full)+
          '">'+(shown?esc(shown):'—')+'</span>'+
        (full?'<button type="button" class="icon btn-eye" '+
              'title="Tampilkan atau sembunyikan key" '+
              'aria-label="Tampilkan atau sembunyikan key">'+ICON_EYE+'</button>'+
              '<button type="button" class="icon btn-copy" data-key="'+esc(full)+
              '" title="Copy API key" aria-label="Copy API key">'+ICON_COPY+
              '</button>':'')+
      '</div></td>'+
      '<td data-h="Bayar">'+(pay?'<span class="tag">'+esc(pay)+
        '</span>':'<span class="tag">—</span>')+'</td>'+
      '<td class="c-when" data-h="Dibuat">'+esc(v.created_at||'—')+
        (v.checked_at?'<div class="sub">dicek '+esc(v.checked_at)+'</div>':'')+
      '</td>'+
      '<td class="c-act" data-h="Aksi">'+
        '<button type="button" class="icon btn-ref" data-email="'+esc(email)+
          '" title="Cek ulang credit akun ini" aria-label="Cek ulang credit"'+
          (RUNNING?' disabled':'')+'>'+ICON_REF+'</button>'+
        '<button type="button" class="icon risk btn-del" data-email="'+esc(email)+
          '" title="Hapus akun ini" aria-label="Hapus akun"'+
          (RUNNING?' disabled':'')+'>'+ICON_DEL+'</button>'+
      '</td></tr>';
  }
  el.innerHTML=h+'</tbody></table></div>';
  syncSelUI();
}

function syncSelUI(){
  const shown=filtered().map(([e])=>e);
  // buang pilihan yang tak lagi tampil karena filter berubah
  for(const e of [...SEL]) if(!shown.includes(e)) SEL.delete(e);
  const n=SEL.size;
  $('picked-bar').hidden=!n;
  $('picked-n').textContent=n+' akun dipilih';
  $('btn-del-sel').disabled=RUNNING;
  $('btn-ref-sel').disabled=RUNNING;
  const ra=$('btn-ref-all');
  if(ra) ra.disabled=RUNNING||!Object.keys(ACC).length;
  const all=$('ck-all');
  if(all){
    all.checked = shown.length>0 && n===shown.length;
    all.indeterminate = n>0 && n<shown.length;
  }
}

function renderAccounts(acc, running){
  ACC=acc||{}; RUNNING=!!running;
  // polling tiap 2.5s: kalau isinya sama, JANGAN bangun ulang tabel --
  // itu akan menutup kembali key yang baru dibuka/di-scroll pengguna
  const sig=JSON.stringify(acc)+'|'+running;
  if(sig===lastAccSig) return;
  lastAccSig=sig;
  renderRows();
}

/* ---------------- copy / hapus / refresh ---------------- */
async function copyText(text){
  try{
    await navigator.clipboard.writeText(text);
    return true;
  }catch(e){
    // clipboard API butuh origin aman/izin -- fallback textarea+execCommand
    try{
      const ta=document.createElement('textarea');
      ta.value=text; ta.style.position='fixed'; ta.style.opacity='0';
      document.body.appendChild(ta); ta.select();
      const ok=document.execCommand('copy');
      ta.remove();
      return ok;
    }catch(e2){ return false; }
  }
}

async function copyKeys(scope, fmt){
  let rows=filtered().filter(([,v])=>v.api_key);
  if(scope==='sel') rows=rows.filter(([e])=>SEL.has(e));
  if(!rows.length){
    say(scope==='sel'?'Belum ada baris yang dipilih.'
                     :'Tidak ada API key untuk dicopy.','err');
    return;
  }
  const text = fmt==='csv'
    ? 'email,api_key,plan,credit\n'+rows.map(([e,v])=>
        [e, v.api_key, v.plan||'', v.credit??''].join(',')).join('\n')
    : rows.map(([,v])=>v.api_key).join('\n');
  const ok=await copyText(text);
  say(ok ? rows.length+(fmt==='csv'?' baris CSV dicopy.':' API key dicopy.')
         : 'Browser menolak akses clipboard, copy gagal.', ok?'ok':'err');
}

/* ---------------- cek credit dari API key (bulk) ---------------- */
// Kunci tak pernah masuk URL, tak pernah ditampilkan balik, dan textarea
// dikosongkan begitu hasilnya keluar. KEY_ROWS hanya memuat hasil, bukan kunci.
let KEY_ROWS=[];

// Pemisahnya harus sama dengan split_keys() di Python, kalau tidak angka di
// penghitung berbeda dari jumlah yang benar-benar dicek.
function splitKeys(teks){
  const out=[], seen=new Set();
  for(const baris of (teks||'').split(/\r?\n/)){
    const b=baris.trim();
    if(!b||b.startsWith('#')) continue;
    for(let bag of b.replace(/[,;]/g,' ').split(/\s+/)){
      bag=bag.replace(/^["']|["']$/g,'').trim();
      if(bag&&!bag.startsWith('#')&&!seen.has(bag)){ seen.add(bag); out.push(bag); }
    }
  }
  return out;
}

function countKeys(){
  const n=splitKeys($('key-in').value).length;
  $('key-n').innerHTML = n ? '<b>'+n+'<\/b> key unik siap dicek' : '';
}

function pickKeyFile(){ $('key-file').click(); }

// Dibaca di browser: file tak pernah diunggah, jadi server tak perlu parser
// multipart dan kunci tak melewati disk server.
function readKeyFile(inp){
  const f=inp.files&&inp.files[0];
  if(!f) return;
  const fr=new FileReader();
  fr.onload=()=>{
    const ta=$('key-in');
    const lama=ta.value.trim();
    ta.value=(lama?lama+'\n':'')+String(fr.result||'').trim();
    inp.value='';                 // biar file yang sama bisa dipilih lagi
    countKeys();
    const n=splitKeys(ta.value).length;
    say(f.name+' dimuat, '+n+' key unik siap dicek.','ok');
  };
  fr.onerror=()=>{ inp.value=''; say('Gagal membaca '+f.name+'.','err'); };
  fr.readAsText(f);
}

async function checkKey(){
  const inp=$('key-in'), out=$('key-out'), btn=$('btn-key');
  const show=(cls,html)=>{ out.hidden=false; out.className='keyout '+cls;
                           out.innerHTML=html; };
  const keys=splitKeys(inp.value);
  if(!keys.length){
    show('err','Tempel API key dulu, atau pilih file berisi daftar key.');
    inp.focus(); return;
  }
  btn.disabled=true;
  const label=btn.textContent; btn.textContent='Mengecek...';
  show('', 'Mengecek '+keys.length+' key di Genspark...');
  $('key-table').innerHTML='';
  try{
    const j=await jpost('/api/check-key',
                        new URLSearchParams({key:inp.value}));
    if(!j.ok||!j.rows){ show('err', esc(j.error||'Cek gagal.')); return; }
    KEY_ROWS=j.rows;
    inp.value='';                 // jangan tinggalkan kunci di DOM
    countKeys();
    renderKeyRows();
  }finally{
    btn.disabled=false; btn.textContent=label;
  }
}

function renderKeyRows(){
  const out=$('key-out');
  const n={HIDUP:0,HABIS:0,INVALID:0,GAGAL:0};
  let total=0;
  for(const r of KEY_ROWS){
    n[r.status]=(n[r.status]||0)+1;
    if(r.status==='HIDUP') total+=Number(r.credit)||0;
  }
  const satu=KEY_ROWS.length===1;
  out.hidden=false;
  out.className='keyout '+(n.HIDUP+n.HABIS?'ok':'err');
  out.innerHTML='Total credit <b>'+numId(Math.round(total))+'<\/b>'+
    '<span class="sub">'+
      'HIDUP <b>'+n.HIDUP+'<\/b> &middot; HABIS <b>'+n.HABIS+
      '<\/b> &middot; INVALID <b>'+n.INVALID+'<\/b>'+
      (n.GAGAL?' &middot; GAGAL <b>'+n.GAGAL+'<\/b>':'')+
      ' dari '+KEY_ROWS.length+
      ' key'+(satu?'':' &mdash; urutannya sama dengan yang Anda tempel')+
    '<\/span>'+
    (n.GAGAL?'<span class="sub">'+n.GAGAL+' key tak terbaca karena gangguan '+
      'jaringan, bukan key mati &mdash; coba lagi untuk yang itu.<\/span>':'');

  // Satu key: cukup ringkasan di atas, tabel satu baris cuma bikin ramai.
  if(satu){
    const r=KEY_ROWS[0];
    $('key-table').innerHTML='';
    out.innerHTML += '<span class="sub">'+
      (r.error ? esc(r.error)
               : (r.plan?'plan <b>'+esc(r.plan)+'<\/b> &middot; ':'')+
                 (r.email ? (r.tersimpan?'akun tersimpan <b>':'akun <b>')+
                            esc(r.email)+'<\/b>'+
                            (r.tersimpan?'':' (tak ada di accounts.json)')
                          : 'tak cocok dengan akun mana pun di accounts.json'))+
      '<\/span>';
    return;
  }

  let h='<div class="bar" style="margin:12px 0 8px">'+
    '<button class="btn tiny" onclick="copyKeyRows()">Copy hasil CSV<\/button>'+
    '<\/div><div class="tbl-wrap"><table><thead><tr>'+
    '<th>#<\/th><th>Status<\/th><th>Akun<\/th><th>Plan<\/th>'+
    '<th class="c-num">Credit<\/th><\/tr><\/thead><tbody>';
  for(const r of KEY_ROWS){
    // GAGAL netral, bukan merah: key-nya belum tentu bermasalah
    const kelas = r.status==='HIDUP'?'good'
                : (r.status==='HABIS'||r.status==='INVALID')?'bad':'';
    h+='<tr>'+
      '<td data-h="#" class="c-num">'+r.idx+'<\/td>'+
      '<td data-h="Status"><span class="tag '+kelas+'">'+r.status+'<\/span><\/td>'+
      '<td data-h="Akun" class="c-mail">'+
        (r.email?esc(r.email):'<span class="nil">tak dikenal<\/span>')+
        (r.email&&!r.tersimpan
          ?'<div class="sub">tak ada di accounts.json<\/div>':'')+
        (r.error?'<div class="sub">'+esc(r.error)+'<\/div>':'')+
      '<\/td>'+
      '<td data-h="Plan">'+(r.plan?esc(r.plan):'&mdash;')+'<\/td>'+
      '<td data-h="Credit" class="c-num">'+
        (r.credit==null?'&mdash;':numId(Math.round(r.credit)))+'<\/td>'+
    '<\/tr>';
  }
  $('key-table').innerHTML=h+'<\/tbody><\/table><\/div>';
}

async function copyKeyRows(){
  if(!KEY_ROWS.length){ say('Belum ada hasil untuk dicopy.','err'); return; }
  // kunci sengaja TIDAK ikut: CSV ini sering ditempel ke tempat lain
  const csv='idx,status,email,plan,credit,error\n'+KEY_ROWS.map(r=>
    [r.idx, r.status, r.email||'', r.plan||'', r.credit??'',
     (r.error||'').replace(/[",\n]/g,' ')].join(',')).join('\n');
  const ok=await copyText(csv);
  say(ok?KEY_ROWS.length+' baris CSV dicopy.'
        :'Browser menolak akses clipboard, copy gagal.', ok?'ok':'err');
}

function clearKey(){
  $('key-in').value='';
  $('key-out').hidden=true;
  $('key-out').innerHTML='';
  $('key-table').innerHTML='';
  KEY_ROWS=[];
  countKeys();
  $('key-in').focus();
}

async function refreshCredit(scope, one){
  const data=new URLSearchParams();
  let n=0;
  if(one){ data.append('email', one); n=1; }
  else if(scope==='sel'){
    for(const e of SEL) data.append('email', e);
    n=SEL.size;
    if(!n){ say('Belum ada baris yang dipilih.','err'); return; }
  }else{
    n=Object.keys(ACC).length;      // tanpa email = semua
  }
  const j=await jpost('/api/refresh-credit', data);
  if(!j.ok){ say(j.error||'Cek ulang credit gagal dimulai.','err'); return; }
  // jalan sebagai `signup.py credit`; lognya masuk panel proses yang sama
  $('log').innerHTML=''; lastSeq=0; stickBottom=true; $('jump').hidden=true;
  say('Cek ulang '+n+' akun dimulai. Ikuti prosesnya di tab Jalankan.','ok');
}

async function deleteSelected(){
  const list=[...SEL];
  if(!list.length) return;
  if(!confirm('Hapus '+list.length+' akun dari accounts.json?')) return;
  let ok=0, fail=0;
  for(const email of list){
    const j=await jpost('/api/delete-account', new URLSearchParams({email}));
    if(j.ok){ ok++; SEL.delete(email); } else fail++;
  }
  say(ok+' akun dihapus'+(fail?', '+fail+' gagal':'')+'.', fail?'err':'ok');
  lastAccSig='';          // paksa bangun ulang tabel
  loadState();
}

/* ---------------- delegasi event tabel ---------------- */
document.addEventListener('change', (ev)=>{
  const row=ev.target.closest('.ck-row');
  if(row){
    const email=row.dataset.email;
    if(row.checked) SEL.add(email); else SEL.delete(email);
    row.closest('tr').classList.toggle('picked', row.checked);
    syncSelUI();
    return;
  }
  if(ev.target.id==='ck-all'){
    const on=ev.target.checked;
    SEL.clear();
    if(on) filtered().forEach(([e])=>SEL.add(e));
    document.querySelectorAll('.ck-row').forEach(c=>{
      c.checked=on;
      c.closest('tr').classList.toggle('picked', on);
    });
    syncSelUI();
  }
});

document.addEventListener('click', async (ev)=>{
  const cp=ev.target.closest('.btn-copy');
  if(cp){
    const ok=await copyText(cp.dataset.key);
    say(ok?'API key dicopy.':'Browser menolak akses clipboard, copy gagal.',
        ok?'ok':'err');
    return;
  }
  const ey=ev.target.closest('.btn-eye');
  if(ey){
    const span=ey.parentElement.querySelector('.keytext');
    const full=span.dataset.full;
    const open=span.classList.toggle('show');
    span.textContent=open?full:(full.slice(0,16)+'…');
    return;
  }
  const rf=ev.target.closest('.btn-ref');
  if(rf){ refreshCredit(null, rf.dataset.email); return; }
  const del=ev.target.closest('.btn-del');
  if(!del) return;
  const email=del.dataset.email;
  if(!confirm('Hapus akun '+email+' dari accounts.json?')) return;
  const j=await jpost('/api/delete-account', new URLSearchParams({email}));
  say(j.ok?'Akun dihapus.':'Gagal menghapus: '+(j.error||''), j.ok?'ok':'err');
  lastAccSig='';
  loadState();
});

/* ---------------- mulai ---------------- */
// tab & mode terakhir diingat, biar reload tak melempar balik ke awal
try{
  const t=localStorage.getItem('tab');
  if(t && $('view-'+t)) showTab(t);
  const m=localStorage.getItem('mode');
  pickMode(m && MODE_LABEL[m] ? m : 'signup');
}catch(e){ pickMode('signup'); }

loadState();
pollLog();
pollState();
pollPending();
</script>
</body>
</html>
"""


def render_page():
    """Bangun dua form: setting umum (tab Setting) dan aturan dump (kartu Dump).

    Field ber-level "adv" dikumpulkan ke <details> per grup, supaya layar awal
    hanya memperlihatkan yang benar-benar perlu disentuh.
    """
    forms = {"setting": "", "dump": ""}
    for group, tab, blurb, fields in GROUPS:
        main_items, adv_items = "", ""
        for key, ftype, label, opts, hint, level in fields:
            cls = "field" + (" full" if ftype == "textarea" else "")
            html = (f'<div class="{cls}">'
                    + field_html(key, ftype, label, opts, hint, level)
                    + '</div>')
            if level == "adv":
                adv_items += html
            else:
                main_items += html
        body = f'<div class="grid">{main_items}</div>' if main_items else ""
        if adv_items:
            body += ('<details class="adv"><summary>Pengaturan lanjutan</summary>'
                     f'<div class="grid">{adv_items}</div></details>')
        desc = f'<p class="blurb">{blurb}</p>' if blurb else ""
        if tab == "dump":
            forms[tab] += body
        else:
            forms[tab] += (f'<fieldset><legend>{group}</legend>{desc}{body}'
                           '</fieldset>')
    return (PAGE.replace("__FORM__", forms["setting"])
                .replace("__FORM_DUMP__", forms["dump"]))

# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            body = render_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/state":
            self._json(state())
        elif u.path == "/api/log":
            qs = urllib.parse.parse_qs(u.query)
            after = int(qs.get("after", ["0"])[0])
            lines, seq = wait_log(after)
            self._json({"lines": lines, "seq": seq})
        elif u.path == "/api/pending":
            qs = urllib.parse.parse_qs(u.query)
            after = int(qs.get("after", ["0"])[0])
            pending, seq = wait_pending(after)
            self._json({"pending": pending, "seq": seq})
        else:
            self.send_error(404)

    def _post_form(self):
        length = int(self.headers.get("Content-Length", 0))
        return urllib.parse.parse_qs(
            self.rfile.read(length).decode("utf-8"), keep_blank_values=True)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/save":
            qs = self._post_form()
            values = {k: (v[0] if v else "") for k, v in qs.items()
                      if k in CONFIG_KEYS}
            try:
                save_env(values)
                self._json({"ok": True})
            except Exception as ex:
                self._json({"ok": False, "error": str(ex)}, 500)
        elif u.path == "/api/start":
            mode = urllib.parse.parse_qs(u.query).get("mode", ["signup"])[0]
            if mode not in ("signup", "credit", "dump"):
                mode = "signup"
            ok, err = start(mode)
            self._json({"ok": ok, "error": err})
        elif u.path == "/api/stop":
            self._json({"ok": stop()})
        elif u.path == "/api/answer":
            qs = self._post_form()
            value = qs.get("value", [""])[0]
            ok, err = answer(value)
            self._json({"ok": ok, "error": err})
        elif u.path == "/api/delete-account":
            qs = self._post_form()
            email = qs.get("email", [""])[0]
            ok, err = delete_account(email)
            self._json({"ok": ok, "error": err})
        elif u.path == "/api/check-key":
            qs = self._post_form()
            rows, err = check_keys_bulk(qs.get("key", [""])[0])
            self._json({"ok": rows is not None, "error": err, "rows": rows})
        elif u.path == "/api/refresh-credit":
            qs = self._post_form()
            emails = [e for e in qs.get("email", []) if e]
            ok, err = refresh_credit(emails)
            self._json({"ok": ok, "error": err})
        else:
            self.send_error(404)


def pick_port():
    want = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    if not want:
        want = os.environ.get("WEBUI_PORT", "8765")
    base = int(want)
    for p in range(base, base + 12):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            return srv, p
        except OSError:
            continue
    raise RuntimeError(f"port {base}..{base + 11} semua terpakai")


def main():
    srv, port = pick_port()
    url = f"http://127.0.0.1:{port}/"
    no_browser = "--no-browser" in sys.argv
    print(f"WebUI genspark-signup: {url}")
    print("Tekan Ctrl+C untuk berhenti.")
    if not no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nberhenti.")
    finally:
        stop()
        srv.server_close()


if __name__ == "__main__":
    main()
