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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

# Skema form: (grup, tab, [(key, tipe, label, opsi, hint), ...]).
# tab: "setting" (halaman Setting) | "dump" (tampil langsung di halaman Dump)
GROUPS = [
    ("Captcha", "setting", [
        ("CAPTCHA_PROVIDER", "select", "Provider",
         ["manual", "2captcha", "capsolver", "anticaptcha", "capmonster",
          "rucaptcha", "azcaptcha"],
         "manual = captcha ditampilkan di halaman ini untuk diketik"),
        ("CAPTCHA_KEY", "password", "API key", None,
         "kosongkan kalau manual"),
        ("CAPTCHA_TRIES", "number", "Coba ulang", None,
         "berapa kali solver boleh salah baca"),
    ]),
    ("Email", "setting", [
        ("EMAIL_SOURCE", "select", "Sumber email", ["file", "emailnator"],
         "emailnator = alamat + OTP otomatis"),
        ("EMAIL_COUNT", "number", "Jumlah akun", None, "kalau emailnator"),
        ("EMAIL_TRIES", "number", "Tukar alamat", None,
         "maks ganti alamat kalau conflict"),
        ("OTP_TIMEOUT", "number", "Timeout OTP (detik)", None,
         "berapa lama nunggu OTP masuk inbox"),
    ]),
    ("Proxy", "setting", [
        ("PROXY", "textarea", "Proxy", None,
         "koma / baris, atau di proxy.txt; kosong = koneksi langsung"),
        ("PROXY_TRIES", "number", "Coba proxy", None,
         "ganti proxy sebanyak ini kalau mati"),
        ("TIMEOUT", "text", "Timeout request (detik)", None,
         "kosongkan = otomatis (5 pakai proxy, 30 langsung)"),
        ("MAIL_TIMEOUT", "number", "Timeout inbox (detik)", None,
         "minimal 20, emailnator lambat"),
    ]),
    ("Akun & Jalankan", "setting", [
        ("PASSWORD", "password", "Password", None,
         "wajib; syarat Azure B2C: kapital+kecil+angka+simbol, min 8"),
        ("WORKERS", "number", "Worker paralel", None, ""),
        ("TAB_DELAY", "number", "Jeda antar tab (detik)", None, ""),
        ("FREE_CREDIT", "number", "Batas credit gratis", None,
         "credit di atas ini = kuota langganan masuk"),
    ]),
    ("Aturan dump", "dump", [
        ("DUMP_MIN_CREDIT", "number", "Ambang credit", None,
         "akun disimpan kalau creditnya >= ini"),
        ("DUMP_TARGET", "number", "Target akun bagus", None,
         "berhenti setelah dapat sebanyak ini"),
        ("DUMP_MAX_TRIES", "number", "Maks percobaan", None,
         "batas atas biar tak jalan selamanya"),
        ("DUMP_WORKERS", "number", "Paralel", None,
         "akun digarap serentak; dipaksa 1 kalau captcha manual"),
        ("DUMP_CREDIT_WAIT", "number", "Tunggu credit (detik)", None,
         "credit menyusul setelah signup; jangan baca terlalu cepat"),
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


def save_env(values):
    """Tulis nilai ke .env, pertahankan baris lain + urutan + komentar.

    Nilai kosong = kunci dihapus dari .env, supaya signup.py memakai
    defaultnya (cocok dengan keterangan "kosongkan untuk otomatis").
    """
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
    return {
        "settings": read_env(),
        "accounts": read_accounts(),
        "running": is_running(),
        "mode": RUN.mode if is_running() else None,
        "akun_lines": count_lines(AKUN_FILE),
        "proxy_lines": count_lines(PROXY_FILE),
        "log_seq": RUN.seq,
        "pid": RUN.proc.pid if is_running() else None,
    }


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

def field_html(key, ftype, label, options, hint):
    if ftype == "select":
        opts = "".join(f'<option value="{o}">{o}</option>' for o in options)
        ctl = f'<select name="{key}" id="{key}">{opts}</select>'
    elif ftype == "textarea":
        ctl = (f'<textarea name="{key}" id="{key}" rows="2" '
               'spellcheck="false" autocomplete="off"></textarea>')
    elif ftype == "password":
        ctl = (
            f'<div class="pwrap"><input type="password" name="{key}" '
            f'id="{key}" autocomplete="new-password">'
            f'<button type="button" class="eye" tabindex="-1" '
            f'aria-label="lihat/sembunyikan" onclick="toggleEye(\'{key}\')">'
            '<svg class="eo" viewBox="0 0 24 24" width="16" height="16" fill="none" '
            'stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7'
            '-4 7-11 7-11-7-11-7Z"/><circle cx="12" cy="12" r="3"/></svg>'
            '<svg class="ec" hidden viewBox="0 0 24 24" width="16" height="16" fill="none" '
            'stroke="currentColor" stroke-width="2"><path d="M3 3l18 18M10.6 10.6a3 3 0 0 0 '
            '4.24 4.24M9.9 5.1A11 11 0 0 1 23 12s-1.4 2.5-4 4.4M6.1 6.1C3.6 7.9 2 10 2 10'
            's4 7 11 7c1.1 0 2.1-.15 3-.4"/></svg></button></div>')
    elif ftype == "number":
        ctl = f'<input type="number" name="{key}" id="{key}">'
    else:
        ctl = (f'<input type="text" name="{key}" id="{key}" '
               'autocomplete="off">')
    hint_html = f'<div class="hint">{hint}</div>' if hint else ""
    return (f'<span class="lab">{label}</span>{ctl}{hint_html}')


PAGE = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>genspark-signup</title>
<style>
  /* palet: #DDDDDD #222831 #30475E #F05454 */
  :root{--bg:#222831;--panel:#272e39;--panel2:#30475E;--line:#3a5471;
        --text:#DDDDDD;--muted:#9aa8b8;--acc:#F05454;--acc-lite:#f47070;
        --ok:#7fd1a8;--warn:#e8b866;--bad:#F05454}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;
       background:var(--bg);color:var(--text)}
  header{padding:12px 22px;border-bottom:1px solid var(--line);
         display:flex;align-items:center;gap:14px;flex-wrap:wrap;
         background:var(--panel)}
  header h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.01em}
  header .sub{color:var(--muted);font-size:12px}
  .badge{padding:3px 10px;border-radius:20px;font-size:12px;
         border:1px solid var(--line);background:var(--panel2)}
  .badge.idle{color:var(--muted)}
  .badge.run{color:#1a1a1a;background:var(--ok);border-color:var(--ok);
             font-weight:600}

  /* ---- navigasi tab ---- */
  nav{display:flex;gap:4px;padding:0 22px;background:var(--panel);
      border-bottom:1px solid var(--line);flex-wrap:wrap}
  nav button{background:transparent;border:0;border-bottom:2px solid transparent;
             color:var(--muted);padding:11px 16px;border-radius:0;
             font:inherit;font-weight:500;cursor:pointer}
  nav button:hover{color:var(--text);background:var(--panel2)}
  nav button.on{color:var(--text);border-bottom-color:var(--acc);
                font-weight:600}

  main{padding:16px 22px}
  .page{display:none}
  .page.on{display:grid;grid-template-columns:minmax(320px,42fr) 58fr;
           gap:16px;align-items:start}
  @media(max-width:920px){.page.on{grid-template-columns:1fr}}
  .page.solo.on{grid-template-columns:1fr;max-width:900px}
  section{background:var(--panel);border:1px solid var(--line);
          border-radius:10px;padding:16px}
  section h2{font-size:14px;margin:0 0 4px;font-weight:600}
  section .desc{color:var(--muted);font-size:12.5px;margin-bottom:12px}
  fieldset{border:0;margin:0 0 14px;padding:0}
  legend{font-size:11.5px;color:var(--acc-lite);text-transform:uppercase;
         letter-spacing:.07em;margin-bottom:8px;font-weight:700}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}
  @media(max-width:520px){.grid{grid-template-columns:1fr}}
  .field{display:flex;flex-direction:column;gap:4px}
  .field.full{grid-column:1 / -1}
  .lab{font-size:12px;color:var(--muted)}
  input,select,textarea{background:var(--panel2);border:1px solid var(--line);
       color:var(--text);border-radius:7px;padding:8px 10px;font:inherit;
       width:100%}
  input:focus,select:focus,textarea:focus{outline:none;
       border-color:var(--acc-lite)}
  textarea{font-family:ui-monospace,Consolas,monospace;font-size:12px}
  .hint{font-size:11.5px;color:var(--muted)}
  .pwrap{position:relative}
  .pwrap input{padding-right:34px}
  .eye{position:absolute;right:4px;top:50%;transform:translateY(-50%);
       background:transparent;border:0;padding:4px;color:var(--muted);
       cursor:pointer;display:flex}
  .eye:hover{color:var(--text)}
  .ask-box{border:1px solid var(--warn);border-radius:9px;padding:12px;
           margin-bottom:12px;background:#2c3038}
  .ask-box .lab{color:var(--warn);font-weight:600;font-size:12.5px;
                margin-bottom:8px;display:block}
  .ask-box img{display:block;max-width:220px;border-radius:6px;margin-bottom:8px;
               background:#fff}
  .ask-box .row input{max-width:220px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  button{background:var(--panel2);color:var(--text);border:1px solid var(--line);
         padding:9px 16px;border-radius:7px;font:inherit;cursor:pointer}
  button:hover{border-color:var(--acc);background:#3a5471}
  button.primary{background:var(--acc);border-color:var(--acc);
         color:#fff;font-weight:600}
  button.primary:hover{background:var(--acc-lite);border-color:var(--acc-lite)}
  button.danger:hover{border-color:var(--bad);color:var(--bad)}
  button:disabled{opacity:.4;cursor:not-allowed}
  button.sm{padding:5px 10px;font-size:12px}
  .flash{font-size:12.5px;color:var(--muted);min-height:1.3em}
  .flash.ok{color:var(--ok)} .flash.err{color:var(--bad)}
  .logbox{background:#1b2027;border:1px solid var(--line);border-radius:8px;
       height:42vh;min-height:220px;overflow:auto;padding:10px 12px;
       font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;
       word-break:break-word}
  .logbox .dim{color:var(--muted)}
  table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);
        vertical-align:top;word-break:break-all}
  th{color:var(--muted);font-weight:600;position:sticky;top:0;
     background:var(--panel)}
  .empty{color:var(--muted);font-size:12.5px;padding:6px 0}
  .plan-badge{color:var(--ok)}
  .note{border-left:3px solid var(--acc);background:var(--panel2);
        padding:9px 12px;border-radius:0 7px 7px 0;font-size:12.5px;
        color:var(--muted);margin-bottom:12px}
  .note b{color:var(--text)}

  /* ---- tabel hasil: api key + filter ---- */
  .keycell{display:flex;align-items:center;gap:6px}
  .keytext{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;
           max-width:230px;overflow:hidden;text-overflow:ellipsis;
           white-space:nowrap}
  .keytext.show{white-space:normal;word-break:break-all;max-width:340px}
  .iconbtn{background:transparent;border:1px solid transparent;padding:3px 5px;
           border-radius:5px;color:var(--muted);cursor:pointer;
           display:inline-flex;align-items:center;line-height:1}
  .iconbtn:hover{color:var(--text);background:var(--panel2);
                 border-color:var(--line)}
  .filters{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;
           padding:12px;background:var(--panel2);border:1px solid var(--line);
           border-radius:8px;margin-bottom:12px}
  .filters .f{display:flex;flex-direction:column;gap:4px}
  .filters .f input,.filters .f select{min-width:120px}
  .filters .lab{font-size:11.5px}
  .tally{color:var(--muted);font-size:12.5px;margin-left:auto;
         align-self:center}
  tr.picked{background:rgba(240,84,84,.10)}
  input[type=checkbox]{width:15px;height:15px;accent-color:var(--acc);
                       cursor:pointer;padding:0}
</style>
</head>
<body>
<header>
  <div>
    <h1>genspark-signup</h1>
    <div class="sub">setting disimpan ke .env, proses jalan di belakang layar</div>
  </div>
  <div class="row" style="margin-left:auto">
    <span id="status" class="badge idle">diam</span>
    <span class="badge" title="akun di akun.txt">akun.txt: <b id="cnt-akun">0</b></span>
    <span class="badge" title="proxy dari .env + proxy.txt">proxy: <b id="cnt-proxy">0</b></span>
    <button id="btn-stop" class="danger sm" onclick="stopRun()" disabled>Stop</button>
  </div>
</header>

<nav>
  <button data-tab="buat" class="on" onclick="showTab('buat')">Buat akun</button>
  <button data-tab="dump" onclick="showTab('dump')">Dump akun</button>
  <button data-tab="setting" onclick="showTab('setting')">Setting</button>
  <button data-tab="hasil" onclick="showTab('hasil')">Hasil</button>
</nav>

<main>
  <!-- ============ Buat akun ============ -->
  <div class="page on" id="page-buat">
    <section>
      <h2>Buat akun</h2>
      <div class="desc">Signup + checkout Stripe + ambil API key.</div>
      <div class="note"><b>Tab checkout akan terbuka</b> dan kartu diisi manual
        di browser. Akun yang sudah selesai dilewati, yang sudah berbayar tak
        ditagih dua kali.</div>
      <div class="row">
        <button class="primary" onclick="startRun('signup')"
                data-run="1">Mulai buat akun</button>
        <button onclick="startRun('credit')" data-run="1">Cek credit</button>
      </div>
      <div class="flash" id="flash-buat"></div>
    </section>
    <section>
      <div id="ask" hidden></div>
      <h2>Log</h2>
      <div class="logbox" id="log"><span class="dim">belum ada proses dijalankan…</span></div>
    </section>
  </div>

  <!-- ============ Dump akun ============ -->
  <div class="page" id="page-dump">
    <section>
      <h2>Dump akun</h2>
      <div class="desc">Bikin akun dari tempmail, cek credit, simpan yang lolos ambang.</div>
      <div class="note"><b>Tanpa checkout Stripe</b> — tak ada kartu dan tak ada
        tagihan. Akun yang creditnya di bawah ambang tetap dilaporkan tapi tidak
        disimpan. Butuh <b>EMAIL_SOURCE=emailnator</b> (atur di tab Setting).</div>
      <div id="cfg-dump">__FORM_DUMP__</div>
      <div class="row">
        <button class="primary" onclick="startRun('dump')"
                data-run="1">Mulai dump</button>
      </div>
      <div class="flash" id="flash-dump"></div>
    </section>
    <section>
      <div id="ask-dump-slot"></div>
      <h2>Log dump</h2>
      <div class="logbox" id="log-dump"><span class="dim">belum ada proses dijalankan…</span></div>
    </section>
  </div>

  <!-- ============ Setting ============ -->
  <div class="page solo" id="page-setting">
    <section>
      <h2>Setting</h2>
      <div class="desc">Disimpan ke <code>.env</code>. Kosongkan field untuk memakai nilai default.</div>
      <div class="row" style="margin-bottom:10px">
        <button class="primary" onclick="save(true)">Simpan setting</button>
      </div>
      <div class="flash" id="flash-setting"></div>
      <div id="cfg">__FORM__</div>
    </section>
  </div>

  <!-- ============ Hasil ============ -->
  <div class="page solo" id="page-hasil">
    <section>
      <h2>Hasil</h2>
      <div class="desc">Isi <code>accounts.json</code> — diperbarui otomatis.</div>

      <div class="filters">
        <div class="f">
          <span class="lab">Credit minimal</span>
          <input type="number" id="f-cmin" placeholder="mis. 2000"
                 oninput="applyFilter()">
        </div>
        <div class="f">
          <span class="lab">Credit maksimal</span>
          <input type="number" id="f-cmax" placeholder="tanpa batas"
                 oninput="applyFilter()">
        </div>
        <div class="f">
          <span class="lab">Dibuat</span>
          <select id="f-time" onchange="applyFilter()">
            <option value="">kapan saja</option>
            <option value="1">24 jam terakhir</option>
            <option value="7">7 hari terakhir</option>
            <option value="30">30 hari terakhir</option>
            <option value="custom">rentang sendiri…</option>
          </select>
        </div>
        <div class="f" id="f-range" hidden>
          <span class="lab">Dari — sampai</span>
          <div class="row" style="gap:6px">
            <input type="date" id="f-from" onchange="applyFilter()">
            <input type="date" id="f-to" onchange="applyFilter()">
          </div>
        </div>
        <div class="f">
          <span class="lab">&nbsp;</span>
          <button class="sm" onclick="resetFilter()">Reset</button>
        </div>
        <span class="tally" id="tally"></span>
      </div>

      <div class="row" style="margin-bottom:4px">
        <button class="primary" onclick="copyKeys('sel')" id="btn-copy-sel"
                disabled>Copy terpilih</button>
        <button onclick="copyKeys('all')">Copy semua</button>
        <button onclick="copyKeys('sel','csv')" id="btn-csv-sel" disabled
                title="email,api_key,plan,credit">CSV terpilih</button>
        <button onclick="copyKeys('all','csv')"
                title="email,api_key,plan,credit">CSV semua</button>
        <button class="sm" onclick="toggleAllKeys()" id="btn-reveal">Lihat semua key</button>
        <button class="sm danger" onclick="deleteSelected()" id="btn-del-sel"
                disabled>Hapus terpilih</button>
      </div>
      <div class="row" style="margin-bottom:4px">
        <button class="sm" onclick="refreshCredit('sel')" id="btn-ref-sel"
                disabled title="login lalu baca ulang credit dari Genspark">
          Refresh credit terpilih</button>
        <button class="sm" onclick="refreshCredit('all')" id="btn-ref-all"
                title="login ke semua akun lalu baca ulang creditnya">
          Refresh semua credit</button>
        <span class="hint" id="ref-note"></span>
      </div>
      <div class="flash" id="flash-hasil"></div>
      <div id="accounts" class="empty">memuat…</div>
    </section>
  </div>
</main>

<script>
const $ = id => document.getElementById(id);
let lastSeq = 0;

function esc(s){return (s??'').replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

async function jget(url){const r=await fetch(url);return r.json();}
async function jpost(url, body){
  const r=await fetch(url,{method:'POST',body});
  return r.json();
}

let curTab='buat';

function showTab(name){
  curTab=name;
  document.querySelectorAll('.page').forEach(p=>
    p.classList.toggle('on', p.id==='page-'+name));
  document.querySelectorAll('nav button').forEach(b=>
    b.classList.toggle('on', b.dataset.tab===name));
  // kotak tanya captcha/OTP ikut ke tab yang sedang dilihat, biar tak
  // tersembunyi di tab lain saat proses menunggu jawaban
  placeAsk();
  try{ localStorage.setItem('tab', name); }catch(e){}
}

function placeAsk(){
  const ask=$('ask');
  const slot=curTab==='dump' ? $('ask-dump-slot') : null;
  if(slot && ask.parentElement!==slot) slot.appendChild(ask);
  if(!slot){
    const home=$('page-buat').querySelector('section:last-child');
    if(ask.parentElement!==home) home.insertBefore(ask, home.firstChild);
  }
}

function flash(msg,kind){
  const f=$('flash-'+curTab) || $('flash-buat');
  if(!f) return;
  f.textContent=msg; f.className='flash '+(kind||'');
}

async function loadState(){
  try{
    const s=await jget('/api/state');
    const cfg=s.settings;
    document.querySelectorAll('#cfg [name], #cfg-dump [name]').forEach(el=>{
      if(el.name in cfg && el!==document.activeElement)
        el.value=cfg[el.name]??'';
    });
    $('cnt-akun').textContent=s.akun_lines;
    $('cnt-proxy').textContent=s.proxy_lines;
    renderRun(s);
    renderAccounts(s.accounts, s.running);
  }catch(e){}
}

function toggleEye(key){
  const inp=$(key); if(!inp) return;
  const btn=inp.parentElement.querySelector('.eye');
  const show=inp.type==='password';
  inp.type=show?'text':'password';
  btn.querySelector('.eo').hidden=show;
  btn.querySelector('.ec').hidden=!show;
}

const MODE_LABEL={signup:'buat akun', dump:'dump', credit:'cek credit'};

function renderRun(s){
  const st=$('status'), sp=$('btn-stop');
  const runBtns=document.querySelectorAll('[data-run]');
  if(s.running){
    st.textContent=(MODE_LABEL[s.mode]||s.mode)+' berjalan · pid '+s.pid;
    st.className='badge run';
    if(s.mode) logMode=s.mode;   // reload halaman: arahkan log ke panel benar
    runBtns.forEach(b=>b.disabled=true); sp.disabled=false;
  }else{
    st.textContent='diam'; st.className='badge idle';
    runBtns.forEach(b=>b.disabled=false); sp.disabled=true;
  }
}

const ICON_COPY='<svg viewBox="0 0 24 24" width="14" height="14" fill="none" '+
  'stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="12" height="12" rx="2"/>'+
  '<path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
const ICON_EYE='<svg viewBox="0 0 24 24" width="14" height="14" fill="none" '+
  'stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7'+
  '-4 7-11 7-11-7-11-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
const ICON_REF='<svg viewBox="0 0 24 24" width="14" height="14" fill="none" '+
  'stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7"/>'+
  '<path d="M21 3v6h-6"/></svg>';

let ACC={};              // accounts terakhir dari server
let RUNNING=false;
let showKeys=false;      // "Lihat semua key" ditekan?

// accounts.json menyimpan created_at sebagai "YYYY-MM-DD HH:MM:SS" waktu lokal
function parseTs(s){
  if(!s) return null;
  const d=new Date(String(s).replace(' ','T'));
  return isNaN(d) ? null : d;
}

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
  $('btn-reveal').textContent=showKeys?'Sembunyikan key':'Lihat semua key';
  renderRows();
}

function renderRows(){
  const el=$('accounts'), rows=filtered();
  const total=Object.keys(ACC).length;
  $('tally').textContent=total
    ? rows.length+' dari '+total+' akun'+
      (rows.length?' · total credit '+rows.reduce(
        (s,[,v])=>s+(Number(v.credit)||0),0).toLocaleString('id-ID'):'')
    : '';
  if(!total){el.innerHTML='<div class="empty">belum ada akun.</div>';return;}
  if(!rows.length){
    el.innerHTML='<div class="empty">tak ada akun yang cocok dengan filter.</div>';
    syncSelUI(); return;}
  let html='<table><thead><tr>'+
    '<th style="width:26px"><input type="checkbox" id="ck-all" '+
      'title="pilih semua yang tampil"></th>'+
    '<th>email</th><th>plan</th><th>credit</th>'+
    '<th>api_key</th><th>bayar</th><th>dibuat</th><th></th></tr></thead><tbody>';
  for(const [email,v] of rows){
    const full=v.api_key||'';
    const shown=showKeys?full:(full?full.slice(0,16)+'…':'');
    const ck=SEL.has(email)?' checked':'';
    html+='<tr'+(SEL.has(email)?' class="picked"':'')+'>'+
      '<td><input type="checkbox" class="ck-row" data-email="'+esc(email)+'"'+ck+'></td>'+
      '<td>'+esc(email)+'</td>'+
      '<td class="plan-badge">'+esc(v.plan||'')+'</td>'+
      '<td>'+esc(String(v.credit??''))+'</td>'+
      '<td><div class="keycell">'+
        '<span class="keytext'+(showKeys?' show':'')+'" data-full="'+esc(full)+'">'+
          esc(shown)+'</span>'+
        (full?'<button type="button" class="iconbtn btn-eye" title="lihat/sembunyikan">'
              +ICON_EYE+'</button>'+
              '<button type="button" class="iconbtn btn-copy" data-key="'+esc(full)+
              '" title="copy API key">'+ICON_COPY+'</button>':'')+
      '</div></td>'+
      '<td>'+esc(v.payment_status||'')+'</td>'+
      '<td>'+esc(v.created_at||'')+
        (v.checked_at?'<div class="hint">cek: '+esc(v.checked_at)+'</div>':'')+
      '</td>'+
      '<td class="row" style="gap:5px;flex-wrap:nowrap">'+
        '<button type="button" class="iconbtn btn-ref" data-email="'+esc(email)+
        '" title="refresh credit akun ini"'+(RUNNING?' disabled':'')+'>'+
        ICON_REF+'</button>'+
        '<button type="button" class="danger sm btn-del" data-email="'+esc(email)+
        '"'+(RUNNING?' disabled':'')+'>Hapus</button></td></tr>';
  }
  el.innerHTML=html+'</tbody></table>';
  syncSelUI();
}

// ---- pilihan (checkbox) ----
const SEL=new Set();          // email yang dicentang

function syncSelUI(){
  const shown=filtered().map(([e])=>e);
  // buang pilihan yang tak lagi tampil karena filter berubah
  for(const e of [...SEL]) if(!shown.includes(e)) SEL.delete(e);
  const n=SEL.size;
  ['btn-copy-sel','btn-csv-sel'].forEach(id=>{
    const b=$(id); if(b) b.disabled=!n;});
  const bd=$('btn-del-sel');
  if(bd) bd.disabled=!n||RUNNING;
  // refresh butuh proses bebas: tak bisa jalan kalau ada run lain
  const rs=$('btn-ref-sel'), ra=$('btn-ref-all');
  if(rs) rs.disabled=!n||RUNNING;
  if(ra) ra.disabled=RUNNING||!Object.keys(ACC).length;
  ['btn-copy-sel','btn-csv-sel'].forEach(id=>{
    const b=$(id);
    if(b) b.textContent=(id==='btn-copy-sel'?'Copy terpilih':'CSV terpilih')+
      (n?' ('+n+')':'');});
  if(bd) bd.textContent='Hapus terpilih'+(n?' ('+n+')':'');
  if(rs) rs.textContent='Refresh credit terpilih'+(n?' ('+n+')':'');
  const all=$('ck-all');
  if(all){
    all.checked = shown.length>0 && n===shown.length;
    all.indeterminate = n>0 && n<shown.length;
  }
}

let lastAccJson='';

function renderAccounts(acc, running){
  ACC=acc||{}; RUNNING=!!running;
  // polling tiap 2.5s: kalau isinya sama, JANGAN bangun ulang tabel --
  // itu akan menutup kembali key yang baru dibuka/di-scroll pengguna
  const sig=JSON.stringify(acc)+'|'+running;
  if(sig===lastAccJson) return;
  lastAccJson=sig;
  renderRows();
}

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
    flash(scope==='sel'?'belum ada baris dipilih':'tak ada API key untuk dicopy','err');
    return;}
  const text = fmt==='csv'
    ? 'email,api_key,plan,credit\n'+rows.map(([e,v])=>
        [e, v.api_key, v.plan||'', v.credit??''].join(',')).join('\n')
    : rows.map(([,v])=>v.api_key).join('\n');
  const ok=await copyText(text);
  flash(ok ? rows.length+' API key dicopy'+(fmt==='csv'?' (CSV)':'')+
             (scope==='sel'?' — dari pilihan':'')
           : 'gagal copy — browser menolak akses clipboard',
        ok?'ok':'err');
}

async function refreshCredit(scope, one){
  const data=new URLSearchParams();
  let n=0;
  if(one){ data.append('email', one); n=1; }
  else if(scope==='sel'){
    for(const e of SEL) data.append('email', e);
    n=SEL.size;
    if(!n){flash('belum ada baris dipilih','err'); return;}
  }else{
    n=Object.keys(ACC).length;      // tanpa email = semua
  }
  const j=await jpost('/api/refresh-credit', data);
  if(!j.ok){flash(j.error||'gagal mulai refresh','err'); return;}
  // refresh jalan sebagai proses `signup.py credit` -> lognya ke panel utama
  logMode='credit'; lastSeq=0; $('log').innerHTML='';
  $('ref-note').textContent='refresh '+n+' akun berjalan… lihat tab Buat akun untuk lognya';
  flash('refresh credit '+n+' akun dimulai (login tiap akun, butuh beberapa detik)','ok');
}

async function deleteSelected(){
  const list=[...SEL];
  if(!list.length) return;
  if(!confirm('Hapus '+list.length+' akun dari accounts.json?')) return;
  let ok=0, fail=0;
  for(const email of list){
    const j=await jpost('/api/delete-account', new URLSearchParams({email}));
    if(j.ok){ok++; SEL.delete(email);} else fail++;
  }
  flash(ok+' akun dihapus'+(fail?', '+fail+' gagal':''), fail?'err':'ok');
  lastAccJson='';          // paksa bangun ulang tabel
  loadState();
}

// checkbox: pilih satu / pilih semua yang tampil
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
  // copy satu API key
  const cp=ev.target.closest('.btn-copy');
  if(cp){
    const ok=await copyText(cp.dataset.key);
    flash(ok?'API key dicopy':'gagal copy — browser menolak akses clipboard',
          ok?'ok':'err');
    return;
  }
  // lihat/sembunyikan satu API key
  const ey=ev.target.closest('.btn-eye');
  if(ey){
    const span=ey.parentElement.querySelector('.keytext');
    const full=span.dataset.full;
    const open=span.classList.toggle('show');
    span.textContent=open?full:(full.slice(0,16)+'…');
    return;
  }
  // refresh credit satu akun
  const rf=ev.target.closest('.btn-ref');
  if(rf){
    refreshCredit(null, rf.dataset.email);
    return;
  }
  const btn=ev.target.closest('.btn-del');
  if(!btn) return;
  const email=btn.dataset.email;
  if(!confirm('Hapus akun '+email+' dari accounts.json?')) return;
  const data=new URLSearchParams({email});
  const j=await jpost('/api/delete-account', data);
  flash(j.ok?'akun dihapus':'gagal hapus: '+(j.error||''), j.ok?'ok':'err');
  loadState();
});

