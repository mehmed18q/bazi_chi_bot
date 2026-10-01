# راهنمای به‌روزرسانی ربات روی سرور

این دستورالعمل برای استقرار نمونهٔ همین مخزن است: پروژه در
`/opt/bazi_chi_bot`، سرویس `bazi_chi_bot.service` و دیتابیس SQLite. اگر مسیرها،
کاربر سرویس یا روش اجرا روی سرور شما متفاوت است، فرمان‌ها را با همان تنظیمات واقعی
تطبیق دهید. فرمان‌ها را با کاربری اجرا کنید که به مخزن و `.venv` دسترسی نوشتن دارد؛
برای عملیات سیستمی از `sudo` استفاده شده است. **هیچ‌یک از این مراحل، دستور
`reset_database` را اجرا نمی‌کند.**

چالش روزانه از ساعت ۱۲ تا ۱۳ به وقت تهران باز است. تا حد ممکن به‌روزرسانی را بیرون
از این بازه انجام دهید و مطمئن شوید ساعت سرور همگام است. فقط یک نمونه از ربات با
همین توکن در حال polling باشد.

## اجرای خودکار

برای استقرار نمونهٔ بالا، از کاربری که به مخزن و `.venv` دسترسی نوشتن و مجوز
`sudo` دارد، اجرا کنید:

```bash
cd /opt/bazi_chi_bot
bash scripts/deploy.sh
```

اسکریپت فقط روی شاخهٔ تمیز `master` اجرا می‌شود؛ نسخه را از `origin/master` با
fast-forward می‌گیرد، سرویس را متوقف می‌کند، از `.env`، واحد systemd و SQLite
پشتیبان می‌گیرد، وابستگی‌ها و تست‌ها را اجرا می‌کند و سرویس را دوباره بالا می‌آورد.
اگر `master` از قبل به‌روز باشد (مثلاً `git pull` را دستی زده باشید)، همین مراحل
برای نسخهٔ فعلی نیز اجرا می‌شوند و دیگر بدون بک‌آپ و restart خارج نمی‌شود. برای
محیط مجازی فاقد `pip` نیز، اگر `uv` نصب باشد از آن برای نصب وابستگی‌ها استفاده
می‌کند؛ محیط مجازی بازسازی نمی‌شود.
فایل دیتابیس مستقیم در `/var/lib/data/backup` با نامی مانند
`bazi_chi_bot(2026-10-01T09-30-00.123456789Z).sqlite3` ذخیره می‌شود؛ زمان نام فایل
UTC است. `.env`، فایل سرویس و شناسهٔ commit در یک زیرپوشهٔ جدا در همان مسیر
می‌مانند. هر دو مسیر و commitها در خروجی چاپ می‌شوند. اگر آپدیت، فایل واحد systemd را تغییر
دهد ولی نسخهٔ نصب‌شده یا drop-inهای سرور سفارشی باشند، اسکریپت **قبل از توقف
سرویس** متوقف می‌شود. اگر خطا پس از تغییر کد یا هنگام دیپلوی دوبارهٔ همین نسخه رخ
دهد، برای جلوگیری از اجرای کد و دیتابیس ناسازگار، سرویس متوقف می‌ماند؛ بخش
«بازگشت در صورت خطا» را دنبال کنید. بررسی عملی
ربات در تلگرام همچنان لازم است. برای فرمان‌های بازگشت، مسیر زیرپوشهٔ اطلاعات
استقرار چاپ‌شده را ابتدا در متغیر `bot_backup_dir` قرار دهید؛ مسیر فایل دیتابیس
در `database-backup-path.txt` همان زیرپوشه ثبت می‌شود. متغیرهای داخل اسکریپت به
shell شما منتقل نمی‌شوند.

بخش‌های بعد، روش دستی و راهنمای بازگشت را توضیح می‌دهند.

## ۱. بررسی پیش از توقف

```bash
cd /opt/bazi_chi_bot
sudo systemctl status bazi_chi_bot.service --no-pager
git status --short
git branch --show-current
git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
git rev-parse HEAD
git fetch --prune
git log --oneline HEAD..'@{upstream}'
git diff --stat HEAD..'@{upstream}'
```

اگر `git status --short` خروجی دارد یا شاخه upstream ندارد، همین‌جا متوقف شوید و
علت را بررسی کنید؛ تغییرات محلی یا فایل‌های داده را با `reset --hard`، `clean` یا
stash خودکار کنار نگذارید. تغییرات ورودی، مخصوصاً migrationها و فایل سرویس را
مرور کنید. نسخهٔ فعلی (`git rev-parse HEAD`) را برای بازگشت نگه دارید.

## ۲. توقف و پشتیبان‌گیری

