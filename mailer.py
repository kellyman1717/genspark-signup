#!/usr/bin/env python3
"""Emailnator temp-mail: generate alamat + baca OTP otomatis.

Emailnator ditulis ulang jadi aplikasi Next.js (2026); API lama (XSRF-TOKEN +
`/generate-email` dengan body `{"email":["dotGmail"]}`) sudah tak dipakai.
API baru, tanpa cookie XSRF:

  POST /api/generate-email  {"ids":[3]}                     -> {"status":"success","email":"x@gmail.com",...}
  POST /api/message-list     {"email":"x@gmail.com","limit":20} -> {"status":"success","messages":[{id,from,subject,timestamp,time_ago}]}
  GET  /api/message/<id>                                    -> {"content":"<html>...","from":...,"subject":...}

Tipe alamat berupa id numerik: domain=1, plusGmail=2, dotGmail=3, googleMail=8.
"""
import json, re, time, urllib.parse, urllib.request, urllib.error
import http.cookiejar

import proxies

HOST = "https://www.emailnator.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# Jenis alamat: dotGmail (titik acak, inbox Gmail asli) | plusGmail | googleMail | domain
ETYPE = {"domain": 1, "plusGmail": 2, "dotGmail": 3, "googleMail": 8}
KIND = "dotGmail"
ADS = {"ADSVPN"}  # id iklan bawaan emailnator, bukan email nyata
HEARTBEAT = 30    # detik: seberapa sering kabari kalau OTP belum datang