async function save(loud){
  const data=new URLSearchParams();
  // kirim SEMUA field dari kedua form: server menghapus kunci yang dikirim
  // kosong, jadi mengirim sebagian saja akan menghapus setting tab lain
  document.querySelectorAll('#cfg [name], #cfg-dump [name]').forEach(el=>
    data.append(el.name, el.value));
  const j=await jpost('/api/save', data);
  if(loud || !j.ok)
    flash(j.ok?'setting tersimpan':'gagal: '+(j.error||''), j.ok?'ok':'err');
  return j.ok;
}

async function startRun(mode){
  const ok=await save(false); if(!ok) return;
  const j=await jpost('/api/start?mode='+mode);
  if(!j.ok){flash(j.error||'gagal mulai','err'); return;}
  logMode=mode;
  const box=$(mode==='dump'?'log-dump':'log');
  box.innerHTML='';                       // log baru, jangan tumpuk yang lama
  lastSeq=0;                              // proses baru -> seq server juga reset
  flash('mulai '+(MODE_LABEL[mode]||mode)+'…','ok');
}

async function stopRun(){
  await jpost('/api/stop'); flash('diminta berhenti…');
}

let logMode='signup';       // log dump masuk panel dump, sisanya panel utama

async function pollLog(){
  while(true){
    try{
      const d=await jget('/api/log?after='+lastSeq);
      if(d.lines && d.lines.length){
        const log=$(logMode==='dump'?'log-dump':'log');
        for(const t of d.lines){
          const div=document.createElement('div');
          div.textContent=t; log.appendChild(div);
        }
        lastSeq=d.seq;
        log.scrollTop=log.scrollHeight;
      }
    }catch(e){}
    await new Promise(r=>setTimeout(r,400));
  }
}

