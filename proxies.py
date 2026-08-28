#!/usr/bin/env python3
"""Proxy pool + round-robin. Semua tipe: http, https, socks4, socks5, socks5h.

Format di proxy.txt / .env (satu per baris, koma juga boleh):
    http://user:pass@1.2.3.4:8080
    socks5://user:pass@1.2.3.4:1080
    socks5h://1.2.3.4:1080          # DNS lewat proxy
    1.2.3.4:8080                    # tanpa skema -> dianggap http
    1.2.3.4:8080:user:pass          # format host:port:user:pass
"""
import itertools, os, socket, threading, urllib.error, urllib.parse, urllib.request

DEFAULT_SCHEME = "http"
SOCKS = ("socks4", "socks4a", "socks5", "socks5h")


def normalize(raw):
    """Satu baris -> URL proxy lengkap, atau None kalau baris tak terpakai."""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" not in raw:
        # host:port:user:pass -> http://user:pass@host:port
        bits = raw.split(":")
        if len(bits) == 4:
            host, port, user, pw = bits
            raw = f"{DEFAULT_SCHEME}://{user}:{pw}@{host}:{port}"
        else:
            raw = f"{DEFAULT_SCHEME}://{raw}"
    u = urllib.parse.urlsplit(raw)
    if not u.hostname or not u.port:
        raise ValueError(f"proxy tanpa host/port: {raw}")
    if u.scheme not in ("http", "https") + SOCKS:
        raise ValueError(f"skema proxy tak didukung: {u.scheme}")
    return raw


def parse(text):
    """Teks banyak baris/koma -> list URL proxy.

    Komentar dibuang per-BARIS dulu, baru dipecah koma. Kalau koma dipecah
    lebih dulu, baris komentar seperti "# Tipe: http, https, socks5" menyisakan
    token sampah ("https") yang lolos dari cek startswith("#").
    """
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0]          # komentar di ujung baris ikut hilang
        for chunk in line.split(","):
            p = normalize(chunk)
            if p:
                out.append(p)
    return out


def load(env_value="", path=None):
    """Gabung PROXY dari .env dan isi proxy.txt. Duplikat dibuang, urutan tetap."""
    found = parse(env_value)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            found += parse(f.read())
    seen, uniq = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def is_network_error(ex):
    """True kalau kegagalan berasal dari jaringan/proxy, bukan jawaban server.

    Proxy busuk muncul sebagai URLError: "Tunnel connection failed: 400 Bad
    Request" (proxy tolak CONNECT) atau WinError 10060 (proxy diam). Keduanya
    layak dicoba ulang dengan proxy lain. HTTPError DIKECUALIKAN: itu jawaban
    sah dari server tujuan, ganti IP tak mengubah apa pun.
    """
    if isinstance(ex, urllib.error.HTTPError):
        return False
    if isinstance(ex, (urllib.error.URLError, socket.timeout, ConnectionError,
                       TimeoutError)):
        return True
    if isinstance(ex, OSError):
        return True
    # req() membungkus sebagian error jadi RuntimeError -> cocokkan teksnya
    text = str(ex).lower()
    return any(k in text for k in (
        "tunnel connection failed", "urlopen error", "winerror 100",
        "proxy", "timed out", "connection reset", "connection aborted",
        "connection refused", "socks", "remote end closed",
    ))


class Pool:
    """Round-robin, aman dipakai banyak thread. Kosong -> None (koneksi langsung)."""

    def __init__(self, proxies=()):
        self.proxies = list(proxies)
        self._lock = threading.Lock()
        self._cycle = itertools.cycle(self.proxies) if self.proxies else None

    def __len__(self):
        return len(self.proxies)

    def next(self):
        if not self._cycle:
            return None
        with self._lock:            # itertools.cycle tak thread-safe
            return next(self._cycle)


def build_opener(proxy_url, *handlers):
    """Opener yang lewat proxy. SOCKS butuh PySocks; http/https pakai stdlib."""
    if not proxy_url:
        return urllib.request.build_opener(*handlers)
    scheme = urllib.parse.urlsplit(proxy_url).scheme
    if scheme in SOCKS:
        try:
            from sockshandler import SocksiPyHandler
            import socks
        except ImportError as e:
            raise RuntimeError(
                f"proxy {scheme} butuh PySocks: pip install PySocks") from e
        u = urllib.parse.urlsplit(proxy_url)
        kind = socks.SOCKS4 if scheme.startswith("socks4") else socks.SOCKS5
        # socks5h / socks4a = resolve DNS di sisi proxy
        return urllib.request.build_opener(
            SocksiPyHandler(kind, u.hostname, u.port,
                            rdns=scheme.endswith("h") or scheme.endswith("a"),
                            username=u.username, password=u.password),
            *handlers)
    # http/https: ProxyHandler sudah paham user:pass@host:port
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}),
        *handlers)


def demo():
    """Self-check: parsing + rotasi, tanpa jaringan."""
    assert normalize("1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert normalize("1.2.3.4:8080:bob:rahasia") == "http://bob:rahasia@1.2.3.4:8080"
    assert normalize("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert normalize("# komentar") is None and normalize("  ") is None
    for bad in ("http://tanpaport", "ftp://1.2.3.4:21"):
        try:
            normalize(bad)
            raise AssertionError(f"{bad} harus ditolak")
        except ValueError:
            pass

    assert parse("1.1.1.1:80, 2.2.2.2:80") == ["http://1.1.1.1:80", "http://2.2.2.2:80"]
    # komentar berisi koma jangan dipecah jadi token sampah
    assert parse("# Tipe: http, https, socks5" + chr(10) + "1.1.1.1:80") == ["http://1.1.1.1:80"]
    assert parse("1.1.1.1:80  # proxy utama") == ["http://1.1.1.1:80"]
    assert load("1.1.1.1:80\n1.1.1.1:80") == ["http://1.1.1.1:80"]  # duplikat dibuang

    p = Pool(["http://a:1", "http://b:2"])
    assert [p.next() for _ in range(5)] == ["http://a:1", "http://b:2",
                                            "http://a:1", "http://b:2", "http://a:1"]
    assert Pool().next() is None and len(Pool()) == 0

    # rotasi tetap adil walau diambil dari banyak thread
    import collections, threading as th
    big = Pool([f"http://p{i}:80" for i in range(4)])
    hits, lock = collections.Counter(), th.Lock()
    def grab():
        for _ in range(50):
            v = big.next()
            with lock:
                hits[v] += 1
    ts = [th.Thread(target=grab) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert set(hits.values()) == {50}, dict(hits)

    assert build_opener(None) is not None
    assert build_opener("http://1.2.3.4:8080") is not None

    # error jaringan/proxy -> boleh ganti proxy
    for e in (urllib.error.URLError("Tunnel connection failed: 400 Bad Request"),
              urllib.error.URLError("[WinError 10060] no response"),
              socket.timeout("timed out"),
              ConnectionResetError("connection reset by peer"),
              RuntimeError("<urlopen error Tunnel connection failed>")):
        assert is_network_error(e), e
    # jawaban server -> jangan ganti proxy, tak ada gunanya
    for e in (urllib.error.HTTPError("http://x", 500, "boom", {}, None),
              RuntimeError("SelfAsserted failed: ViralErrorUserCreationConflict"),
              ValueError("captcha salah")):
        assert not is_network_error(e), e
    print("PASS: proxy parsing, round-robin, deteksi error OK")


if __name__ == "__main__":
    demo()