توقف سرویس قبل از پشتیبان‌گیری، نوشتن هم‌زمان در دیتابیس را متوقف می‌کند. مسیر
پشتیبان را یادداشت کنید؛ همهٔ فرمان‌های این بخش و بخش بازگشت باید در همان نشست shell
یا با مقدار همان مسیر اجرا شوند.

```bash
sudo systemctl stop bazi_chi_bot.service
umask 077
if [ ! -d /var/lib/data/backup ]; then
  sudo install -d -m 0700 /var/lib/data/backup
fi
bot_backup_stamp="$(date -u +%Y-%m-%dT%H-%M-%S.%NZ)"
bot_backup_dir="$(sudo mktemp -d "/var/lib/data/backup/deploy-${bot_backup_stamp}.XXXXXX")"
bot_db_backup="/var/lib/data/backup/bazi_chi_bot(${bot_backup_stamp}).sqlite3"
git rev-parse HEAD | sudo tee "$bot_backup_dir/previous-commit.txt" >/dev/null
sudo install -m 0600 .env "$bot_backup_dir/.env"
if [ -f /etc/systemd/system/bazi_chi_bot.service ]; then
  sudo install -m 0600 /etc/systemd/system/bazi_chi_bot.service "$bot_backup_dir/bazi_chi_bot.service"
fi
```

مسیر واقعی دیتابیس از `DATABASE_PATH` در `.env` خوانده می‌شود. به‌دلیل حالت WAL،
**فقط فایل `.sqlite3` را با `cp` کپی نکنید**؛ API پشتیبان‌گیری SQLite یک تصویر سازگار
از دیتابیس می‌سازد. فرمان زیر همچنین سلامت پشتیبان را بررسی می‌کند و در صورت خطا
باید به‌روزرسانی متوقف شود.

```bash
sudo env BOT_BACKUP_PATH="$bot_db_backup" .venv/bin/python - <<'PY'
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from bazi_chi_bot.config import Settings

source_path = Settings().database_path.resolve()
if not source_path.is_file():
    raise SystemExit(f"Database not found: {source_path}")
backup_path = Path(os.environ["BOT_BACKUP_PATH"])
try:
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    raise SystemExit(f"Database backup already exists: {backup_path}") from None
else:
    os.close(descriptor)
with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
    with closing(sqlite3.connect(backup_path)) as backup:
        source.backup(backup)
        result = backup.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"Backup integrity check failed: {result}")
backup_path.chmod(0o600)
print(f"Database backup OK: {backup_path}")
PY
printf '%s\n' "$bot_db_backup" | sudo tee "$bot_backup_dir/database-backup-path.txt" >/dev/null
sudo chmod 0600 "$bot_backup_dir/database-backup-path.txt"
```

اگر ابزار دعوت از اکسل را هم روی همین سرور استفاده می‌کنید، از داده‌های مستقل آن
در `data/outreach/` نیز طبق سیاست پشتیبان‌گیری خود نسخه بگیرید. `.env` و پشتیبان‌ها
حاوی دادهٔ حساس‌اند؛ آن‌ها را در Git یا مسیر عمومی قرار ندهید.

## ۳. دریافت نسخه، نصب و آزمون

```bash
git pull --ff-only
.venv/bin/python --version
.venv/bin/pip install -e '.[test,outreach]'
.venv/bin/pytest -q
```

نسخهٔ Python باید حداقل ۳.۱۴ باشد. اگر `.venv` موجود نیست، پیش از نصب با
`python3.14 -m venv .venv` آن را بسازید. گزینهٔ `outreach` برای وابستگی‌های
`tests/test_outreach_script.py` لازم است؛ اجرای خود ربات به آن نیاز ندارد.

تست بانک سؤال فقط خواندنی است، اما مسیر ثابت `data/bazi_chi_bot.sqlite3` را بررسی
می‌کند. اگر `DATABASE_PATH` سرور به مسیر دیگری اشاره می‌کند، به‌جای دستور آخر،
فرمان زیر را اجرا کنید و بانک واقعی را جداگانه با اتصال فقط‌خواندنی بررسی کنید:

```bash
.venv/bin/pytest -q --ignore=tests/test_question_bank.py
.venv/bin/python - <<'PY'
import sqlite3
from contextlib import closing

from bazi_chi_bot.config import Settings

database_path = Settings().database_path.resolve()
with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)) as database:
    counts = dict(database.execute(
        "SELECT kind, COUNT(*) FROM questions WHERE active = 1 GROUP BY kind"
    ).fetchall())
    empty_count = database.execute(
        "SELECT COUNT(*) FROM questions WHERE text IS NULL OR trim(text) = ''"
    ).fetchone()[0]
if counts.get("truth", 0) < 1 or counts.get("dare", 0) < 1 or empty_count:
    raise SystemExit(f"Question bank invalid: active={counts}, empty={empty_count}")
print(f"Question bank OK: active={counts}, empty={empty_count}")
PY
```

