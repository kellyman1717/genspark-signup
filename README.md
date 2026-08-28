# genspark-signup

Otomasi alur signup + langganan Genspark.ai, direkonstruksi dari HAR trace.
Alur lengkap ada di [`genspark_flow.md`](genspark_flow.md).

## Isi

| File | Guna |
|---|---|
| `signup.py` | alur utama: login/signup, checkout, ambil API key |
| `mailer.py` | Emailnator: bikin alamat + baca OTP otomatis |
| `solvers.py` | captcha solver (2captcha, capsolver, dll) |
| `proxies.py` | pool proxy + round-robin, semua tipe |
| `test_har.py` | self-check parsing terhadap HAR + regression test |
| `SETUP.md` | langkah pakai di device baru |
| `genspark_flow.md` | dokumentasi endpoint dan alurnya |

Hanya pustaka standar Python. `PySocks` (lihat `requirements.txt`)
opsional, dibutuhkan hanya untuk proxy SOCKS.

## Pakai

```bat
copy .env.example .env
```

Isi `.env`:

```ini
CAPTCHA_PROVIDER=2captcha
CAPTCHA_KEY=api-key-kamu
EMAIL_SOURCE=emailnator
EMAIL_COUNT=3
PROXY=
```

Jalankan:

```bat
py signup.py           :: proses akun
py signup.py credit    :: cek sisa credit tiap akun
py test_har.py         :: self-check
```

## Alur

```
fase 1  login akun existing        -> paralel
fase 2  signup: captcha + OTP      -> paralel kalau solver + emailnator aktif
fase 3  checkout Stripe + API key  -> paralel
```

Hasil masuk `accounts.json`. Menjalankan ulang aman: akun yang sudah selesai
dilewati, yang sudah berbayar tidak ditagih dua kali.

## Catatan

- Alamat Emailnator (`dotGmail`) itu **publik** — inbox bisa dibaca siapa pun
  yang men-generate alamat sama. Jangan pakai untuk akun yang mau dipakai lama.
- `.env`, `accounts.json`, `akun.txt`, `proxy.txt` masuk `.gitignore`.
  Jangan pernah di-commit.
- Kartu pembayaran diisi manual di browser oleh pengguna; skrip tidak
  menyimpan atau memproses data kartu.
