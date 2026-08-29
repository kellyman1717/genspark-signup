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
}

# Skema form: (grup, [(key, tipe, label, opsi, hint), ...]).
GROUPS = [
    ("Captcha", [
        ("CAPTCHA_PROVIDER", "select", "Provider",
         ["manual", "2captcha", "capsolver", "anticaptcha", "capmonster",
          "rucaptcha", "azcaptcha"],
         "manual = ketik sendiri (belum didukung di WebUI)"),
        ("CAPTCHA_KEY", "password", "API key", None,
         "kosongkan kalau manual"),
        ("CAPTCHA_TRIES", "number", "Coba ulang", None,
         "berapa kali solver boleh salah baca"),
    ]),
    ("Email", [
        ("EMAIL_SOURCE", "select", "Sumber email", ["file", "emailnator"],
         "emailnator = alamat + OTP otomatis"),
        ("EMAIL_COUNT", "number", "Jumlah akun", None, "kalau emailnator"),
        ("EMAIL_TRIES", "number", "Tukar alamat", None,
         "maks ganti alamat kalau conflict"),
        ("OTP_TIMEOUT", "number", "Timeout OTP (detik)", None,
         "berapa lama nunggu OTP masuk inbox"),
    ]),
    ("Proxy", [
        ("PROXY", "textarea", "Proxy", None,
         "koma / baris, atau di proxy.txt; kosong = koneksi langsung"),
        ("PROXY_TRIES", "number", "Coba proxy", None,
         "ganti proxy sebanyak ini kalau mati"),
        ("TIMEOUT", "text", "Timeout request (detik)", None,
         "kosongkan = otomatis (5 pakai proxy, 30 langsung)"),
        ("MAIL_TIMEOUT", "number", "Timeout inbox (detik)", None,
         "minimal 20, emailnator lambat"),
    ]),
    ("Akun & Jalankan", [
        ("PASSWORD", "password", "Password", None,
         "wajib; syarat Azure B2C: kapital+kecil+angka+simbol, min 8"),
        ("WORKERS", "number", "Worker paralel", None, ""),
        ("TAB_DELAY", "number", "Jeda antar tab (detik)", None, ""),
        ("FREE_CREDIT", "number", "Batas credit gratis", None,
         "credit di atas ini = kuota langganan masuk"),
    ]),
    ("Dump (tanpa checkout)", [
        ("DUMP_MIN_CREDIT", "number", "Ambang credit", None,
         "akun disimpan kalau creditnya >= ini"),
        ("DUMP_TARGET", "number", "Target akun bagus", None,
         "berhenti setelah dapat sebanyak ini"),
        ("DUMP_MAX_TRIES", "number", "Maks percobaan", None,
         "batas atas biar tak jalan selamanya"),
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


def start(mode):
    with RUN.lock:
        if RUN.proc is not None and RUN.proc.poll() is None:
            return False, "masih berjalan — hentikan dulu"
        # -u: print() disiram langsung (tanpanya stdout di-buffer saat di-pipe,
        # log baru muncul saat proses selesai)
        cmd = [sys.executable, "-u", SIGNUP]
        if mode != "signup":
            cmd.append(mode)        # "credit" | "dump"
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
  :root{--bg:#0f1216;--panel:#171b21;--panel2:#1e232b;--line:#2a323c;
        --text:#d7e0ea;--muted:#8894a3;--acc:#4f9dff;--ok:#3ecf8e;
        --warn:#ffb454;--bad:#ff6b6b}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;
       background:var(--bg);color:var(--text)}
  header{padding:14px 22px;border-bottom:1px solid var(--line);
         display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  header h1{font-size:17px;margin:0;font-weight:600}
  header .sub{color:var(--muted);font-size:12.5px}
  .badge{padding:3px 10px;border-radius:20px;font-size:12px;
         border:1px solid var(--line);background:var(--panel2)}
  .badge.idle{color:var(--muted)}
  .badge.run{color:#06121c;background:var(--acc);border-color:var(--acc);
             font-weight:600}
  main{display:grid;grid-template-columns:minmax(340px,44fr) 56fr;
       gap:16px;padding:16px 22px;align-items:start}
  @media(max-width:920px){main{grid-template-columns:1fr}}
  section{background:var(--panel);border:1px solid var(--line);
          border-radius:12px;padding:16px}
  section h2{font-size:14px;margin:0 0 12px;font-weight:600}
  fieldset{border:0;margin:0 0 14px;padding:0}
  legend{font-size:12px;color:var(--acc);text-transform:uppercase;
         letter-spacing:.06em;margin-bottom:8px;font-weight:600}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}
  @media(max-width:480px){.grid{grid-template-columns:1fr}}
  .field{display:flex;flex-direction:column;gap:4px}
  .field.full{grid-column:1 / -1}
  .lab{font-size:12px;color:var(--muted)}
  input,select,textarea{background:var(--panel2);border:1px solid var(--line);
       color:var(--text);border-radius:8px;padding:8px 10px;font:inherit;
       width:100%}
  input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc)}
  textarea{font-family:ui-monospace,Consolas,monospace;font-size:12px}
  .hint{font-size:11.5px;color:var(--muted)}
  .pwrap{position:relative}
  .pwrap input{padding-right:34px}
  .eye{position:absolute;right:4px;top:50%;transform:translateY(-50%);
       background:transparent;border:0;padding:4px;color:var(--muted);
       cursor:pointer;display:flex}
  .eye:hover{color:var(--acc)}
  .ask-box{border:1px solid var(--warn);border-radius:10px;padding:12px;
           margin-bottom:12px;background:#221d12}
  .ask-box .lab{color:var(--warn);font-weight:600;font-size:12.5px;
                margin-bottom:8px;display:block}
  .ask-box img{display:block;max-width:220px;border-radius:6px;margin-bottom:8px;
               background:#fff}
  .ask-box .row input{max-width:220px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  button{background:var(--panel2);color:var(--text);border:1px solid var(--line);
         padding:9px 16px;border-radius:8px;font:inherit;cursor:pointer}
  button:hover{border-color:var(--acc)}
  button.primary{background:var(--acc);border-color:var(--acc);
         color:#06121c;font-weight:600}
  button.danger:hover{border-color:var(--bad);color:var(--bad)}
  button:disabled{opacity:.45;cursor:not-allowed}
  #flash{font-size:12.5px;color:var(--muted)}
  #flash.ok{color:var(--ok)} #flash.err{color:var(--bad)}
  #log{background:#0a0d10;border:1px solid var(--line);border-radius:8px;
       height:46vh;min-height:240px;overflow:auto;padding:10px 12px;
       font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;
       word-break:break-word}
  #log .dim{color:var(--muted)}
  table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);
        vertical-align:top;word-break:break-all}
  th{color:var(--muted);font-weight:600;position:sticky;top:0;
     background:var(--panel)}
  .empty{color:var(--muted);font-size:12.5px;padding:6px 0}
  .plan-badge{color:var(--ok)}
</style>
</head>
<body>
<header>
  <div>
    <h1>genspark-signup</h1>
    <div class="sub">setting diubah di sini → disimpan ke .env → signup.py jalan di belakang</div>
  </div>
  <div class="row" style="margin-left:auto">
    <span id="status" class="badge idle">diam</span>
    <span class="badge" title="akun di akun.txt">akun.txt: <b id="cnt-akun">0</b></span>
    <span class="badge" title="proxy dari .env + proxy.txt">proxy: <b id="cnt-proxy">0</b></span>
  </div>
</header>

<main>
  <section>
    <h2>Setting</h2>
    <div class="row" style="margin-bottom:12px">
      <button class="primary" id="btn-save" onclick="save()">Simpan setting</button>
      <button id="btn-start" class="primary" onclick="startRun('signup')">Mulai</button>
      <button id="btn-dump" onclick="startRun('dump')"
              title="bikin akun tempmail, simpan yang creditnya lolos ambang (tanpa checkout)">Dump</button>
      <button id="btn-credit" onclick="startRun('credit')">Cek credit</button>
      <button id="btn-stop" class="danger" onclick="stopRun()" disabled>Stop</button>
    </div>
    <div id="flash"></div>

    <div id="cfg">
      __FORM__
    </div>
  </section>

  <section>
    <div id="ask" hidden></div>
    <h2>Log</h2>
    <div id="log"><span class="dim">belum ada proses dijalankan…</span></div>
    <h2 style="margin-top:16px">Hasil (accounts.json)</h2>
    <div id="accounts" class="empty">memuat…</div>
  </section>
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

function flash(msg,kind){const f=$('flash');f.textContent=msg;
  f.className=kind||'';}

async function loadState(){
  try{
    const s=await jget('/api/state');
    const cfg=s.settings;
    document.querySelectorAll('#cfg [name]').forEach(el=>{
      if(el.name in cfg) el.value=cfg[el.name]??'';
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

function renderRun(s){
  const st=$('status'), sp=$('btn-stop');
  const runBtns=[$('btn-start'), $('btn-dump'), $('btn-credit')];
  if(s.running){
    st.textContent='berjalan ('+s.mode+' pid '+s.pid+')';
    st.className='badge run';
    runBtns.forEach(b=>b.disabled=true); sp.disabled=false;
  }else{
    st.textContent='diam'; st.className='badge idle';
    runBtns.forEach(b=>b.disabled=false); sp.disabled=true;
  }
}

function renderAccounts(acc, running){
  const el=$('accounts');
  const rows=Object.entries(acc||{}).sort((a,b)=>
    (b[1].created_at||'').localeCompare(a[1].created_at||''));
  if(!rows.length){el.innerHTML='<div class="empty">belum ada akun.</div>';return;}
  let html='<table><thead><tr><th>email</th><th>plan</th><th>credit</th>'+
    '<th>api_key</th><th>bayar</th><th>dibuat</th><th></th></tr></thead><tbody>';
  for(const [email,v] of rows){
    const key=(v.api_key||'').slice(0,14)+(v.api_key?'…':'');
    html+='<tr><td>'+esc(email)+'</td>'+
      '<td class="plan-badge">'+esc(v.plan||'')+'</td>'+
      '<td>'+esc(String(v.credit??''))+'</td>'+
      '<td>'+esc(key)+'</td>'+
      '<td>'+esc(v.payment_status||'')+'</td>'+
      '<td>'+esc((v.created_at||'').slice(5,16))+'</td>'+
      '<td><button type="button" class="danger btn-del" data-email="'+esc(email)+
      '"'+(running?' disabled':'')+'>Hapus</button></td></tr>';
  }
  el.innerHTML=html+'</tbody></table>';
}

document.addEventListener('click', async (ev)=>{
  const btn=ev.target.closest('.btn-del');
  if(!btn) return;
  const email=btn.dataset.email;
  if(!confirm('Hapus akun '+email+' dari accounts.json?')) return;
  const data=new URLSearchParams({email});
  const j=await jpost('/api/delete-account', data);
  flash(j.ok?'akun dihapus':'gagal hapus: '+(j.error||''), j.ok?'ok':'err');
  loadState();
});

async function save(){
  const data=new URLSearchParams();
  document.querySelectorAll('#cfg [name]').forEach(el=>
    data.append(el.name, el.value));
  const j=await jpost('/api/save', data);
  flash(j.ok?'setting tersimpan':'gagal: '+(j.error||''), j.ok?'ok':'err');
  return j.ok;
}

async function startRun(mode){
  const ok=await save(); if(!ok) return;
  const j=await jpost('/api/start?mode='+mode);
  if(!j.ok){flash(j.error||'gagal mulai','err'); return;}
  const msg={credit:'mulai cek credit…', dump:'mulai dump akun…'}[mode]
            || 'mulai signup…';
  flash(msg,'ok');
}

async function stopRun(){
  await jpost('/api/stop'); flash('diminta berhenti…');
}

async function pollLog(){
  while(true){
    try{
      const d=await jget('/api/log?after='+lastSeq);
      if(d.lines && d.lines.length){
        const log=$('log');
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

loadState();
pollLog();
pollState();
pollPending();
</script>
</body>
</html>
"""


def render_page():
    form = ""
    for group, fields in GROUPS:
        items = []
        for key, ftype, label, opts, hint in fields:
            cls = " full" if ftype == "textarea" else ""
            items.append('<div class="field' + cls + '">'
                         + field_html(key, ftype, label, opts, hint)
                         + '</div>')
        items = "".join(items)
        form += f"<fieldset><legend>{group}</legend><div class=\"grid\">{items}</div></fieldset>"
    return PAGE.replace("__FORM__", form)


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
