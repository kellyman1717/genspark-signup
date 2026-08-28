# Genspark.ai Signup Flow (Azure AD B2C)

## Overview
Genspark uses Azure AD B2C with custom policy `B2C_1_new_login`. Signup flow is a SelfAsserted page with captcha + email OTP verification.

## API Endpoints

| # | Method | URL | Description |
|---|--------|-----|-------------|
| 1 | GET | `/api/login?legacy_b2c=true&redirect_url=...` | Initiate login -> 307 redirect to B2C authorize |
| 2 | GET | `/b2c_1_new_login/oauth2/v2.0/authorize?...` | B2C authorize page (renders login HTML) |
| 3 | GET | `/api/auth/login_template?v2` | Login template HTML |
| 4 | GET | `/api/CombinedSigninAndSignup/unified?local=signup&csrf_token=...` | Signup page (contains CSRF + tx in window.SETTINGS) |
| 5 | GET | `/api/auth/signup_template` | Signup details template |
| 6 | GET | `/SelfAsserted/DisplayControlAction/vbeta/captchaControlChallengeCode/GetChallenge?tx=...` | Get captcha image (base64 JPEG) + challengeId |
| 7 | POST | `/SelfAsserted/DisplayControlAction/vbeta/captchaControlChallengeCode/VerifyChallenge?tx=...` | Submit captcha text |
| 8 | POST | `/SelfAsserted/DisplayControlAction/vbeta/emailVerificationControl/SendCode?tx=...` | Send OTP code to email |
| 9 | POST | `/SelfAsserted/DisplayControlAction/vbeta/emailVerificationControl/VerifyCode?tx=...` | Verify OTP code |
| 10 | POST | `/SelfAsserted?tx=...&p=B2C_1_new_login` | Submit full signup form |
| 11 | GET | `/api/SelfAsserted/confirmed?csrf_token=...` | Confirm -> 302 redirect to `/api/auth?state=...&client_info=...` |
| 12 | GET | `/api/auth?state=...&client_info=...` | Exchange auth code -> 307 redirect to home (sets session) |

## Flow Diagram

```
Browser                      Genspark API              Azure AD B2C
  |                              |                         |
  |-- GET /api/login ----------->|                         |
  |                              |-- 307 Location: ------->|
  |                              |   authorize endpoint    |
  |<------------------------------------------------------|
  |                              
  |-- GET /authorize?client_id=...&response_type=code... ->|
  |                                                         |
  |<-- login page HTML (with idp buttons) -----------------|
  |
  |-- GET /api/CombinedSigninAndSignup/unified ----------->|
  |   ?local=signup&csrf_token=...                         |
  |<-- signup page HTML (with window.SETTINGS) ------------|
  |
  |-- GET /api/auth/signup_template ---------------------->|
  |<-- signup details HTML ---------------------------------|
  |                                                         |
  |-- GET GetChallenge?tx=... ---------------------------->|
  |<-- {challengeId, challengeString(base64 JPEG)} --------|
  |
  |-- POST VerifyChallenge?tx=... ------------------------>|
  |   body: challengeId, captchaEntered, challengeType     |
  |<-- {status:"200", isCaptchaSolved:"True"} -------------|
  |
  |-- POST emailVerificationControl/SendCode?tx=... ------>|
  |   body: email                                          |
  |<-- {status:"200"} --------------------------------------|
  |
  |-- POST emailVerificationControl/VerifyCode?tx=... ---->|
  |   body: email, emailVerificationCode                   |
  |<-- {status:"200"} --------------------------------------|
  |
  |-- POST /SelfAsserted?tx=...&p=B2C_1_new_login ------->|
  |   body: email, verificationCode, captcha,              |
  |         newPassword, reenterPassword, request_type     |
  |<-- {status:"200"} --------------------------------------|
  |
  |-- GET /api/SelfAsserted/confirmed?csrf_token=... ----->|
  |<-- 302 Location: /api/auth?state=...&client_info=... --|
  |
  |-- GET /api/auth?state=...&client_info=... ------------>|
  |<-- 307 Location: / (session cookie set) ---------------|
  |                                                         |
  |-- GET /api/user -------------------------------------->|
  |<-- {user data} -----------------------------------------|
```

## Key Parameters

### client_id
`536a4e98-fd24-4cbc-a67b-417e209e0080`

