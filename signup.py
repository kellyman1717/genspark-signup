#!/usr/bin/env python3
"""Genspark.ai auto-signup per email from akun.txt (Azure AD B2C flow)."""
import json, re, sys, time, urllib.parse, urllib.request, urllib.error, base64, os, webbrowser
import csv
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import mailer
import proxies
import solvers

BASE = "https://login.genspark.ai/gensparkad.onmicrosoft.com/B2C_1_new_login"
APP = "https://www.genspark.ai"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"

PWD_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "akun.txt")
ACCOUNTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
HOST = "https://login.genspark.ai"

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path=ENV_FILE):
    """Baca .env -> dict. Env var asli menang, jadi `set X=..` bisa menimpa .env."""
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    cfg.update({k: v for k, v in os.environ.items() if k in cfg or k in (
        "CAPTCHA_PROVIDER", "CAPTCHA_KEY", "CAPTCHA_TRIES",
        "EMAIL_SOURCE", "EMAIL_COUNT", "EMAIL_TRIES", "OTP_TIMEOUT",
        "PROXY", "PROXY_TRIES", "TIMEOUT", "MAIL_TIMEOUT", "WORKERS",
        "TAB_DELAY", "FREE_CREDIT", "PASSWORD")})
    return cfg


ENV = load_env()

# Captcha: manual (default) | 2captcha | capsolver | anticaptcha | capmonster
#          | rucaptcha | azcaptcha.  Isi di .env (lihat .env.example).
CAPTCHA_PROVIDER = ENV.get("CAPTCHA_PROVIDER", "manual")
CAPTCHA_KEY = ENV.get("CAPTCHA_KEY", "")
CAPTCHA_TRIES = int(ENV.get("CAPTCHA_TRIES", "3"))  # solver bisa salah baca

# Email: emailnator (auto, alamat + OTP otomatis) | file (akun.txt, OTP manual)
EMAIL_SOURCE = ENV.get("EMAIL_SOURCE", "file")
EMAIL_COUNT = int(ENV.get("EMAIL_COUNT", "1"))  # berapa akun kalau emailnator
EMAIL_TRIES = int(ENV.get("EMAIL_TRIES", "3"))  # tukar alamat kalau sudah dipakai
OTP_TIMEOUT = int(ENV.get("OTP_TIMEOUT", "300"))
WORKERS = int(ENV.get("WORKERS", "6"))       # paralel untuk fase jaringan

# Proxy: isi PROXY di .env (pisah koma/baris) dan/atau taruh di proxy.txt.
# Tipe: http, https, socks4, socks4a, socks5, socks5h. Kosong -> koneksi langsung.
PROXY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.txt")
PROXY_POOL = proxies.Pool(proxies.load(ENV.get("PROXY", ""), PROXY_FILE))
# proxy publik banyak yang mati; coba proxy lain sebanyak ini kalau koneksi gagal
PROXY_TRIES = int(ENV.get("PROXY_TRIES", "4"))
# Timeout request ke Genspark. Pendek supaya proxy mati cepat ketahuan:
# menunggu 30s untuk proxy yang tak akan menjawab cuma memboroskan waktu.
TIMEOUT = int(ENV.get("TIMEOUT", "5" if len(PROXY_POOL) else "30"))
# Emailnator jauh lebih lambat: message-list pada inbox berisi butuh 5-6s
# (terukur), jadi TIMEOUT pendek bikin pembacaan inbox gagal terus.
# Dipisah, dan tak pernah lebih pendek dari 20s.
MAIL_TIMEOUT = max(int(ENV.get("MAIL_TIMEOUT", "30")), 20)
# Password semua akun. WAJIB diisi di .env; tak ada default, supaya
# password nyata tak pernah ikut tersimpan di dalam kode.
PASSWORD = ENV.get("PASSWORD", "")
SAMPLE_PASSWORD = "GantiIni@2026"  # hanya untuk pesan bantuan
# Diset webui.py pada subprocess-nya. Saat aktif, captcha/OTP manual dikirim
# lewat marker JSON di stdout (dibaca webui.py) bukan gambar/prompt terminal.
WEBUI = os.environ.get("GENSPARK_WEBUI") == "1"
IO_LOCK = threading.Lock()   # accounts.json + stdout
TAB_LOCK = threading.Lock()  # buka tab Stripe satu-satu
# detik jeda antar tab checkout; kartu diisi manusia, jangan diserbu
TAB_DELAY = float(ENV.get("TAB_DELAY", "3"))
# saldo akun gratis. Credit di atas ini berarti kuota langganan sudah masuk.
FREE_CREDIT = int(ENV.get("FREE_CREDIT", "200"))

# ---- mode dump (py signup.py dump) ----
# Bikin akun dari tempmail, cek credit, SIMPAN yang creditnya lolos ambang.
# Tanpa checkout Stripe: tak ada kartu, tak ada tagihan.
DUMP_MIN_CREDIT = int(ENV.get("DUMP_MIN_CREDIT", "2000"))  # ambang simpan
DUMP_TARGET = int(ENV.get("DUMP_TARGET", "1"))    # berapa akun bagus dicari
DUMP_MAX_TRIES = int(ENV.get("DUMP_MAX_TRIES", "10"))  # batas percobaan
# berapa akun digarap serentak. Dipaksa 1 kalau captcha manual: jawaban
# diketik satu-satu, jadi dua prompt sekaligus tak bisa dijawab.
DUMP_WORKERS = max(int(ENV.get("DUMP_WORKERS", "3")), 1)
# detik menunggu credit/plan menyusul setelah signup. Pendek: akun yang cuma
# dapat bonus gratis tak akan naik, dan menunggu lama menghambat antrean.
DUMP_CREDIT_WAIT = int(ENV.get("DUMP_CREDIT_WAIT", "25"))
DUMP_LOCK = threading.Lock()   # accounts dict + alokasi alamat email

# Tier dari HAR /api/payment/sub2/tier_config. Ganti TIER_ID untuk paket lain.
TIER_ID = "plus1"
TIER = {
    "price_id": "price_1T7YeoHy7UpDvrVidVyuxxNm",
    "price_name": "ai.genspark.vip.plus.c1.month",
    "plan_price": "24.99",
}


def gmail_key(email):
    """Kunci inbox Gmail: titik diabaikan Gmail, jadi a.b@gmail dan ab@gmail
    adalah inbox yang SAMA -- dan akun Genspark yang sama."""
    user, _, dom = email.partition("@")
    return user.replace(".", "").lower() + "@" + dom.lower()


def fresh_email(mail, avoid=(), tries=6):
    """Alamat emailnator yang inbox-nya belum pernah dipakai.

    Emailnator mengacak titik pada nama yang sama, jadi ia bisa memberi
    "a.n.disan.t.o@" padahal "an.d.i.santo@" sudah punya akun -- Gmail
    mengabaikan titik, jadi keduanya satu inbox dan signup kena conflict.
    """
    for _ in range(tries):
        em = mail.new_email()
        if gmail_key(em) not in avoid:
            return em
    return em      # sudah usaha; biarkan alur conflict yang menangani