async function pollState(){
  while(true){
    await loadState();
    await new Promise(r=>setTimeout(r,2500));
  }
}

let lastPendingSeq=0;

function renderPending(p){
  const box=$('ask');
  if(!p){box.hidden=true; box.innerHTML=''; return;}
  box.hidden=false;
  const img=p.kind==='captcha'
    ? '<img src="'+esc(p.image)+'" alt="captcha">'
    : '';
  const label=p.kind==='captcha'
    ? 'Captcha diperlukan — '+esc(p.email)
    : 'Kode OTP diperlukan — '+esc(p.email);
  box.innerHTML='<span class="lab">'+label+'</span>'+img+
    '<div class="row"><input type="text" id="ask-input" autocomplete="off" '+
    'placeholder="'+(p.kind==='captcha'?'jawaban captcha':'kode OTP')+'">'+
    '<button type="button" class="primary" id="ask-send">Kirim</button></div>';
  $('ask-send').onclick=sendAnswer;
  $('ask-input').addEventListener('keydown', e=>{
    if(e.key==='Enter') sendAnswer();
  });
  $('ask-input').focus();
}

async function sendAnswer(){
  const inp=$('ask-input');
  if(!inp) return;
  const value=inp.value;
  inp.disabled=true;
  const data=new URLSearchParams({value});
  const j=await jpost('/api/answer', data);
  if(!j.ok){
    flash(j.error||'gagal kirim jawaban','err');
    if($('ask-input')) $('ask-input').disabled=false;   // biar bisa dicoba lagi
  }
}

async function pollPending(){
  while(true){
    try{
      const d=await jget('/api/pending?after='+lastPendingSeq);
      lastPendingSeq=d.seq;
      renderPending(d.pending);
    }catch(e){}
    await new Promise(r=>setTimeout(r,400));
  }
}

// tab terakhir diingat, biar refresh tak selalu balik ke tab pertama
try{
  const t=localStorage.getItem('tab');
  if(t && $('page-'+t)) showTab(t);
}catch(e){}

loadState();
pollLog();
pollState();
pollPending();
</script>
</body>
</html>
"""


def render_page():
    """Bangun dua form: setting umum (tab Setting) dan aturan dump (tab Dump)."""
    forms = {"setting": "", "dump": ""}
    for group, tab, fields in GROUPS:
        items = "".join(
            '<div class="field' + (" full" if ftype == "textarea" else "") + '">'
            + field_html(key, ftype, label, opts, hint) + '</div>'
            for key, ftype, label, opts, hint in fields)
        forms[tab] += (f'<fieldset><legend>{group}</legend>'
                       f'<div class="grid">{items}</div></fieldset>')
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