### authorize params
- `response_type=code`
- `redirect_uri=https://www.genspark.ai/api/auth`
- `scope=email offline_access openid profile`
- `code_challenge_method=S256` (PKCE)
- `prompt=login`

### CSRF Token
Embedded in signup page HTML as `window.SETTINGS.csrf`. Same token used for all subsequent AJAX calls. Sent as `X-CSRF-TOKEN` header.

### tx (Transaction ID / StateProperties)
Base64-encoded JSON containing `TID` (UUID). Example:
`StateProperties=eyJUSUQiOiJiYTFjNDA4OC00OTJm...`
decodes to: `{"TID":"ba1c4088-492f-462c-b8da-c5077c89a2ff"}`

### Request Headers
- `X-CSRF-TOKEN: <csrf from page>`
- `X-Requested-With: XMLHttpRequest`
- `Content-Type: application/x-www-form-urlencoded; charset=UTF-8`
- `Referer: https://login.genspark.ai/.../unified?...`

### Captcha
- Type: Visual (text-based, 4 chars)
- Image: base64 JPEG in `challengeString`
- Challenge ID: UUID

### Password
Observed: `Masuk@123456` (hardcoded in signup)

## Session Flow

### Signup Form Submission (step 10)
```
email=user%40email.com
emailVerificationCode=<OTP from email>
captchaControlChallengeCode=<captcha text>
newPassword=<password>
reenterPassword=<password>
request_type=RESPONSE
```

### Confirmation -> Auth -> Session
1. `/SelfAsserted/confirmed` returns 302 with Location pointing to `/api/auth`
2. `/api/auth` returns 307 redirect to home page
3. Subsequent requests include session cookie (set by B2C)
4. `/api/user` returns user data with `email`, `plan`, etc.

## Cookie Handling
B2C sets various cookies during the flow. Must maintain session via cookie jar.

## OTP Email
After step 8 (SendCode), OTP is sent to the provided email. Must read email inbox to extract the code. OTP format: 6-digit numeric code.

## Payment Flow (Stripe)

### Tier Config
`GET /api/payment/sub2/tier_config` returns 13 tiers. Default `plus1`:
```
price_id: price_1T7YeoHy7UpDvrVidVyuxxNm
price_name: ai.genspark.vip.plus.c1.month
plan_price: 24.99
```

### Create Checkout Session
`POST /api/payment/create-checkout-session-web` (JSON):
```json
{
  "price_id": "price_1T7YeoHy7UpDvrVidVyuxxNm",
  "price_name": "ai.genspark.vip.plus.c1.month",
  "plan_price": "24.99",
  "plan_type": "subscription",
  "testmode": "false",
  "sandmode": "false",
  "current_path": "/",
  "checkout_source": "sub2",
  "fromurl": "first_month_banner",
  "wallet_coupon_key": "first_month:<cogen_id>",
  "wallet_coupon_explicit": true
}
```
Response: `data.url` = `https://checkout.stripe.com/c/pay/cs_live_...`

### Coupon
`wallet_coupon_key: first_month:<cogen_id>` -> first month $0 (HAR shows `amount_in_cents: 0`).

### Checkout Status Poll
`GET /api/payment/checkout-session?checkout_session_id=cs_live_...`
Response: `{"payment_status":"paid","quantity":1,"amount_in_cents":0,"currency":"usd"}`

### API Key
`POST /api/api_tokens/create` (JSON `{"key_name":"..."}`)
Response: `data.token` = `gsk-...` (JWT-encoded cogen_id + key_id)

## accounts.json
Saved per email: `email`, `cogen_id`, `api_key`, `key_name`, `payment_status`, `plan`, `created_at`.

## Captcha Solver

Captcha B2C = gambar JPEG base64, 4-8 karakter alfanumerik → endpoint **normal captcha / ImageToTextTask** (bukan reCAPTCHA).

### Provider didukung (`solvers.py`)
| Provider | API | Host |
|---|---|---|
| `2captcha` | in.php/res.php | https://2captcha.com |
| `rucaptcha` | in.php/res.php | https://rucaptcha.com |
| `azcaptcha` | in.php/res.php | https://azcaptcha.com |
| `capsolver` | createTask | https://api.capsolver.com |
| `anticaptcha` | createTask | https://api.anti-captcha.com |
| `capmonster` | createTask | https://api.capmonster.cloud |
| `manual` | — | input ketik sendiri (default) |