def used_inboxes():
    """Kunci inbox yang sudah terpakai: dari accounts.json dan akun.txt."""
    keys = set()
    for em in load_accounts():
        keys.add(gmail_key(em))
    if os.path.exists(PWD_SRC):
        with open(PWD_SRC, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    keys.add(gmail_key(line.split(":")[0]))
    return keys


def record_emails(fresh):
    """Catat alamat ke akun.txt biar bisa dipakai lagi. Awali newline kalau file
    lama tak diakhiri newline, supaya alamat baru tak nempel ke baris terakhir."""
    if not fresh:
        return
    with IO_LOCK:
        pre = ""
        if os.path.exists(PWD_SRC):
            with open(PWD_SRC, encoding="utf-8") as f:
                prev = f.read()
            if prev and not prev.endswith(chr(10)):
                pre = chr(10)
        with open(PWD_SRC, "a", encoding="utf-8") as f:
            f.write(pre + chr(10).join(fresh) + chr(10))


def get_emails():
    """EMAIL_SOURCE=file -> baca akun.txt. =emailnator -> bikin alamat baru."""
    if EMAIL_SOURCE.lower() == "emailnator":
        m = mailer.Emailnator(proxy=PROXY_POOL.next(), timeout=MAIL_TIMEOUT)
        avoid = used_inboxes()
        fresh = []
        for _ in range(EMAIL_COUNT):
            em = fresh_email(m, avoid)
            avoid.add(gmail_key(em))     # jangan tabrakan di dalam batch ini juga
            fresh.append(em)
        record_emails(fresh)
        return fresh
    emails = []
    with open(PWD_SRC, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            email = line.split(":")[0].strip()
            if email:
                emails.append(email)
    return emails


def credit_by_key(api_key, proxy=None, timeout=None):
    """Sisa credit dari API key SAJA -- tanpa login, captcha, atau password.

    Endpoint yang dipakai CLI resmi `gsk` (@genspark/cli): GET
    /api/tool_cli/me dengan header X-Api-Key. Ini satu request dan tak
    menyentuh Azure B2C sama sekali, jadi jauh lebih murah daripada login.

    Balasannya: email, name, plan, personal_plan, org_plan, org_role,
    credit_balance (float -- lebih presisi daripada angka di app web).

    Melempar RuntimeError dengan pesan yang bisa dibaca pengguna. API key
    TIDAK pernah masuk ke pesan error: pesan itu berakhir di log dan di UI.
    """
    key = (api_key or "").strip()
    if not key:
        raise RuntimeError("API key kosong")

    opener = proxies.build_opener(proxy)
    req = urllib.request.Request(
        f"{APP}/api/tool_cli/me",
        headers={"User-Agent": UA, "Accept": "application/json",
                 "X-Api-Key": key})
    try:
        raw = opener.open(req, timeout=timeout or TIMEOUT).read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("API key ditolak (tidak valid atau dicabut)") from None
        raise RuntimeError(f"HTTP {e.code} dari /api/tool_cli/me") from None
    try:
        j = json.loads(raw)
    except ValueError:
        raise RuntimeError("balasan bukan JSON (mungkin diblokir Cloudflare)") from None
    if not isinstance(j, dict) or "credit_balance" not in j:
        raise RuntimeError(f"balasan tak memuat credit_balance: {str(j)[:120]}")
    return j


def key_cogen_id(api_key):
    """cogen_id yang tertanam di dalam API key, tanpa jaringan. None kalau gagal.

    Key `gsk-<base64url>` membungkus JSON {cogen_id, key_id, ctime, ...}.
    Berguna untuk mencocokkan key lepas dengan akun di accounts.json tanpa
    memanggil Genspark sama sekali.
    """
    body = (api_key or "").strip()
    body = body[4:] if body[:4].lower() == "gsk-" else body
    for pad in range(4):
        try:
            raw = base64.urlsafe_b64decode(body + "=" * pad)
        except Exception:
            continue
        i, jx = raw.find(b"{"), raw.find(b"}")
        if 0 <= i < jx:
            try:
                return json.loads(raw[i:jx + 1]).get("cogen_id")
            except ValueError:
                pass
    return None


class Client:
    def __init__(self, proxy=None):
        # satu proxy dipegang sepanjang sesi: B2C mengikat transaksi ke IP,
        # ganti IP di tengah alur bikin transaksi ditolak.
        self.proxy = proxy if proxy is not None else PROXY_POOL.next()
        self.opener = proxies.build_opener(
            self.proxy, urllib.request.HTTPCookieProcessor())
        self.csrf = None
        self.tx = None
        self.challenge_id = None

    def req(self, url, headers=None, data=None, method=None):
        h = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if headers:
            h.update(headers)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        r = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            return self.opener.open(r, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                return e  # expose redirect for Location handling
            # sertakan body: "HTTP Error 500" tanpa isi tak bisa didiagnosa
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                detail = ""
            raise RuntimeError(f"HTTP {e.code} {url.split('?')[0]}: {detail}") from None

    def api(self, url, data=None):
        h = {
            "X-CSRF-TOKEN": self.csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": self.page_url,
        }
        return self.req(url, h, data)

    def app_api(self, path, data=None):
        """Genspark app API (JSON, session cookie)."""
        h = {
            "User-Agent": UA,  # tanpa ini Cloudflare balas 403
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": APP,
            "Referer": APP + "/",
        }
        body = json.dumps(data).encode() if data is not None else None
        r = urllib.request.Request(APP + path, data=body, headers=h)
        return json.load(self.opener.open(r, timeout=TIMEOUT))

    def jget(self, url):
        return json.load(self.api(url))

    def jpost(self, url, data):
        return json.load(self.api(url, data))

    # ---- flow steps ----
    def start(self):
        # 1. /api/login -> 307 to authorize
        r = self.req(f"{APP}/api/login?legacy_b2c=true&redirect_url={urllib.parse.quote(APP + '/')}")
        auth_url = r.geturl()
        # 2. authorize page
        r = self.req(auth_url)
        page = r.read().decode("utf-8", "replace")
        m = re.search(r'"api":"CombinedSigninAndSignup","csrf":"([^"]+)"', page)
        if not m:
            raise RuntimeError("unified csrf not found in authorize page")
        self.login_csrf = m.group(1)
        m2 = re.search(r'"transId":"([^"]+)"', page)
        if not m2:
            raise RuntimeError("transId not found in authorize page")
        self.login_tx = m2.group(1)
        self.login_url = auth_url
        self.page_url = f"{BASE}/api/CombinedSigninAndSignup/unified?local=signup&csrf_token=" + self.login_csrf

    def open_signup(self):
        """Fetch the signup page to get its own csrf + transId."""
        r = self.req(self.page_url)
        page = r.read().decode("utf-8", "replace")
        m = re.search(r'"csrf":"([^"]+)"', page)
        if not m:
            raise RuntimeError("csrf not found in signup page")
        self.csrf = m.group(1)
        m = re.search(r'"transId":"([^"]+)"', page)
        if not m:
            raise RuntimeError("transId not found in signup page")
        self.tx = m.group(1)  # "StateProperties=..."

    P = "&p=B2C_1_new_login"

    def get_captcha(self):
        url = f"{BASE}/SelfAsserted/DisplayControlAction/vbeta/captchaControlChallengeCode/GetChallenge?tx={self.tx}{self.P}&challengeType=Visual"
        j = self.jget(url)
        if j.get("status") != "200":
            raise RuntimeError(f"GetChallenge failed: {j}")
        self.challenge_id = j["challengeId"]
        return j["challengeString"]  # data:image/jpeg;base64,...

    def verify_captcha(self, text):
        """Return True if solved, False if wrong answer / expired session."""
        url = f"{BASE}/SelfAsserted/DisplayControlAction/vbeta/captchaControlChallengeCode/VerifyChallenge?tx={self.tx}{self.P}"
        j = self.jpost(url, {
            "challengeId": self.challenge_id,
            "captchaEntered": text,
            "challengeType": "Visual",
            "azureRegion": "",
        })
        return j.get("status") == "200" and j.get("isCaptchaSolved") == "True"

    def send_code(self, email):
        url = f"{BASE}/SelfAsserted/DisplayControlAction/vbeta/emailVerificationControl/SendCode?tx={self.tx}{self.P}"
        j = self.jpost(url, {"email": email})
        if j.get("status") != "200":
            raise RuntimeError(f"SendCode failed: {j}")

    def verify_code(self, email, code):
        url = f"{BASE}/SelfAsserted/DisplayControlAction/vbeta/emailVerificationControl/VerifyCode?tx={self.tx}{self.P}"
        j = self.jpost(url, {"email": email, "emailVerificationCode": code})
        if j.get("status") != "200":
            raise RuntimeError(f"VerifyCode failed: {j}")

    def signup(self, email, otp, captcha, password=PASSWORD):
        url = f"{BASE}/SelfAsserted?tx={self.tx}{self.P}"
        j = self.jpost(url, {
            "email": email,
            "emailVerificationCode": otp,
            "captchaControlChallengeCode": captcha,
            "newPassword": password,
            "reenterPassword": password,
            "request_type": "RESPONSE",
        })
        if j.get("status") != "200":
            raise RuntimeError(f"SelfAsserted failed: {j}")
        # 11. confirmed -> 302 to /api/auth -> 307 to home (session set)
        self.req(f"{BASE}/api/SelfAsserted/confirmed?csrf_token={self.csrf}",
                 headers={"Referer": self.page_url})
        # 12. verify session
        return json.load(self.req(f"{APP}/api/user"))

    def login(self, email, password=PASSWORD):
        """Sign in an existing account. No captcha/OTP. Returns True on success."""
        # login page SETTINGS: api=CombinedSigninAndSignup, but POST goes to SelfAsserted
        self.csrf, self.tx, self.page_url = self.login_csrf, self.login_tx, self.login_url
        j = self.jpost(f"{BASE}/SelfAsserted?tx={self.tx}{self.P}", {
            "request_type": "RESPONSE",
            "email": email,
            "password": password,
        })
        if j.get("status") != "200":
            return False
        # confirmed -> /api/auth -> home (session cookie set)
        self.req(f"{BASE}/api/CombinedSigninAndSignup/confirmed"
                 f"?rememberMe=false&csrf_token={self.csrf}&tx={self.tx}{self.P}",
                 headers={"Referer": self.login_url})
        u = self.get_user()
        return bool(u.get("data", {}).get("cogen", {}).get("id"))

    # ---- payment + api key ----
    def get_user(self):
        return self.app_api("/api/user")

    def set_data_retention(self, on=True):
        """Setel AI data retention. on=True -> Genspark BOLEH menyimpan data.

        Nama field di Genspark terbalik dari nama argumen ini:
        disable_data_retention=false berarti retention MENYALA. Pembalikan itu
        dikurung di sini saja supaya pemanggilnya tak perlu ikut berpikir
        terbalik. Endpoint sama dengan tombolnya di web (lihat HAR):
        POST /api/user/update body {"disable_data_retention": false}.

        Balasannya memuat nilai tersimpan, jadi yang dikembalikan adalah status
        retention sesudah perubahan (bukan sekadar status==0) supaya "berhasil"
        berarti benar-benar tersimpan, bukan cuma request diterima.
        """
        j = self.app_api("/api/user/update",
                         {"disable_data_retention": not on})
        if j.get("status") != 0:
            raise RuntimeError(f"user/update failed: {j}")
        dis = j.get("data", {}).get("cogen", {}).get("disable_data_retention")
        return None if dis is None else not dis

    def create_checkout(self, price_id, price_name, plan_price, plan_type="subscription",
                        coupon_key=None):
        """Return checkout.stripe.com URL. coupon_key='first_month:<cogen_id>' -> $0."""
        body = {
            "price_id": price_id,
            "price_name": price_name,
            "plan_price": plan_price,
            "plan_type": plan_type,
            "testmode": "false",
            "sandmode": "false",
            "current_path": "/",
            "checkout_source": "sub2",
            "fromurl": "first_month_banner",
        }
        if coupon_key:
            body["wallet_coupon_key"] = coupon_key
            body["wallet_coupon_explicit"] = True
        j = self.app_api("/api/payment/create-checkout-session-web", body)
        if j.get("status") != 0:
            raise RuntimeError(f"create-checkout failed: {j}")
        return j["data"]["url"]

    def checkout_status(self, session_id):
        """{"payment_status":"paid",...}. 500 selama checkout belum selesai -> {} """
        try:
            return self.app_api(f"/api/payment/checkout-session?checkout_session_id={session_id}")
        except urllib.error.HTTPError as e:
            if e.code in (404, 500):
                return {}  # belum selesai / belum ada di backend
            raise

    def wait_paid(self, session_id, timeout=900, interval=5, email=""):
        """Poll sampai paid. Fallback: cek plan user (webhook kadang lebih dulu)."""
        deadline = time.time() + timeout
        started = time.time()
        beat = started + 60          # kabari tiap menit: ini menunggu manusia
        while time.time() < deadline:
            st = self.checkout_status(session_id)
            if st.get("payment_status") == "paid":
                return st
            try:
                plan = self.get_user()["data"]["cogen"].get("plan")
            except Exception:
                plan = None          # jaringan goyah bukan alasan berhenti
            if plan and plan != "free":
                return {"payment_status": "paid", "plan": plan}
            if time.time() >= beat:
                beat = time.time() + 60
                log(f"  [{email}] masih nunggu kartu diisi "
                    f"({int(time.time() - started)}s dari {int(timeout)}s)")
            time.sleep(interval)
        raise RuntimeError(f"timeout {timeout}s nunggu pembayaran")

    def credit_balance(self):
        """Sisa credit. -1 kalau endpoint tak balas angka."""
        try:
            j = self.app_api("/api/payment/get_credit_balance")
            return j.get("data", {}).get("balance", -1)
        except Exception:
            return -1

    def wait_credit(self, floor=FREE_CREDIT, timeout=120, interval=5, email=""):
        """Tunggu credit langganan masuk.

        Plan naik ke plus lebih dulu daripada credit dikreditkan, jadi membaca
        saldo tepat setelah pembayaran bisa memberi 100 -- bonus akun gratis,
        bukan kuota langganan. Tunggu sampai saldo melewati batas akun gratis.
        """
        deadline = time.time() + timeout
        bal = self.credit_balance()
        while bal <= floor and time.time() < deadline:
            time.sleep(interval)
            bal = self.credit_balance()
        if bal <= floor:
            log(f"  [{email}] credit masih {bal} setelah {timeout}s; "
                "cek lagi nanti dengan: py signup.py credit")
        return bal

    def create_api_key(self, key_name):
        j = self.app_api("/api/api_tokens/create", {"key_name": key_name})
        if j.get("status") != 0:
            raise RuntimeError(f"api_tokens/create failed: {j}")
        return j["data"]["token"]  # gsk-...


def save_image(data_url, path):
    b64 = data_url.split(",", 1)[1]
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))


def show_image(path):
    try:
        os.startfile(path)  # Windows
    except Exception:
        print(f"captcha saved to {path}")


def load_accounts():
    if os.path.exists(ACCOUNTS):
        with open(ACCOUNTS, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_accounts(acc):
    with IO_LOCK:
        with open(ACCOUNTS, "w", encoding="utf-8") as f:
            json.dump(acc, f, indent=2, ensure_ascii=False)


def log(msg):
    with IO_LOCK:
        print(msg, flush=True)


def force_retention_on(c, email):
    """Paksa data retention MENYALA. Kembalikan True kalau tersimpan.

    Sengaja tidak melempar: akun yang sudah dibayar dan sudah punya API key
    jangan hangus hanya karena satu setelan gagal diubah. Kegagalan dicatat di
    log dan di accounts.json, jadi bisa diulang lewat "Cek ulang credit".
    """
    try:
        if c.set_data_retention(True) is True:
            log(f"  [{email}] data retention: dinyalakan")
            return True
        log(f"  [{email}] PERINGATAN data retention: Genspark tak "
            f"mengonfirmasi perubahan")
    except Exception as ex:
        log(f"  [{email}] PERINGATAN data retention gagal: {str(ex)[:120]}")
    return False


def with_proxy_retry(fn, email, tries=None):
    """Jalankan fn(proxy) dengan proxy dari pool; ganti proxy kalau koneksi gagal.

    Hanya error jaringan yang diulang. Jawaban server (HTTP 4xx/5xx, conflict,
    captcha salah) langsung dilempar: ganti IP tak mengubah hasilnya, dan
    mengulanginya cuma memboroskan captcha.
    """
    if tries is None:
        tries = PROXY_TRIES if len(PROXY_POOL) else 1
    last = None
    for n in range(1, tries + 1):
        proxy = PROXY_POOL.next()
        try:
            return fn(proxy)
        except Exception as ex:
            if not proxies.is_network_error(ex) or n == tries:
                raise
            last = ex
            log(f"  [{email}] proxy bermasalah ({str(ex)[:60]}), ganti proxy "
                f"[{n}/{tries}]")
    raise last


class NeedsHuman(Exception):
    """Akun belum ada -> perlu jalur signup (captcha + OTP)."""
    def __init__(self, email):
        super().__init__(f"{email} perlu signup")
        self.email = email


def phase_auth(email):
    """Paralel-safe: login akun existing. Raise NeedsHuman kalau akun baru."""
    def once(proxy):
        c = Client(proxy)
        c.start()
        if c.login(email):
            return c, c.get_user()["data"]["cogen"], "login"
        raise NeedsHuman(email)

    return with_proxy_retry(once, email)


def solve_captcha(c, email):
    """Selesaikan captcha. Pakai solver kalau dikonfigurasi, kalau tidak minta input.
    Re-fetch challenge setiap percobaan: challenge expire sambil dibaca."""
    auto = (CAPTCHA_PROVIDER or "manual").lower() != "manual"
    tries = CAPTCHA_TRIES if auto else 10**6  # manual: ulangi sampai benar
    last = None
    for n in range(1, tries + 1):
        img = c.get_captcha()
        if auto:
            try:
                ans = solvers.solve(img, CAPTCHA_PROVIDER, CAPTCHA_KEY)
            except Exception as ex:
                last = f"solver error: {ex}"
                log(f"  [{email}] {last}")
                continue
            log(f"  [{email}] captcha[{CAPTCHA_PROVIDER}] percobaan {n}: {ans}")
        else:
            if WEBUI:
                log(f"__WEBUI_ASK__ {json.dumps({'kind': 'captcha', 'email': email, 'image': img})}")
            else:
                cap = f"captcha_{email.split('@')[0][:12]}.png"
                save_image(img, cap)
                show_image(cap)
            # input(prompt) menulis prompt ke stdout tanpa newline kalau stdout
            # bukan tty (kasus WebUI: pipe) -- teksnya bisa nempel ke baris log
            # berikutnya. Marker di atas sudah membawa info yang sama, jadi
            # kosongkan prompt saat WEBUI.
            ans = input("" if WEBUI else f"[{email}] Captcha: ").strip()
            if not ans:
                raise RuntimeError("captcha kosong")
        if c.verify_captcha(ans):
            return ans
        last = "captcha salah/expired"
        log(f"  [{email}] {last}, ambil ulang...")
    raise RuntimeError(f"captcha gagal setelah {tries}x ({last})")


def get_otp(email, proxy=None):
    """Emailnator -> baca inbox otomatis. Selain itu -> ketik manual."""
    if EMAIL_SOURCE.lower() != "emailnator":
        if WEBUI:
            log(f"__WEBUI_ASK__ {json.dumps({'kind': 'otp', 'email': email})}")
        otp = input("" if WEBUI else f"[{email}] Kode OTP: ").strip()
        if not otp:
            raise RuntimeError("otp kosong")
        return otp
    log(f"  [{email}] nunggu OTP di inbox...")
    return mailer.wait_otp(mailer.Emailnator(proxy=proxy, timeout=MAIL_TIMEOUT), email, sender="genspark",
                           timeout=OTP_TIMEOUT, log=log)


CONFLICT = "ViralErrorUserCreationConflict"
COUPON_UNAVAILABLE = "wallet_coupon_unavailable"


def _signup_once(email, proxy=None):
    """Satu percobaan signup. Return (client, cogen).

    WAJIB Client baru: percobaan login yang gagal meninggalkan transaksi B2C
    dalam keadaan rusak, dan SendCode balas HTTP 500 kalau tx itu dipakai lagi.
    """
    c = Client(proxy)
    c.start()
    c.open_signup()
    ctext = solve_captcha(c, email)
    c.send_code(email)
    otp = get_otp(email, c.proxy)
    c.verify_code(email, otp)
    c.signup(email, otp, ctext)
    return c, c.get_user()["data"]["cogen"]


def interactive_signup(email, tries=EMAIL_TRIES):
    """Captcha + OTP. Return (email_terpakai, client, cogen).

    Alamat emailnator itu publik: bisa saja sudah dipakai orang lain dengan
    password berbeda, jadi login gagal DAN signup kena CONFLICT. Kalau sumbernya
    emailnator, ambil alamat lain lalu ulangi -- alamat itu memang sekali pakai.
    """
    auto_mail = EMAIL_SOURCE.lower() == "emailnator"
    proxy = PROXY_POOL.next()   # satu IP dipegang sepanjang SATU percobaan
    for n in range(1, tries + 1):
        try:
            # proxy mati -> ganti IP dan mulai transaksi baru; itu sah karena
            # tiap percobaan memang membuka sesi B2C sendiri
            c, cogen = with_proxy_retry(lambda pr: _signup_once(email, pr), email)
            return email, c, cogen
        except RuntimeError as ex:
            if CONFLICT not in str(ex) or n == tries:
                raise
            if not auto_mail:
                # akun.txt: alamat itu milik user, tak boleh ditukar. Password
                # kemungkinan beda dari PASSWORD -> minta yang benar.
                raise RuntimeError(
                    f"{email} sudah terdaftar tapi password bukan '{PASSWORD}'. "
                    "Set PASSWORD di .env sesuai akun itu, atau pakai email lain."
                ) from None
            baru = fresh_email(
                mailer.Emailnator(proxy=PROXY_POOL.next(), timeout=MAIL_TIMEOUT),
                used_inboxes())
            log(f"  [{email}] sudah dipakai -> tukar ke {baru}")
            record_emails([baru])
            email = baru


def phase_finish(email, c, cogen, accounts):
    """Paralel-safe: retention on + checkout (kalau perlu) + API key + simpan."""
    cogen_id = cogen["id"]
    # Didahulukan sebelum checkout: menunggu kartu diisi manusia bisa lama, dan
    # selama itu akun sudah aktif. Retention disetel sedini mungkin.
    retention_on = force_retention_on(c, email)
    if cogen.get("plan", "free") != "free":
        st = {"payment_status": "paid", "plan": cogen["plan"]}
        log(f"  [{email}] payment: sudah {cogen['plan']}, skip checkout")
    else:
        # Coupon first_month tak selalu berlaku (wallet_coupon_unavailable):
        # sudah pernah dipakai, atau akunnya tak memenuhi syarat. Kalau ditolak,
        # ulang tanpa coupon -- tagihan jadi harga penuh, jadi ini diberitahukan.
        try:
            url = c.create_checkout(TIER["price_id"], TIER["price_name"],
                                    TIER["plan_price"], "subscription",
                                    coupon_key=f"first_month:{cogen_id}")
        except RuntimeError as ex:
            if COUPON_UNAVAILABLE not in str(ex):
                raise
            log(f"  [{email}] coupon first_month ditolak -> lanjut TANPA diskon "
                f"(${TIER['plan_price']})")
            url = c.create_checkout(TIER["price_id"], TIER["price_name"],
                                    TIER["plan_price"], "subscription")
        session_id = re.search(r"cs_live_[A-Za-z0-9]+", url).group(0)
        # Kartu diisi manusia satu per satu: membuka lima tab serentak justru
        # menyulitkan. Tab dibuka berjarak, polling tetap jalan paralel.
        with TAB_LOCK:
            log(f"  [{email}] BUKA & ISI KARTU: {url}")
            webbrowser.open(url)
            time.sleep(TAB_DELAY)
        st = c.wait_paid(session_id, email=email)  # poll paralel
        log(f"  [{email}] payment: {st.get('payment_status')}")

    key_name = f"gk-{int(time.time() * 1000)}"
    token = c.create_api_key(key_name)
    # credit langganan menyusul setelah plan naik, jadi ditunggu
    credit = (c.wait_credit(email=email)
              if st.get("payment_status") == "paid" else c.credit_balance())
    accounts[email] = {
        "email": cogen["email"],
        "cogen_id": cogen_id,
        "api_key": token,
        "key_name": key_name,
        "payment_status": st.get("payment_status"),
        "plan": c.get_user()["data"]["cogen"].get("plan"),
        "credit": credit,
        "data_retention_on": retention_on,
        "proxy": c.proxy or "direct",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_accounts(accounts)
    log(f"  [{email}] OK credit={credit} api_key={token[:24]}...")


def main():
    emails = get_emails()
    if not emails:
        print("akun.txt kosong")
        return
    accounts = load_accounts()
    todo = [e for e in emails if e not in accounts]
    print(f"{len(emails)} akun ({len(emails) - len(todo)} sudah selesai, {len(todo)} diproses)")
    if not PASSWORD:
        print("PASSWORD kosong. Isi di .env, mis. PASSWORD=" + repr(SAMPLE_PASSWORD)[1:-1])
        print("Syarat Azure B2C: huruf besar + kecil, angka, simbol, min 8 karakter.")
        return

    prov = (CAPTCHA_PROVIDER or "manual").lower()
    if prov == "manual":
        print("captcha: manual (ketik sendiri)")
    elif not CAPTCHA_KEY:
        print(f"captcha: {prov} - CAPTCHA_KEY KOSONG, isi di .env dulu")
        return
    else:
        try:  # gagal di sini lebih baik daripada boros captcha lalu error
            print(f"captcha: {prov} (saldo {solvers.balance(prov, CAPTCHA_KEY)})")
        except Exception as ex:
            print(f"captcha: {prov} - key ditolak: {ex}")
            return
    print(f"proxy  : {len(PROXY_POOL)} proxy (round-robin, timeout {TIMEOUT}s, "
          f"ganti sampai {PROXY_TRIES}x)"
          if len(PROXY_POOL) else
          f"proxy  : tak dipakai (koneksi langsung, timeout {TIMEOUT}s)")
    print(f"         inbox timeout {MAIL_TIMEOUT}s")
    print(f"email  : {EMAIL_SOURCE.lower()}"
          + (" (OTP otomatis)" if EMAIL_SOURCE.lower() == "emailnator"
             else " (OTP diketik manual)"))
    if not todo:
        return

    # FASE 1 paralel: login akun yang sudah ada
    print()
    print(f"== fase 1: login paralel ({WORKERS} worker) ==")
    ready, needs_human, failed = [], [], {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        # as_completed: akun yang selesai dulu dilaporkan dulu. Kalau hasil
        # dipanen berurutan submit, satu akun lambat menahan laporan akun lain
        # yang sudah kelar -- kelihatan seperti macet padahal jalan.
        futs = {pool.submit(phase_auth, e): e for e in todo}
        for fut in as_completed(futs):
            email = futs[fut]
            try:
                c, cogen, how = fut.result()
                log(f"  [{email}] {how} OK (plan={cogen.get('plan')})")
                ready.append((email, c, cogen))
            except NeedsHuman:
                log(f"  [{email}] akun baru -> perlu signup")
                needs_human.append(email)
            except Exception as ex:
                log(f"  [{email}] GAGAL: {ex}")
                failed[email] = str(ex)

    # FASE 2: signup. Paralel kalau captcha+OTP dua-duanya otomatis,
    # serial kalau salah satu butuh diketik manusia.
    if needs_human:
        auto = ((CAPTCHA_PROVIDER or "manual").lower() != "manual"
                and EMAIL_SOURCE.lower() == "emailnator")
        print()
        print(f"== fase 2: signup {'paralel' if auto else 'manual'} "
              f"({len(needs_human)} akun) ==")
        if auto:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futs = {pool.submit(interactive_signup, e): e for e in needs_human}
                for fut in as_completed(futs):
                    email = futs[fut]
                    try:
                        used, c, cogen = fut.result()
                        ready.append((used, c, cogen))
                        log(f"  [{used}] signup OK")
                    except Exception as ex:
                        log(f"  [{email}] GAGAL: {ex}")
                        failed[email] = str(ex)
        else:
            for email in needs_human:
                try:
                    used, c, cogen = interactive_signup(email)
                    print(f"  [{used}] signup OK")
                    ready.append((used, c, cogen))
                except Exception as ex:
                    print(f"  [{email}] GAGAL: {ex}")
                    failed[email] = str(ex)

    # FASE 3 paralel: checkout + API key. Semua URL Stripe dibuka sekaligus,
    # isi kartu satu-satu di tab masing-masing; polling jalan bareng.
    if ready:
        print()
        print(f"== fase 3: checkout + api key paralel ({len(ready)} akun) ==")
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = {pool.submit(phase_finish, e, c, g, accounts): e
                    for e, c, g in ready}
            for fut in as_completed(futs):
                email = futs[fut]
                try:
                    fut.result()
                except Exception as ex:
                    log(f"  [{email}] GAGAL: {ex}")
                    failed[email] = str(ex)

    print()
    print("=== HASIL ===")
    for e in emails:
        v = accounts.get(e)
        if v:
            print(f"  OK    {e}: plan={v.get('plan')} credit={v.get('credit','?')} "
                  f"api_key={v.get('api_key','?')[:24]}...")
        else:
            print(f"  GAGAL {e}: {failed.get(e, 'tidak diproses')}")


def dump_one(accounts):
    """Satu putaran dump: bikin akun tempmail baru -> cek credit -> simpan
    kalau lolos ambang. Return (email, credit, plan, disimpan?).

    Tanpa checkout: tak ada kartu dan tak ada tagihan. Akun yang creditnya
    di bawah ambang tetap dilaporkan (biar distribusinya kelihatan) tapi
    tidak masuk accounts.json.
    """
    # Ambil alamat + catat SEKALIGUS di bawah satu lock: kalau tidak, dua
    # worker bisa membaca used_inboxes() yang sama lalu dapat inbox kembar
    # (Gmail mengabaikan titik) dan yang kedua kena conflict.
    with DUMP_LOCK:
        em = fresh_email(
            mailer.Emailnator(proxy=PROXY_POOL.next(), timeout=MAIL_TIMEOUT),
            used_inboxes())
        record_emails([em])      # catat biar tak dipakai ulang lain kali
    log(f"  [{em}] bikin akun...")
    used, c, cogen = interactive_signup(em)

    # Plan dan credit MENYUSUL beberapa saat setelah signup: membaca saldo
    # langsung memberi 100 (bonus akun gratis), bukan kuota sebenarnya.
    # Tunggu, tapi jangan lama-lama -- akun yang memang cuma dapat bonus
    # gratis tak akan pernah naik, dan menunggu 120s per akun menghambat
    # seluruh antrean dump.
    credit = c.wait_credit(floor=min(FREE_CREDIT, DUMP_MIN_CREDIT - 1),
                           timeout=DUMP_CREDIT_WAIT, interval=3, email=used)
    plan = c.get_user()["data"]["cogen"].get("plan", "free")
    log(f"  [{used}] plan={plan} credit={credit} (ambang {DUMP_MIN_CREDIT})")
    if credit < DUMP_MIN_CREDIT:
        return used, credit, plan, False

    # Setelah lolos ambang, bukan sebelum: akun di bawah ambang dibuang dan
    # tak pernah dipakai, jadi memaksa setelannya cuma memboroskan request.
    retention_on = force_retention_on(c, used)

    key_name = f"gk-{int(time.time() * 1000)}"
    token = c.create_api_key(key_name)
    rec = {
        "email": cogen["email"],
        "cogen_id": cogen["id"],
        "api_key": token,
        "key_name": key_name,
        "payment_status": "none",   # dump: tak lewat checkout
        "plan": plan,
        "credit": credit,
        "data_retention_on": retention_on,
        "proxy": c.proxy or "direct",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # accounts dict dibagi antar worker -> ubah + tulis di bawah lock, biar
    # tak ada akun yang hilang karena dua worker menyimpan bersamaan
    with DUMP_LOCK:
        accounts[used] = rec
        save_accounts(accounts)
    log(f"  [{used}] DISIMPAN credit={credit} api_key={token[:24]}...")
    return used, credit, plan, True


def dump_mode():
    """py signup.py dump -> bikin akun tempmail sampai dapat DUMP_TARGET akun
    dengan credit >= DUMP_MIN_CREDIT, maksimal DUMP_MAX_TRIES percobaan."""
    if not PASSWORD:
        print("PASSWORD kosong. Isi di .env, mis. PASSWORD="
              + repr(SAMPLE_PASSWORD)[1:-1])
        return
    if EMAIL_SOURCE.lower() != "emailnator":
        print("mode dump butuh EMAIL_SOURCE=emailnator (alamat dibuat otomatis).")
        return

    prov = (CAPTCHA_PROVIDER or "manual").lower()
    # captcha manual = jawaban diketik manusia satu-satu; dua prompt serentak
    # tak bisa dijawab, jadi paralel dimatikan.
    workers = 1 if prov == "manual" else min(DUMP_WORKERS, DUMP_MAX_TRIES)
    print(f"== dump: cari {DUMP_TARGET} akun dengan credit >= {DUMP_MIN_CREDIT} "
          f"(maks {DUMP_MAX_TRIES} percobaan) ==")
    print(f"captcha: {prov}"
          + ("" if prov == "manual" else f" (saldo dicek saat dipakai)"))
    print(f"paralel: {workers} akun serentak"
          + (" (captcha manual -> dipaksa serial)" if prov == "manual"
             and DUMP_WORKERS > 1 else ""))
    print("tanpa checkout Stripe: tak ada kartu, tak ada tagihan.")
    print()

    accounts = load_accounts()
    saved, rows = [], []
    started = done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {}
        # isi kolam sampai penuh, lalu tiap satu selesai -> jalankan satu lagi
        # HANYA kalau target belum tercapai, jadi captcha tak diboroskan.
        # Yang sudah berjalan tetap dipanen sampai habis: akun yang sudah
        # jadi (dan sudah masuk accounts.json) harus tetap dilaporkan.
        # Jangan pernah menjalankan lebih dari sisa kebutuhan: kalau target
        # sisa 1, memulai 3 worker berarti 2 captcha terbuang. Yang sedang
        # berjalan ikut dihitung sebagai "calon berhasil".
        def room():
            butuh = DUMP_TARGET - len(saved) - len(futs)
            return max(min(butuh, DUMP_MAX_TRIES - started), 0)

        while len(futs) < workers and room() > 0:
            started += 1
            futs[pool.submit(dump_one, accounts)] = started
        while futs:
            fut = next(as_completed(list(futs)))
            n = futs.pop(fut)
            done += 1
            try:
                email, credit, plan, keep = fut.result()
                rows.append((email, credit, plan, keep))
                if keep:
                    saved.append(email)
            except Exception as ex:
                log(f"  [percobaan {n}] GAGAL: {ex}")
                rows.append(("?", -1, "?", False))
            log(f"-- selesai {done} (terkumpul {len(saved)}/{DUMP_TARGET}, "
                f"dimulai {started}/{DUMP_MAX_TRIES}) --")
            # isi ulang hanya sebanyak yang masih benar-benar dibutuhkan
            while len(futs) < workers and room() > 0:
                started += 1
                futs[pool.submit(dump_one, accounts)] = started

    print()
    print("=== HASIL DUMP ===")
    for email, credit, plan, keep in rows:
        tag = "SIMPAN" if keep else "buang "
        print(f"  {tag} {email}: plan={plan} credit={credit}")
    print()
    print(f"tersimpan {len(saved)} akun (ambang credit {DUMP_MIN_CREDIT}), "
          f"{len(rows)} percobaan")
    if not saved and rows:
        best = max((c for _, c, _, _ in rows), default=-1)
        print(f"credit tertinggi yang didapat: {best}. Kalau semua akun baru "
              f"memang di bawah {DUMP_MIN_CREDIT}, turunkan DUMP_MIN_CREDIT "
              "di .env.")


def check_credits(only=None):
    """py signup.py credit [email ...] -> login tiap akun di accounts.json,
    cetak sisa credit, dan simpan hasilnya kembali.

    only = daftar email yang mau disegarkan saja. Kosong/None -> semua.
    Akun yang tak disegarkan TIDAK disentuh, jadi nilainya tak hilang.
    """
    accounts = load_accounts()
    if not accounts:
        print("accounts.json kosong")
        return
    if only:
        target = {e: r for e, r in accounts.items() if e in set(only)}
        tak_ada = [e for e in only if e not in accounts]
        for e in tak_ada:
            print(f"  ? {e}: tak ada di accounts.json, dilewati")
        if not target:
            print("tak ada akun yang cocok untuk disegarkan")
            return
    else:
        target = accounts

    def one(email, rec):
        # Jalur cepat: API key cukup untuk membaca credit (satu request, tanpa
        # B2C). Dipakai hanya kalau retention akun ini SUDAH menyala -- menyetel
        # retention wajib punya sesi login, jadi akun yang belum beres tetap
        # lewat jalur panjang supaya tombol ini tetap bisa memperbaikinya.
        if rec.get("api_key") and rec.get("data_retention_on") is True:
            try:
                info = credit_by_key(rec["api_key"], proxy=PROXY_POOL.next())
                return email, {
                    "plan": info.get("plan"),
                    "credit": info.get("credit_balance"),
                    "data_retention_on": True,
                    "status": None,          # /me tak melaporkan ini
                    "period_end": None,
                    "via": "api key",
                }, None
            except RuntimeError:
                pass                          # key ditolak -> jatuh ke login

        c = Client()
        c.start()
        if not c.login(email, rec.get("password", PASSWORD)):
            return email, None, "login gagal"
        u = c.get_user()["data"]["cogen"]
        # Sekalian paksa retention menyala: sesinya sudah terbuka di sini, jadi
        # tak perlu login kedua kali. Yang sudah menyala dilewati -- akun lama
        # yang sudah beres tak perlu request ulang tiap kali cek credit.
        #
        # Nilainya dibaca terbalik: disable_data_retention=false berarti
        # retention MENYALA. `is False` dipakai, bukan `not ...`, karena akun
        # yang belum pernah disetel bernilai null -- dan null harus tetap
        # disetel, bukan dianggap sudah beres.
        if u.get("disable_data_retention") is False:
            retention_on = True
        else:
            retention_on = force_retention_on(c, email)
        return email, {
            "plan": u.get("plan"),
            "credit": c.credit_balance(),
            "data_retention_on": retention_on,
            "status": u.get("personal_membership_ext", {}).get("status"),
            "period_end": u.get("personal_membership_ext", {}).get("current_period_end"),
            "via": "login",
        }, None

    print(f"cek {len(target)} akun...")
    print()
    rows, total, fresh = [], 0, {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        # future -> email: kalau one() melempar, hasilnya tak memuat email dan
        # akun yang gagal jadi tak bisa ditelusuri ("? : timeout"). Petanya
        # dibuat di sini supaya nama akunnya tetap terlaporkan.
        futs = {pool.submit(one, e, r): e for e, r in target.items()}
        for fut in as_completed(futs):
            try:
                email, info, err = fut.result()
            except Exception as ex:
                rows.append((futs[fut], None, f"{type(ex).__name__}: {ex}"[:80]))
                continue
            rows.append((email, info, err))
            if info:
                # /api/tool_cli/me memberi float (mis. 888.6), app web memberi
                # int. Dibulatkan ke bawah supaya sisa credit tak pernah
                # dilaporkan lebih besar dari yang benar-benar ada.
                info["credit"] = int(info.get("credit") or 0)
                total += max(info["credit"], 0)
                fresh[email] = info

    # Muat ulang dari disk sebelum menulis: WebUI bisa menghapus akun sambil
    # refresh berjalan, dan menyimpan salinan lama akan menghidupkannya lagi.
    # Yang ditimpa hanya akun yang benar-benar disegarkan.
    latest = load_accounts()
    for email, info in fresh.items():
        if email in latest:
            latest[email]["credit"] = info["credit"]
            latest[email]["plan"] = info["plan"]
            latest[email]["data_retention_on"] = info["data_retention_on"]
            latest[email]["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_accounts(latest)

    # Yang gagal dikelompokkan di atas: kalau berbaur di antara 56 baris sukses,
    # satu akun yang gagal mudah terlewat -- dan itulah yang membuat total di
    # sini tampak tak cocok dengan WebUI.
    for email, info, err in sorted(rows, key=lambda r: (r[1] is not None, r[0])):
        if err:
            print(f"  GAGAL {email}: {err} (credit lama dipertahankan)")
        else:
            print(f"  {email}")
            # Jalur API key tak melaporkan status langganan; mencetak
            # "status=None sampai=None" bikin akun sehat tampak rusak, jadi
            # kolom itu dilewati saja kalau memang tak ada datanya.
            bagian = [f"plan={info['plan']}", f"credit={info['credit']}"]
            if info.get("status") or info.get("period_end"):
                bagian.append(f"status={info['status']}")
                bagian.append(f"sampai={info['period_end']}")
            bagian.append("retention="
                          + ("nyala" if info["data_retention_on"]
                             else "MASIH MATI"))
            bagian.append(f"via={info.get('via') or 'login'}")
            print("        " + " ".join(bagian))
    print()
    ret_mati = [e for e, i, x in rows if i and not i["data_retention_on"]]
    if ret_mati:
        print(f"retention masih mati di {len(ret_mati)} akun; jalankan ulang "
              "cek credit untuk mencoba lagi")

    # Total di sini hanya menjumlahkan akun yang BERHASIL disegarkan, sedangkan
    # WebUI menjumlahkan semua akun di accounts.json. Dulu keduanya dicetak
    # tanpa penjelasan, jadi selisihnya terlihat seperti salah hitung. Kalau ada
    # yang gagal, sebutkan sisanya sekalian supaya angkanya bisa dicocokkan.
    tak_kena = [e for e, i, x in rows if not i]
    lewat_key = sum(1 for v in fresh.values() if v.get("via") == "api key")
    print(f"berhasil dicek: {len(fresh)} akun, total credit {total}")
    if lewat_key:
        print(f"  di antaranya  : {lewat_key} lewat API key (tanpa login), "
              f"{len(fresh) - lewat_key} lewat login")
    if tak_kena:
        print(f"gagal dicek   : {len(tak_kena)} akun (lihat GAGAL di atas); "
              "credit lamanya tidak ikut dijumlahkan di baris atas")
    # Dihitung dari accounts.json, bukan total+sisa: kalau yang disegarkan cuma
    # sebagian (refresh baris terpilih), akun di luar pilihan tetap ikut
    # dijumlahkan WebUI dan takkan pernah muncul di rows.
    semua = sum(int(v.get("credit") or 0) for v in latest.values())
    if semua != total:
        print(f"total semua   : {semua} dari {len(latest)} akun tersimpan "
              "<- angka inilah yang tampil di WebUI")


def load_keys(path):
    """Baca API key dari file, satu per baris. Kosong dan '#' dilewati.

    Duplikat dibuang: satu key dua kali berarti satu akun dihitung dua kali di
    total, dan itu kesalahan yang sulit terlihat pada daftar panjang.
    """
    keys, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or ln in seen:
                continue
            seen.add(ln)
            keys.append(ln)
    return keys


def check_keys(args):
    """py signup.py key <gsk-...|file> [...] [--save hasil.csv]

    Cek credit hanya dari API key -- tanpa login, password, atau captcha.
    Argumen boleh key langsung atau nama file berisi key.
    """
    save = None
    if "--save" in args:
        i = args.index("--save")
        if i + 1 >= len(args):
            print("--save butuh nama file CSV")
            return
        save = args[i + 1]
        args = args[:i] + args[i + 2:]

    # Argumen yang bukan key dianggap nama file. Key selalu diawali "gsk-",
    # jadi keduanya bisa dibedakan tanpa menyentuh disk untuk tiap argumen.
    keys, seen = [], set()
    for a in args:
        masuk = [a] if a.startswith("gsk-") else None
        if masuk is None:
            try:
                masuk = load_keys(a)
            except OSError as ex:
                print(f"tak bisa baca {a}: {ex}")
                return
        for k in masuk:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    if not keys:
        print("tak ada API key untuk dicek")
        return

    accounts = load_accounts()
    # cogen_id -> email, untuk mengenali key lepas tanpa memanggil Genspark
    milik = {v.get("cogen_id"): e for e, v in accounts.items() if v.get("cogen_id")}

    def one(i, k):
        punya = milik.get(key_cogen_id(k))
        try:
            info = credit_by_key(k, proxy=PROXY_POOL.next())
        except Exception as ex:
            return i, punya, None, f"{ex}"[:80]
        return i, punya, info, None

    print(f"cek {len(keys)} key unik...")
    print()
    rows = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = [pool.submit(one, i, k) for i, k in enumerate(keys)]
        for fut in as_completed(futs):
            i, punya, info, err = fut.result()
            rows[i] = (punya, info, err)

    total = 0.0
    n = {"HIDUP": 0, "HABIS": 0, "INVALID": 0}
    hasil = []
    for i in range(len(keys)):
        punya, info, err = rows[i]
        if err:
            # Key ditolak/dicabut: bedakan dari credit habis. Akun habis masih
            # bisa diisi, key invalid sudah tak ada gunanya.
            st, email, plan, credit = "INVALID", punya or "?", "?", ""
        else:
            credit = float(info.get("credit_balance") or 0)
            st = "HIDUP" if credit > 0 else "HABIS"
            total += credit if credit > 0 else 0
            email = punya or info.get("email") or "?"
            plan = info.get("plan") or "?"
        n[st] += 1
        hasil.append({"idx": i + 1, "status": st, "email": email, "plan": plan,
                      "credit": credit, "error": err or ""})
        ket = err or f"plan={plan} credit={credit}"
        # kuncinya sendiri tak pernah dicetak
        print(f"  {i + 1:3d} {st:7s} {email:40s} {ket}")

    print()
    print(f"HIDUP {n['HIDUP']} | HABIS {n['HABIS']} | INVALID {n['INVALID']} "
          f"| total credit {total:,.1f}")

    if save:
        # newline="" wajib di Windows, kalau tidak tiap baris CSV dobel.
        # csv.writer mengutip sendiri, jadi koma di pesan error aman.
        with open(save, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["idx", "status", "email", "plan",
                                              "credit", "error"])
            w.writeheader()
            w.writerows(hasil)
        print(f"hasil tersimpan: {save}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg in ("key", "keys", "apikey"):
        if len(sys.argv) < 3:
            print("pakai: py signup.py key <gsk-...|file> [...] [--save hasil.csv]")
            print("       file = daftar key, satu per baris ('#' = komentar)")
        else:
            check_keys(sys.argv[2:])
    elif arg in ("credit", "credits", "saldo"):
        # argumen sisanya = email tertentu saja; kosong -> semua akun
        check_credits(sys.argv[2:] or None)
    elif arg == "dump":
        dump_mode()
    else:
        main()
