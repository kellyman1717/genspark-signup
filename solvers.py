#!/usr/bin/env python3
"""Captcha solver: image-to-text base64 (bukan reCAPTCHA).

Captcha Genspark = gambar JPEG 4-8 karakter alfanumerik. Semua provider di
bawah pakai endpoint "normal captcha" / "ImageToTextTask".

Set lewat env var, atau CAPTCHA_PROVIDER + CAPTCHA_KEY di signup.py:
    set CAPTCHA_PROVIDER=2captcha
    set CAPTCHA_KEY=xxxxxxxx
Provider: 2captcha | capsolver | anticaptcha | capmonster | manual (default)
"""
import json, time, urllib.request, urllib.error

import proxies

TIMEOUT = 180  # detik nunggu hasil solve
POLL = 5


# Solver API tak perlu diproksikan (bukan sasaran rate-limit Genspark), tapi
# ikut proxy kalau jaringan lokal memblokirnya.
PROXY = None


def _post_json(url, payload, timeout=30):
    r = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with proxies.build_opener(PROXY).open(r, timeout=timeout) as resp:
        return json.load(resp)


def _get(url, timeout=30):
    with proxies.build_opener(PROXY).open(url, timeout=timeout) as resp:
        return resp.read().decode()


def _strip_b64(data_url):
    """'data:image/jpeg;base64,XXX' -> 'XXX'"""
    return data_url.split(",", 1)[1] if "," in data_url else data_url


# ---- provider: 2captcha (dan API-compatible: ruCaptcha, azcaptcha) ----
def solve_2captcha(b64, key, host="https://2captcha.com"):
    j = _post_json(f"{host}/in.php", {
        "key": key, "method": "base64", "body": b64,
        "json": 1, "numeric": 0, "min_len": 4, "max_len": 8,
    })
    if j.get("status") != 1:
        raise RuntimeError(f"2captcha in.php: {j.get('request')}")
    cid = j["request"]
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL)
        r = _get(f"{host}/res.php?key={key}&action=get&id={cid}&json=1")
        j = json.loads(r)
        if j.get("status") == 1:
            return j["request"]
        if j.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha res.php: {j.get('request')}")
    raise RuntimeError("2captcha timeout")


# ---- provider: CapSolver / Anti-Captcha / CapMonster (createTask API) ----
def solve_createtask(b64, key, host, task_type="ImageToTextTask"):
    j = _post_json(f"{host}/createTask", {
        "clientKey": key,
        "task": {"type": task_type, "body": b64},
    })
    if j.get("errorId"):
        raise RuntimeError(f"createTask: {j.get('errorDescription') or j}")
    tid = j["taskId"]
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL)
        j = _post_json(f"{host}/getTaskResult", {"clientKey": key, "taskId": tid})
        if j.get("errorId"):
            raise RuntimeError(f"getTaskResult: {j.get('errorDescription') or j}")
        if j.get("status") == "ready":
            sol = j.get("solution", {})
            return sol.get("text") or sol.get("gRecaptchaResponse")
    raise RuntimeError("createTask timeout")


PROVIDERS = {
    "2captcha":    lambda b, k: solve_2captcha(b, k),
    "rucaptcha":   lambda b, k: solve_2captcha(b, k, "https://rucaptcha.com"),
    "azcaptcha":   lambda b, k: solve_2captcha(b, k, "https://azcaptcha.com"),
    "capsolver":   lambda b, k: solve_createtask(b, k, "https://api.capsolver.com"),
    "anticaptcha": lambda b, k: solve_createtask(b, k, "https://api.anti-captcha.com"),
    "capmonster":  lambda b, k: solve_createtask(b, k, "https://api.capmonster.cloud"),
}


def solve(data_url, provider, key):
    """Return teks captcha. provider='manual'/kosong -> raise, caller minta input."""
    provider = (provider or "manual").lower()
    if provider == "manual":
        raise ValueError("manual")
    fn = PROVIDERS.get(provider)
    if not fn:
        raise ValueError(f"provider tak dikenal: {provider}. Pilih: {', '.join(PROVIDERS)}")
    if not key:
        raise ValueError(f"CAPTCHA_KEY kosong untuk provider {provider}")
    return fn(_strip_b64(data_url), key)


def balance(provider, key):
    """Cek saldo — sekaligus bukti key valid. Return float."""
    provider = (provider or "").lower()
    hosts = {"2captcha": "https://2captcha.com", "rucaptcha": "https://rucaptcha.com",
             "azcaptcha": "https://azcaptcha.com"}
    if provider in hosts:
        j = json.loads(_get(f"{hosts[provider]}/res.php?key={key}&action=getbalance&json=1"))
        if j.get("status") != 1:
            raise RuntimeError(f"{provider}: {j.get('request')}")
        return float(j["request"])
    ct = {"capsolver": "https://api.capsolver.com",
          "anticaptcha": "https://api.anti-captcha.com",
          "capmonster": "https://api.capmonster.cloud"}
    if provider in ct:
        j = _post_json(f"{ct[provider]}/getBalance", {"clientKey": key})
        if j.get("errorId"):
            raise RuntimeError(f"{provider}: {j.get('errorDescription') or j}")
        return float(j["balance"])
    raise ValueError(f"provider tak dikenal: {provider}")


def demo():
    """Self-check: routing + parsing tanpa panggil jaringan."""
    assert _strip_b64("data:image/jpeg;base64,QUJD") == "QUJD"
    assert _strip_b64("QUJD") == "QUJD"
    for name in ["2captcha", "capsolver", "anticaptcha", "capmonster", "rucaptcha"]:
        assert name in PROVIDERS, name
    for bad, exp in [("manual", "manual"), ("nope", "tak dikenal")]:
        try:
            solve("data:image/jpeg;base64,QQ==", bad, "k")
            raise AssertionError(f"{bad} harus raise")
        except ValueError as e:
            assert exp in str(e), str(e)
    try:
        solve("data:image/jpeg;base64,QQ==", "2captcha", "")
        raise AssertionError("key kosong harus raise")
    except ValueError as e:
        assert "kosong" in str(e)
    print("PASS: solvers routing OK")


if __name__ == "__main__":
    # python solvers.py                 -> self-check
    # python solvers.py 2captcha KEY    -> cek saldo + validitas key
    import sys
    if len(sys.argv) == 3:
        print(f"{sys.argv[1]} saldo: {balance(sys.argv[1], sys.argv[2])}")
    else:
        demo()