### Pakai
```bat
set CAPTCHA_PROVIDER=2captcha
set CAPTCHA_KEY=xxxxxxxxxxxxx
py signup.py
```
Atau edit `CAPTCHA_PROVIDER` / `CAPTCHA_KEY` di atas `signup.py`.

Solver salah baca → ambil captcha baru, ulangi sampai `CAPTCHA_TRIES` (default 3).

## Multi-Akun Paralel

`WORKERS = 6` (atas `signup.py`). Tiga fase:

| Fase | Isi | Mode |
|---|---|---|
| 1 | login akun existing | paralel |
| 2 | signup: captcha + OTP | serial (OTP butuh baca email) |
| 3 | checkout Stripe + API key | paralel |

Tiap `Client` punya cookie jar sendiri → sesi antar akun tak tercampur. `accounts.json` + stdout dilindungi `IO_LOCK`.

Email yang sudah ada di `accounts.json` dilewati → run ulang aman untuk melanjutkan yang gagal.

## Sumber Email (Emailnator)

Dari HAR `www.emailnator.com.har`. Host `https://www.emailnator.com`.

| Method | Endpoint | Body | Response |
|---|---|---|---|
| GET | `/` | — | set cookie `XSRF-TOKEN` + `gmailnator_session` |
| POST | `/generate-email` | `{"email":["dotGmail"]}` | `{"email":["x@gmail.com"]}` |
| POST | `/message-list` | `{"email":"x@gmail.com"}` | `{"messageData":[{messageID,from,subject,time}]}` |
| POST | `/message-list` | `{"email":"...","messageID":"..."}` | isi email (HTML mentah) |
| POST | `/delete-message` | `{"email":"...","messageID":"..."}` | `{"status":"success"}` |

### Header wajib
- `X-XSRF-TOKEN: <cookie XSRF-TOKEN, URL-decoded>`
- `X-Requested-With: XMLHttpRequest`
- `Content-Type: application/json`
- `Origin` / `Referer`: `https://www.emailnator.com`

Token = Laravel encrypted cookie (`eyJpdiI6...`). Ambil dari cookie jar setelah `GET /`, URL-decode, kirim sebagai header.

### Jenis alamat
`dotGmail` (default — titik acak, inbox Gmail asli) | `plusGmail` | `googleMail` | `domain`

`messageID: "ADSVPN"` = iklan bawaan emailnator, difilter di `mailer.py`.

### Pakai
```bat
set EMAIL_SOURCE=emailnator
set EMAIL_COUNT=3
set CAPTCHA_PROVIDER=capsolver
set CAPTCHA_KEY=xxxxx
py signup.py
```
Alamat baru dicatat ke `akun.txt`. Dengan `EMAIL_SOURCE=emailnator` + solver aktif, fase 2 (signup) jalan **paralel** — captcha dan OTP dua-duanya otomatis.

`EMAIL_SOURCE=file` (default): baca `akun.txt`, OTP diketik manual.

## Catatan: transaksi B2C sekali pakai

Percobaan **login yang gagal** meninggalkan transaksi B2C (`tx` / StateProperties) dalam keadaan rusak. Kalau `tx` itu dipakai untuk signup, `SendCode` balas:

```
HTTP 500 - The page cannot be displayed because an internal server error has occurred.
```

Jadi setiap jalur signup **wajib** buka sesi baru (`Client()` + `start()` + `open_signup()`), bukan melanjutkan sesi yang sudah dipakai untuk login. Dijaga oleh `test_fresh_client_for_signup()`.

Error lain yang sudah dipetakan:

| Error | Arti | Penanganan |
|---|---|---|
| `NoChallengeSession` | challenge captcha expired | ambil captcha baru, ulangi |
| `WrongAnswer` (`isCaptchaSolved: False`) | captcha salah baca | ambil captcha baru, ulangi |
| `ViralErrorUserCreationConflict` | email sudah terdaftar | login dulu; kalau login juga gagal berarti password beda -> tukar alamat (emailnator) |
| `HTTP 500` di `SendCode` | `tx` bekas login gagal | sesi baru |
| `HTTP 403` di `/api/*` | `User-Agent` tak dikirim | selalu kirim UA |
| `HTTP 500` di `checkout-session` | session belum siap di backend | poll ulang, cek `plan` juga |