class Emailnator:
    def __init__(self, kind=KIND, proxy=None, timeout=30):
        self.kind = kind
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        # API baru tak butuh cookie/CSRF, tapi cookie tetap dipertahankan
        # seandainya cloudflare ikut menyetel sesuatu. Satu proxy dipegang
        # sepanjang hidup objek supaya sesinya konsisten.
        self.proxy = proxy
        self.opener = proxies.build_opener(
            proxy, urllib.request.HTTPCookieProcessor(self.jar))

    def _json_body(self, payload):
        return json.dumps(payload).encode()

    def _post(self, path, payload):
        r = urllib.request.Request(
            HOST + path, data=self._json_body(payload),
            headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": HOST,
                "Referer": HOST + "/",
            })
        with self.opener.open(r, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _get(self, path):
        r = urllib.request.Request(
            HOST + path,
            headers={"User-Agent": UA, "Accept": "application/json"})
        with self.opener.open(r, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def new_email(self):
        kind_id = ETYPE.get(self.kind)
        payload = {"ids": [kind_id]} if kind_id is not None else {"email": [self.kind]}
        j = self._post("/api/generate-email", payload)
        got = (j or {}).get("email")
        if not got:
            raise RuntimeError(f"generate-email kosong: {j}")
        return got

    def messages(self, email):
        j = self._post("/api/message-list", {"email": email, "limit": 20})
        out = []
        for m in (j or {}).get("messages", []):
            mid = m.get("id")
            if mid in ADS:
                continue
            # wait_otp() dan sisanya memakai kunci lama: messageID/from/subject
            out.append({"messageID": mid,
                        "from": m.get("from", ""),
                        "subject": m.get("subject", "")})
        return out

    def body(self, email, message_id):
        bare = message_id.split("?", 1)[0]
        j = self._get("/api/message/" + urllib.parse.quote(bare, safe=""))
        if isinstance(j, str):
            return j
        content = (j or {}).get("content")
        if content is not None:
            return content
        # tak ada "content" -> tampilkan apa adanya supaya OTP tetap bisa dibaca
        plain = " ".join(str(v) for k, v in (j or {}).items()
                         if k in ("from", "subject"))
        return plain + " " + json.dumps(j)


def extract_otp(text, digits=6):
    """Ambil kode OTP dari isi email. Prioritas: dekat kata 'code', lalu angka polos."""
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"&[a-z#0-9]+;", " ", plain)
    near = re.search(
        r"(?:code|kode|verification|verify|otp|password)\D{0,40}?(\d{%d})" % digits,
        plain, re.I)
    if near:
        return near.group(1)
    hits = re.findall(r"\b(\d{%d})\b" % digits, plain)
    return hits[0] if hits else None


def _warn(log, warned, email, ex):
    """Laporkan satu jenis error sekali saja per akun: emailnator sering gagal
    dan mencetak tiap kegagalan akan menutupi keterangan akun lain."""
    key = type(ex).__name__ + str(ex)[:40]
    if key not in warned:
        warned.add(key)
        log(f"    [{email}] inbox: {ex} (dicoba ulang, diam-diam)")


def wait_otp(mail, email, sender=None, timeout=180, interval=5, digits=6, log=print):
    """Poll inbox sampai OTP ketemu. sender = filter substring pada from/subject.

    Kegagalan baca inbox itu wajar (emailnator lambat dan kadang menolak), jadi
    ditelan lalu dicoba lagi. Tapi hanya dilaporkan sekali per jenis error:
    tanpa itu satu inbox lambat bisa membanjiri log puluhan baris identik dan
    menutupi keterangan akun lain yang jalan bersamaan.
    """
    deadline = time.time() + timeout
    skip, warned = set(), set()      # skip = pesan yang jelas bukan OTP
    errors = 0
    started = time.time()
    beat = started + HEARTBEAT       # kabari kalau lama, biar tak tampak macet
    while time.time() < deadline:
        if time.time() >= beat:
            beat = time.time() + HEARTBEAT
            log(f"    [{email}] masih nunggu OTP "
                f"({int(time.time() - started)}s dari {int(timeout)}s"
                + (f", {errors} gagal baca" if errors else "") + ")")
        try:
            msgs = mail.messages(email)
        except Exception as ex:
            errors += 1
            _warn(log, warned, email, ex)
            msgs = []
        for m in msgs:
            mid = m.get("messageID")
            if mid in skip:
                continue
            blob = f"{m.get('from','')} {m.get('subject','')}"
            if sender and sender.lower() not in blob.lower():
                skip.add(mid)        # bukan dari pengirim yang dicari, abaikan
                continue
            # body() bisa gagal atau isinya belum lengkap; JANGAN tandai skip,
            # supaya pesan yang benar dicoba lagi pada putaran berikutnya.
            try:
                body = mail.body(email, mid)
            except Exception as ex:
                errors += 1
                _warn(log, warned, email, ex)
                continue
            code = extract_otp(body, digits)
            if code:
                log(f"    [{email}] OTP {code} dari {m.get('from','?')[:40]}")
                return code
        time.sleep(interval)
    raise RuntimeError(
        f"OTP tak ketemu dalam {timeout}s ({errors} kegagalan baca inbox)")


def demo():
    """Self-check parsing OTP — tanpa jaringan."""
    assert extract_otp("<p>Your code is <b>123456</b></p>") == "123456"
    assert extract_otp("Verification code: 981946 expires") == "981946"
    assert extract_otp("Kode OTP kamu 767330") == "767330"
    # angka panjang lain jangan bocor jadi OTP
    assert extract_otp("Order 9876543210 total 55") is None
    # kode dekat kata kunci menang atas angka lain yang muncul lebih dulu
    assert extract_otp("Ref 111111 ... your verification code is 222222") == "222222"
    assert extract_otp("no digits here") is None
    assert "ADSVPN" in ADS

    # body() gagal sekali lalu berhasil -> tetap dapat OTP, jangan gagalkan akun
    class Recovers:
        def __init__(self):
            self.n = 0

        def messages(self, e):
            return [{"messageID": "m1", "from": "Microsoft on behalf of Genspark"}]

        def body(self, e, m):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("The read operation timed out")
            return "code 424242"

    r = Recovers()
    assert wait_otp(r, "x@g.com", sender="genspark", timeout=5, interval=0,
                    log=lambda *a: None) == "424242"
    assert r.n == 2, r.n

    # isi belum lengkap -> pesan dibaca ulang, bukan dibuang selamanya
    class Slow:
        def __init__(self):
            self.n = 0

        def messages(self, e):
            return [{"messageID": "m1", "from": "Microsoft on behalf of Genspark"}]

        def body(self, e, m):
            self.n += 1
            return "memuat..." if self.n == 1 else "code 999888"

    assert wait_otp(Slow(), "x@g.com", sender="genspark", timeout=5, interval=0,
                    log=lambda *a: None) == "999888"

    # pesan yang tak cocok sender hanya diperiksa sekali, tak diunduh berulang
    class Noise:
        def __init__(self):
            self.body_calls = 0

        def messages(self, e):
            return [{"messageID": "spam", "from": "SHEIN"}]

        def body(self, e, m):
            self.body_calls += 1
            return "diskon"

    n = Noise()
    try:
        wait_otp(n, "x@g.com", sender="genspark", timeout=0.3, interval=0.1,
                 log=lambda *a: None)
        raise AssertionError("harus timeout")
    except RuntimeError:
        pass
    assert n.body_calls == 0, n.body_calls

    # heartbeat: menunggu lama harus tetap mengabari, jangan diam total
    global HEARTBEAT
    orig, HEARTBEAT = HEARTBEAT, 0.1
    try:
        beats = []
        try:
            wait_otp(Noise(), "x@g.com", sender="genspark", timeout=0.5,
                     interval=0.1, log=beats.append)
            raise AssertionError("harus timeout")
        except RuntimeError:
            pass
        assert any("masih nunggu OTP" in b for b in beats), beats
    finally:
        HEARTBEAT = orig
    print("PASS: extract_otp + wait_otp tahan gagal baca body")


if __name__ == "__main__":
    demo()
