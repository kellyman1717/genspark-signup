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
py signup.py dump      :: bikin akun tempmail, simpan yang creditnya lolos ambang
py signup.py credit    :: cek sisa credit tiap akun
py signup.py credit a@gmail.com b@gmail.com   :: cek akun tertentu saja
py test_har.py         :: self-check
py webui.py            :: WebUI — satu perintah, atur setting & jalankan dari browser
```

### Mode dump

`py signup.py dump` (atau tombol **Dump** di WebUI) bikin akun dari alamat
tempmail, cek plan + credit-nya, lalu **simpan hanya yang creditnya memenuhi
ambang**. Alurnya: signup → email → password → cek credit & plan → simpan
kalau lolos. Tanpa checkout Stripe sama sekali: tak ada kartu, tak ada
tagihan. Akun yang creditnya di bawah ambang tetap dilaporkan (biar
distribusinya kelihatan) tapi tidak masuk `accounts.json`.

Diatur lewat `.env` atau form WebUI:

```ini
DUMP_MIN_CREDIT=2000   ; simpan kalau credit >= ini
DUMP_TARGET=1          ; berhenti setelah dapat sebanyak ini
DUMP_MAX_TRIES=10      ; batas percobaan (tiap percobaan pakai captcha)
DUMP_WORKERS=3         ; akun digarap serentak
DUMP_CREDIT_WAIT=25    ; detik menunggu credit menyusul setelah signup
```

Butuh `EMAIL_SOURCE=emailnator`. Akun yang lolos tetap dibuatkan API key,
jadi langsung terbaca `py signup.py credit`.

Dump berjalan **paralel** (`DUMP_WORKERS`) dan hanya menjalankan sebanyak
yang masih dibutuhkan — kalau target sisa satu, ia tak memulai tiga worker
sekaligus, jadi captcha tak terbuang. Kalau `CAPTCHA_PROVIDER=manual`,
paralel dipaksa jadi 1 karena jawaban captcha diketik satu per satu.

`DUMP_CREDIT_WAIT` penting: plan naik lebih dulu daripada credit
dikreditkan, jadi membaca saldo terlalu cepat memberi 100 (bonus akun
gratis) dan akun bagus ikut terbuang.

### WebUI

`py webui.py` membuka `http://127.0.0.1:8765` di browser. Setting diubah lewat
form dan disimpan ke `.env`; tombol **Mulai** / **Cek credit** menjalankan
`signup.py` di belakang layar, log ditayangkan langsung, dan hasil terbaca dari
`accounts.json`. Hanya stdlib, tanpa install. Captcha dan OTP manual juga bisa
dijawab dari browser: gambarnya muncul di halaman, jawabannya diteruskan ke
proses yang sedang menunggu.

Tab **Hasil** bisa menyegarkan credit langsung: tombol putar per baris untuk
satu akun, atau **Refresh credit terpilih** / **Refresh semua credit**. Hanya
akun yang disegarkan yang ditimpa, jadi nilai akun lain tak hilang, dan waktu
cek terakhir dicatat per akun.

Argumen: `py webui.py 9000` (ganti port), `py webui.py --no-browser` (jangan
buka tab otomatis). Port default bisa juga diset lewat env `WEBUI_PORT`.

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