## Konfigurasi lewat .env

```bat
copy .env.example .env
```

Lalu isi `.env`:

```ini
CAPTCHA_PROVIDER=2captcha
CAPTCHA_KEY=api-key-2captcha-kamu
EMAIL_SOURCE=emailnator
EMAIL_COUNT=3
```

| Key | Default | Isi |
|---|---|---|
| `CAPTCHA_PROVIDER` | `manual` | `2captcha`, `capsolver`, `anticaptcha`, `capmonster`, `rucaptcha`, `azcaptcha` |
| `CAPTCHA_KEY` | — | API key provider |
| `CAPTCHA_TRIES` | `3` | ulang kalau solver salah baca |
| `EMAIL_SOURCE` | `file` | `file` (akun.txt) / `emailnator` (otomatis) |
| `EMAIL_COUNT` | `1` | jumlah akun kalau `emailnator` |
| `OTP_TIMEOUT` | `300` | detik menunggu OTP |
| `WORKERS` | `6` | worker paralel |
| `PASSWORD` | `Masuk@123456` | password semua akun |

Env var proses menimpa `.env`, jadi `set EMAIL_COUNT=5` tetap berlaku untuk sekali jalan.

`.env` masuk `.gitignore` bareng `accounts.json` dan `akun.txt` — semuanya berisi rahasia.

Saat start, saldo provider dicek dulu. Key salah/kosong -> berhenti sebelum boros captcha:

```
captcha: 2captcha (saldo 3.45)              <- siap
captcha: 2captcha - key ditolak: ERROR_WRONG_USER_KEY
captcha: 2captcha - CAPTCHA_KEY KOSONG, isi di .env dulu
```

Cek manual tanpa jalankan signup:
```bat
py solvers.py 2captcha api-key-kamu
```

## Alamat emailnator itu publik

`dotGmail` = Gmail asli dengan titik acak, inbox-nya terbuka di emailnator. Bisa saja alamat yang kamu dapat **sudah dipakai orang lain** dengan password berbeda. Akibatnya:

1. Fase 1 login gagal (password bukan `PASSWORD`)
2. Fase 2 signup kena `ViralErrorUserCreationConflict`

Penanganan:

- `EMAIL_SOURCE=emailnator` -> ambil alamat baru, ulangi sampai `EMAIL_TRIES` (default 3). Alamat memang sekali pakai, jadi menukar itu benar. Alamat pengganti dicatat ke `akun.txt`, dan `accounts.json` menyimpan alamat yang benar-benar dipakai.
- `EMAIL_SOURCE=file` -> **tidak** ditukar (alamat itu milikmu). Pesannya:
  ```
  x@gmail.com sudah terdaftar tapi password bukan 'Masuk@123456'.
  Set PASSWORD di .env sesuai akun itu, atau pakai email lain.
  ```

Tambahan `.env`: `EMAIL_TRIES=3`.

Untuk akun yang mau dipakai lama, jangan pakai emailnator - orang lain bisa baca OTP-nya dan reset password.

## Proxy

Isi `PROXY` di `.env` (pisah koma) dan/atau `proxy.txt` (satu per baris). Keduanya digabung, duplikat dibuang.

```ini
PROXY=http://user:pass@1.2.3.4:8080, socks5://5.6.7.8:1080
```

Tipe yang didukung:

| Skema | Catatan |
|---|---|
| `http` / `https` | stdlib `ProxyHandler` |
| `socks4` / `socks4a` | butuh PySocks |
| `socks5` / `socks5h` | butuh PySocks; akhiran `h`/`a` = DNS diresolve di proxy |

Format baris yang diterima:

```
http://user:pass@1.2.3.4:8080
socks5h://1.2.3.4:1080
1.2.3.4:8080                  <- tanpa skema = http
1.2.3.4:8080:user:pass        <- host:port:user:pass
```

Komentar (`#`) boleh di awal baris atau di ujung baris.

### Round-robin

`proxies.Pool` bergilir dan thread-safe (`itertools.cycle` sendirinya tidak, jadi dikunci).

**Satu proxy dipegang sepanjang satu percobaan** — `Client` dan inbox Emailnator memakai IP yang sama. Alasannya: B2C mengikat transaksi ke IP; ganti IP di tengah alur bikin transaksi ditolak.

