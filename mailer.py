#!/usr/bin/env python3
"""Emailnator temp-mail: generate alamat + baca OTP otomatis.

API (dari HAR www.emailnator.com.har):
  GET  /                -> set cookie XSRF-TOKEN + gmailnator_session
  POST /generate-email  {"email":["dotGmail"]}          -> {"email":["x@gmail.com"]}
  POST /message-list    {"email":"x@gmail.com"}         -> {"messageData":[{messageID,from,subject,time}]}
  POST /message-list    {"email":"...","messageID":"."} -> isi email (HTML)

Token XSRF dikirim sebagai header X-XSRF-TOKEN (URL-decoded dari cookie).
"""
import json, re, time, urllib.parse, urllib.request, urllib.error
import http.cookiejar

import proxies

HOST = "https://www.emailnator.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# Jenis alamat: dotGmail (titik acak, inbox Gmail asli) | plusGmail | googleMail | domain
KIND = "dotGmail"
ADS = {"ADSVPN"}  # messageID iklan bawaan emailnator, bukan email nyata


class Emailnator:
    def __init__(self, kind=KIND, proxy=None, timeout=30):
        self.kind = kind
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        # token XSRF diikat ke sesi, jadi satu proxy dipegang sepanjang hidup objek
        self.proxy = proxy
        self.opener = proxies.build_opener(
            proxy, urllib.request.HTTPCookieProcessor(self.jar))
        self.token = None

    def _bootstrap(self):
        r = urllib.request.Request(HOST + "/", headers={"User-Agent": UA})
        with self.opener.open(r, timeout=self.timeout) as resp:
            resp.read()
        self.token = next((urllib.parse.unquote(c.value)
                           for c in self.jar if c.name == "XSRF-TOKEN"), None)
        if not self.token:
            raise RuntimeError("XSRF-TOKEN tak ada; emailnator berubah?")

    def _post(self, path, payload):
        if not self.token:
            self._bootstrap()
        r = urllib.request.Request(
            HOST + path, data=json.dumps(payload).encode(),
            headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-XSRF-TOKEN": self.token,
                "X-Requested-With": "XMLHttpRequest",
                "Origin": HOST,
                "Referer": HOST + "/",
            })
        with self.opener.open(r, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw  # /message-list dengan messageID balas HTML

    def new_email(self):
        j = self._post("/generate-email", {"email": [self.kind]})
        got = (j or {}).get("email") or []
        if not got:
            raise RuntimeError(f"generate-email kosong: {j}")
        return got[0]

    def messages(self, email):
        j = self._post("/message-list", {"email": email})
        return [m for m in (j or {}).get("messageData", [])
                if m.get("messageID") not in ADS]

    def body(self, email, message_id):
        r = self._post("/message-list", {"email": email, "messageID": message_id})
        return r if isinstance(r, str) else json.dumps(r)


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


def wait_otp(mail, email, sender=None, timeout=180, interval=5, digits=6, log=print):
    """Poll inbox sampai OTP ketemu. sender = filter substring pada from/subject."""
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        try:
            msgs = mail.messages(email)
        except Exception as ex:
            log(f"    inbox error: {ex}")
            msgs = []
        for m in msgs:
            mid = m.get("messageID")
            if mid in seen:
                continue
            seen.add(mid)
            blob = f"{m.get('from','')} {m.get('subject','')}"
            if sender and sender.lower() not in blob.lower():
                continue
            code = extract_otp(mail.body(email, mid), digits)
            if code:
                log(f"    OTP {code} dari: {m.get('from','?')[:50]}")
                return code
        time.sleep(interval)
    raise RuntimeError(f"OTP tak ketemu dalam {timeout}s")


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
    print("PASS: extract_otp OK")


if __name__ == "__main__":
    demo()