شکست هر آزمون یا نبود سؤال، مانع راه‌اندازی نسخهٔ جدید است.

اگر فایل `bazi_chi_bot.service` در نسخهٔ جدید تغییر کرده، ابتدا آن را با واحد نصب‌شده
و هر drop-in سفارشی (`sudo systemctl cat bazi_chi_bot.service`) مقایسه کنید. فقط پس
از اطمینان از حفظ تنظیمات سرور، فایل جدید را نصب و systemd را بارگذاری کنید:

```bash
sudo install -m 0644 bazi_chi_bot.service /etc/systemd/system/bazi_chi_bot.service
sudo systemctl daemon-reload
```

این دو فرمان را در هر آپدیت بدون بررسی اجرا نکنید؛ ممکن است واحد فعلی سرور سفارشی
باشد. migrationهای SQLite هنگام شروع ربات خودکار اجرا می‌شوند و به ریست دیتابیس
نیازی نیست.

## ۴. راه‌اندازی و تأیید

```bash
sudo systemctl start bazi_chi_bot.service
sudo systemctl is-active bazi_chi_bot.service
sudo systemctl status bazi_chi_bot.service --no-pager
sudo journalctl -u bazi_chi_bot.service -n 100 --no-pager
```

باید وضعیت `active` باشد و خطای اتصال تلگرام یا migration در لاگ نبینید. در گفت‌وگوی
خصوصی با ربات، `/start`، منوی بازی و یک تعامل متناسب با ساعت روز را امتحان کنید؛
در بازهٔ ۱۲ تا ۱۳ تهران، بازشدن چالش روزانه و پس از ۱۳ بسته‌بودن آن را نیز بررسی
کنید. اگر سرویس بالا نیامد، پیش از تلاش‌های مکرر علت را در لاگ بررسی کنید.

## بازگشت در صورت خطا

اگر نسخهٔ جدید **هنوز اجرا نشده**، دیتابیس مهاجرت نکرده و معمولاً بازگشت کد و
وابستگی‌ها کافی است. اگر حتی یک‌بار اجرا شده، ممکن است schema تغییر کرده باشد؛
کد قدیمی را به‌تنهایی روی دیتابیس جدید اجرا نکنید. ابتدا سرویس را متوقف کنید،
نسخهٔ سازگار کد و دیتابیس را از **همان پشتیبان** برگردانید، سپس سرویس را شروع کنید.
بازگرداندن دیتابیس، داده‌های ثبت‌شده پس از زمان پشتیبان را از دست می‌دهد؛ پیش از آن
دربارهٔ حفظ این داده‌ها تصمیم بگیرید.

```bash
cd /opt/bazi_chi_bot
sudo systemctl stop bazi_chi_bot.service
git status --short
git switch --detach "$(sudo cat "$bot_backup_dir/previous-commit.txt")"
sudo install -m 0600 "$bot_backup_dir/.env" .env
```

اگر worktree تمیز نیست، قبل از `git switch` تغییرات را بررسی کنید و چیزی را کورکورانه
حذف نکنید. برای بازگرداندن دیتابیس، در حالی که هیچ فرایند دیگری از آن استفاده
نمی‌کند، از API SQLite استفاده کنید؛ فایل اصلی و فایل‌های `-wal` و `-shm` را دستی
جابه‌جا یا حذف نکنید:

```bash
bot_db_backup="$(sudo cat "$bot_backup_dir/database-backup-path.txt")"
sudo env BOT_BACKUP_PATH="$bot_db_backup" .venv/bin/python - <<'PY'
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from bazi_chi_bot.config import Settings

backup_path = Path(os.environ["BOT_BACKUP_PATH"])
database_path = Settings().database_path.resolve()
with closing(sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)) as backup:
    with closing(sqlite3.connect(database_path)) as database:
        backup.backup(database)
        result = database.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"Restored database integrity check failed: {result}")
print(f"Database restored: {database_path}")
PY
.venv/bin/pip install -e '.[test,outreach]'
```

اگر واحد systemd را هم تغییر داده‌اید، نسخهٔ آن را از همین پوشهٔ پشتیبان برگردانید
و `sudo systemctl daemon-reload` بزنید. سپس:

```bash
sudo systemctl start bazi_chi_bot.service
sudo systemctl is-active bazi_chi_bot.service
sudo journalctl -u bazi_chi_bot.service -n 100 --no-pager
```

بررسی‌های عملی بخش قبل را هم تکرار کنید. `git switch --detach` برای بازگشت
اضطراری است؛ پیش از انتشار بعدی، شاخهٔ اصلی و وضعیت آن را آگاهانه بازبینی کنید.
چون پروژه lockfile وابستگی ندارد، نصب دوباره لزوماً نسخهٔ دقیق همهٔ پکیج‌های قبلی
را برنمی‌گرداند.
