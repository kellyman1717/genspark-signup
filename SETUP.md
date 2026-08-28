# Setup di device baru

## 1. Yang dibutuhkan

- **Python 3.8+** — cek: `py --version`
- **Git** — untuk clone

Alur inti memakai pustaka standar Python saja, jadi tanpa install apa pun sudah
jalan. Satu paket opsional (`PySocks`) hanya dibutuhkan kalau `PROXY` memakai
skema `socks4`/`socks5`; proxy `http`/`https` jalan dengan stdlib.

```bat
pip install -r requirements.txt    :: hanya kalau pakai proxy SOCKS
```

Tanpa itu, proxy SOCKS berhenti dengan pesan yang menyebutkan paketnya:
`proxy socks5 butuh PySocks: pip install PySocks`

## 2. Clone dan konfigurasi

```bat
git clone https://github.com/kellyman1717/genspark-signup.git
cd genspark-signup
copy .env.example .env
```

Buka `.env`, isi tiga hal ini minimal:

```ini
CAPTCHA_PROVIDER=2captcha
CAPTCHA_KEY=api-key-2captcha-kamu
PASSWORD=PasswordPilihanmu@2026
```

`PASSWORD` **wajib** — tak ada nilai default di kode. Syarat Azure B2C: huruf
besar + kecil, angka, simbol, minimal 8 karakter.

## 3. Pastikan jalan

```bat
py test_har.py           :: 14 self-check; yang butuh HAR dilewati
py solvers.py 2captcha api-key-kamu   :: cek key valid + saldo
```

`py test_har.py` harus berakhir `SEMUA LOLOS`. Satu test dilewati dengan pesan
`SKIP: HAR tak ada` — itu normal, hanya test parsing terhadap HAR trace asli
yang tak ikut repo.

## 4. Jalankan

```bat
py signup.py           :: proses akun
py signup.py credit    :: cek sisa credit tiap akun
```

## Yang TIDAK ikut repo

File-file ini masuk `.gitignore` karena berisi data pribadi, jadi di device
baru memang belum ada:

| File | Isi | Perlu disalin? |
|---|---|---|
| `.env` | API key, password | Buat dari `.env.example` |
| `accounts.json` | email, API key Genspark, credit | **Ya**, kalau mau lanjut akun lama |
| `akun.txt` | daftar email | Opsional; `EMAIL_SOURCE=emailnator` bikin sendiri |
| `proxy.txt` | daftar proxy | Opsional |

### Memindahkan akun lama

`accounts.json` adalah satu-satunya file yang berisi hasil kerja: API key
Genspark tiap akun. Kalau hilang, akunnya tak hilang — tapi API key-nya tak
bisa dibaca ulang dari Genspark, jadi harus dibuat baru.

Salin manual (jangan lewat Git, isinya rahasia):

```bat
:: dari device lama
copy accounts.json <flashdisk-atau-lokasi-aman>
```

Setelah `accounts.json` ada, jalankan `py signup.py credit` untuk memastikan
semua akun masih bisa login dan credit-nya terbaca.

Kalau `PASSWORD` di device baru **berbeda** dari yang dipakai saat akun dibuat,
login akan gagal dengan pesan:

```
x@gmail.com sudah terdaftar tapi password bukan '<PASSWORD di .env>'.
```

Isi `PASSWORD` sesuai password akun-akun itu.

## Catatan

- Alamat Emailnator (`dotGmail`) itu **publik** — inbox bisa dibaca siapa pun
  yang men-generate alamat sama. Jangan pakai untuk akun yang mau dipakai lama.
- Kartu pembayaran diisi manual di browser. Skrip tidak menyimpan atau
  memproses data kartu.
- Jalankan ulang aman: akun yang sudah ada di `accounts.json` dilewati, dan
  akun yang sudah berbayar tidak ditagih dua kali.
