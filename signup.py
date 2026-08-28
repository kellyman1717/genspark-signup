#!/usr/bin/env python3
"""Genspark.ai auto-signup per email from akun.txt (Azure AD B2C flow)."""
import json, re, sys, time, urllib.parse, urllib.request, urllib.error, base64, os, webbrowser
import threading
from concurrent.futures import ThreadPoolExecutor

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
        "EMAIL_SOURCE", "EMAIL_COUNT", "OTP_TIMEOUT", "WORKERS", "PASSWORD")})
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
PASSWORD = ENV.get("PASSWORD", "Masuk@123456")  # password semua akun
IO_LOCK = threading.Lock()  # accounts.json + stdout

# Tier dari HAR /api/payment/sub2/tier_config. Ganti TIER_ID untuk paket lain.
TIER_ID = "plus1"
TIER = {
    "price_id": "price_1T7YeoHy7UpDvrVidVyuxxNm",
    "price_name": "ai.genspark.vip.plus.c1.month",
    "plan_price": "24.99",
}


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
        m = mailer.Emailnator(proxy=PROXY_POOL.next())
        fresh = [m.new_email() for _ in range(EMAIL_COUNT)]
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
            return self.opener.open(r, timeout=30)
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
        return json.load(self.opener.open(r, timeout=30))

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

    def wait_paid(self, session_id, timeout=900, interval=5):
        """Poll sampai paid. Fallback: cek plan user (webhook kadang lebih dulu)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.checkout_status(session_id)
            if st.get("payment_status") == "paid":
                return st
            plan = self.get_user()["data"]["cogen"].get("plan")
            if plan and plan != "free":
                return {"payment_status": "paid", "plan": plan}
            time.sleep(interval)
        raise RuntimeError(f"timeout {timeout}s nunggu pembayaran")

    def credit_balance(self):
        """Sisa credit. -1 kalau endpoint tak balas angka."""
        try:
            j = self.app_api("/api/payment/get_credit_balance")
            return j.get("data", {}).get("balance", -1)
        except Exception:
            return -1

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


class NeedsHuman(Exception):
    """Akun belum ada -> perlu jalur signup (captcha + OTP)."""
    def __init__(self, email):
        super().__init__(f"{email} perlu signup")
        self.email = email


def phase_auth(email):
    """Paralel-safe: login akun existing. Raise NeedsHuman kalau akun baru."""
    c = Client()
    c.start()
    if c.login(email):
        return c, c.get_user()["data"]["cogen"], "login"
    raise NeedsHuman(email)


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
            cap = f"captcha_{email.split('@')[0][:12]}.png"
            save_image(img, cap)
            show_image(cap)
            ans = input(f"[{email}] Captcha: ").strip()
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
        otp = input(f"[{email}] Kode OTP: ").strip()
        if not otp:
            raise RuntimeError("otp kosong")
        return otp
    log(f"  [{email}] nunggu OTP di inbox...")
    return mailer.wait_otp(mailer.Emailnator(proxy=proxy), email, sender="genspark",
                           timeout=OTP_TIMEOUT, log=log)


CONFLICT = "ViralErrorUserCreationConflict"


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
    proxy = PROXY_POOL.next()   # satu IP untuk seluruh percobaan akun ini
    for n in range(1, tries + 1):
        try:
            c, cogen = _signup_once(email, proxy)
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
            baru = mailer.Emailnator(proxy=proxy).new_email()
            log(f"  [{email}] sudah dipakai -> tukar ke {baru}")
            record_emails([baru])
            email = baru


def phase_finish(email, c, cogen, accounts):
    """Paralel-safe: checkout (kalau perlu) + API key + simpan."""
    cogen_id = cogen["id"]
    if cogen.get("plan", "free") != "free":
        st = {"payment_status": "paid", "plan": cogen["plan"]}
        log(f"  [{email}] payment: sudah {cogen['plan']}, skip checkout")
    else:
        url = c.create_checkout(TIER["price_id"], TIER["price_name"],
                                TIER["plan_price"], "subscription",
                                coupon_key=f"first_month:{cogen_id}")
        session_id = re.search(r"cs_live_[A-Za-z0-9]+", url).group(0)
        log(f"  [{email}] BUKA & ISI KARTU: {url}")
        webbrowser.open(url)
        st = c.wait_paid(session_id)  # poll paralel, tak blokir akun lain
        log(f"  [{email}] payment: {st.get('payment_status')}")

    key_name = f"gk-{int(time.time() * 1000)}"
    token = c.create_api_key(key_name)
    credit = c.credit_balance()
    accounts[email] = {
        "email": cogen["email"],
        "cogen_id": cogen_id,
        "api_key": token,
        "key_name": key_name,
        "payment_status": st.get("payment_status"),
        "plan": c.get_user()["data"]["cogen"].get("plan"),
        "credit": credit,
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
    print(f"proxy  : {len(PROXY_POOL)} proxy (round-robin)"
          if len(PROXY_POOL) else "proxy  : tak dipakai (koneksi langsung)")
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
        for email, fut in [(e, pool.submit(phase_auth, e)) for e in todo]:
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
                for email, fut in [(e, pool.submit(interactive_signup, e))
                                   for e in needs_human]:
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
            for email, fut in [(e, pool.submit(phase_finish, e, c, g, accounts))
                               for e, c, g in ready]:
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


def check_credits():
    """py signup.py credit -> login tiap akun di accounts.json, cetak sisa credit."""
    accounts = load_accounts()
    if not accounts:
        print("accounts.json kosong")
        return

    def one(email, rec):
        c = Client()
        c.start()
        if not c.login(email, rec.get("password", PASSWORD)):
            return email, None, "login gagal"
        u = c.get_user()["data"]["cogen"]
        return email, {
            "plan": u.get("plan"),
            "credit": c.credit_balance(),
            "status": u.get("personal_membership_ext", {}).get("status"),
            "period_end": u.get("personal_membership_ext", {}).get("current_period_end"),
        }, None

    print(f"cek {len(accounts)} akun...")
    print()
    rows, total = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for fut in [pool.submit(one, e, r) for e, r in accounts.items()]:
            try:
                email, info, err = fut.result()
            except Exception as ex:
                rows.append(("?", None, str(ex)[:60]))
                continue
            rows.append((email, info, err))
            if info:
                total += max(info["credit"], 0)
                accounts[email]["credit"] = info["credit"]
                accounts[email]["plan"] = info["plan"]
    save_accounts(accounts)

    for email, info, err in sorted(rows, key=lambda r: r[0]):
        if err:
            print(f"  GAGAL {email}: {err}")
        else:
            print(f"  {email}")
            print(f"        plan={info['plan']} credit={info['credit']} "
                  f"status={info['status']} sampai={info['period_end']}")
    print()
    print(f"total credit: {total}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("credit", "credits", "saldo"):
        check_credits()
    else:
        main()
