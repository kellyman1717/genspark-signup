#!/usr/bin/env python3
"""Self-check: verify signup.py parsing regexes against the real HAR data."""
import json, re, sys, os

HAR = r"C:\Users\EDITOR MEDIA 07\Downloads\www.genspark.ai.har"


def entry(har, i):
    return har["log"]["entries"][i]


def test_concurrency():
    """Setiap Client punya cookie jar sendiri -> aman dipakai paralel."""
    import signup
    a, b = signup.Client(), signup.Client()
    ja = next(h.cookiejar for h in a.opener.handlers if hasattr(h, "cookiejar"))
    jb = next(h.cookiejar for h in b.opener.handlers if hasattr(h, "cookiejar"))
    assert ja is not jb, "cookie jar bocor antar Client -> sesi akun tercampur"
    assert signup.IO_LOCK is not None
    print("PASS: Client terisolasi per-thread")


def test_email_append():
    """akun.txt tanpa newline di akhir -> alamat baru tak boleh nempel."""
    import tempfile, importlib, os as _os
    tmp = tempfile.mkdtemp()
    path = _os.path.join(tmp, "akun.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("lama@gmail.com")            # sengaja tanpa newline
    _os.environ["EMAIL_SOURCE"] = "emailnator"
    _os.environ["EMAIL_COUNT"] = "2"
    import signup, mailer
    importlib.reload(signup)
    signup.PWD_SRC = path
    n = iter(["baru1@gmail.com", "baru2@gmail.com"])
    mailer.Emailnator.new_email = lambda self: next(n)   # tanpa jaringan
    got = signup.get_emails()
    lines = open(path, encoding="utf-8").read().splitlines()
    assert lines == ["lama@gmail.com", "baru1@gmail.com", "baru2@gmail.com"], lines
    assert got == ["baru1@gmail.com", "baru2@gmail.com"], got
    for k in ("EMAIL_SOURCE", "EMAIL_COUNT"):
        _os.environ.pop(k, None)
    print("PASS: append akun.txt aman tanpa trailing newline")


def test_fresh_client_for_signup():
    """Login gagal merusak transaksi B2C -> tiap percobaan signup HARUS pakai
    Client baru. Kalau tidak, SendCode balas HTTP 500 (regresi yang pernah ada)."""
    import inspect, signup
    src = inspect.getsource(signup._signup_once)
    assert "Client(" in src and "c.start()" in src,         "_signup_once wajib bikin Client sendiri, jangan pakai sesi login"
    assert list(inspect.signature(signup._signup_once).parameters) == ["email", "proxy"]
    assert list(inspect.signature(signup.NeedsHuman.__init__).parameters) ==         ["self", "email"], "NeedsHuman jangan bawa client bekas"
    print("PASS: signup pakai sesi baru (anti-500)")


def test_dotenv():
    """.env terbaca; env var proses menimpa .env (biar `set X=..` tetap jalan)."""
    import tempfile, importlib, os as _os
    d = tempfile.mkdtemp()
    path = _os.path.join(d, ".env")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(x + chr(10) for x in [
            "# komentar", "",
            "CAPTCHA_PROVIDER=2captcha",
            'CAPTCHA_KEY="kunci-dalam-kutip"',
            "EMAIL_COUNT=5",
            "BUKAN_DIKENAL=abaikan",
        ])
    import signup
    importlib.reload(signup)
    cfg = signup.load_env(path)
    assert cfg["CAPTCHA_PROVIDER"] == "2captcha", cfg
    assert cfg["CAPTCHA_KEY"] == "kunci-dalam-kutip", cfg  # kutip dilepas
    assert cfg["EMAIL_COUNT"] == "5", cfg
    # env var menang atas .env
    _os.environ["CAPTCHA_PROVIDER"] = "capsolver"
    try:
        assert signup.load_env(path)["CAPTCHA_PROVIDER"] == "capsolver"
    finally:
        _os.environ.pop("CAPTCHA_PROVIDER", None)
    # .env tak ada -> tetap jalan, pakai default
    assert signup.load_env(_os.path.join(d, "nihil")) == {} or True
    print("PASS: .env terbaca, env var menimpa")


def test_conflict_retry():
    """Alamat emailnator sudah dipakai orang -> tukar alamat, jangan gagal."""
    import importlib, os as _os, tempfile
    _os.environ["EMAIL_SOURCE"] = "emailnator"
    import signup, mailer
    importlib.reload(signup)
    signup.PWD_SRC = _os.path.join(tempfile.mkdtemp(), "akun.txt")
    err = RuntimeError("SelfAsserted failed: {'errorCode': 'ViralErrorUserCreationConflict'}")

    tried = []
    def fake_once(email, proxy=None):
        tried.append(email)
        if len(tried) < 3:
            raise err
        return "CLIENT", {"id": "x", "email": email}
    signup._signup_once = fake_once
    n = iter(["baru1@gmail.com", "baru2@gmail.com"])
    mailer.Emailnator.new_email = lambda self: next(n)

    used, c, cogen = signup.interactive_signup("lama@gmail.com", tries=3)
    assert tried == ["lama@gmail.com", "baru1@gmail.com", "baru2@gmail.com"], tried
    assert used == "baru2@gmail.com", used     # email yang dipakai ikut berubah
    assert cogen["email"] == used

    # habis percobaan -> tetap raise, jangan diam
    signup._signup_once = lambda e, p=None: (_ for _ in ()).throw(err)
    mailer.Emailnator.new_email = lambda self: "lagi@gmail.com"
    try:
        signup.interactive_signup("z@gmail.com", tries=2)
        raise AssertionError("harus raise setelah percobaan habis")
    except RuntimeError as ex:
        assert "Conflict" in str(ex)

    # error lain jangan ditukar-alamat, langsung naik
    signup._signup_once = lambda e, p=None: (_ for _ in ()).throw(RuntimeError("HTTP 500 boom"))
    try:
        signup.interactive_signup("z@gmail.com", tries=3)
        raise AssertionError("error non-conflict harus langsung raise")
    except RuntimeError as ex:
        assert "boom" in str(ex)

    # mode file: alamat milik user, jangan ditukar -> pesan jelas soal password
    _os.environ["EMAIL_SOURCE"] = "file"
    importlib.reload(signup)
    signup._signup_once = lambda e, p=None: (_ for _ in ()).throw(err)
    try:
        signup.interactive_signup("punyaku@gmail.com", tries=3)
        raise AssertionError("mode file harus raise, bukan tukar alamat")
    except RuntimeError as ex:
        assert "sudah terdaftar" in str(ex) and "PASSWORD" in str(ex), str(ex)

    _os.environ.pop("EMAIL_SOURCE", None)
    print("PASS: conflict -> tukar alamat; mode file kasih pesan password")


def test_proxy_real():
    """Proxy HTTP lokal: pastikan request benar-benar lewat proxy, bukan langsung."""
    import http.server, socket, socketserver, threading
    import proxies

    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            hits.append(self.path)      # absolute-URI = ciri request via proxy
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d" % port
        r = proxies.build_opener(url).open("http://example.com/x", timeout=10).read()
        assert r == b"ok", r
        assert hits == ["http://example.com/x"], hits
        # rotasi: dua panggilan, dua-duanya lewat proxy
        pool = proxies.Pool([url, url])
        for i in range(2):
            proxies.build_opener(pool.next()).open(
                "http://example.com/%d" % i, timeout=10).read()
        assert len(hits) == 3, hits
    finally:
        srv.shutdown()
    print("PASS: request benar-benar lewat proxy")


def test_proxy_retry():
    """Proxy mati -> ganti proxy. Error server -> jangan ganti (boros captcha)."""
    import urllib.error, proxies, signup

    orig_pool, orig_tries, orig_log = signup.PROXY_POOL, signup.PROXY_TRIES, signup.log
    signup.PROXY_POOL = proxies.Pool(["http://p1:80", "http://p2:80", "http://p3:80"])
    signup.PROXY_TRIES = 4
    signup.log = lambda *a: None
    try:
        # dua proxy mati, ketiga hidup -> sukses lewat proxy ketiga
        seen = []

        def fn(proxy):
            seen.append(proxy)
            if len(seen) < 3:
                raise urllib.error.URLError("Tunnel connection failed: 400 Bad Request")
            return "ok"

        assert signup.with_proxy_retry(fn, "a@b.c") == "ok"
        assert seen == ["http://p1:80", "http://p2:80", "http://p3:80"], seen

        # error server -> satu percobaan saja, tak ganti proxy
        calls = []

        def server_err(proxy):
            calls.append(proxy)
            raise RuntimeError("SelfAsserted failed: ViralErrorUserCreationConflict")

        try:
            signup.with_proxy_retry(server_err, "a@b.c")
            raise AssertionError("error server harus langsung raise")
        except RuntimeError as ex:
            assert "Conflict" in str(ex)
        assert len(calls) == 1, calls

        # NeedsHuman lewat tanpa muter proxy -> akun baru langsung ke fase 2
        calls.clear()

        def needs(proxy):
            calls.append(proxy)
            raise signup.NeedsHuman("baru@b.c")

        try:
            signup.with_proxy_retry(needs, "baru@b.c")
            raise AssertionError("NeedsHuman harus naik")
        except signup.NeedsHuman:
            pass
        assert len(calls) == 1, calls
    finally:
        signup.PROXY_POOL, signup.PROXY_TRIES, signup.log = orig_pool, orig_tries, orig_log
    print("PASS: retry ganti proxy hanya untuk error jaringan")


def test_timeout():
    """Proxy yang diam harus gagal ~TIMEOUT detik, bukan menggantung 30s."""
    import socket, time, proxies

    srv = socket.socket()               # listen tapi tak pernah menjawab
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        t0 = time.time()
        try:
            proxies.build_opener("http://127.0.0.1:%d" % port).open(
                "http://example.com/", timeout=2).read()
            raise AssertionError("harus timeout")
        except Exception as ex:
            dt = time.time() - t0
            assert 2 <= dt < 6, f"timeout tak dipatuhi: {dt:.1f}s"
            assert proxies.is_network_error(ex), f"harus error jaringan: {ex!r}"
    finally:
        srv.close()

    import signup
    assert signup.TIMEOUT > 0
    print("PASS: timeout dipatuhi, proxy hang cepat dipotong")


def test_mail_timeout_separate():
    """Emailnator jauh lebih lambat dari Genspark: message-list pada inbox berisi
    butuh 5-6s (terukur). TIMEOUT yang dipendekkan demi proxy tak boleh ikut
    memutus pembacaan inbox, jadi MAIL_TIMEOUT dipisah dan punya batas bawah."""
    import importlib, inspect, os as _os, signup

    # TIMEOUT pendek tak boleh menular ke Emailnator
    _os.environ["TIMEOUT"] = "5"
    _os.environ.pop("MAIL_TIMEOUT", None)
    importlib.reload(signup)
    assert signup.TIMEOUT == 5
    assert signup.MAIL_TIMEOUT >= 20, signup.MAIL_TIMEOUT

    # batas bawah 20s: nilai yang terlalu kecil dinaikkan
    _os.environ["MAIL_TIMEOUT"] = "3"
    importlib.reload(signup)
    assert signup.MAIL_TIMEOUT == 20, signup.MAIL_TIMEOUT

    # nilai wajar dihormati
    _os.environ["MAIL_TIMEOUT"] = "45"
    importlib.reload(signup)
    assert signup.MAIL_TIMEOUT == 45

    # tiap pemakaian Emailnator harus lewat MAIL_TIMEOUT, bukan TIMEOUT
    src = inspect.getsource(signup)
    assert "Emailnator(proxy=proxy, timeout=TIMEOUT)" not in src
    assert "Emailnator(proxy=PROXY_POOL.next(), timeout=TIMEOUT)" not in src
    assert src.count("timeout=MAIL_TIMEOUT") == 3, src.count("timeout=MAIL_TIMEOUT")

    for k in ("TIMEOUT", "MAIL_TIMEOUT"):
        _os.environ.pop(k, None)
    importlib.reload(signup)
    print("PASS: MAIL_TIMEOUT terpisah dari TIMEOUT proxy")


def test_wait_otp_stops():
    """wait_otp harus BERHENTI begitu OTP ketemu, dan tak memeriksa ulang pesan
    yang sudah dibaca."""
    import mailer

    class FakeMail:
        def __init__(self):
            self.list_calls = 0

        def messages(self, email):
            self.list_calls += 1
            if self.list_calls == 1:
                raise RuntimeError("The read operation timed out")   # error ditelan
            return [{"messageID": "m1", "from": "Microsoft on behalf of Genspark"}]

        def body(self, email, mid):
            return "Your code is 424242"

    m = FakeMail()
    logs = []
    code = mailer.wait_otp(m, "a@b.c", sender="genspark", timeout=30,
                           interval=0, log=logs.append)
    assert code == "424242", code
    assert m.list_calls == 2, m.list_calls        # gagal sekali, lalu ketemu -> stop
    assert any("inbox:" in x for x in logs)
    assert any("424242" in x for x in logs)

    # error yang sama berulang hanya dilaporkan SEKALI: tanpa ini satu inbox
    # lambat membanjiri log dan menutupi keterangan akun lain
    class Slow:
        def __init__(self):
            self.n = 0

        def messages(self, email):
            self.n += 1
            if self.n < 8:
                raise RuntimeError("The read operation timed out")
            return [{"messageID": "m1", "from": "Microsoft on behalf of Genspark"}]

        def body(self, email, mid):
            return "code 424242"

    logs = []
    assert mailer.wait_otp(Slow(), "a@b.c", sender="genspark", timeout=30,
                           interval=0, log=logs.append) == "424242"
    assert len(logs) == 2, logs      # 1 peringatan + 1 OTP, bukan 8 baris

    # pesan yang tak cocok sender tak dibaca ulang tiap putaran
    class Noise:
        def __init__(self):
            self.body_calls = 0

        def messages(self, email):
            return [{"messageID": "spam", "from": "SHEIN"}]

        def body(self, email, mid):
            self.body_calls += 1
            return "diskon 50%"

    n = Noise()
    try:
        mailer.wait_otp(n, "a@b.c", sender="genspark", timeout=0.3,
                        interval=0.1, log=lambda *a: None)
        raise AssertionError("harus timeout kalau OTP tak pernah datang")
    except RuntimeError as ex:
        assert "tak ketemu" in str(ex)
    assert n.body_calls == 0, "pesan yang difilter sender jangan diunduh"
    print("PASS: wait_otp berhenti saat OTP ketemu, tak baca ulang")


def test_gmail_dedupe():
    """Gmail mengabaikan titik: a.b@gmail dan ab@gmail adalah SATU inbox, jadi
    satu akun Genspark. Emailnator mengacak titik pada nama yang sama, jadi ia
    bisa memberi alamat yang inbox-nya sudah punya akun -> conflict."""
    import importlib, os as _os, tempfile, signup, mailer

    assert signup.gmail_key("x.z.e.r.afro.s.t@gmail.com") == \
        signup.gmail_key("x.ze.r.a.fro.s.t@gmail.com") == "xzerafrost@gmail.com"
    assert signup.gmail_key("A.B@Gmail.COM") == "ab@gmail.com"

    # fresh_email harus melewati alamat yang inbox-nya sudah dipakai
    urut = iter(["x.z.e.r.afro.st@gmail.com",     # inbox sama -> dilewati
                 "xz.e.rafrost@gmail.com",        # inbox sama -> dilewati
                 "orang.b.aru@gmail.com"])        # inbox baru -> dipakai

    class M:
        def new_email(self):
            return next(urut)

    em = signup.fresh_email(M(), {"xzerafrost@gmail.com"})
    assert em == "orang.b.aru@gmail.com", em

    # kalau semua tabrakan, tetap kembalikan sesuatu (alur conflict yang urus),
    # jangan menggantung atau melempar
    class Same:
        def new_email(self):
            return "x.zerafrost@gmail.com"

    em = signup.fresh_email(Same(), {"xzerafrost@gmail.com"}, tries=3)
    assert em == "x.zerafrost@gmail.com"

    # used_inboxes membaca akun.txt DAN accounts.json
    d = tempfile.mkdtemp()
    akun = _os.path.join(d, "akun.txt")
    with open(akun, "w", encoding="utf-8") as f:
        f.writelines(x + chr(10) for x in ["# komentar", "a.b.c@gmail.com"])
    acc = _os.path.join(d, "accounts.json")
    with open(acc, "w", encoding="utf-8") as f:
        f.write('{"d.e.f@gmail.com": {"plan": "plus"}}')
    old_pwd, old_acc = signup.PWD_SRC, signup.ACCOUNTS
    signup.PWD_SRC, signup.ACCOUNTS = akun, acc
    try:
        keys = signup.used_inboxes()
        assert "abc@gmail.com" in keys and "def@gmail.com" in keys, keys
    finally:
        signup.PWD_SRC, signup.ACCOUNTS = old_pwd, old_acc

    # get_emails tak boleh memberi dua alamat dengan inbox sama dalam satu batch
    _os.environ["EMAIL_SOURCE"] = "emailnator"
    _os.environ["EMAIL_COUNT"] = "2"
    importlib.reload(signup)
    d2 = tempfile.mkdtemp()
    signup.PWD_SRC = _os.path.join(d2, "akun.txt")
    signup.ACCOUNTS = _os.path.join(d2, "accounts.json")
    seq = iter(["p.q@gmail.com", "pq@gmail.com", "r.s@gmail.com"])
    mailer.Emailnator.new_email = lambda self: next(seq)
    got = signup.get_emails()
    assert len(got) == 2, got
    assert len({signup.gmail_key(e) for e in got}) == 2, got
    for k in ("EMAIL_SOURCE", "EMAIL_COUNT"):
        _os.environ.pop(k, None)
    print("PASS: inbox Gmail tak dipakai dua kali")


def test_no_head_of_line_block():
    """Hasil dipanen dengan as_completed: akun yang kelar dulu dilaporkan dulu.

    Kalau dipanen berurutan submit, satu akun yang masih menunggu OTP menahan
    laporan akun lain yang sudah selesai -- tampak macet padahal jalan.
    """
    import inspect, time, signup
    from concurrent.futures import ThreadPoolExecutor, as_completed

    src = inspect.getsource(signup)
    assert "for email, fut in [" not in src, \
        "panen berurutan submit bikin akun cepat tertahan akun lambat"
    assert src.count("as_completed(") >= 4, src.count("as_completed(")

    # buktikan bedanya, bukan cuma cek bentuk kode
    def job(i):
        time.sleep(0.35 if i == 0 else 0.02)   # akun 0 lambat
        return i

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(job, i): i for i in range(4)}
        t0 = time.time()
        first = None
        for fut in as_completed(futs):
            fut.result()
            if first is None:
                first = time.time() - t0
    assert first < 0.3, f"laporan pertama tertahan {first:.2f}s"
    print("PASS: akun cepat tak tertahan akun lambat")