Pool kosong -> koneksi langsung. IP yang dipakai tercatat di `accounts.json` (field `proxy`).

Install PySocks kalau pakai SOCKS:
```bat
pip install PySocks
```

## Cek sisa credit

```bat
py signup.py credit
```

Login paralel ke semua akun di `accounts.json`, cetak plan + credit + masa aktif, lalu perbarui `accounts.json`:

```
  c.o.n.toh.akun1@gmail.com
        plan=plus credit=9716 status=active sampai=2026-09-28T10:39:30

total credit: 29627
```

Endpoint: `GET /api/payment/get_credit_balance` -> `{"data":{"balance":10120}}`

Credit juga ikut disimpan otomatis setiap kali akun selesai diproses.

## Kenapa Stripe kadang tak minta kartu

Kalau kartu sudah tersimpan di akun Stripe (`stripeAccountEmail` terisi) dan coupon `first_month:<cogen_id>` bikin tagihan $0, checkout selesai tanpa form. `wait_paid()` sudah menangani ini: selain memantau `payment_status`, ia juga memeriksa `plan != free`, karena webhook Stripe kadang menaikkan plan lebih dulu daripada endpoint `checkout-session` melaporkan `paid`.

Verifikasi manual:
```bat
py signup.py credit
```
`plan=plus` + `status=active` = pembayaran benar-benar masuk.

### Retry ganti proxy

Proxy publik banyak yang mati. Kalau koneksi gagal, `with_proxy_retry()` mengambil proxy berikutnya dari pool dan mengulang percobaan itu dari awal (sesi B2C baru), sampai `PROXY_TRIES` (default 4).

```
[x@gmail.com] proxy bermasalah (Tunnel connection failed: 400 Bad Request), ganti proxy [1/4]
[x@gmail.com] proxy bermasalah ([WinError 10060] ...), ganti proxy [2/4]
```

Yang diulang **hanya** error jaringan:

| Gejala | Arti | Tindakan |
|---|---|---|
| `Tunnel connection failed: 400` | proxy tolak CONNECT (HTTPS tak didukung / auth kurang) | ganti proxy |
| `WinError 10060` timeout | proxy diam / kelebihan beban | ganti proxy |
| `connection reset` / `refused` | proxy tutup koneksi | ganti proxy |
| HTTP 4xx/5xx dari Genspark | jawaban sah server | **jangan** ganti, langsung gagal |
| `ViralErrorUserCreationConflict` | email sudah dipakai | tukar alamat, bukan proxy |
| captcha salah | solver salah baca | ambil captcha baru |

Pembeda ada di `proxies.is_network_error()`. `HTTPError` sengaja dikecualikan: ganti IP tak mengubah jawaban server, dan mengulang percobaan cuma memboroskan kuota captcha.

Pool kosong -> satu percobaan saja, tanpa retry.

Tambahan `.env`: `PROXY_TRIES=4`, `TIMEOUT=5`.

### Timeout

`TIMEOUT` (detik) membatasi tiap request. Default menyesuaikan: **5** kalau pool proxy terisi, **30** kalau koneksi langsung.

Alasannya proxy busuk punya dua cara gagal, dan yang kedua mahal:

| Gejala | Waktu gagal |
|---|---|
| `Tunnel connection failed: 400` | langsung, proxy menolak |
| `WinError 10060` / `SSL: UNEXPECTED_EOF` | menggantung sampai timeout |

Dengan timeout 30s, empat percobaan proxy diam = 2 menit terbuang per akun. Dengan 5s = 20 detik. Terukur: proxy yang listen tapi tak menjawab gagal tepat pada batas timeout.

Naikkan kalau proxy kamu lambat tapi sehat (`TIMEOUT=10`); turunkan kalau daftar proxy banyak sampahnya (`TIMEOUT=3`).

`TIMEOUT` **hanya** berlaku untuk request ke Genspark. Emailnator punya `MAIL_TIMEOUT` sendiri (default 30s, minimum 20s), karena jauh lebih lambat -- terukur:

| Endpoint | Waktu |
|---|---|
| `GET /` (bootstrap) | 0.1s |
| `generate-email` | 0.3s |
| `message-list` (inbox kosong) | ~1s |
| `message-list` (inbox berisi) | **5-6s** |