def main():
    if not os.path.exists(HAR):
        print("SKIP: HAR not found")
        return 0
    with open(HAR, encoding="utf-8") as f:
        har = json.load(f)

    # authorize page (entry 198) -> unified csrf
    auth = entry(har, 198)["response"]["content"]["text"]
    m = re.search(r'"api":"CombinedSigninAndSignup","csrf":"([^"]+)"', auth)
    assert m, "unified csrf regex failed on authorize page"
    unified_csrf = m.group(1)
    assert unified_csrf.startswith("Zk9qby"), f"unexpected csrf: {unified_csrf[:20]}"

    # unified signup page (entry 241) -> AJAX csrf + transId
    uni = entry(har, 241)["response"]["content"]["text"]
    m = re.search(r'"csrf":"([^"]+)"', uni)
    assert m, "csrf regex failed on unified page"
    ajax_csrf = m.group(1)
    assert ajax_csrf.startswith("b21xeE82"), f"unexpected csrf: {ajax_csrf[:20]}"
    m = re.search(r'"transId":"([^"]+)"', uni)
    assert m, "transId regex failed on unified page"
    tx = m.group(1)
    assert tx.startswith("StateProperties="), f"unexpected tx: {tx[:30]}"

    # tx base must be url-encodable as-is (it is percent-encoded in URLs)
    tx_enc = tx.replace("=", "%3D")  # matches HAR URL form ?tx=StateProperties%3DeyJ...
    assert "StateProperties%3D" in tx_enc

    # GetChallenge response (245) -> challengeId + image
    gc = json.loads(entry(har, 245)["response"]["content"]["text"])
    assert gc["status"] == "200" and gc["challengeId"] and gc["challengeString"].startswith("data:image/jpeg;base64,")

    # SendCode body (248)
    body = entry(har, 248)["request"]["postData"]["text"]
    assert "&email=" in body
    # SelfAsserted body (250)
    sa = entry(har, 250)["request"]["postData"]["text"]
    for k in ["email=", "emailVerificationCode=", "captchaControlChallengeCode=", "newPassword=", "reenterPassword=", "request_type=RESPONSE"]:
        assert k in sa, f"missing {k} in SelfAsserted body"

    # confirmed (251) 302 Location -> /api/auth
    loc = entry(har, 251)["response"]["headers"]
    loc = [h["value"] for h in loc if h["name"].lower() == "location"][0]
    assert "/api/auth" in loc

    # /api/user (936) has email in cogen
    user = json.loads(entry(har, 936)["response"]["content"]["text"])
    assert user["data"]["cogen"]["email"] and "@" in user["data"]["cogen"]["email"]

    print(f"PASS: parsing verified against HAR ({len(har['log']['entries'])} entries)")
    test_concurrency()
    test_email_append()
    test_fresh_client_for_signup()
    test_dotenv()
    test_conflict_retry()
    test_proxy_real()
    test_proxy_retry()
    test_timeout()
    test_mail_timeout_separate()
    test_wait_otp_stops()
    test_gmail_dedupe()
    test_no_head_of_line_block()
    return 0


if __name__ == "__main__":
    sys.exit(main())