Dulu keduanya memakai satu nilai, jadi `TIMEOUT=5` demi proxy ikut memutus pembacaan inbox: pada inbox berisi hanya **1 dari 6** percobaan berhasil. Setelah dipisah: **6 dari 6**.

## Alamat Emailnator bisa satu inbox

Gmail mengabaikan titik: `a.b@gmail.com` dan `ab@gmail.com` adalah inbox yang **sama**, dan karena Genspark memakai email sebagai identitas, keduanya juga akun yang sama.

Emailnator mengacak posisi titik pada nama yang sama, jadi ia bisa memberi `a.n.dis.a.n.t.o@gmail.com` padahal `an.d.i.santo@gmail.com` sudah punya akun. Hasilnya `ViralErrorUserCreationConflict` yang membingungkan -- alamatnya kelihatan baru, tapi inbox-nya bukan.

`gmail_key()` menormalkan alamat (buang titik, huruf kecil), lalu `used_inboxes()` mengumpulkan kunci dari `accounts.json` **dan** `akun.txt`. `fresh_email()` meminta alamat baru sampai inbox-nya belum terpakai (maksimal 6 kali), termasuk mencegah tabrakan di dalam satu batch. Kalau tetap tabrakan, alur tukar-alamat yang menangani.

Cek duplikat di daftar sendiri:

```bat
py -c "import signup,collections;d=collections.Counter(signup.gmail_key(e) for e in open('akun.txt',encoding='utf-8') if e.strip() and not e.startswith('#'));print([k for k,v in d.items() if v>1])"
```

## Log inbox error diringkas

Membaca inbox Emailnator memang kadang gagal (`The read operation timed out`), dan itu ditelan lalu dicoba lagi -- bukan kegagalan akun. Tapi tiap kegagalan dulu dicetak satu baris, jadi satu inbox lambat bisa membanjiri layar puluhan baris identik dan menutupi keterangan akun lain yang jalan bersamaan.

Sekarang tiap jenis error dilaporkan **sekali** per akun, dan barisnya diberi nama akun supaya jelas milik siapa saat berjalan paralel. Jumlah kegagalan disebut di pesan timeout kalau OTP memang tak pernah datang:

```
[x@gmail.com] inbox: The read operation timed out (dicoba ulang, diam-diam)
[x@gmail.com] OTP 424242 dari Microsoft on behalf of Genspark
```

Terukur: 7 kegagalan berurutan menghasilkan 2 baris log, bukan 8.

## Kelihatan macet padahal jalan

Gejalanya: dua akun sudah dapat OTP, lalu tak ada keluaran apa pun.

Penyebabnya cara memanen hasil. Pola lama memanen berurutan submit:

```python
for email, fut in [(e, pool.submit(kerja, e)) for e in daftar]:
    fut.result()          # menunggu akun PERTAMA, walau yang lain sudah kelar
```

Terukur, empat pekerjaan (akun pertama 3s, sisanya 0.1s):

| Pola | Laporan pertama |
|---|---|
| berurutan submit | 3.0s (semua sekaligus di akhir) |
| `as_completed` | 0.1s |

Jadi akun yang sudah selesai tetap diam sampai akun terlambat kelar. Sekarang keempat fase memakai `as_completed`.

Ditambah tanda hidup, karena menunggu OTP dan menunggu kartu memang lama:

```
[x@gmail.com] masih nunggu OTP (30s dari 300s, 2 gagal baca)
[x@gmail.com] masih nunggu kartu diisi (60s dari 900s)
```

### Gagal baca body tak lagi mematikan akun

`wait_otp` dulu memanggil `mail.body()` tanpa pelindung, padahal Emailnator sering timeout. Sekali gagal, akun itu langsung mati meski percobaan berikutnya akan berhasil. Selain itu pesan langsung ditandai sudah-dibaca sebelum isinya diperiksa, jadi email OTP yang belum lengkap saat diambil tak pernah dicek lagi.

Sekarang: `body()` dibungkus, dan hanya pesan yang **jelas bukan** dari pengirim yang dicari yang ditandai lewati. Pesan dari pengirim yang benar dicoba ulang sampai OTP-nya terbaca.

### Tab Stripe dibuka berjarak

Lima akun berarti lima tab checkout. Karena kartu diisi manusia satu per satu, tab dibuka berjarak `TAB_DELAY` detik (default 3) sambil polling tetap paralel.

Tambahan `.env`: `TAB_DELAY=3`.
